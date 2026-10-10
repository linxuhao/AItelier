"""Current invocation owner composes bounded starvation without double charges."""
import copy,json
from unittest.mock import MagicMock
import pytest
from core.ai_router import AIGateway
from core import agents
from core.dpe_pipeline import NativeOutputCapExhausted
from tests.integration.test_native_parity import engine,budget_case,_run,_turn,_tc
from tests.unit import test_native_direct_code as f
from tests.unit.test_native_starved_output_reclaim import traced_host,starve,payloads


def test_live_sized_cap_after_retained_write_has_no_reward_or_false_completion(budget_case,monkeypatch):
    eng,ws,draft,traces=budget_case;eng.factory.get_max_tool_turns.return_value=2
    nat=eng.factory.get_native_agent.return_value;nat.gateway.max_output_tokens=32768;nat.gateway.last_outbound={}
    remember=MagicMock();monkeypatch.setattr(agents,'remember_output_cap',remember)
    nat.gateway.escalate_output_cap=MagicMock(side_effect=lambda:AIGateway.escalate_output_cap(nat.gateway))
    stream=iter([_turn(tool_calls=[_tc('edit')]),_turn(reasoning='capped reasoning',truncated=True)])
    caps=[]
    def scripted(**kwargs):
        result=next(stream);caps.append(nat.gateway.max_output_tokens);nat.gateway.last_usage={'completion_tokens':32768 if result.truncated else 7,'reasoning_tokens':32768 if result.truncated else 0};return result
    nat.turn.side_effect=scripted
    with pytest.raises(NativeOutputCapExhausted):_run(eng,ws)
    assert caps==[32768,32768] and nat.gateway.max_output_tokens==32768
    nat.gateway.escalate_output_cap.assert_not_called();remember.assert_not_called()
    assert draft.read_text()=='partial draft; not completed\n'
    failure=next(x for _,event,x in traces if event=='output_cap_exhausted')
    assert failure['turn_budget']=={'max_turns':2,'turns_used':2}
    assert failure['output_budget']['completion_tokens']==32775 and failure['output_budget']['reasoning_tokens']==32768
    assert failure['output_budget']['escalations_used']==0
    assert failure['written_files']==['partial.gd']


def test_last_paid_correction_batch_settles_on_reclaim_without_another_provider_turn(tmp_path,monkeypatch):
    from types import SimpleNamespace
    sf,rid,claim,root=f.run_fixture(tmp_path,monkeypatch)
    batch=SimpleNamespace(text='',reasoning_content='',truncated=False,tool_calls=[
        {'id':'B','function':{'name':'create','arguments':json.dumps({'file':'B.py','content':'B=True\n'})}},
        {'id':'finish','function':{'name':'finish_step','arguments':'{}'}}])
    calls=[];eng,ws=traced_host(sf,rid,claim,root,[f.response('create',file='A.py',content='A=True\n'),starve(),batch],calls,3)
    trace=eng._trace_cb
    def crash_after_fence(category,event,payload):
        trace(category,event,payload)
        if event=='side_effect_completed' and 'B.py' in payload.get('written_files',[]):raise KeyboardInterrupt('owned after B effect fence before receipt')
    eng._trace_cb=crash_after_fence
    with pytest.raises(KeyboardInterrupt):f.execute(eng,ws,rid,claim)
    assert len(calls)==3 and (root/'B.py').read_text()=='B=True\n'
    fences=copy.deepcopy(payloads(sf,'side_effect_completed'))
    recovered,ws=traced_host(sf,rid,claim,root,[AssertionError('provider must not be called for retained paid batch')],calls,3)
    assert f.execute(recovered,ws,rid,claim) is True
    assert len(calls)==3 and payloads(sf,'side_effect_completed')==fences
    assert payloads(sf,'native_batch_recovery')[-1]['provider_called'] is False
    assert payloads(sf,'output_starvation_recovered')[-1]['turn']==3
    assert (root/'A.py').read_text()=='A=True\n' and (root/'B.py').read_text()=='B=True\n'
    sf._conn.close()
