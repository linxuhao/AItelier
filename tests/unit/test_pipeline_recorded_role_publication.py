"""One absent-or-exactly-equal rule at the common publication boundary.

Every publication of a recorded whole unit (sealed bundle, adoption-recorded
target) goes through ``pipeline_registry._commit_registration``, which checks
the COMPLETE proposed role set under the registration exclusion before any
role is installed: absent names are installed, identical live entries are
reused as they are, and any different live definition refuses with nothing
changed. Reconciliation, sealed registration, reload and boot have no hidden
exception. Old-side settlement after the new side was served refuses when it
would drop roles runs admitted there resolve; the compatible repair is new-side
reconciliation followed by an ordinary logical restore, which retains every
history through a fresh boot.

Actual AItelierSkillFlow (SQLite), production ToolLoader, ConfigRegistry,
MCP editor bodies, native reconcile/register_sealed, the reload path and the
boot scan. Only owned descriptor writes or an ordinary sibling's manifest
registration fail where stated. No model or tool body runs; every claim is
released unexecuted.
"""
import copy
import json

import pytest
from skillflow.core import graph_digest

from contracts.rsi.pipeline_versioning.test_contract import literal, load_vectors
from core import pipeline_frozen_bundle as fb, pipeline_registry as pr
from core.config_registry import ConfigRegistry
from core.skillflow_host import AItelierSkillFlow
from test_pending_recovery_roles_corrected import attempt, full_state, save
from test_pipeline_all_served_roles import register_unit
from test_pipeline_frozen_bundle_boundary import _FailSibling, _failing_sibling
from test_pipeline_normal_versions import active, call, claim, ok, tools, world  # noqa: F401


def reboot(fx, registry_cls=ConfigRegistry, before_scan=None):
    """A fresh process over the same SQLite/files: production order (host,
    ConfigRegistry.build, optional host registrations, then the boot scan)."""
    from api import dependencies as deps
    fx.sf._conn.close()
    fx.sf = AItelierSkillFlow(str(fx.root / 'sf.db'), tool_loader=fx.loader,
                              workspace_base=str(fx.root / 'ws'),
                              projects_base=str(fx.root / 'projects'))
    fx.registry = registry_cls.build(fx.sf)
    fx.mp.setattr(deps, '_skillflow_instance', fx.sf)
    fx.mp.setattr(deps, '_config_registry_instance', fx.registry)
    if before_scan is not None:
        before_scan(fx.sf)
    return pr.load_generated_configs(fx.sf, fx.registry)


def effective(target, name, entry):
    """*entry* as a host registration of the same effective definition."""
    cfg = fb._expected_roles(target, {name: entry})[name]
    return {'model': cfg.model, 'tools': list(cfg.tools), **copy.deepcopy(cfg.config)}


def settled_write_failure(fx, monkeypatch, template):
    """The actual editor fails only at its final settled-descriptor write."""
    real, trips = fb.fsync_write, []

    def fail(path, text):
        if path.name == 'gen_vt.adoption.json' and json.loads(text).get('state') == 'adopted':
            trips.append(str(path)); raise OSError('owned settled descriptor write failure')
        return real(path, text)

    with monkeypatch.context() as mp:
        mp.setattr(fb, 'fsync_write', fail)
        failed = call(fx, 'edit_template', role='maker', template=template)
    assert failed.get('publication_failed') and trips, failed
    desc = fb.read_descriptor(fx.gen, 'gen_vt')
    assert desc['state'] == 'pending'
    return desc


def blocked(fx, name='gen_vt'):
    return fb.launch_block(name, fx.gen, sf=fx.sf, registry=fx.registry)


LATE = {
    'conflict-prompt': lambda d: {**d, 'system_prompt': 'HOST REVIEWER\n'},
    'conflict-typed': lambda d: {**d, 'thinking': {**d['thinking'], 'enable': 1}},
    'equal': lambda d: d,
}


