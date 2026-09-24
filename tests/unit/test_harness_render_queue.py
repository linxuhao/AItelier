"""A busy render lock queues the request; only a holder needing reconciliation stops it.

Every test here serves the real harness: a real ``ThreadingHTTPServer`` running
the real ``_Handler.do_POST`` over a durable owner table in a temp sqlite. Only
the render bodies (``run_script`` / ``x11_input_smoke`` / ``playtest_project``)
are replaced, by a stand-in that records which owner row was active while it
ran and, for the holder, blocks until the test lets it go.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"
RENDER_ROUTES = ("/playtest", "/script", "/x11_input_smoke")


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("GODOT_LIFECYCLE_DB", str(tmp_path / "owners.sqlite3"))
    monkeypatch.setenv("GODOT_DEPLOYMENT_LOCK", str(tmp_path / "deployment-admission.lock"))
    monkeypatch.setenv("GODOT_RENDER_EFFECT_LOCK", str(tmp_path / "render-effect.lock"))
    spec = importlib.util.spec_from_file_location("godot_harness_render_queue", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.RENDER_OWNER_WAIT_POLL_SEC = 0.02
    return module


class _Rig:
    """The harness, served, with render bodies that report who owned the render."""

    def __init__(self, gh, monkeypatch, tmp_path):
        self.gh = gh
        self.project_dir = str(tmp_path)
        self.blocking = {"op-holder"}
        self.block_all = False
        self.release = threading.Event()
        self.entered: dict[str, threading.Event] = {}
        self.rendered: list[tuple[str, str | None]] = []
        self.queued = threading.Event()
        self.waits: dict[str, dict] = {}
        self._lock = threading.Lock()

        def body(route):
            def run(*_args, **_kwargs):
                active = [r for r in gh.render_owner_snapshot() if r["status"] == "active"]
                op = active[0]["operation_id"] if len(active) == 1 else None
                with self._lock:
                    self.rendered.append((route, op))
                    self.entered.setdefault(op, threading.Event()).set()
                if op in self.blocking or self.block_all:
                    self.release.wait(30)
                report = {"passed": True, "route": route}
                if route == "/playtest":
                    report["timing"] = {"report_serialize_sec": 0.0}
                return report
            return run

        monkeypatch.setattr(gh, "run_script", body("/script"))
        monkeypatch.setattr(gh, "x11_input_smoke", body("/x11_input_smoke"))
        monkeypatch.setattr(gh, "playtest_project", body("/playtest"))

        real_conflict = gh._render_owner_conflict

        def conflict(row):
            payload = real_conflict(row)
            if payload["owner_kind"] == "active":
                self.queued.set()
            return payload

        monkeypatch.setattr(gh, "_render_owner_conflict", conflict)

        real_waiting = gh.acquire_render_owner_waiting

        def waiting(project_id, run_id, operation_id, **kwargs):
            outcome = self.waits.setdefault(operation_id, {"done": threading.Event()})
            try:
                row = real_waiting(project_id, run_id, operation_id, **kwargs)
                outcome["result"] = "owner"
                return row
            except BaseException as exc:
                outcome["result"] = type(exc).__name__
                raise
            finally:
                outcome["done"].set()

        monkeypatch.setattr(gh, "acquire_render_owner_waiting", waiting)

        self.server = gh.ThreadingHTTPServer(("127.0.0.1", 0), gh._Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    def payload(self, op: str, **extra) -> dict:
        return {"project_dir": self.project_dir, "project_id": "p", "run_id": "r",
                "operation_id": op, "scripts": ["res://a.gd"], **extra}

    def post(self, route: str, payload: dict, timeout: float = 30) -> tuple[int, dict]:
        req = urllib.request.Request(self.base + route, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def post_in_thread(self, route: str, payload: dict, timeout: float = 30):
        out: dict = {}

        def go():
            try:
                out["code"], out["body"] = self.post(route, payload, timeout)
            except Exception as exc:
                out["exc"] = exc

        thread = threading.Thread(target=go, daemon=True)
        thread.start()
        return thread, out

    def start_holder(self) -> str:
        """Put a live render in flight; return its owner_id."""
        self.holder = self.post_in_thread("/script", self.payload("op-holder"))
        assert self.entered.setdefault("op-holder", threading.Event()).wait(10), (
            "the holder render never started")
        return self.row("op-holder")["owner_id"]

    def row(self, op: str) -> dict:
        rows = [r for r in self.gh.render_owner_snapshot() if r["operation_id"] == op]
        assert len(rows) == 1, rows
        return rows[0]

    def ops_with_rows(self) -> list[str]:
        return [r["operation_id"] for r in self.gh.render_owner_snapshot()]

    def settled_snapshot(self) -> list[dict]:
        """The owner table once the handlers' finally blocks have released.

        A handler sends its response before its finally releases the owner.
        """
        deadline = time.monotonic() + 5
        while (any(r["status"] == "active" for r in self.gh.render_owner_snapshot())
               and time.monotonic() < deadline):
            time.sleep(0.02)
        return self.gh.render_owner_snapshot()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    gh = _load(tmp_path, monkeypatch)
    r = _Rig(gh, monkeypatch, tmp_path)
    yield r
    r.close()


# ── a-busy-lock-queues-the-request ──────────────────────────────────────────

@pytest.mark.parametrize("route", RENDER_ROUTES)
def test_a_busy_lock_queues_the_request(rig, route):
    holder_id = rig.start_holder()
    second, out = rig.post_in_thread(route, rig.payload("op-next"))
    assert rig.queued.wait(10), "the second request never met the live holder"
    time.sleep(0.3)
    assert second.is_alive() and "code" not in out, (
        f"{route} answered while a live holder still rendered: {out!r}")
    assert ("op-next" not in [op for _, op in rig.rendered]), rig.rendered

    rig.release.set()
    second.join(30)
    rig.holder[0].join(30)

    assert out.get("code") == 200, (
        f"{route} behind a live render owner must wait and then run, got {out!r}")
    assert rig.holder[1].get("code") == 200, rig.holder[1]
    body = out["body"]
    assert body["route"] == route
    assert body["render_owner_waited_for_owner_ids"] == [holder_id], body
    assert body["render_owner_wait_sec"] >= 0.3, body
    if route == "/playtest":
        assert body["timing"]["render_owner_wait_sec"] == body["render_owner_wait_sec"]
        assert body["timing"]["render_owner_waited_for_owner_ids"] == [holder_id]
    assert rig.rendered == [("/script", "op-holder"), (route, "op-next")], (
        "the queued request must render as the one active owner, after the holder")
    assert [(r["operation_id"], r["generation"], r["status"])
            for r in rig.settled_snapshot()] == [
        ("op-holder", 1, "released"), ("op-next", 2, "released")]


def test_a_live_render_keeps_its_heartbeat_fresh(rig):
    """A render longer than the staleness threshold must still read as live."""
    rig.gh.RENDER_OWNER_HEARTBEAT_INTERVAL_SEC = 0.05
    rig.start_holder()
    started = rig.row("op-holder")["started_at"]
    time.sleep(0.5)
    beat = rig.row("op-holder")["heartbeat_at"]
    rig.release.set()
    rig.holder[0].join(30)
    assert beat - started >= 0.3, (
        f"the holder's heartbeat did not advance during its render "
        f"(started_at={started}, heartbeat_at={beat})")


# ── a-lost-owner-still-blocks-and-says-so ───────────────────────────────────

@pytest.mark.parametrize("route", RENDER_ROUTES)
def test_a_lost_owner_still_blocks_and_says_so(rig, route):
    gh = rig.gh
    lost = gh.acquire_render_owner("p0", "r0", "op-lost")
    gh.mark_render_owner_lost(lost["owner_id"], lost["generation"], "worker exited")
    started = time.monotonic()
    code, body = rig.post(route, rig.payload("op-next"), timeout=10)
    assert code == 409, (code, body)
    assert rig.rendered == [], "a render ran past an owner_lost holder"
    assert rig.ops_with_rows() == ["op-lost"], "a new owner was admitted past owner_lost"
    assert time.monotonic() - started < 5.0, "an owner_lost holder is refused, never waited out"
    assert body.get("owner_kind") == "owner_lost", body
    assert body.get("owner_id") == lost["owner_id"], body
    assert body.get("needs_reconciliation") is True, body
    assert "owner_lost" in body["detail"] and "reconcil" in body["detail"], body
    assert "worker exited" in body["detail"], body


def test_a_stale_active_owner_blocks_and_a_fresh_one_is_queued(rig):
    gh = rig.gh
    holder = gh.acquire_render_owner("p0", "r0", "op-dead")
    threshold = gh.RENDER_OWNER_HEARTBEAT_STALE_SEC

    def set_heartbeat_age(age):
        with gh._lifecycle_connection() as conn:
            conn.execute("UPDATE render_owners SET heartbeat_at=? WHERE owner_id=?",
                         (time.time() - age, holder["owner_id"]))
            conn.commit()

    set_heartbeat_age(threshold + 5)
    code, body = rig.post("/playtest", rig.payload("op-next", render_wait_timeout_sec=5),
                          timeout=10)
    assert code == 409, (code, body)
    assert body.get("owner_kind") == "active_stale", body
    assert body.get("owner_id") == holder["owner_id"], body
    assert body.get("needs_reconciliation") is True, body
    assert f"threshold {threshold:.0f}s" in body["detail"], body
    assert "reconcil" in body["detail"], body
    assert rig.rendered == [] and rig.ops_with_rows() == ["op-dead"]

    set_heartbeat_age(threshold - 5)
    code, body = rig.post("/playtest", rig.payload("op-next", render_wait_timeout_sec=0.3),
                          timeout=10)
    assert (code, body.get("error")) == (409, "render owner wait timed out"), (
        "a holder inside the heartbeat threshold is a live render to queue behind",
        body)
    assert body["owner_id"] == holder["owner_id"]
    assert rig.rendered == [] and rig.ops_with_rows() == ["op-dead"]


def test_the_playtest_tool_does_not_read_a_refusal_as_an_unreachable_builder(
        rig, monkeypatch):
    from aitelier.tools.godot_playtest import impl

    gh = rig.gh
    logged = []
    monkeypatch.setattr(impl, "log_gate_skip",
                        lambda gate, reason, **_detail: logged.append(reason))
    lost = gh.acquire_render_owner("p0", "r0", "op-lost")
    gh.mark_render_owner_lost(lost["owner_id"], lost["generation"], "worker exited")
    monkeypatch.setattr(impl, "_BUILDER_URL", rig.base)
    refused = impl.post_playtest(rig.payload("op-tool"), timeout=10)
    assert refused["gate_skipped"] is True
    assert refused["skipped_because"] == "render_owner_owner_lost", refused
    assert refused["render_owner_conflict"]["owner_id"] == lost["owner_id"]
    assert lost["owner_id"] in refused["summary"] and "409" in refused["summary"]
    assert "refused" in refused["summary"], refused["summary"]
    assert logged == ["render_owner_owner_lost"], logged

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    monkeypatch.setattr(impl, "_BUILDER_URL", f"http://127.0.0.1:{closed_port}")
    unreachable = impl.post_playtest(rig.payload("op-tool"), timeout=10)
    assert unreachable["gate_skipped"] is True
    assert "skipped_because" not in unreachable and "render_owner_conflict" not in unreachable
    assert logged == ["render_owner_owner_lost", "godot-builder unreachable"], logged
    assert ((refused["passed"], refused["gate_skipped"])
            == (unreachable["passed"], unreachable["gate_skipped"])), (
        "a refusal is a gate that did not run: it keeps the tool's absence shape, "
        "never a failure charged to the build", refused)


# ── the-wait-is-bounded-by-the-callers-own-timeout ──────────────────────────

def _raw_post(base: str, route: str, payload: dict) -> socket.socket:
    host, port = base.rsplit("/", 1)[1].split(":")
    body = json.dumps(payload).encode()
    sock = socket.create_connection((host, int(port)), timeout=10)
    sock.sendall((f"POST {route} HTTP/1.1\r\nHost: {host}\r\n"
                  f"Content-Type: application/json\r\n"
                  f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
    return sock


def test_a_caller_that_disconnects_while_queued_never_gets_the_render(rig):
    rig.start_holder()
    sock = _raw_post(rig.base, "/script", rig.payload("op-ghost"))
    assert rig.queued.wait(10), "the ghost request never queued"
    sock.close()
    abandoned = rig.waits["op-ghost"]["done"].wait(5)

    rig.release.set()
    rig.holder[0].join(30)
    rig.waits["op-ghost"]["done"].wait(5)
    time.sleep(0.2)

    assert abandoned, "the queued request kept waiting after its caller disconnected"
    assert rig.waits["op-ghost"].get("result") == "RenderWaitAbandoned", rig.waits
    assert "op-ghost" not in rig.ops_with_rows(), (
        "an owner row was created for a request whose caller had disconnected")
    assert [op for _, op in rig.rendered] == ["op-holder"], rig.rendered


def test_the_requests_own_wait_limit_ends_the_wait_without_a_render(rig):
    holder_id = rig.start_holder()
    code, body = rig.post("/x11_input_smoke",
                          rig.payload("op-capped", render_wait_timeout_sec=0.3), timeout=10)
    rig.release.set()
    rig.holder[0].join(30)
    time.sleep(0.2)
    assert code == 409, (code, body)
    assert body["error"] == "render owner wait timed out", body
    assert body["owner_id"] == holder_id and body["needs_reconciliation"] is False
    assert body["wait_timeout_sec"] == 0.3 and body["waited_sec"] >= 0.3, body
    assert "op-capped" not in rig.ops_with_rows(), (
        "an owner row was created after the request's own wait limit ran out")
    assert [op for _, op in rig.rendered] == ["op-holder"], rig.rendered


def test_an_open_client_socket_is_not_read_as_a_disconnect(tmp_path, monkeypatch):
    gh = _load(tmp_path, monkeypatch)
    left, right = socket.socketpair()
    handler = object.__new__(gh._Handler)
    handler.connection = left
    try:
        assert handler._render_client_abandoned() is False
        right.close()
        assert handler._render_client_abandoned() is True
    finally:
        left.close()


# ── the wait is bounded while the deployment fence holds it, and after ownership ─
# P3c / P3d come from the r2 review:
# ~/.AItelier/director/reports/lockq-r2-review-20260924/probes/probe_wait_heartbeat_test.py

def _hold_fence(gh):
    stream = open(gh.DEPLOYMENT_LOCK, "a+b")
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    return stream


def _lift(stream):
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    stream.close()


def _not_rendered_rows(rig, op: str) -> list[tuple[str, str]]:
    return [(r["status"], r["reason"] or "") for r in rig.settled_snapshot()
            if r["operation_id"] == op]


def test_a_caller_that_disconnects_while_the_fence_is_held_never_gets_the_render(rig):
    rig.start_holder()
    sock = _raw_post(rig.base, "/script", rig.payload("op-fenced"))
    assert rig.queued.wait(10), "the request never queued"
    fence = _hold_fence(rig.gh)
    try:
        time.sleep(0.5)
        rig.release.set()
        rig.holder[0].join(30)
        rig.settled_snapshot()
        sock.close()
        ended_while_fenced = rig.waits["op-fenced"]["done"].wait(3)
    finally:
        _lift(fence)
    rig.waits["op-fenced"]["done"].wait(5)
    rows = _not_rendered_rows(rig, "op-fenced")
    assert [op for _, op in rig.rendered] == ["op-holder"], (
        "a request whose caller disconnected while the deployment fence was held "
        "rendered once the fence lifted", rig.rendered)
    assert all(status == "released" and reason.startswith("not rendered")
               for status, reason in rows), rows
    assert ended_while_fenced, (
        "the queued request did not notice its caller had gone while the "
        "deployment fence was held")


def test_the_requests_own_wait_limit_holds_while_the_fence_is_held(rig):
    rig.start_holder()
    t, out = rig.post_in_thread("/script",
                                rig.payload("op-capped", render_wait_timeout_sec=0.5))
    assert rig.queued.wait(10), "the request never queued"
    fence = _hold_fence(rig.gh)
    try:
        rig.release.set()
        rig.holder[0].join(30)
        rig.settled_snapshot()
        t.join(1.5)
        answered_while_fenced = "code" in out
    finally:
        _lift(fence)
    t.join(15)
    body = out.get("body") or {}
    assert [op for _, op in rig.rendered] == ["op-holder"], (
        "a request rendered past its own render_wait_timeout_sec", out)
    assert (out.get("code"), body.get("error")) == (409, "render owner wait timed out"), out
    assert all(status == "released" and reason.startswith("not rendered")
               for status, reason in _not_rendered_rows(rig, "op-capped"))
    assert answered_while_fenced, (
        "the wait limit was not enforced until the deployment fence lifted", out)
    assert body.get("deployment_fence_held") is True, body


def _hold_effect_lock(gh):
    stream = open(gh.RENDER_EFFECT_LOCK, "a+b")
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    return stream


def _wait_for_owner(rig, op: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if op in rig.ops_with_rows():
            return
        time.sleep(0.02)
    raise AssertionError(f"{op} never took ownership")


def test_a_caller_gone_after_ownership_releases_the_row_unrendered(rig):
    """Ownership taken, then the request blocks on the render-effect lock; the
    caller disconnects there. The re-check before the render must catch it."""
    effect = _hold_effect_lock(rig.gh)
    try:
        sock = _raw_post(rig.base, "/script", rig.payload("op-late"))
        _wait_for_owner(rig, "op-late")
        time.sleep(0.2)
        sock.close()
        time.sleep(0.3)
    finally:
        _lift(effect)
    rows = _not_rendered_rows(rig, "op-late")
    assert rig.rendered == [], ("rendered for a caller that had disconnected", rig.rendered)
    assert rows == [("released", "not rendered: the caller disconnected before "
                                 "the render started")], rows


def test_a_limit_reached_after_ownership_releases_the_row_unrendered(rig):
    effect = _hold_effect_lock(rig.gh)
    try:
        t, out = rig.post_in_thread("/x11_input_smoke",
                                    rig.payload("op-late", render_wait_timeout_sec=0.3))
        _wait_for_owner(rig, "op-late")
        time.sleep(0.6)
    finally:
        _lift(effect)
    t.join(15)
    rows = _not_rendered_rows(rig, "op-late")
    body = out.get("body") or {}
    assert rig.rendered == [], ("rendered past render_wait_timeout_sec", out)
    assert (out.get("code"), body.get("error")) == (409, "render owner wait timed out"), out
    assert "after the limit" in body["detail"], body
    assert rows == [("released", "not rendered: render_wait_timeout_sec ran out "
                                 "before the render started")], rows


# ── one-failed-heartbeat-does-not-make-a-live-render-stale ──────────────────
# P2d comes from the r2 review (probe_wait_heartbeat_test.py::test_p2d_...).

def test_a_locked_owner_table_does_not_make_a_live_render_stale(rig):
    gh = rig.gh
    gh.RENDER_OWNER_HEARTBEAT_INTERVAL_SEC = 0.2
    gh.RENDER_OWNER_HEARTBEAT_STALE_SEC = 8.0
    holder_id = rig.start_holder()
    blocker = sqlite3.connect(gh.LIFECYCLE_DB, timeout=1)
    blocker.execute("BEGIN EXCLUSIVE")
    # A request that queues while the table is locked: its first poll loses
    # sqlite's busy wait, and it must keep waiting rather than fail.
    during, during_out = rig.post_in_thread(
        "/script", rig.payload("op-during-lock", render_wait_timeout_sec=8), timeout=20)
    time.sleep(6.5)            # longer than _lifecycle_connection's timeout=5
    blocker.rollback()
    blocker.close()
    unlocked_at = time.time()
    answers = []
    deadline = time.monotonic() + gh.RENDER_OWNER_HEARTBEAT_STALE_SEC + 1
    while time.monotonic() < deadline:
        code, body = rig.post("/script", rig.payload(
            f"op-probe-{len(answers)}", render_wait_timeout_sec=0.5), timeout=15)
        answers.append((code, body.get("error"), body.get("owner_kind")))
    beat_after_unlock = rig.row("op-holder")["heartbeat_at"]
    still_rendering = rig.holder[0].is_alive()
    during.join(20)
    rig.release.set()
    rig.holder[0].join(30)
    stops = [a for a in answers if a[2] != "active"]
    assert still_rendering, "the holder render ended early; the probe measured nothing"
    assert answers and not stops, (
        "a live render was read as needing reconciliation after its heartbeat "
        "failed once", stops)
    assert beat_after_unlock > unlocked_at, "the heartbeat never resumed after the lock"
    assert (during_out.get("code"), (during_out.get("body") or {}).get("error")) == (
        409, "render owner wait timed out"), (
        "a request that queued while the owner table was locked did not keep waiting",
        during_out)
    assert rig.holder[1].get("code") == 200
    assert [(r["owner_id"], r["status"]) for r in rig.settled_snapshot()
            if r["operation_id"] == "op-holder"] == [(holder_id, "released")]


_CHILD = """
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("gh_child", sys.argv[1])
gh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gh)
gh.RENDER_OWNER_HEARTBEAT_INTERVAL_SEC = 0.1
row = gh.acquire_render_owner("p-child", "r-child", "op-child")
gh._start_owner_heartbeat(row["owner_id"], row["generation"])
print(row["owner_id"], flush=True)
time.sleep(600)
"""


def test_a_holder_whose_process_died_goes_stale_and_blocks(rig):
    """The control: a real process owns the render and heartbeats; it is killed.
    Its row must go stale past the threshold and block until reconciled."""
    gh = rig.gh
    gh.RENDER_OWNER_HEARTBEAT_STALE_SEC = 2.0
    child = subprocess.Popen([sys.executable, "-c", _CHILD, str(HARNESS)],
                             stdout=subprocess.PIPE, text=True)
    try:
        child_owner = child.stdout.readline().strip()
        assert child_owner, "the child process never took ownership"
        time.sleep(gh.RENDER_OWNER_HEARTBEAT_STALE_SEC + 0.5)
        live_code, live = rig.post("/script", rig.payload(
            "op-live", render_wait_timeout_sec=0.3), timeout=10)
    finally:
        child.kill()
        child.wait(10)
    time.sleep(gh.RENDER_OWNER_HEARTBEAT_STALE_SEC + 0.5)
    code, body = rig.post("/script", rig.payload("op-after", render_wait_timeout_sec=5),
                          timeout=10)
    assert (live_code, live.get("owner_kind")) == (409, "active"), (
        "while the child heartbeat, it was a live render to queue behind", live)
    assert (code, body.get("owner_kind"), body.get("owner_id")) == (
        409, "active_stale", child_owner), body
    assert body.get("needs_reconciliation") is True
    assert rig.rendered == []
    assert [(r["owner_id"], r["status"]) for r in gh.render_owner_snapshot()] == [
        (child_owner, "active")], "the dead holder's row must stay until reconciled"


@pytest.mark.parametrize("ending", ["returns", "raises"])
def test_the_heartbeat_stops_when_the_render_ends(rig, monkeypatch, ending):
    """With the production interval, a heartbeat left running would sleep on for
    RENDER_OWNER_HEARTBEAT_INTERVAL_SEC after its render ended."""
    gh = rig.gh
    if ending == "raises":
        entered = threading.Event()

        def boom(*_a, **_k):
            entered.set()
            rig.release.wait(30)
            raise RuntimeError("render body raised")
        monkeypatch.setattr(gh, "run_script", boom)
    before = {t for t in threading.enumerate() if t.name == "render-owner-heartbeat"}
    t, out = rig.post_in_thread("/script", rig.payload("op-holder"))
    started = (entered if ending == "raises"
               else rig.entered.setdefault("op-holder", threading.Event()))
    assert started.wait(10)
    beats = [t2 for t2 in threading.enumerate()
             if t2.name == "render-owner-heartbeat" and t2 not in before]
    rig.release.set()
    t.join(30)
    rig.settled_snapshot()
    for beat in beats:
        beat.join(2)
    assert len(beats) == 1, beats
    assert out.get("code") == (200 if ending == "returns" else 500), out
    assert not beats[0].is_alive(), "the heartbeat outlived its render"


# ── a-queue-wait-is-never-charged-as-a-product-failure ─────────────────────
# Built from the r2 review's P4 and readers probes:
# ~/.AItelier/director/reports/lockq-r2-review-20260924/probes/probe_queue_timeout_test.py
# ~/.AItelier/director/reports/lockq-r2-review-20260924/probes/probe_readers_test.py

TOOL_TIMEOUT = 2          # stands in for post_playtest's 3600 s


def _game_project(tmp_path: Path) -> Path:
    proj = tmp_path / "game"
    (proj / "playtest").mkdir(parents=True)
    (proj / "project.godot").write_text("[application]\n", encoding="utf-8")
    (proj / "playtest" / "s1.yaml").write_text("name: s1\ntimeline: []\n",
                                               encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    return proj


def _compile_step_route(ret: dict) -> str:
    """The node game_harness's 5_compile step goes to on this return."""
    import yaml

    addon = yaml.safe_load((HARNESS.parents[2] / "configs" / "addons"
                            / "game_harness.yaml").read_text(encoding="utf-8"))

    def walk(node):
        if isinstance(node, dict):
            if node.get("id") == "5_compile" and "transitions" in node:
                return node
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            return None
        for child in children:
            hit = walk(child)
            if hit:
                return hit
        return None

    for edge in walk(addon)["transitions"]:
        match = edge.get("match")
        if not match or ret.get(match["field"]) == match.get("value"):
            return edge["to"]
    return "<none>"


