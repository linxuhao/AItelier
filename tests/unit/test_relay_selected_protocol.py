"""Selected protocol + real State relay/runner/SDK; only the gateway is controlled."""
import asyncio
from copy import deepcopy
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.output_targets import git

from aitelier.runner import AgentStepRunner
from core import run_isolation as ri
from core.agents import AgentFactory
from core.db_manager import DBManager
from core.state_attempts import StateAttempts
from core.state_graph import StateGraphStore
from core.state_service import StateService, state_seed_text
from core.workspace_manager import WorkspaceManager


RETAINED = 'VALUE = 42\n'
INSTRUCTION = 'Finish retained helper wiring and add the targeted assertion.'


@pytest.fixture(autouse=True)
def bounded_fixture_storage(tmp_path):
    """Each real DB/worktree is disposable; do not accumulate a suite in tmpfs."""
    yield
    shutil.rmtree(tmp_path)


class OfflineGateway:
    litellm_model = 'offline/controlled'
    last_usage = {}

    def __init__(self, actions, native_failure=False):
        self.actions = iter(actions)
        self.native_failure = native_failure
        self.requests = []
        self.responses = []

    def generate(self, **kwargs):
        self.requests.append(deepcopy({'mode': 'json', **kwargs}))
        action = next(self.actions)
        value = action if isinstance(action, str) else json.dumps(action if isinstance(action, dict) else {'actions': action})
        self.responses.append(value)
        return value

    def generate_native(self, **kwargs):
        self.requests.append(deepcopy({'mode': 'native', **kwargs}))
        if self.native_failure:
            raise ValueError('controlled pre-effect native protocol failure')
        action = next(self.actions)
        calls = [{'id': 'call-'+str(len(self.requests))+'-'+str(i), 'type': 'function',
                  'function': {'name': a['tool'], 'arguments': json.dumps(a.get('params', {}))}}
                 for i, a in enumerate(action)]
        self.responses.append(calls)
        return SimpleNamespace(text='', reasoning_content='', truncated=False, tool_calls=calls)


def action(name, **params):
    return {'tool': name, 'params': params}


def relay_world(tmp_path, monkeypatch, native, is_relay=True, action_only=False):
    monkeypatch.setenv('AITELIER_HOME', str(tmp_path/'home'))
    from api import dependencies
    dependencies._tool_loader_instance = None
    loader = dependencies.get_tool_loader()  # actual host loader + installed SDK catalog
    db = DBManager(str(tmp_path/'state.db'))
    store = StateGraphStore(db, project_read_trusted=True)
    store.create_project('fixture', 'Owned synthetic relay')
    store.add_nodes('fixture', [{'key':'work','goal':'Finish the helper','dependencies':[],
                                'acceptance':[{'id':'useful','kind':'test','description':'Useful read'}]}])
    attempts = StateAttempts(store)
    src=tmp_path/'source';src.mkdir();git(src,'init','-q')
    (src/'seed.txt').write_text('owned base\n');git(src,'add','--','seed.txt');git(src,'commit','-qm','owned base')
    sf=SkillFlow(str(tmp_path/'sf.db'),tool_loader=loader,
                 workspace_base=str(tmp_path/'artifacts'),projects_base=str(tmp_path/'projects'),
                 code_path_resolver=lambda pid,run_id=None:ri.resolve_for_resolver(db,run_id) if run_id else src)
    sf.agent_registry.register('relay_role', model='offline/controlled', tools=['read','create','edit','finish_step'] if action_only else [], native_tool_calling=bool(native),
                               fallback_to_json_mode=native == 'fallback', max_tool_turns=8, system_prompt='Follow the retained-work contract.')
    node=StepNode(id='implementation',agent_config='relay_role',output_mode='write',output_target='code',
                  output_allow_full_write=not action_only,context=[{'source':{'config':'relay_fixture','output':'seed.md','required':True}},
                                                       {'from':'repository','mode':'tool'}],transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name='relay_fixture',begin=node.id,steps=[node]))
    ws=WorkspaceManager(str(tmp_path/'artifacts'))
    monkeypatch.setattr(dependencies,'get_skillflow',lambda:sf)
    monkeypatch.setattr(dependencies,'get_db_manager',lambda:db)
    def begin(attempt):
        pid=attempt['execution_project_id'];db.ensure_project(pid,name=pid,repo_type='existing',repo_path=str(src))
        ws.setup_workspace(pid,repo_type='existing',repo_path=str(src))
        from core.seed_publication import publish_seeds, seed_dir
        publish_seeds(seed_dir(sf,pid,'relay_fixture'), {'seed.md':state_seed_text(
            attempt['context'],[],bool(attempt['context'].get('relay')))})
        rid=sf.create_run('relay_fixture',context={'project_id':pid},project_id=pid);sf.start_run(rid);attempts.bind_run(attempt['attempt_id'],rid,sf)
        rec=ri.ensure_for_run(db,run_id=rid,project_id=pid,config_name='relay_fixture',repo_mode='code')
        sf.advance_run(rid);claim=sf.claim_next_step(rid)
        return rid,claim,Path(rec['worktree_path'])
    prior=attempts.reserve('fixture','work',1,'relay_fixture','first',INSTRUCTION)
    rid,claim,root=begin(prior)
    schemas=claim.inputs['_tool_schemas']
    if 'apply_patch' in schemas:
        name='apply_patch';params={'patch':'*** Begin Patch\n*** Add File: retained.py\n+VALUE = 42\n*** End Patch'}
    else:
        name='create';params={'file':'retained.py','content':RETAINED}
    result=sf.execute_tool(name,params,run_id=rid,step_id=claim.step_id,
                           step_instance_id=claim.token.step_instance_id,claim_epoch=claim.token.claim_epoch,
                           project_root=str(root))
    assert (root/'retained.py').read_text()==RETAINED, result
    if not is_relay:
        return sf,db,ws,rid,claim,root,prior
    sf.fail_run(rid,'native turn budget exhausted (8/8)')
    prior=attempts.reconcile(prior['attempt_id'],sf)
    service=StateService(db,ws,sf,{},project_read_trusted=True)
    inv=service.get_attempt(prior['attempt_id'])['relay_inventory']
    assert inv['code_changes']['files']['retained.py']['bytes']==len(RETAINED.encode())
    successor=attempts.reserve('fixture','work',1,'relay_fixture','continue',INSTRUCTION,
                               continue_from=prior['attempt_id'],relay_digest=inv['digest'])
    relay=service._prepare_relay(successor,str(src));successor=attempts.pin_relay(successor['attempt_id'],relay)
    rid,claim,root=begin(successor)
    assert (root/'retained.py').read_text()==RETAINED
    assert not claim.inputs.get('_legacy_code_staging'), 'only supported current code protocol is eligible'
    return sf,db,ws,rid,claim,root,successor


