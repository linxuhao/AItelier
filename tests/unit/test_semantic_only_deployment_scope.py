"""Semantic-only trusted admission over sealed complete inventories, no service effects."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import fcntl
import pytest
from core import deployment_quiescence as dq
from tests.unit.test_backend_passive_deployment import packet, seal


def semantic_packet():
    obs = packet()
    obs['registered_external_owners'][0]['node_key'] = 'deployment.semantic-maintenance'
    obs['registered_external_owners'] += [
        {'attempt_id': 'peer', 'project_id': 'other', 'node_key': 'validation.collector', 'status': 'active'},
        {'attempt_id': 'reviewer', 'project_id': 'aitelier', 'node_key': 'integration.review', 'status': 'active'},
        {'attempt_id': 'paused-unknown', 'status': 'paused', 'settled_at': None},
        {'attempt_id': 'abandoned-unknown', 'status': 'abandoned', 'abandon_kind': 'unknown', 'settled_at': None}]
    row = deepcopy(obs['sidecar_owners'][0]); row['run_id'] = 'other-ready'; row['root'] = '/other'
    obs['sidecar_owners'].append(row)
    obs['blockers']['sidecar_owners'] = obs['sidecar_owners']
    return seal(obs)


def decision(obs, **changes):
    return {'action': 'redeploy', 'actor': 'driver:director', 'reason': 'Reviewed semantic service only',
            'ticket': 'exact current peer-compatible window', 'inventory_digest': obs['digest'],
            'expires_at': (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            'semantic_only': True, 'deployment_attempt_id': 'deployment-current',
            'acknowledge_unknown': True,
            'coordinated_owners': [{'kind': 'registered_external_owners', 'id': n, 'scope_ref': 'current peer ACK'}
                                   for n in ['peer', 'reviewer']], **changes}


def refuse(tmp_path, obs, override=None, **trusted):
    before = deepcopy(obs)
    with pytest.raises(dq.DeploymentBlocked):
        dq.authorize('redeploy', obs, journal=tmp_path/'journal.json',
                     override=decision(obs) if override is None else override,
                     **({'semantic_only': True} if not trusted else trusted))
    event = dq._load_journal(tmp_path/'journal.json')['latest']
    assert event['status'] == 'aborted' and not event['usable'] and not event['pending']
    assert event['blockers'] == obs['blockers'] and event['errors'] == obs['errors']
    assert obs == before
    return event


@pytest.mark.parametrize('success', [False, True])
def test_semantic_scope_preserves_current_facts_and_finalizes_under_same_fence(tmp_path, monkeypatch, success):
    obs = semantic_packet(); before = deepcopy(obs)
    monkeypatch.setattr(dq, 'admission_fence_path', lambda: tmp_path/'admission.lock')
    fence = dq.acquire_cutover_fence()
    try:
        other = (tmp_path/'admission.lock').open('a+b')
        try:
            with pytest.raises(BlockingIOError): fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally: other.close()
        clearance = dq.authorize('redeploy', obs, journal=tmp_path/'journal.json',
                                 override=decision(obs), semantic_only=True)
        event = clearance['event']; audit = event['audit']
        assert clearance['allowed'] and event['status'] == 'overridden'
        assert audit['semantic_only'] is True and 'backend_only' not in audit and 'godot_only' not in audit
        assert audit['deployment_controller_identity'] == 'recorded_owner'
        assert audit['deployment_attempt_id'] == 'deployment-current'
        assert audit['coordinated_owners'] == decision(obs)['coordinated_owners']
        assert audit['acknowledge_unknown'] is True
        assert event['blockers'] == obs['blockers'] and event['errors'] == obs['errors']
        assert event['inventory_digest'] == obs['digest'] and obs == before
        assert all(row in audit['affected_ownership'] for row in obs['external_owners'])
        assert not any(row.get('attempt_id') == 'deployment-current' for row in audit['affected_ownership'])
        done = dq.finalize(clearance, success=success, journal=tmp_path/'journal.json')
        assert done['event']['status'] == ('completed' if success else 'aborted')
        assert done['event']['usable'] is success and not done['event']['pending']
        assert done['event']['prior_event_id'] == event['event_id']
    finally: dq.release_cutover_fence(fence)
    with (tmp_path/'admission.lock').open('a+b') as released:
        fcntl.flock(released, fcntl.LOCK_SH | fcntl.LOCK_NB)


@pytest.mark.parametrize('trusted', [{}, {'semantic_only': False}, {'backend_only': True}, {'godot_only': True}])
def test_json_scope_never_grants_semantic_caller_authority(tmp_path, trusted):
    obs = semantic_packet()
    refuse(tmp_path, obs, **({'semantic_only': False} if not trusted else trusted))


@pytest.mark.parametrize('scope', ['backend_only', 'godot_only'])
def test_semantic_scope_is_mutually_exclusive_in_decision_and_caller(tmp_path, scope):
    obs = semantic_packet()
    refuse(tmp_path, obs, decision(obs, **{scope: True}))
    with pytest.raises(ValueError, match='mutually exclusive'):
        dq.authorize('redeploy', obs, journal=tmp_path/'mixed.json',
                     override=decision(obs), semantic_only=True, **{scope: True})


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_semantic_caller_requires_literal_boolean(tmp_path, value):
    with pytest.raises(ValueError, match='explicit boolean'):
        dq.authorize('redeploy', semantic_packet(), journal=tmp_path/'journal.json', semantic_only=value)


@pytest.mark.parametrize('change', [
    {'inventory_digest': '0'*64}, {'expires_at': '2000-01-01T00:00:00+00:00'},
    {'expires_at': '2100-01-01'}, {'action': 'restart'}, {'actor': 'driver:other'},
    {'deployment_attempt_id': 'another'}, {'deployment_attempt_id': ''},
    {'semantic_only': False}, {'semantic_only': 'true'}, {'acknowledge_unknown': False},
    {'coordinated_owners': []}, {'coordinated_owners': [{'kind': 'registered_external_owners', 'id': 'peer', 'scope_ref': 'ACK'}]}])
def test_invalid_stale_unacknowledged_or_incomplete_cover_refuses(tmp_path, change):
    obs = semantic_packet(); refuse(tmp_path, obs, decision(obs, **change))


@pytest.mark.parametrize('field', ['semantic_only', 'deployment_attempt_id', 'coordinated_owners'])
def test_missing_scope_controller_or_cover_refuses(tmp_path, field):
    obs = semantic_packet(); override = decision(obs); override.pop(field)
    refuse(tmp_path, obs, override)


@pytest.mark.parametrize('quiet', [False, True])
def test_trusted_semantic_caller_cannot_reuse_a_generic_decision(tmp_path, quiet):
    obs = semantic_packet()
    if quiet:
        obs = {**obs, 'runs': [], 'sidecar_owners': [], 'external_owners': [],
               'registered_external_owners': [], 'godot_render_owners': [], 'blockers': {}, 'errors': [], 'quiescent': True}
        seal(obs)
    plain = decision(obs)
    for k in ['semantic_only', 'deployment_attempt_id', 'coordinated_owners']: plain.pop(k)
    refuse(tmp_path, obs, plain)
    with pytest.raises(dq.DeploymentBlocked):
        dq.authorize('redeploy', obs, journal=tmp_path/'none.json', semantic_only=True)


@pytest.mark.parametrize('state', ['pending', 'revision', 'released', 'error'])
def test_unsettled_semantic_demand_remains_an_authoritative_veto(tmp_path, state):
    obs = semantic_packet(); row = obs['sidecar_owners'][0]
    if state == 'pending': row['outcome'] = 'pending'
    elif state == 'revision': row['done_revision'] = 1
    elif state == 'released': row['desired'] = 'released'
    else: row['error'] = 'worker failed'
    seal(obs); refuse(tmp_path, obs)


@pytest.mark.parametrize('effect', ['run', 'operation', 'lost', 'unknown', 'lease', 'render', 'known-process', 'own-child'])
def test_semantic_scope_cannot_name_away_physical_effects(tmp_path, effect):
    obs = semantic_packet()
    if effect in ['run', 'operation', 'lost', 'unknown']:
        row = {'run_id': 'physical', 'project_id': 'other', 'status': 'running',
               'active_operations': int(effect != 'run'),
               'audit': {'alive': int(effect == 'operation'), 'lost': ['lost'] if effect == 'lost' else [],
                         'unknown': ['opaque'] if effect == 'unknown' else []}}
        obs['runs'] = [row]; obs['blockers']['active_runs'] = [row]
        obs['blockers']['active_operations'] = [row] if effect != 'run' else []
    elif effect == 'lease': obs['blockers']['checkout_leases'] = [{'owner': 'writer', 'canonical_checkout': '/source'}]
    elif effect == 'render':
        row = {'owner_id': 'render', 'status': 'active'}
        obs['godot_render_owners'] = [row]; obs['blockers']['godot_render_owners'] = [row]
    else:
        row = {'kind': 'process', 'command': 'python semantic-worker.py', 'active': True}
        if effect == 'own-child': row.update(attempt_id='deployment-current', ownership='registered')
        obs['external_owners'].append(row)
    seal(obs); refuse(tmp_path, obs)


@pytest.mark.parametrize('error', ['semantic indexing operation is active', 'semantic daemon operations are active',
    'semantic project operation is active or unknown', 'semantic project owner measurement failed: unknown',
    'semantic operation ownership is unknown', 'docker inventory failed', 'unrelated measurement error',
    dq.UNKNOWN_PROCESS_ERROR_PREFIX+'different process'])
def test_semantic_operation_marker_daemon_and_unrelated_errors_veto(tmp_path, error):
    obs = semantic_packet(); obs['errors'].append(error); seal(obs); refuse(tmp_path, obs)


def test_render_owner_cannot_be_coordinated_for_semantic_service(tmp_path):
    obs = semantic_packet(); row = {'owner_id': 'render', 'status': 'active'}
    obs['godot_render_owners'] = [row]; obs['blockers']['godot_render_owners'] = [row]; seal(obs)
    cover = decision(obs)['coordinated_owners'] + [{'kind': 'godot_render_owners', 'id': 'render', 'scope_ref': 'claimed ACK'}]
    refuse(tmp_path, obs, decision(obs, coordinated_owners=cover))


@pytest.mark.parametrize('patch', [{'owner_driver_id': 'other'}, {'owner_driver_id': None}, {'project_id': 'other'}, {'node_key': 'research.not-controller'}])
def test_own_controller_passivity_requires_exact_recorded_identity(tmp_path, patch):
    obs = semantic_packet(); obs['registered_external_owners'][0].update(patch); seal(obs)
    if patch == {'owner_driver_id': None}:
        # Unknown is disclosed/acknowledged, never promoted to a known own controller.
        event = dq.authorize('redeploy', obs, journal=tmp_path/'journal.json', override=decision(obs), semantic_only=True)['event']
        assert event['audit']['deployment_controller_identity'] == 'unknown'
        assert obs['registered_external_owners'][0] in event['audit']['affected_ownership']
    else: refuse(tmp_path, obs)


@pytest.mark.parametrize('malformed', ['digest', 'missing', 'count'])
def test_malformed_frozen_measurement_cannot_be_overridden(tmp_path, malformed):
    obs = semantic_packet()
    if malformed == 'digest': obs['digest'] = '0'*64
    elif malformed == 'missing': obs.pop('sidecar_owners')
    else:
        obs['runs'] = [{'run_id': 'bad', 'project_id': 'other', 'status': 'running', 'active_operations': -1}]
        obs['digest'] = dq._observation_digest(obs)
    before = deepcopy(obs)
    with pytest.raises(dq.DeploymentBlocked, match='malformed'):
        dq.authorize('redeploy', obs, journal=tmp_path/'journal.json', override=decision(obs), semantic_only=True)
    assert obs == before


@pytest.mark.parametrize('trusted', [False, True])
def test_quiet_inventory_still_requires_matching_trusted_semantic_scope(tmp_path, trusted):
    obs = semantic_packet()
    obs.update(runs=[], sidecar_owners=[], external_owners=[], registered_external_owners=[],
               godot_render_owners=[], blockers={}, errors=[], quiescent=True)
    seal(obs)
    exact = decision(obs)
    exact.pop('deployment_attempt_id'); exact.pop('coordinated_owners')
    if not trusted:
        with pytest.raises(dq.DeploymentBlocked, match='trusted semantic caller'):
            dq.authorize('redeploy', obs, journal=tmp_path/'journal.json', override=exact)
    else:
        clearance = dq.authorize('redeploy', obs, journal=tmp_path/'journal.json',
                                 override=exact, semantic_only=True)
        assert clearance['event']['audit']['semantic_only'] is True
        assert clearance['event']['audit']['override_scope'] == 'quiescent_restriction'
        assert dq.finalize(clearance, success=True, journal=tmp_path/'journal.json')['event']['usable']
