"""Accepted early-red, bounded diagnostics and report writer composed together."""
import hashlib
import json
from pathlib import Path

import pytest

from aitelier.tools.run_tests import impl as rt
from tests.unit import test_repo_gate_early_contract_red as early


def test_early_contract_red_retains_full_raw_and_bounded_diagnostic(tmp_path, monkeypatch):
    diagnostic = "SCRIPT ERROR: generic authored schema contract failed"
    noisy = early.GATE.replace(
        'print("CONTRACT ERROR:", error, flush=True)',
        'print("CONTRACT ERROR:", error, flush=True)\n'
        f'    print({diagnostic!r}, error, flush=True)\n'
        '    print("  [0] original generic fixture contract check", flush=True)\n'
        '    print("suite ok\\n" * 900, flush=True)')
    monkeypatch.setattr(early, "GATE", noisy)
    gate = early.run_gate(tmp_path, monkeypatch, "empty")
    assert gate["returncode"] == 1 and gate["measured"] == rt.REPO_GATE_MEASURED_FAIL
    assert gate["report_attribution"]["state"] == "own"
    assert gate["gate_coverage"] is None and gate["gate_coverage_error"]
    assert gate["retained_findings"] == 1
    assert gate["output_truncated"] is True and diagnostic not in gate["output"]
    assert diagnostic in gate["failure_context"]
    assert "authored scenario set is empty" in gate["failure_context"]
    assert "original generic fixture contract check" in gate["failure_context"]
    assert len(gate["failure_context"]) <= 6000
    raw = Path(gate["output_ref"]["path"]).read_bytes()
    assert diagnostic.encode() in raw and raw.count(b"suite ok") == 900
    assert hashlib.sha256(raw).hexdigest() == gate["output_ref"]["sha256"]
    Path(gate["repo"], "gate.py").unlink()
    monkeypatch.setattr(rt, "_acquire_repo_gate", lambda _: gate)
    out = tmp_path / "writer"
    result = rt.run_tests(project_root=gate["repo"], out_dir=str(out))
    report = json.loads((out / "test_report.json").read_text())
    assert report["repo_gate"]["measured"] == rt.REPO_GATE_MEASURED_FAIL
    assert result["repo_gate_absent"] is False and report["passed"] is False
    assert report["repo_gate"]["failure_context"] == gate["failure_context"]
    assert report["repo_gate"]["output_ref"] == gate["output_ref"]
    assert any(f.startswith("repo_gate:run_tests.sh#manifest/") for f in report["failures"])
    assert result["release_evidence"] != "passed"


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_raw_only_gate_cannot_claim_complete_coverage(tmp_path, monkeypatch, exit_code):
    from tests.unit.test_run_tests_unmeasured_declaration import _write_raw_gate
    repo = tmp_path / "repo"; repo.mkdir()
    _write_raw_gate(repo, 'AITELIER_REPO_GATE_CASE={"case_id":"A","status":"failed","detail":"raw only"}',
                    exit_code=exit_code)
    monkeypatch.setattr(rt, "REPO_GATE_UNMEASURED_ATTEMPTS", 1)
    out, state = tmp_path / "out", tmp_path / "state"
    result = rt.run_tests(project_root=str(repo), out_dir=str(out), state_dir=str(state))
    report = json.loads((out / "test_report.json").read_text())
    assert report["repo_gate"]["returncode"] == exit_code
    assert report["repo_gate"]["measured"] == rt.REPO_GATE_UNMEASURED
    assert report["repo_gate"]["gate_coverage"] is None
    assert report["repo_gate"]["gate_coverage_error"]
    assert report["baseline_state"] == "unmeasured" and report["passed_relative"] is False
    assert result["passed"] is False and result["repo_gate_absent"] is True
    assert result["release_evidence"] != "passed"
    assert not (Path(rt._baseline_dir(str(state), repo)) / rt.BASELINE_FILE).exists()