def run_selected(tmp_path,monkeypatch,native,actions,is_relay=True,action_only=False):
    sf,db,ws,rid,claim,root,successor=relay_world(tmp_path,monkeypatch,native,is_relay=is_relay,action_only=action_only)
    gateway=OfflineGateway(actions, native_failure=native == 'fallback')
    monkeypatch.setattr(AgentFactory,'_build_gateway',lambda self,name:gateway)
    error=None
    try:
        value=asyncio.run(AgentStepRunner(db,ws).execute(claim))
        sf.confirm_step(claim.token,value)
    except BaseException as exc:
        error=exc
    with sf.trace_connection(successor['execution_project_id']) as conn:
        rows=[{'category':r[0],'event':r[1],'payload':json.loads(r[2] or '{}')} for r in conn.execute(
            'SELECT category,event,payload_json FROM skillflow_trace WHERE run_id=? ORDER BY seq',(rid,))]
    evidence={'run_id':rid,'native_config':native,'claim_catalog':sorted(claim.inputs['_tool_schemas']),
              'legacy_code_staging':claim.inputs.get('_legacy_code_staging'),'relay':successor['context'].get('relay'),
              'requests':gateway.requests,'responses':gateway.responses,'trace':rows,
              'error':repr(error),'retained_bytes':(root/'retained.py').read_text(),
              'forbidden_path_exists':(root/'forbidden.py').exists()}
    dest=Path(os.environ.get('RELAY_PROTOCOL_EVIDENCE',str(tmp_path/'evidence')));dest.mkdir(parents=True,exist_ok=True)
    (dest/(tmp_path.name+'-'+rid+'.json')).write_text(json.dumps(evidence,indent=2,default=str)+'\n')
    if error:raise error
    return evidence


@pytest.mark.parametrize('native',[False,True,'fallback'],ids=['json','native','fallback'])
def test_selected_relay_ack_reaches_actual_nonempty_read(tmp_path,monkeypatch,native):
    evidence=run_selected(tmp_path,monkeypatch,native,[[action('acknowledge_relay',retained_bytes=len(RETAINED),
                 incomplete_items=['finish retained helper wiring','add targeted assertion'])],
                 [action('read',path='retained.py')],[action('finish_step',summary='Verified retained helper')]])
    assert {r['mode'] for r in evidence['requests']}==({'native','json'} if native == 'fallback' else {'native'} if native else {'json'})
    assert any(r['event']=='relay_progress_acknowledged' for r in evidence['trace'])
    read_results=[r for r in evidence['trace'] if r['category']=='tool_result' and r['event']=='read']
    assert read_results, evidence
    assert RETAINED.strip() in json.dumps(evidence['requests']), 'real read must reach the controlled provider'


