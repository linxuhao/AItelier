import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from skillflow import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.core import StepResult
from api import dependencies as deps, meta_routers as mr, run_routers as rr, mcp_router
from api.auth import get_optional_user
from api.authz import require_reader
from core.db_manager import DBManager

PID = 'normal-checkpoint-proof'

@pytest.fixture
def live(tmp_path, monkeypatch):
    sf=SkillFlow(str(tmp_path/'sdk.db'),workspace_base=str(tmp_path/'ws'),projects_base=str(tmp_path/'projects'))
    graph=PipelineGraph(name='normal_checkpoint_proof',begin='plan',steps=[
        StepNode(id='plan',step_type='agent',checkpoint=True,checkpoint_label='Retained plan',transitions=[Transition(to='implement')]),
        StepNode(id='implement',step_type='agent',transitions=[])])
    sf.register_graph(graph)
    rid=sf.create_run(graph.name, project_id=PID);sf.start_run(rid)
    claim=sf.claim_next_step(rid);sf.confirm_step(claim.token,StepResult(outputs={'proposal':'reviewed text'}));sf.advance_run(rid)
    assert sf.get_run(rid)['status']=='paused'
    db=DBManager(str(tmp_path/'host.db'));db.ensure_project(PID)
    assert sf.get_run(rid)['project_id']==PID, sf.get_run(rid)
    assert db.get_project(PID)
    for module in [deps,mr,rr]:monkeypatch.setattr(module,'get_skillflow',lambda:sf)
    monkeypatch.setattr(deps,'get_db_manager',lambda:db)
    import core.scheduler as scheduler
    monkeypatch.setattr(scheduler,'wake_scheduler',lambda:None)
    monkeypatch.setattr(scheduler,'_sync_project_status_to_db',lambda pid:None)
    app=FastAPI();app.include_router(mr.router);app.include_router(rr.router)
    app.dependency_overrides[mr.get_db_manager]=lambda:db
    app.dependency_overrides[rr.get_db_manager]=lambda:db
    # Imported dependency identity differs only when monkeypatched after route declaration.
    for route in app.routes:
        if hasattr(route,'dependant'):
            for dep in route.dependant.dependencies:
                if getattr(dep.call,'__name__','')=='get_db_manager':app.dependency_overrides[dep.call]=lambda:db
    app.dependency_overrides[get_optional_user]=lambda:None
    app.dependency_overrides[require_reader]=lambda:None
    client=TestClient(app,raise_server_exceptions=False)
    captured={}
    def tool(name,kind,description):
        def deco(fn):captured[name]=fn;return fn
        return deco
    mcp_router._register_run_tools(tool)
    yield sf,rid,client,captured


def test_http_manual_decision_carries_the_observed_identity(live):
    sf,rid,client,tools=live
    observed=sf.get_checkpoint(rid)
    body={'run_id':rid,'checkpoint':'plan','expected_checkpoint':observed['expected_checkpoint'],'decision_id':'http-real-sdk-proof'}
    response=client.post(f'/api/runs/{rid}/checkpoint/approve',json=body)
    assert response.status_code==200,(response.text, sf.get_run(rid), client.app.dependency_overrides)
    assert sf.get_run(rid)['status']=='running'


def test_mcp_manual_decision_carries_the_observed_identity(live):
    sf,rid,client,tools=live
    observed=sf.get_checkpoint(rid)
    # A new schema must accept the original SDK identity rather than refresh it.
    try:
        result=tools['answer_checkpoint'](rid,expected_checkpoint=observed['expected_checkpoint'],decision_id='mcp-real-sdk-proof')
    except TypeError:
        result=tools['answer_checkpoint'](rid)
    assert 'error' not in result,result
    assert sf.get_run(rid)['status']=='running'


def _body(sf, rid, decision='observed-decision'):
    cp = sf.get_checkpoint(rid)
    return {'run_id': rid, 'checkpoint': cp['step_id'],
            'expected_checkpoint': cp['expected_checkpoint'], 'decision_id': decision}


def _approval_count(sf, rid):
    with sf._ro() as conn:
        return conn.execute("SELECT count(*) FROM skillflow_outbox WHERE event_type='checkpoint_approved' AND json_extract(payload_json,'$.run_id')=?", (rid,)).fetchone()[0]


def _sibling(sf):
    rid = sf.create_run('normal_checkpoint_proof', project_id=PID)
    sf.start_run(rid)
    claim = sf.claim_next_step(rid)
    sf.confirm_step(claim.token, StepResult(outputs={'proposal': 'new sibling'}))
    sf.advance_run(rid)
    return rid


