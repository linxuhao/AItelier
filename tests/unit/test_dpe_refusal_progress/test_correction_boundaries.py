"""Normal SDK/runner regressions for the first independent Source findings."""
import json
from pathlib import Path

import pytest

from core.dpe_pipeline import PipelineEngine, ScopeObservationUnavailable
from tests.unit.test_dpe_refusal_progress import test_progress as f



def item_case(tmp_path, monkeypatch, mode):
    # The accepted baseline fixture has no edge back to its loop header, so
    # SDK97 correctly reports loop_item=None. Keep that fixture unchanged and
    # add the real return edge here to exercise actual item-bound directories.
    node = f.StepNode
    def with_return(**kwargs):
        if kwargs.get('id') == 'review':
            kwargs['transitions'] = [
                f.Transition(to='task_loop', match={'from_file':'review_verdict.json','field':'passed','value':True}),
                f.Transition(to='t_impl', match={'from_file':'review_verdict.json','field':'passed','value':False}, max_loop=3)]
        return node(**kwargs)
    monkeypatch.setattr(f, 'StepNode', with_return)
    try:
        return f.Case(tmp_path, monkeypatch, mode)
    finally:
        monkeypatch.setattr(f, 'StepNode', node)


def receipt(claim):
    return Path(claim.inputs['_artifact_dir'])/'write_scope_receipt.json'


def seed(c):
    claim=c.claim()
    row=next(r for r in c.sf.get_steps(c.run) if r['id']==claim.token.step_instance_id)
    assert row['loop_item']=='card_a' and Path(claim.inputs['_artifact_dir']).name=='card_a'
    c.execute(claim,f.denied(c.mode),f.finish(c.mode))
    return claim,json.loads(receipt(claim).read_text())


def test_fixed_code_slot_activates_from_normal_runner(tmp_path,monkeypatch):
    c=item_case(tmp_path,monkeypatch,'json-content'); claim,data=seed(c)
    assert data['scope_progress_version']==1
    assert data['scope_cause']['index']==0
    assert data['response_witness']['origin']['step_instance_id']==claim.token.step_instance_id
    assert data['response_witness']['carry_match'] is True


def test_genuine_mixed_slot_does_not_activate(tmp_path,monkeypatch):
    node=f.StepNode
    def mixed(**kw):
        if kw.get('id')=='t_impl':
            kw['output_target']='artifact'
            kw['output_fixed']={'file':{'file':'*.py','target':'code'},
                                'note':{'file':'note.md','target':'artifact'}}
        return node(**kw)
    monkeypatch.setattr(f,'StepNode',mixed)
    c=item_case(tmp_path,monkeypatch,'json-content');claim=c.claim()
    c.execute(claim,f.denied(c.mode),f.finish(c.mode))
    assert claim.inputs['_has_code_output'] is True and len(c.denials())==1
    if receipt(claim).exists():
        assert 'scope_progress_version' not in json.loads(receipt(claim).read_text())


@pytest.mark.parametrize('damage',['delete-cause','delete-witness','null-cause','null-witness','version','delete-all-progress'])
def test_required_v1_history_is_not_a_fresh_budget(tmp_path,monkeypatch,damage):
    c=item_case(tmp_path,monkeypatch,'json-tool'); first,data=seed(c)
    second=c.review_again(first)
    if damage=='delete-cause':data.pop('scope_cause')
    elif damage=='delete-witness':data.pop('response_witness')
    elif damage=='null-cause':data['scope_cause']=None
    elif damage=='null-witness':data['response_witness']=None
    elif damage=='delete-all-progress':
        for k in ('scope_progress_version','scope_cause','response_witness'):data.pop(k)
    else:data['scope_progress_version']=9
    target=receipt(second);target.write_text(json.dumps(data));raw=target.read_bytes()
    before=len(c.requests)
    with pytest.raises(ScopeObservationUnavailable):c.execute(second,f.finish(c.mode))
    assert len(c.requests)==before
    assert target.read_bytes()==raw, 'damaged bytes must not be silently migrated/overwritten'


def test_deleted_required_item_receipt_is_not_fresh(tmp_path,monkeypatch):
    c=item_case(tmp_path,monkeypatch,'json-tool');first,_=seed(c)
    second=c.review_again(first);receipt(second).unlink()
    before=len(c.requests)
    with pytest.raises(ScopeObservationUnavailable):c.execute(second,f.finish(c.mode))
    assert len(c.requests)==before and not receipt(second).exists()


def test_intact_legacy_raw_history_can_carry_into_correction(tmp_path,monkeypatch):
    from core.write_scope import WriteScope
    c=item_case(tmp_path,monkeypatch,'json-tool');claim=c.claim()
    scope=WriteScope('card_a',('allowed.py','partial.py'))
    raw={**scope.refusal('create','forbidden.py'),
         'step_instance_id':claim.token.step_instance_id,'claim_epoch':claim.token.claim_epoch}
    data={'run_id':c.run,'step_id':'t_impl','task':'card_a','policy':scope.policy,
          'allowed_scope':scope.allowed(),'violations':[raw],
          'claim_history':[{'step_instance_id':claim.token.step_instance_id,'claim_epoch':claim.token.claim_epoch}]}
    receipt(claim).parent.mkdir(parents=True,exist_ok=True)
    receipt(claim).write_text(json.dumps(data))
    good={'actions':[f.action('create',file='allowed.py',content='LEGACY = True\n')]}
    c.execute(claim,good)
    after=json.loads(receipt(claim).read_text())
    assert after['violations'][0]==raw
    assert after['scope_cause']['index']==0 and after['response_witness']['carry_match']
    assert (c.root/'allowed.py').read_text()=='LEGACY = True\n'