def _tools_on(rig, monkeypatch):
    from aitelier.tools.godot_compile import impl as compile_impl
    from aitelier.tools.godot_playtest import impl as pt_impl

    skips = []
    for mod in (pt_impl, compile_impl):
        monkeypatch.setattr(mod, "log_gate_skip",
                            lambda gate, reason, **_d: skips.append(reason))
        monkeypatch.setattr(mod, "_BUILDER_URL", rig.base)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    monkeypatch.setattr(rig.gh, "compile_project", lambda proj, timeout=120: {
        "passed": True, "returncode": 0, "file_count": 1, "errors": [],
        "warning_count": 0, "summary": "stub compile ok"})
    real_post = pt_impl.post_playtest
    monkeypatch.setattr(pt_impl, "post_playtest",
                        lambda payload, timeout=3600: real_post(payload,
                                                                timeout=TOOL_TIMEOUT))
    return compile_impl, pt_impl, skips


def _run_compile_step(compile_impl, tmp_path, proj):
    from aitelier.gate_evidence import stamp_report

    graph = tmp_path / "graph"
    (graph / "5_test").mkdir(parents=True)
    tr = stamp_report({"passed": True, "summary": "stub pass"}, run_id="run-q",
                      out_dir=str(graph / "5_test"), start_cycle=True)
    (graph / "5_test" / "test_report.json").write_text(json.dumps(tr))
    ret = compile_impl.godot_compile(
        project_root=str(proj), out_dir=str(graph / "5_compile"), run_id="run-q",
        evidence_cycle_from="5_test", fail_fast_gates='[["5_test", "test_report.json"]]')
    report = json.loads((graph / "5_compile" / "playtest_report.json").read_text())
    return graph, ret, report


