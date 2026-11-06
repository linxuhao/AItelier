# tests/unit/test_absence_survives_the_stamp.py
"""An absence the producer wrote survives the stamp. A measured red still reddens.

THE DEFECT THIS FILE IS ABOUT (`iss-b161216de6a14a81`, measured on 46f9ad8a2a
and on 36ab487f0c — pre-existing in both, so it is not the marker's change):

`run_tests` finishes with a GREEN pytest leg and a repository gate it could not
measure, and says so in its own words: `passed: false`, `repo_gate_absent: true`,
`repo_gate_unmeasured: true`, `evidence_state: "not_run"`.

    raw report          report_state `skipped`,  release_disposition `unresolved`
    after stamp_report  stored evidence_state `skipped`, stored release_evidence
                        `known_failure`
    re-read the file    report_state `failed`,   release_disposition `known_failure`

Two defects in one order-dependent write. `stamp_report` wrote `report_state`'s
answer back over `evidence_state`, and `skipped` is NOT one of this module's
absence statuses, so the annotation destroyed the absence it was derived from:
every later reader of the file (the audit, `release_disposition`, anything that
greps it) saw a measured failure. A failure RELEASES the run past the hold, and
imposing that hold is the entire reason this module exists. Writing the state
before asking for the disposition also made the disposition read the stamp's
own annotation, so the value a pipeline routes on (the return) and the value an
audit computes from the file could answer differently for the SAME report.

WHAT THE FIX IS, AND WHAT IT IS NOT. The stamp keeps the producer's own state
whenever the label it would write is not one that reads back as itself
(`gate_evidence._write_state`), so the absence vocabulary is fixed as a CLASS,
not word by word and not consumer by consumer: no consumer had to learn a new
key, and no stored answer moved for any shape whose label was already
self-describing. The measured-failure and green shapes answer exactly what they
answered before — pinned here as controls, because "an absence no longer reads
as a failure" is only true if a failure still does.
"""
import json

import pytest

from aitelier import gate_evidence as ge
from aitelier.gate_coverage import (
    PRODUCER_ROUND_FEEDBACK, PRODUCER_RUN_TESTS, PROVISIONAL_PRODUCER,
    PROVISIONAL_PURPOSE)

RUN = "run-absence-stamp"


# ── the shapes, taken from the module's own table ──────────────────────────
#
# Every entry is a shape one of this repository's real producers writes and
# `report_state` has a branch for; the table is here so that a new branch, or a
# branch that starts disagreeing with the file it just wrote, reddens this file
# instead of only the shape that happened to be thought of.
ABSENCE = {
    # `run_tests`: green pytest leg, repository gate NOT measured.
    "passed": False,
    "returncode": 0,
    "repo_gate_absent": True,
    "repo_gate_unmeasured": True,
    "evidence_state": "not_run",
    "repo_gate": {"script": "run_tests.sh", "measured": "unmeasured",
                  "returncode": 3, "attempts": 1,
                  "output": "gate declared it has no verdict"},
    "failures": ["repo_gate:run_tests.sh was NOT measured (1 attempt(s))"],
    "summary": "412 passed; the repository gate was NOT measured",
}
MEASURED_RED = {
    "passed": False,
    "returncode": 1,
    "failures": ["FAILED tests/test_x.py::test_x - AssertionError"],
    "summary": "1 failed, 411 passed",
}
GREEN = {"passed": True, "returncode": 0, "failures": [],
         "summary": "412 passed"}

SHAPES = {
    # ── absences: every one of these parks, before the stamp and after it ──
    "absence_gate_unmeasured": ABSENCE,
    "absence_gate_absent_green_leg": {
        "passed": False, "returncode": 0, "repo_gate_absent": True,
        "evidence_state": "not_run",
        "failures": ["repo_gate: no script was found"]},
    "absence_status_word": {"passed": False, "status": "not_run",
                            "failures": ["no measurement was taken"]},
    "absence_pending": {"passed": False, "pending": True},
    "absence_infrastructure": {"passed": False, "returncode": None,
                               "infrastructure_unavailable": True,
                               "evidence_state": "infrastructure_unavailable"},
    "absence_blind": {"passed": False, "returncode": None, "blind": True},
    "absence_node_partial": {
        "passed": True, "failures": [],
        "node_unmeasured": [{"dir": "packages/p7", "kind": "capped",
                             "reason": "not visited: more than 8 node projects"}]},
    "absence_unrun_flag": {"passed": False, "executed": False},
    "absence_skipped_marker": {"passed": False, "gate_skipped": True,
                               "skipped_because": "pytest_timeout"},
    "absence_unattributable": {
        "passed": False, "returncode": 0, "repo_gate_unattributable": True,
        "evidence_state": "unattributable",
        "failures": ["repo_gate: the report under the ticket is not provably "
                     "the gate's"]},
    # ── the marker: partial, and a measured red under it is still a red ────
    "provisional_partial": {
        "passed": False, "returncode": None, "purpose": PROVISIONAL_PURPOSE,
        **{PROVISIONAL_PRODUCER: PRODUCER_ROUND_FEEDBACK},
        "gate_coverage": {"purpose": PROVISIONAL_PURPOSE, "coverage": "subset"}},
    "provisional_measured_red": dict(
        MEASURED_RED, purpose=PROVISIONAL_PURPOSE,
        **{PROVISIONAL_PRODUCER: PRODUCER_RUN_TESTS}),
    # ── measured shapes: unchanged, and pinned as the controls ─────────────
    "measured_red": MEASURED_RED,
    "measured_known_failure": dict(MEASURED_RED, passed_relative=True,
                                   baseline_state="compared"),
    "measured_unattributed": dict(MEASURED_RED, passed_relative=True),
    "measured_green": GREEN,
    "measured_green_provisional_refused": {
        "passed": True, "returncode": 0, "purpose": PROVISIONAL_PURPOSE,
        **{PROVISIONAL_PRODUCER: PRODUCER_RUN_TESTS},
        "gate_coverage": {"purpose": PROVISIONAL_PURPOSE, "coverage": "subset"}},
    "unreadable_passed": {"passed": "false"},
}


