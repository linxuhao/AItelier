"""Real engine/SQLite usage projection; no fake completed goal or dispatch."""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode

from api.state_http import create_state_router
from api.state_only import create_app
from core.db_manager import DBManager
from core.state_database import StateDatabase
from core.state_service import StateService
from core.state_graph import StateNotFound
from core.state_commands import execute


def spec(key):
    return {'key':key,'goal':'Goal '+key,'acceptance':[{'id':'c','kind':'test','description':'Required real test'}]}


@pytest.fixture
def system(tmp_path,monkeypatch):
    monkeypatch.setenv('AITELIER_HOME',str(tmp_path/'home'))
    sf=SkillFlow(str(tmp_path/'engine.sqlite'))
    sf.register_graph(PipelineGraph(name='feature',begin='work',steps=[StepNode(id='work')]))
    db=DBManager(str(tmp_path/'host.sqlite'));service=StateService(db,sf=sf)
    service.create_project('game','Game');service.create_project('other','Other')
    service.store.add_nodes('game',[spec('a'),spec('b')]);service.store.add_nodes('other',[spec('other')])
    yield SimpleNamespace(sf=sf,db=db,s=service,tmp=tmp_path)
    sf._conn.close()


def make_run(system,status='running',project='shared-exec'):
    sf=system.sf;rid=sf.create_run('feature',project_id=project)
    if status!='pending':sf.start_run(rid)
    if status=='paused':sf.pause_run(rid)
    elif status=='failed':sf.fail_run(rid,'fixture failure')
    elif status=='completed':
        sf.advance_run(rid);claim=sf.claim_next_step(rid);sf.confirm_step(claim.token,StepResult());sf.advance_run(rid)
    assert sf.get_run(rid)['status']==status
    return rid


def link(system,rid,key='a',reference=None,project='game'):
    return system.s.portfolio.add_reference(project,key,reference or 'ref-'+rid,'run',rid,
        'Historical association, not acceptance','fixture',protect=False)


def usage(system,rid,prompt=None,completion=None,hit=None,miss=None):
    system.sf.trace(rid,'usage','token_usage',{'prompt_tokens':prompt,'completion_tokens':completion,
        'cache_hit_tokens':hit,'cache_miss_tokens':miss})


def test_all_runs_counted_only_running_listed_and_exact_ids_deduplicated(system):
    a=system.s.attempts.reserve('game','a',1,'feature','attempt-1')
    bound=make_run(system,project=a['execution_project_id']);system.s.attempts.bind_run(a['attempt_id'],bound,system.sf)
    link(system,bound,reference='duplicate-attempt');link(system,bound,key='b',reference='duplicate-second-node')
    ids=[bound]
    for status in ['completed','failed','paused','pending']:
        rid=make_run(system,status);ids.append(rid);link(system,rid,key='b')
    unrelated=make_run(system,'running');link(system,unrelated,project='other',key='other')
    for rid in ids:usage(system,rid,100,20,75,25)
    usage(system,unrelated,900000,10,900000,0)
    out=system.s.project_run_summary('game')
    assert out['counts']=={'total':5,'running':1,'finished':1,'failed':1,'other':2,'unavailable':0}
    assert [r['run_id'] for r in out['running_runs']]==[bound]
    assert out['running_runs'][0]['node_keys']==['a','b']
    assert out['usage']['total_tokens']==600
    assert out['usage']['cache_hit_tokens']==375
    assert out['usage']['cache_hit_ratio']==.75
    assert out['usage']['partial'] is False
    # Non-running run IDs/payloads are not returned to the graph panel.
    assert all(rid not in json.dumps(out) for rid in ids[1:]+[unrelated])


def test_prompt_plus_completion_and_weighted_cache_not_average_or_double_count(system):
    first=make_run(system);second=make_run(system,'failed');link(system,first);link(system,second)
    usage(system,first,100,10,100,0)
    usage(system,second,900,90,0,900)
    # No cache telemetry must not count as a 100% cache miss.
    usage(system,first,1000,50,None,None)
    out=system.s.project_run_summary('game')['usage']
    assert out['prompt_tokens']==2000 and out['completion_tokens']==150
    assert out['total_tokens']==2150
    assert out['cache_hit_tokens']==100 and out['cache_hit_ratio']==.1
    assert out['cache_covered_tokens']==1000
    assert out['cache_reported_turns']==2 and out['usage_turns']==3


