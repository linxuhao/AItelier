"""Native health compares each live role with what publication would serve.

Publication re-registers EVERY persisted role, historical ones included, so a
live definition that differs from that registration only in canonical type
(``true`` / ``1`` / ``1.0``, nested values too) is a mixed unit. Python ``==``
calls such pairs equal; ``whole_state`` must not, or an admitted edit changes
what an existing run claims under its unchanged graph pin. The host's own
registration defaults remain legitimate and editable."""
import copy
import json
from pathlib import Path

import pytest
import yaml
from skillflow.core import SkillFlowError

from contracts.rsi.pipeline_versioning import contract as c
from contracts.rsi.pipeline_versioning.native_provider import NativeVersions
from contracts.rsi.pipeline_versioning.test_contract import load_vectors, literal
from contracts.rsi.pipeline_versioning.test_native_provider import NativeFixture
from test_pipeline_normal_versions import world, tools, call, claim, config_edit, snapshot, source, ok
from core import pipeline_frozen_bundle as fb, pipeline_registry as pr
from core.pipeline_versions import PipelineVersions, active_roles

MAKER = 'gen_vt__maker'

# field: (persisted value, live value). Python == calls each pair equal.
DRIFTS = {
    'bool_for_float': ('temperature', 1.0, True),
    'int_for_float': ('temperature', 1.0, 1),
    'float_for_int': ('max_tool_turns', 11, 11.0),
    'nested_int_for_bool': ('thinking', {'enable': True, 'effort': 'high'},
                            {'enable': 1, 'effort': 'high'}),
}


def evidence(name, value):
    out = Path('/evidence')
    if out.is_dir():
        with (out / name).open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(value, ensure_ascii=False, default=repr) + '\n')


def register_with(fx, key, persisted):
    unit = literal(load_vectors()['units']['U_A'])
    unit['roles']['$self__maker'][key] = copy.deepcopy(persisted)
    fx.register('gen_vt', unit, None)


def drift_live(fx, role, key, value):
    """The host's supported role registration changes only the live definition."""
    prior = copy.deepcopy(fx.sf.agent_registry.get(role).to_dict())
    cfg = {**prior['config'], 'model': prior['model'], 'tools': prior['tools'], key: value}
    fx.sf.register_agent_config_from_dict(role, cfg)
    return cfg


def editors(fx):
    """All four actual editors: a changing edit and a no-op through each."""
    raw = source(fx)
    graph = yaml.safe_load(raw['graph_yaml'])
    graph['description'] = 'edit of a mixed unit'
    roles = json.loads(raw['roles_text'])
    active = active_roles('gen_vt', graph)['maker']
    roles[active] = {**roles[active], 'system_prompt': 'CONFIG EDIT\n'}
    prompt = json.loads(raw['roles_text'])[active].get('system_prompt')
    return {
        'edit_template': lambda: call(fx, 'edit_template', role='maker', template='NEW ON MIXED\n'),
        'edit_template_noop': lambda: call(fx, 'edit_template', role='maker', template=prompt),
        'edit_role': lambda: call(fx, 'edit_role', role='maker', temperature=0.5),
        'edit_role_noop': lambda: call(fx, 'edit_role', role='maker', model='flash'),
        'edit_pipeline': lambda: call(fx, 'edit_pipeline', graph_yaml=yaml.safe_dump(graph)),
        'edit_pipeline_noop': lambda: call(fx, 'edit_pipeline', graph_yaml=raw['graph_yaml']),
        'config_edit_roles': lambda: config_edit({
            'config_name': 'gen_vt', 'file': 'roles', 'old_str': raw['roles_text'],
            'new_str': json.dumps(roles, ensure_ascii=False, indent=2)}),
        'config_edit_graph': lambda: config_edit({
            'config_name': 'gen_vt', 'old_str': 'versioning fixture pipeline',
            'new_str': 'config edit of a mixed unit'}),
    }


def refuse_everywhere(fx, api, before, version_id, physical, problem):
    """Every editor, MCP and bare restore and native adoption refuse before effects."""
    outcomes = {}
    current = api.current('gen_vt')
    for name, edit in editors(fx).items():
        out = edit()
        outcomes[name] = out
        assert out.get('error') and 'registered mixed' in out['error'], (name, out)
        assert problem in out['error'] and 'prepared_seals' not in out, (name, out)
        assert out['changed'] is False, (name, out)
        assert snapshot(fx) == before, name
    mcp = tools()['restore_pipeline_version']('gen_vt', version_id, current['unit_identity'])
    outcomes['mcp_restore'] = mcp
    assert mcp.get('error') and 'registered mixed' in mcp['error'] and problem in mcp['error'], mcp
    assert snapshot(fx) == before
    with pytest.raises(c.NativeRefused, match='registered mixed') as binding:
        NativeVersions(fx.sf, fx.registry, fx.gen).restore(
            'gen_vt', version_id, expected_current=current['unit_identity'])
    outcomes['binding_restore'] = str(binding.value)
    assert snapshot(fx) == before
    with pytest.raises(fb.BundleRefused, match='registered mixed') as native:
        fb.adopt_bundle(fx.sf, fx.registry, 'gen_vt', physical,
                        expected_current=current['unit_identity'], config_dir=fx.gen)
    outcomes['native_adopt'] = str(native.value)
    assert snapshot(fx) == before
    assert api.current('gen_vt') == current
    return outcomes