def _stamp(tmp_path, name, shape):
    """The RAW answers, the real `stamp_report`, and the file it wrote.

    The raw answers are taken from a COPY before the stamp runs: `stamp_report`
    mutates the report it is handed, which is exactly the property under test.
    """
    step = tmp_path / f"graph-{name}" / "5_test"
    step.mkdir(parents=True)
    raw_state = ge.report_state(dict(shape))
    raw_disposition = ge.release_disposition(dict(shape))
    returned = ge.stamp_report(dict(shape), run_id=RUN, out_dir=str(step),
                               start_cycle=True)
    path = step / "test_report.json"
    path.write_text(json.dumps(returned, indent=2), encoding="utf-8")
    read_back = json.loads(path.read_text(encoding="utf-8"))
    return raw_state, raw_disposition, returned, read_back, step.parent


# ── criterion 1: the absence parks, and the red beside it does not ─────────

def test_an_absence_and_the_measured_red_beside_it_keep_their_answers(tmp_path):
    """The one shape the defect was measured on, and its control, side by side.

    Absence half: the stamped file must answer what the raw report answered —
    `skipped` / `unresolved` — and the audit must not file it as a product
    failure. Red half: the SAME round trip over a report whose pytest leg
    really failed must still answer `failed` / `known_failure` and land in
    `failed_gates`, or this file would be trading one wrong answer for another.
    """
    absence_state, absence_disposition, absence, absence_file, absence_graph = _stamp(
        tmp_path, "absence", ABSENCE)
    assert (absence_state, absence_disposition) == ("skipped", "unresolved")

    # The stamp KEEPS the producer's word instead of renaming it away, writes
    # the disposition it computed from the evidence, and the file says both.
    assert absence["evidence_state"] == "not_run"
    assert absence["release_evidence"] == "unresolved"
    assert absence_file["evidence_state"] == "not_run"
    assert absence_file["release_evidence"] == "unresolved"
    assert ge.report_state(absence_file) == "skipped"
    assert ge.release_disposition(absence_file) == "unresolved"
    # …and nothing about the absence itself was dropped on the way through: the
    # report still says WHICH gate could not be measured.
    assert absence_file["repo_gate_absent"] is True
    assert absence_file["repo_gate_unmeasured"] is True

    audit = ge.audit_evidence(absence_graph, RUN,
                              [["5_test", "test_report.json"]])
    assert audit["passed"] is False, "an absence never passes the audit"
    assert audit["state"] == "skipped"
    assert [entry["state"] for entry in audit["skipped_gates"]] == ["skipped"]
    assert audit["failed_gates"] == [], "an absence may not charge the code"
    assert audit["known_failures"] == []

    # ── the control: the measured failure is still a measured failure ──────
    red_state, red_disposition, red, red_file, red_graph = _stamp(
        tmp_path, "red", MEASURED_RED)
    assert (red_state, red_disposition) == ("failed", "known_failure")

    assert red["release_evidence"] == "known_failure"
    assert ge.report_state(red_file) == "failed"
    assert ge.release_disposition(red_file) == "known_failure"
    assert red_file["release_evidence"] == "known_failure"

    red_audit = ge.audit_evidence(red_graph, RUN,
                                  [["5_test", "test_report.json"]])
    assert red_audit["passed"] is False
    assert [entry["state"] for entry in red_audit["failed_gates"]] == ["failed"]
    assert red_audit["skipped_gates"] == []


