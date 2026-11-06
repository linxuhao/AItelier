"""Early authored-contract reds through a real repository-gate subprocess.

The fixture is original generic YAML validation, not private game source.
It retains the producer protocol before any stage or engine request exists.

The second half drives the TWO producers of `provisional_round_feedback` —
`round_feedback.run_feedback` and this tool's own provisional coverage stamp —
through their real entry points, because the defect being repaired lived in the
gap between them: one purpose string, two meanings, one of them a red.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from aitelier import gate_evidence as ge
from aitelier import round_feedback as rf
from aitelier.gate_coverage import (
    PRODUCER_ROUND_FEEDBACK, PRODUCER_RUN_TESTS, PROVISIONAL_PRODUCER,
    PROVISIONAL_PURPOSE, full_coverage, provisional_producer,
    publication_scope_refusal)
from aitelier.tools.run_tests import impl as rt
from tests.gate_fixture import embed_env, install_entry_executor


GATE = r"""
import json, os, sys, tempfile
from pathlib import Path
import yaml
repo = Path.cwd()
mode = os.environ["EARLY_GATE_MODE"]
parent = Path(os.environ["GATE_REPORT_DIR"])
if mode == "unknown":
    (parent / "first-file").write_text("not the report directory")
d = Path(tempfile.mkdtemp(prefix="contract-", dir=parent))
manifest = {"repo": str(repo), "status": "incomplete", "stages": {}}
if mode.startswith("green") or mode == "foreign":
    manifest.update(status="passed", outcome="measured_pass", exit_code=0)
    coverage = json.loads(os.environ["EARLY_GATE_COVERAGE"])
    if coverage is not None:
        manifest["gate_coverage"] = coverage
    if mode == "green-provisional":
        manifest["purpose"] = "provisional_round_feedback"
    (d / "manifest.json").write_text(json.dumps(manifest))
    if mode == "foreign":
        other = Path(tempfile.mkdtemp(prefix="foreign-", dir=parent))
        (other / "manifest.json").write_text(json.dumps({
            "repo": "/a/different/repository", "status": "failed",
            "outcome": "measured_fail", "exit_code": 1, "stages": {}}))
    sys.exit(0)
try:
    files = sorted((repo / "scenarios").glob("*.yaml"))
    if not files:
        raise ValueError("authored scenario set is empty")
    for file in files:
        yaml.safe_load(file.read_text())
except (ValueError, yaml.YAMLError) as error:
    print("CONTRACT ERROR:", error, flush=True)
    manifest.update(status="failed", outcome="measured_fail", exit_code=1)
    if mode == "declared":
        manifest.update(status="incomplete", outcome="unmeasured", exit_code=2)
        print("AITELIER_REPO_GATE_UNMEASURED=" + json.dumps({"state": "blocked", "reason": "fixture absence"}))
    (d / "manifest.json").write_text(json.dumps(manifest))
    sys.exit(manifest["exit_code"])
