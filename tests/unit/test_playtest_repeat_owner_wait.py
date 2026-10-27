"""Repeat protection after a caller's render-owner wait expired.

Drives the real host tool path (``godot_playtest`` -> ``post_playtest`` ->
urllib) against a finite, in-process HTTP provider that speaks the sidecar's
established wire shapes: POST /playtest (409 "render owner wait timed out" or a
200 report) and read-only GET /lifecycle ({"resource": "render", "owners": [...]}).
No sleeps beyond a sub-second native-timeout case, no live sidecar, no Docker.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aitelier.tools.godot_playtest import impl


class _Provider:
    def __init__(self):
        self.posts: list[dict] = []
        self.gets = 0
        self.post_mode = "wait_timeout"   # | "ok" | "owner_lost" | "slow"
        self.timeout_owner = "owner-A"
        self.lifecycle = (200, {"resource": "render", "owners": []})
        self.server = None

    def row(self, owner_id, status="active", age=1.0, generation=1):
        return {"owner_id": owner_id, "resource": "render", "project_id": "p",
                "run_id": "other", "operation_id": "x", "generation": generation,
                "status": status, "actor": "godot-harness:x",
                "started_at": time.time() - 500, "heartbeat_at": time.time() - age}

    def start(self):
        prov = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path != "/lifecycle":
                    return self._send(404, {"error": "not found"})
                prov.gets += 1
                code, body = prov.lifecycle
                self._send(code, body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                prov.posts.append(json.loads(self.rfile.read(n) or b"{}"))
                if prov.post_mode == "ok":
                    return self._send(200, {
                        "passed": True, "frames": 7, "errors": [], "state": {"x": 1},
                        "behavior": None, "spec_used": False, "summary": "rendered"})
                if prov.post_mode == "owner_lost":
                    return self._send(409, {
                        "error": "render owner exists", "owner_kind": "owner_lost",
                        "owner_id": prov.timeout_owner, "needs_reconciliation": True,
                        "detail": "lost"})
                if prov.post_mode == "slow":
                    time.sleep(1.0)
                    return self._send(200, {"passed": True, "frames": 1})
                return self._send(409, {
                    "error": "render owner wait timed out", "owner_kind": "active",
                    "owner_id": prov.timeout_owner,
                    "waited_for_owner_ids": [prov.timeout_owner],
                    "needs_reconciliation": False, "waited_sec": 450.0,
                    "wait_timeout_sec": 450.0, "detail": "queued behind owner"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def provider(monkeypatch):
    prov = _Provider()
    base = prov.start()
    monkeypatch.setattr(impl, "_BUILDER_URL", base)
    monkeypatch.setattr(impl, "log_gate_skip", lambda *a, **k: None)
    impl._repeat_memory.clear()
    yield prov
    impl._repeat_memory.clear()
    prov.stop()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    return root


def _run(project, out, *, run_id="run-1", op="op-1", project_id="p"):
    out.mkdir(parents=True, exist_ok=True)
    res = impl.godot_playtest(project_root=str(project), out_dir=str(out),
                              project_id=project_id, run_id=run_id, step_id="t_impl",
                              operation_id=op)
    return json.loads((out / "playtest_report.json").read_text(encoding="utf-8")), res


def test_repeat_against_same_active_owner_does_not_post_again(provider, project, tmp_path):
    provider.lifecycle = (200, {"resource": "render", "owners": [provider.row("owner-A")]})
    first, _ = _run(project, tmp_path / "o1", op="op-1")
    assert first["skipped_because"] == "render_owner_wait_timed_out"
    assert len(provider.posts) == 1 and provider.gets == 0

    # A different per-call operation id is the same caller.
    second, res = _run(project, tmp_path / "o2", op="op-2-unique")
    assert len(provider.posts) == 1, "expensive POST repeated against unchanged owner"
    assert provider.gets == 1
    assert second["render_owner_repeat_suppressed"] is True
    assert second["measurement_status"] == "UNMEASURED"
    assert second["frames"] == 0 and second["spec_used"] is False
    assert second["gate_skipped"] is True
    assert second["render_owner_conflict"]["owner_id"] == "owner-A"
    assert second["render_owner_observation"]["owner_id"] == "owner-A"
    assert second["render_owner_observation"]["generation"] == 1
    assert "retry after" in second["retry_condition"]
    assert "UNMEASURED" in second["summary"] and "not a pass" in second["summary"]


def test_released_owner_allows_normal_post_with_measured_result(provider, project, tmp_path):
    provider.lifecycle = (200, {"resource": "render", "owners": [provider.row("owner-A")]})
    _run(project, tmp_path / "o1")
    provider.lifecycle = (200, {"resource": "render", "owners": [
        provider.row("owner-A", status="released")]})
    provider.post_mode = "ok"
    report, _ = _run(project, tmp_path / "o2", op="op-2")
    assert len(provider.posts) == 2
    assert report["frames"] == 7 and report["state"] == {"x": 1}
    assert "render_owner_repeat_suppressed" not in report
    assert impl._repeat_memory == {}


def test_changed_owner_allows_normal_post(provider, project, tmp_path):
    provider.lifecycle = (200, {"resource": "render", "owners": [provider.row("owner-A")]})
    _run(project, tmp_path / "o1")
    provider.lifecycle = (200, {"resource": "render", "owners": [
        provider.row("owner-A", status="released"),
        provider.row("owner-B", generation=2)]})
    provider.post_mode = "ok"
    report, _ = _run(project, tmp_path / "o2", op="op-2")
    assert len(provider.posts) == 2 and report["frames"] == 7


def test_distinct_callers_are_not_blocked_by_each_other(provider, project, tmp_path):
    provider.lifecycle = (200, {"resource": "render", "owners": [provider.row("owner-A")]})
    _run(project, tmp_path / "o1", run_id="run-1")
    _run(project, tmp_path / "o2", run_id="run-2", op="op-2")
    assert len(provider.posts) == 2 and provider.gets == 0
    _run(project, tmp_path / "o3", run_id="run-1", project_id="other-project", op="op-3")
    assert len(provider.posts) == 3


def test_no_prior_refusal_posts_without_observation(provider, project, tmp_path):
    provider.post_mode = "ok"
    report, _ = _run(project, tmp_path / "o1")
    assert report["frames"] == 7 and len(provider.posts) == 1 and provider.gets == 0


def test_request_without_run_identity_keeps_prior_behavior(provider, project, tmp_path):
    provider.lifecycle = (200, {"resource": "render", "owners": [provider.row("owner-A")]})
    _run(project, tmp_path / "o1", run_id="")
    _run(project, tmp_path / "o2", run_id="")
    assert len(provider.posts) == 2 and provider.gets == 0


@pytest.mark.parametrize("lifecycle", [
    (503, {"error": "db locked"}),
    (200, b"not json"),
    (200, {"resource": "render"}),
    (200, {"resource": "render", "owners": "bad"}),
    (200, {"resource": "other", "owners": []}),
    (200, {"resource": "render", "owners": []}),
    (200, {"resource": "render", "owners": [{"owner_id": "owner-A", "status": "active"}]}),
    (200, {"resource": "render", "owners": [{"owner_id": "owner-A", "status": "active",
                                             "heartbeat_at": "now"}]}),
])
def test_unknown_or_unconfirming_observation_is_never_a_block(
        provider, project, tmp_path, lifecycle):
    provider.lifecycle = (200, {"resource": "render", "owners": [provider.row("owner-A")]})
    _run(project, tmp_path / "o1")
    provider.lifecycle = lifecycle
    _run(project, tmp_path / "o2", op="op-2")
    assert len(provider.posts) == 2, lifecycle


def test_stale_heartbeat_active_row_is_not_confirmation(provider, project, tmp_path):
    provider.lifecycle = (200, {"resource": "render", "owners": [provider.row("owner-A")]})
    _run(project, tmp_path / "o1")
    provider.lifecycle = (200, {"resource": "render", "owners": [
        provider.row("owner-A", age=impl._OWNER_HEARTBEAT_STALE_SEC + 60)]})
    _run(project, tmp_path / "o2", op="op-2")
    assert len(provider.posts) == 2


def test_unreachable_lifecycle_is_unknown_not_blocked(provider, project, tmp_path, monkeypatch):
    provider.lifecycle = (200, {"resource": "render", "owners": [provider.row("owner-A")]})
    _run(project, tmp_path / "o1")
    real = impl.urllib.request.urlopen

    def failing(req, *a, **k):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/lifecycle"):
            raise OSError("connection refused")
        return real(req, *a, **k)

    monkeypatch.setattr(impl.urllib.request, "urlopen", failing)
    _run(project, tmp_path / "o2", op="op-2")
    assert len(provider.posts) == 2


def test_reconciliation_refusal_is_not_remembered(provider, project, tmp_path):
    provider.post_mode = "owner_lost"
    provider.lifecycle = (200, {"resource": "render", "owners": [
        provider.row("owner-A", status="owner_lost")]})
    first, _ = _run(project, tmp_path / "o1")
    assert first["skipped_because"] == "render_owner_owner_lost"
    _run(project, tmp_path / "o2", op="op-2")
    assert len(provider.posts) == 2 and provider.gets == 0


def test_native_timeout_is_still_a_failure_and_not_remembered(provider, project):
    provider.post_mode = "slow"
    payload = {"project_dir": str(project), "run_id": "run-1", "project_id": "p"}
    report = impl.post_playtest(payload, timeout=0.3)
    assert report["gate_timeout"] is True and report["passed"] is False
    assert impl._repeat_memory == {}


def test_memory_is_bounded(provider):
    for i in range(impl._REPEAT_MEMORY_MAX + 20):
        impl._remember_wait_expired(("u", "p", f"r{i}", "d"), {"owner_id": "o"})
    assert len(impl._repeat_memory) == impl._REPEAT_MEMORY_MAX
