"""An abandoned render sweep stops at the next entry/scenario boundary.

A render route's `timeout` bounds EACH engine pass; the selection has no bound.
Before this change the client-abort predicate had one consumer (the queue wait),
so a request that already OWNED the render kept running every remaining entry for
a client that was gone, holding the shared render lock for the whole selection.

Every test here serves the real harness: a real ThreadingHTTPServer running the
real `_Handler.do_POST`, the real `run_script` / `_playtest_spec` sweep bodies and
the real owner table in a temp sqlite. The only stand-in is the ENGINE process
boundary — `_run` for /script and `_run_probe` for /playtest — which records every
entry it is asked to run. With `hold_first` the FIRST entry does not return until
THIS request has OBSERVED the caller's departure — the request's own peer
predicate, the same one the sweep consults at its next boundary — so the boundary
is decided by the test's ordering and never by the scheduler. (The first version
held for a fixed 30 s and then let the sweep continue, and the sweep's check raced
the FIN: a relay that forwards the client's departure 50 ms late reproduced the
exact 2026-11-07 payload, both entries run. See
evidence/sweep-determinism-20261108/.)
"""
from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("GODOT_LIFECYCLE_DB", str(tmp_path / "owners.sqlite3"))
    monkeypatch.setenv("GODOT_DEPLOYMENT_LOCK", str(tmp_path / "deployment-admission.lock"))
    monkeypatch.setenv("GODOT_RENDER_EFFECT_LOCK", str(tmp_path / "render-effect.lock"))
    spec = importlib.util.spec_from_file_location("godot_harness_sweep_abandon", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.RENDER_OWNER_WAIT_POLL_SEC = 0.02
    return module


def _script_project(tmp_path: Path, n_entries: int) -> Path:
    """A Godot project whose tests/ holds `n_entries` admitted SceneTree suites."""
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "project.godot").write_text("config_version=5\n")
    for i in range(n_entries):
        (proj / "tests" / f"test_entry_{i}.gd").write_text(
            "extends SceneTree\n\nfunc _init():\n\tquit(0)\n")
    return proj


