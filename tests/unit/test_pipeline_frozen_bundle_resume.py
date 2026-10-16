"""Existing runs of a recorded whole bundle: supported host resume/advance/claim
after a real boot, a rolled-back boot and an ordinary manifest drift.

Tracked form of the independent observer (fixture ``test_before_resume.py``,
sha256 cd2e0d21...): actual AItelierSkillFlow, the production ToolLoader factory
and the paired singleton DB/SkillFlow/ConfigRegistry of ``api.dependencies``,
normal ``start_run`` isolation, the real boot scan in production order. Only
the base test's ``_runtime`` constructor is replaced; no product method is
patched. No provider/model body runs: a granted claim is released unexecuted.
"""
import json

import pytest
import yaml
from skillflow import SkillFlowError

from core import pipeline_frozen_bundle as fb, pipeline_registry as pr, run_isolation
from core.config_registry import ConfigRegistry
from core.db_manager import DBManager
from core.skillflow_host import AItelierSkillFlow
import test_pipeline_frozen_bundle as base
from test_pipeline_frozen_bundle_boundary import _FailSibling, _failing_sibling
from test_pipeline_frozen_bundle_served import WHOLE, _seal_from_toy

CONTEXT = [('gen_toy', 'task.md', 'owned task'), ('other_a', 'a.md', 'owned a'),
           ('other_b', 'b.md', 'owned b')]


def _plain_generated(gen):
    """An ordinary generated config (no descriptor, not sealed)."""
    doc = yaml.safe_load(base.TOY)
    doc['name'] = 'gen_plain'
    for step in doc['steps']:
        step['agent_config'] = 'gen_plain__' + step['agent_config']
        for c in step.get('context', []):
            if c['source'].get('config') == 'toy':
                c['source']['config'] = 'gen_plain'
    (gen / 'gen_plain.yaml').write_text(yaml.safe_dump(doc, sort_keys=False))
    (gen / 'gen_plain.roles.json').write_text(json.dumps({
        'gen_plain__maker': {'model': 'flash', 'tools': ['read_file'], 'system_prompt': 'PLAIN'},
        'gen_plain__reviewer': {'model': 'pro', 'tools': ['read_file'], 'system_prompt': 'R'}}))


def setup(tmp_path, monkeypatch, mode, extras=False):
    from api import dependencies as deps
    monkeypatch.setenv('AITELIER_HOME', str(tmp_path / 'home'))
    db = DBManager(str(tmp_path / 'host.db'))
    monkeypatch.setattr(deps, 'db_instance', db)
    monkeypatch.setattr(deps, '_tool_loader_instance', None)
    loader = deps.get_tool_loader()

    def host_runtime(root, mp):
        gen = root / 'gen'
        gen.mkdir()
        mp.setenv('AITELIER_GENERATED_CONFIGS_DIR', str(gen))
        host = AItelierSkillFlow(str(root / 'sf.db'), tool_loader=loader,
                                 workspace_base=str(root / 'ws'), projects_base=str(root / 'projects'))
        registry = ConfigRegistry()
        mp.setattr(deps, '_skillflow_instance', host)
        mp.setattr(deps, '_config_registry_instance', registry)
        return host, registry, gen
    # Only replace the test fixture's constructor, never a product method.
    monkeypatch.setattr(base, '_runtime', host_runtime)
    sf, reg, gen, _, rid = base._target(tmp_path, monkeypatch)
    rec = run_isolation.record(db, rid)
    assert rec is not None and rec['mode'] == 'none', rec  # real start_run declaration
    more = {}
    if extras:
        # Before adoption, so it pins the OLD graph; left running, never paused.
        more['running'] = sf.create_run('gen_toy', project_id='old-running')
        sf.start_run(more['running'])
        _plain_generated(gen)
    sealed = _seal_from_toy(sf, reg, tmp_path, gen, WHOLE, 'before_resume')
    if extras:
        # A paused run of an ordinary non-generated config ('toy' now declares
        # repo_mode none, so normal start_run isolation owns it repo-less).
        more['toy'] = sf.create_run('toy', project_id='plain-toy')
        sf.start_run(more['toy'])
        sf.pause_run(more['toy'])
    fb.adopt_bundle(sf, reg, 'gen_toy', sealed['physical_name'], config_dir=gen,
                    expected_current=fb.unit_identity(fb.read_unit(gen, 'gen_toy')))
    pin = sf.get_run(rid)['graph_version']
    if mode == 'rollback':
        _failing_sibling(gen)
    sf._conn.close()
    boot = AItelierSkillFlow(str(tmp_path / 'sf.db'), tool_loader=loader,
                             workspace_base=str(tmp_path / 'ws'), projects_base=str(tmp_path / 'projects'))
    registry = (_FailSibling if mode == 'rollback' else ConfigRegistry).build(boot)
    monkeypatch.setattr(deps, '_skillflow_instance', boot)
    monkeypatch.setattr(deps, '_config_registry_instance', registry)
    loaded = pr.load_generated_configs(boot, registry)
    if mode == 'drift':
        registry.register_one(boot, 'gen_toy')
    # Owned ordinary text context; no context tool/provider body is invoked.
    for project in ['old-paused', 'old-running', 'plain-toy', 'plain-gen']:
        for cfg, file, text in CONTEXT + [('toy', 'task.md', 'owned task'),
                                          ('gen_plain', 'task.md', 'owned task')]:
            path = boot._workspace.get_config_path(project, cfg) / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    assert deps.get_skillflow() is boot and deps.get_config_registry() is registry
    assert boot.get_run(rid)['status'] == 'paused' and boot.get_run(rid)['graph_version'] == pin
    return boot, registry, gen, rid, loaded, rec, more


