"""CPU-only host clearance preserves metadata and every physical/error veto.

These checks authorize/finalize an owned maintenance receipt; no service, CPU
session, Source/config bytes, image or external owner is changed by the caller.
The actual effect operator still owes all four slots, idle-service and byte /
consumer / writer comparisons before its bounded effects.
"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import fcntl
import json

import pytest

from cli import server
from core import deployment_quiescence as dq
from tests.unit.test_backend_passive_deployment import packet, seal
from tests.unit.test_host_live_deployment_observation import live
from tests.unit.test_live_measurement_override import normal


def cpu_packet():
    obs = packet()
    obs['registered_external_owners'] += [
        {'attempt_id': 'docs', 'project_id': 'other', 'node_key': 'docs.frozen',
         'harness': 'remote', 'external_id': 'doc-author', 'status': 'active'},
        {'attempt_id': 'research', 'project_id': 'other', 'node_key': 'research.analysis',
         'harness': 'remote', 'external_id': 'research-author', 'status': 'active'}]
    return seal(obs)


def decision(obs, **changes):
    return {'action': 'restart', 'actor': 'driver:director',
            'reason': 'Existing CPU entry/config/idle-service maintenance only',
            'ticket': 'owned CPU service maintenance', 'inventory_digest': obs['digest'],
            'expires_at': (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            'cpu_only': True, 'deployment_attempt_id': 'deployment-current',
            'acknowledge_unknown': True, **changes}


def refuse(tmp_path, obs, override=None, **trusted):
    before = deepcopy(obs)
    with pytest.raises(dq.DeploymentBlocked):
        dq.authorize('restart', obs, journal=tmp_path/'journal.json',
                     override=decision(obs) if override is None else override,
                     **({'cpu_only': True} if not trusted else trusted))
    event = dq._load_journal(tmp_path/'journal.json')['latest']
    assert event['status'] == 'aborted' and not event['usable'] and not event['pending']
    assert event['blockers'] == obs['blockers'] and event['errors'] == obs['errors']
    assert obs == before
    return event


@pytest.mark.parametrize('success', [False, True])
def test_cpu_scope_discloses_active_metadata_without_owner_ack_or_false_quiet(tmp_path, success):
    obs = cpu_packet(); before = deepcopy(obs)
    clearance = dq.authorize('restart', obs, journal=tmp_path/'journal.json',
                             override=decision(obs), cpu_only=True)
    event = clearance['event']; audit = event['audit']
    assert clearance['allowed'] and event['status'] == 'overridden'
    assert audit['cpu_only'] is True and obs['quiescent'] is False
    assert not {'backend_only', 'godot_only', 'semantic_only', 'coordinated_owners'} & audit.keys()
    assert audit['override_scope'] == 'unknown_ownership'
    for row in obs['registered_external_owners'] + obs['external_owners']:
        assert row in audit['affected_ownership']
    assert audit['deployment_controller_identity'] == 'recorded_owner'
    assert event['blockers'] == obs['blockers'] and event['errors'] == obs['errors']
    assert event['inventory_digest'] == obs['digest'] and obs == before
    done = dq.finalize(clearance, success=success, journal=tmp_path/'journal.json')['event']
    assert done['status'] == ('completed' if success else 'aborted')
    assert done['usable'] is success and not done['pending']
    assert done['prior_event_id'] == event['event_id']


@pytest.mark.parametrize('trusted', [{'cpu_only': False}, {'backend_only': True},
                                    {'godot_only': True}, {'semantic_only': True}])
def test_json_cannot_grant_cpu_caller_authority(tmp_path, trusted):
    refuse(tmp_path, cpu_packet(), **trusted)


@pytest.mark.parametrize('scope', ['backend_only', 'godot_only', 'semantic_only'])
def test_cpu_scope_cannot_be_mixed_or_reused_for_another_effect(tmp_path, scope):
    obs = cpu_packet()
    refuse(tmp_path, obs, decision(obs, **{scope: True}))
    with pytest.raises(ValueError, match='mutually exclusive'):
        dq.authorize('restart', obs, journal=tmp_path/'mixed.json',
                     override=decision(obs), cpu_only=True, **{scope: True})
    other = decision(obs, cpu_only=False, **{scope: True})
    refuse(tmp_path, obs, other)


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_cpu_caller_requires_literal_boolean(tmp_path, value):
    with pytest.raises(ValueError, match='explicit boolean'):
        dq.authorize('restart', cpu_packet(), journal=tmp_path/'journal.json', cpu_only=value)


@pytest.mark.parametrize('change', [
    {'inventory_digest': '0'*64}, {'expires_at': '2000-01-01T00:00:00+00:00'},
    {'action': 'redeploy'}, {'actor': 'driver:other'}, {'deployment_attempt_id': 'missing'},
    {'cpu_only': False}, {'cpu_only': 'true'}, {'acknowledge_unknown': False},
    {'coordinated_owners': []}, {'coordinated_owners': [
        {'kind': 'registered_external_owners', 'id': 'docs', 'scope_ref': 'frozen Doc assignment'}]}])
def test_stale_wrong_or_fabricated_coordination_decisions_refuse(tmp_path, change):
    obs = cpu_packet(); refuse(tmp_path, obs, decision(obs, **change))


@pytest.mark.parametrize('quiet', [False, True])
def test_cpu_requires_matching_scope_even_when_inventory_is_quiet(tmp_path, quiet):
    obs = cpu_packet()
    if quiet:
        obs.update(runs=[], sidecar_owners=[], external_owners=[], registered_external_owners=[],
                   godot_render_owners=[], blockers={}, errors=[], quiescent=True)
        seal(obs)
    override = decision(obs)
    for key in ['cpu_only', 'deployment_attempt_id']: override.pop(key)
    refuse(tmp_path, obs, override)
    with pytest.raises(dq.DeploymentBlocked):
        dq.authorize('restart', obs, journal=tmp_path/'none.json', cpu_only=True)
    exact = decision(obs); exact.pop('deployment_attempt_id')
    accepted = dq.authorize('restart', obs, journal=tmp_path/'exact.json',
                            override=exact, cpu_only=True)
    assert accepted['event']['audit']['cpu_only'] is True
    assert accepted['event']['status'] == ('authorized' if quiet else 'overridden')
    dq.finalize(accepted, success=True, journal=tmp_path/'exact.json')


@pytest.mark.parametrize('field,value', [
    ('pid', 123), ('resource', 'cpu'), ('session_id', 'cpu-live'),
    ('operation_id', 'op-live'), ('run_id', 'run-live'), ('runtime', {}),
    ('active_operations', 0), ('future_authoritative_field', None)])
@pytest.mark.parametrize('status', ['active', 'paused', 'unknown', 'settled', 'abandoned'])
def test_registry_physical_or_future_fields_are_never_demoted(tmp_path, field, value, status):
    obs = cpu_packet(); row = obs['registered_external_owners'][1]
    row.update({field: value, 'status': status, 'settled_at': '2000-01-01T00:00:00+00:00',
                'abandon_kind': 'confirmed_stopped'})
    seal(obs)
    event = refuse(tmp_path, obs)
    assert 'authoritative' in event['reason']


@pytest.mark.parametrize('effect', ['run', 'alive', 'lost', 'unknown', 'lease', 'render',
                                    'known-pid', 'own-child', 'pending', 'revision', 'semantic-error'])
def test_physical_operations_sessions_and_demand_veto_cpu_scope(tmp_path, effect):
    obs = cpu_packet()
    if effect in {'run', 'alive', 'lost', 'unknown'}:
        audit = {'alive': int(effect == 'alive'), 'lost': ['op'] if effect == 'lost' else [],
                 'unknown': ['op'] if effect == 'unknown' else []}
        row = {'run_id': 'actual', 'project_id': 'other', 'status': 'running',
               'active_operations': int(effect != 'run'), 'audit': audit}
        obs['runs'] = [row]; obs['blockers']['active_runs'] = [row]
        obs['blockers']['active_operations'] = [row] if effect != 'run' else []
    elif effect == 'lease':
        obs['blockers']['checkout_leases'] = [{'owner': 'writer', 'canonical_checkout': '/source'}]
    elif effect == 'render':
        row = {'owner_id': 'render', 'status': 'active'}
        obs['godot_render_owners'] = [row]; obs['blockers']['godot_render_owners'] = [row]
    elif effect in {'known-pid', 'own-child'}:
        row = {'kind': 'process', 'command': '123 1 python cpu-session.py', 'pid': 123,
               'resource': 'cpu', 'active': True, 'ownership': 'registered'}
        if effect == 'own-child': row['attempt_id'] = 'deployment-current'
        obs['external_owners'].append(row)
    else:
        row = obs['sidecar_owners'][0]
        if effect == 'pending': row['outcome'] = 'pending'
        elif effect == 'revision': row['done_revision'] = 1
        else: row['error'] = 'semantic worker failed'
    seal(obs); refuse(tmp_path, obs)


@pytest.mark.parametrize('error', ['docker inventory failed', 'CPU session measurement failed',
    'semantic daemon operations are active', 'process lineage malformed',
    dq.UNKNOWN_PROCESS_ERROR_PREFIX+'different process'])
def test_measurement_errors_are_never_erased_or_acknowledged_away(tmp_path, error):
    obs = cpu_packet(); obs['errors'].append(error); seal(obs); refuse(tmp_path, obs)


def seed_metadata(db):
    # Owned fixture tables only, using the same normal registry projection as the
    # backend/Godot tests. No runtime manager or production DB is constructed.
    with db.get_connection() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS state_attempts (attempt_id TEXT PRIMARY KEY, '
                     'project_id TEXT, node_key TEXT, harness TEXT, external_id TEXT, status TEXT, '
                     'execution_kind TEXT, abandon_kind TEXT, owner_driver_id TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS state_external_owners (attempt_id TEXT PRIMARY KEY, '
                     'project_id TEXT, node_key TEXT, harness TEXT, external_id TEXT, status TEXT, '
                     'admitted_at TEXT, updated_at TEXT, settled_at TEXT)')
        for attempt, node in [('docs', 'docs.frozen'), ('research', 'research.analysis')]:
            conn.execute("INSERT INTO state_attempts VALUES (?, 'other', ?, 'remote', ?, 'running', 'external', NULL, NULL)",
                         (attempt, node, 'external-'+attempt))
            conn.execute("INSERT INTO state_external_owners VALUES (?, 'other', ?, 'remote', ?, 'active', 't0', 't0', NULL)",
                         (attempt, node, 'external-'+attempt))
        conn.commit()


def registry(db):
    with db.get_connection() as conn:
        return {table: [dict(row) for row in conn.execute('SELECT * FROM '+table+' ORDER BY attempt_id')]
                for table in ['state_attempts', 'state_external_owners']}


def assert_released():
    with dq.admission_fence_path().open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)


@pytest.mark.parametrize('success', [False, True])
def test_normal_initialized_cpu_caller_keeps_active_doc_research_rows_and_same_fence(normal, success):
    runtime, effects = normal; sf, db, rid, _ = runtime
    seed_metadata(db); before = registry(db); run_before = sf.get_run(rid); snapshots = []
    def decide(action, obs):
        with dq.admission_fence_path().open('a+b') as stream:
            with pytest.raises(BlockingIOError): fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        snapshots.append(deepcopy(obs))
        assert obs['provenance']['domain'] == 'live-runtime-and-operator-host'
        assert {row['attempt_id'] for row in obs['registered_external_owners']} == {'docs', 'research'}
        override = decision(obs); override.pop('deployment_attempt_id')
        return override
    clearance = server._require_deployment_clearance('restart', cpu_service=True, override_decision=decide)
    event = clearance['event']
    assert event['audit']['cpu_only'] is True and event['status'] == 'overridden'
    assert not server._backend_only_clearance(clearance)
    assert event['inventory_digest'] == snapshots[0]['digest']
    assert event['blockers'] == snapshots[0]['blockers'] and event['errors'] == snapshots[0]['errors']
    assert all(row in event['audit']['affected_ownership'] for row in snapshots[0]['registered_external_owners'])
    done = server._finish_deployment(clearance, success=success)['event']
    assert done['status'] == ('completed' if success else 'aborted')
    assert registry(db) == before and sf.get_run(rid) == run_before and effects == []
    assert_released()


def test_normal_cpu_caller_preserves_real_admitted_operation_veto(normal):
    runtime, effects = normal; sf, db, rid, _ = runtime; seed_metadata(db); before = registry(db)
    op = sf._admit_op('tool_step', rid, detail='owned physical operation')
    try:
        def decide(action, obs):
            value = decision(obs); value.pop('deployment_attempt_id'); return value
        with pytest.raises(RuntimeError, match='authoritative'):
            server._require_deployment_clearance('restart', cpu_service=True, override_decision=decide)
        event = json.loads(dq.evidence_path().read_text())['latest']
        assert event['status'] == 'aborted' and event['blockers']['active_operations']
        assert registry(db) == before and effects == []
    finally: sf._retire_op(op)
    assert_released()


@pytest.mark.parametrize('flags', [{'backend_service': True, 'cpu_service': True},
                                  {'godot_service': True, 'cpu_service': True},
                                  {'cpu_service': 'true'}, {'compose_root': '/canonical'}])
def test_wrong_cli_effect_flags_refuse_before_measurement_or_effect(monkeypatch, flags):
    monkeypatch.setattr(dq, 'acquire_cutover_fence', lambda: pytest.fail('fence acquired'))
    with pytest.raises(ValueError): server._require_deployment_clearance('restart', **flags)


@pytest.mark.parametrize('flags', [{}, {'backend_service': True}, {'godot_service': True}])
def test_cpu_decision_cannot_authorize_normal_other_service_caller(normal, flags):
    def decide(action, obs):
        value = decision(obs); value.pop('deployment_attempt_id'); return value
    with pytest.raises(RuntimeError, match='trusted CPU service caller'):
        server._require_deployment_clearance('restart', override_decision=decide, **flags)
    assert normal[1] == []
    assert json.loads(dq.evidence_path().read_text())['latest']['status'] == 'aborted'
    assert_released()


def test_normal_cpu_measurement_failure_is_durable_and_releases_fence(normal, monkeypatch):
    def fail(*args, **kwargs): raise OSError('owned CPU/backend identity measurement failed')
    monkeypatch.setattr(server, '_live_runtime_observation', fail)
    def decide(action, obs):
        value = decision(obs); value.pop('deployment_attempt_id'); return value
    with pytest.raises(RuntimeError, match='authoritative'):
        server._require_deployment_clearance('restart', cpu_service=True, override_decision=decide)
    event = json.loads(dq.evidence_path().read_text())['latest']
    assert event['status'] == 'aborted' and not event['usable']
    assert any('owned CPU/backend identity measurement failed' in error for error in event['errors'])
    assert normal[1] == []
    assert_released()
