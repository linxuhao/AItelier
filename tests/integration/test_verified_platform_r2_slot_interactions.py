"""Official fixed-slot identity and the repaired original mixed-write boundary."""
import json

import pytest

from tests.integration.test_verified_platform_r2_retained_boundaries import record
from tests.unit.test_json_delivery_failure import EXTRA_BRACE, action, engine, response
from tests.unit.test_json_slot_target_integrity import fixed, slot_engine
from core.dpe_pipeline import MaxRetriesExceeded
from skillflow import write_tools


@pytest.mark.parametrize('kind', ['string', 'file', 'output'])
@pytest.mark.parametrize('same_id', [False, True], ids=['unrelated-id-refused', 'same-id-repaired'])
@pytest.mark.parametrize('operation', ['create', 'edit'])
def test_official_required_slot_repair_retains_other_success_once(tmp_path, kind, same_id, operation):
    outputs = fixed(kind, 'docs/*.json')
    if operation == 'edit':
        (tmp_path / 'docs').mkdir()
        for name in ['a', 'b']:
            (tmp_path / f'docs/{name}.json').write_text('"BASE"\n')
        failed = action('edit_doc', id='a', old_str='ABSENT', new_str='FIXED')
        repair = action('edit_doc', id='a' if same_id else 'b', old_str='BASE', new_str='FIXED')
    else:
        failed = action('create_doc', id='a', initialContent='{invalid json')
        repair = action('create_doc', id='a' if same_id else 'b', initialContent={'value': 'FIXED'})
    seed = action('create_doc', id='seed', initialContent={'value': 'RETAINED'})
    e = slot_engine(tmp_path, [EXTRA_BRACE, response(seed, failed), response(repair),
                             response(action('finish_step'))], outputs)
    receipts = []
    execute = e._exec_tool

    def capture(call):
        result = execute(call)
        receipts.append({'call': call, 'receipt': result,
                         'resolved_target': write_tools.resolve_write_target('doc', outputs, call['params'])})
        return result

    e._exec_tool = capture
    value, error = None, None
    try:
        value = e.run_step(1, 'implement', None, 'fixture', agent_config_name='stub',
                           tool_schemas=e._tool_schemas, output_fixed=outputs,
                           output_dir=str(tmp_path), resolved_context={})
    except MaxRetriesExceeded as exc:
        error = str(exc)
    record(f'official-slot-{operation}-{kind}-{same_id}', {
        'value': value, 'error': error, 'calls': e.calls, 'receipts': receipts,
        'prompts': e.prompts, 'events': e.events, 'traces': e.traces,
        'files': {str(p.relative_to(tmp_path)): p.read_text()
                  for p in tmp_path.rglob('*') if p.is_file()}})
    assert (value is True) == same_id
    assert json.loads((tmp_path / 'docs/seed.json').read_text()) == {'value': 'RETAINED'}
    assert sum(c['params'].get('id') == 'seed' for c in e.calls) == 1
    assert receipts[1]['receipt'].get('error')
    assert receipts[1]['resolved_target'] == 'docs/a.json'
    assert receipts[2]['resolved_target'] == ('docs/a.json' if same_id else 'docs/b.json')
    assert 'Failed to parse JSON' in e.prompts[1]
    if operation == 'edit':
        assert (tmp_path / 'docs/a.json').read_text() == ('"FIXED"\n' if same_id else '"BASE"\n')
        assert (tmp_path / 'docs/b.json').read_text() == ('"BASE"\n' if same_id else '"FIXED"\n')
    else:
        assert (tmp_path / 'docs/a.json').exists() == same_id
        assert (tmp_path / 'docs/b.json').exists() == (not same_id)
    if same_id:
        assert error is None
        assert sum(k == 'step_done' for k, _ in e.events) == 1
    else:
        assert error and not any(k == 'step_done' for k, _ in e.events)


def test_original_mixed_create_and_edit_completes_only_after_real_edit_repair(tmp_path):
    replies = [EXTRA_BRACE,
        response(action('create', file='partial.py', content='PARTIAL = 1\n'),
                 action('edit', file='required.py', content='REQUIRED = 1\n')),
        response(action('read', path='required.py')),
        response(action('edit', file='required.py', content='REQUIRED = 1\n')),
        response(action('finish_step'))]
    e = engine(tmp_path, replies)
    e.factory.is_native = lambda _: False
    edit_calls = []

    def dispatch(call):
        e.calls.append(call)
        if call['tool'] == 'read':
            assert not (tmp_path / 'required.py').exists()
            assert not any(k == 'step_done' for k, _ in e.events)
            return {'content': 'required delivery still absent'}
        if call['tool'] == 'edit':
            edit_calls.append(call)
            if len(edit_calls) == 1:
                return {'error': 'owned required delivery edit failed'}
        (tmp_path / call['params']['file']).write_text(call['params']['content'])
        return {'written': [call['params']['file']]}

    e._exec_tool = dispatch
    value = e.run_step(1, 'implement', None, 'fixture', agent_config_name='stub',
                       tool_schemas=e._tool_schemas)
    record('original-mixed-write-genuine-repair', {
        'value': value, 'calls': e.calls, 'prompts': e.prompts, 'events': e.events,
        'traces': e.traces, 'partial_bytes': (tmp_path / 'partial.py').read_text(),
        'required_bytes': (tmp_path / 'required.py').read_text()})
    assert value is True
    assert len(edit_calls) == 2
    assert sum(c['tool'] == 'create' for c in e.calls) == 1
    assert (tmp_path / 'partial.py').read_text() == 'PARTIAL = 1\n'
    assert (tmp_path / 'required.py').read_text() == 'REQUIRED = 1\n'
    assert 'owned required delivery edit failed' in e.prompts[3]
    assert sum(k == 'step_done' for k, _ in e.events) == 1
