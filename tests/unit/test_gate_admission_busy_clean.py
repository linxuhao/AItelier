"""A busy but clean repository-gate run reads clean; a real wait still reads waited.

The render-contention judgement used to compare the SUMMED `render_owner_wait_sec`
of a run against the per-request floor (`RENDER_CONTENTION_WAIT_FLOOR_SEC`, one
second). Summed over a busy run, small per-request numbers add up: a request that
queued at all records at least `RENDER_OWNER_WAIT_POLL_SEC` (0.25 s), so a run of
many requests that each queued only briefly summed past one second without any
single request waiting a second, and the report claimed a wait behind "no owner
id recorded". The judgement is now per request: the largest single recorded wait
of the run must reach the floor, or a named owner must be present.

Two poles, both through the REAL `AdmissionRelay` (`gate_admission`) against a
fake upstream that answers each request with a render-queue answer:

  * `a-clean-busy-run-reads-clean`: N requests, each answered with 0.0115 s and
    naming no owner -- that number is this rig's INPUT, not a measurement of the
    harness's uncontended acquisition, which records exactly 0.0 because its
    clock starts at the first FAILED admission attempt. The summed wait is past
    the floor, yet the block reads `none` and the report summary carries no
    contention sentence.
  * `a-real-wait-is-still-named`: a request that queued behind a live owner
    reads `waited`, names that owner, carries the recorded wait, and prints the
    contention sentence. A run that queued and was also busy keeps the summed
    wait `admission_summary` recorded, not a new number.

The per-request rule is also pinned directly: two requests of 0.6 s, no owner,
sum past the floor and are still clean, because no single request reached it.

The report-site fold is driven through `run_tests` with the gate dict assembled
from the relay's own records (the same way the existing hand-built tests feed
`_acquire_repo_gate`); the real queue path is pinned by
`tests/unit/test_repo_gate_admission.py::test_the_real_gate_path_*`.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aitelier import gate_admission
from aitelier.tools.run_tests import impl as rt

# A hand-supplied sub-second wait this rig's fake upstream ANSWERS with (0.0115 s
# is not what the harness records for an uncontended acquisition -- that is
# exactly 0.0, its clock starting at the first FAILED admission attempt -- it is
# the number fed in here), and enough of them that their SUM is past the
# one-second floor.
UNCONTENDED_WAIT_SEC = 0.0115
UNCONTENDED_ACQUISITIONS = 100
OWNER = "8dc68cf4e96847a8b3df40e7d3752c5c"
REAL_WAIT_SEC = 0.551


class _Upstream:
    """A render-queue upstream answering every POST with the same answer.

    The GET for the owner table is 404, so the relay sends no interim responses
    and nothing races the answers under test.
    """

    def __init__(self, answer: dict):
        self.answer = answer
        self.server = None
        self.base = ""

    def start(self):
        upstream = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self._send(200, upstream.answer)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _through_relay(base: str, count: int) -> list[dict]:
    """Send `count` render requests through the REAL relay; return its records."""
    relay = gate_admission.AdmissionRelay(base, render_wait_sec=5, upstream_timeout=30)
    url = relay.start()
    try:
        for index in range(count):
            body = {"project_dir": "/tmp/proj", "project_id": "repo",
                    "run_id": "run-busy", "operation_id": f"op-{index}",
                    "scripts": ["res://tests/a.gd"]}
            req = urllib.request.Request(url + "/script",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                assert r.status == 200, r.status
                r.read()
    finally:
        relay.stop()
    return relay.snapshot()


def _answer(*, wait: float, owners=()) -> dict:
    """The render queue's success answer, in the names the harness uses."""
    return {"passed": True, "results": [], "summary": "ok",
            "render_owner_wait_sec": wait,
            "render_owner_waited_for_owner_ids": list(owners)}


def _report_for(tmp_path, monkeypatch, admission: dict, name: str) -> dict:
    """Run a passing `run_tests` whose repository gate carries `admission`'s block."""
    gate = {"script": "run_tests.sh", "returncode": 0, "passed": True,
            "output": "gate: all green", "output_truncated": False,
            "admission": admission | {"ticket": f"rt-{name}", "upstream": "http://x"},
            "render_contention": rt._render_contention(admission),
            "ticket": f"rt-{name}", "retained_findings": 0,
            "gate_coverage": None}
    gate["measured"] = rt._repo_gate_outcome(gate)
    monkeypatch.setattr(rt, "_acquire_repo_gate", lambda *_a, **_k: gate)
    repo = tmp_path / name / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    out = tmp_path / name / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out), run_id=name)
    return json.loads((out / "test_report.json").read_text())


# ── a-clean-busy-run-reads-clean ───────────────────────────────────────────