def exercise(tmp_path, monkeypatch, mode):
    host, registry, gen, rid, loaded, rec, _ = setup(tmp_path, monkeypatch, mode)
    desc = fb.read_descriptor(gen, 'gen_toy')
    whole = fb.whole_state(host, registry, gen, 'gen_toy', desc['new'])
    blocked = fb.launch_block('gen_toy', gen, sf=host)  # real paired singleton registry
    new_refusal = None
    if mode != 'healthy':
        try:
            host.create_run('gen_toy', project_id='own-new-launch')
        except SkillFlowError as e:
            new_refusal = str(e)
        assert new_refusal and blocked
    before = host.get_run(rid)
    held = snapshot(host, gen, rid)
    error = None
    advanced = None
    claimed = None
    try:
        host.resume_run(rid)        # the supported public host API
        advanced = host.advance_run(rid)
        claimed = host.claim_next_step(rid)
    except Exception as exc:
        error = {'type': type(exc).__name__, 'message': str(exc)}
    after = host.get_run(rid)
    role = host.agent_registry.get('gen_toy__maker')
    data = {'case': mode, 'run_id': rid, 'loaded': loaded, 'descriptor_state': desc['state'],
            'whole_state': whole, 'registry_is_actual_pair': True, 'normal_isolation_record': rec,
            'launch_block': blocked, 'actual_new_launch_refusal': new_refusal,
            'before_status': before['status'], 'after_status': after['status'],
            'pin_before': before['graph_version'], 'pin_after': after['graph_version'],
            'advance_return': advanced, 'exception': error, 'claimed': claimed is not None,
            'claimed_step': claimed.step_id if claimed else None,
            'claim_token': str(claimed.token) if claimed else None,
            'claimed_agent_config': claimed.inputs.get('_agent_config') if claimed else None,
            'live_old_role': role.to_dict() if role else None,
            'manifest': fb.manifest_hints(registry.get('gen_toy')),
            'untouched': snapshot(host, gen, rid) == held}
    if claimed:
        host.release_claim(claimed.token, 'owned Source regression completed')
    host._conn.close()
    return data


