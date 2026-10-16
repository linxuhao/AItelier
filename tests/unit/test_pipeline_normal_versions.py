"""Actual ordinary frontdoors + genuine SDK lifecycle; no model/provider executes."""
import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from skillflow.core import StepResult, graph_digest

from contracts.rsi.pipeline_versioning.test_contract import load_vectors, literal
from contracts.rsi.pipeline_versioning.test_native_provider import NativeFixture
from core import pipeline_frozen_bundle as fb, pipeline_registry as pr
from core.pipeline_versions import PipelineVersions
from core.config_registry import ConfigRegistry
from core.skillflow_host import AItelierSkillFlow


def observe(kind, value):
    import os
    out = Path('/evidence')
    if out.is_dir():
        with (out / 'normal-editor-observations.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({'case': os.getenv('PYTEST_CURRENT_TEST'), 'kind': kind,
                                 'value': value}, ensure_ascii=False, default=str) + '\n')


def snapshot(fx):
    value = fx.snapshot()
    observe('full_snapshot', json.loads(value))
    return value


def config_edit(args):
    out = meta()._tool_config_edit(args)
    observe('actual_config_edit', {'input': args, 'result': out})
    return out


def tools():
    from api import mcp_router
    got = {}
    def tool(name, kind, description):
        def collect(fn):
            got[name] = fn
            return fn
        return collect
    mcp_router._register_edit_tools(tool)
    mcp_router._register_read_tools(tool)
    return got


def meta():
    from core.meta_agent import MetaAgent
    return MetaAgent(MagicMock(), MagicMock(), owner_email='test@local')


@pytest.fixture
def world(tmp_path):
    fx = NativeFixture(tmp_path)
    v = load_vectors()
    tool = v['tools']['T_A'] if 'T_A' in v['tools'] else next(iter(v['tools'].values()))
    fx.install_tool(tool['name'], tool['files'])
    fx.register('gen_vt', literal(v['units']['U_A']), None)
    api = PipelineVersions(fx.sf, fx.registry, fx.gen)
    yield fx, api
    fx.close()


def claim(fx, name):
    rid = fx.runs[name]
    if fx.sf.get_run(rid)['status'] == 'paused':
        fx.sf.resume_run(rid)
    fx.sf.advance_run(rid)
    got = fx.sf.claim_next_step(rid)
    assert got is not None
    cfg = copy.deepcopy(got.inputs['_agent_config'])
    fx.sf.release_claim(got.token, 'normal version fixture observation, unexecuted')
    resolved = cfg['config'].get('system_prompt')
    if cfg['config'].get('template'):
        resolved = fb.safe_file(fb.ROOT / 'templates', cfg['config']['template']).read_bytes().decode('utf-8')
    observe('actual_unexecuted_claim', {'run': fx.sf.get_run(rid), 'agent_config': cfg,
                                       'resolved_prompt_utf8': resolved})
    return cfg


def call(fx, which, **extra):
    t = tools()
    args = {'config': 'gen_vt', **extra}
    out = t[which](**args)
    observe('actual_frontdoor', {'tool': which, 'input': args, 'result': out})
    return out


def ok(out):
    assert 'error' not in out, out
    return out


def active(fx):
    return next(r for _, r in fb.agent_refs(fx.sf._graphs['gen_vt'].to_dict()))


def source(fx):
    return fb.read_unit(fx.gen, 'gen_vt')


def assert_history(fx):
    for name, rid in fx.runs.items():
        run = fx.sf.get_run(rid)
        row = fx.sf._conn.execute(
            'SELECT digest,yaml_text FROM skillflow_graph_versions WHERE name=? AND version=?',
            (run['graph_name'],run['graph_version'])).fetchone()
        assert row is not None and row['digest'] == run['graph_digest']
        resolved = fx.sf._get_resolver_for_run(rid).graph.to_dict()
        assert graph_digest(resolved) == run['graph_digest']
        observe('sdk_history_and_resolver', {'name':name,'run':run,'history':dict(row),'resolved':resolved})