def test_stamp_file_writes_the_absence_the_audit_reads(tmp_path):
    """The path the chained tools use (`stamp_file`), not just the dict path.

    `godot_compile` and `godot_vision` write their report and then stamp the
    FILE, so a fix that only held when the caller kept the returned dict would
    leave those two gates exactly as broken as before.
    """
    step = tmp_path / "graph-stampfile" / "5_test"
    step.mkdir(parents=True)
    ge.stamp_report({}, run_id=RUN, out_dir=str(step), start_cycle=True)
    path = step / "test_report.json"
    path.write_text(json.dumps(dict(ABSENCE)), encoding="utf-8")

    ge.stamp_file(path, run_id=RUN, out_dir=str(step))

    read_back = json.loads(path.read_text(encoding="utf-8"))
    assert read_back["evidence_state"] == "not_run"
    assert read_back["release_evidence"] == "unresolved"
    assert ge.report_state(read_back) == "skipped"
    assert ge.release_disposition(read_back) == "unresolved"
    audit = ge.audit_evidence(step.parent, RUN,
                              [["5_test", "test_report.json"]])
    assert audit["failed_gates"] == []
    assert [entry["state"] for entry in audit["skipped_gates"]] == ["skipped"]


def test_a_label_that_reads_back_as_itself_is_still_written(tmp_path):
    """The rule's other half, so "keep the producer's word" cannot drift into
    "stop annotating absences at all".

    A report that carries the absence in a FIELD the reader consults
    (`executed: false`) needs no rescue: writing `skipped` over it reads back as
    `skipped`, so the stamp still normalises the label exactly as it did before —
    and only the shape whose absence lived in the state word keeps its own.
    Removing the write for every absence would move stored answers this node
    must not move, for reports whose only record of the absence is that field.
    """
    _state, _disposition, returned, read_back, _graph = _stamp(
        tmp_path, "flags", SHAPES["absence_unrun_flag"])

    assert returned["evidence_state"] == "skipped"
    assert read_back["evidence_state"] == "skipped"
    assert ge.report_state(read_back) == "skipped"
    assert ge.release_disposition(read_back) == "unresolved"
    # …and the producer's own fact is still there to be read.
    assert read_back["executed"] is False


# ── criterion 2: the returned answer IS the written answer, for every shape ─

@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_return_the_file_and_the_raw_report_answer_the_same(tmp_path, name):
    """One answer per report, whichever way a consumer reads it.

    `release_disposition` recomputed on the file must equal the value the tool
    RETURNED, because the pipeline routes on the return while the audit reads
    the file; and both must equal what the RAW report answered, because a stamp
    that changes the classification is not a stamp. The order inside
    `stamp_report` is what this catches: computing the disposition AFTER
    writing the state makes it read the annotation, and on the absence shapes
    that answers `known_failure` where the evidence answers `unresolved`.
    """
    raw_state, raw_disposition, returned, read_back, _graph = _stamp(
        tmp_path, name, SHAPES[name])

    assert ge.report_state(read_back) == raw_state
    assert ge.release_disposition(read_back) == raw_disposition
    assert returned["release_evidence"] == raw_disposition, (
        "the tool returned a different answer than the evidence gives")
    assert read_back["release_evidence"] == raw_disposition
    assert ge.report_state(returned) == raw_state


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_no_shape_is_handed_back_read_as_a_different_state(tmp_path, name):
    """The label the stamp LEAVES must still read as the state it reports.

    This is the whole rule stated as a property: after the stamp, reading the
    report's own state vocabulary gives the same answer the classifier gives.
    It holds either because the label written was self-describing, or because
    the stamp kept the producer's word instead of renaming it.
    """
    _raw_state, _raw_disposition, _returned, read_back, _graph = _stamp(
        tmp_path, name, SHAPES[name])

    state = ge.report_state(read_back)
    label = read_back.get("evidence_state")
    if label is not None and label != state:
        # Only reachable through the producer's own word for an absence; that
        # word must still BE an absence (the vocabulary the module recognises),
        # never something a reader would call a measured failure.
        assert ge._absence_state(dict(read_back, evidence_state=label), False), (
            f"{name!r}: the stamp kept {label!r} but it is not an absence word")


# ── the trap, so the guard is not loosened by accident ────────────────────

def test_a_derived_skipped_label_still_reads_as_a_measured_failure():
    """Why the stamp may not write its own `skipped` over the producer's word.

    `skipped` is a DERIVED label: it is not one of this module's absence
    statuses, so a report whose only record of the absence was that label reads
    `failed` / `known_failure` — i.e. it releases. This witness is the reason
    `_write_state` asks the fixpoint question instead of writing the label; if
    the vocabulary ever learns to recover `skipped`, this test says so and the
    guard can be reconsidered, which is the only honest way for a pin on a
    KNOWN-WRONG answer to stay in the suite.
    """
    demoted = dict(ABSENCE, evidence_state=ge.report_state(ABSENCE))
    assert demoted["evidence_state"] == "skipped"
    assert ge.report_state(demoted) == "failed"
    assert ge.release_disposition(demoted) == "known_failure"
    # …and the raw report, which the same label was derived from, parks.
    assert ge.release_disposition(ABSENCE) == "unresolved"
