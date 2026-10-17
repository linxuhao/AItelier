"""Every role an existing run actually resolves stays exact through adoption.

Persisted roles are not every name the live AgentRegistry serves. The host's
supported ``register_agent_config_from_dict`` plus an ordinary older graph and
run can leave the alias an adoption will publish existing ONLY live. Every
changing path (MCP and bare restore, native ``adopt_bundle`` and the normal
editors) compares the complete proposed registration, as the host would serve
it, with each same-named live entry before any effect: a different definition
refuses with nothing changed, an identical one is reused."""
import copy
import json
from pathlib import Path

import pytest
import yaml
from skillflow import PipelineGraph

from contracts.rsi.pipeline_versioning import contract as c
from contracts.rsi.pipeline_versioning.native_provider import NativeVersions
from contracts.rsi.pipeline_versioning.test_contract import load_vectors, literal
from test_pipeline_normal_versions import world, tools, call, claim, snapshot, source, ok, active
from core import pipeline_frozen_bundle as fb, pipeline_registry as pr

# How the live-only definition differs from the one adoption proposes. The
# typed pairs are equal to Python ``==``; canonical values tell them apart.
CONFLICTS = {
    'prompt': lambda d: {**d, 'system_prompt': 'LIVE ONLY RETAINED PROMPT\n'},
    'prompt_and_bool': lambda d: {**d, 'system_prompt': 'LIVE ONLY RETAINED PROMPT\n',
                                  'temperature': True},
    'bool_for_float': lambda d: {**d, 'temperature': True},
    'int_for_float': lambda d: {**d, 'temperature': 1},
    'float_for_int': lambda d: {**d, 'max_tool_turns': 11.0},
    'nested_int_for_bool': lambda d: {**d, 'thinking': {**d['thinking'], 'enable': 1}},
    'model': lambda d: {**d, 'model': 'pro'},
    'tools': lambda d: {**d, 'tools': []},
    'extra_setting': lambda d: {**d, 'max_output_tokens': 512},
}


def evidence(name, value):
    out = Path('/evidence')
    if out.is_dir():
        with (out / name).open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(value, ensure_ascii=False, default=repr) + '\n')


def register_unit(fx, defaults=False):
    unit = literal(load_vectors()['units']['U_A'])
    maker = unit['roles']['$self__maker']
    if defaults:                        # served with the host's registration defaults
        del maker['temperature'], maker['thinking']
    else:
        maker['temperature'] = 1.0
    fx.register('gen_vt', unit, None)


def destination(fx, api):
    """The catalogued original version and the maker alias adopting it would publish."""
    original = api.capture('gen_vt')
    preview = fb.adopted_unit(fx.gen, 'gen_vt', original['physical_name'])
    persisted = json.loads(source(fx)['roles_text'])
    alias, proposed = next((k, v) for k, v in json.loads(preview['roles_text']).items()
                           if k not in persisted and k.endswith('_maker'))
    return original, alias, proposed


def live_only(fx, alias, definition):
    """Supported host registration of *alias* (live only, as publication would
    normalize it), a genuine older graph version referencing it and an SDK run
    pinned there, then the ordinary reload of the persisted target."""
    effective = fb._expected_roles('gen_vt', {alias: definition})[alias]
    fx.sf.register_agent_config_from_dict(
        alias, {'model': effective.model, 'tools': list(effective.tools),
                **copy.deepcopy(effective.config)})
    graph = yaml.safe_load(source(fx)['graph_yaml'])
    graph['description'] = 'older graph version using a live-only role'
    for node, role in fb.agent_refs(graph):
        if role.endswith('__maker'):
            node['agent_config'] = alias
    path = fx.root / 'older-live-only.yaml'
    path.write_text(yaml.safe_dump(graph, sort_keys=False))
    fx.sf.register_graph(PipelineGraph.from_yaml(path))
    fx.start_run('OldAlias', 'gen_vt'); fx.pause_run('OldAlias')
    first = claim(fx, 'OldAlias')
    assert first['name'] == alias
    assert 'error' not in pr.reload_generated_pipeline(fx.sf, fx.registry, 'gen_vt')
    assert alias not in json.loads(source(fx)['roles_text'])
    return first