class _Engine:
    """The engine process boundary: records each entry it is asked to run.

    When `hold_first` is set, the FIRST entry does not return until this request
    has observed the caller's departure, so the sweep's next boundary check
    reads an EOF that is already queued instead of one still in flight. The
    deadline is what keeps a predicate that never fires (a mutant, a broken
    product) a RED rather than a hung suite; `hold_lapsed` records it.
    """

    HOLD_DEADLINE_SEC = 20.0
    POLL_SEC = 0.005

    def __init__(self, harness):
        self.gh = harness
        self.ran: list[str] = []
        self.first_running = threading.Event()
        self.hold_first = False
        self.hold_lapsed = False
        self._lock = threading.Lock()

    def enter(self, label: str) -> None:
        with self._lock:
            self.ran.append(label)
            first = len(self.ran) == 1
        if first and self.hold_first:
            self.first_running.set()
            deadline = time.monotonic() + self.HOLD_DEADLINE_SEC
            while not self.gh._request_client_abandoned():
                if time.monotonic() >= deadline:
                    self.hold_lapsed = True
                    return
                time.sleep(self.POLL_SEC)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    gh = _load(tmp_path, monkeypatch)
    engine = _Engine(gh)

    def fake_run(args, timeout, extra_env=None, render=False, **_kw):
        # /script: `godot --path <copy> -s res://tests/<file>`; the sweep's own loop
        # calls this once per admitted entry.
        engine.enter(args[args.index("-s") + 1])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def fake_probe(dst, state_path, frames, timeout, extra, scene="", capture_at=None,
                   timing=None, render=True, **_kw):
        # /playtest: one engine pass per scenario, named by the probe spec the sweep
        # wrote for it. The no-input control is named "control" and is not a scenario.
        spec = json.loads(Path(extra["AITELIER_PROBE_SPEC"]).read_text())
        engine.enter(spec.get("scenario", "control"))
        if timing is not None:
            # A real pass reports the game time it stepped: the determined
            # game-time check reads it, so the stand-in reports the frame budget
            # at the fixed rate, exactly as the engine's own clock would.
            timing["game_usec"] = int(frames * 1_000_000 / gh.PLAYTEST_FIXED_FPS)
        return ({"frames": frames, "nodes": {"n": 1}, "complete": True,
                 "asserts": [{"name": "a", "passed": True, "measurement": "ok"}]},
                [], False)

    monkeypatch.setattr(gh, "_run", fake_run)
    monkeypatch.setattr(gh, "_run_probe", fake_probe)
    monkeypatch.setattr(gh, "_import_resources", lambda dst, timeout, raw_logs=None: None)
    monkeypatch.setattr(gh, "_inject_probe", lambda dst, **_kw: None)

    server = gh.ThreadingHTTPServer(("127.0.0.1", 0), gh._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    yield gh, engine, base, server
    server.shutdown()
    server.server_close()
    # A hold that timed out means the stand-in let the sweep continue while the
    # caller was still connected: the test measured the scheduler, not the sweep.
    assert not engine.hold_lapsed, (
        "the engine stand-in's hold expired after %.0fs without the request ever "
        "observing the caller's departure" % engine.HOLD_DEADLINE_SEC)


def _send(port: int, route: str, payload: dict) -> socket.socket:
    """Write one request and return the open socket, so the test decides when the caller leaves."""
    body = json.dumps(payload).encode()
    sock = socket.create_connection(("127.0.0.1", port), timeout=60)
    sock.sendall((f"POST {route} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                  f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                  f"Connection: close\r\n\r\n").encode() + body)
    return sock


def _read_response(sock: socket.socket) -> dict:
    """The full HTTP response on a connected socket, as JSON."""
    chunks = []
    while True:
        data = sock.recv(65536)
        if not data:
            break
        chunks.append(data)
    raw = b"".join(chunks)
    return json.loads(raw.split(b"\r\n\r\n", 1)[1])


def _await_release(gh, timeout: float = 10.0) -> None:
    """Wait until the handler's finally has released the render owner row."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(r["status"] == "active" for r in gh.render_owner_snapshot()):
            return
        time.sleep(0.02)
    raise AssertionError("render owner row was never released")


# ── /script ─────────────────────────────────────────────────────────────────

def test_an_abandoned_script_sweep_never_starts_the_second_entry(harness, tmp_path):
    gh, engine, base, server = harness
    proj = _script_project(tmp_path, 2)
    engine.hold_first = True
    sock = _send(server.server_port, "/script", {
        "project_dir": str(proj), "project_id": "p", "run_id": "r",
        "operation_id": "op-abandon-script", "render": False, "timeout": 60})
    assert engine.first_running.wait(15), "entry 1 never reached the engine"
    sock.close()  # the caller is gone while entry 1 is still running
    _await_release(gh)

    # (a) the second entry never reached the engine — only the first did.
    assert engine.ran == ["res://tests/test_entry_0.gd"], engine.ran


def test_an_abandoned_script_sweep_says_so_and_is_not_a_pass(harness, tmp_path, monkeypatch):
    gh, engine, base, server = harness
    proj = _script_project(tmp_path, 2)
    # Capture the sweep's own return value: the caller's socket is gone, so the
    # verdict is observed at the call, not on the wire.
    seen: dict = {}
    real = gh.run_script

    def capture(*a, **kw):
        seen["report"] = real(*a, **kw)
        return seen["report"]

    monkeypatch.setattr(gh, "run_script", capture)
    engine.hold_first = True
    sock = _send(server.server_port, "/script", {
        "project_dir": str(proj), "project_id": "p", "run_id": "r",
        "operation_id": "op-abandon-script-verdict", "render": False, "timeout": 60})
    assert engine.first_running.wait(15)
    sock.close()
    _await_release(gh)

    report = seen["report"]
    # (b) not a pass
    assert report["passed"] is False, report["summary"]
    # (c) the summary names the abandonment, and the named field says so too
    assert "disconnected" in report["summary"], report["summary"]
    assert report["abandoned"] is True
    assert report["executed_suites"] == 1 == len(report["results"])
    assert [r["script"] for r in report["results"]] == ["res://tests/test_entry_0.gd"]


def test_a_connected_script_client_gets_every_entry_and_the_passing_verdict(harness, tmp_path):
    gh, engine, base, server = harness
    proj = _script_project(tmp_path, 2)
    sock = _send(server.server_port, "/script", {
        "project_dir": str(proj), "project_id": "p", "run_id": "r",
        "operation_id": "op-connected-script", "render": False, "timeout": 60})
    out = _read_response(sock)
    sock.close()
    # (d) a connected client: every entry ran and the verdict is today's green.
    assert engine.ran == ["res://tests/test_entry_0.gd", "res://tests/test_entry_1.gd"], engine.ran
    assert out["passed"] is True, out
    assert len(out["results"]) == 2
    assert "abandoned" not in out
    assert out["summary"] == "2 script(s) ran, all passed.", out["summary"]


# ── /playtest ───────────────────────────────────────────────────────────────

_SPEC = {"scene": "res://main.tscn", "scenarios": [
    {"name": "alpha", "timeline": [{"at": 1, "assert": {"A.b": "b == 1"}}]},
    {"name": "beta", "timeline": [{"at": 1, "assert": {"A.b": "b == 1"}}]},
]}


def _playtest_project(tmp_path: Path) -> Path:
    proj = tmp_path / "pt"
    proj.mkdir()
    (proj / "project.godot").write_text("config_version=5\n")
    return proj


def test_an_abandoned_playtest_never_starts_the_second_scenario(harness, tmp_path, monkeypatch):
    gh, engine, base, server = harness
    proj = _playtest_project(tmp_path)
    seen: dict = {}
    real = gh.playtest_project

    def capture(*a, **kw):
        seen["report"] = real(*a, **kw)
        return seen["report"]

    monkeypatch.setattr(gh, "playtest_project", capture)
    engine.hold_first = True
    sock = _send(server.server_port, "/playtest", {
        "project_dir": str(proj), "project_id": "p", "run_id": "r",
        "operation_id": "op-abandon-playtest", "spec": _SPEC, "timeout": 60})
    assert engine.first_running.wait(15), "scenario alpha never reached the engine"
    sock.close()
    _await_release(gh)

    # (a) the second scenario never reached the engine.
    assert engine.ran == ["alpha"], engine.ran
    report = seen["report"]
    # (b) not a pass, (c) the summary and the named field say the sweep was abandoned.
    assert report["passed"] is False, report["summary"]
    assert "disconnected" in report["summary"], report["summary"]
    assert report["abandoned"] is True
    assert report["executed_scenarios"] == 1
    assert [s["name"] for s in report["behavior"]["scenarios"]] == ["alpha"]


def test_a_connected_playtest_client_gets_every_scenario_and_the_passing_verdict(
        harness, tmp_path):
    gh, engine, base, server = harness
    proj = _playtest_project(tmp_path)
    sock = _send(server.server_port, "/playtest", {
        "project_dir": str(proj), "project_id": "p", "run_id": "r",
        "operation_id": "op-connected-playtest", "spec": _SPEC, "timeout": 60})
    out = _read_response(sock)
    sock.close()
    # (d) a connected client: both scenarios ran and today's verdict stands.
    assert engine.ran == ["alpha", "beta"], engine.ran
    assert out["passed"] is True, out["summary"]
    assert [s["name"] for s in out["behavior"]["scenarios"]] == ["alpha", "beta"]
    assert "abandoned" not in out