FAITHFUL = action('acknowledge_relay', retained_bytes=len(RETAINED),
                  incomplete_items=['finish retained helper wiring','add targeted assertion'])


@pytest.mark.parametrize('native',[False,True,'fallback'],ids=['json','native','fallback'])
@pytest.mark.parametrize('bad', ['bytes','missing','unrelated','read','write','read_then_ack'])
def test_relay_refuses_before_faithful_ack(tmp_path,monkeypatch,native,bad):
    first = {
        'bytes':[action('acknowledge_relay',retained_bytes=999,incomplete_items=FAITHFUL['params']['incomplete_items'])],
        'missing':[action('acknowledge_relay',retained_bytes=len(RETAINED),incomplete_items=[])],
        'unrelated':[action('acknowledge_relay',retained_bytes=len(RETAINED),incomplete_items=['decorate a lamp'])],
        'read':[action('read',path='retained.py')],
        'write':[action('create',file='forbidden.py',content='must not land')],
        'read_then_ack':[action('read',path='retained.py'),FAITHFUL],
    }[bad]
    evidence=run_selected(tmp_path,monkeypatch,native,[first,[FAITHFUL],
                          [action('read',path='retained.py')],[action('finish_step',summary='done')]])
    assert not evidence['forbidden_path_exists']
    reads=[i for i,r in enumerate(evidence['trace']) if r['event']=='read' and r['category']=='tool_call']
    acks=[i for i,r in enumerate(evidence['trace']) if r['event']=='relay_progress_acknowledged']
    assert len(reads)==1 and acks and acks[0]<reads[0]
    assert 'acknowledg' in json.dumps(evidence['requests']).lower()


@pytest.mark.parametrize('native',[False,'fallback'],ids=['json','fallback'])
def test_json_leading_ack_and_read_same_batch(tmp_path,monkeypatch,native):
    evidence=run_selected(tmp_path,monkeypatch,native,[[FAITHFUL,action('read',path='retained.py')],
                         [action('finish_step',summary='done')]])
    assert any(r['event']=='relay_progress_acknowledged' for r in evidence['trace'])
    assert RETAINED.strip() in json.dumps(evidence['requests'])
    assert not any(r['event']=='acknowledge_relay' and r['category']=='tool_call' for r in evidence['trace']), 'host control must not be delegated to SDK allowlist'


def test_json_files_cannot_bypass_first_ack(tmp_path,monkeypatch):
    evidence=run_selected(tmp_path,monkeypatch,False,[{'files':{'forbidden.py':'must not land'}},
                         [FAITHFUL],[action('read',path='retained.py')],[action('finish_step',summary='done')]])
    assert not evidence['forbidden_path_exists']
    assert any(r['event']=='relay_operation_refused_before_ack' for r in evidence['trace'])


@pytest.mark.parametrize('native',[False,True],ids=['json','native'])
def test_ordinary_nonrelay_has_no_relay_tool_and_reads_normally(tmp_path,monkeypatch,native):
    evidence=run_selected(tmp_path,monkeypatch,native,[[action('read',path='retained.py')],
                         [action('finish_step',summary='ordinary done')]],is_relay=False)
    assert evidence['relay'] is None
    assert not any(r['event']=='relay_progress_acknowledged' for r in evidence['trace'])
    if native:
        assert all(t['function']['name']!='acknowledge_relay' for t in evidence['requests'][0]['tools'])
    else:
        assert 'acknowledge_relay' not in evidence['requests'][0]['user_prompt']
    assert RETAINED.strip() in json.dumps(evidence['requests'])


def test_generic_empty_json_remains_refused_not_repaired(tmp_path,monkeypatch):
    from core.dpe_pipeline import MaxRetriesExceeded
    with pytest.raises(MaxRetriesExceeded,match='exhausted 8 tool exploration turns'):
        run_selected(tmp_path,monkeypatch,False,['']*8)


@pytest.mark.parametrize('native',[False,'fallback'],ids=['json','fallback'])
def test_action_only_json_selected_route_keeps_real_sdk_grants(tmp_path,monkeypatch,native):
    evidence=run_selected(tmp_path,monkeypatch,native,[[action('read',path='retained.py')],
                         [FAITHFUL,action('read',path='retained.py')],
                         [action('finish_step',summary='done')]],action_only=True)
    assert 'write' not in evidence['claim_catalog']
    assert 'acknowledge_relay' not in evidence['claim_catalog']
    reads=[i for i,r in enumerate(evidence['trace']) if r['event']=='read' and r['category']=='tool_call']
    acks=[i for i,r in enumerate(evidence['trace']) if r['event']=='relay_progress_acknowledged']
    assert len(reads)==1 and len(acks)==1 and acks[0]<reads[0]
    assert RETAINED.strip() in json.dumps(evidence['requests'])
