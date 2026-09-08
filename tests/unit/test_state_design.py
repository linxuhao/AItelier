"""Design/version contracts against isolated SQLite and authenticated HTTP."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from api.state_only import create_app
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService
from core.state_graph import StateConflict, StateGraphError, StateNotFound

SCOPE = {'mode': 'all'}


@pytest.fixture
def service(tmp_path):
    s = StateService(StateDatabase(str(tmp_path / 'state.sqlite')), actor='authorized-author')
    s.create_project('game', 'Game')
    s.store.add_nodes('game', [{'key': 'work', 'goal': 'Deliver behavior', 'acceptance': [
        {'id': 'behavior', 'kind': 'test', 'description': 'Actual behavior passes'}]},
        {'key': 'other', 'goal': 'Other behavior', 'acceptance': [
        {'id': 'other', 'kind': 'test', 'description': 'Other behavior passes'}]}])
    return s


def revision(s, design_id='cooldown', expected_revision=0, **changes):
    args = dict(project_id='game', design_id=design_id, expected_revision=expected_revision,
                title='Cooldown', statement='Cooldown is two seconds.', rationale='Avoid repeated activation.',
                open_questions=[], scope=SCOPE, lifecycle_status='approved')
    return s.design.create_revision(**(args | changes))


def baseline(s, name='b1', refs=None, expected=None):
    return s.design.create_baseline('game', name, refs or [{'design_id': 'cooldown', 'revision': 1}], expected)


def bind(s, name='b1', revision=1, purpose='implements', design_revision=1):
    return s.design.bind_node('game', 'work', revision, name, [{'design_id': 'cooldown',
        'revision': design_revision, 'purpose': purpose, 'coverage_scope': SCOPE}], 'Adopt exact design inputs')


def relation(kind, did, revision=1):
    return {'type': kind, 'target': {'design_id': did, 'revision': revision}, 'rationale': 'Explicit relationship'}


def test_immutable_revisions_and_stale_edit_cas(service):
    first = revision(service)
    second = revision(service, expected_revision=1, statement='Cooldown is three seconds.')
    assert second['revision'] == 2 and second['content']['parent_revision'] == 1
    assert first == service.design.get_revision('game', 'cooldown', 1)
    with pytest.raises(StateConflict, match='revision changed'):
        revision(service, expected_revision=1)
    assert first['author'] == 'authorized-author'
    for operation in ['UPDATE state_design_revisions SET payload_json=\'{}\'', 'DELETE FROM state_design_revisions']:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'), service.store.transaction(write=True) as conn:
            conn.execute(operation)


def test_concurrent_edits_do_not_overwrite(service):
    revision(service)
    def edit(i):
        try:
            return revision(service, expected_revision=1, statement=str(i))['revision']
        except StateConflict:
            return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(edit, [1, 2]), key=str) == [2, 'conflict']


def test_baseline_pins_history_and_rejects_invalid_or_stale_selection(service):
    revision(service); b1 = baseline(service)
    revision(service, expected_revision=1, statement='Changed')
    assert service.design.get_baseline('game', 'b1') == b1
    assert b1['manifest']['selected_revisions'][0]['revision'] == 1
    with pytest.raises(StateConflict, match='current baseline changed'):
        baseline(service, 'b2')
    with pytest.raises(StateNotFound):
        baseline(service, 'bad', [{'design_id': 'cooldown', 'revision': 99}], 'b1')
    with pytest.raises(StateGraphError, match='exactly one'):
        baseline(service, 'bad', [{'design_id': 'cooldown', 'revision': 1}, {'design_id': 'cooldown', 'revision': 2}], 'b1')
    assert service.design.catalog('game')['current_baseline_id'] == 'b1'
    for table in ['state_design_baselines']:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'), service.store.transaction(write=True) as conn:
            conn.execute('DELETE FROM ' + table)


def test_unresolved_is_never_an_approved_requirement(service):
    with pytest.raises(StateGraphError, match='unresolved'):
        revision(service, open_questions=['What about co-op?'])
    revision(service, lifecycle_status='draft', open_questions=['What about co-op?'])
    baseline(service)
    with pytest.raises(StateGraphError, match='approved'):
        bind(service)
    result = bind(service, purpose='context')
    assert result['design_context']['bindings'][0]['purpose'] == 'context'
    rendered = service.design.export_markdown('game', 'b1')['markdown']
    assert 'Non-approved statement' in rendered and 'Open questions (not approved requirements)' in rendered
    assert '### Approved statement' not in rendered


def test_exact_refs_full_supersession_and_branch_conflicts(service):
    revision(service)
    with pytest.raises(StateNotFound):
        revision(service, 'invalid', relations=[relation('depends_on', 'missing')])
    with pytest.raises(StateGraphError, match='partial-scope'):
        revision(service, 'partial', relations=[relation('supersedes', 'cooldown') | {'scope': {'mode': 'coop'}}])
    with pytest.raises(StateGraphError, match='identical scope'):
        revision(service, 'partial', scope={'mode': 'coop'}, relations=[relation('supersedes', 'cooldown')])
    revision(service, 'replacement', relations=[relation('supersedes', 'cooldown')])
    revision(service, 'branch', relations=[relation('supersedes', 'cooldown')])
    for ids in [('cooldown', 'replacement'), ('replacement', 'branch')]:
        with pytest.raises(StateConflict, match='supersession'):
            baseline(service, refs=[{'design_id': d, 'revision': 1} for d in ids])
    revision(service, 'successor', relations=[relation('supersedes', 'replacement')])
    with pytest.raises(StateConflict, match='supersession'):
        baseline(service, refs=[{'design_id': d, 'revision': 1} for d in ('cooldown', 'successor')])
    baseline(service, refs=[{'design_id': 'successor', 'revision': 1}])


def test_dependency_refs_are_exact_and_freeze_only_needed_prose(service):
    revision(service, 'interface')
    revision(service, relations=[relation('depends_on', 'interface')])
    revision(service, 'unrelated', statement='Large unrelated subsystem')
    with pytest.raises(StateGraphError, match='exact revision selected'):
        baseline(service)
    baseline(service, refs=[{'design_id': d, 'revision': 1} for d in ['interface', 'cooldown', 'unrelated']])
    binding = bind(service)
    ctx = binding['design_context']
    assert [d['design_id'] for d in ctx['design_dependencies']] == ['interface']
    assert 'Large unrelated subsystem' not in json.dumps(ctx)
    assert len(ctx['manifest']['selected_revisions']) == 3
    assert service.store.get_node('game', 'work')['dependencies'] == []


def test_deterministic_readonly_view_and_drift_detection(service):
    revision(service); baseline(service)
    original = service.design.export_markdown('game', 'b1')
    assert 'cooldown @ 1' in original['markdown'] and 'Baseline: b1' in original['markdown']
    revision(service, expected_revision=1, title='Moved section')
    baseline(service, 'b2', [{'design_id': 'cooldown', 'revision': 2}], 'b1')
    assert original == service.design.export_markdown('game', 'b1')
    assert service.design.check_markdown('game', 'b1', original['markdown'])['matches']
    assert not service.design.check_markdown('game', 'b1', original['markdown'] + 'edit')['matches']


def test_binding_freezes_external_context_and_prevents_cross_version_evidence(service):
    revision(service); baseline(service); bind(service)
    a = service.start_external_attempt('game', 'work', 2, 'harness', 'job', 'request')
    frozen = a['context']
    assert frozen['design_context']['bindings'][0]['design']['content']['statement'] == 'Cooldown is two seconds.'
    revision(service, expected_revision=1, statement='Cooldown is three seconds.')
    baseline(service, 'b2', [{'design_id': 'cooldown', 'revision': 2}], 'b1')
    # Selecting a baseline does not alter execution scope, status or dependencies.
    assert service.attempts.get(a['attempt_id'])['context'] == frozen
    assert service.design.node_bindings('game', 'work')['baseline_review_required']
    candidate = service.report_external_attempt(a['attempt_id'], 'done', 0, a['context_hash'], 'candidate',
        'reports/result.json', 'b' * 64, True, 'a' * 64, 'sha256')
    assert candidate['status'] == 'candidate'
    bind(service, 'b2', revision=2, design_revision=2)
    assert service.attempts.get(a['attempt_id'])['context'] == frozen
    with pytest.raises(StateConflict, match='stale'):
        service.attempts.record_evidence(a['attempt_id'], 'e', 'behavior', 'pass', 'a' * 64,
            'reports/test.json', 'b' * 64, 'reviewer')
    with pytest.raises(StateConflict):
        service.attempts.verify('game', 'work', 3, a['attempt_id'], 'reviewer')
    assert service.attempts.get(a['attempt_id'])['status'] == 'candidate'
    new = service.start_external_attempt('game', 'work', 3, 'harness', 'job-2', 'request-2')
    assert new['context_hash'] != a['context_hash']
    assert new['context']['design_context']['baseline_id'] == 'b2'
    assert service.store.get_node('game', 'other')['revision'] == 1


def test_binding_cas_exact_selection_and_workflow_reservation(service):
    revision(service); baseline(service)
    with pytest.raises(StateGraphError, match='not selected'):
        bind(service, design_revision=2)
    bind(service)
    with pytest.raises(StateConflict, match='revision changed'):
        bind(service)
    a = service.attempts.reserve('game', 'work', 2, 'workflow', 'request')
    assert a['context']['design_context']['baseline_id'] == 'b1'
    assert a['context']['binding_snapshot_hash']
    with pytest.raises(sqlite3.IntegrityError, match='immutable'), service.store.transaction(write=True) as conn:
        conn.execute('DELETE FROM state_design_bindings')
    unbound = service.attempts.reserve('game', 'other', 1, 'workflow', 'request')
    assert 'design_context' not in unbound['context']


def test_design_http_authorization_read_surface_and_actor_spoofing(tmp_path):
    app = create_app(str(tmp_path / 'http.sqlite'), 'x' * 32, with_mcp=False)
    client = TestClient(app)
    assert client.post('/api/state/query/design_catalog', json={'project_id': 'p'}).status_code == 401
    assert client.post('/api/state/commands/create_design_revision', json={}).status_code == 401
    client.headers['Authorization'] = 'Bearer ' + 'x' * 32
    assert client.post('/api/state/commands/create_project', json={'project_id': 'p', 'title': 'Project'}).status_code == 200
    body = dict(project_id='p', design_id='rule', expected_revision=0, title='Rule', statement='Do this',
                rationale='Because', open_questions=[], scope=SCOPE)
    assert client.post('/api/state/query/create_design_revision', json=body).status_code == 422
    assert client.post('/api/state/commands/create_design_revision', json=body | {'author': 'spoof'}).status_code == 422
    response = client.post('/api/state/commands/create_design_revision', json=body)
    assert response.status_code == 200 and response.json()['author'] == 'authenticated-state-token'
    with pytest.raises(StateGraphError, match='read surface'):
        execute(app.state.state_service, 'create_design_revision', body)
    assert 'create_design_revision' in client.get('/api/state/schema').json()['operations']


def test_old_receipt_remains_historical_after_explicit_rebinding(service):
    revision(service); baseline(service); bind(service)
    a = service.start_external_attempt('game', 'work', 2, 'harness', 'job', 'request')
    service.report_external_attempt(a['attempt_id'], 'done', 0, a['context_hash'], 'candidate',
        'reports/result.json', 'b' * 64, True, 'a' * 64, 'sha256')
    service.attempts.record_evidence(a['attempt_id'], 'e', 'behavior', 'pass', 'a' * 64,
        'reports/test.json', 'b' * 64, 'reviewer')
    receipt = service.attempts.verify('game', 'work', 2, a['attempt_id'], 'reviewer')
    assert json.loads(receipt['provenance_json'])['design'] == {
        'baseline_id': 'b1', 'manifest_hash': a['context']['design_context']['manifest_hash'],
        'binding_snapshot_hash': a['context']['binding_snapshot_hash']}
    revision(service, expected_revision=1, statement='New rule')
    baseline(service, 'b2', [{'design_id': 'cooldown', 'revision': 2}], 'b1')
    assert service.store.get_node('game', 'work')['status'] == 'VERIFIED'
    bind(service, 'b2', revision=2, design_revision=2)
    assert service.store.get_node('game', 'work')['status'] == 'STALE'
    with service.store.transaction() as conn:
        historical = dict(conn.execute('SELECT * FROM state_acceptances WHERE receipt_id=?',
            (receipt['receipt_id'],)).fetchone())
    assert historical == receipt
    assert service.attempts.evidence(a['attempt_id'])[0]['verdict'] == 'pass'


def test_approved_dependency_cannot_silently_inherit_draft(service):
    revision(service, 'draft', lifecycle_status='draft', open_questions=['Unresolved'])
    revision(service, relations=[relation('depends_on', 'draft')])
    with pytest.raises(StateGraphError, match='non-approved requirement'):
        baseline(service, refs=[{'design_id': d, 'revision': 1} for d in ['cooldown', 'draft']])


def test_mcp_design_commands_share_auth_and_exact_read_contract(tmp_path):
    app = create_app(str(tmp_path / 'mcp.sqlite'), 'x' * 32)
    with TestClient(app) as client:
        def rpc(name, action, args, auth=True):
            headers = {'Accept': 'application/json, text/event-stream'}
            if auth:
                headers['Authorization'] = 'Bearer ' + 'x' * 32
            return client.post('/mcp/', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': name, 'arguments': {'action': action, 'arguments': args}}}, headers=headers)
        assert rpc('state_graph_read', 'design_catalog', {'project_id': 'p'}, False).status_code == 401
        response = rpc('state_graph_write', 'create_project', {'project_id': 'p', 'title': 'P'})
        assert not response.json()['result'].get('isError')
        body = dict(project_id='p', design_id='r', expected_revision=0, title='Rule', statement='Do this',
                    rationale='Because', open_questions=[], scope=SCOPE)
        result = rpc('state_graph_write', 'create_design_revision', body).json()['result']
        assert not result.get('isError'), result
        result = rpc('state_graph_read', 'get_design_revision', {'project_id': 'p', 'design_id': 'r', 'revision': 1}).json()['result']
        assert json.loads(result['content'][0]['text'])['result']['content']['statement'] == 'Do this'
        assert rpc('state_graph_read', 'create_design_revision', body).json()['result']['isError']
