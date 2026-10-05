"""Evidence attribution through real typed commands, SQLite and event waits."""
import asyncio
import hashlib
import json
import sqlite3

import pytest

from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError, StateConflict
from core.state_service import StateService

ARTIFACT = 'a' * 64


def invoke(service, action, **args):
    return execute(service, action, args, allow_write=True)


@pytest.fixture
def ready(tmp_path, monkeypatch):
    monkeypatch.setenv('AITELIER_HOME', str(tmp_path / 'home'))
    db = StateDatabase(str(tmp_path / 'state.sqlite'))
    service = StateService(db, actor='authenticated-shared-actor', project_read_trusted=True)
    invoke(service, 'create_project', project_id='p', title='Attribution test')
    invoke(service, 'add_nodes', project_id='p', nodes=[{
        'key': 'n', 'goal': 'Check attribution', 'acceptance': [
            {'id': 'a', 'kind': 'test', 'description': 'Check first recording'},
            {'id': 'b', 'kind': 'test', 'description': 'Check second recording'},
            {'id': 'c', 'kind': 'test', 'description': 'Check legacy recording'}]}])
    attempt = invoke(service, 'start_external_attempt', project_id='p', node_key='n',
                     expected_revision=1, harness='isolated-test', external_id='worker', request_key='r')
    return service, attempt, tmp_path


def finish(ready, status):
    service, attempt, root = ready
    body = json.dumps({'status': status, 'settled': True, 'usable': True}).encode()
    path = root / 'terminal.json'
    path.write_bytes(body)
    args = dict(attempt_id=attempt['attempt_id'], observation_id='end', expected_version=0,
                context_hash=attempt['context_hash'], status=status, report_ref=str(path),
                report_sha256=hashlib.sha256(body).hexdigest(), quiescent=True)
    if status == 'candidate':
        args.update(artifact=ARTIFACT, artifact_kind='sha256')
    return invoke(service, 'report_external_attempt', **args)


def recording(ready, criterion, identity=None):
    _, attempt, root = ready
    body = json.dumps({'status': 'completed', 'settled': True, 'usable': True,
                       'criterion_id': criterion, 'verdict': 'pass', 'artifact': ARTIFACT}).encode()
    path = root / (criterion + '.json')
    path.write_bytes(body)
    args = dict(attempt_id=attempt['attempt_id'], evidence_id='ev-' + criterion,
                criterion_id=criterion, verdict='pass', artifact=ARTIFACT,
                report_ref=str(path), report_sha256=hashlib.sha256(body).hexdigest())
    if identity is not None:
        args['director_identity'] = identity
    return args


@pytest.mark.parametrize('status', ['candidate', 'failed'])
def test_two_directors_legacy_and_idempotency_project_through_events_and_wait(ready, status):
    service, attempt, _ = ready
    finish(ready, status)
    args = recording(ready, 'a', 'director-one')
    first = invoke(service, 'record_evidence', **args)
    assert first['reviewer'] == service.actor
    assert first['director_identity'] == 'director-one'
    assert invoke(service, 'record_evidence', **args) == first
    with pytest.raises(StateConflict):
        invoke(service, 'record_evidence', **(args | {'director_identity': 'director-two'}))
    invoke(service, 'record_evidence', **recording(ready, 'b', 'director-two'))
    legacy = invoke(service, 'record_evidence', **recording(ready, 'c'))
    assert legacy['reviewer'] == service.actor
    assert legacy['director_identity'] is None
    events = invoke(service, 'events', project_id='p')
    ev = [e for e in events if e['event_type'] == 'evidence_recorded']
    assert len(ev) == 3
    payloads = [e['payload'] for e in ev]
    assert [p.get('director_identity') for p in payloads] == ['director-one', 'director-two', None]
    assert all(p['reviewer'] == service.actor and p['actor'] == service.actor for p in payloads)
    waited = asyncio.run(invoke(service, 'wait_for_state_change', project_id='p',
                                attempt_ids=[attempt['attempt_id']], after=0, timeout_seconds=0))
    assert [e for e in waited['events'] if e['event_type'] == 'evidence_recorded'] == ev
    assert sum(p.get('director_identity') == 'director-one' for p in payloads) == 1
    assert sum(p.get('director_identity') != 'director-one' for p in payloads) == 2
    assert service.attempts.get(attempt['attempt_id'])['status'] == status


