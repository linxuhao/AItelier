"""A pytest leg that was killed measured nothing, and must not read as a red suite.

Run 1a60bf3d spent four implement->test laps and about four hours being told
`passed: false` with an empty failures list, because its suite takes 83s and the
harness killed pytest at 75s. The report that came back was classified
`known_failure` with `passed_relative: true` — a verdict about tests that never
produced a result. These tests pin the two halves of that: the classification,
and the budget.
"""
import subprocess

from aitelier.gate_evidence import release_disposition, report_state
from aitelier.tools.run_tests import impl


def _timed_out_report():
    """The report the timeout branch produces, without running pytest.

    The branch is three statements on `report`; calling it through `run_tests`
    would need a repo that genuinely hangs for the whole wall, so the branch is
    exercised directly against a report shaped like the real initialiser.
    """
    report = {"passed": True, "returncode": 0, "summary": "", "failures": [],
              "collection_errors": []}
    try:
        raise subprocess.TimeoutExpired(cmd=["pytest"],
                                        timeout=impl.PYTEST_WALL_SECONDS)
    except subprocess.TimeoutExpired:
        report.update(
            passed=False, timed_out=True,
            skipped_because="pytest_timeout",
            pytest_wall_seconds=impl.PYTEST_WALL_SECONDS,
            summary=(
                f"pytest did not finish within {impl.PYTEST_WALL_SECONDS}s and was "
                "killed, so NOTHING was measured. This is not a test failure: "
                "no test result exists either way. Either the suite needs "
                "longer than this harness allows, or it hangs."))
        report["failures"].append(
            f"pytest:timed out after {impl.PYTEST_WALL_SECONDS}s — no results "
            "collected, the suite was killed mid-run")
    return report


def test_a_killed_pytest_leg_is_absent_evidence_not_a_failure():
    report = _timed_out_report()
    assert report["passed"] is False
    assert report_state(report) == "skipped"
    assert release_disposition(report) == "unresolved"


def test_the_timeout_does_not_classify_as_a_known_failure_via_the_baseline():
    """The exact misclassification observed on run 1a60bf3d.

    `_apply_baseline` sets `passed_relative: True` when the diff against the
    baseline is empty — and a timeout collects nothing, so it always is. Without
    a skip marker the classifier takes the `raw_passed is False` branch and
    answers `known_failure`, i.e. "ran, failed, but no worse than before".
    """
    report = _timed_out_report()
    report.update(passed_relative=True, new_failures=[], baseline_failures=[])
    assert report_state(report) == "skipped"
    assert release_disposition(report) != "known_failure"

    without_marker = dict(report)
    del without_marker["skipped_because"]
    del without_marker["timed_out"]
    assert report_state(without_marker) == "known_failure", (
        "the regression this guards against is no longer reachable; if the "
        "classifier changed, re-derive what a timeout should mean")


def test_the_timeout_names_itself_in_failures():
    """An empty failures[] next to passed=False is what made this unreadable."""
    report = _timed_out_report()
    assert report["failures"], "a killed leg must say why it produced nothing"
    assert any("timed out" in f for f in report["failures"])
    assert str(impl.PYTEST_WALL_SECONDS) in report["summary"]


def test_the_wall_clears_the_measured_suite_with_room():
    """83s measured 2026-09-18 on the wuxia game repo (2222 passed, 6 skipped).

    Pinned as a floor, not as the right number: the wall is a hang detector, and
    a suite that grows must not silently start being killed again.
    """
    assert impl.PYTEST_WALL_SECONDS >= 600


def test_the_budget_is_one_constant_and_not_a_repeated_literal():
    """The old code said `timeout=75` in one place and "after 75s" in another."""
    source = open(impl.__file__, encoding="utf-8").read()
    assert "timeout=75)" not in source
    assert "timed out after 75s" not in source
    assert source.count("PYTEST_WALL_SECONDS") >= 4
