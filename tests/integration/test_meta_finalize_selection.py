import json
from pathlib import Path
import pytest
from skillflow import PipelineGraph
from skillflow.core import StepResult
from tests.integration.test_meta_checkpoint_finalize import normal, approve, BRIEF
from core import scheduler


def register_dpe(sf,db,ws,pid,monkeypatch):
    import yaml
    graph=PipelineGraph.from_yaml(str(Path(__file__).resolve().parents[2]/'configs/dpe_default.yaml'))
    for role in yaml.safe_load((Path(__file__).resolve().parents[2]/'agent_configs/dpe_default.yaml').read_text()):
        sf.register_agent_config(role,model='host',tools=[])
    sf.register_graph(graph)
    db.update_project(pid,config_name=graph.name)
    monkeypatch.setattr(scheduler,'get_workspace_manager',lambda:ws)
    monkeypatch.setattr(scheduler,'ws',ws)
    return graph


@pytest.mark.parametrize('route',['run','project'])
def test_synced_checkpoint_selects_then_constructs_one_dpe(normal,monkeypatch,route):
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    graph=register_dpe(sf,db,ws,pid,monkeypatch)
    scheduler._sync_project_status_to_db(pid)
    before=db.get_project(pid)
    assert before['status']=='checkpoint:Project conversation'
    assert before['meta_state']=='paused'
    assert db.get_active_projects(limit=10)==[]
    path=f'/api/runs/{rid}/checkpoint/approve' if route=='run' else f'/api/meta/{pid}/checkpoint/approve'
    body={'run_id':rid,'checkpoint':'gather','expected_checkpoint':cp['expected_checkpoint'],'decision_id':'normal-selector'}
    response=client.post(path,json=body)
    assert response.status_code==200,response.text
    assert sf.get_run(rid)['status']=='completed'
    selected=db.get_active_projects(limit=10)
    print(json.dumps({'route':route,'before':before['status'],'after':db.get_project(pid)['status'],'selected':[p['project_id']for p in selected]}))
    assert [p['project_id']for p in selected]==[pid], 'genuine completed meta remains ineligible for normal poller'
    build=scheduler._get_or_create_skillflow_run(selected[0]['project_id'])
    assert build and sf.get_run(build)['graph_name']==graph.name
    assert sf.get_run(build)['status']=='running'
    assert client.post(path,json=body).status_code==200
    assert scheduler._get_or_create_skillflow_run(pid)==build
    assert len([r for r in sf.list_runs(project_id=pid)if r['graph_name']==graph.name])==1
    assert wakes==['completed']


def test_genuine_paused_dpe_projection_survives_meta_replay(normal,monkeypatch):
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    graph=register_dpe(sf,db,ws,pid,monkeypatch)
    assert approve(normal).status_code==200
    build=scheduler._get_or_create_skillflow_run(pid)
    assert build
    sf.advance_run(build)
    claimed=sf.claim_next_step(build)
    assert claimed.step_id=='1'
    dest=Path(sf.output_directory(build,'1','sota'));dest.mkdir(parents=True,exist_ok=True)
    (dest/'step1_sota.md').write_text('Private deterministic SOTA checkpoint fixture. No model executed.\n')
    sf.confirm_step(claimed.token,StepResult(outputs={'sota':'Private deterministic SOTA checkpoint fixture.'}))
    sf.advance_run(build)
    assert sf.get_run(build)['status']=='paused'
    scheduler._sync_project_status_to_db(pid)
    before=db.get_project(pid)
    assert before['status']=='checkpoint:SOTA / Codebase Analysis Review'
    assert before['meta_state']=='paused'
    wake_count=len(wakes)
    assert approve(normal).status_code==200
    after=db.get_project(pid)
    for key in ('status','meta_state','current_project_step','completed_project_steps','brief'):
        assert after[key]==before[key],key
    assert sf.get_run(build)['status']=='paused'
    assert scheduler._get_or_create_skillflow_run(pid)==build
    assert len(wakes)==wake_count


@pytest.mark.parametrize('state',['pending','running','failed'])
def test_existing_primary_build_keeps_its_own_projection(normal,monkeypatch,state):
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    graph=register_dpe(sf,db,ws,pid,monkeypatch)
    assert approve(normal).status_code==200
    build=sf.create_run(graph.name,project_id=pid)
    if state!='pending':sf.start_run(build)
    if state=='failed':sf.fail_run(build,'Owned actual failed DPE control')
    scheduler._sync_project_status_to_db(pid)
    before=db.get_project(pid);wake_count=len(wakes)
    response=approve(normal)
    assert response.status_code==(409 if state=='failed' else 200),response.text
    after=db.get_project(pid)
    for key in ('status','meta_state','current_project_step','completed_project_steps','brief'):
        assert after[key]==before[key],key
    assert sf.get_run(build)['status']==state
    assert len(wakes)==wake_count


def test_current_foreign_run_is_not_reset_by_completed_meta(normal,monkeypatch):
    from skillflow.graph import StepNode
    sf,db,ws,pid,rid,root,client,cp,wakes=normal
    monkeypatch.setattr(scheduler,'ws',ws)
    assert approve(normal).status_code==200
    graph=PipelineGraph(name='private_foreign_control',begin='work',steps=[StepNode(id='work',step_type='agent')])
    sf.register_graph(graph)
    foreign=sf.create_run(graph.name,project_id=pid);sf.start_run(foreign)
    scheduler._sync_project_status_to_db(pid)
    before=db.get_project(pid);wake_count=len(wakes)
    assert before['status']=='running:work'
    assert approve(normal).status_code==200
    after=db.get_project(pid)
    for key in ('status','meta_state','current_project_step','completed_project_steps','brief'):
        assert after[key]==before[key],key
    assert sf.get_run(foreign)['status']=='running'
    assert len(wakes)==wake_count