def test_unknown_tokens_and_explicit_zero_are_different(system):
    rid=make_run(system);link(system,rid)
    before=system.s.project_run_summary('game')['usage']
    assert before['total_tokens'] is None and before['cache_hit_ratio'] is None
    assert before['runs_without_token_usage']==1 and before['partial'] is True
    usage(system,rid,0,0,0,0)
    after=system.s.project_run_summary('game')['usage']
    assert after['total_tokens']==0 and after['cache_hit_tokens']==0
    assert after['cache_hit_ratio'] is None and after['partial'] is False


def test_unreported_negative_boolean_malformed_counters_are_not_fabricated(system):
    rid=make_run(system);link(system,rid)
    usage(system,rid,10,2,8,2)
    usage(system,rid,5,None,None,None)
    usage(system,rid,-20,1,-1,0)
    usage(system,rid,True,1,False,True)
    system.sf.trace(rid,'usage','another-event',{'prompt_tokens':100000,'completion_tokens':100})
    out=system.s.project_run_summary('game')['usage']
    assert out['total_tokens']==12 and out['token_reported_turns']==1
    assert out['cache_hit_tokens']==8 and out['cache_reported_turns']==1
    assert out['partial'] is True


def test_projection_reads_actual_status_not_stale_attempt_state_and_never_reconciles(system,monkeypatch):
    a=system.s.attempts.reserve('game','a',1,'feature','r')
    rid=make_run(system,project=a['execution_project_id']);system.s.attempts.bind_run(a['attempt_id'],rid,system.sf)
    system.sf.fail_run(rid,'real newer engine state')
    before=system.s.store.get_graph('game');events=system.s.store.events('game')
    def forbidden(*args,**kwargs):raise AssertionError('Read must never reconcile/dispatch')
    monkeypatch.setattr(system.s,'reconcile_attempt',forbidden)
    monkeypatch.setattr(system.sf,'start_run',forbidden)
    result=system.s.project_run_summary('game')
    assert result['counts']['failed']==1 and result['running_runs']==[]
    assert system.s.attempts.get(a['attempt_id'])['status']=='running'
    assert system.s.store.get_graph('game')==before and system.s.store.events('game')==events


def test_external_only_state_does_not_initialize_optional_executor(tmp_path):
    def forbidden():raise AssertionError('No runtime should be initialized for an external-only project')
    service=StateService(StateDatabase(str(tmp_path/'bare.sqlite')),runtime_factory=forbidden)
    service.create_project('game','Game');service.store.add_nodes('game',[spec('a')])
    service.start_external_attempt('game','a',1,'own-harness','job','req')
    out=service.project_run_summary('game')
    assert out['counts']['total']==0 and out['usage']['total_tokens']==0
    assert out['usage']['cache_hit_ratio'] is None
    assert out['external_attempts_excluded']==1
    with service.db.get_connection() as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone()


def test_missing_run_or_unavailable_runtime_is_not_finished_failed_or_zero_activity(system):
    link(system,'missing-exact-run')
    out=system.s.project_run_summary('game')
    assert out['counts']['total']==out['counts']['unavailable']==1
    assert out['counts']['finished']==out['counts']['failed']==0
    offline=StateService(system.db)
    out=offline.project_run_summary('game')
    assert out['runtime_unavailable'] is True and out['counts']['unavailable']==1
    assert out['usage']['total_tokens'] is None


def test_usage_reader_failure_does_not_hide_running_run(system,monkeypatch):
    rid=make_run(system);link(system,rid)
    monkeypatch.setattr(system.sf,'trace_query',lambda *_:(_ for _ in ()).throw(OSError('secret internal path')))
    out=system.s.project_run_summary('game')
    assert out['counts']['running']==1 and out['running_runs'][0]['run_id']==rid
    assert out['usage']['usage_errors']==1 and out['usage']['total_tokens'] is None
    assert 'secret internal path' not in json.dumps(out)


