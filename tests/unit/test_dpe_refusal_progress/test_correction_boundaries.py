"""Normal SDK/runner regressions for the first independent Source findings."""
import json
from pathlib import Path

import pytest

from core.dpe_pipeline import PipelineEngine, ScopeObservationUnavailable
from tests.unit.test_dpe_refusal_progress import test_progress as f


def receipt(claim):
    return Path(claim.inputs['_artifact_dir'])/'write_scope_receipt.json'


def seed(c):
    claim=c.claim()
    c.execute(claim,f.denied(c.mode),f.finish(c.mode))
    return claim,json.loads(receipt(claim).read_text())


def test_fixed_code_slot_activates_from_normal_runner(tmp_path,monkeypatch):
    c=f.Case(tmp_path,monkeypatch,'json-content'); claim,data=seed(c)
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
    c=f.Case(tmp_path,monkeypatch,'json-content');claim,data=seed(c)
    assert claim.inputs['_has_code_output'] is True
    assert data['violations'] and 'scope_progress_version' not in data
    assert data.get('scope_cause') is None


@pytest.mark.parametrize('damage',['delete-cause','delete-witness','null-cause','null-witness','version'])
def test_required_v1_history_is_not_a_fresh_budget(tmp_path,monkeypatch,damage):
    c=f.Case(tmp_path,monkeypatch,'json-tool'); first,data=seed(c)
    second=c.review_again(first)
    if damage=='delete-cause':data.pop('scope_cause')
    elif damage=='delete-witness':data.pop('response_witness')
    elif damage=='null-cause':data['scope_cause']=None
    elif damage=='null-witness':data['response_witness']=None
    else:data['scope_progress_version']=9
    target=receipt(second);target.write_text(json.dumps(data));raw=target.read_bytes()
    before=len(c.requests)
    with pytest.raises(ScopeObservationUnavailable):c.execute(second,f.finish(c.mode))
    assert len(c.requests)==before
    assert target.read_bytes()==raw, 'damaged bytes must not be silently migrated/overwritten'


def test_deleted_required_item_receipt_is_not_fresh(tmp_path,monkeypatch):
    c=f.Case(tmp_path,monkeypatch,'json-tool');first,_=seed(c)
    second=c.review_again(first);receipt(second).unlink()
    before=len(c.requests)
    with pytest.raises(ScopeObservationUnavailable):c.execute(second,f.finish(c.mode))
    assert len(c.requests)==before and not receipt(second).exists()


def test_intact_legacy_raw_history_can_carry_into_correction(tmp_path,monkeypatch):
    c=f.Case(tmp_path,monkeypatch,'json-tool');first,data=seed(c)
    second=c.review_again(first)
    for k in ('scope_progress_version','scope_cause','response_witness'):data.pop(k)
    receipt(second).write_text(json.dumps(data))
    good={'actions':[f.action('create',file='allowed.py',content='LEGACY = True\n')]}
    c.execute(second,good)
    after=json.loads(receipt(second).read_text())
    assert after['violations'][0]==data['violations'][0]
    assert after['scope_cause']['index']==0 and after['response_witness']['carry_match']
    assert (c.root/'allowed.py').read_text()=='LEGACY = True\n'


def test_first_denial_without_successful_next_response_needs_no_witness(tmp_path,monkeypatch):
    c=f.Case(tmp_path,monkeypatch,'json-tool');first=c.claim()
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
    c=f.Case(tmp_path,monkeypatch,'native');initial,_=seed(c)
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
    c=f.Case(tmp_path,monkeypatch,'native');initial,data=seed(c)
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
