"""Exercise production gateway/runner hooks; the endpoint response is synthetic."""
from types import SimpleNamespace
import copy
import pytest

from aitelier.writing_bench import adapter
from aitelier.writing_bench.reading import Coverage, ReviewSession, load_certificate
from aitelier.writing_bench.storage import BenchError, sha, encode, git
from test_bench import bench, request, staged, accept
from test_workflow import Session
from test_presented_coverage import coverage, report, full, tool_message, native_page


def test_successful_outbound_only_and_after_real_projection():
    from core.ai_router import AIGateway
    from core.dpe_pipeline import _project_native_messages
    class StubGateway(AIGateway):
        def __init__(self, callback):
            self.on_messages_presented=callback;self.last_outbound=None;self.fail=False
        @property
        def active_model(self):return 'fixture/model'
        def _build_kwargs(self, messages, **extra):
            return {'messages':self._sanitize_messages(messages)}
        def _complete_prebuilt(self, kwargs):
            self.received=copy.deepcopy(kwargs['messages'])
            if self.fail:raise RuntimeError('provider did not answer')
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='ok',tool_calls=[]),finish_reason='stop')])
    c=coverage(long=True);p,ctx=list(c.materials.values());gateway=StubGateway(c.observe)
    visible,_=_project_native_messages(tool_message(native_page(ctx)))
    gateway.generate_native(visible)
    assert c.ranges[ctx.path]==[]
    gateway.fail=True
    with pytest.raises(RuntimeError):gateway.generate_native([{'role':'user','content':p.text}])
    assert c.ranges[p.path]==[]
    gateway.fail=False;gateway.generate_native([{'role':'user','content':p.text}])
    assert c.ranges[p.path]==[[0,len(p.text)]]


def test_runner_refuses_verdict_before_framework_write(monkeypatch,tmp_path):
    from core.dpe_pipeline import PipelineEngine
    import api.dependencies as deps
    engine=object.__new__(PipelineEngine)
    engine._run_id='r';engine._current_step='literary_review'
    engine._step_instance_id=1;engine._tool_schemas={'write_verdict':{}}
    engine._write_scope_refusal=lambda *a:None
    c=coverage();engine._writing_review_session=ReviewSession(c,tmp_path,{'run_id':'r','step_id':'literary_review','step_instance_id':1})
    calls=[];monkeypatch.setattr(deps,'get_skillflow',lambda:SimpleNamespace(execute_tool=lambda *a,**k:calls.append((a,k)) or {'written':True}))
    result=engine._exec_tool({'tool':'write_verdict','params':report()})
    assert result['missing_review_material'] and not calls
    full(c)
    result=engine._exec_tool({'tool':'write_verdict','params':report()})
    assert result['written'] and len(calls)==1
    assert load_certificate(tmp_path,'literary')['report_sha256']==sha(encode(report()))


def test_real_graph_pass_flags_without_host_certificate_never_stage(tmp_path,monkeypatch,bench):
    from skillflow import StepResult
    session=Session(tmp_path,monkeypatch,bench);run=session.start(request(bench))
    session.sf.advance_run(run);claim=session.sf.claim_next_step(run)
    assert claim.step_id=='literary_review'
    identity,_=bench.review_materials(run,'literary')
    value={'review_key':identity['review_key'],'reviewed_chapters':identity['targets'],
           'passed':True,'read_complete':True,'feedback':'伪称已看完','findings':[]}
    saved=session.sf.execute_tool('write_verdict',value,run_id=run,step_id=claim.step_id,
             step_instance_id=claim.token.step_instance_id,claim_epoch=claim.token.claim_epoch)
    assert 'error' not in saved
    session.sf.confirm_step(claim.token,StepResult(outputs={'written':saved}))
    with pytest.raises((BenchError,OSError)):
        session.drive(run)
    assert not (bench.work(run)/'stage.json').exists()
    assert git(bench.policy.repo,'rev-parse','HEAD')==bench.policy.genesis


def test_host_session_composes_from_exact_live_step(tmp_path,monkeypatch,bench):
    session=Session(tmp_path,monkeypatch,bench);run=session.start(request(bench))
    session.sf.advance_run(run);claim=session.sf.claim_next_step(run)
    e=SimpleNamespace(_config_name=adapter.CONFIG,_current_step=claim.step_id,_run_id=run,
                      _step_instance_id=claim.token.step_instance_id,_claim_epoch=claim.token.claim_epoch)
    observed=adapter.begin_observed_review(e)
    assert observed.claim['step_instance_id']==claim.token.step_instance_id
    identity,m=bench.review_materials(run,'literary')
    observed.observe([{'role':'user','content':x.text} for x in m])
    value={'review_key':identity['review_key'],'reviewed_chapters':identity['targets'],
           'passed':True,'read_complete':True,'feedback':'当前稿具体判断','findings':[]}
    assert observed.guard('create_verdict',value) is None
    assert load_certificate(bench.work(run)/'reading','literary')['claim']['run_id']==run
    assert adapter.begin_observed_review(SimpleNamespace(_config_name='other',_current_step='literary_review')) is None


def test_already_accepted_backup_survives_code_upgrade_not_new_approval(bench,monkeypatch):
    from aitelier.writing_bench import bench as domain
    stage=staged(bench);receipt=accept(bench,stage)
    monkeypatch.setattr(domain,'engine_identity',lambda:'f'*64)
    assert bench.accepted('run1',stage['commit'])==receipt
    with pytest.raises(BenchError,match='implementation changed'):
        bench.promote('run1',sha(encode(stage)))
    proof={'verified':True,'repository_private_after':True,'remote_master_after':stage['commit'],
           'remote_tag_after':bench.policy.genesis,'force':False,'mirror':False}
    assert bench.record_backup('run1',stage['commit'],proof)['status']=='backed_up'


def test_read_only_real_model_probe_has_valid_source_and_no_delivery_calls():
    import ast
    from pathlib import Path
    p=Path(__file__).resolve().parents[2]/'scripts/writing_bench_review_probe.py'
    tree=ast.parse(p.read_text())
    called={node.func.attr for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)}
    assert not {'promote','record_backup','approve_checkpoint'} & called
    assert {'generate_native','observe','guard'} <= called
