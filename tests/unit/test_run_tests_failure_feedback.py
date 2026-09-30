import json
from pathlib import Path

import pytest
from aitelier.tools.run_tests import impl as rt


DIAGNOSTIC = 'SCRIPT ERROR: token=ACTOR locale=zh shown=ACTOR wanted=角色'
STACK = '   [0] check (res://tests/test_internal_token_display.gd:46)'


def capture(monkeypatch, tmp_path, text, rc=1):
    class Proc:
        returncode = rc
        def communicate(self, timeout):
            return text, ''
    monkeypatch.setattr(rt.subprocess, 'Popen', lambda *a, **kw: Proc())
    return rt._run_node_cmd(tmp_path, ['fake-gate'], 10,
                           env_overrides={'GATE_REPORT_DIR': str(tmp_path)})


def test_early_failure_reaches_implement_report(monkeypatch, tmp_path):
    text = 'mapper check\n' + DIAGNOSTIC + '\n' + STACK + '\n' + 'suite ok\n' * 900
    gate = capture(monkeypatch, tmp_path, text)
    assert gate['returncode'] == 1
    gate.update(script='run_tests.sh', measured=rt.REPO_GATE_MEASURED_FAIL)
    monkeypatch.setattr(rt, '_acquire_repo_gate', lambda repo: gate)
    monkeypatch.setattr(rt, '_resolve_pytest_python', lambda repo, report: (None, None))
    out = tmp_path / 'ws' / 'coding_impl' / 'test'
    rt.run_tests(project_root=str(tmp_path), out_dir=str(out))
    report = json.loads((out / 'test_report.json').read_text())
    assert DIAGNOSTIC in report['repo_gate']['failure_context']
    assert STACK in report['repo_gate']['failure_context']
    assert DIAGNOSTIC in '\n'.join(report['failures'])
    # Use the real no-shell implement consumer's declared step source.
    import yaml
    from skillflow.context import ContextResolver
    from skillflow.graph import _normalize_context_spec
    config = yaml.safe_load((Path(rt.__file__).parents[3] / 'configs' / 'coding_impl.yaml').read_text())
    implement = next(step for step in config['steps'] if step['id'] == 'implement')
    source = next(spec for spec in implement['context'] if spec.get('source', {}).get('step') == 'test')
    visible = '\n'.join(ContextResolver(tmp_path / 'ws').resolve(
        [_normalize_context_spec(source)], current_config='coding_impl').values())
    assert 'token=ACTOR locale=zh shown=ACTOR wanted=角色' in visible
    assert STACK in visible
    assert visible.count('suite ok') < 900
    raw = report['repo_gate']['output_ref']
    assert Path(raw['path']).read_text() == text + '\n'
    assert raw['sha256']
    assert len(report['repo_gate']['failure_context']) <= 6000
    assert report['failure_identity_error'] == 'repository gate did not emit per-case identities'


def test_case_identity_scanned_before_truncation(monkeypatch, tmp_path):
    record = 'AITELIER_REPO_GATE_CASE=' + json.dumps(dict(case_id='mapper/zh', status='failed', detail=DIAGNOSTIC))
    gate = capture(monkeypatch, tmp_path, record + '\n' + 'ok\n' * 2000)
    cases, error = rt._repo_gate_failure_cases(gate)
    assert error is None
    assert cases == [dict(case_id='mapper/zh', status='failed', detail=DIAGNOSTIC)]


@pytest.mark.parametrize('text', ['FAILSAFE enabled\nPASS', 'normal text FAIL substring\nPASS',
    'AITELIER_REPO_GATE_UNMEASURED={"state":"blocked","reason":"FAIL no measurement"}'])
def test_prose_and_declaration_are_not_diagnostics(monkeypatch, tmp_path, text):
    gate = capture(monkeypatch, tmp_path, text, 0)
    assert gate['failure_context'] == ''
    assert rt._repo_gate_failure_cases(gate)[0] == []
    expected = rt.REPO_GATE_UNMEASURED if text.startswith('AITELIER_REPO_GATE_UNMEASURED=') else rt.REPO_GATE_MEASURED_PASS
    assert rt._repo_gate_outcome(gate) == expected


def test_diagnostic_overflow_is_explicit(monkeypatch, tmp_path):
    gate = capture(monkeypatch, tmp_path, ('FAIL: ' + 'x' * 100 + '\n') * 200)
    assert len(gate['failure_context']) <= 6000
    assert gate['failure_context_truncated'] is True
    assert Path(gate['output_ref']['path']).exists()


@pytest.mark.parametrize('marker', ['SCRIPT ERROR:', 'ERROR:', 'push_error', 'FAILED', 'FAIL:'])
def test_multiline_failure_markers_keep_context_without_inventing_verdict(monkeypatch, tmp_path, marker):
    gate = capture(monkeypatch, tmp_path, 'mapper check\n' + marker + ' assertion\n'
                   'token=ACTOR locale=zh\nshown=ACTOR wanted=角色\n' + STACK + '\n' + 'ok\n' * 2000, 0)
    assert 'mapper check' in gate['failure_context']
    assert 'shown=ACTOR wanted=角色' in gate['failure_context']
    assert STACK in gate['failure_context']
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_MEASURED_PASS
    assert rt._repo_gate_failure_cases(gate)[0] == []


def test_legacy_truncated_capture_is_still_unknown():
    cases, error = rt._repo_gate_failure_cases(dict(output='FAIL: assertion', output_truncated=True))
    assert cases == []
    assert error == 'repository gate output was truncated'


def test_raw_output_does_not_follow_gate_created_symlink(monkeypatch, tmp_path):
    target = tmp_path / 'unrelated'
    target.write_text('keep')
    (tmp_path / 'command-output.txt').symlink_to(target)
    gate = capture(monkeypatch, tmp_path, DIAGNOSTIC)
    assert target.read_text() == 'keep'
    assert gate['output_retention_error']
    assert 'output_ref' not in gate
    assert DIAGNOSTIC in gate['failure_context']
