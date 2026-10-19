"""Actual gateway/SDK driver boundaries; transport scripted, no paid provider.

Projection/handoff cases exercise the real transformations and gateway closure,
but do not claim the native runner chose a budget handoff. Driver injections
prove propagation only, paired with the accepted real refusal-decision cases.
"""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest
from aitelier.runner import AgentStepRunner
from core.ai_router import AIGateway
from core.dpe_pipeline import RepeatedScopeRefusal, ScopeObservationUnavailable
from core.scope_refusal import RequestClosure, carry_text
from tests.unit.test_dpe_refusal_progress.test_progress import Case, denied, finish
from tests.unit.test_repeated_scope_refusal import gateway


@pytest.fixture
def cause(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch, 'native')
    claim = case.claim()
    case.execute(claim, denied('native'), finish('native'))
    receipt = json.loads((Path(claim.inputs['_artifact_dir'])/'write_scope_receipt.json').read_text())
    assert len(case.denials()) == 1
    return receipt['scope_cause']


def reply():
    return litellm.ModelResponse(model='offline', choices=[{'index':0,
        'finish_reason':'stop','message':{'role':'assistant','content':'{"done":true}'}}],
        usage={'prompt_tokens':8,'completion_tokens':4,'total_tokens':12})


@pytest.mark.parametrize('mode', ['native', 'json'])
@pytest.mark.parametrize('outcome', ['failover', 'failure'])
def test_gateway_pairs_only_success_and_restores_observers(gateway, cause, monkeypatch, mode, outcome):
    requests, events, presented = [], [], []
    prior_complete = lambda event, payload: events.append((event, dict(payload)))
    prior_present = lambda messages: presented.append(copy.deepcopy(messages))
    gateway.on_completion, gateway.on_messages_presented = prior_complete, prior_present
    def complete(**kwargs):
        requests.append(copy.deepcopy({k:kwargs.get(k)for k in ('model','messages','tools','response_format')}))
        if outcome == 'failure':raise ValueError('controlled request-shaped failure')
        if len(requests) == 1:
            raise litellm.exceptions.ServiceUnavailableError('controlled endpoint unavailable', model='one/model', llm_provider='one')
        return reply()
    monkeypatch.setattr(litellm, 'completion', complete)
    def invoke():
        if mode == 'native':return gateway.generate_native([{'role':'system','content':''}, {'role':'user','content':carry_text(cause)}])
        return gateway.generate('', carry_text(cause), is_json_mode=True)
    with RequestClosure(gateway, cause) as closure:
        if outcome == 'failure':
            with pytest.raises(ValueError, match='request-shaped'):invoke()
            with pytest.raises(ValueError, match='no completed'):closure.evidence()
            assert not presented
        else:
            invoke()
            evidence = closure.evidence()
            completed = [p for e,p in events if e=='completion_finished'and p['outcome']=='completed']
            errors = [p for e,p in events if e=='completion_finished'and p['outcome']=='error']
            assert len(requests)==2 and len(errors)==len(completed)==1
            assert evidence['invocation_id']==completed[0]['invocation_id']!=errors[0]['invocation_id']
            assert len(presented)==1 and presented[0]==requests[-1]['messages']
            assert carry_text(cause) in json.dumps(presented[0]) or any(carry_text(cause) in str(m.get('content',''))for m in presented[0])
            if mode=='json':assert 'valid JSON' in str(presented[0])
    assert gateway.on_completion is prior_complete and gateway.on_messages_presented is prior_present
    print('BOUNDARY_TRANSPORT '+json.dumps({'mode':mode,'outcome':outcome,'requests':requests,'events':events,'presented':presented}))


