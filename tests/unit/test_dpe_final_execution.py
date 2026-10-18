"""Real tiny suites through unchanged run_tests/evidence, inside disposable CPU only.

Transport substitution reuses repository_executor_entry: it executes real pytest
in this already isolated test job. This is adapter/gate evidence, not a claim of
normal host-launcher ownership admission or a model-driven canary delivery.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from aitelier.tools.run_tests import impl as rt
from aitelier.tools.verify_evidence.impl import verify_evidence
from core import repository_executor_entry


@pytest.mark.parametrize('case,source,passed', [
    ('pass', 'def test_actual_sum():\n    assert sum([2, 3]) == 5\n', True),
    ('fail', 'def test_actual_sum():\n    assert sum([2, 3]) == 6\n', False),
    ('empty', '# No test was delivered\n', False),
    ('skipped', 'import pytest\npytest.skip("whole suite unavailable", allow_module_level=True)\n', False),
    ('unavailable', 'def test_actual_sum():\n    assert sum([2, 3]) == 5\n', False),
])
def test_actual_final_gate_never_replaces_execution_with_authored_source(tmp_path, monkeypatch, case, source, passed):
    repo = tmp_path / 'repo'; repo.mkdir(); (repo / 'test_behavior.py').write_text(source)
    graph = tmp_path / 'graph'; test_out = graph / '5_test'; test_out.mkdir(parents=True)
    calls = []
    def execute(root, args, timeout, **kwargs):
        calls.append(list(args))
        if case == 'unavailable':
            raise rt.repository_executor.IsolationUnavailable('controlled CPU facility unavailable')
        result = repository_executor_entry.run({'repo': str(root), 'args': args, 'timeout': min(timeout, 30)})
        return SimpleNamespace(**result)
    monkeypatch.setattr(rt.repository_executor, 'execute', execute)
    run_id = 'dpe-final-' + case
    result = rt.run_tests(project_root=str(repo), out_dir=str(test_out), repo_gate=False,
                          state_dir=str(tmp_path / 'state'), run_id=run_id, evidence_cycle_start=True)
    report = json.loads((test_out / 'test_report.json').read_text())
    audit = verify_evidence(out_dir=str(graph / '5_evidence'), run_id=run_id,
                            gates='[["5_test", "test_report.json"]]')
    assert calls and result['passed'] is passed and report['passed'] is passed
    assert audit['passed'] is passed
    if case in ('pass', 'fail'):
        assert report['returncode'] == (0 if passed else 1)
        assert ('1 passed' if passed else '1 failed') in report['summary']
    if case in ('empty', 'skipped'):
        assert report['no_tests_collected'] and report['returncode'] == 5
    if case == 'unavailable':
        assert report['infrastructure_unavailable'] and report['skipped']


def test_default_dpe_reuses_unchanged_grants_and_final_execution_route():
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / 'configs/dpe_default.yaml').read_text())
    steps = {s['id']: s for s in config['steps']}
    assert steps['t_impl']['output'] == {'mode': 'write', 'target': 'code'}
    roles = yaml.safe_load((root / 'agent_configs/dpe_default.yaml').read_text())
    assert roles['task_implementer']['tools'] == ['semantic_search', 'list_tree', 'test_write', 'read_test_written', 'apply_patch', 'capabilities_available']
    assert steps['task_loop']['transitions'][-1]['to'] == '5_test'
    assert steps['5_test']['tool_name'] == 'run_tests' and steps['5_test']['capability'] == 'stateful'
    assert steps['5_test']['transitions'][0]['to'] == '5_evidence'
    assert steps['5_evidence']['tool_name'] == 'verify_evidence'
    assert steps['5_evidence']['transitions'][0]['to'] == '5_readme'
    assert any(s.get('source', {}).get('required') and s['source'].get('file') == 'code_changes.json'
               for s in steps['t_impl_review']['context'])
