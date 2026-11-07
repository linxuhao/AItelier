"""One owned DSH process per State project; State remains the task authority."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from pathlib import Path
import signal
import sys
import time
import uuid

from core.driver_credentials import read_token_file
from core.state_graph import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS dsh_drivers (
    project_id TEXT PRIMARY KEY,
    driver_id TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'stopped',
    provider TEXT NOT NULL, model TEXT NOT NULL,
    session_id TEXT NOT NULL,
    subagents_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Which models actually answered each driver session. The completion endpoint
-- chose the endpoint (gateway.active_model) and then kept nothing: the child's
-- own session file only ever recorded the route alias, so a review criterion
-- keyed on the writer's model family had no measured value to cite.
--
-- `served_by` is the LAST endpoint this session could NAME - not "the endpoint
-- of the last turn". Those are different facts the moment a turn cannot name
-- one (the gateway answered but could not attribute the answer, or handed back
-- the route argument it was given): such a turn leaves the known endpoint in
-- place, because one unattributable turn is not evidence that the earlier
-- attribution was wrong, and overwriting it would erase the only measured
-- identity the session ever had. NULL means this session has named NO endpoint
-- yet - never a placeholder, and never `route` (an alias in this column would
-- read as if `worker` had answered).
--
-- `served_by_seen` is every endpoint the session named, as JSON text: first-seen
-- order, no duplicates, capped at SERVED_BY_SEEN_LIMIT distinct values (a
-- session that exhausted the cap keeps the FIRST ones and `served_by` still
-- names the latest). One column would answer "who spoke last" for a session
-- that failed over mid-life and so mis-report which model family wrote it; the
-- list makes that a property of the session rather than of its final turn.
--
-- One row per (project, session): a later call moves `last_seen` and `calls`,
-- `first_seen` stays.
CREATE TABLE IF NOT EXISTS dsh_driver_session_models (
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    route TEXT NOT NULL,
    served_by TEXT,
    served_by_seen TEXT NOT NULL DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, session_id)
);
"""

# How many distinct endpoints one session's `served_by_seen` keeps. Small on
# purpose: the question it answers is "which model families wrote this", and a
# session that named more than a handful of endpoints is a routing story, not a
# list anybody reads. The FIRST endpoints are kept (that is the order the column
# promises) even after the cap; `served_by` still names the latest one.
SERVED_BY_SEEN_LIMIT = 8


# Local MCP endpoints the driver is given by default on this host. Ports match
# the services that already run here: agentmcp (asr/ocr/pdf/web + media + video)
# on 9003 and dev-tools-mcp (files, bash, subagent jobs) on 7676. Both are plain
# streamable-HTTP and need no token from loopback - agentmcp's gate only demands
# one beyond the Cloudflare edge.
DSH_EXTRA_MCP_DEFAULTS = {
    "devmcp": "http://127.0.0.1:7676/mcp",
    "zvec": "http://127.0.0.1:7999/mcp",
    # Media definitions stay behind caller-scoped search_tools/call_tool for
    # the driver and native children. Blank the URL to disable this service.
    "agentmcp": "http://127.0.0.1:9003/mcp",
}


