"""Scripted native agent, real SkillFlow dispatch and owned filesystem effects."""
import json
from pathlib import Path
import pytest
from core.dpe_pipeline import NativeSideEffectsRetained
from tests.unit.test_native_direct_code import run_fixture, host, response, execute


def owned_tools(tmp_path, monkeypatch):
    holder, executions = {}, []
    def remove(file):
        target = holder['root'] / file
        previous = target.read_text()
        target.unlink()
        executions.append(('remove', previous))
        return {'deleted': [file], 'applied': True, 'execution': len(executions), 'previous': previous}
    def set_state(value):
        target = tmp_path / 'state.json'
        previous = json.loads(target.read_text()) if target.exists() else None
        target.write_text(json.dumps(value))
        executions.append(('state', value))
        return {'state_written': True, 'previous': previous, 'value': value, 'execution': len(executions)}
    tools = {
        'remove_owned': ({'parameters': {'file': {'type': 'string', 'required': True}}}, remove),
        'set_owned_state': ({'parameters': {'value': {'type': 'string', 'required': True}}}, set_state),
    }
    sf, rid, claim, root = run_fixture(tmp_path, monkeypatch, tools)
    holder['root'] = root
    return sf, rid, claim, root, executions


def test_delete_add_identical_delete_is_new_operation(tmp_path, monkeypatch):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    replies, receipts = [], []
    def turn(messages, **kwargs):
        receipts.extend(json.loads(m['content']) for m in messages if m.get('role') == 'tool' and m.get('name') == 'remove_owned' and json.loads(m['content']) not in receipts)
        replies.append(1)
        n = len(replies)
        if n == 1:
            return response('remove_owned', file='baseline.py')
        if n == 2:
            assert not (root / 'baseline.py').exists()
            return response('create', file='baseline.py', content='new incarnation\n')
        if n == 3:
            assert (root / 'baseline.py').read_text() == 'new incarnation\n'
            return response('remove_owned', file='baseline.py')
        if n == 4:
            return response('create', file='evidence.py', content='verified = True\n')
        return response('finish_step', summary='current delete really applied')
    e, ws = host(sf, rid, claim, root, turn)
    assert execute(e, ws, rid, claim)
    assert not (root / 'baseline.py').exists(), 'byte-identical final delete replayed obsolete success'
    assert len(executions) == 2
    assert receipts[-1]['previous'] == 'new incarnation\n'
    assert receipts[-1].get('replayed_side_effect') is False
    sf._conn.close()


def test_identical_state_call_after_other_mutation_executes(tmp_path, monkeypatch):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    count = []
    def turn(messages, **kwargs):
        count.append(1)
        n = len(count)
        if n <= 3:
            return response('set_owned_state', value='A' if n != 2 else 'B')
        if n == 4:
            return response('create', file='evidence.py', content='verified = True\n')
        return response('finish_step', summary='current state applied')
    e, ws = host(sf, rid, claim, root, turn)
    assert execute(e, ws, rid, claim)
    assert json.loads((tmp_path / 'state.json').read_text()) == 'A'
    assert executions == [('state', 'A'), ('state', 'B'), ('state', 'A')]
    sf._conn.close()


class HostCrash(BaseException):
    pass


def crash_after_fence(e, sf, rid, claim, tool):
    original = e._trace_cb
    def trace(category, event, payload):
        original(category, event, payload)
        if event == 'side_effect_completed' and payload['tool'] == tool:
            raise HostCrash('after durable fence, before tool-result delta')
    e._trace_cb = trace


def batch(*calls):
    from types import SimpleNamespace
    combined = [dict(tc, id=f'original-{i}') for i, call in enumerate(calls) for tc in call.tool_calls]
    return SimpleNamespace(text='', reasoning_content='', truncated=False, tool_calls=combined)


def traces(sf, rid):
    return [(r['event'], r['payload']) for r in sf.get_trace(rid)]


