"""Corrected observer of the retained pending-recovery probe.

The retained independent observer (``test_pending_recovery_roles.py``, sha256
a3d05d7d..., kept unchanged with its 1 FAIL/1 PASS raw) claims ``Original`` on
the still-pending ``gen_vt`` at its line 63 BEFORE it saves or asserts the
reconciliation outcome. A correct refusal leaves ``gen_vt`` pending, and the
pending gate (``AItelierSkillFlow._refuse_unserved_bundle``) must deny that
claim, so the literal probe cannot pass under correct behaviour. This observer
keeps the same fixture, the same actual editor failure, the same supported
host registration, separate ordinary graph and genuine SDK runs, and records
the refusal and the Later evidence first. It then requires an identical full
state, Later's exact claim and pin, and the pending denial of Original with
nothing changed. The equal-definition control must still reconcile and keep
both original claims and the intended future claim. In the conflict case an
explicit old-side reconciliation is a SEPARATE later operation, recorded as
such; it is never the outcome of the refused new-side call.

Actual AItelierSkillFlow, ConfigRegistry, ToolLoader, MCP ``edit_template``
body, native ``reconcile_adoption`` and the installed SDK. Only the owned
settled-descriptor write raises ``OSError``; no model or tool body runs and
every claim is released unexecuted.
"""
import copy
import json
from pathlib import Path

import pytest
import yaml
from skillflow import PipelineGraph, SkillFlowError

from core import pipeline_frozen_bundle as fb
from test_pipeline_normal_versions import world, call, claim  # noqa: F401  (fixture)

OUT = Path('/evidence')


def save(name, value):
    if OUT.is_dir():
        (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                           default=repr) + '\n')


def full_state(fx):
    """Every provider a refused recovery must leave alone: all generated and
    tool file bytes (target files, adoption descriptor, prepared seals), every
    SQLite graph/version row, run and step row, each live role (its object,
    definition and resolved tool schemas), the manifests, capabilities and the
    target's CAS identity (read from its files, as ``current`` does)."""
    state = json.loads(fx.snapshot())
    sf = fx.sf
    with sf._lock:
        state['sqlite'] = {table: [dict(r) for r in sf._conn.execute(
            f'SELECT * FROM {table} ORDER BY rowid')] for table in (
                'skillflow_graphs', 'skillflow_graph_versions', 'skillflow_runs',
                'skillflow_steps')}
    state['live_roles'] = {name: {'object': id(cfg), 'definition': cfg.to_dict(),
                                  'tool_schemas': getattr(cfg, 'tool_schemas', None)}
                           for name, cfg in sorted(sf.agent_registry._configs.items())}
    state['file_bytes'] = {p.name: p.read_bytes().decode('utf-8', 'replace')
                           for p in sorted(fx.gen.iterdir()) if p.is_file()}
    state['cas'] = fb.unit_identity(fb.read_unit(fx.gen, 'gen_vt'))
    return json.loads(json.dumps(state, default=repr))


def attempt(fn, *args):
    try:
        return {'value': fn(*args)}
    except Exception as exc:                     # noqa: BLE001 - recorded, then asserted
        return {'error': {'type': type(exc).__name__, 'message': str(exc)}}


@pytest.mark.parametrize('conflict', [True, False],
                         ids=['later-conflicting-registration', 'equal-registration-control'])
