"""Identical literal round scenarios for fake and a FUTURE genuine graph binding.

No provider-kind skip/xfail. Future fixture controls must use actual host owner
context/checkpoint CAS/providers and observations, not fabricate lifecycle facts.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
from typing import Protocol
from .contract import OptimizerError, ROLES

ROOT=Path(__file__).parent
SCENARIOS=json.loads((ROOT/'fixtures/scenarios.json').read_text())['scenarios']
LITERAL=json.loads((ROOT/'fixtures/round.json').read_text())
CAMPAIGN=json.loads((ROOT/'fixtures/campaign_scenarios.json').read_text())


class Fixture(Protocol):
    evidence_kind: str
    actor: str  # test fixture chooses actual caller context, NEVER a wire field
    def prepare(self,kind='generated'): ...
    def invoke(self,port,payload): ...
    def snapshot(self): ...
    def observations(self): ...
    def set_fault(self,name): ...
    def approve_checkpoint(self): ...
    def selection_authorization(self): ...
    def approve_adoption(self): ...
    def configure_budget(self,requests): ...


def edit_fixture(kind):
    e=copy.deepcopy(LITERAL['useful_edit']);key=LITERAL['prompt_key']
    if kind=='case_id': e['prompts'][key]+=' '+LITERAL['forbidden_case_ids'][0]
    elif kind=='case_literal': e['prompts'][key]+=' '+LITERAL['forbidden_literals'][0]
    elif kind in ('topology','route'):
        e['graph']=copy.deepcopy(LITERAL['graph'])
        if kind=='topology': e['graph']['begin']='different_step'
        else: e['graph']['route']='candidate-controlled-route'
    elif kind=='model': e['prompts']={'model:investigator':'new model'}
    elif kind=='unknown': e['model']='changed'
    elif kind=='empty': e={}
    elif kind=='second_useful': e['prompts'][key]+='Second proposal: quote the line that defines the answer.\n'
    elif kind!='useful': raise ValueError('unknown literal edit')
    return e


def run_scenario(factory, vector):
    f=factory()
    if 'requests' in vector:f.configure_budget(vector['requests'])
    q=f.prepare(vector.get('kind','generated'));request=q.model_dump(mode='json')
    if 'round_number' in vector: request['round_number']=vector['round_number']
    if 'usd_micros' in vector: request['budget']['round']['upper']['usd_micros']=vector['usd_micros']
    observed=f.observations();reference=observed['view']['reference_version']
    if vector.get('adoption'): f.approve_adoption()
    if vector.get('deny_holdout'): f.holdout_allowed=False  # fixture control, native changes actual grant
    ports=[];error=None
    for _ in range(24):
        observed=f.observations();view=observed['view'];port=view['phase']
        if view['stopped']: break
        if observed.get('pending_checkpoint'):
            if not vector.get('skip_checkpoint'):
                f.actor='owner';f.approve_checkpoint()
        if vector.get('early') and port=='freeze': port='holdout'
        f.actor=ROLES[port]
        if vector.get('wrong_actor')==port: f.actor='proposer'
        if vector.get('at')==port: f.set_fault(vector['fault'])
        if port=='freeze':payload=request
        elif port=='diagnose':payload={'observation':'Train feedback shows missing cited evidence.',
            'train_case_ids':['holdout-01'] if vector.get('diagnosis_holdout') else ['train-01']}
        elif port=='hypothesis':payload={'hypothesis_id':'one-hypothesis','change':'Ask for explicit evidence citations.',
            'falsifiable_prediction':'Mean normalized train score improves, with fixed settings and equal counts.',
            'metric':'mean_normalized','train_case_ids':['train-01']}
        elif port=='candidate':payload=edit_fixture(vector.get('edit','useful'))
        elif port=='holdout_seal':payload=f.selection_authorization()
        elif port=='decision':payload={'action':vector.get('decision','candidate'),
            'review_ref':'reviewed' if vector.get('adoption') else None,
            'owner_decision_ref':'owner-approved' if vector.get('adoption') else None}
        elif port=='stop':payload={'reason':view['stop_reason'] or 'owner_stop'}
        else:payload={}
        before=f.snapshot()
        try:f.invoke(port,payload)
        except OptimizerError as exc:
            error=exc.code
            assert vector.get('error')==error and vector['error_at']==port,(vector,port,error)
            assert f.snapshot()==before,'refusal mutated owned state'
            break
        ports.append(port)
    else:raise AssertionError('round did not stop in finite graph')
    out=f.observations();view=out['view']
    assert out['evidence_kind']==f.evidence_kind
    if 'error' in vector:assert error==vector['error']
    else:
        assert error is None and view['stopped'] and view['stop_reason']==vector['stop']
        if 'numeric' in vector:assert out['numeric']['holdout']['status']==vector['numeric']
        if vector['name']=='family_nonregression':assert 'family_regression' in out['numeric']['holdout']['rejection_reasons']
        if 'known_requests' in vector:assert view['known']['requests']==vector['known_requests']
        if 'difference' in vector:
            assert out['numeric']['holdout']['overall_difference']['exact']==vector['difference']
            assert out['numeric']['holdout']['sign_flip_p']['exact']==vector['p']
        if vector.get('unknown'):
            assert view['retained_unknown']['usd_micros']>0 and not view['quiescent']
            assert not out['holdout_consumed']
        else:assert not any(view['retained_unknown'].values()) and view['quiescent']
    current=vector.get('current','reference')
    if current=='reference':assert view['current_version']==reference and not out['current_history']
    elif current=='candidate':assert view['current_version']==view['candidate_version'] and out['current_history']==[view['candidate_version']]
    else:assert view['current_version'] not in (reference,view['candidate_version'])
    # Numeric result alone/manual default/rejection never makes candidate current.
    assert not view['grants_authority']
    if view['public_train']:
        public=json.dumps(view['public_train']);assert 'HOLDOUT' not in public and 'private_result' not in public and 'run_id' not in public
    return {'scenario':vector['name'],'evidence_kind':f.evidence_kind,'ports':ports,
            'error':error,'observations':out}


def run_shared(factory):
    return [run_scenario(factory,v) for v in SCENARIOS]


def _round_payload(f,port,view,step):
    if port=='diagnose':return {'observation':'Train feedback shows missing cited evidence.','train_case_ids':['train-01']}
    if port=='hypothesis':return {'hypothesis_id':'one-hypothesis','change':'Ask for explicit evidence citations.',
        'falsifiable_prediction':'Mean normalized train score improves, with fixed settings and equal counts.',
        'metric':'mean_normalized','train_case_ids':['train-01']}
    if port=='candidate':return edit_fixture(step.get('edit','useful'))
    if port=='holdout_seal':return f.selection_authorization()
    if port=='decision':return {'action':'candidate','review_ref':None,'owner_decision_ref':None}
    if port=='stop':return {'reason':view['stop_reason'] or 'owner_stop'}
    return {}


def run_campaign(factory, vector):
    """SAME literal multiround campaign for fake and native: rounds share the
    ORIGINAL campaign lifetime/amounts and the accepted scoreboard cohort."""
    c=CAMPAIGN['campaign'];f=factory()
    f.configure_campaign(c['rounds'],c['round_elapsed_ms'],c['campaign_elapsed_ms'])
    rounds={}
    for step in vector['steps']:
        if 'control' in step:f.campaign_control(step['control']);continue
        rid=step['round'];f.begin_round(rid)
        if 'requests' in step:f.configure_budget(step['requests'])
        q=f.prepare(step.get('kind','generated'));request=q.model_dump(mode='json')
        assert q.round_id==rid and q.round_number==c['rounds'][rid]
        if step.get('lost_host'):
            f.freeze_in_lost_host(q);rounds[rid]={'lost_host':True};continue
        ports=[];error=None
        for _ in range(24):
            observed=f.observations();view=observed['view'];port=view['phase']
            if view['stopped']:break
            if observed.get('pending_checkpoint'):f.actor='owner';f.approve_checkpoint()
            if step.get('owner_stop_at')==port:port='stop'
            f.actor=ROLES[port]
            if step.get('at')==port:f.set_fault(step['fault'])
            payload=request if port=='freeze' else _round_payload(f,port,view,step)
            before=f.snapshot()
            try:f.invoke(port,payload)
            except OptimizerError as exc:
                error=exc.code
                assert step.get('error')==error and step['error_at']==port,(vector['name'],rid,port,error)
                assert f.snapshot()==before,'refusal mutated owned state'
                break
            ports.append(port)
        else:raise AssertionError('round did not stop in finite graph')
        out=f.observations();view=out['view']
        if 'error' in step:assert error==step['error'],(rid,error)
        else:
            assert error is None and view['stopped'] and view['stop_reason']==step['stop'],(vector['name'],rid,view['stop_reason'],error)
            assert view['quiescent'] is step.get('quiescent',True)
        if 'ports' in step:assert ports==step['ports'],(rid,ports)
        if 'known_requests' in step:assert view['known']['requests']==step['known_requests'],(rid,view['known'])
        if 'numeric' in step:assert out['numeric']['holdout']['status']==step['numeric']
        if 'reference_reused' in step:assert out['reference_reused'] is step['reference_reused'],rid
        assert not view['grants_authority'] and view['current_version']==view['reference_version']
        rounds[rid]={'ports':ports,'error':error,'observations':out}
    campaign=f.campaign_observations()
    assert campaign['evidence_kind']==f.evidence_kind
    assert campaign['known']['requests']==vector['campaign_known_requests'],campaign['known']
    assert campaign['quiescent'] is vector['quiescent'],campaign
    return {'scenario':vector['name'],'evidence_kind':f.evidence_kind,'rounds':rounds,'campaign':campaign}