def test_summary_is_not_limited_to_first_reference_page(system):
    for i in range(105):
        rid=make_run(system,'failed',project='exec-'+str(i));link(system,rid,reference='r'+str(i))
    assert system.s.project_run_summary('game')['counts']['total']==105
    assert system.s.project_run_summary('game')['counts']['failed']==105


def test_shared_rest_mcp_command_contract_and_unknown_project(system):
    app=FastAPI();app.include_router(create_state_router(lambda:system.s,lambda:None))
    with TestClient(app) as c:
        assert c.get('/api/state/projects/game/run-summary').json()['counts']['total']==0
        response=c.post('/api/state/query/project_run_summary',json={'project_id':'game'})
        assert response.status_code==200 and response.json()['running_runs']==[]
        assert c.get('/api/state/projects/not-a-project/run-summary').status_code==404
        assert c.post('/api/state/query/project_run_summary',json={'project_id':'game','run_id':'foreign'}).status_code==422
    assert execute(system.s,'project_run_summary',{'project_id':'game'})['counts']['total']==0
    with pytest.raises(StateNotFound):system.s.project_run_summary('absent')


def test_standalone_read_auth_and_no_runtime_for_empty_project(tmp_path):
    token='private-state-fixture-'+'x'*40
    app=create_app(str(tmp_path/'state.sqlite'),token)
    app.state.state_service.create_project('game','Private')
    with TestClient(app) as c:
        assert c.get('/api/state/projects/game/run-summary').status_code==401
        assert c.get('/api/state/projects/game/run-summary',headers={'Authorization':'Bearer '+token}).json()['counts']['total']==0


def test_usage_queries_follow_per_execution_trace_database(tmp_path,monkeypatch):
    monkeypatch.setenv('AITELIER_HOME',str(tmp_path/'home'))
    sf=SkillFlow(str(tmp_path/'engine.sqlite'),trace_db_path=str(tmp_path/'traces'))
    sf.register_graph(PipelineGraph(name='feature',begin='work',steps=[StepNode(id='work')]))
    service=StateService(DBManager(str(tmp_path/'host.sqlite')),sf=sf)
    service.create_project('game','Game');service.store.add_nodes('game',[spec('a')])
    rid=sf.create_run('feature',project_id='per-project');sf.start_run(rid)
    service.portfolio.add_reference('game','a','ref','run',rid,'Actual relation','fixture',protect=False)
    sf.trace(rid,'usage','token_usage',{'prompt_tokens':90,'completion_tokens':10,'cache_hit_tokens':60,'cache_miss_tokens':30},project_id='per-project')
    assert (tmp_path/'traces/per-project/trace.db').exists()
    result=service.project_run_summary('game')
    assert result['usage']['total_tokens']==100 and result['usage']['cache_hit_ratio']==2/3
    sf._conn.close()


def test_invalid_json_trace_does_not_remove_valid_usage(system):
    rid=make_run(system);link(system,rid);usage(system,rid,30,7,None,None)
    # Legacy corruption fixture; normal trace() serializes valid JSON.
    usage(system,rid,50,5,None,None)
    system.sf._conn.execute("UPDATE skillflow_trace SET payload_json='invalid{' WHERE run_id=? AND seq=(SELECT MAX(seq) FROM skillflow_trace WHERE run_id=? AND category='usage' AND event='token_usage')",(rid,rid))
    system.sf._conn.commit()
    result=system.s.project_run_summary('game')['usage']
    assert result['total_tokens']==37 and result['token_reported_turns']==1
    assert result['usage_turns']==2 and result['partial'] is True


def test_full_host_summary_keeps_writer_authorization(system,monkeypatch):
    from api import authz
    from api.state_graph_routers import router,get_service
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_service]=lambda:system.s
    monkeypatch.setattr(authz,'gate_enabled',lambda:True)
    monkeypatch.setattr(authz.cf_access,'email_from_request_headers',lambda *_:None)
    with TestClient(app) as c:
        assert c.get('/api/state/projects/game/run-summary').status_code==403
        assert c.post('/api/state/query/project_run_summary',json={'project_id':'game'}).status_code==403
