"""An unreadable baseline keeps its concrete cause in the delivered report AND the return (UNRUN by its author).

`_apply_baseline` records `baseline_error = "unreadable baseline <path>: <parse error>"`
when the stored baseline cannot be parsed. Historically (an Sep 20 measured run) the
normal tool return carried `baseline_state: unreadable` with no cause; the final
return now copies `report["baseline_error"]` (added by 49de597c35a9a1614eba0b01878c8c4e414c7b08).

These tests drive the real public `run_tests` through the existing owned executor
fixture (real entry, real pytest), with real baseline bytes on disk. Nothing about
the report or the baseline decision is hand-built or forced.

Run (director-registered isolated CPU check only):
    python -m pytest tests/unit/test_run_tests_baseline_diagnostic_delivery.py -q
"""
import json

import pytest

from tests.unit.test_run_tests_storage_exhaustion import (  # noqa: F401 (fixture re-export)
    GREEN, RED, _baseline, _run, env)


@pytest.mark.parametrize("source", [RED, GREEN], ids=["red", "green"])
@pytest.mark.parametrize("content,fragment", [
    (b"{not json", "line 1 column"),
    (b"\xff\xfe\x00garbage", "utf-8"),
], ids=["malformed-json", "undecodable-bytes"])
def test_unreadable_baseline_cause_is_in_report_file_and_return(env, source, content, fragment):
    path = _baseline(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    result, report = _run(env, source)

    assert report is not None and result["written"] == "test_report.json"
    assert report["baseline_state"] == "unreadable"
    assert result["baseline_state"] == "unreadable"
    cause = report.get("baseline_error")
    assert cause and cause.startswith("unreadable baseline "), cause
    assert str(path) in cause and fragment in cause, cause
    assert result["baseline_error"] == cause, "the normal return must carry the same concrete cause"
    assert report["passed_relative"] is False and result["passed_relative"] is False
    assert report["baseline_failures"] == [] and result["baseline_known"] == 0
    assert path.read_bytes() == content, "an unreadable baseline is never overwritten"


def test_unreadable_baseline_return_matches_persisted_report_bytes(env):
    path = _baseline(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not json")
    result, _ = _run(env, RED)
    on_disk = json.loads((env.out / "test_report.json").read_text())
    assert result["baseline_error"] == on_disk["baseline_error"] != None  # noqa: E711
    assert result["new_failures"] == on_disk["new_failures"] and result["new_failures"]


def test_measured_readable_baseline_has_no_spurious_unreadable_cause(env):
    seeded, seeded_report = _run(env, RED)            # real run seeds a real baseline file
    assert seeded["baseline_state"] == "seeded" and _baseline(env).is_file()
    assert not seeded["baseline_error"] and not seeded_report.get("baseline_error")

    compared, report = _run(env, RED)                 # real parse of that readable file
    assert report["baseline_state"] == compared["baseline_state"] == "compared"
    assert not compared["baseline_error"] and not report.get("baseline_error")
    assert compared["baseline_known"] == 1 and report["baseline_failures"]
    assert compared["new_failures"] == [] and compared["passed_relative"] is True
    assert report["passed_relative"] is True
