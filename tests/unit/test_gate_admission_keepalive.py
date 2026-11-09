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

import pytest

from aitelier import gate_admission
from aitelier.tools.run_tests import impl as rt
from tests.gate_fixture import (GATE_PY, HarnessRig, embed_env, install_entry_executor,
                                write_gate)


def _repo(tmp_path, *, client_timeout=None):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    write_gate(repo)
    if client_timeout is not None:
        # The executor entry scrubs the child environment, so a fixture value
        # reaches the gate only when the gate program carries it (`embed_env`).
        (repo / "gate.py").write_text(
            embed_env(GATE_PY, FIXTURE_GATE_CLIENT_TIMEOUT=str(client_timeout)))
    return repo


def _queued_gate(tmp_path, monkeypatch, *, render, queue, client_timeout):
    rig = HarnessRig(tmp_path / "harness", monkeypatch, render_seconds=render)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    monkeypatch.setenv("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS", "60")
    monkeypatch.setenv("AITELIER_REPO_GATE_KEEPALIVE_SECONDS", "0.5")
    try:
        rig.hold()
        threading.Timer(queue, rig.release.set).start()
        started = time.monotonic()
        gate = rt._run_repo_gate(_repo(tmp_path, client_timeout=client_timeout))
        elapsed = time.monotonic() - started
    finally:
        rig.close()
    return gate, elapsed, rig


def test_a_render_after_a_queue_longer_than_the_client_timeout_is_answered(
        tmp_path, monkeypatch):
    """Queue 4 s, render 1 s, client timeout 2 s."""
    install_entry_executor(monkeypatch)  # Real entry inside the outer test capsule.
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
    """Queue 1.5 s, render 6 s, client timeout 2 s: once admitted, the gate's
    own timeout runs again, and a render longer than it is not waited out.

    What the row pins is the record: the render must still be running when
    `stop()` cuts the upstream, or the harness answers first and the record
    reads `answered` instead of `abandoned`. That is a race between what is
    left of the render after the 2 s client timeout and the teardown, so the
    render value is the margin that keeps the race from deciding the row --
    not a constant that only has to beat the timeout.

    At the original 3.0 s the race decides the row in both directions, and the
    record holds eight failures and five passes of that construction. All
    eight end `assert 'answered' == 'abandoned'` with exit 1: three on the
    audit base 98e8007e11 carrying the embed_env client timeout (`.../work/
    docker-audit-r3-evidence-20261109/logs/arm3/keepalive-render-bound-
    run{1,2,3}/stdout-stderr.log`, the `:87` of that carrier tree), three on
    a scratch archive of 9eeb9f00c5 with only this render value reverted to
    3.0 (`.../evidence/dead-env-20261109/render30b-run{1,2,3}.log`), and two
    on the un-widened corrected tree (`.../work/docker-audit-r3-evidence-
    20261109/r4/logs/corrected-not-widened/render-bound-run{1,2}/
    stdout-stderr.log`, 4.72s / 4.71s). The five passes are the same
    construction landing the other way: three at `.../evidence/docker-rows-
    unmark-director-20261109/render30-run{1,2,3}.log` (4.27s / 4.51s / 4.25s)
    and two at `.../evidence/docker-audit-r5-20261109/narrow/keepalive-
    render-bound-run{1,2}/stdout-stderr.log` (4.25s / 4.21s).

    At 6.0 s the row is green in every retained run, and each run is named
    with the tree that printed it: the audit tree's r4 corrected rows
    (`.../work/docker-audit-r3-evidence-20261109/r4/logs/corrected/render-
    bound-run{1,2,3}/stdout-stderr.log`, 4.22s / 4.24s / 4.75s), the same
    three retained again at `.../evidence/docker-audit-r5-20261109/corrected/
    keepalive-render-bound-run{1,2,3}/stdout-stderr.log` (4.76s / 4.23s /
    4.24s), and two 11-row passes whose own header names the tree
    `9eeb9f00c5 + working-tree edits` with the three dead setenv lines already
    deleted (`.../evidence/dead-env-20261109/impl-test.log`, and
    `director-remesaure.log` beside it, which states no tree of its own).

    The teardown figures are measured, and the audit's probe file is where
    they live: `PROBE-7 _run_node_cmd took 4.051`, which that file labels
    entry teardown, and `PROBE-7 relay.stop took 0.5`, both under
    `teardown_budget_measured` in `.../evidence/docker-audit-r5-20261109/
    diff-98e8007e11-e32d5d16.diff`. The `~1 s` this docstring used to claim as
    "0.5 s of executor-entry teardown plus 0.5 s of relay shutdown" was
    neither: the entry teardown that probe recorded is 4.051 s, and the 0.5 s
    belongs to relay.stop."""
    install_entry_executor(monkeypatch)  # Real entry inside the outer test capsule.
    gate, _elapsed, rig = _queued_gate(tmp_path, monkeypatch, render=6.0,
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
    install_entry_executor(monkeypatch)  # Real entry inside the outer test capsule.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NoLifecycle)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GODOT_BUILDER_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("AITELIER_REPO_GATE_KEEPALIVE_SECONDS", "0.2")
    try:
        gate = rt._run_repo_gate(_repo(tmp_path, client_timeout=1))
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
