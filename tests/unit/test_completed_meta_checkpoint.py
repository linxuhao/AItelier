import json
from types import SimpleNamespace
import pytest
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.core import StepResult
from skillflow.exceptions import SkillFlowError
from tests.unit.test_normal_checkpoint_protocol import live
from core.checkpoint_protocol import checkpoint_view
from core.meta_run import approve_meta
from core.meta_agent import MetaAgent
from api import mcp_router


def completed_meta(sf, client=None):
    graph = PipelineGraph(name='independent_meta', begin='gather', steps=[
        StepNode(id='gather', step_type='agent', checkpoint=True, transitions=[Transition(to='finalize')]),
        StepNode(id='finalize', step_type='agent', transitions=[])])
    sf.register_graph(graph)
    rid = sf.create_run(graph.name, project_id='normal-checkpoint-proof')
    sf.start_run(rid)
    c = sf.claim_next_step(rid)
    original = {'brief': {'user_stories': ['Original reviewed story'], 'goal': 'ORIGINAL'}, 'need_input': False}
    sf.confirm_step(c.token, StepResult(outputs={'gather_state': original}))
    sf.advance_run(rid)
    observed = checkpoint_view(sf, rid)
    if client is None:
        sf.approve_checkpoint(rid, expected_checkpoint=observed['expected_checkpoint'], decision_id=observed['decision_id'])
    else:
        response = client.post(f'/api/runs/{rid}/checkpoint/approve', json=observed)
        assert response.status_code == 200, response.text
    sf.advance_run(rid)
    c = sf.claim_next_step(rid)
    sf.confirm_step(c.token, StepResult(outputs={'finalized': True}))
    sf.advance_run(rid)
    assert sf.get_run(rid)['status'] == 'completed'
    return rid, observed, original


def test_http_retry_of_prior_checkpoint_does_not_approve_later_pause(live):
    sf, _, client, _ = live
    graph = PipelineGraph(name='independent_two_checkpoints', begin='a', steps=[
        StepNode(id='a', step_type='agent', checkpoint=True, transitions=[Transition(to='b')]),
        StepNode(id='b', step_type='agent', checkpoint=True, transitions=[Transition(to='c')]),
        StepNode(id='c', step_type='agent', transitions=[])])
    sf.register_graph(graph)
    rid = sf.create_run(graph.name, project_id='normal-checkpoint-proof'); sf.start_run(rid)
    c=sf.claim_next_step(rid); sf.confirm_step(c.token, StepResult(outputs={'plan':'A'})); sf.advance_run(rid)
    old=client.get(f'/api/runs/{rid}/checkpoint').json()
    path=f'/api/runs/{rid}/checkpoint/approve'
    assert client.post(path,json=old).status_code == 200
    sf.advance_run(rid); c=sf.claim_next_step(rid); sf.confirm_step(c.token, StepResult(outputs={'plan':'B'})); sf.advance_run(rid)
    later=sf.get_checkpoint(rid)
    assert client.post(path,json=old).status_code == 200
    assert sf.get_run(rid)['status'] == 'paused'
    assert sf.get_checkpoint(rid) == later


@pytest.mark.asyncio
async def test_actual_registered_mcp_refuses_stale_and_replays_while_completed(live):
    sf, rid, client, _ = live
    observed=checkpoint_view(sf,rid)
    server=mcp_router.build_mcp()
    tool=server._tool_manager.get_tool('answer_checkpoint')
    props={t.name:t for t in await server.list_tools()}['answer_checkpoint'].inputSchema['properties']
    assert {'expected_checkpoint','decision_id'} <= set(props)
    args={'run_id':rid,'expected_checkpoint':observed['expected_checkpoint'],'decision_id':observed['decision_id']}
    result=await tool.run(args, context=server.get_context()); assert 'error' not in result,result
    sf.advance_run(rid); c=sf.claim_next_step(rid); sf.confirm_step(c.token,StepResult(outputs={'done':True})); sf.advance_run(rid)
    assert sf.get_run(rid)['status']=='completed'
    result=await tool.run(args, context=server.get_context()); assert 'error' not in result,result
    bad={**args,'expected_checkpoint':{**args['expected_checkpoint'],'version':999}}
    result=await tool.run(bad,context=server.get_context()); assert 'collision' in result.get('error',''),result