class DshDrivers:
    def __init__(self, db, registry, root: Path, *, command=None, base_url=None, host_controller=False):
        self.db, self.registry = db, registry
        self.root = Path(root)
        self.command = command or [os.getenv("AITELIER_DSH_BIN", "/opt/aitelier-dsh/node_modules/.bin/dsh")]
        self.base_url = base_url or os.getenv("AITELIER_DSH_API_URL", "http://127.0.0.1:4444")
        self.processes = {}
        self.monitors = set()
        self.gateways = {}
        self.lock = asyncio.Lock()
        self.closing = False
        self.host_controller = host_controller
        with db.get_connection() as conn:
            conn.executescript(SCHEMA)
            # Additive: `CREATE TABLE IF NOT EXISTS` leaves an existing table
            # alone, so the column that records EVERY endpoint a session named
            # has to be added to a database that predates it. The rows already
            # there keep the one endpoint they named; they are not re-derived.
            if "served_by_seen" not in {r["name"] for r in conn.execute(
                    "PRAGMA table_info(dsh_driver_session_models)")}:
                conn.execute("ALTER TABLE dsh_driver_session_models ADD COLUMN "
                             "served_by_seen TEXT NOT NULL DEFAULT '[]'")
                # A row written before the column existed named exactly one
                # endpoint (or none) - `served_by` is that list of length <= 1.
                for row in conn.execute("SELECT project_id,session_id,route,served_by "
                                        "FROM dsh_driver_session_models "
                                        "WHERE served_by IS NOT NULL AND served_by<>route").fetchall():
                    conn.execute("UPDATE dsh_driver_session_models SET served_by_seen=? "
                                 "WHERE project_id=? AND session_id=?",
                                 (json.dumps([row["served_by"]]), row["project_id"], row["session_id"]))
            # The invariant this column enforces also has to hold for what is
            # already stored - and on EVERY start, not only when the column is
            # added: a row whose `served_by` IS the route alias claims the alias
            # answered, which is exactly the defect, and a build older than this
            # one can still write such a row into this database (a rollback to an
            # older image, or a second deployment sharing the file). Measured
            # consequence of skipping it on an already-migrated database: the
            # alias is read back as if it were an endpoint
            # (repro/probe_alias_cleanup_boundary.py, kept with the delivery).
            conn.execute("UPDATE dsh_driver_session_models SET served_by=NULL "
                         "WHERE served_by=route")
            conn.commit()

    @property
    def external_host(self):
        """The shared installation setting selects the host controller, not uvicorn."""
        if self.host_controller:
            return False
        try:
            return json.loads((self.root / "runtime.json").read_text()).get("mode") == "host"
        except FileNotFoundError:
            return False

    def running(self, project_id):
        if not self.external_host:
            proc = self.processes.get(project_id)
            return proc is not None and proc.returncode is None
        row = self.get(project_id)
        if row is None or not row["enabled"]:
            return False
        try:
            state = json.loads((self.root / row["driver_id"] / "host-process.json").read_text())
            age = time.time() - state["seen_at"]
            return state["session_id"] == row["session_id"] and bool(state["pid"]) and 0 <= age < 15
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def rows(self):
        with self.db.get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM dsh_drivers ORDER BY project_id")]

    def get(self, project_id):
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM dsh_drivers WHERE project_id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def update(self, project_id, **fields):
        allowed = {"enabled", "phase", "subagents_json", "last_error"}
        if not fields.keys() <= allowed:
            raise ValueError("unsupported driver update")
        fields["updated_at"] = now()
        with self.db.get_connection() as conn:
            conn.execute("UPDATE dsh_drivers SET " + ",".join(k + "=?" for k in fields) +
                         " WHERE project_id=?", (*fields.values(), project_id))
            conn.commit()

    def _discard_provision(self, project_id, driver_id, directory, actor):
        """Undo a provisioning attempt that lost the insert race.

        Best effort and never fatal: the caller has already established that
        another driver owns this project, so a failure to tidy up must not turn
        into a startup error. The directory goes last, after the membership, so a
        partial cleanup leaves a registered identity WITHOUT a usable token
        rather than a token belonging to nobody.
        """
        try:
            self.registry.set_membership(project_id, driver_id, "removed", 1,
                                         "lost DSH provisioning race", actor=actor,
                                         project_exists=lambda _: True)
        except Exception:
            pass
        shutil.rmtree(directory, ignore_errors=True)

    def _provision(self, project_id, provider, model, actor):
        if self.registry is None:
            raise ValueError("driver identity must be enabled to start a DSH driver")
        with self.db.get_connection() as conn:
            if not conn.execute("SELECT 1 FROM state_projects WHERE project_id=?", (project_id,)).fetchone():
                raise ValueError("State project does not exist")
        row = self.get(project_id)
        if row:
            return row
        # Do not derive a driver id from a possibly long or non-ASCII project id.
        driver_id = "dsh-" + uuid.uuid4().hex[:20]
        directory = self.root / driver_id
        directory.mkdir(parents=True, mode=0o700)
        registered = self.registry.register(driver_id, f"DSH · {project_id}"[:200],
                                            capabilities={"runtime": "dsh", "project_id": project_id}, actor=actor)
        token = directory / "token"
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(registered["token"] + "\n")
        self.registry.set_membership(project_id, driver_id, "member", 0,
                                     "Managed per-project DSH driver", actor=actor,
                                     project_exists=lambda _: True)
        try:
            with self.db.get_connection() as conn:
                conn.execute("INSERT INTO dsh_drivers(project_id,driver_id,provider,model,session_id,updated_at) "
                             "VALUES(?,?,?,?,?,?)", (project_id, driver_id, provider, model,
                                                    "aitelier-" + driver_id, now()))
                conn.commit()
        except sqlite3.IntegrityError:
            # The check-then-insert above is not atomic across processes: the host
            # supervisor and a host-mode backend can both provision a project whose
            # row does not exist yet. The primary key picks a winner, but the loser
            # has already created a directory, written a token and registered a
            # second identity - an orphan holding a token for a project another
            # driver id already manages. Undo our own side effects and hand back
            # the winner's row: this is cleanup, not a contract change, and the
            # winner is untouched.
            self._discard_provision(project_id, driver_id, directory, actor)
            row = self.get(project_id)
            if row is None:  # the winner's row vanished too; report honestly
                raise
            return row
        return self.get(project_id)

    def _environment(self, row):
        directory = self.root / row["driver_id"]
        token = directory / "token"
        read_token_file(token)  # Explicit refusal rather than silently using owner credentials.
        profile = directory / "home" / "profiles" / "driver"
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        (profile / "package.json").write_text(json.dumps({"private": True, "dsh": {
            "profile": {"bundles": ["@deepseek-ai/dsh-base"]}}}))
        source = Path(__file__).resolve().parents[1] / "integrations" / "dsh-driver" / "runtime.mjs"
        # JSON is YAML too; the runtime installs its own native State/pipeline tools.
        # Local MCP services the driver may use, each switchable independently:
        # the URL is the switch. A blank URL means "do not register it", so an
        # operator can take one away without editing code, and a host that does
        # not run these services keeps exactly the tool set it had before.
        #
        # failOnStartupError stays False for these: they are capabilities, not
        # prerequisites. A service that is down must cost the driver its tools,
        # not its ability to start - which is why the State MCP above is the only
        # one that fails hard.
        extra = []
        for server, default_url in DSH_EXTRA_MCP_DEFAULTS.items():
            url = os.environ.get(
                "AITELIER_DSH_MCP_" + server.upper(), default_url).strip()
            if not url:
                continue
            extra.append({"id": "mcp-" + server, "name": "@deepseek-ai/dsh-mcp-client", "config": {
                "serverName": server, "transport": "streamable-http", "url": url,
                "failOnStartupError": False, "toolCallTimeoutMs": 960000}})
        # Auto-compact fires at `min(W * thresholdRatio, W - O - B)`; the shipped
        # ratio is 0.8. It is a RATIO of the model's own window, not a token
        # count: 0.25 is 250k on a 1M window and 50k on a 200k one.
        #
        # The director's own route compacts near-clear (retainTokens 0 keeps only
        # the last balanced step): its work survives compaction by design - the
        # State snapshot is re-injected after every successful compaction - and
        # the verbatim tail was most of what a compaction left behind (measured
        # 2026-11-06: 245-316k left at a 500k trigger with the shipped 16% tail,
        # ~50k after idle compaction, which retains nothing). Workers have no such
        # snapshot: every other route keeps the previous policy (half the window,
        # DSH's default 16% verbatim tail).
        ratio = os.environ.get("AITELIER_DSH_COMPACT_RATIO", "0.25").strip()
        try:
            ratio = min(0.8, max(0.2, float(ratio)))
        except ValueError:
            ratio = 0.25
        # dsh-base's tool-result-pruner runs first at pressure and trims old tool
        # outputs in place. Measured 2026-11-06 (wuxia-myth, 253k): a 21.5k trim
        # brought the context back under the trigger, so the summary never ran,
        # and the rewrite mid-history cost a 165k uncached re-prefill; the session
        # then hovers just under the trigger. Without it, pressure goes straight
        # to the summary (the director's near-clear above).
        patches = [
            {"id": "hmr", "disabled": True},
            {"id": "tool-result-pruner", "disabled": True},
            {"id": "compaction-basic", "config": {
                "thresholdRatio": 0.5,
                "modelPolicies": [{"provider": row["provider"], "model": row["model"],
                                   "thresholdRatio": ratio, "retainTokens": 0}]}},
            *[{"id": tool, "config": {"provider": provider, "toolName": name,
                "agentOptions": {"provider": "aitelier", "model": "worker"}}}
              for tool, provider, name in (("tool-subagent", "spawn", "subagent"),
                                           ("tool-subagent-fork", "fork", "subagent_fork"))],
            {"insert": [{"id": "aitelier-driver", "name": str(source)},
                {"id": "mcp-aitelier", "name": "@deepseek-ai/dsh-mcp-client", "config": {
                    "serverName": "aitelier", "transport": "streamable-http",
                    "url": self.base_url.rstrip("/") + "/mcp",
                    "headers": {"X-AItelier-Driver-Token": "__DRIVER_TOKEN__"},
                    "failOnStartupError": True, "toolCallTimeoutMs": 960000}}] + extra},
        ]
        import yaml
        (profile / "cordis.patch.yml").write_text(yaml.safe_dump(patches, sort_keys=False).replace(
            "__DRIVER_TOKEN__", "!!js process.env.AITELIER_DRIVER_TOKEN"))
        env = dict(os.environ)
        for name in ("AITELIER_ADMIN_TOKEN", "AITELIER_DRIVER_TOKEN", "AITELIER_DRIVER_ID",
                     "AITELIER_DRIVER_TOKEN_FILE", "DSH_HOME"):
            env.pop(name, None)
        env.update(DSH_HOME=str(directory / "home"), AITELIER_DRIVER_ID=row["driver_id"],
                   AITELIER_DRIVER_TOKEN=read_token_file(token),
                   AITELIER_DRIVER_TOKEN_FILE=str(token), AITELIER_DSH_PROJECT=row["project_id"],
                   AITELIER_DSH_SESSION=row["session_id"], AITELIER_DSH_PROVIDER=row["provider"],
                   AITELIER_DSH_MODEL=row["model"], AITELIER_URL=self.base_url,
                   AITELIER_DSH_STATE_DIR=str(directory),
                   AITELIER_DSH_ENTRY=str(Path(self.command[0]).resolve()),
                   # The runtime's todo_* tools run `python -m core.todo_cli` from this checkout.
                   AITELIER_TODO_PYTHON=sys.executable,
                   AITELIER_TODO_CWD=str(Path(__file__).resolve().parents[1]))
        if self.host_controller:
            env.update(DSH_PERMISSION_MODE="danger-full-access", AITELIER_DSH_EXECUTION="host",
                       AITELIER_DSH_BACKEND_SOURCE=str(Path(__file__).resolve().parents[1]))
        return env

    async def start(self, project_id, *, provider="aitelier", model="driver", actor):
        async with self.lock:
            if self.closing:
                raise ValueError("driver manager is shutting down")
            if self.external_host:
                row = self._provision(project_id, provider, model, actor)
                if not row["enabled"]:
                    self.update(project_id, enabled=1, phase="starting", last_error="", subagents_json="[]")
                return self.public(self.get(project_id))
            proc = self.processes.get(project_id)
            if proc is not None and proc.returncode is None:
                return self.public(self.get(project_id))
            row = self._provision(project_id, provider, model, actor)
            directory = self.root / row["driver_id"]
            try:
                env = self._environment(row)
                self.update(project_id, enabled=1, phase="starting", last_error="", subagents_json="[]")
                log_fd = os.open(directory / "runtime.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(log_fd, "ab") as log:
                    proc = await asyncio.create_subprocess_exec(*self.command, "--profile", "driver",
                        env=env, cwd=directory, stdout=log, stderr=log, start_new_session=True)
                self.processes[project_id] = proc
                task = asyncio.create_task(self._observe(project_id, proc))
                self.monitors.add(task)
                task.add_done_callback(self.monitors.discard)
            except (OSError, ValueError) as exc:
                self.update(project_id, enabled=0, phase="failed", last_error=str(exc))
                raise ValueError(str(exc)) from exc
            return self.public(self.get(project_id))

    async def _observe(self, project_id, proc):
        code = await proc.wait()
        if self.processes.get(project_id) is proc:
            self.processes.pop(project_id, None)
            row = self.get(project_id)
            # The row can be deleted between the exit and this read. Reading
            # through None raised inside a task whose only consumer is
            # `add_done_callback(self.monitors.discard)`, so nobody retrieved the
            # exception: it surfaced only as "Task exception was never
            # retrieved" while the project kept a phase nobody updated. A deleted
            # project has no phase to set, so there is nothing to do.
            if row is not None and row["enabled"] and not self.closing:
                self.update(project_id, phase="failed", subagents_json="[]",
                            last_error=f"DSH exited with code {code}; see runtime.log")

    async def stop(self, project_id, *, preserve_enabled=False):
        async with self.lock:
            row = self.get(project_id)
            if row is None:
                raise ValueError("this project has no managed DSH driver")
            if self.external_host:
                self.update(project_id, enabled=0, phase="stopping")
                # The host acknowledges shutdown by removing its process marker.
                marker = self.root / row["driver_id"] / "host-process.json"
                for _ in range(100):
                    if not marker.exists():
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise ValueError("host driver shutdown is still pending; inspect the host service")
                self.gateways.pop(project_id, None)
                self.update(project_id, phase="stopped", subagents_json="[]")
                return self.public(self.get(project_id))
            self.update(project_id, enabled=int(preserve_enabled), phase="stopping")
            proc = self.processes.get(project_id)
            if proc is not None and proc.returncode is None:
                # The process has a private session/group. Never signal other drivers.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    await asyncio.wait_for(proc.wait(), timeout=15)
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
                except ProcessLookupError:
                    await proc.wait()
            self.processes.pop(project_id, None)
            self.gateways.pop(project_id, None)
            self.update(project_id, phase="stopped", subagents_json="[]")
            from core.driver_presence import declare
            if self.registry:
                declare(self.db, row["driver_id"], "offline", "Driver stopped", None,
                        actor="driver:" + row["driver_id"])
            return self.public(self.get(project_id))

    async def resume(self):
        if self.external_host:
            return
        for row in self.rows():
            if row["enabled"]:
                try:
                    await self.start(row["project_id"], actor="dsh-runtime-recovery")
                except ValueError:
                    pass  # Failure remains visible on the dashboard; other projects still start.

    async def close(self):
        self.closing = True
        if self.external_host:
            return  # Backend restarts must not stop the host's resident sessions.
        for project_id in list(self.processes):
            await self.stop(project_id, preserve_enabled=bool(self.get(project_id)["enabled"]))
        if self.monitors:
            await asyncio.gather(*self.monitors)

    def history(self, project_id, *, before_seq=9007199254740991, limit=50):
        import subprocess
        row = self.get(project_id)
        if row is None:
            raise ValueError("this project has no managed DSH driver")
        root = self.root / row["driver_id"] / "home" / "sessions"
        if not root.exists():
            return {"driver_id": row["driver_id"], "session_id": row["session_id"],
                    "entries": [], "has_more": False, "next_before_seq": None}
        script = Path(__file__).resolve().parents[1] / "integrations" / "dsh-driver" / "history.mjs"
        env = dict(os.environ, AITELIER_DSH_ENTRY=str(Path(self.command[0]).resolve()))
        try:
            result = subprocess.run(["node", str(script), str(root), row["session_id"], str(before_seq), str(limit)],
                                    env=env, capture_output=True, text=True, timeout=25)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("DSH history unavailable: " + str(exc)) from exc
        if result.returncode:
            raise ValueError("DSH history unavailable: " + result.stderr[-1000:])
        return {"driver_id": row["driver_id"], **json.loads(result.stdout)}

    @staticmethod
    def _named_endpoint(served_by, route):
        """The endpoint this turn NAMED, or None when it could not name one.

        Two different ways a turn fails to name an endpoint, and the store owes
        the same answer to both. It answered but could not attribute the answer
        (None/''), or the gateway handed back the ROUTE argument it was given
        (`worker`) - an alias, not anywhere this session was served from. Both
        land here as None; neither may be turned into a string that reads like
        an endpoint, and `route` must never be stored as if the alias answered.
        """
        if not served_by or served_by == route:
            return None
        return served_by

    @staticmethod
    def _seen_endpoints(raw):
        """The stored JSON list, never a string that would break the reader."""
        try:
            values = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            return []
        return [v for v in values if isinstance(v, str) and v]

    def record_model_call(self, project_id, session_id, *, route, served_by):
        """Keep WHICH endpoints answered this session, durably, per project.

        Called by the completion endpoint for every answered turn, with the
        endpoint the gateway was actually bound to after any failover and the
        route the session asked for.

        `served_by` is the LAST endpoint this session could NAME - deliberately
        not "the endpoint of the last turn". A turn that cannot name one (empty,
        or the route alias) leaves the already-known endpoint in place: one
        unattributable turn says nothing about who answered the earlier ones, so
        writing NULL there would erase the session's only measured identity.

        `served_by_seen` is every endpoint this session has named, first-seen
        order, no duplicates, bounded by SERVED_BY_SEEN_LIMIT - the answer to
        "which model family wrote this" for a session that failed over, which a
        single last-value column gets wrong by construction.

        Neither column ever holds `route`: an endpoint equal to the alias is
        stored as unknown (NULL / absent from the list), here, so no caller can
        get it wrong by passing `served_by=gateway.model`.
        """
        endpoint = self._named_endpoint(served_by, route)
        stamp = now()
        with self.db.get_connection() as conn:
            # The merge below is a read-modify-write, so the write lock is taken
            # BEFORE the read: two turns for one session arriving at once must
            # not each merge onto the same list and lose one endpoint. Explicit
            # BEGIN/COMMIT because a DEFERRED transaction would only take the
            # lock at the INSERT, after both had read.
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT route,served_by,served_by_seen "
                                   "FROM dsh_driver_session_models "
                                   "WHERE project_id=? AND session_id=?",
                                   (project_id, session_id)).fetchone()
                seen = self._seen_endpoints(row["served_by_seen"]) if row else []
                if not seen and row is not None:
                    # A row written before `served_by_seen` existed named one
                    # endpoint at most; keep it as the first sighting instead of
                    # losing it to the backfill this process may have missed.
                    known = self._named_endpoint(row["served_by"], row["route"] or route)
                    if known:
                        seen.append(known)
                if endpoint and endpoint not in seen and len(seen) < SERVED_BY_SEEN_LIMIT:
                    seen.append(endpoint)
                conn.execute(
                    "INSERT INTO dsh_driver_session_models"
                    "(project_id,session_id,route,served_by,served_by_seen,first_seen,last_seen,calls) "
                    "VALUES(?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(project_id,session_id) DO UPDATE SET "
                    "route=excluded.route, "
                    # COALESCE, not excluded: an unnamed turn keeps the endpoint
                    # the session named earlier. This is the whole of c1 - the
                    # value only moves when a turn actually names one.
                    "served_by=COALESCE(excluded.served_by,dsh_driver_session_models.served_by), "
                    "served_by_seen=excluded.served_by_seen, "
                    "last_seen=excluded.last_seen, "
                    "calls=dsh_driver_session_models.calls+1",
                    (project_id, session_id, route, endpoint, json.dumps(seen), stamp, stamp))
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass  # BEGIN itself may have been what failed; nothing to undo.
                raise

    def session_models(self, project_id):
        """This project's recorded sessions, oldest first. Never another project's.

        Returns the stored values as the read surface must hand them back:
        `served_by` is the last endpoint the session could NAME (NULL when it has
        named none) and `served_by_seen` is the parsed list of every endpoint it
        named, first-seen order - a caller never has to re-derive either from the
        raw JSON text or fall back to "well, it said worker".
        """
        with self.db.get_connection() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT session_id,route,served_by,served_by_seen,first_seen,last_seen,calls "
                "FROM dsh_driver_session_models WHERE project_id=? "
                "ORDER BY first_seen,session_id", (project_id,))]
        for row in rows:
            row["served_by_seen"] = self._seen_endpoints(row["served_by_seen"])
        return rows

    @staticmethod
    def public(row):
        return {k: row[k] for k in ("project_id", "driver_id", "enabled", "phase", "provider", "model",
                                    "session_id", "last_error", "updated_at")}

    @staticmethod
    def _utc_now_iso() -> str:
        """Current UTC instant in the same shape state_attempts stores leases.

        Both sides are ISO-8601 with an explicit +00:00 offset, so a string
        comparison is a valid time comparison and avoids parsing per row.
        """
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def status(self):
        drivers = [d for d in (self.registry.list() if self.registry else []) if d["status"] != "retired"]
        for row in self.rows():
            for d in drivers:
                if d["driver_id"] == row["driver_id"]:
                    d["presence"] = {"status": "online" if self.running(row["project_id"])
                                     and row["phase"] in {"running", "waiting"} else "offline"}
        seen = {d["driver_id"]: d for d in drivers}
        projects = {}
        with self.db.get_connection() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            members = conn.execute("SELECT project_id,driver_id FROM project_drivers WHERE status='member'").fetchall() if self.registry else []
            for m in members:
                if m["driver_id"] in seen:
                    projects.setdefault(m["project_id"], {"drivers": [], "running_subagents": 0, "running_pipelines": 0, "unsettled_attempts": 0, "unsettled_expired_attempts": 0})["drivers"].append(seen[m["driver_id"]])
            for r in conn.execute("SELECT project_id,COUNT(*) n FROM state_attempts "
                                  "WHERE execution_kind='skillflow' AND status='running' GROUP BY project_id") if 'state_attempts' in tables else []:
                projects.setdefault(r["project_id"], {"drivers": [], "running_subagents": 0, "running_pipelines": 0, "unsettled_attempts": 0, "unsettled_expired_attempts": 0})["running_pipelines"] = r["n"]
            # "running_subagents" means LIVE workers, and only the native child
            # list tracks liveness: the runtime empties it when a child exits.
            # An external attempt proves a worker was REGISTERED, not that anyone
            # is working - two of three "running" attempts on 2026-11-03 had
            # leases expired 5 and 21 days earlier. Counting attempts as workers
            # therefore reports dead rows as staff.
            #
            # They are still worth seeing: an unsettled attempt is money spent
            # with no result recorded. So they get their own figure, split by
            # whether the lease is still good, and are never added into a running
            # count. Native ids are UUIDs and external_ids are free-form slugs, so
            # the two can never be matched by identity - which is the second
            # reason not to union them: a caller that registers both would be
            # counted twice for one worker.
            unsettled = {}
            for r in conn.execute("SELECT project_id,external_id,lease_expires_at FROM state_attempts "
                                  "WHERE execution_kind='external' AND status='running' "
                                  "AND project_id IS NOT NULL") if 'state_attempts' in tables else []:
                lease = r["lease_expires_at"] or ""
                live = bool(lease) and lease > self._utc_now_iso()
                slot = unsettled.setdefault(r["project_id"], {"live": set(), "expired": set()})
                slot["live" if live else "expired"].add(r["external_id"] or "")
            for pid, slot in unsettled.items():
                projects.setdefault(pid, {"drivers": [], "running_subagents": 0,
                                          "running_pipelines": 0, "unsettled_attempts": 0,
                                          "unsettled_expired_attempts": 0})
        for row in self.rows():
            p = projects.setdefault(row["project_id"], {"drivers": [], "running_subagents": 0,
                                                        "running_pipelines": 0,
                                                        "unsettled_attempts": 0,
                                                        "unsettled_expired_attempts": 0})
            p["managed"] = self.public(row)
            p["running_subagents"] = len(json.loads(row["subagents_json"])) if self.running(row["project_id"]) else 0
        for pid, slot in unsettled.items():
            p = projects[pid]
            p["unsettled_attempts"] = len(slot["live"]) + len(slot["expired"])
            p["unsettled_expired_attempts"] = len(slot["expired"])
        def counts(ds):
            return {"total": len(ds), "online": sum(d.get("presence", {}).get("status") == "online" and d["status"] == "active" for d in ds)}
        for p in projects.values():
            p.update(counts(p.pop("drivers")))
        return {**counts(drivers), "projects": projects}