def recorded(fx):
    return fb.recorded_unit(fx.gen, 'gen_vt') or source(fx)


def healthy(fx):
    return fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', recorded(fx))


def refuse_all(fx, api, original, alias):
    """MCP restore, bare restore, native adoption and an editor whose result is
    the same version: each refuses with every provider unchanged."""
    current = api.current('gen_vt')
    pin = fx.sf.get_run(fx.runs['OldAlias'])
    descriptor = fb.read_descriptor(fx.gen, 'gen_vt')
    before = snapshot(fx)
    outcomes = {}
    mcp = tools()['restore_pipeline_version']('gen_vt', original['version_id'],
                                              current['unit_identity'])
    outcomes['mcp_restore'] = mcp
    assert mcp.get('error') and f'would replace live role {alias}' in mcp['error'], mcp
    assert mcp['code'] == 'NATIVE_REFUSED' and 'prepared_seals' not in mcp
    assert snapshot(fx) == before
    with pytest.raises(c.NativeRefused, match=f'would replace live role {alias}') as bare:
        NativeVersions(fx.sf, fx.registry, fx.gen).restore(
            'gen_vt', original['version_id'], expected_current=current['unit_identity'])
    outcomes['binding_restore'] = str(bare.value)
    assert snapshot(fx) == before
    with pytest.raises(fb.BundleRefused, match=f'would replace live role {alias}') as native:
        fb.adopt_bundle(fx.sf, fx.registry, 'gen_vt', original['physical_name'],
                        expected_current=current['unit_identity'], config_dir=fx.gen)
    outcomes['native_adopt'] = str(native.value)
    assert snapshot(fx) == before
    edit = call(fx, 'edit_template', role='maker', template='OLD MAKER\n')
    outcomes['edit_template_to_original'] = edit
    assert edit.get('error') and f'would replace live role {alias}' in edit['error'], edit
    assert edit['changed'] is False and 'prepared_seals' not in edit
    after = snapshot(fx)
    assert after == before
    assert api.current('gen_vt') == current
    assert fb.read_descriptor(fx.gen, 'gen_vt') == descriptor
    assert fx.sf.get_run(fx.runs['OldAlias']) == pin
    return outcomes, json.loads(before), json.loads(after), current, pin


@pytest.mark.parametrize('case', sorted(CONFLICTS))
def test_conflicting_live_only_destination_refuses_every_changing_path(world, case):
    fx, api = world
    register_unit(fx)
    original, alias, proposed = destination(fx, api)
    retained = CONFLICTS[case](copy.deepcopy(proposed))
    assert fb.canonical(retained) != fb.canonical(proposed)
    first = live_only(fx, alias, retained)
    # Ordinary adopting edit: the current target moves on, the alias stays live only.
    moved = ok(call(fx, 'edit_template', role='maker', template='CURRENT NEW CONTENT\n'))
    assert moved['changed'] and alias not in json.loads(source(fx)['roles_text'])
    old = claim(fx, 'OldAlias')
    assert fb.canonical(old) == fb.canonical(first)
    assert healthy(fx) == []
    outcomes, before, after, current, pin = refuse_all(fx, api, original, alias)
    assert healthy(fx) == []
    assert fb.served_role_conflicts(fx.sf, 'gen_vt', fb.adopted_unit(
        fx.gen, 'gen_vt', original['physical_name'])) == [alias]
    assert fb.canonical(claim(fx, 'OldAlias')) == fb.canonical(old)
    # The refusal is targeted: a second and third edit stay useful.
    second = ok(call(fx, 'edit_template', role='maker', template='SECOND EDIT\n'))
    third = ok(call(fx, 'edit_role', role='maker', temperature=0.5))
    assert second['changed'] and third['changed']
    fx.start_run('Future', 'gen_vt')
    future = claim(fx, 'Future')
    assert future['config']['system_prompt'] == 'SECOND EDIT\n'
    assert fb.canonical(future['config']['temperature']) == fb.canonical(0.5)
    assert fb.canonical(claim(fx, 'OldAlias')) == fb.canonical(old)
    assert fx.sf.get_run(fx.runs['OldAlias'])['graph_version'] == pin['graph_version']
    assert healthy(fx) == []
    evidence('all-served-roles.jsonl', {
        'test': 'conflict', 'case': case, 'alias': alias, 'proposed': proposed,
        'retained': retained, 'outcomes': outcomes, 'current': current,
        'sdk_run': pin, 'old_claim_before': old, 'old_claim_after': claim(fx, 'OldAlias'),
        'snapshot_before': before, 'snapshot_after': after, 'snapshot_unchanged': before == after,
        'second_edit': second, 'third_edit': third, 'future_claim': future})