def test_exact_read_and_project_decision_do_not_retarget_a_newer_sibling(live):
    sf, rid, client, tools = live
    body = _body(sf, rid)
    newer = _sibling(sf)
    response = client.get(f'/api/runs/{rid}/checkpoint')
    assert response.status_code == 200, response.text
    view = response.json()
    assert view['run_id'] == rid
    assert view['expected_checkpoint'] == body['expected_checkpoint']
    assert view['outputs'] == {'proposal': 'reviewed text'}
    assert view['step_output']['files'] == {'proposal': 'reviewed text'}
    assert view['decision_id'] == client.get(f'/api/runs/{rid}/checkpoint').json()['decision_id']
    response = client.post(f'/api/meta/{PID}/checkpoint/approve', json=body)
    assert response.status_code == 200, response.text
    assert sf.get_run(rid)['status'] == 'running'
    assert sf.get_run(newer)['status'] == 'paused'


def test_same_decision_replays_after_running_and_completion_without_new_events(live):
    sf, rid, client, tools = live
    body = _body(sf, rid)
    path = f'/api/runs/{rid}/checkpoint/approve'
    for _ in range(2):
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
    assert _approval_count(sf, rid) == 1
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)
    sf.confirm_step(claimed.token, StepResult(outputs={'done': True}))
    sf.advance_run(rid)
    assert sf.get_run(rid)['status'] == 'completed'
    assert client.post(path, json=body).status_code == 200
    assert _approval_count(sf, rid) == 1
    assert len([s for s in sf.get_steps(rid) if s['step_id'] == 'implement']) == 1


@pytest.mark.parametrize('mutation', ['missing-token', 'missing-decision', 'blank-decision',
    'extra-token-key', 'bool-version', 'bad-hash', 'foreign-token', 'foreign-route-body'])
def test_missing_malformed_and_foreign_decisions_refuse_before_effects(live, mutation):
    sf, rid, client, tools = live
    body = _body(sf, rid)
    if mutation == 'missing-token': body.pop('expected_checkpoint')
    elif mutation == 'missing-decision': body.pop('decision_id')
    elif mutation == 'blank-decision': body['decision_id'] = ' '
    elif mutation == 'extra-token-key': body['expected_checkpoint']['extra'] = 1
    elif mutation == 'bool-version': body['expected_checkpoint']['version'] = True
    elif mutation == 'bad-hash': body['expected_checkpoint']['outputs_sha256'] = 'f' * 63
    elif mutation == 'foreign-token': body['expected_checkpoint'] = sf.get_checkpoint(_sibling(sf))['expected_checkpoint']
    else: body['run_id'] = _sibling(sf)
    response = client.post(f'/api/runs/{rid}/checkpoint/approve', json=body)
    assert response.status_code in (409, 422), response.text
    assert sf.get_run(rid)['status'] == 'paused'
    assert _approval_count(sf, rid) == 0


def test_stale_revision_refuses_and_preserves_the_new_checkpoint(live):
    sf, rid, client, tools = live
    body = _body(sf, rid)
    sf.reject_checkpoint(rid, 'plan', 'Revise the actual retained plan')
    claim = sf.claim_next_step(rid)
    sf.confirm_step(claim.token, StepResult(outputs={'proposal': 'revised'}))
    sf.advance_run(rid)
    current = sf.get_checkpoint(rid)
    assert current['expected_checkpoint'] != body['expected_checkpoint']
    response = client.post(f'/api/runs/{rid}/checkpoint/approve', json=body)
    assert response.status_code == 409 and 'Stale' in response.text
    assert sf.get_checkpoint(rid) == current
    assert _approval_count(sf, rid) == 0


def test_mcp_replay_and_collision_are_not_inferred_from_global_running_status(live):
    sf, rid, client, tools = live
    body = _body(sf, rid)
    args = dict(expected_checkpoint=body['expected_checkpoint'], decision_id=body['decision_id'])
    for _ in range(2):
        result = tools['answer_checkpoint'](rid, **args)
        assert 'error' not in result, result
    collision = {**args, 'expected_checkpoint': {**args['expected_checkpoint'], 'version': 999}}
    result = tools['answer_checkpoint'](rid, **collision)
    assert 'collision' in result['error']
    assert _approval_count(sf, rid) == 1
    newer = _sibling(sf)
    result = tools['answer_checkpoint'](newer, **args)
    assert 'collision' in result['error']
    assert sf.get_run(newer)['status'] == 'paused'