@pytest.mark.parametrize('case', sorted(LATE))
def test_late_conflict_in_the_complete_role_set_installs_nothing(world, monkeypatch, case):
    """Recovery after a crash: the pending target is refused at boot, so none
    of its roles is live. A host registration then holds the LAST name of the
    proposed set; reconciliation must not install even the earlier missing one."""
    fx, api = world
    fx.start_run('Original', 'gen_vt'); fx.pause_run('Original')
    original = claim(fx, 'Original'); fx.pause_run('Original')
    desc = settled_write_failure(fx, monkeypatch, 'RECORDED PROMPT\n')
    loaded = reboot(fx)
    new_roles = json.loads(desc['new']['roles_text'])
    added = [n for n in new_roles if n not in json.loads(desc['old']['roles_text'])]
    first, last = added[0], added[-1]
    absent_before = sorted(n for n in new_roles if n not in fx.sf.agent_registry)
    fx.sf.register_agent_config_from_dict(last, LATE[case](effective('gen_vt', last, new_roles[last])))
    host_object = id(fx.sf.agent_registry.get(last))
    before = full_state(fx)
    outcome = attempt(lambda: fb.reconcile_adoption(
        fx.sf, fx.registry, 'gen_vt', to='new', config_dir=fx.gen))
    after = full_state(fx)
    value = {'case': case, 'loaded_at_boot': loaded, 'proposed_order': list(new_roles),
             'first_missing': first, 'late_host_name': last, 'absent_before': absent_before,
             'reconcile_new': outcome, 'state_before': before, 'state_after': after,
             'gate_after': blocked(fx)}
    if 'value' in outcome:
        value['original_claim'] = claim(fx, 'Original')
        fx.start_run('Future', 'gen_vt')
        value['future_claim'] = claim(fx, 'Future')
        value['whole_state'] = fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', desc['new'])
    save(f'late-conflict-{case}.json', value)

    assert 'gen_vt' not in loaded and len(added) >= 2 and first != last
    assert absent_before == sorted(new_roles)              # nothing of the unit is live
    assert id(fx.sf.agent_registry.get(last)) == host_object
    if case.startswith('conflict'):
        error = outcome.get('error')
        assert error and error['type'] == 'LiveRoleConflict', outcome
        assert f'would replace live role {last};' in error['message']
        assert after == before
        assert first not in fx.sf.agent_registry
        assert [n for n in new_roles if n in fx.sf.agent_registry] == [last]
        assert fb.read_descriptor(fx.gen, 'gen_vt') == desc and 'unreconciled' in value['gate_after']
    else:
        assert 'value' in outcome, outcome
        assert all(n in fx.sf.agent_registry for n in new_roles)
        assert value['gate_after'] is None and value['whole_state'] == []
        assert fb.canonical(value['original_claim']) == fb.canonical(original)
        assert value['future_claim']['config']['system_prompt'] == 'RECORDED PROMPT\n'


@pytest.mark.parametrize('case', ['conflict', 'equal'])
def test_sealed_registration_installs_absent_reuses_equal_refuses_different(world, case):
    fx, api = world
    phys = api.capture('gen_vt')['physical_name']
    sealed = fb.verify_sealed(fx.gen, phys)
    names = list(sealed['roles'])
    first, last = names[0], names[-1]
    host = effective(phys, last, sealed['roles'][last])
    if case == 'conflict':
        host['system_prompt'] = 'HOST SEALED NAME\n'
    fx.sf.register_agent_config_from_dict(last, host)
    host_object = id(fx.sf.agent_registry.get(last))
    before = full_state(fx)
    outcome = attempt(lambda: fb.register_sealed(fx.sf, fx.registry, phys, fx.gen))
    after = full_state(fx)
    gate = blocked(fx, phys)
    save(f'sealed-registration-{case}.json', {'physical': phys, 'host': host, 'outcome': outcome,
                                               'before': before, 'after': after, 'gate': gate})
    assert id(fx.sf.agent_registry.get(last)) == host_object
    if case == 'conflict':
        assert outcome['error']['type'] == 'LiveRoleConflict', outcome
        assert after == before and first not in fx.sf.agent_registry
        assert phys not in fx.sf._graphs and gate
    else:
        assert 'value' in outcome, outcome
        assert first in fx.sf.agent_registry and gate is None
        assert fb.whole_state(fx.sf, fx.registry, fx.gen, phys,
                              fb.recorded_unit(fx.gen, phys)) == []


@pytest.mark.parametrize('case', ['conflict', 'equal', 'equal-host-defaults'])
def test_reload_of_a_recorded_target_never_replaces_a_different_live_role(world, case):
    fx, api = world
    if case == 'equal-host-defaults':
        register_unit(fx, defaults=True)       # retained roles served with host defaults
    fx.start_run('Old', 'gen_vt'); fx.pause_run('Old')
    old = claim(fx, 'Old'); fx.pause_run('Old')
    ok(call(fx, 'edit_template', role='maker', template='ADOPTED\n'))
    alias = active(fx)
    roles = json.loads(fb.read_descriptor(fx.gen, 'gen_vt')['new']['roles_text'])
    host = effective('gen_vt', alias, roles[alias])
    if case == 'conflict':
        host['system_prompt'] = 'HOST OVERRIDE\n'
    fx.sf.register_agent_config_from_dict(alias, host)
    objects = {n: id(c) for n, c in fx.sf.agent_registry._configs.items()}
    before = full_state(fx)
    out = pr.reload_generated_pipeline(fx.sf, fx.registry, 'gen_vt')
    after = full_state(fx)
    value = {'case': case, 'alias': alias, 'host': host, 'reload': out,
             'before': before, 'after': after, 'gate': blocked(fx)}
    if case != 'conflict':
        value['old_claim'] = claim(fx, 'Old')
        fx.start_run('Future', 'gen_vt')
        value['future_claim'] = claim(fx, 'Future')
    save(f'recorded-reload-{case}.json', value)
    assert {n: id(c) for n, c in fx.sf.agent_registry._configs.items()} == objects
    if case == 'conflict':
        assert 'error' in out and f'would replace live role {alias};' in out['error'], out
        assert after == before and value['gate'] and f'live role {alias}' in value['gate']
    else:
        assert out == {'config_name': 'gen_vt'} and value['gate'] is None
        assert fb.canonical(value['old_claim']) == fb.canonical(old)
        assert value['future_claim']['config']['system_prompt'] == 'ADOPTED\n'


