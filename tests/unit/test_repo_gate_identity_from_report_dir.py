"""A red repository gate says which cases are red, however long its output.

Criterion `failure-identity-survives-a-long-output`: identities are read from
the structured report the gate retains under its ticket (GATE_REPORT_DIR),
never from the bounded tail of its output, so the tail's size changes nothing
about them. The gate here runs through the admission relay against the REAL
harness admission code (`tests/gate_fixture.py`) and prints more progress
output than the retained tail holds, as the game repository's gate does.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aitelier.tools.run_tests import impl as rt
from tests.gate_fixture import HarnessRig, red_report, write_gate


@pytest.fixture
def rig(tmp_path, monkeypatch):
    rig = HarnessRig(tmp_path / "harness", monkeypatch)
    rig.script_report = red_report()
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    monkeypatch.setenv("FIXTURE_GATE_NOISE", "6000")
    yield rig
    rig.close()


def _run(tmp_path, name):
    repo = tmp_path / name
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    write_gate(repo)
    state = tmp_path / f"state-{name}"
    baseline_dir = Path(rt._baseline_dir(str(state), repo.resolve()))
    baseline_dir.mkdir(parents=True)
    # A taken baseline with no known red: every red of this run is new.
    (baseline_dir / rt.BASELINE_FILE).write_text(json.dumps({"failures": []}))
    out = tmp_path / f"out-{name}"
    result = rt.run_tests(project_root=str(repo), out_dir=str(out),
                          state_dir=str(state))
    return result, json.loads((out / "test_report.json").read_text())


def _identities(report):
    return [rt._failure_key(f) for f in report["new_failures"]]


def test_a_long_red_output_still_names_every_failure(rig, tmp_path):
    result, report = _run(tmp_path, "repo")
    gate = report["repo_gate"]
    assert gate["returncode"] == 1
    assert gate["output_truncated"] is True
    assert len(gate["output"]) == rt.OUTPUT_TAIL_CHARS
    assert "failure_identity_error" not in report
    assert report["baseline_state"] == "compared"
    ids = _identities(report)
    assert len(ids) == 3 and len(set(ids)) == 3, ids
    assert all(i.startswith("repo_gate:run_tests.sh#script/") for i in ids), ids
    assert result["new_failures"] == report["new_failures"]
    joined = "\n".join(report["new_failures"])
    for text in ("script res://tests/a.gd FAILED", "script res://tests/b.gd FAILED",
                 "script gate failed: 0 passed, 2 failed"):
        assert text in joined


def test_the_identities_do_not_depend_on_the_retained_tail(rig, tmp_path, monkeypatch):
    """The same failure, read with the 2000-character tail and with a tail
    larger than the whole output: the same entries, one for one."""
    _, bounded = _run(tmp_path, "bounded")
    monkeypatch.setattr(rt, "OUTPUT_TAIL_CHARS", 10 ** 9)
    _, whole = _run(tmp_path, "whole")
    assert bounded["repo_gate"]["output_truncated"] is True
    assert whole["repo_gate"]["output_truncated"] is False
    assert _identities(bounded) == _identities(whole)
    assert bounded["new_failures"] == whole["new_failures"]


def _ticket(tmp_path, manifest, files):
    ticket = tmp_path / "ticket"
    report = ticket / "wuxia-godot-gate-x"
    report.mkdir(parents=True)
    (report / "manifest.json").write_text(json.dumps(manifest))
    for name, value in files.items():
        (report / name).write_text(json.dumps(value))
    return ticket


def test_a_stage_report_without_findings_contributes_its_errors(tmp_path):
    """The game gate writes no findings file for /compile; a failed compile
    names its located errors."""
    ticket = _ticket(tmp_path, {"stages": {
        "python": {"report": "python.json"},
        "compile": {"report": "compile.json"}}}, {
        "python.json": {"returncode": 0}, "python-findings.json": [],
        "compile.json": {"passed": False, "errors": [
            {"file": "res://a.gd", "line": 3, "msg": "Parse Error"},
            {"file": "res://b.gd", "line": 9, "msg": "Identifier not declared"}]}})
    cases, error = rt._report_dir_failure_cases(ticket)
    assert error is None
    assert [c["detail"] for c in cases] == ["res://a.gd:3: Parse Error",
                                           "res://b.gd:9: Identifier not declared"]
    assert all(c["case_id"].startswith("compile/") for c in cases)


def test_a_red_gate_whose_report_names_nothing_is_an_identity_error(tmp_path):
    """Never a pass-on-absence: a red gate with an empty report is unreadable."""
    ticket = _ticket(tmp_path, {"stages": {"python": {"report": "python.json"}}},
                     {"python.json": {"returncode": 1}, "python-findings.json": []})
    cases, error = rt._report_dir_failure_cases(ticket)
    assert cases == [] and error


def test_two_reports_under_one_ticket_are_an_identity_error(tmp_path):
    ticket = _ticket(tmp_path, {"stages": {}}, {})
    second = ticket / "another"
    second.mkdir()
    (second / "manifest.json").write_text("{}")
    cases, error = rt._report_dir_failure_cases(ticket)
    assert cases == [] and "2 reports" in error


def test_no_retained_report_falls_back_to_the_output_records(tmp_path):
    gate = {"returncode": 1, "output_truncated": False,
            "report_dir": str(tmp_path / "empty-ticket"),
            "output": 'AITELIER_REPO_GATE_CASE={"case_id":"c1","status":"failed"}'}
    (tmp_path / "empty-ticket").mkdir()
    cases, error = rt._repo_gate_failure_cases(gate)
    assert error is None and [c["case_id"] for c in cases] == ["c1"]