def test_four_editors_roundtrip_and_real_old_new_claims(world):
    fx, api = world
    fx.start_run('A', 'gen_vt'); fx.pause_run('A')
    fx.start_run('Done', 'gen_vt'); fx.complete_run('Done')
    initial = api._capture_live('gen_vt')[1]
    pins = {n: fx.sf.get_run(r) for n, r in fx.runs.items()}
    original_roles = {n: copy.deepcopy(fx.sf.agent_registry.get(n).to_dict()) for n in fx.sf.agent_registry.list_names()}
    assert api.list_versions() == []  # automatic catalog; no manual capture
    one = ok(call(fx, 'edit_template', role='maker', template='新版本\nλ\n'))
    assert one['changed'] and len(api.list_versions()) == 2
    v0 = one['previous_version_id']
    fx.start_run('B', 'gen_vt'); fx.pause_run('B')
    two = ok(call(fx, 'edit_role', role='maker', model='pro', temperature=0.7,
                  thinking={'enable': False}))
    assert two['changed'] and two['value']['temperature'] == 0.7
    fx.start_run('C', 'gen_vt')
    graph = yaml.safe_load(source(fx)['graph_yaml']); graph['description'] = 'ordinary graph edit'
    three = ok(call(fx, 'edit_pipeline', graph_yaml=yaml.safe_dump(graph)))
    assert three['changed']
    role = active(fx)
    old_source = source(fx)['roles_text']
    new_roles = json.loads(old_source); new_roles[role]['system_prompt'] = 'fourth'
    four = ok(config_edit({'config_name': 'gen_vt', 'file': 'roles',
              'old_str': old_source, 'new_str': json.dumps(new_roles, ensure_ascii=False)}))
    assert four['changed']
    five = ok(config_edit({'config_name': 'gen_vt', 'old_str': 'ordinary graph edit',
                                      'new_str': 'fifth graph'}))
    assert five['changed'] and len(api.list_versions()) == 6
    for name, cfg in original_roles.items():
        assert fx.sf.agent_registry.get(name).to_dict() == cfg
    assert claim(fx, 'A')['config']['system_prompt'] == 'OLD MAKER\n'
    assert claim(fx, 'B')['config']['system_prompt'] == '新版本\nλ\n'
    c = claim(fx, 'C'); assert c['model'] == 'pro' and c['config']['temperature'] == 0.7
    fx.start_run('D', 'gen_vt'); assert claim(fx, 'D')['config']['system_prompt'] == 'fourth'
    assert_history(fx)
    before_restore_roles = {n: copy.deepcopy(fx.sf.agent_registry.get(n).to_dict()) for n in fx.sf.agent_registry.list_names()}
    restore = ok(tools()['restore_pipeline_version']('gen_vt', v0, api.current('gen_vt')['unit_identity']))
    assert restore['changed'] and api._capture_live('gen_vt')[1] == initial
    seals = api.list_versions(); api.capture('gen_vt'); assert api.list_versions() == seals
    assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', fb.recorded_unit(fx.gen, 'gen_vt')) == []
    for n, cfg in before_restore_roles.items(): assert fx.sf.agent_registry.get(n).to_dict() == cfg
    for n, old in pins.items():
        row = fx.sf.get_run(fx.runs[n]); assert row['graph_version'] == old['graph_version'] and row['graph_digest'] == old['graph_digest']
    # Both warm and freshly instantiated SDK resolver/AgentRegistry paths.
    from api import dependencies as deps
    fx.sf._conn.close()
    fx.sf = AItelierSkillFlow(str(fx.root / 'sf.db'), tool_loader=fx.loader,
                             workspace_base=str(fx.root / 'ws'), projects_base=str(fx.root / 'projects'))
    fx.registry = ConfigRegistry.build(fx.sf)
    fx.mp.setattr(deps, '_skillflow_instance', fx.sf)
    fx.mp.setattr(deps, '_config_registry_instance', fx.registry)
    assert 'gen_vt' in pr.load_generated_configs(fx.sf, fx.registry)
    assert claim(fx, 'A')['config']['system_prompt'] == 'OLD MAKER\n'
    assert claim(fx, 'B')['config']['system_prompt'] == '新版本\nλ\n'
    assert claim(fx, 'C')['model'] == 'pro'
    assert claim(fx, 'D')['config']['system_prompt'] == 'fourth'
    observe('roundtrip_versions', {'versions': api.list_versions(), 'initial': initial, 'restored': api._capture_live('gen_vt')[1]})
    assert_history(fx)
    completed = fx.sf.get_run(fx.runs['Done'])
    assert completed['status'] == 'completed'
    historical = fx.sf._get_resolver_for_run(fx.runs['Done']).graph
    assert graph_digest(historical.to_dict()) == completed['graph_digest']
    observe('completed_history_no_claim', {'run': completed, 'resolved_graph': historical.to_dict()})
    fx.start_run('Future', 'gen_vt')
    assert claim(fx, 'Future')['config']['system_prompt'] == 'OLD MAKER\n'


