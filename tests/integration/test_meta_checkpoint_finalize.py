"""Current real approval/finalizer/selector regressions; no historical race claim."""
import json
import logging
import subprocess
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from skillflow import PipelineGraph
from skillflow.core import StepResult
from core.skillflow_host import AItelierSkillFlow
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager
from api import dependencies as deps, meta_routers as mr, run_routers as rr
from api.auth import get_optional_user
from core import scheduler

BRIEF = {'project_name': 'Current reviewed fixture', 'description': 'Tiny deterministic result',
         'user_stories': ['As a user I want the reviewed result'], 'goals': ['Exact retained brief']}
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def normal(tmp_path, monkeypatch):
    db = DBManager(str(tmp_path/'host.db'))
    pid = 'current-meta-proof'
    repo = tmp_path/'repo'
    repo.mkdir()
    subprocess.run(['git','init','-q',str(repo)],check=True)
    (repo/'README.md').write_text('Owned deterministic meta fixture\n')
    subprocess.run(['git','-C',str(repo),'add','README.md'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=fixture','-c','user.email=f@invalid',
                    '-c','core.hooksPath=/dev/null','commit','-qm','Fixture base'],check=True)
    db.ensure_project(pid,name='Current meta proof',repo_type='existing',repo_path=str(repo))
    db.set_project_meta_state(pid,'drafting')
    sf = AItelierSkillFlow(str(tmp_path/'sdk.db'),tool_loader=deps.get_tool_loader(),
                          workspace_base=str(tmp_path/'workspaces'),projects_base=str(tmp_path/'projects'))
    for role in ('intent_detector','meta_conversation_agent'):
        sf.register_agent_config(role,model='host',tools=[])
    sf.register_graph(PipelineGraph.from_yaml(str(REPO/'configs/meta_conversation.yaml')))
    ws = WorkspaceManager(base_path=str(tmp_path/'workspaces'))
    for module in (deps,mr,rr,scheduler):
        monkeypatch.setattr(module,'get_skillflow',lambda:sf)
    monkeypatch.setattr(deps,'get_db_manager',lambda:db)
    monkeypatch.setattr(deps,'get_workspace_manager',lambda:ws)
    monkeypatch.setattr(scheduler,'db',db)
    monkeypatch.setattr(scheduler,'ws',ws)
    monkeypatch.setattr(scheduler,'get_workspace_manager',lambda:ws)
    rid = sf.create_run('meta_conversation',project_id=pid)
    wakes = []
    monkeypatch.setattr(scheduler,'wake_scheduler',lambda *a,**k:wakes.append(sf.get_run(rid)['status']))
    sf.start_run(rid)
    root = Path(sf._workspace.get_project_path(pid))
    (root/'meta').mkdir(parents=True)
    (root/'meta/conversation.md').write_text('User: preserve this exact reviewed requirement\n')
    for sid,slot,filename,value in [('intent_detect','result','intent_result.json',{'intent':'new_project'}),
        ('gather','gather_state','gather_state.json',{'need_input':False,'brief':BRIEF})]:
        sf.advance_run(rid)
        claimed = sf.claim_next_step(rid)
        assert claimed.step_id == sid
        dest = Path(sf.output_directory(rid,sid,slot))
        dest.mkdir(parents=True,exist_ok=True)
        (dest/filename).write_text(json.dumps(value))
        sf.confirm_step(claimed.token,StepResult(outputs={slot:value}))
    sf.advance_run(rid)
    assert sf.get_run(rid)['status'] == 'paused'
    cp = sf.get_checkpoint(rid)
    app = FastAPI()
    app.include_router(mr.router)
    app.include_router(rr.router)
    app.dependency_overrides[mr.get_db_manager] = lambda:db
    app.dependency_overrides[rr.get_db_manager] = lambda:db
    for route in app.routes:
        if hasattr(route,'dependant'):
            for dependency in route.dependant.dependencies:
                if getattr(dependency.call,'__name__','') == 'get_db_manager':
                    app.dependency_overrides[dependency.call] = lambda:db
    app.dependency_overrides[get_optional_user] = lambda:None
    # Capture the callable actually held by the transport, including concurrent requests.
    requests, records, translated = [], [], []
    capture_lock = threading.Lock()
    raw = Path('/raw') if Path('/raw').is_dir() else tmp_path
    capture = raw/(tmp_path.name+'-normal-transport.json')
    from core import checkpoint_observation as observation
    from fastapi import HTTPException
    from fastapi.exception_handlers import http_exception_handler
    async def retain_error(request, exc):
        translated.append(exc)
        return await http_exception_handler(request, exc)
    app.add_exception_handler(HTTPException, retain_error)
    def save():
        with capture_lock:
            capture.write_text(json.dumps({'requests':requests,'records':records,
                'translated':[{'type':type(e).__name__,'cause':type(e.__cause__).__name__,
                'frames':[f.name for f in traceback.extract_tb(e.__cause__.__traceback__)] if e.__cause__ else []}
                for e in translated]},indent=2))
    class Retain(logging.Handler):
        def emit(self, record):
            records.append(record.checkpoint_observation)
            save()
    handler = Retain()
    observation.logger.addHandler(handler)
    class Transport:
        async def __call__(self, scope, receive, send):
            if scope['type'] != 'http':
                return await app(scope,receive,send)
            row = {'path':scope['path'],'thread':threading.get_ident()}
            requests.append(row)
            async def retain_send(message):
                if message['type']=='http.response.start': row['status']=message['status']
                await send(message)
            try:
                await app(scope,receive,retain_send)
            except BaseException as exc:
                row['exception_type']=type(exc).__name__
                row['frames']=[f.name for f in traceback.extract_tb(exc.__traceback__)]
                raise
            finally:
                save()
    transport = Transport()
    transport.product_app = app
    with TestClient(transport,raise_server_exceptions=False) as client:
        assert client._transport.app is transport
        yield sf,db,ws,pid,rid,root,client,cp,wakes
    observation.logger.removeHandler(handler)
    assert observation._current.get() is None
    if requests:
        assert capture.is_file() and capture.stat().st_size > 0
        assert json.loads(capture.read_text())['requests'] == requests


