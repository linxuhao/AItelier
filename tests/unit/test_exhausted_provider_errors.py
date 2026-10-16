"""Provider exhaustion at actual gateway/native/JSON production seams, offline."""
import json,os
from pathlib import Path
from types import SimpleNamespace
import litellm
import pytest
from tenacity import wait_none
from core.ai_router import AIGateway
from core.agents import DPEAgent,DPEAgentNative
from core.trace_reader import trace_list
from skillflow import SkillFlow
from skillflow.graph import PipelineGraph,StepNode
from tests.integration.test_native_parity import engine,_WS,_setup,TS

FIRST='first-provider-timeout-'+('x'*260)+'-original-tail'

@pytest.fixture
def call_case(engine,tmp_path,monkeypatch,request):
    monkeypatch.setenv('AITELIER_HOME',str(tmp_path/'home'))
    monkeypatch.setenv('OWN_PROVIDER_TEST_KEY','owned-fixture-only')
    from core import model_routes
    model_routes.reset_cache()
    config=tmp_path/'providers.json';config.write_text(json.dumps({'openai':{'base_url':'http://offline.invalid','api_key_env':'OWN_PROVIDER_TEST_KEY'}}))
    routes=tmp_path/'routes.json';routes.write_text('{}')
    monkeypatch.setattr(AIGateway.generate_native.retry,'wait',wait_none())
    monkeypatch.setattr(AIGateway.generate.retry,'wait',wait_none())
    sf=SkillFlow(str(tmp_path/'engine.db'),artifact_history=False)
    sf.register_graph(PipelineGraph(name='provider-failure',begin='t_impl',steps=[StepNode(id='t_impl',step_type='agent')]))
    rid=sf.create_run('provider-failure',project_id='default');sf.start_run(rid);sf.advance_run(rid);claim=sf.claim_next_step(rid)
    import api.dependencies as deps
    monkeypatch.setattr(deps,'get_skillflow',lambda:sf)
    engine._trace_cb=claim.trace
    engine.factory.get_max_retries.return_value=3
    engine.factory.get_max_tool_turns.return_value=4
    engine.assembler=SimpleNamespace(assemble=lambda *a,**k:'owned controlled provider fixture')
    engine.events=[]
    engine._emit=lambda event,payload=None:engine.events.append((event,payload))
    calls=[]
    error=litellm.exceptions.Timeout(message=FIRST,model='owned-fixture',llm_provider='offline')
    failure=getattr(request,'param','timeout')
    if failure=='cancel':
        import asyncio
        error=asyncio.CancelledError('owned fixture cancellation')
    elif failure=='connection':
        error=litellm.exceptions.APIConnectionError(message=FIRST,model='owned-fixture',llm_provider='offline')
    elif failure=='refusal':
        from core.ai_router import ProviderRefusal
        error=ProviderRefusal(FIRST)
    elif failure=='auth':
        error=litellm.exceptions.AuthenticationError(message=FIRST,model='owned-fixture',llm_provider='offline')

    for mode in ('native','json'):
        gateway=AIGateway('openai/owned-'+mode,config_path=str(config),routes_path=str(routes))
        def fail(kwargs,mode=mode):
            calls.append(mode)
            raise error
        monkeypatch.setattr(gateway,'_completion_bounded',fail)
        agent=DPEAgentNative(gateway,'owned fixture') if mode=='native' else DPEAgent(gateway,'owned fixture')
        getattr(engine.factory,'get_native_agent' if mode=='native' else 'get_agent').return_value=agent
    _setup(tmp_path);ws=_WS(tmp_path)
    def run(schemas=TS):
        return engine.run_step(1,'t_impl',ws,'default',agent_config_name='task_implementer',tool_schemas=schemas,
            run_id=rid,step_instance_id=claim.token.step_instance_id,claim_epoch=claim.token.claim_epoch)
    def evidence(name,caught):
        rows=sf.get_trace(rid)
        summary=trace_list(sf,rid,step='t_impl',errors_only=True)
        result={'run_id':rid,'step_id':'t_impl','step_instance_id':claim.token.step_instance_id,
            'claim_epoch':claim.token.claim_epoch,'calls':calls,'caught_type':type(caught).__name__,
            'caught_message':str(caught),'same_original_exception':caught is error,'events':engine.events,
            'trace':rows,'summary':summary}
        root=os.environ.get('REVIEW_RAW')
        if root:(Path(root)/(name+'.json')).write_text(json.dumps(result,indent=2,default=str)+'\n')
        return rows,summary
    yield SimpleNamespace(engine=engine,sf=sf,rid=rid,claim=claim,error=error,calls=calls,run=run,evidence=evidence)
    sf._conn.close();model_routes.reset_cache()

