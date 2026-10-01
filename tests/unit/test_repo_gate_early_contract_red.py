"""Early authored-contract reds through a real repository-gate subprocess.

The fixture is original generic YAML validation, not private game source.
It retains the producer protocol before any stage or engine request exists.
"""
import json
import sys
from pathlib import Path

import pytest

from aitelier.gate_coverage import full_coverage, publication_scope_refusal
from aitelier.tools.run_tests import impl as rt


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
    (repo / "gate.py").write_text(GATE)
    script = repo / "run_tests.sh"
    script.write_text(f"#!/bin/sh\nexec {sys.executable} gate.py\n")
    script.chmod(0o755)
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("EARLY_GATE_MODE", mode)
    monkeypatch.setenv("EARLY_GATE_COVERAGE", json.dumps(coverage))
    result = rt._run_repo_gate(repo)
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
    monkeypatch.setattr(rt, "_acquire_repo_gate", lambda _: gate)
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