def test_corrected_pending_recovery_observer(world, monkeypatch, conflict):
    fx, api = world
    fx.start_run('Original', 'gen_vt'); fx.pause_run('Original')
    original_claim = claim(fx, 'Original'); fx.pause_run('Original')
    original_role = original_claim['name']
    before_publish = full_state(fx)
    real, trips = fb.fsync_write, []

    def write_failure(path, text):
        if path.name == 'gen_vt.adoption.json' and json.loads(text).get('state') == 'adopted':
            trips.append(str(path)); raise OSError('owned settled descriptor write failure')
        return real(path, text)

    with monkeypatch.context() as mp:
        mp.setattr(fb, 'fsync_write', write_failure)
        failed = call(fx, 'edit_template', role='maker', template='RECORDED RECOVERY PROMPT\n')
    desc = fb.read_descriptor(fx.gen, 'gen_vt')
    pending_gate = fb.launch_block('gen_vt', fx.gen, sf=fx.sf, registry=fx.registry)
    expected = fb._expected_roles('gen_vt', json.loads(desc['new']['roles_text']))
    graph = yaml.safe_load(desc['new']['graph_yaml'])
    alias = next(role for _, role in fb.agent_refs(graph))
    cfg = expected[alias]
    registered = {'model': cfg.model, 'tools': list(cfg.tools), **copy.deepcopy(cfg.config)}
    if conflict:
        registered.update(system_prompt='LATER HOST RETAINED PROMPT\n', temperature=True)
    # An ordinary supported host role registration in the pending interval,
    # then an ordinary graph + genuine paused SDK run that resolves that name.
    fx.sf.register_agent_config_from_dict(alias, registered)
    graph['name'] = 'recovery_observer'
    graph['description'] = 'ordinary independent SDK graph using an already registered role'
    path = fx.root / 'recovery-observer.yaml'
    path.write_text(yaml.safe_dump(graph, sort_keys=False))
    fx.sf.register_graph(PipelineGraph.from_yaml(path))
    fx.registry.register_one(fx.sf, 'recovery_observer', hint_overrides={'repo_mode': 'none'})
    fx.start_run('Later', 'recovery_observer'); fx.pause_run('Later')
    later_claim = claim(fx, 'Later'); fx.pause_run('Later')
    later_object = id(fx.sf.agent_registry.get(alias))
    later_pin = fx.sf.get_run(fx.runs['Later'])
    original_pin = fx.sf.get_run(fx.runs['Original'])
    observed_conflicts = fb.served_role_conflicts(fx.sf, 'gen_vt', desc['new'])
    descriptor_before = fb.descriptor_path(fx.gen, 'gen_vt').read_bytes()
    state_before = full_state(fx)

    reconcile_new = attempt(lambda: fb.reconcile_adoption(
        fx.sf, fx.registry, 'gen_vt', to='new', config_dir=fx.gen))

    state_after = full_state(fx)
    descriptor_after = fb.descriptor_path(fx.gen, 'gen_vt').read_bytes()
    gate_after = fb.launch_block('gen_vt', fx.gen, sf=fx.sf, registry=fx.registry)
    later_after = claim(fx, 'Later'); fx.pause_run('Later')
    later_pin_after = fx.sf.get_run(fx.runs['Later'])
    value = {'case': 'conflict' if conflict else 'equal-control', 'original_error': failed,
             'failure_trips': trips, 'pending_descriptor': desc, 'pending_gate': pending_gate,
             'snapshot_before_publication': before_publish,
             'later_registered_definition': registered, 'destination_alias': alias,
             'ordinary_graph': graph, 'observed_conflicts_before': observed_conflicts,
             'snapshot_before_reconcile': state_before, 'reconcile_new': reconcile_new,
             'snapshot_after_reconcile': state_after,
             'descriptor_unchanged': descriptor_after == descriptor_before,
             'descriptor_after': fb.read_descriptor(fx.gen, 'gen_vt'), 'gate_after': gate_after,
             'later_claim_before': later_claim, 'later_claim_after': later_after,
             'later_pin_before': later_pin, 'later_pin_after': later_pin_after,
             'later_object_unchanged': id(fx.sf.agent_registry.get(alias)) == later_object,
             'original_claim_before': original_claim, 'original_pin_before': original_pin}

    if conflict:
        # The original target stays pending: Original's resume/claim is denied
        # by the existing gate with nothing changed.
        before_denial = full_state(fx)
        denial = {'resume': attempt(fx.sf.resume_run, fx.runs['Original']),
                  'claim_next_step': attempt(fx.sf.claim_next_step, fx.runs['Original'])}
        after_denial = full_state(fx)
        value.update(original_denial=denial, denial_state_unchanged=before_denial == after_denial,
                     original_pin_after_denial=fx.sf.get_run(fx.runs['Original']),
                     original_role_after_denial=json.loads(json.dumps(
                         fx.sf.agent_registry.get(original_role).to_dict(), default=repr)))
        save('corrected-recovery-conflict.json', value)
        # A SEPARATE, explicit later operation (not the refused call's outcome):
        # settle on the recorded old side, whose roles do not include the
        # conflicting new-only alias, then observe actual claims.
        resolution = attempt(lambda: fb.reconcile_adoption(
            fx.sf, fx.registry, 'gen_vt', to='old', config_dir=fx.gen))
        separate = {'operation': "explicit reconcile_adoption(to='old') after the refusal",
                    'result': resolution,
                    'descriptor_state': fb.read_descriptor(fx.gen, 'gen_vt')['state']}
        if 'value' in resolution:
            separate['original_claim'] = claim(fx, 'Original')
            separate['later_claim'] = claim(fx, 'Later')
            fx.start_run('FutureOld', 'gen_vt')
            separate['future_claim'] = claim(fx, 'FutureOld')
            separate['whole_state'] = fb.whole_state(fx.sf, fx.registry, fx.gen, 'gen_vt',
                                                     fb.recorded_unit(fx.gen, 'gen_vt'))
        value['separate_old_side_resolution'] = separate
    else:
        value['original_claim_after'] = claim(fx, 'Original')
        fx.start_run('Future', 'gen_vt')
        value['future_claim'] = claim(fx, 'Future')
        value['whole_state_after'] = fb.whole_state(
            fx.sf, fx.registry, fx.gen, 'gen_vt', fb.read_descriptor(fx.gen, 'gen_vt')['new'])
    save('corrected-recovery-conflict.json' if conflict else
         'corrected-recovery-equal-control.json', value)

    # The original failure, unchanged from the retained observer.
    assert failed.get('publication_failed') and failed.get('changed') is None and trips, failed
    assert len(failed['prepared_seals']) == 2 and all(s['complete'] for s in failed['prepared_seals'])
    assert desc['state'] == 'pending' and pending_gate and 'unreconciled' in pending_gate
    assert (later_pin_after['graph_version'], later_pin_after['graph_digest']) == (
        later_pin['graph_version'], later_pin['graph_digest'])
    assert fb.canonical(later_after) == fb.canonical(later_claim)
    assert value['later_object_unchanged']
    if conflict:
        assert observed_conflicts == [alias]
        error = reconcile_new.get('error')
        assert error and error['type'] == 'LiveRoleConflict', reconcile_new
        assert f'would replace live role {alias}' in error['message']
        assert state_after == state_before
        assert value['descriptor_unchanged'] and value['descriptor_after']['state'] == 'pending'
        assert gate_after and 'unreconciled' in gate_after
        for name in ('resume', 'claim_next_step'):
            got = denial[name].get('error')
            assert got and got['type'] == 'SkillFlowError' and 'unreconciled' in got['message'], denial
        assert value['denial_state_unchanged']
        assert value['original_pin_after_denial'] == original_pin
        assert value['original_role_after_denial'] == state_before['live_roles'][original_role]['definition']
        assert later_after['config']['system_prompt'] == 'LATER HOST RETAINED PROMPT\n'
        assert later_after['config']['temperature'] is True
        # The separate old-side operation keeps both genuine claims exact.
        assert 'value' in separate['result'], separate
        assert separate['descriptor_state'] == 'reconciled_old' and separate['whole_state'] == []
        assert fb.canonical(separate['original_claim']) == fb.canonical(original_claim)
        assert fb.canonical(separate['later_claim']) == fb.canonical(later_claim)
        assert separate['future_claim']['config']['system_prompt'] == 'OLD MAKER\n'
    else:
        assert observed_conflicts == []
        assert 'value' in reconcile_new, reconcile_new
        assert value['descriptor_after']['state'] == 'reconciled_new' and gate_after is None
        assert fb.canonical(value['original_claim_after']) == fb.canonical(original_claim)
        assert value['future_claim']['config']['system_prompt'] == 'RECORDED RECOVERY PROMPT\n'
        assert value['whole_state_after'] == []
        assert fx.sf.get_run(fx.runs['Original'])['graph_version'] == original_pin['graph_version']