@pytest.mark.parametrize('adopted', [False, True])
def test_every_editor_noop_has_zero_effects(world, adopted):
    fx, api = world
    if adopted: ok(call(fx, 'edit_template', role='maker', template='active'))
    prompt = api._capture_live('gen_vt')[1]['roles']['maker']['prompt']
    current = api._capture_live('gen_vt')[1]['roles']['maker']
    cases = [lambda: call(fx, 'edit_template', role='maker', template=prompt),
             lambda: call(fx, 'edit_role', role='maker', model=current['model']),
             lambda: call(fx, 'edit_pipeline', graph_yaml=source(fx)['graph_yaml']),
             lambda: config_edit({'config_name':'gen_vt', 'old_str':'name: gen_vt', 'new_str':'name: gen_vt'}),
             lambda: config_edit({'config_name':'gen_vt', 'file':'roles', 'old_str':'{', 'new_str':'{\n',})]
    # Last case uses exact unique text to make a representation-only JSON change.
    cases[-1] = lambda: config_edit({'config_name':'gen_vt', 'file':'roles',
        'old_str': source(fx)['roles_text'], 'new_str': json.dumps(json.loads(source(fx)['roles_text']), indent=4)})
    for run in cases:
        before = snapshot(fx); out = ok(run()); assert out['changed'] is False
        assert snapshot(fx) == before
    if not adopted: assert api.list_versions() == []


def test_preflight_refusals_preserve_full_snapshot(world):
    fx, api = world
    first = ok(call(fx, 'edit_template', role='maker', template='active'))
    raw = source(fx)
    unsupported = json.loads(raw['roles_text']); unsupported[active(fx)]['unsupported_null'] = None
    cases = [lambda: call(fx, 'edit_role', role='gen_vt__maker', model='pro'),
             lambda: call(fx, 'edit_template', role='unused', template='text'),
             lambda: call(fx, 'edit_pipeline', graph_yaml='steps: ['),
             lambda: call(fx, 'edit_role', role='maker', model='pro', expected_current='0'*64),
             lambda: call(fx, 'edit_role', role='maker', tools=['missing_tool_for_normal_edit']),
             lambda: call(fx, 'edit_role', role='maker', temperature=float('nan')),
             lambda: config_edit({'config_name':'gen_vt','file':'roles','old_str':'OLD MAKER','new_str':'historical corruption'}),
             lambda: config_edit({'config_name':'gen_vt','file':'roles','old_str':raw['roles_text'],'new_str':'['}),
             lambda: config_edit({'config_name':'gen_vt','file':'templates/shared.md','old_str':'x','new_str':'y'}),
             lambda: call(fx, 'edit_pipeline', graph_yaml=raw['graph_yaml']+'\nx-aitelier:\n  repo_mode: code\n'),
             lambda: tools()['edit_template']('dpe_default', 'maker', 'text'),
             lambda: tools()['edit_template'](api.read(first['version_id'])['native']['physical_name'], 'maker', 'text')]
    cases.append(lambda: config_edit({'config_name':'gen_vt','file':'roles',
        'old_str':raw['roles_text'],'new_str':json.dumps(unsupported)}))
    for run in cases:
        before = snapshot(fx); result = run(); assert result.get('error'), result
        assert 'prepared_seals' not in result and snapshot(fx) == before, result