def snapshot(host, gen, rid):
    """Everything a refusal must leave alone: the run row (status, pin,
    updated_at, current node), its step rows, claims, operations and trace,
    and the recorded unit's descriptor/graph/roles bytes."""
    with host._lock:
        run = dict(host._conn.execute('SELECT * FROM skillflow_runs WHERE id=?', (rid,)).fetchone())
        steps = [dict(r) for r in host._conn.execute(
            'SELECT * FROM skillflow_steps WHERE run_id=? ORDER BY id', (rid,))]
        ops = host._conn.execute('SELECT count(*) FROM skillflow_active_ops WHERE run_id=?',
                                 (rid,)).fetchone()[0]
    files = {p.name: p.read_bytes() for p in sorted(gen.glob('gen_toy.*'))}
    return {'run': run, 'steps': steps, 'ops': ops, 'trace': len(host.get_trace(rid)),
            'files': files}


def drive(host, rid, *, resume=True):
    """resume (optional) -> advance -> claim through the host; claim released."""
    if resume:
        host.resume_run(rid)
    advanced = host.advance_run(rid)
    claimed = host.claim_next_step(rid)
    assert claimed is not None and claimed.step_id == 'make', (advanced, claimed)
    cfg = claimed.inputs.get('_agent_config')
    host.release_claim(claimed.token, 'owned Source regression completed')
    return advanced, cfg


def refused(call, rid):
    with pytest.raises(SkillFlowError) as info:
        call(rid)
    return str(info.value)


# ── The independent observer's cases (same substantive assertions) ───────────

def test_healthy_whole_unit_resumes_and_claims_original_prompt(tmp_path, monkeypatch, record_property):
    x = exercise(tmp_path, monkeypatch, 'healthy')
    record_property('observation', json.dumps(x, default=str))
    assert x['whole_state'] == [] and x['exception'] is None, x
    assert x['claimed'] and x['claimed_step'] == 'make', x
    assert x['claimed_agent_config']['config']['system_prompt'] == 'OLD MAKER', x
    assert x['after_status'] == 'running' and x['pin_after'] == x['pin_before']


@pytest.mark.parametrize('mode', ['rollback', 'drift'])
def test_existing_paused_run_refuses_resume_until_whole_unit_reconciled(
        tmp_path, monkeypatch, record_property, mode):
    x = exercise(tmp_path, monkeypatch, mode)
    record_property('observation', json.dumps(x, default=str))
    assert x['whole_state'], x
    assert x['after_status'] == 'paused' and not x['claimed'], x
    # Refused at resume itself, for the same reason a new run is refused, and
    # before any effect: row, pin, steps, trace and recorded unit unchanged.
    assert x['exception']['type'] == 'SkillFlowError', x
    assert x['launch_block'] in x['exception']['message'], x
    assert x['advance_return'] is None and x['untouched'], x
    assert x['pin_after'] == x['pin_before'], x


# ── Ordinary advance/claim callers that never call resume ─────────────────────

@pytest.mark.parametrize('mode', ['rollback', 'drift'])
def test_direct_advance_and_claim_cannot_bypass_the_check(tmp_path, monkeypatch, record_property, mode):
    host, registry, gen, rid, _, _, more = setup(tmp_path, monkeypatch, mode, extras=True)
    running = more['running']
    assert host.get_run(running)['status'] == 'running'
    held = {r: snapshot(host, gen, r) for r in (rid, running)}
    role = host.agent_registry.get('gen_toy__maker')
    seen = {}
    for name, run in [('paused', rid), ('running', running)]:
        for call in ('advance_run', 'claim_next_step', 'approve_checkpoint', 'reactivate_run'):
            seen[f'{name}.{call}'] = refused(getattr(host, call), run)
    seen['paused.reject_checkpoint'] = refused(
        lambda r: host.reject_checkpoint(r, 'make', 'owned feedback'), rid)
    record_property('observation', json.dumps(seen))
    block = fb.launch_block('gen_toy', gen, sf=host)
    assert block and all(block in message for message in seen.values()), seen
    assert {r: snapshot(host, gen, r) for r in (rid, running)} == held
    assert host.get_run(rid)['status'] == 'paused' and host.get_run(running)['status'] == 'running'
    after = host.agent_registry.get('gen_toy__maker')
    assert (after.to_dict() if after else None) == (role.to_dict() if role else None)
    host._conn.close()