def test_feedback_refusal_and_actual_rejection_feedback(live):
    sf, rid, client, tools = live
    body = _body(sf, rid)
    response = client.post(f'/api/runs/{rid}/checkpoint/approve', json={**body, 'feedback': 'binding amendment'})
    assert response.status_code == 400
    assert 'feedback' in tools['answer_checkpoint'](rid, feedback='binding amendment')['error']
    assert sf.get_run(rid)['status'] == 'paused'
    response = client.post(f'/api/runs/{rid}/checkpoint/reject', json={'run_id':rid,'checkpoint':'plan','feedback':'use retained revision'})
    assert response.status_code == 200, response.text
    claimed = sf.claim_next_step(rid)
    assert 'use retained revision' in claimed.inputs['_feedback']


def test_manual_post_does_not_fetch_a_fresh_token(live, monkeypatch):
    sf, rid, client, tools = live
    body = _body(sf, rid)
    def forbidden(*a, **kw): raise AssertionError('manual decision fetched a fresh token')
    monkeypatch.setattr(sf, 'get_checkpoint', forbidden)
    assert client.post(f'/api/runs/{rid}/checkpoint/approve', json=body).status_code == 200
    result = tools['answer_checkpoint'](rid, expected_checkpoint=body['expected_checkpoint'], decision_id=body['decision_id'])
    assert 'error' not in result, result


@pytest.mark.asyncio
async def test_explicit_auto_policy_and_manual_meta_path_use_sdk_identity(live):
    sf, rid, client, tools = live
    from core.run_driver import _watch
    await _watch(sf, rid, auto_approve=True, max_watch_s=0.001)
    assert sf.get_run(rid)['status'] == 'running'
    assert _approval_count(sf, rid) == 1
    newer = _sibling(sf)
    from core.meta_run import approve_meta
    body = _body(sf, newer, 'manual-meta-proof')
    approve_meta(sf, newer, step_runner=lambda claimed: StepResult(outputs={'done':True}),
                 expected_checkpoint=body['expected_checkpoint'], decision_id=body['decision_id'])
    assert sf.get_run(newer)['status'] == 'completed'
    assert _approval_count(sf, newer) == 1


@pytest.mark.asyncio
async def test_butler_uses_supplied_observation_and_exact_run(live, monkeypatch):
    sf, rid, client, tools = live
    from core.meta_agent import MetaAgent
    agent = MetaAgent.__new__(MetaAgent)
    agent.db = deps.get_db_manager()
    view = MetaAgent._summarize_run_state(agent, rid)
    assert view['outputs'] == {'proposal':'reviewed text'}
    newer = _sibling(sf)
    async def no_model_driver(run_id): return {'run_id':run_id, 'status':sf.get_run(run_id)['status']}
    monkeypatch.setattr(agent, '_run_pipeline_until_checkpoint', no_model_driver)
    monkeypatch.setattr(sf, 'get_checkpoint', lambda *a: (_ for _ in ()).throw(AssertionError('fresh token')))
    result = await agent._tool_approve_checkpoint(view)
    assert 'error' not in result, result
    assert result['run_id'] == rid
    assert sf.get_run(newer)['status'] == 'paused'


def test_fixed_slot_review_is_retained_not_reread_from_mutable_files(live):
    from pathlib import Path
    sf, rid, client, tools = live
    graph = PipelineGraph(name='fixed_checkpoint_proof', begin='plan', steps=[
        StepNode(id='plan', step_type='agent', checkpoint=True,
                 output_fixed={'plan':'plan.md'}, transitions=[Transition(to='implement')]),
        StepNode(id='implement', step_type='agent', transitions=[])])
    sf.register_graph(graph)
    rid = sf.create_run(graph.name, project_id=PID)
    sf.start_run(rid)
    claim = sf.claim_next_step(rid)
    path = Path(sf.output_directory(rid, 'plan', 'plan')) / 'plan.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('# Actually reviewed retained proposal\n')
    sf.confirm_step(claim.token, StepResult(outputs={'plan':'submitted filename is not authority'}))
    sf.advance_run(rid)
    before = client.get(f'/api/runs/{rid}/checkpoint').json()
    path = Path(sf.output_directory(rid, 'plan', 'plan')) / 'plan.md'
    path.write_text('# MUTATED after checkpoint\n')
    after = client.get(f'/api/runs/{rid}/checkpoint').json()
    assert before['expected_checkpoint'] == after['expected_checkpoint']
    assert after['outputs'] == {'plan':'# Actually reviewed retained proposal\n'}
    assert after['step_output']['files'] == {'plan.md':'# Actually reviewed retained proposal\n'}