def test_crash_before_result_restores_retained_invocation_before_provider(tmp_path, monkeypatch):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    before_calls = []
    def before(messages, **kwargs):
        before_calls.append(1)
        return response('remove_owned', file='baseline.py')
    e, ws = host(sf, rid, claim, root, before)
    crash_after_fence(e, sf, rid, claim, 'remove_owned')
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    first_fences = {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')}
    observed, receipts = [], []
    def after(messages, **kwargs):
        observed.append(list(executions))
        receipts.extend(json.loads(m['content']) for m in messages if m.get('role') == 'tool' and m.get('name') == 'remove_owned')
        return response('finish_step', summary='retained invocation recovered')
    resumed, ws2 = host(sf, rid, claim, root, after)
    assert execute(resumed, ws2, rid, claim)
    assert before_calls == [1] and observed == [[('remove', 'baseline=True')]]
    assert executions == [('remove', 'baseline=True')] and not (root / 'baseline.py').exists()
    assert receipts[0]['replayed_side_effect'] is True and receipts[0]['executed_now'] is False
    assert {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')} == first_fences
    events = traces(sf, rid)
    assert any(name == 'native_batch_recovery' and p['provider_called'] is False for name, p in events)
    assert sum(name == 'side_effect_completed' for name, p in events) == 1
    sf._conn.close()


def test_incomplete_multicall_keeps_results_positions_across_two_crashes(tmp_path, monkeypatch):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    # Read and failed read are real dispatch calls; only successful state calls
    # receive mutation fences. Some results exist before the first crash.
    original_batch = batch(response('read', path='baseline.py'),
        response('set_owned_state', value='A'), response('read', path='absent.py'),
        response('set_owned_state', value='B'), response('remove_owned', file='baseline.py'))
    e, ws = host(sf, rid, claim, root, lambda **kw: original_batch)
    crash_after_fence(e, sf, rid, claim, 'set_owned_state')
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    original_bytes = {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')}
    unexpected_calls = []
    def should_not_call(messages, **kwargs):
        unexpected_calls.append(1)
        raise AssertionError('provider called before outstanding retained calls settled')
    again, ws2 = host(sf, rid, claim, root, should_not_call)
    crash_after_fence(again, sf, rid, claim, 'set_owned_state')
    with pytest.raises(HostCrash):
        execute(again, ws2, rid, claim)
    provider_states = []
    def final(messages, **kwargs):
        provider_states.append((list(executions), json.loads((tmp_path / 'state.json').read_text()), (root / 'baseline.py').exists()))
        return response('finish_step', summary='batch settled')
    restored, ws3 = host(sf, rid, claim, root, final)
    assert execute(restored, ws3, rid, claim)
    assert unexpected_calls == []
    assert executions == [('state', 'A'), ('state', 'B'), ('remove', 'baseline=True')]
    assert provider_states == [(executions, 'B', False)]
    for name, raw in original_bytes.items():
        assert (e._effect_fence_dir / name).read_bytes() == raw
    rows = traces(sf, rid)
    assert sum(name == 'side_effect_completed' for name, p in rows) == 3
    rebuilt = restored._hydrate_resume_observations(restored._rebuild_from_deltas(rows, 8))
    assert rebuilt['recovery_turn'] is None and rebuilt['dropped_tail'] == 0
    pairs = [m for m in rebuilt['messages'] if m.get('role') == 'tool' and m.get('tool_call_id', '').startswith('original-')]
    assert [m['tool_call_id'] for m in pairs] == [f'original-{i}' for i in range(5)]
    assert 'baseline=True' in pairs[0]['content']
    assert json.loads(pairs[2]['content']).get('error')
    assert json.loads(pairs[1]['content'])['replayed_side_effect'] is True
    assert json.loads(pairs[3]['content'])['replayed_side_effect'] is True
    sf._conn.close()


def test_confirmed_historical_effect_after_restart_is_new_invocation(tmp_path, monkeypatch):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    before_calls = []
    def before(messages, **kwargs):
        before_calls.append(1)
        if len(before_calls) == 1:
            return response('set_owned_state', value='A')
        raise HostCrash('completed batch retained before next provider response')
    e, ws = host(sf, rid, claim, root, before)
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    calls = []
    def after(messages, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            changed_id = response('set_owned_state', value='A')
            changed_id.tool_calls[0]['id'] = 'new-provider-id'
            return changed_id
        return response('finish_step', summary='new invocation')
    restored, ws2 = host(sf, rid, claim, root, after)
    assert execute(restored, ws2, rid, claim)
    assert executions == [('state', 'A'), ('state', 'A')]
    assert len(list(restored._effect_fence_dir.glob('*.json'))) == 2
    sf._conn.close()


def test_ordinary_native_retry_does_not_replay_previous_call(tmp_path, monkeypatch):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    calls = []
    def turn(messages, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError('scripted ordinary provider failure before output')
        if len(calls) in (1, 3):
            return response('set_owned_state', value='A')
        if len(calls) == 4:
            return response('create', file='evidence.py', content='verified = True\n')
        return response('finish_step', summary='retry completed')
    e, ws = host(sf, rid, claim, root, turn)
    e.factory.get_max_retries = lambda _: 2
    assert execute(e, ws, rid, claim)
    assert executions == [('state', 'A'), ('state', 'A')]
    state_receipts = [p for name, p in traces(sf, rid) if name == 'side_effect_completed' and p['tool'] == 'set_owned_state']
    assert len(state_receipts) == 2 and state_receipts[0]['invocation_key'] != state_receipts[1]['invocation_key']
    sf._conn.close()


def test_recovery_keeps_withdrawn_tool_ownership_guard(tmp_path, monkeypatch):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    original_batch = batch(response('set_owned_state', value='A'), response('remove_owned', file='baseline.py'))
    e, ws = host(sf, rid, claim, root, lambda **kw: original_batch)
    crash_after_fence(e, sf, rid, claim, 'set_owned_state')
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    claim.inputs['_tool_schemas'].pop('remove_owned')
    receipts = []
    def after(messages, **kwargs):
        receipts.extend(json.loads(m['content']) for m in messages if m.get('role') == 'tool' and m.get('name') == 'remove_owned')
        return response('finish_step', summary='refused withdrawn tool')
    restored, ws2 = host(sf, rid, claim, root, after)
    with pytest.raises(NativeSideEffectsRetained, match='recovery remains incomplete'):
        execute(restored, ws2, rid, claim)
    assert executions == [('state', 'A')] and (root / 'baseline.py').exists()
    assert receipts == []  # No provider may turn the unfinished refusal into success.
    unsettled = [p for event, p in traces(sf, rid) if event == 'native_recovery_action_unsettled']
    assert 'not granted' in restored._read_native_observation(unsettled[0]['result_ref'])
    sf._conn.close()


def use_legacy_fences(e, *, crash=False):
    original = e._persist_native_effect
    def persist(key, result, names, effect, **kwargs):
        ref = original(key, result, names, effect)
        if crash:
            raise HostCrash('legacy fence durable before completion trace')
        return ref
    e._persist_native_effect = persist


def test_legacy_unambiguous_durable_fence_recovers_without_execution(tmp_path, monkeypatch):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    e, ws = host(sf, rid, claim, root, lambda **kw: response('set_owned_state', value='A'))
    use_legacy_fences(e, crash=True)
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    first_bytes = {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')}
    provider_states = []
    def after(messages, **kwargs):
        provider_states.append(list(executions))
        return response('finish_step', summary='legacy recovered')
    restored, ws2 = host(sf, rid, claim, root, after)
    assert execute(restored, ws2, rid, claim)
    assert provider_states == [[('state', 'A')]] and executions == [('state', 'A')]
    assert {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')} == first_bytes
    sf._conn.close()


@pytest.mark.parametrize('ambiguous', ['confirmed-history', 'repeated-batch'])
def test_ambiguous_legacy_fence_refuses_without_new_owner_effect(tmp_path, monkeypatch, ambiguous):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    calls = []
    def before(messages, **kwargs):
        calls.append(1)
        if ambiguous == 'repeated-batch':
            return batch(response('set_owned_state', value='A'), response('set_owned_state', value='A'))
        return response('set_owned_state', value='A')
    e, ws = host(sf, rid, claim, root, before)
    use_legacy_fences(e, crash=ambiguous == 'repeated-batch')
    if ambiguous == 'confirmed-history':
        original = e._trace_cb
        seen_assistant = []
        def trace(category, event, payload):
            original(category, event, payload)
            if event == 'prompt_delta' and payload.get('role') == 'assistant':
                seen_assistant.append(1)
                if len(seen_assistant) == 2:
                    raise HostCrash('fresh same-argument batch header before body')
        e._trace_cb = trace
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    first_bytes = {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')}
    provider_calls = []
    def after(messages, **kwargs):
        provider_calls.append(1)
        return response('finish_step', summary='must not get here')
    restored, ws2 = host(sf, rid, claim, root, after)
    with pytest.raises(NativeSideEffectsRetained, match='legacy native fence is ambiguous'):
        execute(restored, ws2, rid, claim)
    assert provider_calls == [] and executions == [('state', 'A')]
    assert {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')} == first_bytes
    sf._conn.close()


def patch_fixture(tmp_path, monkeypatch):
    import skillflow
    from skillflow.core import SkillFlow
    from skillflow.graph import PipelineGraph, StepNode, Transition
    from skillflow.output_targets import git
    from skillflow.tool_loader import ToolLoader
    root = tmp_path / 'code'
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'baseline.py').write_text('baseline=True')
    git(root, 'add', '--', 'baseline.py')
    git(root, 'commit', '-qm', 'owned base')
    sf = SkillFlow(str(tmp_path / 'sf.db'),
        tool_loader=ToolLoader(Path(skillflow.__file__).parent / 'tools'),
        workspace_base=str(tmp_path / 'artifacts'),
        code_path_resolver=lambda pid, run_id=None: root)
    node = StepNode(id='implement', output_mode='write', output_target='code',
        output_allow_full_write=True, config={'extra_tools': ['apply_patch']},
        context=[{'from': 'repository', 'mode': 'tool'}], transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name='g', begin=node.id, steps=[node]))
    rid = sf.create_run('g', project_id='p')
    sf.start_run(rid)
    sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    monkeypatch.setattr('api.dependencies.get_skillflow', lambda: sf)
    assert 'apply_patch' in claim.inputs['_tool_schemas']
    return sf, rid, claim, root


def test_real_apply_patch_delete_add_identical_delete(tmp_path, monkeypatch):
    sf, rid, claim, root = patch_fixture(tmp_path, monkeypatch)
    delete = '*** Begin Patch\n*** Delete File: baseline.py\n*** End Patch'
    add = '*** Begin Patch\n*** Add File: baseline.py\n+recreated = True\n*** End Patch'
    evidence = '*** Begin Patch\n*** Add File: evidence.py\n+verified = True\n*** End Patch'
    requests, receipts, snapshots = [], [], []
    def turn(messages, **kwargs):
        snapshots.append((root / 'baseline.py').read_text() if (root / 'baseline.py').exists() else None)
        requests.append(1)
        receipts[:] = [json.loads(m['content']) for m in messages if m.get('role') == 'tool' and m.get('name') == 'apply_patch']
        n = len(requests)
        if n <= 4:
            return response('apply_patch', patch=[delete, add, delete, evidence][n - 1])
        return response('finish_step', summary='real repeated delete')
    e, ws = host(sf, rid, claim, root, turn)
    assert execute(e, ws, rid, claim)
    print(json.dumps({'actual_patch_receipts': receipts, 'snapshots': snapshots}))
    assert receipts[0].get('applied') is True and receipts[1].get('applied') is True
    assert snapshots[1] is None and snapshots[2] == 'recreated = True\n'
    assert not (root / 'baseline.py').exists(), 'real apply_patch replayed obsolete delete receipt'
    assert (root / 'evidence.py').read_text() == 'verified = True\n'
    assert receipts[2]['applied'] is True and receipts[2]['deleted'] == ['baseline.py']
    assert receipts[2]['replayed_side_effect'] is False and receipts[2]['executed_now'] is True
    completions = [p for name, p in traces(sf, rid) if name == 'side_effect_completed' and p['tool'] == 'apply_patch']
    assert len(completions) == 4
    assert completions[0]['call_key'] == completions[2]['call_key']
    assert completions[0]['invocation_key'] != completions[2]['invocation_key']
    sf._conn.close()


def test_real_apply_patch_crash_after_fence_is_not_executed_again(tmp_path, monkeypatch):
    sf, rid, claim, root = patch_fixture(tmp_path, monkeypatch)
    delete = '*** Begin Patch\n*** Delete File: baseline.py\n*** End Patch'
    e, ws = host(sf, rid, claim, root, lambda **kw: response('apply_patch', patch=delete))
    body_calls = []
    original_exec = e._exec_tool
    def execute_body(action):
        body_calls.append(action['tool'])
        return original_exec(action)
    e._exec_tool = execute_body
    crash_after_fence(e, sf, rid, claim, 'apply_patch')
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    receipts = []
    def after(messages, **kwargs):
        receipts.extend(json.loads(m['content']) for m in messages if m.get('role') == 'tool' and m.get('name') == 'apply_patch')
        return response('finish_step', summary='real patch recovery')
    restored, ws2 = host(sf, rid, claim, root, after)
    original_restored_exec = restored._exec_tool
    def restored_body(action):
        body_calls.append(action['tool'])
        return original_restored_exec(action)
    restored._exec_tool = restored_body
    assert execute(restored, ws2, rid, claim)
    assert body_calls.count('apply_patch') == 1 and not (root / 'baseline.py').exists()
    assert receipts[0]['applied'] is True and receipts[0]['replayed_side_effect'] is True
    assert receipts[0]['executed_now'] is False
    sf._conn.close()


def test_same_argument_calls_in_retained_batch_have_distinct_invocations(tmp_path, monkeypatch):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    original_batch = batch(response('set_owned_state', value='A'), response('set_owned_state', value='B'), response('set_owned_state', value='A'))
    e, ws = host(sf, rid, claim, root, lambda **kw: original_batch)
    original_trace = e._trace_cb
    def trace(category, event, payload):
        original_trace(category, event, payload)
        if event == 'side_effect_completed' and len(executions) == 3:
            raise HostCrash('third distinct mutation fenced')
    e._trace_cb = trace
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    provider_states = []
    def after(messages, **kwargs):
        provider_states.append(list(executions))
        return response('finish_step', summary='ordered repeated batch')
    restored, ws2 = host(sf, rid, claim, root, after)
    assert execute(restored, ws2, rid, claim)
    assert executions == [('state', 'A'), ('state', 'B'), ('state', 'A')]
    assert provider_states == [executions]
    assert len(list(e._effect_fence_dir.glob('*.json'))) == 3
    sf._conn.close()


def test_observed_partial_batch_turn_grant_is_preserved_exactly(tmp_path, monkeypatch):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    original_batch = batch(response('set_owned_state', value='A'), response('ask_more_turns', turns=3, reason='finish'), response('set_owned_state', value='B'), response('set_owned_state', value='C'))
    claim.inputs['_tool_schemas']['ask_more_turns'] = {'parameters': {'turns': {'type': 'integer'}, 'reason': {'type': 'string'}}}
    e, ws = host(sf, rid, claim, root, lambda **kw: original_batch)
    original_trace = e._trace_cb
    def trace(category, event, payload):
        original_trace(category, event, payload)
        if event == 'side_effect_completed' and len(executions) == 3:
            raise HostCrash('grant observed before third state fence')
    e._trace_cb = trace
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    original_grant = [p['content'] for name, p in traces(sf, rid) if name == 'prompt_delta' and p.get('tool_call_id') == 'original-1'][0]
    restored, ws2 = host(sf, rid, claim, root, lambda **kw: response('finish_step', summary='grant retained'))
    assert execute(restored, ws2, rid, claim)
    rebuilt = restored._hydrate_resume_observations(restored._rebuild_from_deltas(traces(sf, rid), 8))
    grants = [m['content'] for m in rebuilt['messages'] if m.get('tool_call_id') == 'original-1']
    assert grants == [original_grant]
    assert json.loads(original_grant)['status'] == 'granted'
    assert rebuilt['turn_grants'] == 1 and rebuilt['current_max_turns'] == 11
    assert executions == [('state', 'A'), ('state', 'B'), ('state', 'C')]
    sf._conn.close()


def test_missing_retained_mutation_output_never_starts_fresh_replay(tmp_path, monkeypatch):
    sf, rid, claim, root = run_fixture(tmp_path, monkeypatch)
    e, ws = host(sf, rid, claim, root, lambda **kw: response('create', file='retained.py', content='retained = True\n'))
    crash_after_fence(e, sf, rid, claim, 'create')
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    original_bytes = {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')}
    (root / 'retained.py').unlink()
    provider_calls = []
    def after(messages, **kwargs):
        provider_calls.append(1)
        return response('create', file='retained.py', content='retained = True\n')
    restored, ws2 = host(sf, rid, claim, root, after)
    with pytest.raises(NativeSideEffectsRetained, match='output is missing'):
        execute(restored, ws2, rid, claim)
    assert provider_calls == [] and not (root / 'retained.py').exists()
    assert {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')} == original_bytes
    sf._conn.close()


def test_modern_history_does_not_mask_unexecuted_identical_batch(tmp_path, monkeypatch):
    sf, rid, claim, root = patch_fixture(tmp_path, monkeypatch)
    delete = '*** Begin Patch\n*** Delete File: baseline.py\n*** End Patch'
    add = '*** Begin Patch\n*** Add File: baseline.py\n+recreated = True\n*** End Patch'
    before_calls = []
    def before(messages, **kwargs):
        before_calls.append(1)
        return response('apply_patch', patch=add if len(before_calls) == 2 else delete)
    e, ws = host(sf, rid, claim, root, before)
    original_trace = e._trace_cb
    headers = []
    def trace(category, event, payload):
        original_trace(category, event, payload)
        if event == 'prompt_delta' and payload.get('role') == 'assistant':
            headers.append(1)
            if len(headers) == 3:
                raise HostCrash('new identical invocation header retained before body')
    e._trace_cb = trace
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    assert (root / 'baseline.py').read_text() == 'recreated = True\n'
    original_bytes = {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')}
    assert len(original_bytes) == 2 and all(json.loads(b)['invocation_key'] for b in original_bytes.values())
    provider_states = []
    def after(messages, **kwargs):
        provider_states.append((root / 'baseline.py').exists())
        return response('finish_step', summary='current unexecuted delete settled')
    restored, ws2 = host(sf, rid, claim, root, after)
    assert execute(restored, ws2, rid, claim)
    assert provider_states == [False] and not (root / 'baseline.py').exists()
    for name, raw in original_bytes.items():
        assert (e._effect_fence_dir / name).read_bytes() == raw
    effects = [p for name, p in traces(sf, rid) if name == 'side_effect_completed' and p['tool'] == 'apply_patch']
    assert len(effects) == 3 and effects[0]['call_key'] == effects[2]['call_key']
    assert effects[0]['invocation_key'] != effects[2]['invocation_key']
    assert json.loads(effects[2]['result_json'])['executed_now'] is True
    sf._conn.close()


@pytest.mark.parametrize('damage', ['unreadable-trace', 'withdrawn-authority', 'pending-failure'])
def test_unsettled_recovery_cannot_accept_finish_and_can_later_repair(tmp_path, monkeypatch, damage):
    import sqlite3
    sf, rid, claim, root = patch_fixture(tmp_path, monkeypatch)
    def add(name):
        return response('apply_patch', patch=f'*** Begin Patch\n*** Add File: {name}\n+OWNED = 1\n*** End Patch')
    pending = (response('apply_patch', patch='*** Begin Patch\n*** Update File: C.py\n@@\n-OWNED = 0\n+OWNED = 1\n*** End Patch')
               if damage == 'pending-failure' else add('C.py'))
    e, ws = host(sf, rid, claim, root, lambda *a, **kw: batch(
        add('A.py'), add('B.py'), pending, response('finish_step', summary='all three owed')))
    persist, fences = e._persist_native_effect, []
    def crash(*args, **kwargs):
        result = persist(*args, **kwargs)
        fences.append(args[0])
        if len(fences) == 2:
            raise HostCrash('B fsynced before result delta')
        return result
    e._persist_native_effect = crash
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    original_bytes = {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')}
    assert (root / 'A.py').exists() and (root / 'B.py').exists()
    assert not (root / 'C.py').exists()
    schemas = dict(claim.inputs['_tool_schemas'])
    trace_reader = sf._get_trace_conn
    if damage == 'withdrawn-authority':
        claim.inputs['_tool_schemas'].pop('apply_patch')
    elif damage == 'unreadable-trace':
        def unavailable(_):
            raise sqlite3.OperationalError('owned trace read unavailable')
        monkeypatch.setattr(sf, '_get_trace_conn', unavailable)
    provider_calls = []
    def next_provider(*args, **kwargs):
        provider_calls.append(1)
        return response('finish_step', summary='must not mask incomplete C')
    restored, ws2 = host(sf, rid, claim, root, next_provider)
    with pytest.raises(NativeSideEffectsRetained):
        execute(restored, ws2, rid, claim)
    monkeypatch.setattr(sf, '_get_trace_conn', trace_reader)
    assert provider_calls == [] and not (root / 'C.py').exists()
    assert {p.name: p.read_bytes() for p in e._effect_fence_dir.glob('*.json')} == original_bytes
    assert not any(event == 'step_done' for event, _ in traces(sf, rid))
    # Repair the actual unavailable evidence/grant/precondition, then settle the
    # same retained batch. The failed action was not marked as already observed.
    claim.inputs['_tool_schemas'] = schemas
    if damage == 'pending-failure':
        (root / 'C.py').write_text('OWNED = 0\n')
    repaired, ws3 = host(sf, rid, claim, root, next_provider)
    assert execute(repaired, ws3, rid, claim)
    assert provider_calls == [] and (root / 'C.py').read_text() == 'OWNED = 1\n'
    for name, content in original_bytes.items():
        assert (e._effect_fence_dir / name).read_bytes() == content
    completed = [p for event, p in traces(sf, rid) if event == 'side_effect_completed']
    assert len(completed) == 2  # A traced; B crashed before its fence trace; C is current.
    sf._conn.close()


def test_failed_effect_from_named_read_remains_fenced_and_incomplete(tmp_path, monkeypatch):
    executions, holder = [], {}
    def state():
        (tmp_path / 'state.txt').write_text('A')
        return {'state_written': True}
    def named_read():
        executions.append(1)
        (holder['root'] / 'unexpected.py').write_text('retained')
        return {'error': 'failed after a declared partial write', 'written': ['unexpected.py']}
    sf, rid, claim, root = run_fixture(tmp_path, monkeypatch, {
        'state_control': ({'parameters': {}}, state),
        'read_file': ({'parameters': {}}, named_read),
    })
    holder['root'] = root
    original = batch(response('state_control'), response('read_file'), response('finish_step'))
    e, ws = host(sf, rid, claim, root, lambda **kw: original)
    crash_after_fence(e, sf, rid, claim, 'state_control')
    with pytest.raises(HostCrash):
        execute(e, ws, rid, claim)
    provider_calls = []
    def provider(**kw):
        provider_calls.append(1)
        return response('finish_step')
    for _ in range(2):
        resumed, ws2 = host(sf, rid, claim, root, provider)
        with pytest.raises(NativeSideEffectsRetained, match='recovery remains incomplete'):
            execute(resumed, ws2, rid, claim)
    assert executions == [1] and provider_calls == []
    assert (root / 'unexpected.py').read_text() == 'retained'
    assert len(list(e._effect_fence_dir.glob('*.json'))) == 2
    assert not any(event == 'step_done' for event, _ in traces(sf, rid))
    sf._conn.close()
