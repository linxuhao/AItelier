# tests/unit/test_baseline_write_failure_keeps_absence.py
"""A baseline that could not be written never turns an absence into a failure.

THE DEFECT (measured 2026-11-06 by the non-author review of f6bf63e855 with an
injected ENOSPC; readable on main 9314c955cf before this file existed).

`run_tests._commit_baseline` writes the baseline decision AFTER the report was
classified, and its failure branch withdraws the claim that decision backed
(`baseline_state: unmeasured`, `passed_relative: False`). It then re-derived the
written answers from the report as it stood. On an absence the producer had
written `evidence_state: "not_run"`, so the first re-derivation overwrote that
word with `report_state`'s `skipped` -- which the absence vocabulary does not
read as an absence -- and the second derived `release_evidence` from the
degraded label:

    producer's report      report_state skipped    release_disposition unresolved
    after the failed write stored evidence_state   skipped
                           stored release_evidence known_failure
    re-read the file       report_state failed     release_disposition known_failure

A run that could not MEASURE was recorded as a measured FAILURE, and a failure
releases past the hold `gate_evidence` exists to impose.

THE FIX reuses the rule `gate_evidence.stamp_report` already follows
(`_write_state`: keep the producer's own word when replacing it would change
`report_state`), through `gate_evidence.restate_classification`, instead of a
second copy of it in the commit path.

THE WITNESS drives the REAL producer: a real `run_tests` over a repository whose
repo gate declares it did not measure, against a stored baseline that this run
must update, so a pending baseline write exists. The write fails at the one
writer `_commit_baseline` calls (`impl._write_replacing`). Controls, in this
same file: the same injected failure over a measured red still answers
failed/known_failure, and over a complete green still answers passed.
"""
import errno
import json
from pathlib import Path

import pytest

from aitelier import gate_evidence as ge
from aitelier.tools.run_tests import impl as rt
from core import gate_deferral
from tests.gate_fixture import install_entry_executor

# The gate's own declaration that it did not measure: a whole line that starts
# with the prefix and carries a JSON object whose `state` is a declared absence.
DECLARED = ('AITELIER_REPO_GATE_UNMEASURED='
            '{"state":"blocked","reason":"godot-builder unreachable: HTTP Error 409"}')
# A test that passes this run, recorded as known-red: the run proves it ran and
# did not fail, so the baseline must shrink, which is the write that is injected.
OLD = "tests/test_ok.py::test_ok"
GREEN_TEST = "def test_ok():\n    assert True\n"
RED_TEST = "def test_bad():\n    assert 1 == 2\n"


@pytest.fixture
def no_space(monkeypatch):
    """ENOSPC at the one writer `_commit_baseline` calls for the baseline."""
    def full(path, text):
        raise OSError(errno.ENOSPC, "No space left on device", str(path))
    monkeypatch.setattr(rt, "_write_replacing", full)


def _baseline_file(state, repo):
    return Path(rt._baseline_dir(str(state), Path(repo))) / rt.BASELINE_FILE


def _write_declared_gate(repo):
    script = repo / "run_tests.sh"
    script.write_text("#!/bin/sh\nprintf '%s\\n' " + repr(DECLARED) + "\nexit 3\n")
    script.chmod(0o755)


RUN = "run-baseline-write-failure"


def _run(repo, out, state, *, stamped=True):
    """A real `run_tests` call. `stamped` opens the evidence cycle the way the
    DPE pipeline does, so the report is stamped BEFORE the baseline is committed
    -- the order in which the failed write degrades an already-stamped absence."""
    kwargs = {"run_id": RUN, "evidence_cycle_start": True} if stamped else {}
    result = rt.run_tests(project_root=str(repo), out_dir=str(out),
                          state_dir=str(state), **kwargs)
    report = json.loads((Path(out) / "test_report.json").read_text(encoding="utf-8"))
    return result, report


def _readers(report):
    return ge.report_state(report), ge.release_disposition(report)


# ── the absence: the producer's word survives the failed write ─────────────