@pytest.mark.parametrize('case', ['conflict', 'equal'])
def test_boot_never_substitutes_a_recorded_role_over_a_different_live_one(world, case):
    fx, api = world
    fx.register('gen_other', literal(load_vectors()['units']['U_A']), None)  # ordinary sibling
    fx.start_run('Old', 'gen_vt'); fx.pause_run('Old')
    old = claim(fx, 'Old'); fx.pause_run('Old')
    ok(call(fx, 'edit_template', role='maker', template='ADOPTED\n'))
    alias = active(fx)
    desc = fb.read_descriptor(fx.gen, 'gen_vt')
    roles = json.loads(desc['new']['roles_text'])
    host = effective('gen_vt', alias, roles[alias])
    if case == 'conflict':
        host['system_prompt'] = 'HOST REGISTERED BEFORE THE SCAN\n'
    seen = {}

    def preregister(sf):                       # a host registration the scan finds
        sf.register_agent_config_from_dict(alias, host)
        seen['object'] = id(sf.agent_registry.get(alias))

    target_files = fb.read_unit(fx.gen, 'gen_vt')
    loaded = reboot(fx, before_scan=preregister)
    after_boot = fb.read_descriptor(fx.gen, 'gen_vt')
    value = {'case': case, 'alias': alias, 'loaded': loaded, 'descriptor_after_boot': after_boot,
             'live_alias': fx.sf.agent_registry.get(alias).to_dict(), 'gate': blocked(fx)}
    if case == 'conflict':
        value['resume_old'] = attempt(fx.sf.resume_run, fx.runs['Old'])
        value['reconcile_new'] = attempt(lambda: fb.reconcile_adoption(
            fx.sf, fx.registry, 'gen_vt', to='new', config_dir=fx.gen))
        value['reconcile_old'] = attempt(lambda: fb.reconcile_adoption(
            fx.sf, fx.registry, 'gen_vt', to='old', config_dir=fx.gen))
    else:
        value['old_claim'] = claim(fx, 'Old')
        fx.start_run('Future', 'gen_vt')
        value['future_claim'] = claim(fx, 'Future')
    save(f'boot-recorded-role-{case}.json', value)
    assert id(fx.sf.agent_registry.get(alias)) == seen['object']
    assert 'gen_other' in loaded and fb.read_unit(fx.gen, 'gen_vt') == target_files
    if case == 'conflict':
        assert 'gen_vt' not in loaded
        assert value['live_alias']['config']['system_prompt'] == 'HOST REGISTERED BEFORE THE SCAN\n'
        assert [n for n in roles if n in fx.sf.agent_registry] == [alias]
        assert after_boot['state'] == 'pending' and after_boot['boot_unverified']['settled'] == 'adopted'
        assert {k: after_boot[k] for k in ('old', 'new')} == {k: desc[k] for k in ('old', 'new')}
        assert 'unreconciled' in value['gate'] and 'unreconciled' in value['resume_old']['error']['message']
        assert value['reconcile_new']['error']['type'] == 'LiveRoleConflict'
        assert 'drop role' in value['reconcile_old']['error']['message']
        assert fb.read_descriptor(fx.gen, 'gen_vt') == after_boot
    else:
        assert 'gen_vt' in loaded and value['gate'] is None
        assert fb.canonical(value['old_claim']) == fb.canonical(old)
        assert value['future_claim']['config']['system_prompt'] == 'ADOPTED\n'


def history(fx):
    out = {}
    for name, rid in fx.runs.items():
        run = fx.sf.get_run(rid)
        row = fx.sf._conn.execute(
            'SELECT digest FROM skillflow_graph_versions WHERE name=? AND version=?',
            (run['graph_name'], run['graph_version'])).fetchone()
        resolved = graph_digest(fx.sf._get_resolver_for_run(rid).graph.to_dict())
        out[name] = {'pin': [run['graph_version'], run['graph_digest']],
                     'history_digest': row and row['digest'], 'resolved_digest': resolved}
    return out