@pytest.mark.parametrize('transform', ['projection', 'handoff'])
def test_real_transformed_messages_and_gateway_retain_complete_carry(gateway, cause, monkeypatch, transform):
    from core.dpe_pipeline import _project_native_messages, _context_handoff_messages
    history=[{'role':'system','content':'fixture'}, {'role':'user','content':'assignment'},
             {'role':'tool','tool_call_id':'old','content':'long observation '+('x'*20000)+carry_text(cause)}]
    if transform=='projection':
        messages, measured=_project_native_messages(history)
        assert measured['compacted_tool_results']==1
    else:
        messages=_context_handoff_messages(history,segment=0,written_files=[])
        assert json.loads(messages[1]['content'])['context_handoff']['to_segment']==1
    requests=[]
    monkeypatch.setattr(litellm,'completion',lambda **kw:requests.append(copy.deepcopy(kw['messages']))or reply())
    with RequestClosure(gateway,cause)as closure:
        # Same bounded carry after transformation, not the old compacted tool result.
        messages.append({'role':'user','content':closure.carry})
        gateway.generate_native(messages)
        assert closure.evidence()['carry_match'] is True
    assert requests and any(carry_text(cause) in str(m.get('content',''))for m in requests[0])
    print('BOUNDARY_TRANSFORM '+json.dumps({'transform':transform,'messages':requests[0]}))


@pytest.mark.parametrize('driver,error_type', [
    ('run_driver',RepeatedScopeRefusal), ('meta',ScopeObservationUnavailable),
    ('pipeline',RepeatedScopeRefusal), ('scheduler',ScopeObservationUnavailable)])
def test_real_driver_does_not_retry_typed_executor_failure(tmp_path, monkeypatch, driver, error_type):
    case=Case(tmp_path,monkeypatch,'native')
    calls=[]
    async def execute(self, claimed):
        calls.append(claimed.token.step_instance_id)
        raise error_type('controlled propagation-only terminal')
    monkeypatch.setattr(AgentStepRunner,'execute',execute)
    if driver=='run_driver':
        from core.run_driver import _step
        asyncio.run(_step(case.sf,case.db,case.ws,case.run,False,10))
    elif driver in ('meta','pipeline'):
        from core.meta_agent import MetaAgent
        agent=MetaAgent.__new__(MetaAgent);agent.db=case.db;agent.ws=case.ws
        fn=agent._run_meta_until_checkpoint if driver=='meta'else agent._run_pipeline_until_checkpoint
        result=asyncio.run(fn(case.run));assert result['status']=='error'
    else:
        from core import scheduler
        monkeypatch.setattr(scheduler,'get_skillflow',lambda:case.sf)
        monkeypatch.setattr(scheduler,'db',case.db);monkeypatch.setattr(scheduler,'ws',case.ws)
        monkeypatch.setattr(scheduler,'_get_or_create_skillflow_run',lambda pid:case.run)
        monkeypatch.setattr(scheduler,'_quota_hold_remaining',lambda:0)
        monkeypatch.setattr(scheduler,'_sync_project_status_to_db',lambda pid:None)
        asyncio.run(scheduler._run_skillflow_tick('p',None))
    steps=case.sf.get_steps(case.run)
    assert len(calls)==1 and case.sf.get_run(case.run)['status']=='failed'
    failed=[s for s in steps if s['id']==calls[0]]
    assert len(failed)==1 and failed[0]['status']=='failed'
    print('BOUNDARY_DRIVER '+json.dumps({'driver':driver,'error':error_type.__name__,'calls':calls,'steps':steps},default=str))


@pytest.mark.parametrize('error_type',[RepeatedScopeRefusal,ScopeObservationUnavailable])
def test_native_typed_error_cannot_buy_json_fallback(error_type):
    from tests.unit.test_native_fallback_quota import _engine, _run
    marker=error_type('controlled dispatch boundary')
    engine=_engine(marker,fallback=True)
    with pytest.raises(error_type)as caught:_run(engine)
    assert caught.value is marker and 'native_fallback'not in engine.events