@pytest.mark.parametrize('identity', ['', ' ', 1, True, {}, [], 'x' * 321, 'bad\nidentity', 'bad\x00identity', 'bad\x85identity', 'bad\u202eidentity', 'bad\ud800identity'])
def test_malformed_identity_refuses_before_service_or_store_write(ready, identity):
    service, _, _ = ready
    finish(ready, 'candidate')
    args = recording(ready, 'a') | {'director_identity': identity}
    before = invoke(service, 'events', project_id='p')
    with service.store.transaction() as conn:
        blobs_before = conn.execute('SELECT COUNT(*) FROM state_external_report_blobs').fetchone()[0]
    with pytest.raises(StateGraphError):
        invoke(service, 'record_evidence', **args)
    with pytest.raises(StateGraphError):
        service.record_evidence(**args)
    with pytest.raises(StateGraphError):
        service.attempts.record_evidence(**args, reviewer=service.actor)
    assert invoke(service, 'events', project_id='p') == before
    assert service.attempts.evidence(ready[1]['attempt_id']) == []
    with service.store.transaction() as conn:
        assert conn.execute('SELECT COUNT(*) FROM state_external_report_blobs').fetchone()[0] == blobs_before


@pytest.mark.parametrize('spoof', ['actor', 'reviewer'])
def test_authenticated_actor_cannot_be_overridden(ready, spoof):
    service, _, _ = ready
    finish(ready, 'candidate')
    args = recording(ready, 'a', 'self-declared') | {spoof: 'spoofed'}
    with pytest.raises(StateGraphError):
        invoke(service, 'record_evidence', **args)
    assert service.attempts.evidence(ready[1]['attempt_id']) == []


def test_upgrade_legacy_row_preserves_hash_duplicate_and_historical_event(ready):
    service, _, _ = ready
    finish(ready, 'candidate')
    args = recording(ready, 'a')
    legacy = invoke(service, 'record_evidence', **args)
    from core.state_graph import digest
    old_payload = {key: legacy[key] for key in (
        'attempt_id', 'criterion_id', 'verdict', 'artifact_ref', 'report_ref',
        'report_sha256', 'reviewer', 'detail')}
    assert legacy['payload_hash'] == digest(old_payload)
    with service.store.transaction(write=True) as conn:
        # Reproduce the previous table and event layout without manufacturing identity.
        conn.execute('ALTER TABLE state_evidence DROP COLUMN director_identity')
        service.store._event(conn, "p", "n", "evidence_recorded",
                             old_payload | {"evidence_id": "historical-fixture"})
    before = invoke(service, 'events', project_id='p')
    upgraded = StateService(service.db, actor=service.actor, project_read_trusted=True)
    assert invoke(upgraded, 'record_evidence', **args) == legacy
    assert invoke(upgraded, 'events', project_id='p') == before
    invoke(upgraded, 'record_evidence', **recording(ready, 'b', 'new-director'))
    with upgraded.store.transaction() as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            conn.execute("UPDATE state_evidence SET director_identity='changed'")


def test_report_attribution_cannot_override_service_actor_or_explicit_identity(ready):
    service, _, root = ready
    finish(ready, 'candidate')
    args = recording(ready, 'a', 'explicit-director')
    path = root / 'a.json'
    report = json.loads(path.read_bytes()) | {
        'actor': 'spoofed-report-actor', 'reviewer': 'spoofed-reviewer',
        'director_identity': 'spoofed-report-director'}
    body = json.dumps(report).encode()
    path.write_bytes(body)
    args['report_sha256'] = hashlib.sha256(body).hexdigest()
    evidence = invoke(service, 'record_evidence', **args)
    assert evidence['reviewer'] == service.actor
    assert evidence['director_identity'] == 'explicit-director'
    other = StateService(service.db, actor='other-authenticated-actor', project_read_trusted=True)
    invoke(other, 'record_evidence', **recording(ready, 'b', 'other-director'))
    events = invoke(service, 'events', project_id='p')
    payloads = [e['payload'] for e in events if e['event_type'] == 'evidence_recorded']
    assert [p['actor'] for p in payloads] == [service.actor, other.actor]
    assert [p['reviewer'] for p in payloads] == [service.actor, other.actor]
    assert [p['director_identity'] for p in payloads] == ['explicit-director', 'other-director']
