"""Real runner/SDK/Git/gateway; only litellm's paid completion is scripted.

Baseline failures assert behavior, never import a proposed production class.
The small graph keeps the default code-only producer/item/repair boundaries.
"""
import asyncio
import copy
import importlib.metadata
import json
import subprocess
from pathlib import Path

import litellm
import pytest
import skillflow
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, LoopConfig, Transition
from skillflow.tool_loader import ToolLoader

from aitelier.runner import AgentStepRunner
from core import run_isolation
from core.db_manager import DBManager
from core.dpe_pipeline import MaxRetriesExceeded
from core.workspace_manager import WorkspaceManager


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def action(name, **params):
    return {'tool': name, 'params': params}


def native(*actions):
    return [{'id': 'call-' + str(i), 'type': 'function', 'function': {
        'name': a['tool'], 'arguments': json.dumps(a['params'])}}
        for i, a in enumerate(actions)]


class Case:
    def __init__(self, tmp, monkeypatch, mode):
        assert importlib.metadata.version('skillflow-py') == '1.5.97'
        monkeypatch.setenv('AITELIER_HOME', str(tmp/'home'))
        monkeypatch.setenv('AITELIER_ZVEC_LIFECYCLE', '0')
        monkeypatch.setenv('AITELIER_LLM_STREAM', '0')
        monkeypatch.delenv('AITELIER_FORCE_ALL_AGENT_MODELS', raising=False)
        self.mode = mode
        self.raw_card = 'card_a'
        self.canonical = tmp/'canonical'; self.canonical.mkdir()
        git(self.canonical, 'init', '-q')
        git(self.canonical, 'config', 'user.name', 'Offline fixture')
        git(self.canonical, 'config', 'user.email', 'fixture@example.invalid')
        (self.canonical/'seed.py').write_text('SEED = True\n')
        git(self.canonical, 'add', '--', 'seed.py'); git(self.canonical, 'commit', '-qm', 'base')
        self.db = DBManager(str(tmp/'host.db'))
        self.db.ensure_project('p', name='refusal', repo_type='existing', repo_path=str(self.canonical))
        self.ws = WorkspaceManager(str(tmp/'artifacts'), projects_base=str(tmp/'projects'))
        self.sf = SkillFlow(str(tmp/'sdk.db'), workspace_base=str(tmp/'artifacts'),
            tool_loader=ToolLoader(Path(skillflow.__file__).parent/'tools'), artifact_history=False,
            code_path_resolver=lambda pid, run_id=None: run_isolation.resolve_for_resolver(self.db, run_id))
        self.sf.register_agent_config('producer', model='deepseek/deepseek-v4-flash',
            system_prompt='Correct the card paths; retain errors; finish when done.',
            native_tool_calling=(mode == 'native'), fallback_to_json_mode=False,
            max_tool_turns=8)
        producer = StepNode(id='t_impl', agent_config='producer',
            output_mode='content' if mode == 'json-content' else 'write',
            output_target='code', output_allow_full_write=True,
            output_fixed={'file': {'file': '*.py', 'target': 'code'}} if mode == 'json-content' else {},
            context=[{'from': 'repository', 'mode': 'tool'}],
            transitions=[Transition(to='review')])
        graph = PipelineGraph(name='dpe_default_v2', begin='task_loop', steps=[
            StepNode(id='task_loop', step_type='loop',
                loop=LoopConfig(source={'step':'3','file':'manifest.json','field':'items'}, item_as='current_task'),
                transitions=[Transition(to='t_impl', max_loop=10), Transition(to='done')]),
            producer,
            StepNode(id='review', output_mode='content', output_target='artifact',
                output_fixed={'verdict': {'file':'review_verdict.json'}},
                transitions=[
                    Transition(to='task_loop', match={'from_file':'review_verdict.json','field':'passed','value':True}),
                    Transition(to='t_impl', match={'from_file':'review_verdict.json','field':'passed','value':False}, max_loop=3)]),
            StepNode(id='done', step_type='gate')])
        self.sf.register_graph(graph)
        d=self.sf._workspace.get_step_dir('p',graph.name,'3'); d.mkdir(parents=True)
        (d/'manifest.json').write_text(json.dumps({'items':[self.raw_card]}))
        self.run=self.sf.create_run(graph.name, {'project_id':'p'}, project_id='p')
        import api.dependencies as deps
        monkeypatch.setattr(deps, 'get_skillflow', lambda:self.sf)
        monkeypatch.setattr(deps, 'get_db_manager', lambda:self.db)
        run_isolation.ensure_for_run(self.db,run_id=self.run,project_id='p',config_name=graph.name,repo_mode='code')
        self.root=Path(run_isolation.resolve_for_resolver(self.db,self.run))
        assert git(self.root,'rev-parse','--show-toplevel') == str(self.root)
        self.sf.start_run(self.run)
        self.runner=AgentStepRunner(self.db,self.ws)
        self.requests=[]; self.responses=[]; self.script=[]; self.claims=[]; self.artifacts=[]
        def complete(**kwargs):
            self.requests.append(copy.deepcopy({k:kwargs.get(k) for k in ('messages','tools','model','response_format')}))
            assert self.script, 'unexpected extra paid generation'
            item=self.script.pop(0)
            if isinstance(item,BaseException): raise item
            if self.mode=='native':
                message={'role':'assistant','content':None,'tool_calls':item}
            else:
                message={'role':'assistant','content': item if isinstance(item,str) else json.dumps(item)}
            response=litellm.ModelResponse(model='offline',choices=[{'index':0,
                'finish_reason':'tool_calls' if self.mode=='native' else 'stop','message':message}],
                usage={'prompt_tokens':24,'completion_tokens':12,'total_tokens':36})
            self.responses.append(response.model_dump())
            return response
        monkeypatch.setattr(litellm,'completion',complete)

    def claim(self, expected='t_impl'):
        for _ in range(12):
            claim=self.sf.claim_next_step(self.run)
            if claim:
                assert claim.step_id==expected
                row=next(r for r in self.sf.get_steps(self.run) if r['id']==claim.token.step_instance_id)
                artifact=Path(claim.inputs['_artifact_dir'])
                assert row['loop_item']==self.raw_card, 'SDK must own this raw loop item'
                assert artifact.name==self.raw_card and artifact.parent.name==claim.step_id
                self.claims.append({'instance':claim.token.step_instance_id,'epoch':claim.token.claim_epoch,'step':claim.step_id,
                                    'loop_item':row['loop_item'],'artifact_dir':str(artifact)})
                self.artifacts.append(claim.inputs['_artifact_dir'])
                # Supply a real card in resolved input, like the manifest's context source.
                claim.inputs.setdefault('_resolved_context',{}).update({
                    '[current_task]':'card_a','Step 3 — tasks/card_a.json':json.dumps({
                        'id':'card_a','owns':['allowed.py','partial.py'],'shared_hotspots':[]})})
                return claim
            self.sf.advance_run(self.run)
        raise AssertionError('SDK did not release '+expected)

    def execute(self, claim, *script):
        self.script=list(script)
        return asyncio.run(self.runner.execute(claim))

    def review_again(self, claim):
        self.sf.confirm_step(claim.token,StepResult())
        review=self.claim('review')
        target=Path(review.inputs['_output_dir']); target.mkdir(parents=True,exist_ok=True)
        (target/'review_verdict.json').write_text('{"passed": false}')
        self.sf.confirm_step(review.token,StepResult(flags={'passed':False}))
        again=self.claim()
        assert again.token.step_instance_id!=claim.token.step_instance_id
        assert again.inputs['_artifact_dir']==claim.inputs['_artifact_dir']
        return again

    def denials(self):
        return [r for r in self.sf.get_trace(self.run,limit=10000) if r['event']=='write_scope_violation']

    def evidence(self):
        print('REFUSAL_EVIDENCE '+json.dumps({'mode':self.mode,'run':self.run,
            'claims':self.claims,'requests':self.requests,'responses':self.responses,
            'denials':self.denials(),
            'completion_events':[r for r in self.sf.get_trace(self.run) if r['event'] in ('completion_started','completion_finished','resumed_from_trace','native_batch_recovery')],
            'sdk_steps':self.sf.get_steps(self.run),
            'receipts':{str(q):json.loads(q.read_text()) for d in set(self.artifacts) for q in Path(d).glob('*.json') if q.name in ('code_changes.json','write_scope_receipt.json')},
            'git_head':git(self.root,'rev-parse','HEAD'),
            'git_status':git(self.root,'status','--porcelain')},default=str))