def test_a_busy_but_clean_run_reads_clean_through_the_real_relay(tmp_path, monkeypatch):
    """N uncontended acquisitions through the real relay: the summed wait is past
    the floor, every request is below it, and the run reads clean everywhere."""
    upstream = _Upstream(_answer(wait=UNCONTENDED_WAIT_SEC)).start()
    try:
        records = _through_relay(upstream.base, UNCONTENDED_ACQUISITIONS)
    finally:
        upstream.stop()

    assert len(records) == UNCONTENDED_ACQUISITIONS, len(records)
    assert all(r["outcome"] == gate_admission.ANSWERED for r in records)
    admission = gate_admission.admission_summary(records)

    # The raw recorded number is what the run really summed: past the floor.
    assert admission["render_owner_wait_sec"] == round(
        UNCONTENDED_WAIT_SEC * UNCONTENDED_ACQUISITIONS, 3)
    assert admission["render_owner_wait_sec"] >= rt.RENDER_CONTENTION_WAIT_FLOOR_SEC
    # Every single request is under the floor, and nobody was queued behind.
    assert admission["queued_behind"] == []
    assert all(gate_admission.recorded_render_wait(r) < rt.RENDER_CONTENTION_WAIT_FLOOR_SEC
               for r in admission["requests"])

    contention = rt._render_contention(admission)
    assert contention == {"owners": [], "wait_sec": 0.0, "state": "none"}, contention

    report = _report_for(tmp_path, monkeypatch, admission, "busy-clean")
    assert report["render_contention"] == {"owners": [], "wait_sec": 0.0, "state": "none"}
    assert "render_contention_absent" not in report
    assert "contention" not in report["summary"].lower()
    assert "render owner" not in report["summary"].lower()


def test_no_single_request_reaching_the_floor_is_clean_however_much_they_sum_to():
    """The rule itself: two requests of 0.6 s each sum to 1.2 s, past the floor,
    but neither request waited for a second, so the run reads clean."""
    records = [
        {"seq": index, "route": "/script", "method": "POST", "outcome": "answered",
         "status": 200, "delivered": True, "render_owner_wait_sec": 0.6,
         "render_owner_waited_for_owner_ids": []}
        for index in range(2)
    ]
    admission = gate_admission.admission_summary(records)
    assert admission["render_owner_wait_sec"] == 1.2
    assert rt._render_contention(admission) == {
        "owners": [], "wait_sec": 0.0, "state": "none"}


def test_a_single_request_at_the_floor_with_no_owner_is_still_contention():
    """The per-request floor still bites: one request that waited a full second
    behind nobody the queue could name is contention, with the recorded wait."""
    record = {"seq": 0, "route": "/script", "method": "POST", "outcome": "answered",
              "status": 200, "delivered": True, "render_owner_wait_sec": 1.0,
              "render_owner_waited_for_owner_ids": []}
    contention = rt._render_contention(gate_admission.admission_summary([record]))
    assert contention == {"owners": [], "wait_sec": 1.0, "state": "waited"}


# ── a-real-wait-is-still-named ─────────────────────────────────────────────

def test_a_real_wait_behind_a_named_owner_is_named_through_the_real_relay(
        tmp_path, monkeypatch):
    """One request queued behind a live owner: the relay records the owner and
    the wait, the block reads `waited` naming that owner, and the report prints
    the contention sentence with the recorded wait."""
    upstream = _Upstream(_answer(wait=REAL_WAIT_SEC, owners=[OWNER])).start()
    try:
        records = _through_relay(upstream.base, 1)
    finally:
        upstream.stop()

    admission = gate_admission.admission_summary(records)
    assert admission["queued_behind"] == [OWNER]
    assert admission["render_owner_wait_sec"] == REAL_WAIT_SEC

    contention = rt._render_contention(admission)
    assert contention == {"owners": [OWNER], "wait_sec": REAL_WAIT_SEC,
                          "state": "waited"}, contention

    report = _report_for(tmp_path, monkeypatch, admission, "real-wait")
    assert report["render_contention"] == contention
    assert report["summary"].startswith("Render contention:"), report["summary"][:200]
    assert OWNER in report["summary"]
    assert f"{REAL_WAIT_SEC:.3f}s" in report["summary"]


def test_a_real_wait_inside_a_busy_run_keeps_the_summed_wait_it_recorded(
        tmp_path, monkeypatch):
    """A busy run that also queued behind an owner: the block names the owner and
    reports the SUM the summary recorded (auditable), not a per-request figure
    and not a rounded claim."""
    clean = _Upstream(_answer(wait=UNCONTENDED_WAIT_SEC)).start()
    try:
        records = _through_relay(clean.base, UNCONTENDED_ACQUISITIONS - 1)
    finally:
        clean.stop()
    waited = _Upstream(_answer(wait=REAL_WAIT_SEC, owners=[OWNER])).start()
    try:
        records += _through_relay(waited.base, 1)
    finally:
        waited.stop()
    for index, record in enumerate(records):
        record["seq"] = index

    admission = gate_admission.admission_summary(records)
    # The sum the summary records: the same per-record values, accumulated in
    # the relay's `seq` order by a plain running total and rounded once, exactly
    # as `admission_summary` does (not a hand-rounded literal, and not the
    # built-in `sum()`, which accumulates differently at the third decimal).
    expected = 0.0
    for record in sorted(records, key=lambda r: r["seq"]):
        expected += float(record["render_owner_wait_sec"] or 0.0)
    expected = round(expected, 3)
    assert admission["render_owner_wait_sec"] == expected
    contention = rt._render_contention(admission)
    assert contention == {"owners": [OWNER], "wait_sec": expected, "state": "waited"}

    report = _report_for(tmp_path, monkeypatch, admission, "busy-real")
    assert report["summary"].startswith("Render contention:")
    assert OWNER in report["summary"]
    assert f"{expected:.3f}s" in report["summary"]