"""


def run_gate(tmp_path, monkeypatch, mode, coverage=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    scenarios = repo / "scenarios"
    scenarios.mkdir()
    if mode == "malformed":
        (scenarios / "bad.yaml").write_text("scenario: [\n")
    # The real executor entry scrubs the child environment: the gate program
    # carries its two controlled settings itself.
    (repo / "gate.py").write_text(embed_env(
        GATE, EARLY_GATE_MODE=mode, EARLY_GATE_COVERAGE=json.dumps(coverage)))
    script = repo / "run_tests.sh"
    script.write_text(f"#!/bin/sh\nexec {sys.executable} gate.py\n")
    script.chmod(0o755)
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "data"))
    # Real subprocess through the real executor entry; no canned verdict.
    calls = install_entry_executor(monkeypatch)
    result = rt._run_repo_gate(repo)
    assert [c["args"][:1] for c in calls] == [["bash"]], calls
    assert calls[0]["report_dir"] and calls[0]["relay_socket"], calls
    print(json.dumps(result, sort_keys=True))
    assert result["admission"]["requests"] == []
    assert result["admission"]["state"] == "no_engine_request"
    return result


@pytest.mark.parametrize("mode,original", [
    ("malformed", "while parsing a flow node"),
    ("empty", "authored scenario set is empty"),
])
def test_own_early_contract_failure_survives_missing_coverage(tmp_path, monkeypatch,
                                                          mode, original):
    gate = run_gate(tmp_path, monkeypatch, mode)
    assert gate["returncode"] == 1
    assert gate["unmeasured_declaration"] is None
    assert original in gate["output"]
    assert gate["report_attribution"]["state"] == "own"
    assert gate["gate_coverage_error"] == "coverage marker is missing or invalid"
    assert gate["measured"] == "measured_fail", gate
    assert gate["retained_findings"] == 1
    cases, error = rt._repo_gate_failure_cases(gate)
    assert error is None
    assert len(cases) == 1
    assert cases[0]["case_id"].startswith("manifest/")
    manifest = Path(gate["report_dir"]) / gate["report_attribution"]["first_entry"] / "manifest.json"
    assert json.loads(manifest.read_text())["stages"] == {}
    # Fold the actual acquired gate into the real writer without rerunning it.
    Path(gate["repo"], "gate.py").unlink()
    monkeypatch.setattr(rt, "_acquire_repo_gate", lambda _repo, *_a, **_k: gate)
    out = tmp_path / "out"
    result = rt.run_tests(project_root=gate["repo"], out_dir=str(out))
    report = json.loads((out / "test_report.json").read_text())
    assert report["repo_gate"]["measured"] == "measured_fail"
    assert result["repo_gate_absent"] is False
    assert not report.get("repo_gate_absent")
    assert any(f.startswith("repo_gate:run_tests.sh#manifest/") for f in report["failures"])
    assert result["release_evidence"] != "full_test_passed"
    assert original in report["repo_gate"]["output"]


def test_early_red_with_unknown_report_is_unattributable(tmp_path, monkeypatch):
    gate = run_gate(tmp_path, monkeypatch, "unknown")
    assert gate["measured"] == "unattributable"
    assert gate["retained_findings"] is None
    assert any("manifest.json [manifest]" in red for red in gate["unattributed_reds"])


def test_provably_foreign_early_red_does_not_blame_own_gate(tmp_path, monkeypatch):
    coverage = full_coverage("a" * 40, {"scenarios": [{"name": "one"}]})
    gate = run_gate(tmp_path, monkeypatch, "foreign", coverage)
    assert gate["measured"] == "measured_pass"
    assert gate["retained_findings"] == 0
    assert "not_own" in gate["report_attribution"]["reports"].values()


def test_actual_declaration_without_red_stays_absent(tmp_path, monkeypatch):
    gate = run_gate(tmp_path, monkeypatch, "declared")
    assert gate["measured"] == "unmeasured"
    assert gate["retained_findings"] == 0
    assert gate["unmeasured_declaration"]["state"] == "blocked"


@pytest.mark.parametrize("mode,coverage", [
    ("green-missing", None),
    ("green-invalid", {"coverage": "full"}),
    ("green-provisional", full_coverage("a" * 40, {"scenarios": [{"name": "one"}]})),
    ("green-subset", {"coverage": "subset", "all_scenarios": ["one", "two"],
                       "selected_scenarios": ["one"], "unselected_scenarios": ["two"],
                       "fallback_full": False, "selection_basis": {
                           "head_sha": "a" * 40, "spec_sha256": "b" * 64,
                           "method": "fixture"}}),
])
def test_partial_green_cannot_be_full_evidence(tmp_path, monkeypatch, mode, coverage):
    gate = run_gate(tmp_path, monkeypatch, mode, coverage)
    assert gate["retained_findings"] == 0
    assert publication_scope_refusal(gate["gate_coverage"])
    if mode != "green-subset":
        assert gate["measured"] == "unmeasured"


# ── the two producers of `provisional_round_feedback` ───────────────────────
#
# One purpose string, two producers, two meanings (measured 2026-11-06 on
# 46f9ad8a2a):
#
#   * `round_feedback.run_feedback` — the report IS the feedback round. Its
#     `passed` is False BY CONSTRUCTION, and `tests/unit/test_round_feedback.py`
#     pins its verifier failures at `partial` (a shipped contract).
#   * `run_tests` (below) — the report IS a test report whose repository-gate
#     coverage was provisional. Its `passed` is the real pytest outcome, so a
#     red there was answered `partial` by the marker alone and never reached
#     the repair path.
#
# `gate_evidence.report_state` answered both on the string. These two probes are
# the ones that hold the difference: a red on the second producer's report is a
# red, everything else keeps the state it had, and the reports say which
# producer wrote them so no reader has to infer it from the shape.

PROVISIONAL_GATE = r"""
import json, os, sys, tempfile
from pathlib import Path
parent = Path(os.environ["GATE_REPORT_DIR"])
directory = Path(tempfile.mkdtemp(prefix="provisional-", dir=parent))
(directory / "manifest.json").write_text(json.dumps({
    "repo": str(Path.cwd()), "status": "passed", "outcome": "measured_pass",
    "exit_code": 0, "stages": {},
    "purpose": "provisional_round_feedback",
    "gate_coverage": json.loads(os.environ["PROVISIONAL_GATE_COVERAGE"]),
}))
sys.exit(0)
"""


def _provisional_coverage():
    coverage = full_coverage("a" * 40, {"scenarios": [{"name": "one"}]})
    coverage["purpose"] = PROVISIONAL_PURPOSE
    return coverage


def _provisional_round(tmp_path, monkeypatch, *, pytest_red, gate_measured_pass=False,
                       node_project=False):
    """`run_tests` over a repository whose gate coverage was PROVISIONAL.

    The pytest leg is real: `install_entry_executor` runs the same subprocess
    the shipped transport runs, so `passed` is a measured outcome rather than a
    fixture's opinion, and the evidence stamp is the real one (`release_evidence`
    is the value `configs/addons/game_harness.yaml` routes on).

    `gate_measured_pass` stubs the acquisition because the shipped path CANNOT
    produce a green report under this marker: a provisional coverage is refused
    by `_retained_coverage`, which makes the gate `unmeasured` and the report an
    absence. The stub isolates the one shape that otherwise stays untested — a
    MEASURED green carrying the marker — so the red half has a control that
    differs from it in nothing but the pytest outcome.

    `node_project` adds a `package.json`, which makes the REAL node gate run and
    — through the same test seam — be refused an executor, exactly as an
    unavailable Node leg does in a deployment. That is the shape where the whole
    report is marked `skipped` / `infrastructure_unavailable` while the pytest
    failure stays in `failures`.
    """
    suffix = ("red-node" if node_project else "red") if pytest_red else (
        "measured-green" if gate_measured_pass else "green")
    repo = tmp_path / ("repo-" + suffix)
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_contract.py").write_text(
        "def test_contract():\n"
        + ("    assert 1 + 1 == 3\n" if pytest_red else "    assert 1 + 1 == 2\n"))
    if node_project:
        (repo / "package.json").write_text(
            json.dumps({"name": "web", "version": "1.0.0",
                        "scripts": {"build": "echo build", "test": "echo test"}}))
    coverage = _provisional_coverage()
    if gate_measured_pass:
        monkeypatch.setattr(rt, "_acquire_repo_gate", lambda _repo, **_kwargs: {
            "script": "run_tests.sh", "returncode": 0, "passed": True,
            "output": "provisional green", "measured": rt.REPO_GATE_MEASURED_PASS,
            "gate_coverage": coverage})
    else:
        (repo / "gate.py").write_text(embed_env(
            PROVISIONAL_GATE, PROVISIONAL_GATE_COVERAGE=json.dumps(coverage)))
        script = repo / "run_tests.sh"
        script.write_text(f"#!/bin/sh\nexec {sys.executable} gate.py\n")
        script.chmod(0o755)
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "data"))
    install_entry_executor(monkeypatch)
    out = tmp_path / ("graph-" + suffix) / "test"
    result = rt.run_tests(project_root=str(repo), out_dir=str(out),
                          run_id="provisional-round", evidence_cycle_start=True)
    report = json.loads((out / "test_report.json").read_text())
    print(json.dumps({"passed": report["passed"],
                      "purpose": report.get("purpose"),
                      "producer": report.get(PROVISIONAL_PRODUCER),
                      "evidence_state": report.get("evidence_state"),
                      "release_evidence": report.get("release_evidence"),
                      "repo_gate_absent": report.get("repo_gate_absent"),
                      "repo_gate_unmeasured": report.get("repo_gate_unmeasured"),
                      "returncode": report.get("returncode"),
                      "skipped": report.get("skipped"),
                      "infrastructure_unavailable": report.get("infrastructure_unavailable"),
                      "failures": report["failures"]}, sort_keys=True))
    return result, report, out


def test_a_red_in_a_provisional_measurement_round_is_not_softened(tmp_path, monkeypatch):
    """A measured pytest red under the provisional stamp reaches the repair path.

    Measured on the real consumer: the report's own `release_evidence` (what the
    shipped configs route on) and `report_state` read through
    `release_disposition`. Two controls sit beside it — a measured GREEN leg
    whose report is a gate absence, and a measured GREEN report carrying the same
    marker — and both keep the answer they had, so this is not "passed: false now
    means failed": the absence is still not charged, and the marker still never
    reads as a pass.
    """
    red_result, red, red_out = _provisional_round(
        tmp_path, monkeypatch, pytest_red=True)
    green_result, green, green_out = _provisional_round(
        tmp_path, monkeypatch, pytest_red=False)
    measured_green_result, measured_green, _mg_out = _provisional_round(
        tmp_path, monkeypatch, pytest_red=False, gate_measured_pass=True)

    # The red half: the real gate measured provisionally, the real pytest leg
    # failed, and that failure is in the report as a measured failure.
    assert red["purpose"] == PROVISIONAL_PURPOSE
    assert red[PROVISIONAL_PRODUCER] == PRODUCER_RUN_TESTS
    assert red["returncode"] == 1 and red["passed"] is False
    assert any(f.startswith("FAILED") for f in red["failures"]), red["failures"]
    assert "repo_gate_absent" in red and red["repo_gate_absent"] is False

    # …and it is not softened. These three are the same fact seen by the three
    # consumers that matter: the state, the release value, and the value the
    # real stamp wrote into the file for the configs to route on.
    assert ge.report_state(red) == "failed"
    assert ge.release_disposition(red) == "known_failure"
    assert red["release_evidence"] == "known_failure" == red_result["release_evidence"]
    assert red_result["release_evidence"] == "known_failure" and red_result["passed"] is False

    # The audit fails the run and files it as a FAILURE, not as a coverage gap:
    # a measured red is the implementer's, and `failed_gates` is where the
    # audit puts a red it read as a red.
    audit = ge.audit_evidence(red_out.parent, "provisional-round",
                              [["test", "test_report.json"]])
    assert audit["passed"] is False
    entry = audit["failed_gates"][0]
    assert entry["state"] == "failed" and entry["passed"] is False
    assert entry["purpose"] == PROVISIONAL_PURPOSE
    assert entry[PROVISIONAL_PRODUCER] == PRODUCER_RUN_TESTS
    assert audit["incomplete_coverage_gates"] == []

    # Control 1 — a measured GREEN pytest leg beside a gate that measured
    # nothing. The report is an absence (`passed: false`, `repo_gate_absent`),
    # and it keeps the answer it had: not a red, not a pass.
    assert green["returncode"] == 0
    assert green["passed"] is False and green["repo_gate_absent"] is True
    assert green["repo_gate_unmeasured"] is True
    assert green["evidence_state"] == "partial" and red["evidence_state"] == "failed"
    assert ge.report_state(green) == "partial"
    assert ge.release_disposition(green) == "unresolved"
    assert green["release_evidence"] == "unresolved" == green_result["release_evidence"]
    green_audit = ge.audit_evidence(green_out.parent, "provisional-round",
                                    [["test", "test_report.json"]])
    assert green_audit["passed"] is False
    assert green_audit["incomplete_coverage_gates"][0]["state"] == "partial"
    assert green_audit["failed_gates"] == []

    # Control 2 — the measured GREEN report that DOES carry the marker with
    # nothing absent beside it. Also unchanged: `partial`, and the coverage
    # refusal still stops it from authorizing a publication.
    assert measured_green["passed"] is True
    assert measured_green["purpose"] == PROVISIONAL_PURPOSE
    assert measured_green[PROVISIONAL_PRODUCER] == PRODUCER_RUN_TESTS
    assert publication_scope_refusal(measured_green["gate_coverage"])
    assert ge.report_state(measured_green) == "partial"
    assert ge.release_disposition(measured_green) == "unresolved"
    assert measured_green["release_evidence"] == "unresolved"
    assert measured_green_result["release_evidence"] == "unresolved"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _feedback_report(tmp_path):
    """The REAL `run_feedback` report for one clean HEAD, transport unreached."""
    repo = tmp_path / "feedback-repo"
    repo.mkdir()
    (repo / "a.gd").write_text("extends Node\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "add", "a.gd")
    _git(repo, "commit", "-qm", "head")
    head = _git(repo, "rev-parse", "HEAD")
    spec = {"scenarios": [{"name": "a", "scene": "res://a.tscn"}]}
    scope = full_coverage(head, spec)
    scope["purpose"] = PROVISIONAL_PURPOSE
    return rf.run_feedback(repo, tmp_path / "feedback-out", scope, spec, {},
                           lambda _: pytest.fail("this fixture has no transport"))


def test_the_two_provisional_producers_are_distinguishable(tmp_path, monkeypatch):
    """Both real producers stamp ONE purpose, and each report says who did it.

    Not by guessing from the shape: `provisional_producer` reads a field the
    producer wrote, and the classifier's answer turns on that field — remove it
    and the run_tests report falls back to the shipped feedback reading.
    """
    feedback = _feedback_report(tmp_path)
    _result, stamped, _out = _provisional_round(
        tmp_path, monkeypatch, pytest_red=False, gate_measured_pass=True)

    # Same marker — this is the defect, stated as a fact about the reports.
    assert feedback["purpose"] == stamped["purpose"] == PROVISIONAL_PURPOSE
    # Different producer, read off the report itself.
    assert provisional_producer(feedback) == PRODUCER_ROUND_FEEDBACK
    assert provisional_producer(stamped) == PRODUCER_RUN_TESTS
    assert feedback[PROVISIONAL_PRODUCER] != stamped[PROVISIONAL_PRODUCER]

    # The feedback round is a feedback round: `passed` False by construction,
    # and `partial` is its shipped answer (unchanged by this change).
    assert feedback["passed"] is False
    assert ge.report_state(feedback) == "partial"
    assert ge.release_disposition(feedback) == "unresolved"

    # The test report is a test report: the same `partial` here comes from a
    # green measurement, and it is the same value for a different reason.
    assert stamped["passed"] is True
    assert ge.report_state(stamped) == "partial"

    # …and the field is what carries the difference. Stripped of it, the SAME
    # report is read as the feedback producer and its red would be softened
    # again — the fail-closed default, and the reason the field has to exist.
    unnamed = {k: v for k, v in stamped.items() if k != PROVISIONAL_PRODUCER}
    assert provisional_producer(unnamed) == ""
    assert ge.report_state(unnamed) == "partial"
    red = dict(unnamed, passed=False, failures=["FAILED tests/test_x.py::test_x"],
               returncode=1)
    assert ge.report_state(red) == "partial"
    assert ge.release_disposition(red) == "unresolved"
    # With the producer named, the same red is the red it is.
    assert ge.report_state(dict(red, **{PROVISIONAL_PRODUCER: PRODUCER_RUN_TESTS})) == "failed"
    assert ge.release_disposition(
        dict(red, **{PROVISIONAL_PRODUCER: PRODUCER_RUN_TESTS})) == "known_failure"



# ── the two holes the review of b765f570 found ─────────────────────────────
#
# Both came from the predicate asking "is any ABSENCE present?" instead of "was
# a RED measured?", and both are answered here on the producer, not on a shape
# invented to suit the classifier.
#
#   1. An absence became a FAILURE once the report had been stamped: the
#      predicate read `evidence_state`, `stamp_report` WRITES `evidence_state`,
#      so an unattributable ticket was charged to the implementer after the
#      stamp renamed it.
#   2. A measured RED stayed softened when another leg was unavailable:
#      `run_tests` marks the whole report `skipped` /
#      `infrastructure_unavailable` when the Node gate could not be measured,
#      which made the vocabulary answer "absence" over a pytest session that
#      really failed.
#
# The evidence is the producer's own `returncode: 1` — a fact about ITS pytest
# leg that survives every later branch and the stamp — so neither hole can come
# back without reddening a probe below.


def _stamp_and_audit(tmp_path, name, report):
    """The real `stamp_report`, then the real `audit_evidence`, on a temp graph."""
    graph = tmp_path / f"graph-{name}"
    step = graph / "test_evidence"
    step.mkdir(parents=True)
    report = dict(report)
    ge.stamp_report(report, run_id="provisional-round", out_dir=str(step),
                    start_cycle=True)
    (step / "test_report.json").write_text(json.dumps(report))
    audit = ge.audit_evidence(graph, "provisional-round",
                              [["test_evidence", "test_report.json"]])
    return report, audit


def test_a_pytest_red_survives_an_unavailable_node_leg(tmp_path, monkeypatch):
    """HOLE 2 — the report's own pytest failure is not softened by a dead leg.

    Driven through the real producer: a repository with a `package.json` whose
    Node gate is refused an executor (the same test seam refuses every node
    profile), beside a pytest leg that really fails. `run_tests` reports exactly
    that shape — `passed: false`, `skipped: true`,
    `infrastructure_unavailable: true` — and keeps the pytest failure and its
    own `returncode`. The old predicate read the absence vocabulary, so this
    report answered `partial` over a red that had been measured.
    """
    result, report, out = _provisional_round(
        tmp_path, monkeypatch, pytest_red=True, node_project=True)

    # The producer's own facts, all of them written by `run_tests`:
    assert report["returncode"] == 1, report.get("returncode")
    assert any(f.startswith("FAILED") for f in report["failures"]), report["failures"]
    assert report["passed"] is False
    assert report["infrastructure_unavailable"] is True and report["skipped"] is True
    # The Node gate could not be measured at all: the refused install is retained
    # as the reason, and the project itself is an ABSENCE (`skipped`, not failed).
    assert report["node"]["skipped"] is True and report["node"]["passed"] is True
    assert report["node"]["checks"]["install"]["isolation_unavailable"] is True
    # …and the vocabulary the old predicate consulted still says "absence",
    # which is precisely why it answered `partial` here.
    assert ge._absence_state(report, False) == "infrastructure_unavailable"

    assert ge.report_state(report) == "failed"
    assert ge.release_disposition(report) == "known_failure"
    assert report["release_evidence"] == "known_failure" == result["release_evidence"]

    audit = ge.audit_evidence(out.parent, "provisional-round",
                              [["test", "test_report.json"]])
    assert audit["passed"] is False
    assert audit["failed_gates"][0]["state"] == "failed"
    assert audit["incomplete_coverage_gates"] == [] and audit["skipped_gates"] == []


# Each of these reports has an ABSENCE as its only defect: the pytest leg
# measured nothing red, and the producer said so in its own words. `returncode`
# is what the producer wrote for that kind of run — absent where pytest never
# ran, `None` where the harness killed it, and `1` only for the storage case,
# where the producer ALSO says the results may not be trusted either way.
ABSENCE_SHAPES = {
    "unattributable": {
        "repo_gate_unattributable": True, "evidence_state": "unattributable",
        "failures": ["repo_gate: the report under the ticket is not provably "
                     "the gate's"]},
    "not_run": {
        "evidence_state": "not_run", "repo_gate_absent": True,
        "repo_gate_unmeasured": True,
        "failures": ["repo_gate:run_tests.sh was NOT measured"]},
    "timeout": {
        "skipped": True, "skipped_because": "pytest_timeout",
        "measurement_not_taken": True, "returncode": None,
        "failures": ["pytest:timed out after 3600s - no results collected"]},
    "storage_during_run": {
        "skipped": True, "skipped_because": "storage_exhausted",
        "storage_unmeasured": True, "infrastructure_unavailable": True,
        "evidence_state": "infrastructure_unavailable", "returncode": 1,
        "failures": ["pytest:storage exhausted - no trustworthy measurement"]},
    "pending": {"pending": True},
    "blind": {"blind": True},
    "gate_absent_green": {"repo_gate_absent": True, "evidence_state": "not_run",
                          "returncode": 0},
}


@pytest.mark.parametrize("shape", sorted(ABSENCE_SHAPES))
def test_an_absence_under_the_marker_stays_partial_through_the_stamp(tmp_path, shape):
    """HOLE 1 — an absence is never charged as a failure, stamped or not.

    Asserted on the RAW report and then through the real `stamp_report` +
    `audit_evidence` round trip, because that round trip is where the hole was:
    the stamp overwrites `evidence_state` with `report_state`'s own answer, so a
    predicate that re-read the state vocabulary of the FILE lost the producer's
    `unattributable` / `not_run` / `infrastructure_unavailable` and called the
    report a measured failure — which the audit then filed under
    `failed_gates`, i.e. charged to the implementer.
    """
    report = dict(ABSENCE_SHAPES[shape], purpose=PROVISIONAL_PURPOSE,
                  **{PROVISIONAL_PRODUCER: PRODUCER_RUN_TESTS},
                  passed=False, gate_coverage=_provisional_coverage())

    # The RAW report: an absence parks, and it never reads as a red.
    assert ge.report_state(report) == "partial"
    assert ge.release_disposition(report) == "unresolved"

    # The SAME report after the real stamp and through the real audit.
    stamped, audit = _stamp_and_audit(tmp_path, shape, report)
    assert stamped["evidence_state"] == "partial"
    assert stamped["release_evidence"] == "unresolved"
    assert ge.report_state(stamped) == "partial"
    assert ge.release_disposition(stamped) == "unresolved"
    assert audit["passed"] is False
    assert [entry["state"] for entry in audit["incomplete_coverage_gates"]] == ["partial"]
    assert audit["failed_gates"] == []