@pytest.mark.parametrize('case', sorted(DRIFTS))
def test_typed_live_role_drift_refuses_every_editor_and_adoption(world, case):
    fx, api = world
    key, persisted, live = DRIFTS[case]
    register_with(fx, key, persisted)
    clean = api.capture('gen_vt')       # an ordinary catalog entry of the persisted unit
    drift_live(fx, MAKER, key, live)
    served = fx.sf.agent_registry.get(MAKER).config[key]
    assert served == persisted and not fb.same_canonical(served, persisted)   # the == trap
    fx.start_run('Old', 'gen_vt'); fx.pause_run('Old')
    old = claim(fx, 'Old')
    assert fb.canonical(old['config'][key]) == fb.canonical(live)
    assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', source(fx)) == [f'live role {MAKER}']
    before = snapshot(fx)
    outcomes = refuse_everywhere(fx, api, before, clean['version_id'], clean['physical_name'],
                                 f'live role {MAKER}')
    assert fb.read_descriptor(fx.gen, 'gen_vt') is None
    after = claim(fx, 'Old')
    assert fb.canonical(after) == fb.canonical(old)
    evidence('live-role-agreement.jsonl', {
        'case': case, 'field': key, 'persisted': persisted, 'live': live,
        'outcomes': outcomes, 'old_claim_before': old, 'old_claim_after': after,
        'snapshot_unchanged': True})


def test_historical_role_drift_is_mixed_until_reloaded(world):
    """An unreferenced historical role is re-registered by publication too."""
    fx, api = world
    register_with(fx, 'temperature', 1.0)
    fx.start_run('Old', 'gen_vt'); fx.pause_run('Old')
    first = ok(call(fx, 'edit_template', role='maker', template='ADOPTED\n'))
    assert first['changed'] and MAKER not in active_roles('gen_vt', fx.sf._graphs['gen_vt'].to_dict()).values()
    old = claim(fx, 'Old'); fx.pause_run('Old')
    assert old['name'] == MAKER and type(old['config']['temperature']) is float
    drift_live(fx, MAKER, 'temperature', True)
    unit = fb.recorded_unit(fx.gen, 'gen_vt')
    assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', unit) == [f'live role {MAKER}']
    block = fb.launch_block('gen_vt', fx.gen, sf=fx.sf, registry=fx.registry)
    assert block and f'live role {MAKER}' in block
    before = snapshot(fx)
    # The existing native admission gate refuses the pinned run before effects.
    with pytest.raises(SkillFlowError, match='not served as its recorded whole bundle'):
        fx.sf.resume_run(fx.runs['Old'])
    assert snapshot(fx) == before
    physical = api._sealed(first['previous_version_id'])[0]
    outcomes = refuse_everywhere(fx, api, before, first['previous_version_id'], physical,
                                 f'live role {MAKER}')
    assert fb.read_descriptor(fx.gen, 'gen_vt')['state'] == 'adopted'
    assert fb.launch_block('gen_vt', fx.gen, sf=fx.sf, registry=fx.registry) == block
    # The host's ordinary reload serves the persisted unit again; editing resumes.
    assert 'error' not in pr.reload_generated_pipeline(fx.sf, fx.registry, 'gen_vt')
    assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', unit) == []
    assert fb.canonical(claim(fx, 'Old')) == fb.canonical(old)
    later = ok(call(fx, 'edit_template', role='maker', template='AFTER RELOAD\n'))
    assert later['changed']
    fx.start_run('Future', 'gen_vt')
    assert claim(fx, 'Future')['config']['system_prompt'] == 'AFTER RELOAD\n'
    assert fb.canonical(claim(fx, 'Old')) == fb.canonical(old)
    evidence('live-role-agreement.jsonl', {
        'case': 'historical_bool_for_float', 'outcomes': outcomes, 'launch_block': block,
        'old_claim': old, 'snapshot_unchanged': True})


