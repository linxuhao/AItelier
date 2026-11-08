"""A repository gate reaches the engine through the render queue, and a gate
the engine did not admit is a third outcome, not a failing test.

Every test serves the REAL harness admission code (`tests/gate_fixture.py`):
its durable render-owner table, its queue and its 409 payloads. The repository
gate is a script with the game gate's transport and exit-code contract, run by
the REAL `run_tests` through the admission relay.

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
from pathlib import Path

import pytest

from aitelier import gate_admission
from aitelier.gate_evidence import release_disposition, report_state
from aitelier.tools.run_tests import impl as rt
from tests.gate_fixture import HOLDER, HarnessRig, red_report, write_gate



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
# The records below carry the relay's REAL field names and the harness's REAL
# answer fields (`render_owner_waited_for_owner_ids`, `render_owner_wait_sec`,
# `outcome`; see `docker/godot/godot_harness.py` and `gate_admission._ANSWER_FIELDS`).
# They run without docker or the render lock: the builder is a fake, and the
# contention is read from the same records the relay writes.

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
    """Requirement (b) end-to-end: a gate that waited behind nobody puts the
    clean block on the gate result, and the report's summary never mentions
    contention. The gate result is the real `_run_repo_gate` shape for an
    answered green run."""
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

    Built on the REAL `run_tests` fold. The gate result is what
    `_run_repo_gate` returns for an answered red after a contended wait: the
    admission summary comes from the real relay records, and only the gate's
    own exit code and retained findings are supplied.
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
