"""A gate's own client timeout counts from admission, not from submission.

The game gate reads each render answer with one socket timeout (`/script`
1800 s, `/playtest` 3600 s), and the harness sends nothing while a request
waits in its render queue. With 1500 s of queue in front of it, a `/script`
render had 300 s left before the gate gave up on an answer that was still
coming, and the engine went on rendering for nobody (review gnr2, 2026-09-25).
The relay now keeps the socket alive until the harness admits the request.

Every test serves the REAL harness admission code (`tests/gate_fixture.py`),
with the render replaced by a sleep, at a scale where the queue is longer than
the gate's client timeout.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aitelier import gate_admission
from aitelier.tools.run_tests import impl as rt
from tests.gate_fixture import HarnessRig, write_gate


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    write_gate(repo)
    return repo


def _queued_gate(tmp_path, monkeypatch, *, render, queue, client_timeout):
    rig = HarnessRig(tmp_path / "harness", monkeypatch, render_seconds=render)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    monkeypatch.setenv("FIXTURE_GATE_CLIENT_TIMEOUT", str(client_timeout))
    monkeypatch.setenv("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS", "60")
    monkeypatch.setenv("AITELIER_REPO_GATE_KEEPALIVE_SECONDS", "0.5")
    try:
        rig.hold()
        threading.Timer(queue, rig.release.set).start()
        started = time.monotonic()
        gate = rt._run_repo_gate(_repo(tmp_path))
        elapsed = time.monotonic() - started
    finally:
        rig.close()
    return gate, elapsed, rig


def test_a_render_after_a_queue_longer_than_the_client_timeout_is_answered(
        tmp_path, monkeypatch):
    """Queue 4 s, render 1 s, client timeout 2 s."""
    gate, elapsed, rig = _queued_gate(tmp_path, monkeypatch, render=1.0,
                                      queue=4.0, client_timeout=2)
    assert elapsed > 4.0
    assert gate["returncode"] == 0, gate["output"]
    assert gate["measured"] == rt.REPO_GATE_MEASURED_PASS
    (request,) = gate["admission"]["requests"]
    assert request["outcome"] == gate_admission.ANSWERED
    assert request["admission_observed"] is True
    assert request["admitted_after_sec"] > 3.0
    assert request["render_owner_wait_sec"] > 3.0
    assert request["keepalives"] >= 4
    assert rig.rendered == ["op-holder", "repo"]


def test_the_client_timeout_still_bounds_the_render_itself(tmp_path, monkeypatch):
    """Queue 1.5 s, render 3 s, client timeout 2 s: once admitted, the gate's
    own timeout runs again, and a render longer than it is not waited out."""
    gate, _elapsed, rig = _queued_gate(tmp_path, monkeypatch, render=3.0,
                                       queue=1.5, client_timeout=2)
    assert gate["returncode"] == 2, gate["output"]
    (request,) = gate["admission"]["requests"]
    assert request["admitted_after_sec"] > 1.0
    assert gate["admission"]["state"] == gate_admission.ABANDONED
    assert rig.rendered == ["op-holder", "repo"]


class _NoLifecycle(BaseHTTPRequestHandler):
    """A builder with no owner table to read: answers /script late, 404s the rest."""
    delay = 2.5

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(self.delay)
        body = json.dumps({"passed": True, "results": [], "summary": "ok"}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *_a):
        pass


def test_no_keepalive_when_admission_cannot_be_seen(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NoLifecycle)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GODOT_BUILDER_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("FIXTURE_GATE_CLIENT_TIMEOUT", "1")
    monkeypatch.setenv("AITELIER_REPO_GATE_KEEPALIVE_SECONDS", "0.2")
    try:
        gate = rt._run_repo_gate(_repo(tmp_path))
    finally:
        server.shutdown()
        server.server_close()
    assert gate["returncode"] == 2
    (request,) = gate["admission"]["requests"]
    assert request["admission_observed"] is False
    assert "keepalives" not in request


def test_a_request_with_no_operation_id_gets_one_to_be_watched(tmp_path, monkeypatch):
    rig = HarnessRig(tmp_path / "harness", monkeypatch)
    relay = gate_admission.AdmissionRelay(rig.base, render_wait_sec=5,
                                          upstream_timeout=30, keepalive_sec=0.2)
    url = relay.start()
    try:
        req = urllib.request.Request(
            url + "/script", data=json.dumps({"project_dir": str(tmp_path)}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.status == 200
    finally:
        relay.stop()
        rig.close()
    (record,) = relay.snapshot()
    assert record["operation_id_injected"] is True
    (op,) = rig.rendered
    assert op.startswith("relay-")