@pytest.mark.parametrize('condition', ['manifest-drift', 'rolled-back-boot'])
def test_old_side_settlement_keeps_new_runs_through_fresh_boot(world, condition):
    """A genuine target run on the NEW side of a successful adoption, then a
    supported mixed condition, an actual ``to='old'`` request and a fresh boot.
    Whatever the request does, every genuine run must still claim its own
    content after the boot."""
    fx, api = world
    fx.start_run('OldRun', 'gen_vt'); fx.pause_run('OldRun')
    old_claim = claim(fx, 'OldRun'); fx.pause_run('OldRun')
    edit = ok(call(fx, 'edit_template', role='maker', template='NEW SIDE PROMPT\n'))
    v_old = edit['previous_version_id']
    fx.start_run('NewRun', 'gen_vt'); fx.pause_run('NewRun')
    new_claim = claim(fx, 'NewRun'); fx.pause_run('NewRun')
    if condition == 'manifest-drift':
        fx.registry.register_one(fx.sf, 'gen_vt')      # ordinary host manifest registration
    else:
        _failing_sibling(fx.gen)                       # an ordinary sibling fails the scan
        reboot(fx, _FailSibling)
        for suffix in ('.yaml', '.roles.json'):
            (fx.gen / f'gen_zzbad{suffix}').unlink()
    desc = fb.read_descriptor(fx.gen, 'gen_vt')
    mixed = fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt', desc['new'])
    gate = blocked(fx)
    cas = fb.unit_identity(fb.read_unit(fx.gen, 'gen_vt'))
    mixed_restore = tools()['restore_pipeline_version']('gen_vt', v_old, cas)
    before = full_state(fx)
    to_old = attempt(lambda: fb.reconcile_adoption(
        fx.sf, fx.registry, 'gen_vt', to='old', config_dir=fx.gen))
    after = full_state(fx)
    value = {'condition': condition, 'descriptor': desc, 'mixed': mixed,
             'gate': gate, 'mixed_mcp_restore': mixed_restore, 'reconcile_old': to_old,
             'state_before_old': before, 'state_after_old': after,
             'old_claim': old_claim, 'new_claim': new_claim}
    if 'value' in to_old:
        # The old-side settlement took effect. Warm only: not boot evidence.
        value['warm_new_claim'] = attempt(claim, fx, 'NewRun')
    else:
        value['reconcile_new'] = attempt(lambda: fb.reconcile_adoption(
            fx.sf, fx.registry, 'gen_vt', to='new', config_dir=fx.gen))
        cas = fb.unit_identity(fb.read_unit(fx.gen, 'gen_vt'))
        value['logical_restore_old'] = tools()['restore_pipeline_version']('gen_vt', v_old, cas)
    value['roles_before_boot'] = sorted(json.loads(fb.read_unit(fx.gen, 'gen_vt')['roles_text']))
    value['loaded'] = reboot(fx)
    value['fresh'] = {name: attempt(claim, fx, name) for name in ('OldRun', 'NewRun')}
    fx.start_run('Future', 'gen_vt')
    value['fresh']['Future'] = attempt(claim, fx, 'Future')
    value['history'] = history(fx)
    value['descriptor_after'] = fb.read_descriptor(fx.gen, 'gen_vt')
    value['whole_state_after'] = fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt',
                                                fb.recorded_unit(fx.gen, 'gen_vt'))
    save(f'old-side-fresh-boot-{condition}.json', value)

    assert mixed and value['gate'] and mixed_restore.get('error'), value
    assert fb.canonical(value['fresh']['OldRun'].get('value')) == fb.canonical(old_claim)
    assert fb.canonical(value['fresh']['NewRun'].get('value')) == fb.canonical(new_claim), \
        'a genuine new-side run lost its role across old-side settlement and a fresh boot'
    assert value['fresh']['Future']['value']['config']['system_prompt'] == 'OLD MAKER\n'
    assert 'gen_vt' in value['loaded'] and value['whole_state_after'] == []
    assert set(json.loads(desc['new']['roles_text'])) <= set(value['roles_before_boot'])
    for name, row in value['history'].items():
        assert row['history_digest'] == row['pin'][1] == row['resolved_digest'], (name, row)
    # The refused old-side settlement changed nothing; repair then restore.
    assert to_old['error']['type'] == 'BundleRefused' and 'drop role' in to_old['error']['message']
    assert after == before
    assert 'value' in value['reconcile_new'] and value['logical_restore_old']['changed']