def test_healthy_running_run_advances_and_claims_directly(tmp_path, monkeypatch):
    host, _, _, rid, _, _, more = setup(tmp_path, monkeypatch, 'healthy', extras=True)
    pin = host.get_run(more['running'])['graph_version']
    _, cfg = drive(host, more['running'], resume=False)
    assert cfg['config']['system_prompt'] == 'OLD MAKER'
    assert host.get_run(more['running'])['graph_version'] == pin
    assert host.get_run(rid)['status'] == 'paused'
    host._conn.close()


# ── Unrelated configs, recovery and registry pairing ─────────────────────────

@pytest.mark.parametrize('mode', ['healthy', 'rollback', 'drift'])
def test_unrelated_configs_stay_admitted(tmp_path, monkeypatch, mode):
    host, _, gen, rid, loaded, _, more = setup(tmp_path, monkeypatch, mode, extras=True)
    drive(host, more['toy'])                         # non-generated config
    if mode != 'rollback':                           # a rolled-back scan serves no gen_*
        assert 'gen_plain' in loaded
        plain = host.create_run('gen_plain', project_id='plain-gen')
        host.start_run(plain)
        host.pause_run(plain)
        _, cfg = drive(host, plain)                  # ordinary generated config
        assert cfg['config']['system_prompt'] == 'PLAIN'
    assert host.get_run(rid)['status'] == 'paused'
    if mode != 'healthy':
        assert fb.launch_block('gen_toy', gen, sf=host)
    host._conn.close()


@pytest.mark.parametrize('mode', ['rollback', 'drift'])
def test_refused_run_resumes_on_its_old_pin_once_served_whole(tmp_path, monkeypatch, mode):
    host, registry, gen, rid, _, _, _ = setup(tmp_path, monkeypatch, mode)
    pin = host.get_run(rid)['graph_version']
    refused(host.resume_run, rid)
    if mode == 'rollback':
        fb.reconcile_adoption(host, registry, 'gen_toy', to='new', config_dir=gen)
        assert fb.read_descriptor(gen, 'gen_toy')['state'] == 'reconciled_new'
    else:
        assert pr.reload_generated_pipeline(host, registry, 'gen_toy') == {'config_name': 'gen_toy'}
    assert fb.launch_block('gen_toy', gen, sf=host) is None
    advanced, cfg = drive(host, rid)
    assert advanced == 'make' and cfg['config']['system_prompt'] == 'OLD MAKER'
    assert host.get_run(rid)['graph_version'] == pin      # no implicit repin
    host._conn.close()


def test_missing_registry_pairing_fails_closed(tmp_path, monkeypatch):
    from api import dependencies as deps
    host, registry, gen, rid, _, _, more = setup(tmp_path, monkeypatch, 'healthy', extras=True)
    # The singleton's registry half is missing (as before get_skillflow builds
    # it) and no boot scan paired one: the manifest cannot be verified.
    monkeypatch.setattr(deps, '_config_registry_instance', None)
    monkeypatch.delattr(host, '_frozen_bundle_registry')
    held = snapshot(host, gen, rid)
    message = refused(host.resume_run, rid)
    assert 'no ConfigRegistry is paired' in message
    assert snapshot(host, gen, rid) == held
    drive(host, more['toy'])                      # needs no manifest: unaffected
    fb.pair_registry(host, registry)              # what the boot scan records
    _, cfg = drive(host, rid)
    assert cfg['config']['system_prompt'] == 'OLD MAKER'
    host._conn.close()


def test_host_owns_every_existing_run_ingress(record_property):
    import core.skillflow_host as sh
    names = ['create_run', 'resume_run', 'approve_checkpoint', 'reject_checkpoint',
             'reactivate_run', 'advance_run', 'claim_next_step']
    codes = {n: getattr(AItelierSkillFlow, n).__code__.co_filename for n in names}
    record_property('co_filename', json.dumps(codes))
    assert set(codes.values()) == {sh.__file__}, codes
