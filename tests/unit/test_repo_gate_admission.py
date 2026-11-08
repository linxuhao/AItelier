"""A repository gate reaches the engine through the render queue, and a gate
the engine did not admit is a third outcome, not a failing test.

The tests marked `baked_toolchain` at the top serve the REAL harness admission
code (`tests/gate_fixture.py`): its durable render-owner table, its queue and
its 409 payloads. The repository gate is a script with the game gate's
transport and exit-code contract, run by the REAL `run_tests` through the
admission relay.

The render-contention block has two kinds of test, and they are kept apart on
purpose. `Hand-built records` tests fold an `admission` summary the test builds
in the relay's own field shape; they cover `_render_contention` and the report's
summary handling, NOT the wire from the relay through the gate result. The
`real relay` tests below (`test_the_real_gate_path_*` and
`test_a_real_uncontended_gate_run_records_no_contention`) run the whole path —
`run_tests` -> `_acquire_repo_gate` -> `_run_repo_gate` -> the real relay ->
the rig's real harness queue — and never supply a gate dict, so the block they
read is the one the gate site wrote.

Criteria (State node `harness.a-gate-that-could-not-run-is-not-a-failing-test`):
  * coding-impl-stops-using-409-as-control-flow — two concurrent gates are
    each ADMITTED by the queue; neither learns anything from a 409;
  * contention-is-a-distinct-outcome — a busy render lock gives a structured
    not-admitted outcome (pole 1); a gate that ran and went red is still a
    code red (pole 2).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from aitelier import gate_admission
from aitelier.gate_evidence import release_disposition, report_state
from aitelier.tools.run_tests import impl as rt
from tests.gate_fixture import (HOLDER, HarnessRig, install_entry_executor,
                                red_report, write_gate)



@pytest.fixture
def rig(tmp_path, monkeypatch):
    rig = HarnessRig(tmp_path / "harness", monkeypatch)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    yield rig
    rig.close()


def _repo(tmp_path, name="repo"):
    repo = tmp_path / name
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    write_gate(repo)
    return repo


def _report(out):
    return json.loads((Path(out) / "test_report.json").read_text())


# ── coding-impl-stops-using-409-as-control-flow ────────────────────────────

@pytest.mark.baked_toolchain  # needs the pinned node/npm the image does not bake
def test_two_concurrent_repo_gates_are_each_admitted_by_the_queue(
        tmp_path, monkeypatch):
    """Gate A holds the render lock while gate B arrives. B queues behind A's
    owner and is admitted when A releases: both get an admission answer, both
    gates measure, and no request of either is answered 409."""
    rig = HarnessRig(tmp_path / "harness", monkeypatch, render_seconds=1.0)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    try:
        repo_a, repo_b = _repo(tmp_path, "gate-a"), _repo(tmp_path, "gate-b")
        results: dict = {}

        def run(name, repo):
            results[name] = rt._run_repo_gate(repo)

        a = threading.Thread(target=run, args=("a", repo_a))
        a.start()
        assert rig.entered_event("gate-a").wait(30), "gate A never rendered"
        b = threading.Thread(target=run, args=("b", repo_b))
        b.start()
        a.join(60)
        b.join(60)
        owners = {r["operation_id"]: r["owner_id"] for r in rig.owners()}
    finally:
        rig.close()

    for name in ("a", "b"):
        gate = results[name]
        assert gate["returncode"] == 0, gate["output"]
        assert gate["measured"] == rt.REPO_GATE_MEASURED_PASS
        assert gate["admission"]["state"] == gate_admission.ANSWERED
        (request,) = gate["admission"]["requests"]
        assert request["status"] == 200 and request["outcome"] == "answered"
        assert request["render_wait_injected"] is True
        assert request["render_wait_timeout_sec"] == gate_admission.RENDER_WAIT_SECONDS
    statuses = [r["status"] for g in results.values()
                for r in g["admission"]["requests"]]
    assert 409 not in statuses, statuses
    # A was admitted at once; B was admitted after queueing behind A's owner.
    assert results["a"]["admission"]["queued_behind"] == []
    assert results["b"]["admission"]["queued_behind"] == [owners["gate-a"]]
    assert results["b"]["admission"]["render_owner_wait_sec"] > 0.3
    assert [op for op in rig.rendered] == ["gate-a", "gate-b"]


def test_a_gate_that_sends_its_own_wait_keeps_it(rig, tmp_path):
    """The relay fills `render_wait_timeout_sec` only when the gate sent none.

    "Sent none" covers both a missing key and an explicit JSON `null`: the
    harness reads a null wait as "queue forever", so forwarding one would drop
    the bound the relay exists to impose. A number the gate sent is kept as
    sent.
    """
    relay = gate_admission.AdmissionRelay(rig.base, render_wait_sec=7,
                                          upstream_timeout=30)
    url = relay.start()
    try:
        import urllib.request
        for body in ({"project_dir": str(tmp_path), "operation_id": "own",
                      "render_wait_timeout_sec": 3},
                     {"project_dir": str(tmp_path), "operation_id": "none"},
                     {"project_dir": str(tmp_path), "operation_id": "null",
                      "render_wait_timeout_sec": None}):
            req = urllib.request.Request(url + "/script", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                assert r.status == 200
    finally:
        relay.stop()
    own, injected, null = relay.snapshot()
    assert own["render_wait_timeout_sec"] == 3 and "render_wait_injected" not in own
    assert injected["render_wait_timeout_sec"] == 7 and injected["render_wait_injected"]
    assert null["render_wait_timeout_sec"] == 7, null
    assert null["render_wait_injected"] is True, null


# ── contention-is-a-distinct-outcome ───────────────────────────────────────

@pytest.mark.baked_toolchain  # needs the pinned node/npm the image does not bake
@pytest.mark.parametrize("holder", ["live_render", "owner_lost"])
def test_pole_1_a_gate_the_engine_did_not_admit_is_not_run(
        rig, tmp_path, monkeypatch, holder):
    """Another owner holds the render lock. `live_render`: a render is in
    flight and the request's queue window runs out; `owner_lost`: the holder
    died and needs reconciliation, so the harness refuses at once. Either way
    the harness answers 409 with its structured payload, the gate exits 2, and
    the report says NOT RUN in structured fields — never a code red."""
    monkeypatch.setenv("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS", "0.5")
    if holder == "live_render":
        rig.hold()
    else:
        rig.mark_owner_lost()
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    result = rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)
    gate = report["repo_gate"]

    assert gate["returncode"] == 2
    assert gate["measured"] == rt.REPO_GATE_UNMEASURED
    assert report["repo_gate_admission"] == gate_admission.NOT_ADMITTED
    (request,) = gate["admission"]["requests"]
    assert request["status"] == 409 and request["outcome"] == "not_admitted"
    assert request["owner_kind"] == ("active" if holder == "live_render"
                                     else "owner_lost")
    assert request["needs_reconciliation"] is (holder == "owner_lost")
    # Not run, and non-passing: never a pass, never a code red.
    assert report["passed"] is False and result["passed"] is False
    assert report["repo_gate_absent"] is True and result["repo_gate_absent"] is True
    assert report["evidence_state"] == "not_run"
    assert report_state(report) == "skipped"
    assert result["release_evidence"] == release_disposition(report) == "unresolved"
    assert "failure_identity_error" not in report
    assert report["passed_relative"] is False
    # The render the gate never got never started for it.
    assert repo.name not in rig.rendered
    admission = json.loads((Path(gate["report_dir"]) / "admission.json").read_text())
    assert admission["state"] == "not_admitted"
    rig.let_go()


@pytest.mark.baked_toolchain  # needs the pinned node/npm the image does not bake
def test_pole_2_a_gate_that_ran_and_went_red_is_a_code_red(rig, tmp_path):
    """The engine admitted and answered; two GDScript suites failed; the gate
    exits 1. That is a measured red with both identities, the release reading
    is a product failure, and nothing routes it to the absence gate."""
    rig.script_report = red_report()
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    result = rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)
    gate = report["repo_gate"]

    assert gate["returncode"] == 1
    assert gate["measured"] == rt.REPO_GATE_MEASURED_FAIL
    assert report["repo_gate_admission"] == gate_admission.ANSWERED
    assert report["passed"] is False and result["passed"] is False
    assert result["repo_gate_absent"] is False
    assert "repo_gate_absent" not in report and "evidence_state" not in report
    assert report_state(report) == "failed"
    assert result["release_evidence"] == "known_failure"
    assert "failure_identity_error" not in report
    details = sorted(c["detail"] for c in gate["failure_cases"])
    assert details == ["script gate failed: 0 passed, 2 failed",
                       "script res://tests/a.gd FAILED",
                       "script res://tests/b.gd FAILED"]
    assert repo.name in rig.rendered


@pytest.mark.baked_toolchain  # needs the pinned node/npm the image does not bake
def test_an_unreachable_engine_is_not_run(tmp_path, monkeypatch):
    """Nothing listens at the builder URL: the relay records `unreachable`,
    the gate exits 2, and the report says not run."""
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setenv("GODOT_BUILDER_URL", f"http://127.0.0.1:{port}")
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    result = rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)
    assert report["repo_gate"]["returncode"] == 2
    assert report["repo_gate"]["measured"] == rt.REPO_GATE_UNMEASURED
    assert report["repo_gate_admission"] == gate_admission.UNREACHABLE
    assert result["repo_gate_absent"] is True and report["passed"] is False


@pytest.mark.parametrize("returncode", [2, 3, 124])
def test_an_answered_gate_that_exits_nonzero_stays_a_measured_failure(returncode):
    """An exit code other than 0/1 is not-run only when the engine refused or
    could not be reached; with an answered last request it is a red."""
    gate = {"returncode": returncode, "output": "", "output_truncated": False,
            "admission": {"state": gate_admission.ANSWERED}}
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_MEASURED_FAIL
    gate["admission"]["state"] = gate_admission.NOT_ADMITTED
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_UNMEASURED


def test_exit_1_is_a_red_whatever_the_relay_saw():
    gate = {"returncode": 1, "output": "", "output_truncated": False,
            "admission": {"state": gate_admission.NOT_ADMITTED}}
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_MEASURED_FAIL


def test_the_summary_reads_the_last_request():
    answered = {"seq": 0, "outcome": "answered", "delivered": True}
    refused = {"seq": 1, "outcome": "not_admitted", "status": 409}
    assert gate_admission.admission_summary([answered, refused])["state"] == "not_admitted"
    assert gate_admission.admission_summary([refused | {"seq": 0}, answered | {"seq": 1}]
                                            )["state"] == "answered"
    assert gate_admission.admission_summary([{"seq": 0}])["state"] == "abandoned"
    assert gate_admission.admission_summary(
        [answered | {"delivered": False}])["state"] == "undelivered"
    assert gate_admission.admission_summary([])["state"] == "no_engine_request"


# ── render-contention is named by the gate's own admission records ─────────
#
# HAND-BUILT RECORDS: the dicts below are assembled by the test in the relay's
# REAL field names and the harness's REAL answer fields
# (`render_owner_waited_for_owner_ids`, `render_owner_wait_sec`, `outcome`; see
# `docker/godot/godot_harness.py` and `gate_admission._ANSWER_FIELDS`) and then
# folded by the REAL `gate_admission.admission_summary` and the REAL
# `rt._render_contention`. No relay, no queue and no harness run in these: they
# pin what the fold does with records shaped like the relay's. That the relay
# and the gate site actually produce such a block is a different claim, and it
# is the `real relay` tests below that make it.

def _answered(seq, *, waited_for=(), wait=0.0, outcome="answered", status=200):
    """One relay record shaped as `AdmissionRelay._relay` writes it."""
    record = {"seq": seq, "route": "/script", "method": "POST",
              "outcome": outcome, "status": status, "delivered": True,
              "render_owner_wait_sec": wait,
              "render_owner_waited_for_owner_ids": list(waited_for)}
    return record


def test_a_gate_that_waited_names_the_owner_and_the_summed_wait():
    """Two requests queued behind the same foreign owner, then one behind
    another. The block names exactly those owner ids, de-duplicated in the
    order the records name them, and the summed seconds; the summary line names
    the wait and the owners so a reader sees it without the JSON."""
    records = [
        _answered(0, waited_for=["owner-A"], wait=180.9249),
        _answered(1, waited_for=["owner-A", "owner-B"], wait=2.5),
    ]
    admission = gate_admission.admission_summary(records)
    contention = rt._render_contention(admission)

    assert contention == {"owners": ["owner-A", "owner-B"],
                          "wait_sec": round(180.9249 + 2.5, 3),
                          "state": "waited"}
    sentence = rt._contention_sentence(contention)
    assert "183.425s" in sentence
    assert "owner-A" in sentence and "owner-B" in sentence
    assert sentence.startswith("Render contention:")


def test_a_gate_that_did_not_wait_has_no_contention_and_says_nothing():
    """No owner was named and no wait recorded: the block is the clean default
    and the summary line is never written."""
    records = [_answered(0), _answered(1, waited_for=[], wait=0.0)]
    contention = rt._render_contention(gate_admission.admission_summary(records))
    assert contention == {"owners": [], "wait_sec": 0.0, "state": "none"}
    # The summary line is written only for a `waited` state. A clean run's
    # report must never name a contention it did not have.
    assert contention["state"] != "waited"


def test_no_engine_request_is_a_clean_none():
    contention = rt._render_contention(gate_admission.admission_summary([]))
    assert contention == {"owners": [], "wait_sec": 0.0, "state": "none"}


def test_a_clean_gate_run_names_no_contention_in_its_report(tmp_path, monkeypatch):
    """HAND-BUILT GATE RECORD, REAL REPORT FOLD. The gate dict is assembled here
    from an `admission` summary of one clean answered record, with its
    `render_contention` written by this test, and `_acquire_repo_gate` is
    monkeypatched to return it. No relay runs and no harness queue runs.

    What this pins is the report site's handling of a clean block: the report
    takes the `none` state through and its summary never mentions contention.
    That the real gate path writes the clean block is pinned by
    `test_a_real_uncontended_gate_run_records_no_contention`, which runs the
    whole path with no gate dict supplied."""
    clean = _answered(0)
    admission = gate_admission.admission_summary([clean])
    gate = {"script": "run_tests.sh", "returncode": 0, "passed": True,
            "output": "gate: all green", "output_truncated": False,
            "admission": admission | {"ticket": "rt-clean", "upstream": "http://x"},
            "render_contention": rt._render_contention(admission),
            "ticket": "rt-clean", "retained_findings": 0,
            "gate_coverage": None}
    gate["measured"] = rt._repo_gate_outcome(gate)
    assert gate["render_contention"] == {"owners": [], "wait_sec": 0.0, "state": "none"}

    monkeypatch.setattr(rt, "_acquire_repo_gate", lambda *_a, **_k: gate)
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out), run_id="clean-gate")
    report = json.loads((out / "test_report.json").read_text())

    assert report["render_contention"] == {"owners": [], "wait_sec": 0.0, "state": "none"}
    assert "contention" not in report["summary"].lower()
    assert "render owner" not in report["summary"].lower()


def test_a_contended_run_that_went_red_stays_a_measured_red(tmp_path, monkeypatch):
    """A contended gate that then ran and went RED is still `measured_fail`:
    the contention is named, and the outcome class is exactly what it was before
    the field existed. Outcome classification reads `admission.state` and the
    exit code only; `render_contention` changes neither.

    HAND-BUILT RECORDS, REAL REPORT FOLD. `run_tests` is real and its report
    fold is real, but the gate dict it is handed is assembled by this test from
    an `admission` summary the test folded itself: no relay runs, no harness
    queue runs, and `render_contention` is written by the test. What this pins
    is therefore the report's handling of a gate result that carries the block.
    That the RELAY and the gate site produce such a block is pinned by
    `test_the_real_gate_path_names_a_queued_render_owner_on_the_gate_result`.
    """
    contended = _answered(0, waited_for=["owner-A"], wait=180.9249)
    admission = gate_admission.admission_summary([contended])
    gate = {"script": "run_tests.sh", "returncode": 1, "passed": False,
            "output": "gate: script FAILED", "output_truncated": False,
            "admission": admission | {"ticket": "rt-test", "upstream": "http://x"},
            "render_contention": rt._render_contention(admission),
            "ticket": "rt-test", "retained_findings": 1,
            "failure_cases": [{"case_id": "playtest/a", "detail": "red"}],
            "gate_coverage": None}
    gate["measured"] = rt._repo_gate_outcome(gate)
    assert gate["measured"] == rt.REPO_GATE_MEASURED_FAIL

    # The same gate without the contention is the same outcome class.
    plain = dict(gate, admission=gate_admission.admission_summary([_answered(0)]),
                 render_contention=rt._render_contention({}))
    plain["measured"] = rt._repo_gate_outcome(plain)
    assert plain["measured"] == gate["measured"] == rt.REPO_GATE_MEASURED_FAIL

    monkeypatch.setattr(rt, "_acquire_repo_gate", lambda *_a, **_k: gate)
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    out = tmp_path / "out"
    result = rt.run_tests(project_root=str(repo), out_dir=str(out),
                          run_id="contended-red")
    report = json.loads((out / "test_report.json").read_text())

    # The contention is named, in the JSON and in the summary a reader sees.
    assert report["render_contention"] == {"owners": ["owner-A"],
                                           "wait_sec": 180.925, "state": "waited"}
    assert report["summary"].startswith("Render contention:")
    assert "owner-A" in report["summary"] and "180.925s" in report["summary"]
    # ...and the outcome class is still a measured red: a product failure, not
    # a "did not run" absence.
    assert report["passed"] is False and result["passed"] is False
    assert "repo_gate_absent" not in report
    assert report.get("evidence_state") != "not_run"
    assert result["repo_gate_absent"] is False
    assert report["repo_gate"]["measured"] == rt.REPO_GATE_MEASURED_FAIL


# ── the block is WIRED: the real relay, through the gate result ────────────
#
# `2ede301c` shipped the block and a test suite that never removed its only
# producer: deleting `result["render_contention"] = _render_contention(admission)`
# from `_run_repo_gate` left every test green, because each one fed a gate dict
# that already carried the key. The tests below feed no gate dict at all: they
# run `run_tests` -> `_acquire_repo_gate` -> `_run_repo_gate` -> the REAL
# admission relay -> the rig's REAL harness queue (`tests/gate_fixture.py`) and
# read whatever block the gate site wrote.

RENDER_HOLD_SEC = 0.5   # how long the gate's request stays queued, once seen
GATE_POST_DEADLINE_SEC = 60


def _await_gate_request(home: Path, deadline: float = GATE_POST_DEADLINE_SEC) -> bool:
    """Wait until the fixture gate has created its report directory.

    `GATE_PY` makes that directory immediately before it POSTs its engine
    request, so its appearance is the test's only view of "the request is
    about to arrive". Dynamic on purpose: a fixed sleep would either race the
    gate or hold the render lock for a wait the test cannot name.
    """
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if any(p.is_dir() and p.name.startswith("fixture-gate-")
               for p in (home / "gate-reports").rglob("*")):
            return True
        time.sleep(0.02)
    return False


def _contended_red_run(tmp_path, monkeypatch):
    """Run the REAL gate path over a repo whose gate QUEUES behind a render owner.

    A holder operation owns the harness's render lock; the gate's request is
    posted while the holder renders, so the harness queues it and answers with
    `render_owner_waited_for_owner_ids` = the holder's owner id once the test
    releases the lock. The gate then measures a red, so the report is a red.
    Nothing here supplies a gate dict.
    """
    rig = HarnessRig(tmp_path / "harness", monkeypatch)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    home = tmp_path / "home"
    monkeypatch.setenv("AITELIER_HOME", str(home))
    rig.script_report = red_report()
    install_entry_executor(monkeypatch)
    repo = _repo(tmp_path)
    out = tmp_path / "out"
    results: dict = {}

    def run():
        results["value"] = rt.run_tests(project_root=str(repo), out_dir=str(out),
                                        run_id="contended-red")

    rig.hold()
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert _await_gate_request(home), "the gate never posted its engine request"
        # The request is on its way and the holder still owns the render, so it
        # has to queue. Hold it there long enough that the wait is a number a
        # reader can see, then let it through.
        time.sleep(RENDER_HOLD_SEC)
    finally:
        rig.let_go()
        worker.join(180)
    try:
        holders = {r["operation_id"]: r["owner_id"] for r in rig.owners()}
    finally:
        rig.close()
    assert "value" in results, "run_tests did not return"
    return results["value"], _report(out), holders[HOLDER]


def test_the_real_gate_path_names_a_queued_render_owner_on_the_gate_result(
        tmp_path, monkeypatch):
    """The STRUCTURED surface, from the real relay: the gate result the fold
    receives carries the holder's owner id, the summed wait and `state:
    "waited"`, and the run it belongs to is a measured red.

    No gate dict is supplied and no `render_contention` value is written by
    this test, so deleting the assignment at the gate site reds here.
    """
    result, report, holder = _contended_red_run(tmp_path, monkeypatch)
    gate = report["repo_gate"]
    admission = gate["admission"]

    assert admission["state"] == gate_admission.ANSWERED
    (request,) = admission["requests"]
    assert request["outcome"] == "answered"
    assert admission["queued_behind"] == [holder], admission
    assert admission["render_owner_wait_sec"] > 0.3, admission

    assert gate["measured"] == rt.REPO_GATE_MEASURED_FAIL
    assert gate["render_contention"] == {
        "owners": [holder],
        "wait_sec": round(admission["render_owner_wait_sec"], 3),
        "state": "waited"}, gate.get("render_contention")
    assert report["passed"] is False and result["passed"] is False


def test_the_real_gate_path_repeats_the_contention_in_the_red_runs_summary(
        tmp_path, monkeypatch):
    """The SURFACE A READER SEES, from the same real path: the red run's summary
    leads with the contention sentence, naming the owner id the harness named
    and the wait the relay measured.

    Deleting the gate-site assignment reds here too: the report then carries
    the `unreported` state (never `none`) and writes no sentence.
    """
    _, report, holder = _contended_red_run(tmp_path, monkeypatch)
    contention = report["render_contention"]

    assert contention["state"] == "waited", contention
    assert contention["owners"] == [holder]
    assert "render_contention_absent" not in report
    summary = report["summary"]
    assert summary.startswith("Render contention:"), summary[:200]
    assert holder in summary, summary[:300]
    assert f"{contention['wait_sec']:.3f}s" in summary, summary[:300]


def test_a_real_uncontended_gate_run_records_no_contention(tmp_path, monkeypatch):
    """The same real path with nobody holding the render lock: the block is the
    clean one and the summary never mentions contention.

    This is the case `_render_contention`'s wait floor exists for. The harness
    starts its wait clock when the request begins TAKING ownership, so an
    uncontended acquisition still records a few milliseconds (measured 0.0115s
    through this rig); without the floor every clean run would report `waited`
    and prefix its summary with a contention sentence.
    """
    rig = HarnessRig(tmp_path / "harness", monkeypatch)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    install_entry_executor(monkeypatch)
    try:
        repo = _repo(tmp_path)
        out = tmp_path / "out"
        rt.run_tests(project_root=str(repo), out_dir=str(out), run_id="clean-gate")
    finally:
        rig.close()
    report = _report(out)

    assert report["repo_gate"]["render_contention"] == {
        "owners": [], "wait_sec": 0.0, "state": "none"}
    assert report["render_contention"] == {
        "owners": [], "wait_sec": 0.0, "state": "none"}
    assert "contention" not in report["summary"].lower()
    assert "render owner" not in report["summary"].lower()


def test_a_sub_second_recorded_wait_is_not_contention():
    """The floor, on the number the harness really records for an uncontended
    acquisition: taking ownership cost 0.0115s, nobody was queued, and that is
    NOT a wait behind anybody. The raw number stays in the admission summary."""
    admission = gate_admission.admission_summary(
        [_answered(0, waited_for=[], wait=0.0115)])
    assert admission["render_owner_wait_sec"] == 0.011
    assert rt._render_contention(admission) == {
        "owners": [], "wait_sec": 0.0, "state": "none"}
    # A named owner is contention whatever the seconds, and a wait past the
    # floor is contention even when it named nobody (a request queued behind
    # the deployment fence leaves no owner row either).
    assert rt._render_contention(
        gate_admission.admission_summary([_answered(0, waited_for=["owner-A"], wait=0.2)])
    )["state"] == "waited"
    assert rt._render_contention(
        gate_admission.admission_summary([_answered(0, waited_for=[], wait=42.0)])
    ) == {"owners": [], "wait_sec": 42.0, "state": "waited"}


# ── NO FAIL-OPEN: a missing block is not a measured clean run ──────────────

def test_a_gate_result_without_the_block_is_not_a_measured_clean_run(
        tmp_path, monkeypatch):
    """A gate result that never got `render_contention` used to be rendered as
    `{"owners": [], "state": "none", "wait_sec": 0.0}` — a measured ABSENCE of
    contention — by the `or _render_contention({})` fallback at the report site.
    A missing wire then read as a clean run.

    It is now `state: "unreported"` with an explicit absence field and NO wait
    number (0.0 would be a measurement), and no sentence is invented.
    """
    gate = {"script": "run_tests.sh", "returncode": 0, "passed": True,
            "output": "gate: all green", "output_truncated": False,
            "admission": gate_admission.admission_summary([_answered(0)])
                          | {"ticket": "rt-noblock", "upstream": "http://x"},
            "ticket": "rt-noblock", "retained_findings": 0,
            "gate_coverage": None}
    gate["measured"] = rt._repo_gate_outcome(gate)
    assert "render_contention" not in gate            # the wire that is missing

    monkeypatch.setattr(rt, "_acquire_repo_gate", lambda *_a, **_k: gate)
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out), run_id="no-block")

    report = json.loads((out / "test_report.json").read_text())
    block = report["render_contention"]
    assert block["state"] == rt.RENDER_CONTENTION_UNREPORTED
    assert block["state"] != "none"
    assert block["owners"] == [] and block["wait_sec"] is None
    assert report["render_contention_absent"]
    assert "render_contention" in report["render_contention_absent"]
    assert "contention" not in report["summary"].lower()
    assert "render owner" not in report["summary"].lower()


def test_a_gate_result_that_measured_none_keeps_the_clean_block(
        tmp_path, monkeypatch):
    """The other half: a gate result that HAS the block with `state: "none"`
    (nobody was queued, the wait was sub-floor) still produces exactly
    `{"owners": [], "wait_sec": 0.0, "state": "none"}` on the report — no
    absence field, and no sentence."""
    gate = {"script": "run_tests.sh", "returncode": 0, "passed": True,
            "output": "gate: all green", "output_truncated": False,
            "admission": gate_admission.admission_summary([_answered(0, wait=0.0115)])
                          | {"ticket": "rt-clean", "upstream": "http://x"},
            "render_contention": {"owners": [], "wait_sec": 0.0, "state": "none"},
            "ticket": "rt-clean", "retained_findings": 0, "gate_coverage": None}
    gate["measured"] = rt._repo_gate_outcome(gate)

    monkeypatch.setattr(rt, "_acquire_repo_gate", lambda *_a, **_k: gate)
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out), run_id="measured-none")

    report = json.loads((out / "test_report.json").read_text())
    assert report["render_contention"] == {
        "owners": [], "wait_sec": 0.0, "state": "none"}
    assert "render_contention_absent" not in report
    assert "contention" not in report["summary"].lower()


def test_a_gate_that_never_started_is_not_a_measured_clean_queue(
        tmp_path, monkeypatch):
    """A repository gate whose admission relay could not be started never
    reached the render queue, so nothing measured it. The early return in
    `_run_repo_gate` used to stamp `_render_contention({})` on that run, which
    reads as `state: "none"` — a measured clean queue — and the report folded it
    through as one.

    REAL PATH, NO GATE DICT: `run_tests` -> `_acquire_repo_gate` ->
    `_run_repo_gate`, with the relay start forced to fail by the same seam
    `test_overlong_namespace_is_typed_before_bind_and_preserves_existing_peer`
    uses (an `AITELIER_HOME` whose socket path overruns AF_UNIX's 107-byte
    budget, so `relay_socket_path` raises before any bind). The gate is a real
    declared `run_tests.sh`, but it never runs, so its own answer cannot
    decide what the block says.
    """
    home = tmp_path / ("h" * 100)
    home.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(home))
    install_entry_executor(monkeypatch)
    repo = _repo(tmp_path)
    out = tmp_path / "out"

    rt.run_tests(project_root=str(repo), out_dir=str(out), run_id="never-started")
    report = _report(out)

    gate = report["repo_gate"]
    assert gate["runner_error"] is True
    assert gate["measured"] == rt.REPO_GATE_UNMEASURED
    assert "107-byte filesystem budget" in gate["output"]
    # The gate result names the absence and asserts no measurement.
    assert gate["render_contention"]["state"] == rt.RENDER_CONTENTION_UNREPORTED
    assert gate["render_contention"]["state"] != "none"
    assert gate["render_contention"]["wait_sec"] is None
    # The report carries that named absence, not a clean block.
    assert report["render_contention"]["state"] == rt.RENDER_CONTENTION_UNREPORTED
    assert report["render_contention"]["state"] != "none"
    assert report["render_contention"]["wait_sec"] is None
    # No contention sentence is invented for a gate that never reached the queue.
    assert "contention" not in report["summary"].lower()
    assert "render owner" not in report["summary"].lower()


def test_a_gate_refused_before_it_ran_is_not_a_measured_clean_queue(
        tmp_path, monkeypatch):
    """A repository gate the executor REFUSED to run: its relay started (so the
    gate site folds an admission summary), but the gate command itself never
    ran, so no engine request reached the queue. The relay record is empty, and
    `admission_summary([])` names `no_engine_request`, which the old fold read as
    `state: "none"` — a measured clean queue.

    REAL PATH, NO GATE DICT: `run_tests` -> `_acquire_repo_gate` -> the REAL
    `_run_repo_gate` and REAL relay; only the executor's refusal of the gate
    command is injected, through the same `install_entry_executor(refuse=...)`
    seam the outcome table uses.
    """
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    install_entry_executor(monkeypatch,
                           refuse=lambda args: "run_tests.sh" in " ".join(args)
                           or list(args)[:1] == ["bash"])
    repo = _repo(tmp_path)
    out = tmp_path / "out"

    rt.run_tests(project_root=str(repo), out_dir=str(out), run_id="refused-gate")
    report = _report(out)

    gate = report["repo_gate"]
    assert gate["runner_error"] is True and gate["isolation_unavailable"] is True
    assert gate["admission"]["requests"] == []
    assert gate["measured"] == rt.REPO_GATE_UNMEASURED
    # No engine request ran, so nothing measured the render queue.
    assert gate["render_contention"]["state"] != "none", gate["render_contention"]
    assert report["render_contention"]["state"] != "none", report["render_contention"]
    assert "contention" not in report["summary"].lower()
    assert "render owner" not in report["summary"].lower()


# A gate that really EXECUTED, measured a red, and never asked the engine
# anything. It is the clean pole of the zero-request case: the queue was not
# touched because nothing needed it, so `none` is the honest reading. It is a
# real subprocess through the real executor entry. It retains its own report
# under GATE_REPORT_DIR, the same way `early_contract_red` does (a directory as
# the first entry, holding a manifest that names this repo and a measured
# failure), then exits 1, and it never contacts the relay.
_EXECUTED_RED_GATE = r"""
import json, os, sys, tempfile
from pathlib import Path
repo = Path.cwd()
parent = Path(os.environ["GATE_REPORT_DIR"])
d = Path(tempfile.mkdtemp(prefix="executed-red-", dir=parent))
manifest = {"repo": str(repo), "status": "failed",
            "outcome": "measured_fail", "exit_code": 1, "stages": {}}
(d / "manifest.json").write_text(json.dumps(manifest))
print("gate: executed and measured a red without any engine request", flush=True)
sys.exit(1)
"""


def test_a_gate_that_executed_and_never_touched_the_queue_stays_a_clean_none(
        tmp_path, monkeypatch):
    """WITNESS for the narrowed branch. The gate REALLY RAN (a real `run_tests.sh`
    subprocess exited 1, so the measured verdict is `measured_fail`) and made
    ZERO engine requests, so its admission record is `no_engine_request`.

    The broad branch named that run `unreported`, which claimed nothing measured
    its queue even though the run executed and simply never needed the queue.
    The narrowed branch must keep the honest clean `none` here, with `wait_sec`
    0.0 and no contention sentence. This pins the clean pole the broad branch
    broke.

    REAL PATH, NO GATE DICT: `run_tests` -> `_acquire_repo_gate` -> the REAL
    `_run_repo_gate` and REAL relay, with a real executed gate that never
    contacts the engine. Nothing is refused and nothing times out, so
    `runner_error` and `timed_out` are both unset.
    """
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    install_entry_executor(monkeypatch)
    repo = _repo(tmp_path)
    (repo / "gate.py").write_text(_EXECUTED_RED_GATE, encoding="utf-8")
    script = repo / "run_tests.sh"
    script.write_text("#!/bin/sh\nexec python3 gate.py\n", encoding="utf-8")
    script.chmod(0o755)
    out = tmp_path / "out"

    rt.run_tests(project_root=str(repo), out_dir=str(out), run_id="executed-red")
    report = _report(out)

    gate = report["repo_gate"]
    # It really executed: not refused, not killed, and it exited with a verdict.
    assert gate.get("runner_error") is not True and gate.get("timed_out") is not True
    assert gate["returncode"] == 1
    assert gate["measured"] == rt.REPO_GATE_MEASURED_FAIL
    # It made zero engine requests, which is the case the broad branch broke.
    assert gate["admission"]["requests"] == []
    assert gate["admission"]["state"] == "no_engine_request"
    # The honest clean pole: the queue was not touched, so `none`, not a
    # fabricated `unreported`, with no wait and no sentence.
    assert gate["render_contention"] == {
        "owners": [], "wait_sec": 0.0, "state": "none"}, gate["render_contention"]
    assert report["render_contention"] == {
        "owners": [], "wait_sec": 0.0, "state": "none"}, report["render_contention"]
    assert "render_contention_absent" not in report
    assert "contention" not in report["summary"].lower()
    assert "render owner" not in report["summary"].lower()
    # ...and it is still the measured red it was.
    assert report["passed"] is False


# ── the summary edge case: an empty summary is not repaired ────────────────

class _EmptyPytestRun:
    """A pytest session that printed nothing at all (a producer defect)."""
    returncode = 1
    stdout = ""
    stderr = ""
    import_error = ""
    git = None
    execution_identity = None
    storage = None


def test_a_waited_gate_with_no_summary_does_not_manufacture_one(
        tmp_path, monkeypatch):
    """`run_tests` recorded no summary, and the gate waited.

    The contention sentence is NOT a summary of a measurement, and
    `core/coding_impl_outcome.py` reads an absent/empty summary as a malformed
    report. Making the sentence the whole summary would have turned that
    malformed report into a well-formed one — a classification changed as a
    side effect of naming a wait. So the summary stays empty, the reason is
    named, and the contention is still in the block.
    """
    gate = {"script": "run_tests.sh", "returncode": 1, "passed": False,
            "output": "gate: script FAILED", "output_truncated": False,
            "admission": gate_admission.admission_summary(
                [_answered(0, waited_for=["owner-A"], wait=180.9249)])
                          | {"ticket": "rt-nosummary", "upstream": "http://x"},
            "render_contention": {"owners": ["owner-A"], "wait_sec": 180.925,
                                  "state": "waited"},
            "ticket": "rt-nosummary", "retained_findings": 1,
            "failure_cases": [{"case_id": "playtest/a", "detail": "red"}],
            "gate_coverage": None}
    gate["measured"] = rt._repo_gate_outcome(gate)
    assert gate["measured"] == rt.REPO_GATE_MEASURED_FAIL

    monkeypatch.setattr(rt, "_acquire_repo_gate", lambda *_a, **_k: gate)
    monkeypatch.setattr(rt.repository_executor, "execute",
                        lambda *_a, **_k: _EmptyPytestRun())
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out), run_id="waited-nosummary")
    report = json.loads((out / "test_report.json").read_text())

    # Nothing was measured into a summary, and nothing invents one now.
    assert report["stdout"] == "" and report["stderr"] == ""
    assert report["summary"] == ""
    # The contention is still named, in the structured block, with the reason
    # the summary does not carry it.
    assert report["render_contention"] == {"owners": ["owner-A"],
                                           "wait_sec": 180.925, "state": "waited"}
    assert "no summary" in report["render_contention_summary_absent"]
    # The report is still the red this gate measured, unchanged by the above.
    assert report["passed"] is False and report["failures"]


# ── the retry boundary: only the FINAL attempt's reading is kept ───────────

def test_retry_first_attempt_waited_final_attempt_did_not_reports_the_final_attempts_reading(
        monkeypatch):
    """`_acquire_repo_gate` re-acquires an unmeasured gate and keeps ONLY the
    final attempt's result, so a wait spent in an earlier attempt is dropped
    and the block reports the final attempt's reading — it is not summed, and
    nothing here pretends the earlier records are still reachable.

    The re-acquisition budget is 1 in production (`REPO_GATE_UNMEASURED_ATTEMPTS`,
    a cost bound, not a tuning knob), so this raises it to 2 to reach the path.
    """
    monkeypatch.setattr(rt, "REPO_GATE_UNMEASURED_ATTEMPTS", 2)
    waited = {"script": "run_tests.sh", "returncode": 2, "passed": False,
              "output": "gate NOT run", "output_truncated": False,
              "admission": {"state": gate_admission.NOT_ADMITTED},
              "render_contention": {"owners": ["owner-A"], "wait_sec": 12.5,
                                    "state": "waited"},
              "measured": rt.REPO_GATE_UNMEASURED}
    final = {"script": "run_tests.sh", "returncode": 0, "passed": True,
             "output": "gate: all green", "output_truncated": False,
             "admission": {"state": gate_admission.ANSWERED},
             "render_contention": {"owners": [], "wait_sec": 0.0, "state": "none"},
             "measured": rt.REPO_GATE_MEASURED_PASS}
    attempts = iter([waited, final])

    gate = rt._acquire_repo_gate(Path("/nonexistent"), run_gate=lambda _repo: next(attempts),
                                 sleep=lambda _seconds: None, run_id="retry")

    assert gate is final
    assert gate["attempts"] == 2
    assert gate["measured"] == rt.REPO_GATE_MEASURED_PASS
    # The FIRST attempt's 12.5s wait is gone with its result, by design.
    assert gate["render_contention"] == {"owners": [], "wait_sec": 0.0,
                                         "state": "none"}
    assert "12.5" not in json.dumps(gate)
