import json
import subprocess
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

BRIEF={'project_name':'Reviewed exact fixture','description':'A tiny approved brief',
       'user_stories':['As a user I want a tiny useful result'], 'goals':['Exact retained brief']}

@pytest.fixture
def normal(tmp_path,monkeypatch,request):
    db=DBManager(str(tmp_path/'host.db'));pid='meta-finalize-real-fixture'
    repo=tmp_path/'repo';repo.mkdir()
    subprocess.run(['git','init','-q',str(repo)],check=True)
    (repo/'README.md').write_text('Owned meta finalization fixture\n')
    subprocess.run(['git','-C',str(repo),'add','README.md'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=fixture','-c','user.email=f@invalid','-c','core.hooksPath=/dev/null','commit','-qm','Fixture base'],check=True)
    db.ensure_project(pid,name='Meta finalize real fixture',repo_type='existing',repo_path=str(repo))
    db.set_project_meta_state(pid,'drafting')
    sf=AItelierSkillFlow(str(tmp_path/'engine.db'),tool_loader=deps.get_tool_loader(),
        workspace_base=str(tmp_path/'workspaces'),projects_base=str(tmp_path/'projects'))
    for role in ('intent_detector','meta_conversation_agent'):sf.register_agent_config(role,model='host',tools=[])
    sf.register_graph(PipelineGraph.from_yaml(str(Path(__file__).resolve().parents[2]/'configs/meta_conversation.yaml')))
    ws=WorkspaceManager(base_path=str(tmp_path/'workspaces'))
    for mod in (deps,mr,rr,scheduler):monkeypatch.setattr(mod,'get_skillflow',lambda:sf)
    monkeypatch.setattr(deps,'get_workspace_manager',lambda:ws)
    monkeypatch.setattr(deps,'get_db_manager',lambda:db)
    monkeypatch.setattr(scheduler,'db',db)
    wakes=[];monkeypatch.setattr(scheduler,'wake_scheduler',lambda *a,**k:wakes.append(sf.get_run(rid)['status']))
    rid=sf.create_run('meta_conversation',project_id=pid);sf.start_run(rid)
    root=Path(sf._workspace.get_project_path(pid));(root/'meta').mkdir(parents=True)
    (root/'meta/conversation.md').write_text('User: preserve this reviewed requirement\n')
    for sid,slot,filename,value in [('intent_detect','result','intent_result.json',{'intent':'new_project'}),
        ('gather','gather_state','gather_state.json',{'need_input':False,'brief':getattr(request,'param',BRIEF)})]:
        sf.advance_run(rid);c=sf.claim_next_step(rid);assert c.step_id==sid
        dest=Path(sf.output_directory(rid,sid,slot));dest.mkdir(parents=True,exist_ok=True)
        (dest/filename).write_text(json.dumps(value))
        sf.confirm_step(c.token,StepResult(outputs={slot:value}))
    sf.advance_run(rid);assert sf.get_run(rid)['status']=='paused'
    cp=sf.get_checkpoint(rid)
    app=FastAPI();app.include_router(mr.router);app.include_router(rr.router)
    app.dependency_overrides[mr.get_db_manager]=lambda:db
    app.dependency_overrides[rr.get_db_manager]=lambda:db
    for route in app.routes:
        if hasattr(route,'dependant'):
            for dep in route.dependant.dependencies:
                if getattr(dep.call,'__name__','')=='get_db_manager':app.dependency_overrides[dep.call]=lambda:db
    app.dependency_overrides[get_optional_user]=lambda:None
    assert db.get_project(pid), 'own fixture project missing'
    wakes.clear()
    with TestClient(app,raise_server_exceptions=False)as client:
        yield sf,db,ws,pid,rid,root,client,cp,wakes


def test_normal_run_route_completes_real_finalize(normal):
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    response=client.post(f'/api/runs/{rid}/checkpoint/approve',json={
        'checkpoint':'gather','expected_checkpoint':cp['expected_checkpoint'],'decision_id':'actual-rest-meta-decision'})
    print(json.dumps({'response':response.json(),'run':sf.get_run(rid),'wakes':wakes},default=str))
    assert response.status_code==200,response.text
    assert sf.get_run(rid)['status']=='completed','approved REST meta checkpoint left genuine finalize pending'
    goals=json.loads((root/'meta_conversation/finalize/step1_goals.json').read_text())
    assert goals['project_name']==BRIEF['project_name']
    assert 'Reviewed exact fixture' in (root/'project/project_brief.md').read_text()
    assert 'preserve this reviewed requirement' in (root/'project/spec.md').read_text()
    assert wakes and all(s=='completed'for s in wakes),'normal build scheduling happened before actual finalize'
    assert db.get_project(pid)['meta_state'] is None


def approve(normal, **changes):
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    body={'checkpoint':'gather','expected_checkpoint':cp['expected_checkpoint'],'decision_id':'retained-decision'}
    body.update(changes)
    return client.post(f'/api/runs/{rid}/checkpoint/approve',json=body)


def test_retained_payload_survives_deleted_mutable_file_and_replay(normal):
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    (root/'meta_conversation/gather/gather_state.json').unlink()
    response=approve(normal)
    assert response.status_code==200,response.text
    goals=root/'meta_conversation/finalize/step1_goals.json'
    before=goals.read_bytes();count=len(wakes)
    assert json.loads(before)['project_name']==BRIEF['project_name']
    assert approve(normal).status_code==200
    assert goals.read_bytes()==before
    assert len(wakes)==count
    assert sf.get_run(rid)['status']=='completed'


def test_wrong_token_and_conflicting_decision_have_no_effect(normal):
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    bad=dict(cp['expected_checkpoint']);bad['step_instance_id']+=1000
    assert approve(normal,expected_checkpoint=bad).status_code==409
    assert sf.get_run(rid)['status']=='paused'
    assert not wakes
    assert not (root/'meta_conversation/finalize/step1_goals.json').exists()
    assert approve(normal).status_code==200
    before=len(wakes)
    assert approve(normal,decision_id='conflicting').status_code==409
    assert len(wakes)==before


def test_failed_finalize_never_schedules_unbriefed_build(normal):
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    (root/'project/project_brief.md').mkdir(parents=True)
    response=approve(normal)
    assert response.status_code==409,response.text
    assert sf.get_run(rid)['status']!='completed'
    assert not wakes
    assert db.get_project(pid)['meta_state']=='drafting'
    assert not (root/'meta_conversation/finalize/step1_goals.json').exists()


def test_project_route_completes_same_actual_finalize(normal):
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    response=client.post(f'/api/meta/{pid}/checkpoint/approve',json={
        'run_id':rid,'checkpoint':'gather','expected_checkpoint':cp['expected_checkpoint'],'decision_id':'project-route'})
    assert response.status_code==200,response.text
    assert sf.get_run(rid)['status']=='completed'
    assert json.loads((root/'meta_conversation/finalize/step1_goals.json').read_text())['project_name']==BRIEF['project_name']
    assert wakes==['completed']


def test_normal_scheduler_creates_one_isolated_dpe_only_after_finalize(normal,monkeypatch):
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    import yaml
    config=str(Path(__file__).resolve().parents[2]/'configs/dpe_default.yaml')
    graph=PipelineGraph.from_yaml(config)
    roles=yaml.safe_load((Path(__file__).resolve().parents[2]/'agent_configs/dpe_default.yaml').read_text())
    for role in roles:
        sf.register_agent_config(role,model='host',tools=[])
    sf.register_graph(graph)
    db.update_project(pid,config_name=graph.name)
    monkeypatch.setattr(scheduler,'get_workspace_manager',lambda:ws)
    assert scheduler._get_or_create_skillflow_run(pid) is None
    assert approve(normal).status_code==200
    build=scheduler._get_or_create_skillflow_run(pid)
    assert build, sf.list_runs(project_id=pid)
    assert build!=rid
    isolation=scheduler.run_isolation.record(db,build)
    assert isolation and isolation['source_repo']!=isolation['worktree_path']
    assert sf.get_run(build)['graph_name']==graph.name
    assert sf.get_run(build)['status']=='running'
    assert scheduler._get_or_create_skillflow_run(pid)==build
    assert approve(normal).status_code==200
    assert scheduler._get_or_create_skillflow_run(pid)==build
    assert len([r for r in sf.list_runs(project_id=pid) if r['graph_name']==graph.name])==1

