"""One owned DSH process per State project; State remains the task authority."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from pathlib import Path
import signal
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
"""


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
        patches = [
            {"id": "hmr", "disabled": True},
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
                   AITELIER_DSH_ENTRY=str(Path(self.command[0]).resolve()))
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
