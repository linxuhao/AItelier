"""Actual run rows, not legacy execution projects; GET has no state side effects."""
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode
from core.db_manager import DBManager
from core.state_service import StateService
from api import run_history_routers as routes
from api.authz import require_writer


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv('AITELIER_HOME', str(tmp_path/'home'))
    db=DBManager(str(tmp_path/'host.db'));sf=SkillFlow(str(tmp_path/'sf.db'))
    for name in ['feature','gen_report','pipeline_forge']:
        sf.register_graph(PipelineGraph(name=name,begin='a',steps=[StepNode(id='a')]))
    db.ensure_project('same-execution',name='Shared execution',repo_type='none',owner_email='owner@test')
    db.ensure_project('other-owner',name='Private other owner',repo_type='none',owner_email='other@test')
    run1=sf.create_run('feature',project_id='same-execution');sf.start_run(run1);sf.fail_run(run1,'fixture failure')
    run2=sf.create_run('gen_report',project_id='same-execution');sf.start_run(run2)
    run3=sf.create_run('pipeline_forge',project_id='other-owner');sf.start_run(run3);sf.pause_run(run3)
    standalone=sf.create_run('gen_report',project_id='engine-only')
    app=FastAPI();app.include_router(routes.router)
    app.dependency_overrides[routes.get_db_manager]=lambda:db
    app.dependency_overrides[routes.get_skillflow]=lambda:sf
    app.dependency_overrides[require_writer]=lambda:None
    monkeypatch.setattr(routes,'owner_filter',lambda *_:None)
    with TestClient(app) as client:
        yield SimpleNamespace(db=db,sf=sf,app=app,client=client,ids=[run1,run2,run3,standalone])
    sf._conn.close()


def test_repeated_and_repo_free_and_authoring_runs_are_each_present(system):
    out=system.client.get('/api/run-history').json()
    assert out['total']==4
    assert {r['id'] for r in out['runs']}==set(system.ids)
    assert sum(r['project_id']=='same-execution' for r in out['runs'])==2
    assert {r['config_name'] for r in out['runs']}=={'feature','gen_report','pipeline_forge'}
    assert not any('input_data' in r or 'output' in r for r in out['runs'])


def test_history_exposes_only_a_running_step(system):
    rows = {r['id']: r for r in system.client.get('/api/run-history').json()['runs']}
    running = rows[system.ids[1]]
    assert running['status'] == 'running'
    assert running['active_step']['step_id'] == running['current_node'] == 'a'
    assert running['active_step']['source'] == 'current_node'
    assert rows[system.ids[2]]['status'] == 'paused'
    assert rows[system.ids[2]]['active_step'] is None


def test_owner_scope_applies_before_aggregation(system,monkeypatch):
    monkeypatch.setattr(routes,'owner_filter',lambda *_:'owner@test')
    out=system.client.get('/api/run-history').json()
    assert out['total']==2
    assert {r['project_id'] for r in out['runs']}=={'same-execution'}


def test_filters_before_pagination_and_bounds(system):
    c=system.client
    assert c.get('/api/run-history?status=paused').json()['runs'][0]['id']==system.ids[2]
    assert c.get('/api/run-history?workflow=gen_report').json()['total']==2
    assert c.get('/api/run-history?q=Shared').json()['total']==2
    first=c.get('/api/run-history?limit=2').json();second=c.get('/api/run-history?limit=2&offset=2').json()
    assert first['next_offset']==2 and second['next_offset'] is None
    assert not {r['id'] for r in first['runs']} & {r['id'] for r in second['runs']}
    for bad in ['limit=0','limit=10000','offset=-1','q='+'x'*201]:assert c.get('/api/run-history?'+bad).status_code==422


def test_read_does_not_initialize_or_mutate_state(system):
    with system.db.get_connection() as c:
        before=[tuple(r) for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    states=[system.sf.get_run(r)['status'] for r in system.ids]
    system.client.get('/api/run-history')
    with system.db.get_connection() as c:
        assert [tuple(r) for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]==before
    assert states==[system.sf.get_run(r)['status'] for r in system.ids]


def test_attempt_ownership_filters_one_project_without_hiding_legacy_globally(system):
    service=StateService(system.db)
    service.create_project('game','Game')
    service.store.add_nodes('game',[{'key':'goal','goal':'Goal','acceptance':[{'id':'test','kind':'test','description':'Acceptance'}]}])
    a=service.attempts.reserve('game','goal',1,'feature','fixture')
    system.db.ensure_project(a['execution_project_id'],repo_type='none')
    rid=system.sf.create_run('feature',project_id=a['execution_project_id'])
    service.attempts.bind_run(a['attempt_id'],rid,system.sf)
    out=system.client.get('/api/run-history?state_project_id=game').json()
    assert out['total']==1 and out['runs'][0]['id']==rid
    assert out['runs'][0]['state_node_key']=='goal'
    assert system.client.get('/api/run-history').json()['total']==5


def test_anonymous_access_denied_before_read(system,monkeypatch):
    from api import authz
    system.app.dependency_overrides.pop(require_writer)
    monkeypatch.setattr(authz,'gate_enabled',lambda:True)
    monkeypatch.setattr(authz.cf_access,'email_from_request_headers',lambda *_:None)
    assert system.client.get('/api/run-history').status_code==403