def test_real_state_controller_refuses_nonowner_before_sdk_transition(live, monkeypatch):
    from tests.unit.test_state_p3_checkpoint_entry import _bind_state_attempt, _as
    sf, rid, client, tools = live
    db = deps.get_db_manager()
    _bind_state_attempt(db, rid)
    body = _body(sf, rid)
    _as(monkeypatch, 'grok')
    response = client.post(f'/api/runs/{rid}/checkpoint/approve', json=body)
    assert response.status_code == 409 and 'not_attempt_owner' in response.text
    result = tools['answer_checkpoint'](rid, expected_checkpoint=body['expected_checkpoint'], decision_id=body['decision_id'])
    assert 'not_attempt_owner' in result['error']
    assert sf.get_run(rid)['status'] == 'paused' and _approval_count(sf, rid) == 0
    _as(monkeypatch, 'codex')
    response = client.post(f'/api/runs/{rid}/checkpoint/approve', json=body)
    assert response.status_code == 200, response.text
    assert _approval_count(sf, rid) == 1


@pytest.mark.asyncio
async def test_auto_event_and_stepping_paths_observe_actual_sdk_checkpoint(live, monkeypatch):
    import asyncio
    from core.run_driver import _watch, _step, drive_run
    sf, rid, client, tools = live
    event_run = sf.create_run('normal_checkpoint_proof', project_id=PID)
    sf.start_run(event_run)
    watcher = asyncio.create_task(_watch(sf, event_run, auto_approve=True, max_watch_s=0.02))
    await asyncio.sleep(0)  # subscription is live before the pause is routed
    claim = sf.claim_next_step(event_run)
    sf.confirm_step(claim.token, StepResult(outputs={'proposal':'event observed'}))
    sf.advance_run(event_run)
    await watcher
    assert sf.get_run(event_run)['status'] == 'running'
    assert _approval_count(sf, event_run) == 1

    class NoModelRunner:
        def __init__(self, **kw): pass
        async def execute(self, claimed): return StepResult(outputs={'done':True})
    monkeypatch.setattr('aitelier.runner.AgentStepRunner', NoModelRunner)
    assert await _step(sf, None, None, rid, auto_approve=True, max_steps=10) == 'completed'
    assert _approval_count(sf, rid) == 1
    protected = _sibling(sf)
    monkeypatch.setattr('core.config_registry._read_host_hints', lambda: {
        'normal_checkpoint_proof': {'manual_checkpoints_only':True}})
    assert await drive_run(sf, None, None, protected, scheduler_owned=True, auto_approve=True) == 'paused'
    assert _approval_count(sf, protected) == 0


def test_mcp_private_read_exposes_exact_retained_observation(live, monkeypatch):
    from types import SimpleNamespace
    from starlette.requests import Request
    sf, rid, client, tools = live
    request = Request({'type':'http', 'headers':[], 'client':('127.0.0.1',1234)})
    context = SimpleNamespace(request_context=SimpleNamespace(request=request))
    captured = {}
    def tool(name, kind, description):
        def deco(fn): captured[name]=fn; return fn
        return deco
    monkeypatch.setattr(mcp_router, '_mcp_may_read_private', lambda req: req is request)
    mcp_router._register_run_tools(tool, SimpleNamespace(get_context=lambda:context))
    result = captured['get_run_status'](rid)
    view = result['checkpoint']
    assert view['run_id'] == rid and view['expected_checkpoint'] == sf.get_checkpoint(rid)['expected_checkpoint']
    assert view['outputs'] == {'proposal':'reviewed text'}
    result = captured['answer_checkpoint'](rid, expected_checkpoint=view['expected_checkpoint'], decision_id=view['decision_id'])
    assert 'error' not in result, result
    assert _approval_count(sf, rid) == 1


def test_meta_finalize_retry_replays_original_decision_after_approval_advanced(live):
    from core.meta_run import approve_meta
    sf, rid, client, tools = live
    body = _body(sf, rid)
    sf.approve_checkpoint(rid, expected_checkpoint=body['expected_checkpoint'], decision_id=body['decision_id'])
    approve_meta(sf, rid, step_runner=lambda claimed: StepResult(outputs={'done':True}),
                 expected_checkpoint=body['expected_checkpoint'], decision_id=body['decision_id'])
    assert sf.get_run(rid)['status'] == 'completed'
    assert _approval_count(sf, rid) == 1
