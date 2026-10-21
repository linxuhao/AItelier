"""Current real approval/finalizer/selector regressions; no historical race claim."""
import json
import logging
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
    for route in app.routes:
        if hasattr(route,'dependant'):
            for dependency in route.dependant.dependencies:
                if getattr(dependency.call,'__name__','') == 'get_db_manager':
                    app.dependency_overrides[dependency.call] = lambda:db
    app.dependency_overrides[get_optional_user] = lambda:None
    with TestClient(app,raise_server_exceptions=False) as client:
        yield sf,db,ws,pid,rid,root,client,cp,wakes


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