def test_editor_refuses_before_cataloging_an_uncataloged_current_unit(world):
    """The normal editors run the same preflight before preparation: with the
    current unit not yet catalogued, no seal is written."""
    fx, api = world
    register_unit(fx)
    original, alias, proposed = destination(fx, api)
    first = live_only(fx, alias, CONFLICTS['prompt_and_bool'](copy.deepcopy(proposed)))
    unit = literal(load_vectors()['units']['U_A'])
    unit['roles']['$self__maker'].update(temperature=1.0, system_prompt='INTERIM\n')
    fx.register('gen_vt', unit, None)   # ordinary file change + reload; never adopted
    assert fb.read_descriptor(fx.gen, 'gen_vt') is None and healthy(fx) == []
    catalog = api.list_versions()
    assert catalog == [original['version_id']] and api._live_id('gen_vt') not in catalog
    old = claim(fx, 'OldAlias')
    assert fb.canonical(old) == fb.canonical(first)
    seals = sorted(p.name for p in fx.gen.glob('gen_rsi_*'))
    outcomes, before, after, current, pin = refuse_all(fx, api, original, alias)
    assert api.list_versions() == catalog
    assert sorted(p.name for p in fx.gen.glob('gen_rsi_*')) == seals
    assert fb.read_descriptor(fx.gen, 'gen_vt') is None
    assert fb.canonical(claim(fx, 'OldAlias')) == fb.canonical(old)
    later = ok(call(fx, 'edit_template', role='maker', template='THIRD\n'))
    assert later['changed'] and len(api.list_versions()) == 3
    fx.start_run('Future', 'gen_vt')
    future = claim(fx, 'Future')
    assert future['config']['system_prompt'] == 'THIRD\n'
    assert fb.canonical(claim(fx, 'OldAlias')) == fb.canonical(old)
    assert healthy(fx) == []
    evidence('all-served-roles.jsonl', {
        'test': 'editor_before_catalog', 'alias': alias, 'outcomes': outcomes,
        'catalog_before': catalog, 'seal_files': seals, 'current': current, 'sdk_run': pin,
        'old_claim': old, 'snapshot_before': before, 'snapshot_after': after,
        'later_edit': later, 'future_claim': future})


PATHS = ('mcp_restore', 'binding_restore', 'native_adopt', 'edit_template')