@pytest.fixture
def cases(tmp_path,monkeypatch):
    made=[]
    def create(mode):
        c=Case(tmp_path,monkeypatch,mode);made.append(c);return c
    yield create
    for c in made:c.evidence()


def denied(mode):
    a=(action('write_file',id='forbidden',content='FORBIDDEN = True\n') if mode=='json-content'
       else action('create',file='forbidden.py',content='FORBIDDEN = True\n'))
    return native(a) if mode=='native' else {'actions':[a]}


def finish(mode):
    a=action('finish_step',summary='done')
    return native(a) if mode=='native' else {'actions':[a]}


@pytest.mark.parametrize('mode',['native','json-tool','json-content'])
def test_cross_instance_repeat_stops_before_another_generation(cases,mode):
    c=cases(mode);first=c.claim()
    c.execute(first,denied(mode),finish(mode))
    assert len(c.denials())==1 and not (c.root/'forbidden.py').exists()
    second=c.review_again(first); before=len(c.requests)
    caught=None
    try:c.execute(second,denied(mode),finish(mode))
    except MaxRetriesExceeded as error:caught=error
    assert caught is not None, 'same denied operation after new producer feedback must raise bounded failure'
    assert 'scope' in str(caught).lower() or 'refus' in str(caught).lower()
    assert len(c.requests)-before==1, 'repeated denied response must not buy another generation'
    first_error=c.denials()[0]['payload']['error']
    assert any(first_error in str(m.get('content','')) for m in c.requests[before]['messages']), 'first raw actionable refusal must reach actual new-producer request'
    assert len(c.denials())>=2
    assert not (c.root/'forbidden.py').exists()