@pytest.fixture(scope='session',autouse=True)
def retained_process_identity():
    import importlib.metadata, os, subprocess
    import core.dpe_pipeline
    head=subprocess.check_output(['git','-C','/work','rev-parse','HEAD'],text=True).strip()
    identity={'sdk':importlib.metadata.version('skillflow-py'),'module':core.dpe_pipeline.__file__,
              'head':head,'pid':os.getpid(),'mounts':[line for line in Path('/proc/self/mountinfo').read_text().splitlines()if ' /work 'in line or ' /raw 'in line]}
    Path('/raw/identity.json').write_text(json.dumps(identity,indent=2))
    assert identity['sdk']=='1.5.97'and identity['module']=='/work/core/dpe_pipeline.py'
    assert head==os.environ['BOUNDARY_TEST_SHA'] and identity['mounts']


def test_native_runner_budget_handoff_retains_actual_refusal_request(tmp_path, monkeypatch):
    """A declared bounded fixture window and long returned reasoning select handoff."""
    from core import model_routes
    providers=tmp_path/'handoff-providers.json'
    providers.write_text(json.dumps({'deepseek':{'base_url':'https://offline.invalid/v1',
        'api_key_env':'BOUNDARY_OFFLINE_KEY','max_input_tokens':20000}}))
    monkeypatch.setenv('BOUNDARY_OFFLINE_KEY','offline-fixture')
    original_config=model_routes.config_or_example
    monkeypatch.setattr(model_routes,'config_or_example',lambda name:str(providers)if name=='llm_providers.json'else original_config(name))
    case=Case(tmp_path,monkeypatch,'native');claim=case.claim()
    transport=litellm.completion
    responses=0
    def complete(**kwargs):
        nonlocal responses
        result=transport(**kwargs);responses+=1
        if responses==1:result.choices[0].message.reasoning_content='context growth '*12000
        return result
    monkeypatch.setattr(litellm,'completion',complete)
    case.execute(claim,denied('native'),finish('native'))
    events=case.sf.get_trace(case.run,limit=10000)
    handoffs=[r for r in events if r['event']=='context_handoff']
    assert handoffs,'the real runner did not select a context-budget handoff'
    assert len(case.requests)==2 and len(case.denials())==1
    receipt=json.loads((Path(claim.inputs['_artifact_dir'])/'write_scope_receipt.json').read_text())
    carry=carry_text(receipt['scope_cause'])
    assert any(carry in str(m.get('content',''))for m in case.requests[-1]['messages'])
    assert any('"context_handoff"'in str(m.get('content',''))for m in case.requests[-1]['messages'])
    assert receipt['response_witness']['carry_match']is True
    print('BOUNDARY_RUNNER_HANDOFF '+json.dumps({'handoffs':handoffs,'requests':case.requests,
        'witness':receipt['response_witness'],'denials':case.denials()},default=str))


def test_unprovisioned_nested_cpu_gate_retains_nonpassing_reason(tmp_path):
    """Measure the actual isolated-executor refusal, never pretend pytest rc5 ran."""
    from aitelier.tools.run_tests import impl as rt
    from tests.unit.test_dpe_refusal_progress.test_progress import git
    repo=tmp_path/'gate-repo';repo.mkdir()
    (repo/'app.py').write_text('VALUE = 1\n')
    git(repo,'init','-q');git(repo,'add','--','app.py')
    git(repo,'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','owned gate fixture')
    out=tmp_path/'gate-output'
    result=rt.run_tests(project_root=str(repo),out_dir=str(out))
    report=json.loads((out/'test_report.json').read_text())
    Path('/raw/nested-cpu-gate-report.json').write_text(json.dumps({'result':result,'report':report},indent=2))
    assert result['passed']is False and report['passed']is False
    assert report.get('infrastructure_unavailable')is True
    assert report.get('skipped_because')=='cpu_executor_unavailable'
    assert report['returncode']==-1 and report['summary']