@pytest.mark.parametrize('defaults', [False, True], ids=['explicit', 'host-defaults'])
@pytest.mark.parametrize('path', PATHS)
def test_identical_live_only_destination_is_reused(world, path, defaults):
    fx, api = world
    register_unit(fx, defaults=defaults)
    original, alias, proposed = destination(fx, api)
    if defaults:
        assert fb.canonical([proposed['temperature'], proposed['thinking']]) == \
            fb.canonical([0.2, {'enable': True}])
    first = live_only(fx, alias, copy.deepcopy(proposed))
    ok(call(fx, 'edit_template', role='maker', template='CURRENT NEW CONTENT\n'))
    old = claim(fx, 'OldAlias')
    assert fb.canonical(old) == fb.canonical(first) and healthy(fx) == []
    roles_before = {n: repr(fx.sf.agent_registry.get(n).to_dict())
                    for n in fx.sf.agent_registry.list_names()}
    current = api.current('gen_vt')
    if path == 'mcp_restore':
        done = ok(tools()['restore_pipeline_version']('gen_vt', original['version_id'],
                                                      current['unit_identity']))
        assert done['changed']
    elif path == 'binding_restore':
        done = NativeVersions(fx.sf, fx.registry, fx.gen).restore(
            'gen_vt', original['version_id'], expected_current=current['unit_identity'])
        assert done['changed']
    elif path == 'native_adopt':
        done = fb.adopt_bundle(fx.sf, fx.registry, 'gen_vt', original['physical_name'],
                               expected_current=current['unit_identity'], config_dir=fx.gen)
    else:
        done = ok(call(fx, 'edit_template', role='maker', template='OLD MAKER\n'))
        assert done['changed'] and done['version_id'] == original['version_id']
    assert active(fx) == alias and healthy(fx) == []
    assert fb.read_descriptor(fx.gen, 'gen_vt')['state'] == 'adopted'
    assert api._capture_live('gen_vt')[2] == original['version_id']
    now = {n: repr(fx.sf.agent_registry.get(n).to_dict()) for n in fx.sf.agent_registry.list_names()}
    assert all(now[n] == r for n, r in roles_before.items())
    assert fb.canonical(claim(fx, 'OldAlias')) == fb.canonical(old)
    fx.start_run('Future', 'gen_vt')
    future = claim(fx, 'Future')
    assert future['name'] == alias and fb.canonical(future['config']) == fb.canonical(old['config'])
    second = ok(call(fx, 'edit_template', role='maker', template='AFTER REUSE\n'))
    fx.start_run('Later', 'gen_vt')
    assert second['changed'] and claim(fx, 'Later')['config']['system_prompt'] == 'AFTER REUSE\n'
    assert fb.canonical(claim(fx, 'OldAlias')) == fb.canonical(old)
    evidence('all-served-roles.jsonl', {
        'test': 'identical_reuse', 'path': path, 'defaults': defaults, 'alias': alias,
        'result': done, 'old_claim': old, 'future_claim': future, 'second_edit': second})


def test_persisted_destination_with_different_live_definition_refuses(world):
    """A destination persisted in roles.json is compared with its live entry too."""
    fx, api = world
    register_unit(fx)
    original, alias, proposed = destination(fx, api)
    prior = source(fx)
    roles = json.loads(prior['roles_text'])
    roles[alias] = copy.deepcopy(proposed)
    graph = yaml.safe_load(prior['graph_yaml'])
    graph['description'] = 'target using a persisted destination alias'
    for node, role in fb.agent_refs(graph):
        if role.endswith('__maker'):
            node['agent_config'] = alias
    (fx.gen / 'gen_vt.roles.json').write_text(json.dumps(roles, ensure_ascii=False, indent=2))
    (fx.gen / 'gen_vt.yaml').write_text(yaml.safe_dump(graph, sort_keys=False))
    assert 'error' not in pr.reload_generated_pipeline(fx.sf, fx.registry, 'gen_vt')
    live = copy.deepcopy(fx.sf.agent_registry.get(alias).to_dict())
    fx.sf.register_agent_config_from_dict(
        alias, {**live['config'], 'model': live['model'], 'tools': live['tools'],
                'temperature': True})
    fx.start_run('Retained', 'gen_vt'); fx.pause_run('Retained')
    old = claim(fx, 'Retained')
    assert old['name'] == alias and old['config']['temperature'] is True
    assert healthy(fx) == [f'live role {alias}']
    current = api.current('gen_vt')
    before = snapshot(fx)
    mcp = tools()['restore_pipeline_version']('gen_vt', original['version_id'],
                                              current['unit_identity'])
    assert mcp.get('error') and f'live role {alias}' in mcp['error'], mcp
    with pytest.raises(c.NativeRefused, match=f'live role {alias}'):
        NativeVersions(fx.sf, fx.registry, fx.gen).restore(
            'gen_vt', original['version_id'], expected_current=current['unit_identity'])
    with pytest.raises(fb.BundleRefused, match=f'live role {alias}'):
        fb.adopt_bundle(fx.sf, fx.registry, 'gen_vt', original['physical_name'],
                        expected_current=current['unit_identity'], config_dir=fx.gen)
    edit = call(fx, 'edit_template', role='maker', template='EDIT\n')
    assert edit.get('error') and f'live role {alias}' in edit['error'] and edit['changed'] is False
    assert snapshot(fx) == before and api.current('gen_vt') == current
    assert fb.canonical(claim(fx, 'Retained')) == fb.canonical(old)
    evidence('all-served-roles.jsonl', {
        'test': 'persisted_destination', 'alias': alias, 'mcp_restore': mcp,
        'editor': edit, 'old_claim': old, 'snapshot_unchanged': True})
