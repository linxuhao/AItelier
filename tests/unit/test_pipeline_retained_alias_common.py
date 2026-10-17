"""Retained roles are immutable on EVERY native adoption path, not one editor.

The common construction is ``preview_adoption``: normal edits, restore and the
native ``adopt_bundle`` all build their next whole unit through it. An existing
role name may only be reused for a canonically identical definition."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from contracts.rsi.pipeline_versioning import contract as c
from contracts.rsi.pipeline_versioning.native_provider import NativeVersions
from contracts.rsi.pipeline_versioning.test_contract import load_vectors, literal
from test_pipeline_normal_versions import world, tools, call, claim, snapshot, source, ok, active, observe
from core import pipeline_frozen_bundle as fb, pipeline_registry as pr


def roles_now(fx):
    return {n: repr(fx.sf.agent_registry.get(n).to_dict()) for n in fx.sf.agent_registry.list_names()}


def evidence(name, value):
    out = Path('/evidence')
    if out.is_dir():
        with (out / name).open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(value, ensure_ascii=False, default=str) + '\n')


def preexisting_alias(fx, api, mutate, *, referenced):
    """Ordinarily register gen_vt (never adopted) with an owned role already at
    the alias adopting its own captured version would use, then start a run."""
    original = api.capture('gen_vt')
    prior = source(fx)
    roles = json.loads(prior['roles_text'])
    preview = fb.adopted_unit(fx.gen, 'gen_vt', original['physical_name'])
    added = {k: v for k, v in json.loads(preview['roles_text']).items() if k not in roles}
    alias, definition = next((k, v) for k, v in added.items() if k.endswith('_maker'))
    retained = mutate(copy.deepcopy(definition))
    roles[alias] = retained
    graph = yaml.safe_load(prior['graph_yaml'])
    graph['description'] = 'target registered beside a pre-existing alias'
    if referenced:
        for node, role in fb.agent_refs(graph):
            if role.endswith('__maker'):
                node['agent_config'] = alias
    (fx.gen / 'gen_vt.roles.json').write_text(json.dumps(roles, ensure_ascii=False, indent=2))
    (fx.gen / 'gen_vt.yaml').write_text(yaml.safe_dump(graph, sort_keys=False, allow_unicode=True))
    assert 'error' not in pr.reload_generated_pipeline(fx.sf, fx.registry, 'gen_vt')
    assert fb.read_descriptor(fx.gen, 'gen_vt') is None
    fx.start_run('Retained', 'gen_vt'); fx.pause_run('Retained')
    return original, alias, definition, retained


CONFLICTS = {
    # Python ``==`` calls these equal to the proposed 1.0; canonical bytes do not.
    'bool_for_float': (1.0, lambda d: {**d, 'temperature': True}),
    'int_for_float': (1.0, lambda d: {**d, 'temperature': 1}),
    'prompt_and_bool': (0.7, lambda d: {**d, 'temperature': True,
                                        'system_prompt': 'RETAINED RUN PROMPT\n'}),
}


@pytest.mark.parametrize('referenced', [True, False])
@pytest.mark.parametrize('case', sorted(CONFLICTS))
def test_conflicting_alias_refuses_on_every_adoption_path(world, case, referenced):
    fx, api = world
    temperature, mutate = CONFLICTS[case]
    unit = literal(load_vectors()['units']['U_A'])
    unit['roles']['$self__maker']['temperature'] = temperature
    fx.register('gen_vt', unit, None)
    original, alias, definition, retained = preexisting_alias(fx, api, mutate, referenced=referenced)
    if case != 'prompt_and_bool':
        assert retained['temperature'] == definition['temperature']  # the Python == trap
    assert fb.canonical(retained) != fb.canonical(definition)
    run_before = claim(fx, 'Retained')
    current = api.current('gen_vt')
    assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', source(fx)) == []
    before = snapshot(fx)
    outcomes = {}
    mcp = tools()['restore_pipeline_version']('gen_vt', original['version_id'], current['unit_identity'])
    outcomes['mcp_restore'] = mcp
    assert mcp.get('error') and 'retained role' in mcp['error'] and 'prepared_seals' not in mcp, mcp
    assert mcp['code'] == 'NATIVE_REFUSED'
    assert snapshot(fx) == before
    with pytest.raises(c.NativeRefused, match='retained role') as binding:
        NativeVersions(fx.sf, fx.registry, fx.gen).restore(
            'gen_vt', original['version_id'], expected_current=current['unit_identity'])
    outcomes['binding_restore'] = str(binding.value)
    assert snapshot(fx) == before
    with pytest.raises(fb.BundleRefused, match='retained role') as native:
        fb.adopt_bundle(fx.sf, fx.registry, 'gen_vt', original['physical_name'],
                        expected_current=current['unit_identity'], config_dir=fx.gen)
    outcomes['native_adopt'] = str(native.value)
    assert snapshot(fx) == before
    assert fb.read_descriptor(fx.gen, 'gen_vt') is None
    assert api.current('gen_vt') == current
    assert fb.canonical(json.loads(source(fx)['roles_text'])[alias]) == fb.canonical(retained)
    run_after = claim(fx, 'Retained')
    assert fb.canonical(run_after) == fb.canonical(run_before)
    if referenced:
        assert run_after['name'] == alias and fb.canonical(run_after['config']['temperature']) == fb.canonical(retained['temperature'])
    # The refusal is targeted: ordinary subsequent editing remains useful.
    edited = ok(call(fx, 'edit_template', role='maker', template='AFTER REFUSAL\n'))
    assert edited['changed'] and fb.canonical(json.loads(source(fx)['roles_text'])[alias]) == fb.canonical(retained)
    fx.start_run('After', 'gen_vt')
    assert claim(fx, 'After')['config']['system_prompt'] == 'AFTER REFUSAL\n'
    assert fb.canonical(claim(fx, 'Retained')) == fb.canonical(run_before)
    adopted = snapshot(fx)
    again = tools()['restore_pipeline_version']('gen_vt', original['version_id'],
                                                api.current('gen_vt')['unit_identity'])
    outcomes['mcp_restore_after_edit'] = again
    assert again.get('error') and 'retained role' in again['error'], again
    assert snapshot(fx) == adopted
    evidence('retained-alias-collisions.jsonl', {
        'case': case, 'referenced': referenced, 'alias': alias, 'proposed': definition,
        'retained': retained, 'outcomes': outcomes, 'claim_before': run_before,
        'claim_after': run_after, 'unchanged': True})


def test_identical_existing_alias_is_reused_by_restore(world):
    fx, api = world
    original, alias, definition, retained = preexisting_alias(fx, api, lambda d: d, referenced=True)
    assert fb.canonical(retained) == fb.canonical(definition)
    run_before = claim(fx, 'Retained')
    before_roles = roles_now(fx)
    current = api.current('gen_vt')
    done = tools()['restore_pipeline_version']('gen_vt', original['version_id'], current['unit_identity'])
    assert 'error' not in done and done['changed'], done
    assert active(fx) == alias
    assert api._capture_live('gen_vt')[2] == original['version_id']
    assert api.current('gen_vt')['graph_version'] == current['graph_version'] + 1
    assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', fb.recorded_unit(fx.gen, 'gen_vt')) == []
    assert fb.read_descriptor(fx.gen, 'gen_vt')['state'] == 'adopted'
    now = roles_now(fx)
    assert all(now[n] == r for n, r in before_roles.items())
    assert fb.canonical(claim(fx, 'Retained')) == fb.canonical(run_before)
    fx.start_run('Future', 'gen_vt')
    assert claim(fx, 'Future')['config']['system_prompt'] == 'OLD MAKER\n'
    evidence('retained-alias-identical.json', {'alias': alias, 'restore': done})


def test_frontdoor_alias_reuse_round_trip(world):
    """Re-adopting an already-retained version reuses its exact aliases."""
    fx, api = world
    fx.start_run('R0', 'gen_vt'); fx.pause_run('R0')
    one = ok(call(fx, 'edit_template', role='maker', template='FIRST\n'))
    a1 = active(fx)
    fx.start_run('R1', 'gen_vt'); fx.pause_run('R1')
    restore = tools()['restore_pipeline_version']
    back = ok(restore('gen_vt', one['previous_version_id'], api.current('gen_vt')['unit_identity']))
    assert back['changed']
    a0 = active(fx)
    fx.start_run('R0b', 'gen_vt'); fx.pause_run('R0b')
    steps = []
    for label, run, alias in [
            ('edit back to FIRST', lambda: call(fx, 'edit_template', role='maker', template='FIRST\n'), a1),
            ('restore original again', lambda: restore('gen_vt', one['previous_version_id'],
                                                       api.current('gen_vt')['unit_identity']), a0)]:
        prior = roles_now(fx)
        out = ok(run())
        assert out['changed'] and active(fx) == alias, (label, out)
        assert all(roles_now(fx)[n] == r for n, r in prior.items()), label
        assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', fb.recorded_unit(fx.gen, 'gen_vt')) == []
        steps.append({'step': label, 'result': out, 'alias': alias})
    for name, prompt in [('R0', 'OLD MAKER\n'), ('R1', 'FIRST\n'), ('R0b', 'OLD MAKER\n')]:
        assert claim(fx, name)['config']['system_prompt'] == prompt, name
    two = ok(call(fx, 'edit_role', role='maker', temperature=1.0))
    fx.start_run('Future', 'gen_vt')
    cfg = claim(fx, 'Future')
    assert cfg['config']['temperature'] == 1.0 and type(cfg['config']['temperature']) is float
    assert cfg['config']['system_prompt'] == 'OLD MAKER\n'
    observe('alias_reuse_round_trip', {'steps': steps, 'final_edit': two})
