"""One owned DSH process per State project; State remains the task authority."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
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


class DshDrivers:
    def __init__(self, db, registry, root: Path, *, command=None, base_url=None):
        self.db, self.registry = db, registry
        self.root = Path(root)
        self.command = command or [os.getenv("AITELIER_DSH_BIN", "/opt/aitelier-dsh/node_modules/.bin/dsh")]
        self.base_url = base_url or os.getenv("AITELIER_DSH_API_URL", "http://127.0.0.1:4444")
        self.processes = {}
        self.monitors = set()
        self.lock = asyncio.Lock()
        self.closing = False
        with db.get_connection() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

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
        with self.db.get_connection() as conn:
            conn.execute("INSERT INTO dsh_drivers(project_id,driver_id,provider,model,session_id,updated_at) "
                         "VALUES(?,?,?,?,?,?)", (project_id, driver_id, provider, model,
                                                "aitelier-" + driver_id, now()))
            conn.commit()
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
        patches = [
            {"id": "hmr", "disabled": True},
            {"insert": [{"id": "aitelier-driver", "name": str(source)},
                {"id": "mcp-aitelier", "name": "@deepseek-ai/dsh-mcp-client", "config": {
                    "serverName": "aitelier", "transport": "streamable-http",
                    "url": self.base_url.rstrip("/") + "/mcp",
                    "headers": {"X-AItelier-Driver-Token": "__DRIVER_TOKEN__"},
                    "failOnStartupError": True, "toolCallTimeoutMs": 960000}}]},
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
        return env

    async def start(self, project_id, *, provider="deepseek-official", model="deepseek-v4.1-flash", actor):
        async with self.lock:
            if self.closing:
                raise ValueError("driver manager is shutting down")
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
            if row["enabled"] and not self.closing:
                self.update(project_id, phase="failed", subagents_json="[]",
                            last_error=f"DSH exited with code {code}; see runtime.log")

    async def stop(self, project_id, *, preserve_enabled=False):
        async with self.lock:
            row = self.get(project_id)
            if row is None:
                raise ValueError("this project has no managed DSH driver")
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
            self.update(project_id, phase="stopped", subagents_json="[]")
            from core.driver_presence import declare
            if self.registry:
                declare(self.db, row["driver_id"], "offline", "Driver stopped", None,
                        actor="driver:" + row["driver_id"])
            return self.public(self.get(project_id))

    async def resume(self):
        for row in self.rows():
            if row["enabled"]:
                try:
                    await self.start(row["project_id"], actor="dsh-runtime-recovery")
                except ValueError:
                    pass  # Failure remains visible on the dashboard; other projects still start.

    async def close(self):
        self.closing = True
        for project_id in list(self.processes):
            await self.stop(project_id, preserve_enabled=True)
        if self.monitors:
            await asyncio.gather(*self.monitors)

    @staticmethod
    def public(row):
        return {k: row[k] for k in ("project_id", "driver_id", "enabled", "phase", "provider", "model",
                                    "session_id", "last_error", "updated_at")}

    def status(self):
        drivers = [d for d in (self.registry.list() if self.registry else []) if d["status"] != "retired"]
        for row in self.rows():
            for d in drivers:
                if d["driver_id"] == row["driver_id"]:
                    proc = self.processes.get(row["project_id"])
                    d["presence"] = {"status": "online" if proc is not None and proc.returncode is None
                                     and row["phase"] in {"running", "waiting"} else "offline"}
        seen = {d["driver_id"]: d for d in drivers}
        projects = {}
        with self.db.get_connection() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            members = conn.execute("SELECT project_id,driver_id FROM project_drivers WHERE status='member'").fetchall() if self.registry else []
            for m in members:
                if m["driver_id"] in seen:
                    projects.setdefault(m["project_id"], {"drivers": [], "running_subagents": 0, "running_pipelines": 0})["drivers"].append(seen[m["driver_id"]])
            for r in conn.execute("SELECT project_id,COUNT(*) n FROM state_attempts "
                                  "WHERE execution_kind='skillflow' AND status='running' GROUP BY project_id") if 'state_attempts' in tables else []:
                projects.setdefault(r["project_id"], {"drivers": [], "running_subagents": 0, "running_pipelines": 0})["running_pipelines"] = r["n"]
        for row in self.rows():
            p = projects.setdefault(row["project_id"], {"drivers": [], "running_subagents": 0, "running_pipelines": 0})
            p["managed"] = self.public(row)
            proc = self.processes.get(row["project_id"])
            p["running_subagents"] = len(json.loads(row["subagents_json"])) if proc is not None and proc.returncode is None else 0
        def counts(ds):
            return {"total": len(ds), "online": sum(d.get("presence", {}).get("status") == "online" and d["status"] == "active" for d in ds)}
        for p in projects.values():
            p.update(counts(p.pop("drivers")))
        return {**counts(drivers), "projects": projects}