def test_first_denial_without_successful_next_response_needs_no_witness(tmp_path,monkeypatch):
    c=item_case(tmp_path,monkeypatch,'json-tool');first=c.claim()
    with pytest.raises(KeyboardInterrupt):
        c.execute(first,f.denied(c.mode),KeyboardInterrupt('no returned action'))
    before=json.loads(receipt(first).read_text())
    assert before['scope_cause'] and before['response_witness'] is None
    c.sf.release_claim(first.token,'pure generation interrupted')
    again=c.claim()
    c.execute(again,{'actions':[f.action('create',file='allowed.py',content='OK\n')]})
    after=json.loads(receipt(again).read_text())
    assert after['scope_cause']['hash']==before['scope_cause']['hash']
    assert (c.root/'allowed.py').read_text()=='OK\n'


@pytest.mark.parametrize('commit_before_crash',[False,True])
def test_recovered_identical_batch_binds_original_producer(tmp_path,monkeypatch,commit_before_crash):
    c=item_case(tmp_path,monkeypatch,'native');initial,_=seed(c)
    a=c.review_again(initial)
    batch=f.native(f.action('read',path='seed.py'),f.action('finish_step',summary='done'))
    c.execute(a,batch)
    old=json.loads(receipt(a).read_text())['response_witness']
    b=c.review_again(a)
    original=PipelineEngine._scope_commit_response
    def crash(self,*args,**kwargs):
        if self._step_instance_id==b.token.step_instance_id:
            if commit_before_crash:original(self,*args,**kwargs)
            raise KeyboardInterrupt('after assistant trace, before tools')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(PipelineEngine,'_scope_commit_response',crash)
    with pytest.raises(KeyboardInterrupt):c.execute(b,batch)
    stored=json.loads(receipt(b).read_text())['response_witness']
    assert stored['response_digest']==old['response_digest']
    assert stored['positions']==old['positions']
    monkeypatch.setattr(PipelineEngine,'_scope_commit_response',original)
    c.sf.release_claim(b.token,'interrupted before any dependent tools')
    again=c.claim();observed=[]
    recovered=PipelineEngine._scope_recovered_batch
    def observe(self,*args,**kwargs):
        recovered(self,*args,**kwargs);observed.append(self._scope_batch.get('witness'))
    monkeypatch.setattr(PipelineEngine,'_scope_recovered_batch',observe)
    before=len(c.requests);c.execute(again)
    assert len(c.requests)==before and len(observed)==1
    if commit_before_crash:
        assert observed[0]['origin']=={'mode':'native','step_instance_id':b.token.step_instance_id,
                                       'claim_epoch':b.token.claim_epoch}
        assert again.token.claim_epoch>b.token.claim_epoch
    else:
        assert observed[0] is None, 'same bytes/positions must not borrow the other producer witness'
    assert not (c.root/'forbidden.py').exists()


def test_unmatched_recovered_denial_retains_same_original_cause(tmp_path,monkeypatch):
    c=item_case(tmp_path,monkeypatch,'native');initial,data=seed(c)
    b=c.review_again(initial);original=PipelineEngine._scope_commit_response
    def crash(*args,**kwargs):raise KeyboardInterrupt('before witness commit')
    monkeypatch.setattr(PipelineEngine,'_scope_commit_response',crash)
    with pytest.raises(KeyboardInterrupt):c.execute(b,f.denied('native'))
    monkeypatch.setattr(PipelineEngine,'_scope_commit_response',original)
    c.sf.release_claim(b.token,'no dependent mutation admitted');again=c.claim()
    c.execute(again,f.native(f.action('create',file='allowed.py',content='RECLAIM = True\n')),f.finish('native'))
    after=json.loads(receipt(again).read_text())
    assert len(after['violations'])==2
    assert after['scope_cause']['hash']==data['scope_cause']['hash']
    assert after['scope_cause']['index']==data['scope_cause']['index']==0
    assert after['scope_cause']['error']==data['violations'][0]
    assert (c.root/'allowed.py').read_text()=='RECLAIM = True\n'


def test_unresolved_task_scope_keeps_existing_denial_semantics(tmp_path,monkeypatch):
    c=item_case(tmp_path,monkeypatch,'native');claim=c.claim()
    claim.inputs['_resolved_context'].pop('Step 3 — tasks/card_a.json')
    c.execute(claim,f.denied('native'),f.finish('native'))
    data=json.loads(receipt(claim).read_text())
    assert data['policy']=='missing-task-card-deny'
    assert 'scope_progress_version' not in data
    assert len(c.denials())==1 and not (c.root/'forbidden.py').exists()


@pytest.mark.parametrize('mode',['native','json-tool','json-content'])
def test_real_item_bound_review_retry_has_same_cause(tmp_path,monkeypatch,mode):
    from core.dpe_pipeline import RepeatedScopeRefusal
    c=item_case(tmp_path,monkeypatch,mode);first,data=seed(c)
    second=c.review_again(first)
    rows=[r for r in c.sf.get_steps(c.run) if r['step_id']=='t_impl']
    assert len(rows)==2 and all(r['loop_item']=='card_a' for r in rows)
    assert Path(second.inputs['_artifact_dir']).name=='card_a'
    before=len(c.requests)
    with pytest.raises(RepeatedScopeRefusal):c.execute(second,f.denied(mode))
    after=json.loads(receipt(second).read_text())
    assert len(c.requests)==before+1
    assert after['scope_cause']['hash']==data['scope_cause']['hash']
    assert after['violations'][0]==data['violations'][0]
    assert after['violations'][-1]['repeated_after_feedback'] is True
    assert any(data['scope_cause']['error']['error'] in str(m.get('content',''))
               for m in c.requests[-1]['messages'])
    c.evidence()
