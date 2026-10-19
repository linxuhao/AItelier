"""A measured required gate failure must block tool and final evidence verdicts."""
import json
from pathlib import Path

from aitelier.tools.run_tests import impl as rt
from aitelier.tools.verify_evidence.impl import verify_evidence
from tests.unit.test_run_tests_failure_feedback import capture, DIAGNOSTIC
from tests.unit.test_dpe_refusal_boundaries.test_boundaries import retained_process_identity


def test_measured_failed_gate_stays_nonpassing_at_tool_and_final_consumer(tmp_path, monkeypatch):
    # Existing fixture substitutes only the external executor transport with the
    # real repository_executor_entry process. The subprocess really exits 1;
    # no test supplies a passed=False report or a final-consumer verdict.
    (tmp_path/'test_fixture_ok.py').write_text('def test_ok():\n    assert True\n')
    gate=capture(monkeypatch,tmp_path,DIAGNOSTIC+'\n',rc=1)
    assert gate['returncode']==1
    assert rt._repo_gate_outcome(gate)==rt.REPO_GATE_MEASURED_FAIL
    gate['script']='run_tests.sh'
    monkeypatch.setattr(rt,'_acquire_repo_gate',lambda repo,**kwargs:gate)
    workspace=tmp_path/'workspace';out=workspace/'5_test';run_id='measured-failed-final-gate'
    result=rt.run_tests(project_root=str(tmp_path),out_dir=str(out),run_id=run_id,
                        evidence_cycle_start=True)
    report=json.loads((out/'test_report.json').read_text())
    assert report['repo_gate']['returncode']==1
    assert report['repo_gate']['measured']==rt.REPO_GATE_MEASURED_FAIL
    assert not report.get('infrastructure_unavailable') and not report.get('skipped')
    assert result['passed']is False and report['passed']is False
    assert result['release_evidence']!='pass' and result['passed_relative']is False
    assert DIAGNOSTIC in '\n'.join(report['failures'])
    verdict=verify_evidence(out_dir=str(workspace/'5_evidence'),run_id=run_id,
                            gates=json.dumps([['5_test','test_report.json']]))
    retained=json.loads((workspace/'5_evidence/evidence_report.json').read_text())
    assert verdict['passed']is False and retained['passed']is False
    assert any(row['step']=='5_test'for row in retained['failed_gates'])
    Path('/raw/measured-failed-gate.json').write_text(json.dumps({
        'actual_process':gate,'tool_result':result,'test_report':report,
        'consumer_result':verdict,'consumer_report':retained},indent=2))