def register_dpe(normal):
    import yaml
    sf,db,ws,pid,*_ = normal
    graph = PipelineGraph.from_yaml(str(REPO/'configs/dpe_default.yaml'))
    for role in yaml.safe_load((REPO/'agent_configs/dpe_default.yaml').read_text()):
        sf.register_agent_config(role,model='host',tools=[])
    sf.register_graph(graph)
    db.update_project(pid,config_name=graph.name)
    return graph


def approve(normal, route='run', **changes):
    sf,db,ws,pid,rid,root,client,cp,wakes = normal
    body = {'run_id':rid,'checkpoint':'gather','expected_checkpoint':cp['expected_checkpoint'],
            'decision_id':'current-reviewed-decision'}
    body.update(changes)
    path = f'/api/runs/{rid}/checkpoint/approve' if route == 'run' else f'/api/meta/{pid}/checkpoint/approve'
    return client.post(path,json=body)


@pytest.mark.parametrize('route',['run','project'])
def test_current_normal_route_finalizes_then_selects_before_constructor(normal, route, tmp_path):
    sf,db,ws,pid,rid,root,client,cp,wakes = normal
    graph = register_dpe(normal)
    scheduler._sync_project_status_to_db(pid)
    before = db.get_project(pid)
    assert before['status'] == 'checkpoint:Project conversation'
    assert before['meta_state'] == 'paused'
    assert db.get_active_projects(limit=10) == []
    response = approve(normal,route)
    selected = db.get_active_projects(limit=10)
    facts = {'route':route,'before':{'status':before['status'],'meta_state':before['meta_state']},
             'http_status':response.status_code,'response':response.json(),'meta_run':sf.get_run(rid),
             'project_after':db.get_project(pid),'selected_before_constructor':[p['project_id'] for p in selected],
             'goals_exist':(root/'meta_conversation/finalize/step1_goals.json').is_file(),
             'brief_exists':(root/'project/project_brief.md').is_file(),'wakes':wakes}
    out = Path('/raw') if Path('/raw').is_dir() else tmp_path
    (out/(route+'-normal-finalize-selection.json')).write_text(json.dumps(facts,indent=2))
    assert response.status_code == 200,response.text
    assert sf.get_run(rid)['status'] == 'completed','normal approval left genuine finalize incomplete'
    assert json.loads((root/'meta_conversation/finalize/step1_goals.json').read_text())['project_name'] == BRIEF['project_name']
    assert [p['project_id'] for p in selected] == [pid], 'ordinary selector must run before constructor'
    build = scheduler._get_or_create_skillflow_run(selected[0]['project_id'])
    assert build and sf.get_run(build)['graph_name'] == graph.name
    isolation = scheduler.run_isolation.record(db,build)
    assert isolation and isolation['source_repo'] != isolation['worktree_path']
    assert approve(normal,route).status_code == 200
    assert scheduler._get_or_create_skillflow_run(pid) == build
    assert len([r for r in sf.list_runs(pid) if r['graph_name'] == graph.name]) == 1


