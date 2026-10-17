"""Standalone round MODEL. No SDK/host/provider/benchmark execution or authority.

Accepted evaluator fake supplies explicitly simulated receipts. The ONLY numeric
selection implementation is the accepted pure evaluate_matrix function.
"""
from __future__ import annotations
import copy
import json
from fractions import Fraction
from pathlib import Path
from pydantic import ValidationError
from contracts.rsi.evaluator.fake import FakeFixture as EvaluationFake
from contracts.rsi.evaluator.contract import (
    EvaluationRequest, EvaluationResult, Refusal, digest, scorer_receipt, validate_result,
)
from contracts.rsi.pipeline_versioning.contract import VersioningError, version_id_of
from core.pipeline_optimization import evaluate_matrix
from .contract import (DESIGN, PORTS, ROLES, OptimizerError, RoundRequest, RoundView)

F = Path(__file__).with_name('fixtures')

def literal():
    return json.loads((F/'round.json').read_text())


def fail(code, detail=''):
    raise OptimizerError(code, detail)


def normalized(graph):
    """Semantic fake of native description/public-source-order whitelist."""
    if not isinstance(graph,dict) or not isinstance(graph.get('steps'),list) or not all(isinstance(s,dict) for s in graph['steps']): fail('FORBIDDEN_EDIT')
    g = copy.deepcopy(graph); g.pop('description', None)
    for s in g.get('steps', []):
        s.pop('description', None)
        ctx = s.get('context', [])
        def movable(c):
            source = c.get('source', {})
            return set(c) == {'source'} and 'config' in source and 'tool' not in source and not source.get('required')
        s['context'] = ['\0movable' if movable(c) else c for c in ctx]
        s['\0order'] = sorted(json.dumps(c,sort_keys=True) for c in ctx if movable(c))
    return g