@pytest.mark.parametrize('mode',['native','json-tool','json-content'])
def test_first_mistaken_path_can_be_corrected(cases,mode):
    c=cases(mode);claim=c.claim()
    allowed=(action('write_file',id='allowed',content='ALLOWED = 42\n') if mode=='json-content'
             else action('create',file='allowed.py',content='ALLOWED = 42\n'))
    response=native(allowed) if mode=='native' else {'actions':[allowed]}
    c.execute(claim,denied(mode),response,finish(mode))
    assert len(c.denials())==1
    assert (c.root/'allowed.py').read_text()=='ALLOWED = 42\n'
    assert not (c.root/'forbidden.py').exists()
    assert 'forbidden.py' in json.dumps(c.requests[1]['messages'])
    c.sf.confirm_step(claim.token,StepResult())
    receipt=json.loads((Path(claim.inputs['_artifact_dir'])/'code_changes.json').read_text())
    assert receipt['files']==['allowed.py']


@pytest.mark.parametrize('mode',['native','json-tool'])
def test_same_batch_is_not_a_second_feedback_opportunity(cases,mode):
    c=cases(mode);claim=c.claim()
    a=action('create',file='forbidden.py',content='NO\n')
    good=action('create',file='allowed.py',content='OK\n')
    batch=native(a,a,good) if mode=='native' else {'actions':[a,a,good]}
    c.execute(claim,batch,finish(mode))
    assert len(c.denials())==2 and (c.root/'allowed.py').read_text()=='OK\n'
    assert len(c.requests)==(2 if mode=='native' else 1)
    assert not (c.root/'forbidden.py').exists()


def test_json_legacy_files_corrects_first_denial_and_retains_partial(cases):
    c=cases('json-content');claim=c.claim()
    first={'files':{'partial.py':'PARTIAL = True\n','forbidden.py':'NO\n'}}
    repair={'files':{'allowed.py':'ALLOWED = True\n'}}
    c.execute(claim,first,repair)
    assert len(c.requests)==2 and len(c.denials())==1
    assert (c.root/'partial.py').read_text()=='PARTIAL = True\n'
    assert (c.root/'allowed.py').read_text()=='ALLOWED = True\n'
    assert not (c.root/'forbidden.py').exists()
    assert 'forbidden.py' in json.dumps(c.requests[1]['messages'])
    c.sf.confirm_step(claim.token,StepResult())
    receipt=json.loads((Path(claim.inputs['_artifact_dir'])/'code_changes.json').read_text())
    assert set(receipt['files'])=={'partial.py','allowed.py'}


@pytest.mark.parametrize('mode',['native','json-tool'])
def test_reclaim_after_only_refusal_keeps_correction_possible(cases,mode):
    c=cases(mode);first=c.claim()
    with pytest.raises(KeyboardInterrupt,match='transport interrupted'):
        c.execute(first,denied(mode),KeyboardInterrupt('transport interrupted'))
    assert len(c.denials())==1 and not (c.root/'forbidden.py').exists()
    c.sf.release_claim(first.token,'fixture controller interrupted; no mutation admitted')
    reclaimed=c.claim()
    assert reclaimed.token.step_instance_id==first.token.step_instance_id
    assert reclaimed.token.claim_epoch>first.token.claim_epoch
    good=action('create',file='allowed.py',content='AFTER_RECLAIM = True\n')
    response=native(good) if mode=='native' else {'actions':[good]}
    c.execute(reclaimed,response,finish(mode))
    assert len(c.denials())==1, 'original refused response must not execute/count again on recovery'
    assert (c.root/'allowed.py').read_text()=='AFTER_RECLAIM = True\n'
    assert not (c.root/'forbidden.py').exists()
    if mode=='native':
        assert any(r['event']=='resumed_from_trace' for r in c.sf.get_trace(c.run))
    c.sf.confirm_step(reclaimed.token,StepResult())


def test_json_invalid_response_remains_ordinary_repair(cases):
    c=cases('json-tool');claim=c.claim()
    good={'actions':[action('create',file='allowed.py',content='VALID = True\n')]}
    c.execute(claim,'This is not JSON.',good,finish('json-tool'))
    assert (c.root/'allowed.py').read_text()=='VALID = True\n'
    assert not c.denials()
    assert len(c.requests)==2