def test_an_absence_whose_baseline_cannot_be_written_stays_an_absence(
        tmp_path, monkeypatch, no_space):
    install_entry_executor(monkeypatch)
    monkeypatch.setenv("AITELIER_REPO_GATE_RETRY_DELAY_SECONDS", "0")
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text(GREEN_TEST)
    _write_declared_gate(repo)
    state = tmp_path / "state"
    baseline = _baseline_file(state, repo)
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"failures": [OLD], "updated_at": 0}))
    before = baseline.read_bytes()
    out = tmp_path / "out"

    result, report = _run(repo, out, state)

    # The injected failure was reached on the baseline path (not a missing run).
    assert report["baseline_write_errno"] == errno.ENOSPC
    assert result["baseline_persisted"] is False
    assert "could not write" in result["baseline_error"]
    assert "was NOT stored" in result["baseline_error"]
    assert baseline.read_bytes() == before, "a failed write leaves the old file intact"
    # The bookkeeping the failure withdraws is still withdrawn.
    assert report["baseline_state"] == "unmeasured"
    assert report["passed_relative"] is False
    assert report["new_failures"] == []
    assert report["baseline_failures"] == []
    assert report["baseline_persisted"] is False
    # The absence the producer declared is still an absence.
    assert report["repo_gate_absent"] is True
    assert report["passed"] is False
    assert report["evidence_state"] == "not_run"
    assert report["release_evidence"] == "unresolved"
    assert result["release_evidence"] == "unresolved"
    # What the readers answer on the file the run wrote...
    assert _readers(report) == ("skipped", "unresolved")
    assert gate_deferral.read_absence(out / "test_report.json") is not None
    # ...and on a copy of it serialized to JSON and read back.
    copy = json.loads(json.dumps(report))
    assert _readers(copy) == ("skipped", "unresolved")


# ── controls: the same injected failure, on a measured red and on a green ──

def test_an_unstamped_absence_gets_no_label_or_disposition_it_never_had(
        tmp_path, monkeypatch, no_space):
    """Without an evidence cycle the report carries no `evidence_state` or
    `release_evidence` at all. The failed write must not CREATE either: the
    writer only restates a label the producer wrote."""
    install_entry_executor(monkeypatch)
    monkeypatch.setenv("AITELIER_REPO_GATE_RETRY_DELAY_SECONDS", "0")
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text(GREEN_TEST)
    _write_declared_gate(repo)
    state = tmp_path / "state"
    baseline = _baseline_file(state, repo)
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"failures": [OLD], "updated_at": 0}))

    result, report = _run(repo, tmp_path / "out", state, stamped=False)

    assert report["baseline_write_errno"] == errno.ENOSPC
    assert report["repo_gate_absent"] is True
    assert "release_evidence" not in report, "the failed write must not create a field"
    assert report.get("evidence_state") in (None, "not_run")
    assert ge.release_disposition(report) == "unresolved"
    assert ge.report_state(json.loads(json.dumps(report))) == "skipped"
    assert ge.release_disposition(json.loads(json.dumps(report))) == "unresolved"


def test_a_measured_red_whose_baseline_cannot_be_written_stays_a_failure(
        tmp_path, monkeypatch, no_space):
    install_entry_executor(monkeypatch)
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_bad.py").write_text(RED_TEST)
    state = tmp_path / "state"            # no baseline yet: the red is a seed, a pending write

    result, report = _run(repo, tmp_path / "out", state)

    assert report["baseline_write_errno"] == errno.ENOSPC
    assert report["returncode"] == 1
    assert result["passed"] is False and report["passed"] is False
    assert report["baseline_state"] == "unmeasured"
    assert report["passed_relative"] is False
    assert report.get("repo_gate_absent") is not True
    assert _readers(report) == ("failed", "known_failure")
    assert result["release_evidence"] == "known_failure"
    assert _readers(json.loads(json.dumps(report))) == ("failed", "known_failure")


def test_a_complete_green_whose_baseline_cannot_be_written_still_passes(
        tmp_path, monkeypatch, no_space):
    install_entry_executor(monkeypatch)
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text(GREEN_TEST)
    state = tmp_path / "state"            # no baseline yet: the green run has a pending write

    result, report = _run(repo, tmp_path / "out", state)

    assert report["baseline_write_errno"] == errno.ENOSPC
    assert result["passed"] is True and report["passed"] is True
    assert report["baseline_state"] == "unmeasured"
    assert _readers(report) == ("passed", "passed")
    assert result["release_evidence"] == "passed"
    assert _readers(json.loads(json.dumps(report))) == ("passed", "passed")