@pytest.mark.parametrize('change',['mutate','delete'])
def test_retained_payload_drives_genuine_emit_and_completed_replay(normal, change):
    from tests.unit.test_normal_checkpoint_protocol import _approval_count
    sf,db,ws,pid,rid,root,client,cp,wakes = normal
    register_dpe(normal)
    scheduler._sync_project_status_to_db(pid)
    gather = root/'meta_conversation/gather/gather_state.json'
    if change == 'delete': gather.unlink()
    else: gather.write_text(json.dumps({'brief':{'project_name':'MUTATED','user_stories':['Wrong']}}))
    assert approve(normal).status_code == 200
    goals = root/'meta_conversation/finalize/step1_goals.json'
    assert json.loads(goals.read_text())['project_name'] == BRIEF['project_name']
    before = (goals.stat().st_mtime_ns,goals.read_bytes(),sf.get_steps(rid,include_payloads=True))
    assert approve(normal).status_code == 200
    assert (goals.stat().st_mtime_ns,goals.read_bytes(),sf.get_steps(rid,include_payloads=True)) == before
    assert _approval_count(sf,rid) == 1


@pytest.mark.parametrize('mode',['missing-token','missing-decision','wrong-hash','conflict','stale'])
def test_identity_refusal_has_no_new_finalize_or_submission(normal, mode):
    sf,db,ws,pid,rid,root,client,cp,wakes = normal
    register_dpe(normal)
    scheduler._sync_project_status_to_db(pid)
    changes={}
    if mode=='missing-token': changes['expected_checkpoint']=None
    elif mode=='missing-decision': changes['decision_id']=''
    elif mode=='wrong-hash': changes['expected_checkpoint']={**cp['expected_checkpoint'],'outputs_sha256':'f'*64}
    elif mode=='conflict':
        assert approve(normal).status_code == 200
        changes['expected_checkpoint']={**cp['expected_checkpoint'],'version':999}
    else:
        sf.reject_checkpoint(rid,'gather','Revise the actual reviewed brief')
        c=sf.claim_next_step(rid)
        dest=Path(sf.output_directory(rid,'gather','gather_state'));dest.mkdir(parents=True,exist_ok=True)
        (dest/'gather_state.json').write_text(json.dumps({'brief':{**BRIEF,'project_name':'Revision'}}))
        sf.confirm_step(c.token,StepResult(outputs={'gather_state':{'brief':{**BRIEF,'project_name':'Revision'}}}))
        sf.advance_run(rid)
    before=(sf.get_run(rid),sf.get_steps(rid,include_payloads=True),db.get_project(pid),sf.list_runs(pid))
    response=approve(normal,**changes)
    assert response.status_code==409,response.text
    assert (sf.get_run(rid),sf.get_steps(rid,include_payloads=True),db.get_project(pid),sf.list_runs(pid))==before


def test_failed_genuine_emitter_is_observed_and_cannot_start_unbriefed_build(normal,tmp_path):
    sf,db,ws,pid,rid,root,client,cp,wakes = normal
    register_dpe(normal)
    scheduler._sync_project_status_to_db(pid)
    # A real owned filesystem refusal, not a patched emitter or SDK connection.
    (root/'project').write_text('Owned fixture blocks the project directory')
    response=approve(normal)
    assert response.status_code==409,response.text
    assert sf.get_run(rid)['status'] != 'completed'
    assert not (root/'meta_conversation/finalize/step1_goals.json').exists()
    assert db.get_active_projects(limit=10)==[]
    assert scheduler._get_or_create_skillflow_run(pid) is None
    assert not [r for r in sf.list_runs(pid) if r['graph_name']=='dpe_default_v2']
    raw=Path('/raw') if Path('/raw').is_dir() else tmp_path
    observed=json.loads((raw/(tmp_path.name+'-normal-transport.json')).read_text())
    assert observed['requests'][-1]['status']==409
    assert len(observed['records'])==1
    record=observed['records'][0]
    assert record['phase']=='emit_project_artifacts' and record['translated']
    assert record['request_id'] and record['run_id']==rid
    assert record['decision_id']=='current-reviewed-decision'
    assert any(e['type']=='builtins.FileExistsError' for e in record['exceptions'])
    assert any(f['function']=='emit_project_artifacts' for e in record['exceptions'] for f in e['frames'])
    assert observed['translated'][-1]['cause']=='RuntimeError'