class FakeRound:
    evidence_kind = 'simulation'

    def __init__(self):
        self.fixture = literal()
        self.phase = 'freeze'; self.actor = 'coordinator'
        self.request = None; self.proposal = self.candidate = None
        self.public = None; self.results = {}; self.numeric = {}
        self.stopped = False; self.stop_reason = None; self.unknown = 0
        self.pending_checkpoint = None; self.holdout_consumed = False; self.authorized = True
        self.holdout_allowed = True; self.adoption_approved = False
        self.fault = 'none'; self.events = []; self.current_history = []
        self.cas = self.fixture['target']['expected_current']
        self.current = self.fixture['reference_version']

    def prepare(self, kind='generated'):
        e = EvaluationFake(); q = e.prepare('train', ('reference',))
        target = dict(self.fixture['target'])
        if kind == 'builtin': target.update(kind='builtin',name='investigate',expected_current=None)
        return RoundRequest(schema='rsi.optimizer.round.v2',campaign_id='synthetic-campaign',
            round_id='synthetic-round-1',round_number=1,design=DESIGN,target=target,
            reference=q.selections[0],benchmark=q.benchmark,runtime=q.runtime,budget=q.budget)

    def set_fault(self, name):
        allowed = {'none','no_authority','unsafe_resume','provider_changed','runtime_drift',
                   'no_train_improvement','holdout_rejected','family_regression',
                   'unknown_execution','unknown_accounting','budget','concurrent_edit',
                   'holdout_reuse','inclusive_margin','target'}
        if name not in allowed: fail('UNKNOWN_INPUT')
        self.fault = name
        if name == 'no_authority': self.authorized = False
        if name == 'concurrent_edit':
            self.cas = 'f'*64; self.current = digest('foreign independently edited current')
        if name == 'holdout_reuse': self.holdout_consumed = True

    def view(self):
        return RoundView(phase=self.phase,reference_version=self.fixture['reference_version'],
            candidate_version=self.candidate,current_version=self.current,public_train=self.public,
            stopped=self.stopped,stop_reason=self.stop_reason,
            retained_unknown_usd_micros=self.unknown,quiescent=not bool(self.unknown),
            action=('retain' if self.phase=='restore' else getattr(self,'adoption_action','candidate')))

    def observations(self):
        return {'evidence_kind':self.evidence_kind,'view':self.view().model_dump(mode='json'),
                'events':copy.deepcopy(self.events),'current_history':list(self.current_history),
                'holdout_consumed':self.holdout_consumed,'pending_checkpoint':self.pending_checkpoint,'cas':self.cas,
                'numeric':copy.deepcopy(self.numeric)}

    def approve_checkpoint(self):
        # Fixture control: future proof uses the genuine host checkpoint CAS.
        if self.actor!='owner': fail('AUTHORITY')
        if self.pending_checkpoint is None: fail('ORDER')
        self.events.append({'checkpoint_approved':self.pending_checkpoint,'provenance':'simulation'})
        self.pending_checkpoint=None

    def snapshot(self):
        return copy.deepcopy((self.observations(),self.proposal,self.candidate,
                              getattr(self,'candidate_content',None),self.results))

    def selection_authorization(self):
        return {'selection':self.selection_seal}

    def approve_adoption(self):
        # Fixture's host-owned review/decision records, not a request JSON grant.
        self.adoption_approved=True

    def _check_edit(self, proposal):
        if not proposal.prompts and proposal.graph is None: fail('NO_ADMISSIBLE_EDIT')
        if set(proposal.prompts)-{self.fixture['prompt_key']}: fail('FORBIDDEN_EDIT')
        raw = json.dumps(proposal.model_dump(mode='json'),ensure_ascii=False)
        if any(v in raw for v in self.fixture['forbidden_case_ids'] + self.fixture['forbidden_literals']):
            fail('CONTAMINATION')
        if proposal.graph is not None and normalized(proposal.graph) != normalized(self.fixture['graph']):
            fail('FORBIDDEN_EDIT')
        c = copy.deepcopy(self.fixture['logical_reference'])
        if proposal.prompts:
            c['roles'][self.fixture['logical_role']]['prompt'] = proposal.prompts[self.fixture['prompt_key']]
        if proposal.graph is not None:
            # Real native binding does full native compilation/canonical projection;
            # fake preserves logical naming rather than inventing another codec.
            g = copy.deepcopy(proposal.graph); g['name']='$self'
            for step in g['steps']:
                if 'agent_config' in step: step['agent_config']=self.fixture['logical_role']
                for ctx in step.get('context',[]):
                    if ctx.get('source',{}).get('config') == self.fixture['graph']['name']:
                        ctx['source']['config']='$self'
            c['graph']=g
        return c

    def _evaluation(self, split, arms):
        e = EvaluationFake()
        if self.candidate is not None:
            e.fixture['identity']['candidate'] = {'logical_version':self.candidate,
                                                 'logical_content':self.candidate_content}
        q = e.prepare(split, arms)
        data=q.model_dump(mode='json')
        data.update(evaluation_id='sim-'+split+'-'+'-'.join(arms),
                    campaign_id=self.request.campaign_id,round_id=self.request.round_id,
                    budget=self.request.budget.model_dump(mode='json'),
                    holdout_selection=self.selection_seal if split=='holdout' else None)
        data['plan_identity']=digest({k:v for k,v in data.items() if k!='plan_identity'})
        q=EvaluationRequest.model_validate_json(json.dumps(data)); e.request=q
        # Existing evaluator types and ordering; ids below are short model receipt
        # references, NOT SDK run/pin/finish operations.
        if split=='train' and arms==('candidate',):
            fault={'unknown_execution':'unknown_execution','unknown_accounting':'unknown_usage',
                   'budget':'envelope','runtime_drift':'runtime_preflight'}.get(self.fault)
            if fault: e.set_fault(fault)
        out=e.evaluate(q)
        if isinstance(out,Refusal):
            self.stop_reason='invalid'; self.phase='stop'; return
        receipts=[]
        for i,r in enumerate(out.receipts):
            if r.checker and r.cell.arm=='candidate':
                points=r.checker.raw_points
                if self.fault=='no_train_improvement' or (split=='holdout' and self.fault=='holdout_rejected'): points=5
                if split=='holdout' and self.fault=='target': points=10
                if split=='holdout' and self.fault=='inclusive_margin':
                    first_four=[c.case_id for c in q.benchmark.cases if c.split=='holdout'][:4]
                    points=6 if r.cell.repetition==1 or (r.cell.repetition==2 and r.cell.case_id in first_four) else 5
                if split=='holdout' and self.fault=='family_regression':
                    points=4 if r.case.family=='family-d' else 7
                r=r.model_copy(update={'checker':r.checker.model_copy(update={'raw_points':points})})
            receipts.append(r.model_copy(update={'receipt_id':f'{split}-{r.cell.arm}-{i:03d}'}))
        # The unchanged projection is correct unless scores were deliberately
        # changed in this model's scorer fixture; rebuild ONLY its numeric fields.
        public=out.optimizer_projection
        if public is not None:
            public=public.model_copy(update={'cells':tuple(p.model_copy(update={
                'normalized_score':str(Fraction(r.checker.raw_points,r.checker.max_points)*100)
                if r.checker else p.normalized_score}) for p,r in zip(public.cells,receipts))})
        out=EvaluationResult(**{**out.model_dump(), 'receipts':tuple(receipts),
            'receipt_catalog':tuple((r.receipt_id,digest(r)) for r in receipts),
            'optimizer_projection':public})
        validate_result(q,out)
        self.results[(split,arms)] = (q,out)
        self.events.append({'evaluation':split,'arms':list(arms),'status':out.status,
                            'provenance':'simulation','cells':len(out.receipts)})
        if split=='train': self.public=out.optimizer_projection
        self.unknown += out.retained_unknown.usd_micros
        if out.status != 'complete':
            self.stop_reason=('unknown' if out.status=='unknown' else 'budget' if any(r.reason=='BUDGET_EXHAUSTED' for r in out.receipts) else 'invalid'); self.phase='stop'

    def _matrix(self, split):
        pairs=([self.results[('train',('reference',))],self.results[('train',('candidate',))]]
               if split=='train' else [self.results[('holdout',('reference','candidate'))]])
        q=pairs[0][0]
        selections={s.arm:s for req,result in pairs for s in req.selections}
        cases=q.benchmark.cases
        manifest={'train_case_ids':[c.case_id for c in cases if c.split=='train'],
          'holdout_case_ids':[c.case_id for c in cases if c.split=='holdout'],
          'case_families':{c.case_id:c.family for c in cases},
          'family_inventory':['family-a','family-b','family-c','family-d'],'repetitions':3,
          'arms':{a:{'bundle_id':s.native_bundle,'runtime_id':digest(q.runtime),
                     'checker_id':q.benchmark.checker_package} for a,s in selections.items()},
          'input_ids':{c.case_id:c.prompt_identity for c in cases}}
        result=evaluate_matrix(manifest,[scorer_receipt(r) for req,out in pairs for r in out.receipts],stage=split)
        self.numeric[split]=result['public_summary']
        return result

    def invoke(self, port, payload):
        if port not in PORTS: fail('UNKNOWN_PORT')
        if self.actor != ROLES[port]: fail('AUTHORITY')
        if self.stopped: fail('STOPPED')
        if self.pending_checkpoint and port!='stop': fail('CHECKPOINT')
        if self.phase != port and port!='stop': fail('ORDER')
        try:
            data=PORTS[port].model_validate_json(json.dumps(payload,allow_nan=False))
        except (ValidationError,ValueError,TypeError,VersioningError) as exc:
            fail('MALFORMED_INPUT',str(exc))
        # All refusals are checked before changing this model's state.
        if port=='freeze':
            if not self.authorized: fail('AUTHORITY')
            if self.fault=='unsafe_resume': fail('UNSAFE_RESUME')
            if self.fault=='provider_changed': fail('PROVIDER_CHANGED')
            if data != self.prepare(data.target.kind): fail('INPUT_MISMATCH')
        if port in ('diagnose','hypothesis'):
            train={c.case_id for c in self.request.benchmark.cases if c.split=='train'}
            if not set(data.train_case_ids)<=train: fail('CONTAMINATION')
        if port=='candidate': content=self._check_edit(data)
        if port=='admissibility' and version_id_of(self.candidate_content)==self.fixture['reference_version']: fail('NO_ADMISSIBLE_EDIT')
        if port=='holdout_seal':
            if not self.holdout_allowed or data.selection!=self.selection_seal or self.holdout_consumed:
                fail('HOLDOUT_AUTHORITY')
        if port in ('adopt','restore') and self.request.target.kind=='generated':
            if self.cas != self.request.target.expected_current: fail('CONFLICT')
        if port=='decision':
            eligible=self.numeric['holdout']['status']=='eligible'
            if data.action in ('adopt','source_patch') and not eligible: fail('NOT_ELIGIBLE')
            if data.action=='adopt' and (self.request.target.kind!='generated' or not self.adoption_approved
                    or data.review_ref!='reviewed' or data.owner_decision_ref!='owner-approved'): fail('ADOPTION_AUTHORITY')
            if data.action=='source_patch' and self.request.target.kind!='builtin': fail('POLICY_REFUSED')
        if port=='stop':
            if self.stop_reason and data.reason!=self.stop_reason: fail('ORDER')
            if not self.stop_reason and data.reason not in {'owner_stop','contamination','no_admissible_edit','budget','round_limit','invalid','unknown'}: fail('ORDER')
        next_port = {'freeze':'train_reference','train_reference':'diagnose','diagnose':'hypothesis',
            'hypothesis':'candidate','candidate':'admissibility','admissibility':'train_candidate',
            'train_candidate':'train_compare','train_compare':'holdout_seal','holdout_seal':'holdout',
            'holdout':'compare','compare':'decision','adopt':'stop','restore':'stop','source_patch':'stop'}
        self.phase=next_port.get(port,'stop')
        if port=='freeze': self.request=data
        elif port=='train_reference': self._evaluation('train',('reference',))
        elif port=='hypothesis': self.pending_checkpoint='hypothesis'
        elif port=='candidate': self.proposal=data; self.candidate_content=content
        elif port=='admissibility':
            self.candidate=version_id_of(self.candidate_content)
        elif port=='train_candidate': self._evaluation('train',('candidate',))
        elif port=='train_compare':
            r=self._matrix('train')
            if r['status']!='train_complete' or Fraction(r['public_summary']['overall_difference']['exact'])<=0:
                self.phase='restore'; self.stop_reason='no_train_improvement'
            else:
                self.selection_seal=digest([self.request,self.candidate,
                    [out.receipt_catalog for req,out in self.results.values()],self.numeric['train']])
                self.pending_checkpoint='train_compare'
        elif port=='holdout':
            self.holdout_consumed=True; self._evaluation('holdout',('reference','candidate'))
        elif port=='compare': self._matrix('holdout')
        elif port=='decision':
            eligible=self.numeric['holdout']['status']=='eligible'
            if not eligible or data.action=='retain': self.phase='restore'; self.stop_reason='holdout_rejected' if not eligible else 'retained'
            elif data.action in ('adopt','source_patch'): self.phase='adoption_review'; self.adoption_action=data.action
            else:
                means=self.numeric['holdout']['case_means']['candidate'].values()
                self.stop_reason='target' if sum(Fraction(v['exact']) for v in means)/8>=95 else 'manual_candidate'
        elif port=='adoption_review': self.phase=self.adoption_action; self.pending_checkpoint='adoption_review'
        elif port=='restore':
            # Reference never changed. This is MODEL verified no-op restoration,
            # not a claim of native rollback; a foreign CAS refuses above.
            self.events.append({'restore_reference':'verified_unchanged','provenance':'simulation'})
            if not self.stop_reason: self.stop_reason='retained'
        elif port=='adopt':
            self.current=self.candidate; self.cas=digest(['adopted',self.candidate])[7:]
            self.current_history.append(self.current); self.stop_reason='adopted'
        elif port=='source_patch': self.stop_reason='source_patch'
        elif port=='stop': self.stop_reason=data.reason; self.stopped=True
        self.events.append({'port':port,'phase':self.phase,'current':self.current})
        return self.view()