@pytest.mark.parametrize('fallback',[False,True])
def test_native_exhaustion_is_not_restarted_as_output_or_json_retry(call_case,fallback):
    c=call_case;c.engine.factory.get_fallback_to_json.return_value=fallback
    with pytest.raises(Exception) as caught:c.run()
    rows,summary=c.evidence('native-fallback-'+str(fallback),caught.value)
    # Keep the gateway's existing three transport attempts, one logical generation.
    assert c.calls==['native']*3,c.calls
    assert caught.value is c.error
    assert not any(event in ('native_fallback','step_retry') for event,_ in c.engine.events)
    errors=[r for r in rows if r['event']=='native_error']
    assert len(errors)==1,errors
    payload=errors[0]['payload']
    assert payload['error_type']==type(c.error).__name__ and payload['original_error']==str(c.error)
    assert errors[0]['step_id']=='t_impl' and errors[0]['step_instance_id']==c.claim.token.step_instance_id
    assert 'original-tail' in payload['original_error']
    assert summary['run_id']==c.rid and summary['entries'][0]['step']=='t_impl'
    assert 'Timeout' in summary['entries'][0]['summary']

@pytest.mark.parametrize('schemas',[{'create':{}},{'write_summary':{}},{'read_file':{},'write_summary':{}}],ids=['tools','content','read-content'])
def test_json_exhaustion_records_original_error_in_ordinary_trace(call_case,schemas):
    c=call_case;c.engine.factory.is_native.return_value=False
    with pytest.raises(Exception) as caught:c.run(schemas)
    rows,summary=c.evidence('json-'+('-'.join(schemas)),caught.value)
    assert c.calls==['json']*3 and caught.value is c.error
    errors=[r for r in rows if r['event']=='provider_error']
    assert len(errors)==1,errors
    payload=errors[0]['payload']
    assert payload['error_type']==type(c.error).__name__ and payload['original_error']==str(c.error)
    assert errors[0]['step_instance_id']==c.claim.token.step_instance_id
    assert summary['count']==1 and summary['entries'][0]['step']=='t_impl'
    assert 'Timeout' in summary['entries'][0]['summary']


@pytest.mark.parametrize('call_case,expected_calls',[('connection',3),('refusal',1),('auth',1)],indirect=['call_case'])
def test_other_exhausted_endpoint_failures_do_not_enter_json(call_case,expected_calls):
    c=call_case;c.engine.factory.get_fallback_to_json.return_value=True
    with pytest.raises(Exception) as caught:c.run()
    rows,summary=c.evidence('endpoint-'+type(c.error).__name__,caught.value)
    assert c.calls==['native']*expected_calls and caught.value is c.error
    assert not any(event in ('native_fallback','step_retry') for event,_ in c.engine.events)
    assert [r['event'] for r in rows if r['category']=='error']==['native_error']
    assert summary['count']==1 and type(c.error).__name__ in summary['entries'][0]['summary']


def test_endpoint_classification_preserves_explicit_cause_and_native_unsupported():
    from core.ai_router import is_provider_failure
    cause=litellm.exceptions.AuthenticationError(message='owned missing key',model='fixture',llm_provider='offline')
    explanation=RuntimeError('owned authentication explanation');explanation.__cause__=cause
    assert is_provider_failure(explanation)
    # Re-raising the same exception from itself can form an explicit-cause cycle.
    unsupported=litellm.exceptions.BadRequestError(message='tools unsupported',model='fixture',llm_provider='offline')
    unsupported.__cause__=unsupported
    assert not is_provider_failure(unsupported)
    assert not is_provider_failure(ValueError('native tool calling unavailable'))
    assert is_provider_failure(litellm.exceptions.BadRequestError(
        message='Your credit balance is too low',model='fixture',llm_provider='offline'))


@pytest.mark.parametrize('call_case',['cancel'],indirect=True)
def test_native_cancellation_propagates_without_format_repair(call_case):
    import asyncio
    c=call_case;c.engine.factory.get_fallback_to_json.return_value=True
    with pytest.raises(asyncio.CancelledError) as caught:c.run()
    rows,_=c.evidence('native-cancellation',caught.value)
    assert c.calls==['native'] and caught.value is c.error
    assert not any(event in ('native_fallback','step_retry','native_error') for event,_ in c.engine.events)
    assert not any(r['category']=='error' for r in rows)
