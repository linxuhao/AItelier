"""New reviewer scenarios; production code comes only from exact /work."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import run_isolation as ri
from core.dpe_pipeline import MaxRetriesExceeded, NativeSideEffectsRetained
from core.state_graph import StateConflict, digest
from tests.unit.test_json_delivery_failure import EXTRA_BRACE, action, engine, response as jr
from tests.unit.test_native_direct_code import run_fixture, host, response, execute
from tests.unit.test_native_effect_recovery import HostCrash, batch, crash_after_fence, owned_tools
from tests.unit.test_scheduler_launch_isolation_failure import launch_world, _reserve
from tests.unit.test_state_relay_latest_payload import owned_world, fail_round, budget

R=Path('/reports')

def record(name, data):
    (R/(name+'.json')).write_text(json.dumps(data,indent=2,default=str)+'\n')

def install_json(e, replies, prompts):
    answers=iter(replies)
    def run(prompt):
        prompts.append(prompt)
        return next(answers)
    e.factory.get_agent=lambda _:SimpleNamespace(run=run,gateway=SimpleNamespace(litellm_model='offline-reviewer'))
    e.factory.get_fallback_to_json=lambda _:True

@pytest.mark.parametrize('json_route',['actions','content'])
def test_actual_native_failure_before_effect_enters_bounded_json_loop(tmp_path,monkeypatch,json_route):
    sf,rid,claim,root=run_fixture(tmp_path,monkeypatch)
    native=[];prompts=[]
    def before(**kwargs):
        native.append(1)
        raise ValueError('owned native response unavailable before effects')
    e,ws=host(sf,rid,claim,root,before)
    e.assembler=SimpleNamespace(assemble=lambda *args,**kwargs:'Write code directly, then finish.\n'+str(args[3]))
    events=[];emit=e._emit
    def observed_emit(kind,data=None):
        events.append((kind,data));emit(kind,data)
    e._emit=observed_emit
    e.factory.get_max_retries=lambda _:2
    if json_route=='actions':claim.inputs['_tool_schemas'].pop('write',None)
    install_json(e,[EXTRA_BRACE,jr(action('create',file='independent.py',content='VALUE = 29\n'))],prompts)
    try:
        result=execute(e,ws,rid,claim)
        rows=sf.get_trace(rid)
        record('actual-native-json-fallback-'+json_route,{'outcome':result,'json_route':json_route,'native_calls':native,'json_prompts':prompts,'trace':rows,'events':events,'bytes':(root/'independent.py').read_text()})
        assert result is True and len(prompts)==2
        if json_route=='actions':assert 'Failed to parse JSON' in prompts[1]
        else:
            assert 'previous response was not valid JSON' in prompts[1]
            assert 'Now respond with valid JSON' in prompts[1]
        assert not any(x['category']=='tool_call' and x['event']=='read' for x in rows)
        assert (root/'independent.py').read_text()=='VALUE = 29\n'
        assert sum(kind=='native_fallback' for kind,_ in events)==1
    finally:sf._conn.close()

def test_actual_native_effect_then_error_refuses_json_fallback(tmp_path,monkeypatch):
    bodies=[]
    def state():
        bodies.append(1);(tmp_path/'effect.txt').write_text('kept')
        return {'state_written':True}
    sf,rid,claim,root=run_fixture(tmp_path,monkeypatch,{'reviewer_state':({'parameters':{}},state)})
    provider=[];json_calls=[]
    def turn(**kwargs):
        provider.append(1)
        if len(provider)==1:return response('reviewer_state')
        raise ValueError('owned native failure after completed mutation')
    e,ws=host(sf,rid,claim,root,turn)
    install_json(e,[],json_calls)
    try:
        with pytest.raises(NativeSideEffectsRetained):execute(e,ws,rid,claim)
        record('native-effects-block-json',{'body_calls':bodies,'provider_calls':provider,'json_calls':json_calls,'effect':(tmp_path/'effect.txt').read_text(),'fences':[json.loads(p.read_text()) for p in e._effect_fence_dir.glob('*.json')]})
        assert bodies==[1] and json_calls==[]
        assert (tmp_path/'effect.txt').read_text()=='kept'
        assert not any(x['event']=='native_fallback' for x in sf.get_trace(rid))
    finally:sf._conn.close()

def test_retained_required_write_failure_stays_pending_until_actual_repair(tmp_path,monkeypatch):
    state_calls=[];delivery_calls=[];ready=False
    def state():
        state_calls.append(1);(tmp_path/'state.txt').write_text('retained')
        return {'state_written':True}
    def delivery():
        delivery_calls.append(1)
        if not ready:return {'error':'owned delivery precondition missing'}
        (tmp_path/'code/delivery.py').write_text('DELIVERED = 1\n')
        return {'written':['delivery.py']}
    sf,rid,claim,root=run_fixture(tmp_path,monkeypatch,{
        'reviewer_state':({'parameters':{}},state),
        'reviewer_delivery':({'parameters':{}},delivery)})
    retained=batch(response('reviewer_state'),response('reviewer_delivery'),response('finish_step'))
    before,ws=host(sf,rid,claim,root,lambda **kw:retained)
    crash_after_fence(before,sf,rid,claim,'reviewer_state')
    provider=[];json_calls=[]
    def forbid(**kwargs):provider.append(1);raise AssertionError('pending retained batch reached provider')
    try:
        with pytest.raises(HostCrash):execute(before,ws,rid,claim)
        original={p.name:p.read_bytes() for p in before._effect_fence_dir.glob('*.json')}
        for _ in range(2):
            resumed,ws2=host(sf,rid,claim,root,forbid);install_json(resumed,[],json_calls)
            with pytest.raises(NativeSideEffectsRetained,match='recovery remains incomplete'):execute(resumed,ws2,rid,claim)
            assert {p.name:p.read_bytes() for p in before._effect_fence_dir.glob('*.json')}==original
            assert not (root/'delivery.py').exists()
        ready=True
        repaired,ws3=host(sf,rid,claim,root,forbid);install_json(repaired,[],json_calls)
        assert execute(repaired,ws3,rid,claim) is True
        assert state_calls==[1] and delivery_calls==[1,1,1]
        assert provider==json_calls==[] and (root/'delivery.py').read_text()=='DELIVERED = 1\n'
        for n,b in original.items():assert (before._effect_fence_dir/n).read_bytes()==b
        record('pending-native-repair',{'state_calls':state_calls,'delivery_calls':delivery_calls,'provider_calls':provider,'json_calls':json_calls,'trace':sf.get_trace(rid),'written':(root/'delivery.py').read_text()})
    finally:sf._conn.close()

def test_recovered_identical_calls_at_distinct_batch_positions_execute(tmp_path,monkeypatch):
    sf,rid,claim,root,executions=owned_tools(tmp_path,monkeypatch)
    retained=batch(response('set_owned_state',value='left'),response('set_owned_state',value='right'),response('set_owned_state',value='left'),response('finish_step'))
    before,ws=host(sf,rid,claim,root,lambda **kw:retained)
    crash_after_fence(before,sf,rid,claim,'set_owned_state')
    provider=[]
    def forbid(**kw):provider.append(1);raise AssertionError('retained batch was not settled before provider')
    try:
        with pytest.raises(HostCrash):execute(before,ws,rid,claim)
        first={p.name:p.read_bytes() for p in before._effect_fence_dir.glob('*.json')}
        restored,ws2=host(sf,rid,claim,root,forbid)
        assert execute(restored,ws2,rid,claim) is True
        fences=[json.loads(p.read_text()) for p in before._effect_fence_dir.glob('*.json')]
        assert executions==[('state','left'),('state','right'),('state','left')]
        assert json.loads((tmp_path/'state.json').read_text())=='left' and provider==[]
        assert len(fences)==3 and len({x['invocation_key'] for x in fences})==3
        assert len({x['call_key'] for x in fences})==2
        for n,b in first.items():assert (before._effect_fence_dir/n).read_bytes()==b
        record('distinct-retained-invocations',{'executions':executions,'provider_calls':provider,'fences':fences,'trace':sf.get_trace(rid)})
    finally:sf._conn.close()

def test_latest_failure_payload_survives_bounded_tail_and_frozen_handoff(tmp_path,monkeypatch):
    service,runtime,source=owned_world(tmp_path,monkeypatch)
    old={'remaining_delivery':['first unfinished'],'read_accounting':{'bytes':120}}
    new={'remaining_delivery':['latest unfinished'],'read_accounting':{'bytes':17},'reviewer_marker':'latest'}
    first=fail_round(service,runtime,source,'independent-first',[budget(1,old)])
    rows=[{'seq':i,'event':'reviewer_noise','payload':{'noise':i}} for i in range(1,620)]
    rows.append(budget(620,new))
    latest=fail_round(service,runtime,source,'independent-latest',rows,first)
    before=copy.deepcopy(runtime.rows)
    result=service.disposition_failed_attempt(latest['attempt_id'],'handoff-external',request_key='independent-handoff',relay_digest=latest['relay_inventory']['digest'],instruction='finish retained latest delivery',harness='reviewer-owned',external_id='reviewer/next')
    context=result['attempt']['context'];frozen=context['relay_handoff']
    assert runtime.rows==before
    assert frozen['remaining_delivery']==new['remaining_delivery']
    assert frozen['relay_inventory']['remaining_delivery_trace']['payload']==new
    assert frozen['original_first_failure']['trace']['payload']==old
    assert frozen['original_first_failure']['run_id']==first['run_id']
    assert result['attempt']['context_hash']==digest(context)
    runtime.rows[latest['run_id']][-1]['payload']['read_accounting']['bytes']=999
    reread=service.external.inspect(result['attempt']['attempt_id'])
    assert reread['context']==context
    assert (latest['run_id'],'desc',500) in runtime.trace_calls
    record('latest-relay-frozen',{'first':first,'latest':latest,'handoff':result,'trace_calls':runtime.trace_calls})

def test_launch_refusal_can_stop_and_only_explicit_corrected_attempt_launches(launch_world):
    w=launch_world;a=_reserve(w,'independent-unavailable-base');pid=a['execution_project_id']
    ri.request_base(w.db,pid,'e'*40,'reviewer unavailable base')
    assert w.scheduler._get_or_create_skillflow_run(pid) is None
    rid=w.sf._conn.execute('SELECT id FROM skillflow_runs WHERE project_id=?',(pid,)).fetchone()[0]
    w.attempts.bind_run(a['attempt_id'],rid,w.sf)
    failed=w.service.reconcile_attempt(a['attempt_id'])
    with pytest.raises(StateConflict,match='no crash-safe relay inventory'):
        w.service.disposition_failed_attempt(a['attempt_id'],'handoff-external',request_key='no-invented-relay',relay_digest='0'*64,instruction='unavailable',harness='reviewer-owned',external_id='reviewer/no-relay')
    stopped=w.service.disposition_failed_attempt(a['attempt_id'],'leave-stopped')
    assert stopped['automatic_retry'] is False and w.scheduler._get_or_create_skillflow_run(pid) is None
    b=_reserve(w,'independent-explicit-corrected');ri.request_base(w.db,b['execution_project_id'],w.head,'reviewer corrected source')
    new=w.scheduler._get_or_create_skillflow_run(b['execution_project_id'])
    w.attempts.bind_run(b['attempt_id'],new,w.sf)
    assert w.attempts.reconcile(b['attempt_id'],w.sf)['status']=='running'
    rec=ri.record(w.db,new)
    assert rec['base_sha']==w.head and Path(rec['worktree_path'])!=w.source
    old=w.sf.get_run(rid)
    assert old['status']=='failed' and old['started_at'] is None
    assert old['error_reason']==failed['error']
    assert all(x['claim_epoch']==0 and x['claimed_at'] is None for x in w.sf.get_steps(rid))
    assert w.sf._conn.execute('SELECT COUNT(*) FROM skillflow_runs WHERE project_id=?',(pid,)).fetchone()[0]==1
    record('launch-state-disposition',{'failed':failed,'leave_stopped':stopped,'old_run':old,'new_run':w.sf.get_run(new),'new_isolation':rec,'logs':w.logs})

def test_json_mixed_write_success_cannot_hide_second_write_error(tmp_path):
    # One valid corrected turn contains two required writes; the first lands,
    # the second returns a real write error. Delivery must remain nonpassing.
    replies=[EXTRA_BRACE,jr(action('create',file='partial.py',content='PARTIAL = 1\n'),action('edit',file='required.py',content='REQUIRED = 1\n')),jr(action('finish_step'))]
    e=engine(tmp_path,replies,max_turns=3)
    calls=[]
    def dispatch(call):
        calls.append(call)
        if call['tool']=='create':
            (tmp_path/call['params']['file']).write_text(call['params']['content'])
            return {'written':['partial.py']}
        if call['tool']=='edit':return {'error':'owned required delivery edit failed'}
        raise AssertionError(call)
    e._exec_tool=dispatch
    outcome=None;error=None
    try:outcome=e._run_tool_step(1,'implement',None,'fixture',agent_config_name='stub')
    except MaxRetriesExceeded as exc:error=str(exc)
    record('json-mixed-write-result',{'outcome':outcome,'error':error,'calls':calls,'prompts':e.prompts,'events':e.events,'traces':e.traces,'partial_bytes':(tmp_path/'partial.py').read_text(),'required_file_exists':(tmp_path/'required.py').exists()})
    assert outcome is not True,'partial valid JSON delivery completed despite a required edit error'
    assert error and 'owned required delivery edit failed' in error
    assert not any(kind=='step_done' for kind,_ in e.events)