def test_recorded_provider_drift_refuses_even_noop(world):
    fx, api = world
    fx.define_capability('normal_cap', [], 'before')
    g = yaml.safe_load(source(fx)['graph_yaml']); g['capabilities'] = ['normal_cap']
    ok(call(fx, 'edit_pipeline', graph_yaml=yaml.safe_dump(g)))
    fx.define_capability('normal_cap', [], 'after')
    before = snapshot(fx)
    for run in [lambda: call(fx,'edit_template',role='maker',template='OLD MAKER\n'),
                lambda: call(fx,'edit_role',role='maker',model='pro'),
                lambda: call(fx,'edit_pipeline',graph_yaml=source(fx)['graph_yaml']),
                lambda: config_edit({'config_name':'gen_vt','old_str':'name: gen_vt','new_str':'name: gen_vt'})]:
        out = run(); assert out.get('error') and 'changed since capture' in out['error'], out
        assert snapshot(fx) == before


def test_registration_failure_retains_prepared_seals_and_reconciles(world, monkeypatch):
    fx, api = world
    fx.start_run('Old', 'gen_vt'); fx.pause_run('Old')
    old = source(fx)
    original = fx.registry.register_one
    def fail(*a, **kw): raise RuntimeError('owned publication registration failure')
    monkeypatch.setattr(fx.registry, 'register_one', fail)
    result = call(fx, 'edit_template', role='maker', template='prepared')
    assert result['publication_failed'] and len(result['prepared_seals']) == 2, result
    assert all(p['complete'] for p in result['prepared_seals'])
    assert source(fx) == old
    monkeypatch.setattr(fx.registry, 'register_one', original)
    desc = fb.read_descriptor(fx.gen, 'gen_vt'); assert desc['state'] == 'rolled_back'
    assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', desc['old']) == []
    assert claim(fx, 'Old')['config']['system_prompt'] == 'OLD MAKER\n'
    ok(call(fx, 'edit_template', role='maker', template='prepared'))
    fx.start_run('New', 'gen_vt'); assert claim(fx, 'New')['config']['system_prompt'] == 'prepared'


@pytest.mark.parametrize('boundary', ['target_roles', 'settled_descriptor'])
def test_write_failure_pending_gate_and_recovery(world, monkeypatch, boundary):
    fx, api = world
    fx.start_run('Old', 'gen_vt'); fx.pause_run('Old')
    original = fb.fsync_write
    tripped = []
    def fail(path, text):
        hit = (path.name == 'gen_vt.roles.json' if boundary == 'target_roles' else
               path.name == 'gen_vt.adoption.json' and json.loads(text).get('state') == 'adopted')
        if hit:
            tripped.append(str(path)); raise OSError('owned write boundary failure')
        return original(path, text)
    monkeypatch.setattr(fb, 'fsync_write', fail)
    result = call(fx, 'edit_template', role='maker', template='after failure')
    assert result.get('publication_failed') and tripped, result
    monkeypatch.setattr(fb, 'fsync_write', original)
    desc = fb.read_descriptor(fx.gen, 'gen_vt')
    if desc['state'] == 'pending':
        assert fb.launch_block('gen_vt', fx.gen, sf=fx.sf)
        before = snapshot(fx)
        assert call(fx,'edit_role',role='maker',model='pro').get('error')
        assert snapshot(fx) == before
        fb.reconcile_adoption(fx.sf, fx.registry, 'gen_vt', to='new', config_dir=fx.gen)
    else:
        assert desc['state'] == 'rolled_back'
        assert fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', desc['old']) == []
        ok(call(fx,'edit_template',role='maker',template='after failure'))
    assert claim(fx, 'Old')['config']['system_prompt'] == 'OLD MAKER\n'
    fx.start_run('New','gen_vt'); assert claim(fx,'New')['config']['system_prompt'] == 'after failure'