@pytest.mark.parametrize("cause", ["live_holder", "deployment_fence"])
def test_a_queue_wait_that_outlasts_the_callers_timeout_is_not_a_product_failure(
        rig, tmp_path, monkeypatch, cause):
    from aitelier.gate_evidence import audit_evidence, release_disposition, report_state
    from aitelier.tools.focused_check.impl import focused_check
    from aitelier.tools.godot_playtest_scenario.impl import godot_playtest_scenario
    from aitelier.tools.godot_vision.impl import godot_vision
    from aitelier.tools.verify_evidence.impl import verify_evidence

    compile_impl, pt_impl, skips = _tools_on(rig, monkeypatch)
    proj = _game_project(tmp_path)
    fence = None
    if cause == "live_holder":
        rig.start_holder()          # held until the test ends: longer than the tool timeout
    else:
        fence = _hold_fence(rig.gh)
    try:
        # Reader A: godot_playtest, its return and its report.
        ret_a = pt_impl.godot_playtest(project_root=str(proj), out_dir=str(tmp_path / "a"))
        rep_a = json.loads((tmp_path / "a" / "playtest_report.json").read_text())
        # Reader C: godot_compile as game_harness wires it, and its route.
        graph, ret_c, rep_c = _run_compile_step(compile_impl, tmp_path, proj)
        # Reader D: the release audit.
        gates = [["5_test", "test_report.json"], ["5_compile", "playtest_report.json"]]
        aud = audit_evidence(graph, "run-q", gates)
        (graph / "5_rel").mkdir()
        ret_d = verify_evidence(out_dir=str(graph / "5_rel"), run_id="run-q",
                                gates=json.dumps(gates))
        # Reader E: godot_playtest_scenario. Reader F: focused_check.
        ret_e = godot_playtest_scenario(scenario="s1", project_root=str(proj),
                                        _timeout_seconds=TOOL_TIMEOUT)
        ret_f = focused_check(kind="godot_scenario", scenario="s1",
                              project_root=str(proj), timeout_seconds=4)
    finally:
        if fence is not None:
            _lift(fence)
        rig.release.set()
    # Reader G: godot_vision on 5_compile's play-test report.
    ws = tmp_path / "ws"
    (ws / "cfg" / "5_compile").mkdir(parents=True)
    (ws / "cfg" / "5_compile" / "playtest_report.json").write_text(json.dumps(rep_c))
    godot_vision(project_root=str(proj), out_dir=str(tmp_path / "g"),
                 workspace_root=str(ws), config_name="cfg", from_step="5_compile")
    rep_g = json.loads((tmp_path / "g" / "vision_report.json").read_text())

    assert not [op for _, op in rig.rendered if op != "op-holder"], (
        "a queued request rendered", rig.rendered)
    for name, rep in (("godot_playtest", rep_a), ("godot_compile", rep_c)):
        assert not rep.get("gate_timeout"), (
            f"{name}: a queue wait was reported as a timed-out play-test", rep)
        assert rep.get("skipped_because") == "render_owner_wait_timed_out", (name, rep)
        # Reader B: gate_evidence.
        assert (report_state(dict(rep)), release_disposition(dict(rep))) == (
            "skipped", "unresolved"), (name, rep)
    assert ret_a["passed"] is True, ret_a
    assert ret_c.get("release_evidence") == "unresolved", ret_c
    assert _compile_step_route(ret_c) == "5_release_wait", ret_c
    assert (aud["state"], aud["passed"]) == ("skipped", False), aud
    assert [g["skipped_because"] for g in aud["skipped_gates"]] == [
        "render_owner_wait_timed_out"], aud
    assert (ret_d.get("state"), ret_d.get("passed")) == ("skipped", False), ret_d
    assert "render_owner_wait_timed_out" in ret_e.get("error", ""), ret_e
    assert ret_f.get("timed_out") is False and "render_owner_wait_timed_out" in str(
        ret_f.get("output", "")), ret_f
    assert release_disposition(rep_g) == "unresolved", rep_g
    assert skips and set(skips) == {"render_owner_wait_timed_out"}, skips


def test_a_timeout_after_the_render_started_stays_a_measured_timeout(
        rig, tmp_path, monkeypatch):
    from aitelier.gate_evidence import release_disposition, report_state

    compile_impl, _pt_impl, _skips = _tools_on(rig, monkeypatch)
    proj = _game_project(tmp_path)
    rig.block_all = True           # every render runs longer than the tool timeout
    try:
        _graph, ret_c, rep_c = _run_compile_step(compile_impl, tmp_path, proj)
    finally:
        rig.release.set()
    assert len(rig.rendered) == 1 and rig.rendered[0][0] == "/playtest", rig.rendered
    assert rep_c.get("gate_timeout") is True and rep_c.get("passed") is False, rep_c
    assert (report_state(dict(rep_c)), release_disposition(dict(rep_c))) == (
        "failed", "known_failure"), rep_c
    assert _compile_step_route(ret_c) == "5_vision", ret_c
