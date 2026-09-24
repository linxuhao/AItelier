"""A busy render lock queues the request; only a holder needing reconciliation stops it.

Every test here serves the real harness: a real ``ThreadingHTTPServer`` running
the real ``_Handler.do_POST`` over a durable owner table in a temp sqlite. Only
the render bodies (``run_script`` / ``x11_input_smoke`` / ``playtest_project``)
are replaced, by a stand-in that records which owner row was active while it
ran and, for the holder, blocks until the test lets it go.
"""
from __future__ import annotations

import importlib.util
import json
import socket
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
                if op in self.blocking:
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
