"""Real api.main shutdown with held HTTP streams and durable claim recovery.

Run in a disposable UID1000 container with an isolated HOME, AITELIER_HOME
and source PYTHONPATH. No TestClient, test-mode app or replacement lifespan.
SHUTDOWN_EVIDENCE_DIR optionally retains all observations and server logs.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import time

import httpx
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
BASE = "1f4f3e98b813dfb87693c78d733f727fd555b425"
DRAIN_SECONDS = 5
EXIT_BUDGET_SECONDS = 10

# Observe real cleanup returns without replacing any application/server method.
# Imported by Python at startup; the shipped Uvicorn command still starts api.main.
_CLEANUP_OBSERVER = '''
import json, os, sys
def observe(frame, event, result):
    if event != "return":
        return
    file, name = frame.f_code.co_filename, frame.f_code.co_name
    if ((file.endswith("/api/mcp_router.py") and name == "close") or
        (file.endswith("/apscheduler/schedulers/base.py") and name == "shutdown") or
        (file.endswith("/core/scheduler.py") and name == "_cancel_detached_ticks")):
        obj = frame.f_locals.get("self")
        row = {"event": event, "file": file, "function": name, "pid": os.getpid()}
        if name == "close":
            row["endpoint_app"] = obj.app
            row["endpoint_server"] = obj.server
        elif name == "shutdown":
            row["scheduler_state"] = obj.state
        else:
            row["cancelled_ticks"] = result
        with open(os.environ["AITELIER_SHUTDOWN_TRACE"], "a") as f:
            f.write(json.dumps(row) + "\\n")
sys.setprofile(observe)
'''


def _docker_command(text):
    return json.loads(next(line[4:] for line in text.splitlines()
                           if line.startswith("CMD ")))


def _command(entry):
    if entry == "base":
        return _docker_command(subprocess.check_output(
            ["git", "show", f"{BASE}:Dockerfile"], cwd=ROOT, text=True))
    if entry == "docker":
        return _docker_command((ROOT / "Dockerfile").read_text())
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())[
        "services"]["aitelier"]["command"]


class Backend:
    def __init__(self, root, entry):
        self.root, self.entry = root, entry
        self.home = root / "home"
        self.data = self.home / ".AItelier"
        self.data.mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ, HOME=str(self.home), AITELIER_HOME=str(self.data),
                        PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1")
        observer = root / "observer"
        observer.mkdir(exist_ok=True)
        (observer / "sitecustomize.py").write_text(_CLEANUP_OBSERVER)
        self.env["PYTHONPATH"] = f"{observer}:{ROOT}"
        # conftest's in-process test locks must not leak into the actual backend.
        for key in ("DPE_DB_PATH", "SKILLFLOW_DB_PATH", "DPE_WS_PATH",
                    "DPE_PROJECTS_PATH", "AITELIER_SCHEDULER_LOCK",
                    "AITELIER_INSTANCE_LOCK", "UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN"):
            self.env.pop(key, None)
        self.observations = []
        self.process = None
        self.generation = 0

    def record(self, event, **values):
        row = {"event": event, "entry": self.entry, "generation": self.generation,
               "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **values}
        self.observations.append(row)
        with (self.root / "observations.jsonl").open("a") as f:
            f.write(json.dumps(row, default=str, sort_keys=True) + "\n")
        print(json.dumps(row, default=str, sort_keys=True), flush=True)

    def start(self):
        self.generation += 1
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.log_path = self.root / f"server-{self.generation}.log"
        self.trace_path = self.root / f"cleanup-{self.generation}.jsonl"
        self.env["AITELIER_SHUTDOWN_TRACE"] = str(self.trace_path)
        self.log = self.log_path.open("w")
        # Enable existing library cleanup logs; application code is unchanged.
        from uvicorn.config import LOGGING_CONFIG
        config = {**LOGGING_CONFIG, "root": {"handlers": ["default"], "level": "INFO"},
                  "loggers": {**LOGGING_CONFIG["loggers"], "aitelier.scheduler": {
                      "handlers": ["default"], "level": "INFO", "propagate": False}}}
        config_path = self.root / "logging.json"
        config_path.write_text(json.dumps(config))
        cmd = _command(self.entry)
        cmd[cmd.index("--port") + 1] = str(port)
        self.process = subprocess.Popen(cmd + ["--log-config", str(config_path)],
                                        cwd=ROOT, env=self.env,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        self.record("process_start", pid=self.process.pid, command=cmd,
                    proc_cmdline=Path(f"/proc/{self.process.pid}/cmdline").read_bytes()
                    .replace(b"\0", b" ").decode(),
                    data_dir=str(self.data), db_paths=[str(self.data / p)
                    for p in ("aitelier.db", "skillflow.db")])

    async def healthy(self, client):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            assert self.process.poll() is None, self.log_path.read_text()
            try:
                r = await client.get(self.url + "/health", timeout=1)
                if r.status_code == 200:
                    proc = Path(f"/proc/{self.process.pid}")
                    self.record("health", status=r.status_code, body=r.json(),
                                pid=self.process.pid,
                                proc_cmdline=proc.joinpath("cmdline").read_bytes()
                                .replace(b"\0", b" ").decode(),
                                proc_status=proc.joinpath("status").read_text(),
                                proc_stat=proc.joinpath("stat").read_text())
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(.1)
        pytest.fail(self.log_path.read_text())

    def fixture_api(self, code):
        result = subprocess.run(["python", "-c", code], env=self.env, cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.record("fixture_api", command=["python", "-c", code], rc=result.returncode,
                    stdout=result.stdout, stderr=result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout.splitlines()[-1])

    def snapshot(self, run_id):
        with sqlite3.connect(self.data / "skillflow.db") as db:
            db.row_factory = sqlite3.Row
            run = dict(db.execute("SELECT * FROM skillflow_runs WHERE id=?",
                                  (run_id,)).fetchone())
            steps = [dict(r) for r in db.execute(
                "SELECT * FROM skillflow_steps WHERE run_id=? ORDER BY id", (run_id,))]
            ops = [dict(r) for r in db.execute(
                "SELECT * FROM skillflow_active_ops WHERE run_id=?", (run_id,))]
        with sqlite3.connect(self.data / "aitelier.db") as db:
            db.row_factory = sqlite3.Row
            tasks = [dict(r) for r in db.execute(
                "SELECT * FROM tasks WHERE project_id=? ORDER BY id", (run["project_id"],))]
        result = {"run": run, "steps": steps, "operations": ops, "tasks": tasks,
                  "db_inodes": {name: (self.data / name).stat().st_ino
                                for name in ("aitelier.db", "skillflow.db")}}
        self.record("persisted_state", state=result)
        return result

    def term(self):
        assert self.process.poll() is None
        self.record("signal", pid=self.process.pid, signal="SIGTERM", count=1)
        self.term_at = time.monotonic()
        self.process.send_signal(signal.SIGTERM)

    async def exit(self, timeout=EXIT_BUDGET_SECONDS):
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(.05)
        elapsed = time.monotonic() - self.term_at
        self.record("shutdown_observed", elapsed_seconds=elapsed,
                    exit_code=self.process.poll(), exited=self.process.poll() is not None,
                    exit_budget_seconds=timeout, sigkill_used=False)
        return self.process.poll(), elapsed

    def cleanup(self):
        if self.process is not None and self.process.poll() is None:
            # Only this exact owned child, on failed proof / established base hang.
            self.record("fixture_reap", pid=self.process.pid, signal="SIGKILL")
            self.process.kill()
            self.process.wait(timeout=10)
        if hasattr(self, "log"):
            self.log.close()


@pytest.fixture
def backend_factory(tmp_path, request):
    owned = []

    def make(entry):
        evidence = os.environ.get("SHUTDOWN_EVIDENCE_DIR")
        root = ((Path(evidence) / request.node.name) if evidence else tmp_path) / entry
        root.mkdir(parents=True, exist_ok=True)
        b = Backend(root, entry)
        owned.append(b)
        return b

    yield make
    for b in owned:
        b.cleanup()


async def _rpc(client, backend, method, params=None):
    response = await client.post(backend.url + "/mcp", headers={
        "Accept": "application/json, text/event-stream"}, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert response.status_code == 200, response.text
    assert "error" not in response.json(), response.text
    backend.record("mcp_rpc", method=method, status=response.status_code,
                   body=response.json())
    return response.json()


@contextlib.asynccontextmanager
async def _open_streams(client, backend):
    await _rpc(client, backend, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "shutdown-proof", "version": "1"}})
    await _rpc(client, backend, "tools/list")
    async with contextlib.AsyncExitStack() as stack:
        streams, readers, first_sse = [], [], asyncio.Event()

        async def consume(path, response):
            try:
                async for line in response.aiter_lines():
                    backend.record("stream_line", path=path, line=line)
                    if path == "/api/events/stream" and line.startswith("data:"):
                        first_sse.set()
            except httpx.TransportError as exc:
                backend.record("stream_transport_end", path=path, error=str(exc))
            finally:
                backend.record("stream_reader_end", path=path)

        for path in ("/api/events/stream", "/mcp"):
            response = await stack.enter_async_context(client.stream(
                "GET", backend.url + path, headers={"Accept": "text/event-stream",
                "MCP-Protocol-Version": "2025-06-18"}, timeout=None))
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            backend.record("stream_headers", path=path, status=response.status_code,
                           headers=dict(response.headers))
            streams.append(response)
            readers.append(asyncio.create_task(consume(path, response)))
        try:
            await asyncio.wait_for(first_sse.wait(), 3)
            await asyncio.sleep(.2)
            presence = await client.get(backend.url + "/api/connections")
            assert presence.json()["total"] == 1
            assert all(not r.is_closed for r in streams)
            assert all(not t.done() for t in readers)
            backend.record("streams_confirmed_open", paths=["/api/events/stream", "/mcp"],
                           readers_done=[t.done() for t in readers],
                           responses_closed=[r.is_closed for r in streams],
                           presence=presence.json(),
                           tcp=Path(f"/proc/{backend.process.pid}/net/tcp").read_text())
            yield readers
        finally:
            for t in readers:
                t.cancel()
            await asyncio.gather(*readers, return_exceptions=True)


def _assert_cleanup(backend):
    log = backend.log_path.read_text()
    # Native library/application events, never fixture-generated success markers.
    events = ["StreamableHTTP session manager shutting down",
              "Application shutdown complete.", "Finished server process"]
    assert all(event in log for event in events), log
    assert log.index("Waiting for application shutdown") < log.index(events[0])
    assert log.index(events[0]) < log.index(events[1])
    trace = [json.loads(line) for line in backend.trace_path.read_text().splitlines()]
    assert all(row["pid"] == backend.process.pid for row in trace)
    assert any(row["function"] == "close" and row["endpoint_app"] is None
               and row["endpoint_server"] is None for row in trace)
    assert any(row["function"] == "shutdown" and row["scheduler_state"] == 0
               for row in trace)
    assert any(row["function"] == "_cancel_detached_ticks" for row in trace)
    backend.record("cleanup_evidence", log_path=str(backend.log_path), native_events=events,
                   actual_cleanup_returns=trace)


# This graph is driven only through SkillFlow's public fixture APIs. The
# interrupted step is claimed, with a committed transcript, but has no admitted
# external operation; startup must reopen it rather than replay the committed step.
GRAPH_CODE = '''
from skillflow.core import StepResult, ClaimToken
from skillflow.exceptions import StaleClaimFenced
from skillflow.graph import PipelineGraph, StepNode, Transition
from api.dependencies import get_skillflow
sf = get_skillflow()
sf.register_graph(PipelineGraph(name="shutdown_fixture", begin="committed", steps=[
    StepNode(id="committed", transitions=[Transition(to="interrupted")]),
    StepNode(id="interrupted")]))
'''


@pytest.mark.parametrize("entry", ["docker", "compose"])
async def test_real_backend_shutdown_restart_and_recovery(backend_factory, entry):
    backend = backend_factory(entry)
    backend.start()
    async with httpx.AsyncClient(timeout=5) as client:
        await backend.healthy(client)
        project = await client.post(backend.url + "/api/projects", json={
            "project_id": "shutdown-fixture", "repo_type": "none"})
        assert project.status_code == 201, project.text
        paused = await client.patch(backend.url + "/api/projects/shutdown-fixture",
                                    params={"status": "paused"})
        assert paused.status_code == 200, paused.text
        assert paused.json()["status"] == "paused"
        task = await client.post(backend.url + "/api/tasks", json={
            "project_id": "shutdown-fixture", "prompt": "Keep this committed task across restart"})
        assert task.status_code == 200, task.text
        task_id = task.json()["id"]
        backend.record("http_task_created", task=task.json(), project=project.json())
        fixture = backend.fixture_api(GRAPH_CODE + f'''
import json
rid = sf.create_run("shutdown_fixture", project_id="shutdown-fixture", context={{"task_id": {task_id}}})
sf.start_run(rid); sf.advance_run(rid)
committed = sf.claim_next_step(rid)
sf.confirm_step(committed.token, StepResult(outputs={{"report": "durable committed output"}}))
sf.advance_run(rid)
interrupted = sf.claim_next_step(rid)
sf.trace(rid, "agent", "prompt_delta", {{"turn": 1, "role": "assistant", "content": "durable interrupted transcript"}},
         step_id="interrupted", step_instance_id=interrupted.token.step_instance_id)
print(json.dumps({{"run_id": rid, "token": interrupted.token.__dict__,
                  "trace": sf.get_trace(rid, step_instance_id=interrupted.token.step_instance_id)}}))
''')
        run_id = fixture["run_id"]
        assert any(t["payload"].get("content") == "durable interrupted transcript"
                   for t in fixture["trace"])
        before = backend.snapshot(run_id)
        assert before["run"]["status"] == "running"
        assert [s["status"] for s in before["steps"]] == ["completed", "claimed"]
        assert before["operations"] == []
        assert before["tasks"][0]["prompt"] == "Keep this committed task across restart"
        async with _open_streams(client, backend) as readers:
            assert all(not t.done() for t in readers)
            backend.term()
            rc, elapsed = await backend.exit()
            assert rc in (0, -signal.SIGTERM), backend.log_path.read_text()
            assert elapsed < EXIT_BUDGET_SECONDS
            assert elapsed >= DRAIN_SECONDS
            assert "timeout graceful shutdown exceeded" in backend.log_path.read_text()
            _assert_cleanup(backend)
        stopped = backend.snapshot(run_id)
        assert stopped["steps"] == before["steps"]
        backend.cleanup()
        backend.start()  # Same HOME, DB files and committed fixture, new process.
        await backend.healthy(client)
        recovered = backend.snapshot(run_id)
        assert recovered["db_inodes"] == before["db_inodes"]
        assert recovered["tasks"] == before["tasks"]
        assert recovered["run"]["current_node"] == "interrupted"
        assert recovered["steps"][0] == before["steps"][0]
        assert recovered["steps"][1]["id"] == before["steps"][1]["id"]
        assert recovered["steps"][1]["status"] == "pending"
        assert recovered["steps"][1]["claimed_by"] is None
        assert recovered["steps"][1]["version"] > before["steps"][1]["version"]
        assert "Startup recovery: reopened 1 claim(s), closed 0 superseded instance(s)" in backend.log_path.read_text()
        after_task = await client.get(backend.url + f"/api/tasks/{task_id}")
        assert after_task.status_code == 200
        assert after_task.json() == task.json()
        completion = backend.fixture_api(GRAPH_CODE + f'''
import json
rid = {run_id!r}
old_token = ClaimToken(**{fixture["token"]!r})
trace = sf.get_trace(rid, step_instance_id=old_token.step_instance_id)
assert trace[:len({fixture["trace"]!r})] == {fixture["trace"]!r}
assert sf.advance_run(rid) == "interrupted"
fresh = sf.claim_next_step(rid)
assert fresh.token.step_instance_id == old_token.step_instance_id
assert fresh.token.claim_epoch > old_token.claim_epoch
assert sf.claim_next_step(rid) is None
try:
    sf.confirm_step(old_token, StepResult(outputs={{"report": "stale duplicate"}}))
except StaleClaimFenced as exc:
    stale_error = type(exc).__name__ + ": " + str(exc)
else:
    raise AssertionError("stale pre-restart claim could commit duplicate output")
sf.confirm_step(fresh.token, StepResult(outputs={{"report": "resumed output"}}))
sf.advance_run(rid)
assert sf.get_run(rid)["status"] == "completed"
assert sf.claim_next_step(rid) is None
print(json.dumps({{"stale_claim_rejected": stale_error, "fresh_token": fresh.token.__dict__,
                  "run": sf.get_run(rid), "trace_preserved": trace}}))
''')
        completed = backend.snapshot(run_id)
        assert len(completed["steps"]) == 2
        assert completed["tasks"] == before["tasks"]
        assert completed["steps"][0] == before["steps"][0]
        assert [s["status"] for s in completed["steps"]] == ["completed", "completed"]
        backend.record("recovery_verified", task=after_task.json(), completion=completion)
        async with _open_streams(client, backend) as readers:
            assert all(not t.done() for t in readers)
            backend.term()
            rc, elapsed = await backend.exit()
            assert rc in (0, -signal.SIGTERM), backend.log_path.read_text()
            assert elapsed < EXIT_BUDGET_SECONDS
            _assert_cleanup(backend)


async def test_accepted_base_command_hangs_with_real_streams(backend_factory):
    # Only startup configuration is reverted; api.main, SSE and MCP source are
    # unchanged by the fix. This isolates the timeout as the causal variable.
    backend = backend_factory("base")
    assert "--timeout-graceful-shutdown" not in _command("base")
    backend.start()
    async with httpx.AsyncClient(timeout=5) as client:
        await backend.healthy(client)
        async with _open_streams(client, backend) as readers:
            backend.term()
            rc, elapsed = await backend.exit()
            assert rc is None, backend.log_path.read_text()
            # MCP's SDK handles TERM by closing its GET stream immediately.
            # FastAPI's SSE stream still holds the accepted base in drain.
            assert not readers[0].done()
            backend.record("negative_control_stream_state_at_deadline",
                           readers_done=[t.done() for t in readers])
            log = backend.log_path.read_text()
            assert "Waiting for connections to close" in log
            assert "Waiting for application shutdown" not in log
            assert "Application shutdown complete" not in log
            backend.record("negative_control_failure_established", requirement_met=False,
                           reason="still running with SSE at 10s; both streams open at TERM; lifespan not reached",
                           elapsed_seconds=elapsed)
            backend.cleanup()  # Exact owned PID reaped only after the failure.