def test_file_prompt_bytes_freeze_without_shared_source_write(world, tmp_path):
    fx, api = world
    template_root = tmp_path / 'templates'; template_root.mkdir()
    template = template_root / 'shared.md'
    raw = '原始共享 λ\n\n'.encode('utf-8'); template.write_bytes(raw)
    fx.mp.setattr(fb, 'ROOT', tmp_path)
    unit = literal(load_vectors()['units']['U_A'])
    unit['roles']['$self__maker'] = {'_frozen': True, 'model': 'flash',
        'tools': ['fx_probe'], 'template': 'shared.md', 'system_prompt': 'ignored inline', 'temperature': 0.4}
    fx.register('gen_file', unit, None)
    fx.register('gen_other', unit, None)
    fx.start_run('FileOld','gen_file'); fx.pause_run('FileOld')
    old_cfg = copy.deepcopy(fx.sf.agent_registry.get('gen_file__maker').to_dict())
    before = api._capture_live('gen_file')[1]
    assert before['roles']['maker']['prompt'].encode('utf-8') == raw
    out = ok(tools()['edit_template']('gen_file', 'maker', '新的内联\n'))
    assert template.read_bytes() == raw
    newrole = next(r for _,r in fb.agent_refs(fx.sf._graphs['gen_file'].to_dict()))
    assert 'template' not in fx.sf.agent_registry.get(newrole).config
    assert fx.sf.agent_registry.get('gen_file__maker').to_dict() == old_cfg
    cfg = claim(fx,'FileOld')
    assert cfg['config']['template'] == 'shared.md' and template.read_bytes() == raw
    ok(tools()['restore_pipeline_version']('gen_file',out['previous_version_id'],api.current('gen_file')['unit_identity']))
    assert api._capture_live('gen_file')[1] == before
    fx.start_run('FileFuture','gen_file')
    assert claim(fx,'FileFuture')['config']['system_prompt'].encode('utf-8') == raw
    assert api._capture_live('gen_other')[1] == before


def test_concurrent_same_cas_has_one_writer_and_active_reads(world):
    from concurrent.futures import ThreadPoolExecutor
    fx, api = world
    cas = api.current('gen_vt')['unit_identity']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda text: call(fx, 'edit_template', role='maker',
                         template=text, expected_current=cas), ['writer one','writer two']))
    assert len([r for r in results if r.get('changed')]) == 1, results
    assert len([r for r in results if r.get('code') == 'CONFLICT']) == 1, results
    assert len(api.list_versions()) == 2
    read = tools()['get_template']('gen_vt','maker')
    assert read['template'] in ('writer one','writer two')
    role = tools()['get_role']('gen_vt','maker')
    assert role['role'] == active(fx)
    assert meta()._tool_config_read({'config_name':'gen_vt'})['unit_identity'] == api.current('gen_vt')['unit_identity']
    ok(call(fx,'edit_role',role='maker',tools=[],thinking={'enable':True,'effort':'low'}))
    fx.start_run('ToolEdit','gen_vt'); cfg=claim(fx,'ToolEdit')
    assert cfg['tools'] == [] and cfg['config']['thinking']['effort'] == 'low'


@pytest.mark.parametrize('damage', ['seal', 'pending', 'mixed'])
def test_damaged_target_or_catalog_refuses_before_every_effect(world, damage):
    fx, api = world
    out = ok(call(fx,'edit_template',role='maker',template='active'))
    if damage == 'seal':
        physical = api.read(out['version_id'])['native']['physical_name']
        path = fx.gen / (physical + '.roles.json'); path.write_text(path.read_text()+' ')
    elif damage == 'pending':
        desc=fb.read_descriptor(fx.gen,'gen_vt'); desc.pop('digest'); desc['state']='pending'
        fb.write_descriptor(fx.gen,'gen_vt',desc)
    else:
        path=fx.gen/'gen_vt.roles.json'; path.write_text(path.read_text()+' ')
    for run in [lambda: call(fx,'edit_template',role='maker',template='active'),
                lambda: call(fx,'edit_role',role='maker',model='pro'),
                lambda: call(fx,'edit_pipeline',graph_yaml=source(fx)['graph_yaml']),
                lambda: config_edit({'config_name':'gen_vt','old_str':'name: gen_vt','new_str':'name: gen_vt'})]:
        before=snapshot(fx); result=run()
        assert result.get('error') and 'prepared_seals' not in result, result
        assert snapshot(fx) == before