def test_completed_human_meta_helper_refuses_missing_identity(live):
    sf, _, _, _ = live
    rid, observed, original = completed_meta(sf)
    # SDK itself refuses this same missing identity, even after completion.
    with pytest.raises(SkillFlowError): sf.approve_checkpoint(rid)
    with pytest.raises(SkillFlowError): approve_meta(sf,rid)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['missing','stale','malformed','foreign','collision','valid-retry','valid-cli-recovery'])
async def test_completed_brief_tool_binds_original_token_and_retained_brief(live, monkeypatch, mode):
    sf, _, client, _ = live
    rid, observed, original = completed_meta(sf, client if mode == 'valid-cli-recovery' else None)
    agent=MetaAgent.__new__(MetaAgent)
    effects=[]
    agent.db=SimpleNamespace(update_project=lambda *a,**kw:effects.append(('update',kw)))
    agent.ws=object()
    agent._log_error=lambda msg:None
    def resolve_build_config(sf,args):
        effects.append(('config',args))
        return 'controlled-build',''
    agent._resolve_build_config=resolve_build_config
    async def no_build(args):
        effects.append(('start',args)); return {'status':'CONTROLLED_DISPATCH','run_id':rid}
    agent._tool_start_pipeline=no_build
    mutation={'brief':{'user_stories':['Disk changed AFTER approval'], 'goal':'MUTATED'}}
    monkeypatch.setattr('core.meta_run.read_gather_state',lambda ws,pid:mutation)
    def no_seed(db,ws,pid,brief):
        effects.append(('seed',brief)); return {'status':'submitted'}
    monkeypatch.setattr('core.project_submit.seed_and_trigger',no_seed)
    args={'run_id':rid}
    if mode!='missing': args.update(expected_checkpoint=observed['expected_checkpoint'],decision_id=observed['decision_id'])
    if mode=='stale': args['expected_checkpoint']={**args['expected_checkpoint'],'version':999}
    if mode=='malformed': args['expected_checkpoint']={**args['expected_checkpoint'],'outputs_sha256':'invalid'}
    if mode in ('foreign','collision'):
        _, other, _ = completed_meta(sf)
        if mode=='foreign': args['expected_checkpoint']=other['expected_checkpoint']
        else: args['decision_id']=other['decision_id']
    result=await agent._tool_approve_project_brief(args)
    print(json.dumps({'mode':mode,'result':result,'effects':effects},ensure_ascii=False))
    if mode in ('valid-retry','valid-cli-recovery'):
        assert [effect[0] for effect in effects] == ['config','update','seed','start']
        assert effects[2] == ('seed',original['brief']), 'Valid retry must use retained original brief, not current disk'
        assert result['status']=='CONTROLLED_DISPATCH'
    else:
        assert result.get('status')=='error' and not effects,'Missing/colliding completed approval must refuse before seed/build'


@pytest.mark.parametrize('mode', ['missing','stale','different-decision','foreign'])
def test_completed_meta_helper_matches_sdk_refusal_and_does_not_finalize(live, monkeypatch, mode):
    sf, _, _, _ = live
    rid, observed, original = completed_meta(sf)
    kwargs = {'expected_checkpoint':observed['expected_checkpoint'], 'decision_id':observed['decision_id']}
    if mode=='missing': kwargs={}
    elif mode=='stale': kwargs['expected_checkpoint']={**kwargs['expected_checkpoint'],'version':999}
    elif mode=='different-decision': kwargs['decision_id']='unrecorded-decision'
    else:
        _, foreign, _ = completed_meta(sf)
        kwargs['expected_checkpoint']=foreign['expected_checkpoint']
    before=sf.get_steps(rid, include_payloads=True)
    effects=[]
    with pytest.raises(SkillFlowError): sf.approve_checkpoint(rid, **kwargs)
    with pytest.raises(SkillFlowError): approve_meta(sf,rid,step_runner=lambda c:effects.append(c),**kwargs)
    assert not effects and sf.get_steps(rid, include_payloads=True)==before


def test_valid_completed_meta_retry_replays_without_rerunning_finalize(live):
    sf, _, _, _ = live
    rid, observed, original = completed_meta(sf)
    before=sf.get_steps(rid, include_payloads=True)
    effects=[]
    approve_meta(sf,rid,step_runner=lambda c:effects.append(c),
                 expected_checkpoint=observed['expected_checkpoint'],decision_id=observed['decision_id'])
    assert sf.get_run(rid)['status']=='completed'
    assert not effects and sf.get_steps(rid, include_payloads=True)==before