def later_run(normal, state):
    from skillflow.graph import StepNode,Transition
    sf,db,ws,pid,*_=normal
    name='foreign_current' if state=='foreign' else 'dpe_default_v2'
    graph=PipelineGraph(name=name,begin='1',steps=[
        StepNode(id='1',step_type='agent',checkpoint=True,checkpoint_label='Real later DPE pause',transitions=[Transition(to='2')]),
        StepNode(id='2',step_type='agent',transitions=[])])
    sf.register_graph(graph)
    db.update_project(pid,config_name='dpe_default_v2')
    rid=sf.create_run(name,project_id=pid)
    if state != 'pending':
        sf.start_run(rid)
    if state in ('paused','failed'):
        c=sf.claim_next_step(rid)
        if state=='failed': sf.fail_step(c.token,'Real later failure',retryable=False)
        else:
            sf.confirm_step(c.token,StepResult(outputs={'later':'Actual reviewed DPE work'}));sf.advance_run(rid)
    return rid


@pytest.mark.parametrize('state',['pending','running','paused','failed','foreign'])
def test_old_meta_replay_preserves_actual_later_owner(normal,state):
    sf,db,ws,pid,rid,root,client,cp,wakes = normal
    assert approve(normal).status_code == 200
    later=later_run(normal,state)
    scheduler._sync_project_status_to_db(pid)
    db.set_project_brief(pid,'Later owned brief must survive')
    if state=='paused': assert sf.get_checkpoint(later)['step_id']=='1'
    before=(db.get_project(pid),sf.get_run(later),sf.get_steps(later,include_payloads=True))
    response=approve(normal)
    assert response.status_code==(409 if state=='failed' else 200),response.text
    assert (db.get_project(pid),sf.get_run(later),sf.get_steps(later,include_payloads=True))==before


def test_ordinary_concurrent_same_decision_requests_emit_once_and_select_one_build(normal):
    from tests.unit.test_normal_checkpoint_protocol import _approval_count
    sf,db,ws,pid,rid,root,client,cp,wakes = normal
    register_dpe(normal)
    scheduler._sync_project_status_to_db(pid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(approve,normal,route) for route in ('run','project')]
        responses=[future.result(timeout=20) for future in futures]
    assert [r.status_code for r in responses]==[200,200],[r.text for r in responses]
    assert _approval_count(sf,rid)==1
    assert len([s for s in sf.get_steps(rid) if s['step_id']=='finalize'])==1
    assert sf.get_run(rid)['status']=='completed'
    selected=db.get_active_projects(limit=10)
    assert [p['project_id'] for p in selected]==[pid]
    first=scheduler._get_or_create_skillflow_run(pid)
    assert scheduler._get_or_create_skillflow_run(pid)==first
    assert len([r for r in sf.list_runs(pid) if r['graph_name']=='dpe_default_v2'])==1


@pytest.mark.asyncio
async def test_normal_butler_approval_and_old_replay_do_not_restart_later_build(normal,monkeypatch):
    from core.meta_agent import MetaAgent
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    register_dpe(normal)
    scheduler._sync_project_status_to_db(pid)
    agent=MetaAgent.__new__(MetaAgent);agent.db=db;agent.ws=ws;agent._log_error=lambda msg:None
    dispatch=[]
    async def start(args):
        # Only paid model execution is replaced; ordinary construction is real.
        selected=db.get_active_projects(limit=10)
        assert [p['project_id'] for p in selected]==[pid]
        build=scheduler._get_or_create_skillflow_run(pid)
        dispatch.append(build)
        return {'status':'controlled-paid-dispatch','run_id':build}
    monkeypatch.setattr(agent,'_tool_start_pipeline',start)
    args={'run_id':rid,'expected_checkpoint':cp['expected_checkpoint'],'decision_id':'butler-reviewed'}
    result=await agent._tool_approve_project_brief(args)
    assert result['status']=='controlled-paid-dispatch',result
    before=db.get_project(pid)
    replay=await agent._tool_approve_project_brief(args)
    assert replay['status']=='already_planned',replay
    assert len(dispatch)==1 and db.get_project(pid)==before