def test_legitimate_defaults_and_exact_numbers_stay_editable(world):
    """Fields the file omits are served with the host's registration defaults;
    that normalization is healthy, and every editor and restore stay useful."""
    fx, api = world
    unit = literal(load_vectors()['units']['U_A'])
    maker = unit['roles']['$self__maker']
    del maker['temperature'], maker['thinking']
    unit['roles']['$self__reviewer']['temperature'] = 1
    fx.register('gen_vt', unit, None)
    persisted = json.loads(source(fx)['roles_text'])[MAKER]
    served = fx.sf.agent_registry.get(MAKER).config
    assert 'temperature' not in persisted and 'thinking' not in persisted
    assert fb.canonical([served['temperature'], served['thinking']]) == fb.canonical([0.2, {'enable': True}])
    assert type(fx.sf.agent_registry.get('gen_vt__reviewer').config['temperature']) is int
    assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', source(fx)) == []
    fx.start_run('Old', 'gen_vt'); fx.pause_run('Old')
    old = claim(fx, 'Old')
    steps = [ok(call(fx, 'edit_template', role='maker', template='FIRST\n')),
             ok(call(fx, 'edit_role', role='maker', temperature=1))]
    raw = source(fx)
    graph = yaml.safe_load(raw['graph_yaml']); graph['description'] = 'third edit'
    steps.append(ok(call(fx, 'edit_pipeline', graph_yaml=yaml.safe_dump(graph))))
    steps.append(ok(config_edit({'config_name': 'gen_vt', 'old_str': 'third edit',
                                 'new_str': 'fourth edit'})))
    for step in steps:
        assert step['changed'], step
    assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', fb.recorded_unit(fx.gen, 'gen_vt')) == []
    assert fb.canonical(claim(fx, 'Old')) == fb.canonical(old)
    fx.start_run('Future', 'gen_vt')
    future = claim(fx, 'Future')
    assert future['config']['system_prompt'] == 'FIRST\n'
    assert type(future['config']['temperature']) is int and future['config']['temperature'] == 1
    restored = ok(tools()['restore_pipeline_version'](
        'gen_vt', steps[0]['previous_version_id'], api.current('gen_vt')['unit_identity']))
    assert restored['changed']
    assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', fb.recorded_unit(fx.gen, 'gen_vt')) == []
    fx.start_run('Restored', 'gen_vt')
    back = claim(fx, 'Restored')
    assert back['config']['system_prompt'] == 'OLD MAKER\n'
    assert fb.canonical([back['config']['temperature'], back['config']['thinking']]) == fb.canonical([0.2, {'enable': True}])
    assert fb.canonical(claim(fx, 'Old')) == fb.canonical(old)
    assert fb.canonical(claim(fx, 'Future')) == fb.canonical(future)
    again = ok(tools()['restore_pipeline_version'](
        'gen_vt', steps[0]['version_id'], api.current('gen_vt')['unit_identity']))
    assert again['changed']                                  # reuses its identical alias
    assert active_roles('gen_vt', fx.sf._graphs['gen_vt'].to_dict())['maker'] == steps[0]['role']
    evidence('live-role-agreement.jsonl', {
        'case': 'legitimate_defaults', 'persisted': persisted, 'served': served,
        'edits': steps, 'restore': restored, 'alias_reuse_restore': again,
        'old_claim': old, 'future_claim': future, 'restored_claim': back})


NUMERIC_TOOL = ("name: fx_probe\ndescription: versioning fixture probe\nparameters:\n"
                "  type: object\n  properties:\n    n: {type: integer, default: %s}\n")


@pytest.fixture
def numeric_tool_world(tmp_path):
    fx = NativeFixture(tmp_path)
    impl = load_vectors()['tools']['fx_probe@1']['files']['impl.py']
    fx.install_tool('fx_probe', {'tool.yaml': NUMERIC_TOOL % '1', 'impl.py': impl})
    fx.register('gen_vt', literal(load_vectors()['units']['U_A']), None)
    yield fx, PipelineVersions(fx.sf, fx.registry, fx.gen)
    fx.close()


def test_typed_tool_schema_change_is_a_changed_provider(numeric_tool_world):
    """The loader serves its cached schema (default 1); tool.yaml now says 1.0."""
    fx, api = numeric_tool_world
    fx.start_run('Old', 'gen_vt'); fx.pause_run('Old')
    old = claim(fx, 'Old')
    assert type(old['tool_schemas']['fx_probe']['parameters']['properties']['n']['default']) is int
    (fx.tools / 'fx_probe' / 'tool.yaml').write_text(NUMERIC_TOOL % '1.0', encoding='utf-8')
    before = snapshot(fx)
    out = call(fx, 'edit_template', role='maker', template='NEW ON CHANGED TOOL\n')
    assert out.get('code') == 'PROVIDER_CHANGED' and 'cached schema' in out['error'], out
    assert 'prepared_seals' not in out and snapshot(fx) == before
    assert fb.canonical(claim(fx, 'Old')) == fb.canonical(old)
