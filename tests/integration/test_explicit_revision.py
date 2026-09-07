from pathlib import Path
from unittest.mock import MagicMock
import pytest
from skillflow.core import SkillFlow,StepResult
from skillflow.graph import PipelineGraph,StepNode,Transition
from core.workspace_manager import WorkspaceManager
from core.dpe_pipeline import NativeTurnBudgetExhausted
from tests.integration.test_native_parity import engine,_turn,_tc

@pytest.fixture
def revision(tmp_path,monkeypatch,engine):
 sf=SkillFlow(str(tmp_path/'sf.db'),workspace_base=str(tmp_path/'ws'))
 sf.register_graph(PipelineGraph(name='revision',begin='a',steps=[StepNode(id='a',step_type='agent',checkpoint=True,transitions=[Transition(to='b')]),StepNode(id='b',step_type='agent')]))
 rid=sf.create_run('revision',project_id='p');sf.start_run(rid);sf.advance_run(rid);old=sf.claim_next_step(rid)
 sf.confirm_step(old.token,StepResult(outputs={'old':'yes'}));sf.advance_run(rid)
 sf.trace(rid,'agent','prompt_delta',{'turn':1,'message':{'role':'user','content':'old revision'}},step_id='a',step_instance_id=old.token.step_instance_id)
 sf.reject_checkpoint(rid,'a','retain untouched cards');new=sf.claim_next_step(rid)
 monkeypatch.setattr('api.dependencies.get_skillflow',lambda:sf)
 ws=WorkspaceManager(str(tmp_path/'ws'),projects_base=str(tmp_path/'projects'))
 code=tmp_path/'code';code.mkdir();monkeypatch.setattr(ws,'get_code_path',lambda *a,**k:code)
 final=ws._final_dir('p','a','revision');final.mkdir(parents=True,exist_ok=True);(final/'untouched.json').write_text('prior card')
 draft=ws._draft_dir('p','a','revision');draft.mkdir(parents=True,exist_ok=True);(draft/'stale.json').write_text('unpromoted garbage')
 eng=engine;eng._trace=lambda cat,ev,payload:sf.trace(rid,cat,ev,payload,step_id='a',step_instance_id=new.token.step_instance_id)
 eng.factory.get_max_tool_turns.return_value=2
 return sf,rid,old,new,eng,ws,draft

def run_case(c):
 sf,rid,old,new,e,w,d=c
 return e.run_step(task_id=1,step_id='a',workspace=w,project_id='p',agent_config_name='pm',run_id=rid,step_instance_id=new.token.step_instance_id,claim_epoch=new.token.claim_epoch,output_dir=str(d),carry_forward=True,tool_schemas={'read_file':{},'edit':{}})

def test_fresh_budget_and_promoted_carry_forward(revision):
 sf,rid,old,new,e,w,d=revision
 before=sf.get_trace(rid,step_instance_id=old.token.step_instance_id)
 nat=e.factory.get_native_agent.return_value
 def finish(*a,**kw):
  assert (d/'untouched.json').read_text()=='prior card'
  assert not (d/'stale.json').exists()
  return _turn(tool_calls=[_tc('finish_step')])
 nat.turn.side_effect=finish
 assert run_case(revision)
 assert nat.turn.call_count==1
 assert sf.get_trace(rid,step_instance_id=old.token.step_instance_id)==before
 assert 'retain untouched cards' in new.inputs['_feedback']

def test_same_revision_exhaustion_never_resets(revision):
 sf,rid,old,new,e,w,d=revision
 nat=e.factory.get_native_agent.return_value
 def edit(action):
  (d/'changed.json').write_text('revision partial');return {'edited':'changed.json'}
 e._exec_tool=MagicMock(side_effect=edit)
 nat.turn.side_effect=[_turn(tool_calls=[_tc('edit')]),_turn(tool_calls=[_tc('read_file')])]
 with pytest.raises(NativeTurnBudgetExhausted):run_case(revision)
 assert nat.turn.call_count==2
 nat.turn.reset_mock()
 sf.release_claim(new.token,'simulated interrupted host')
 reclaimed=sf.claim_next_step(rid)
 assert reclaimed.token.step_instance_id==new.token.step_instance_id
 revision=(sf,rid,old,reclaimed,e,w,d)
 with pytest.raises(NativeTurnBudgetExhausted):run_case(revision)
 nat.turn.assert_not_called()
 assert (d/'changed.json').read_text()=='revision partial'
 assert (d/'untouched.json').read_text()=='prior card'


def test_crash_same_revision_resumes_own_turn_and_draft(revision):
 sf,rid,old,new,e,w,d=revision
 nat=e.factory.get_native_agent.return_value
 def edit(action):
  (d/'changed.json').write_text('new partial');return {'edited':'changed.json'}
 e._exec_tool=MagicMock(side_effect=edit)
 class Crash(BaseException):pass
 nat.turn.side_effect=[_turn(tool_calls=[_tc('edit')]),Crash()]
 with pytest.raises(Crash):run_case(revision)
 sf.release_claim(new.token,'simulated interrupted host')
 reclaimed=sf.claim_next_step(rid)
 assert reclaimed.token.step_instance_id==new.token.step_instance_id
 revision=(sf,rid,old,reclaimed,e,w,d)
 nat.turn.reset_mock();nat.turn.side_effect=[_turn(tool_calls=[_tc('finish_step')])]
 assert run_case(revision)
 assert nat.turn.call_count==1
 assert (d/'changed.json').read_text()=='new partial'
 assert e._resume_from_trace('p',2)['turns']==2

@pytest.mark.asyncio
async def test_real_runner_forwards_carry_forward(revision,monkeypatch):
 from aitelier.runner import AgentStepRunner
 sf,rid,old,new,e,w,d=revision
 new.step_config['output']={'carry_forward':True}
 db=MagicMock();db.get_repo_info.return_value={};db.get_project.return_value=None
 monkeypatch.setattr(w,'setup_workspace',lambda *a,**k:None)
 called=[]
 e.run_step=lambda **kw:called.append(kw) or True
 monkeypatch.setattr('core.dpe_pipeline.PipelineEngine',lambda **kw:e)
 await AgentStepRunner(db,w).execute(new)
 assert called[0]['carry_forward'] is True
 assert called[0]['step_instance_id']==new.token.step_instance_id
 assert called[0]['resolved_context']==new.inputs['_resolved_context']


def test_missing_carry_forward_control(revision):
 sf,rid,old,new,e,w,d=revision
 nat=e.factory.get_native_agent.return_value
 seen=[]
 def finish(*a,**kw):
  seen.append((d/'untouched.json').exists())
  return _turn(tool_calls=[_tc('finish_step')])
 nat.turn.side_effect=finish
 assert e.run_step(task_id=1,step_id='a',workspace=w,project_id='p',agent_config_name='pm',run_id=rid,step_instance_id=new.token.step_instance_id,output_dir=str(d),carry_forward=False,tool_schemas={'read_file':{}})
 assert seen==[False]