def test_real_meta_auth_and_state_controller_refuse_before_approval(normal,monkeypatch):
    from api.auth import CurrentUser
    from tests.unit.test_state_p3_checkpoint_entry import _bind_state_attempt, _as
    from tests.unit.test_normal_checkpoint_protocol import _approval_count
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    app = client._transport.app.product_app
    app.dependency_overrides[get_optional_user]=lambda:CurrentUser(email='foreign@invalid',display_name='Foreign',source='cloudflare')
    assert approve(normal).status_code==404
    assert sf.get_run(rid)['status']=='paused' and _approval_count(sf,rid)==0
    app.dependency_overrides[get_optional_user]=lambda:None
    _bind_state_attempt(db,rid)
    _as(monkeypatch,'grok')
    response=approve(normal)
    assert response.status_code==409 and 'not_attempt_owner' in response.text
    assert sf.get_run(rid)['status']=='paused' and _approval_count(sf,rid)==0
    _as(monkeypatch,'codex')
    assert approve(normal).status_code==200


def test_real_failed_step_explicit_rescue_keeps_existing_resume_path(normal):
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    later=later_run(normal,'paused')
    observed=sf.get_checkpoint(later)
    sf.approve_checkpoint(later,expected_checkpoint=observed['expected_checkpoint'],decision_id='later-rescue-proof')
    sf.advance_run(later)
    claimed=sf.claim_next_step(later)
    assert claimed.step_id=='2'
    sf.fail_step(claimed.token,'Owned nonretryable fixture failure',retryable=False)
    assert sf.get_run(later)['status']=='failed'
    response=client.post(f'/api/runs/{later}/checkpoint/approve',json={
        'run_id':later,'checkpoint':'1','rescue_failed':True})
    assert response.status_code==200,response.text
    assert response.json()['status']=='rescued'
    assert sf.get_run(later)['status']=='running'
    assert sf.get_run(rid)['status']=='paused'
    assert not (root/'meta_conversation/finalize/step1_goals.json').exists()


def _schema(db):
    import sqlite3
    with sqlite3.connect(db.db_path) as conn:
        return conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()


def _state_schema_names(tmp_path):
    # The objects a trusted store's constructor creates, from the real initializer.
    from core.state_graph import initialize_graph_schema
    scratch = DBManager(str(tmp_path/'scratch-state.db'))
    plain = {row[1] for row in _schema(scratch)}
    initialize_graph_schema(scratch)
    return {row[1] for row in _schema(scratch)} - plain


@pytest.mark.parametrize('route',['run','project'])
def test_ordinary_controller_request_runs_no_state_schema_ddl(normal, route, tmp_path):
    # The ownership check on an ordinary request reads; it neither creates nor
    # migrates State schema on the database the route was handed. A run no
    # State attempt binds stays an unmanaged run on an unmanaged database.
    sf,db,ws,pid,rid,root,client,cp,wakes = normal
    names = _state_schema_names(tmp_path)
    assert 'state_nodes' in names and 'state_project_enforcement' in names
    before = _schema(db)
    assert not names & {row[1] for row in before}
    response = approve(normal, route)
    assert response.status_code == 200, response.text
    after = _schema(db)
    assert not names & {row[1] for row in after}
    assert [row for row in after if row[1].startswith('state_')] == []
    assert sf.get_run(rid)['status'] == 'completed'


def test_initialized_state_controller_requests_keep_schema_and_owner(normal, monkeypatch):
    from tests.unit.test_state_p3_checkpoint_entry import _bind_state_attempt, _as
    from tests.unit.test_normal_checkpoint_protocol import _approval_count
    sf,db,ws,pid,rid,root,client,cp,wakes = normal
    _bind_state_attempt(db, rid)
    initialized = _schema(db)
    assert 'state_attempts' in {row[1] for row in initialized}
    _as(monkeypatch, 'grok')
    response = approve(normal)
    assert response.status_code == 409 and 'not_attempt_owner' in response.text
    assert _approval_count(sf, rid) == 0 and _schema(db) == initialized
    _as(monkeypatch, 'codex')
    assert approve(normal, 'project').status_code == 200
    assert _approval_count(sf, rid) == 1 and _schema(db) == initialized
