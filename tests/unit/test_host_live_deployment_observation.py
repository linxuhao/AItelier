"""Normal initialized backend facts + operator host observation, owned CPU only."""
import ast,json,os,subprocess,sys,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core import deployment_quiescence as dq
from core.db_manager import DBManager
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph,StepNode
from api import admin_routers,authz,dependencies
from cli import server


@pytest.fixture
def live(tmp_path,monkeypatch):
    db=DBManager(str(tmp_path/'live.db'));sf=SkillFlow(str(tmp_path/'live-sf.db'))
    sf.register_graph(PipelineGraph(name='live',begin='work',steps=[StepNode(id='work')]))
    rid=sf.create_run('live',project_id='owned');sf.start_run(rid)
    monkeypatch.setattr(dependencies,'_skillflow_instance',sf);monkeypatch.setattr(dependencies,'db_instance',db)
    # Getter sentinels: requesting a new runtime/manager is never necessary.
    def forbidden():raise AssertionError('second composition root getter invoked')
    monkeypatch.setattr(dependencies,'get_skillflow',forbidden);monkeypatch.setattr(dependencies,'get_db_manager',forbidden)
    monkeypatch.setattr(authz,'ADMIN_TOKEN','owned-admin');monkeypatch.setenv('AITELIER_ADMIN_TOKEN','owned-admin')
    app=FastAPI();app.include_router(admin_routers.router);client=TestClient(app)
    yield sf,db,rid,client
    sf._conn.close()


def fetch(live):
    response=live[3].post('/api/admin/deployment-runtime-observation',headers={'X-AItelier-Admin-Token':'owned-admin'})
    assert response.status_code==200,response.text
    return response.json()


def test_normal_route_reuses_actual_initialized_runtime_and_preserves_audit(live):
    sf,db,rid,client=live;before=sf.get_run(rid);facts=fetch(live)
    assert facts['runs'][0]['run_id']==rid and facts['runs'][0]['resumable'] is True
    assert facts['digest']==dq._observation_digest(facts)
    assert facts['runtime_identity']['pid_namespace']==os.readlink('/proc/self/ns/pid')
    assert sf.get_run(rid)==before
    assert dependencies._skillflow_instance is sf and dependencies.db_instance is db


@pytest.mark.parametrize('headers',[{}, {'X-AItelier-Admin-Token':'bad'}, {'Cf-Ray':'through-tunnel','X-AItelier-Admin-Token':'owned-admin'}, {'Cf-Access-Jwt-Assertion':'invalid','X-AItelier-Admin-Token':'owned-admin'}])
def test_normal_original_facts_refuse_untrusted_or_tunnel_identity(live,headers):
    response=live[3].post('/api/admin/deployment-runtime-observation',headers=headers)
    assert response.status_code==403
    assert 'runs' not in response.json()


def test_uninitialized_live_facts_explicit_unavailable_no_getter(live,monkeypatch):
    monkeypatch.setattr(dependencies,'_skillflow_instance',None)
    response=live[3].post('/api/admin/deployment-runtime-observation',headers={'X-AItelier-Admin-Token':'owned-admin'})
    assert response.status_code==503


def host_runner(command):
    # Controlled host command facts; actual runtime audits/authorization/normal wiring are unchanged.
    return SimpleNamespace(returncode=0,stdout='',stderr='')


def test_composed_measure_retains_runtime_blockers_and_host_errors(live,tmp_path):
    sf,db,rid,client=live;facts=fetch(live)
    observation=dq.measure(runtime_facts=facts,command_runner=host_runner)
    assert observation['resumable_runs']==[{'run_id':rid,'project_id':'owned','status':'running'}]
    assert observation['provenance']['runtime_digest']==facts['digest']
    assert observation['provenance']['domain']=='live-runtime-and-operator-host'
    assert dq._validate_observation(observation) is None
    # Actual owned Linux domain lacks Docker/ps: these errors survive, never become quiet.
    missing=dq.measure(runtime_facts=facts)
    assert any('docker inventory failed' in error for error in missing['errors'])
    assert missing['quiescent'] is False
    with pytest.raises(dq.DeploymentBlocked):dq.authorize('restart',missing,journal=tmp_path/'journal.json')
    # Genuine in-process SDK operation preserves its blocking audit through composition.
    sf.advance_run(rid);claim=sf.claim_next_step(rid);assert claim is not None
    # Claim alone may not admit an operation, so use the normal SDK admission surface.
    operation=sf._admit_op('tool_step',rid,detail='owned inverse control')
    try:
        blocked=dq.measure(runtime_facts=fetch(live),command_runner=host_runner)
        assert blocked['quiescent'] is False and blocked['blockers']['active_operations']
        with pytest.raises(dq.DeploymentBlocked):dq.authorize('restart',blocked,journal=tmp_path/'owned-blocked.json')
    finally:
        sf._retire_op(operation)



@pytest.mark.parametrize('shape',['projection','digest','stale','future','boot','malformed','audit'])
def test_original_runtime_snapshot_integrity_freshness_and_domain_refuse(live,shape):
    facts=fetch(live)
    if shape=='projection':facts={'status':'observed','original_observation_digest':facts['digest'],'observation':facts}
    elif shape=='digest':facts['runs']=[]
    elif shape=='stale':facts['observed_at']='2000-01-01T00:00:00+00:00';facts['digest']=dq._observation_digest(facts)
    elif shape=='future':facts['observed_at']='2100-01-01T00:00:00+00:00';facts['digest']=dq._observation_digest(facts)
    elif shape=='boot':facts['runtime_identity']['boot_id']='foreign-host';facts['digest']=dq._observation_digest(facts)
    elif shape=='malformed':facts['registered_external_owners']='not-an-inventory';facts['digest']=dq._observation_digest(facts)
    elif shape=='audit':facts['runs'][0]['active_operations']=9;facts['digest']=dq._observation_digest(facts)
    with pytest.raises((ValueError,TypeError)):dq.measure(runtime_facts=facts,command_runner=lambda _:pytest.fail('malformed facts reached host probe'))


def wire_client(live):
    class Client:
        def __init__(self,*args,**kwargs):self.headers=kwargs['headers']
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,path):return live[3].post(path,headers=self.headers)
    return Client


def bind_host_backend(live,monkeypatch,mismatch=False):
    monkeypatch.setattr(server.httpx,'Client',wire_client(live))
    monkeypatch.setattr(server,'_compose',lambda *args:SimpleNamespace(returncode=0,stdout='a'*64+'\n',stderr=''))
    monkeypatch.setattr(server.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout=str(os.getpid())+'\n',stderr=''))
    if mismatch:
        real=os.readlink
        monkeypatch.setattr(server.os,'readlink',lambda path:'pid:[99999999]' if path.startswith('/proc/') and '/ns/pid' in path and '/self/' not in path else real(path))


def test_normal_cli_fetches_raw_actual_route_and_matches_backend_namespace(live,monkeypatch):
    bind_host_backend(live,monkeypatch)
    facts=server._live_runtime_observation('http://127.0.0.1:4444')
    assert facts['runs'][0]['run_id']==live[2]
    assert 'transport_projection' not in facts


def test_normal_cli_mismatched_backend_namespace_refuses(live,monkeypatch):
    bind_host_backend(live,monkeypatch,mismatch=True)
    with pytest.raises(ValueError,match='operated backend'):server._live_runtime_observation('http://127.0.0.1:4444')


@pytest.mark.parametrize('url',['https://127.0.0.1:4444','http://foreign.example:4444'])
def test_normal_cli_never_sends_admin_to_remote_domain(live,monkeypatch,url):
    monkeypatch.setattr(server.httpx,'Client',lambda *a,**k:pytest.fail('credential-bearing HTTP client created'))
    with pytest.raises(ValueError,match='local backend'):server._live_runtime_observation(url)


def test_cli_guard_no_runtime_import_and_existing_fence_journal_path(live,monkeypatch,tmp_path):
    facts=fetch(live);monkeypatch.setenv('AITELIER_HOME',str(tmp_path/'home'))
    monkeypatch.setattr(server,'_live_runtime_observation',lambda base_url:facts)
    real=dq.measure
    monkeypatch.setattr(dq,'measure',lambda **kw:real(**kw,command_runner=host_runner))
    clearance=server._require_deployment_clearance('restart',base_url='http://127.0.0.1:4444')
    assert clearance['event']['inventory_digest']
    assert clearance['event']['resumable_runs'][0]['run_id']==live[2]
    completed=server._finish_deployment(clearance,success=True)
    assert completed['event']['status']=='completed'


def test_source_import_identity_and_cgroup():
    root=Path(os.environ['EXPECTED_SOURCE'])
    for module in [dq,server,admin_routers]:assert Path(module.__file__).resolve().is_relative_to(root)
    assert Path('/sys/fs/cgroup/memory.max').read_text().strip()=='2147483648'
    assert Path('/sys/fs/cgroup/pids.max').read_text().strip()=='512'


def test_original_accepted_cli_guard_calls_composition_getter(live,monkeypatch,tmp_path):
    # Execute exact accepted preimage guard body in the same owned fixture domain.
    raw=subprocess.check_output(['git','show','caa3bf161f064f1ed32cea99d2fec8e74a07dd67:cli/server.py'],cwd=Path(os.environ['EXPECTED_SOURCE'])).decode()
    tree=ast.parse(raw);node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_require_deployment_clearance')
    namespace={'os':os};exec(compile(ast.Module(body=[node],type_ignores=[]),'accepted-cli-preimage','exec'),namespace)
    calls=[]
    def forbidden():
        calls.append('composition-getter');raise AssertionError('would initialize second runtime')
    monkeypatch.setattr(dependencies,'get_skillflow',forbidden);monkeypatch.setenv('AITELIER_HOME',str(tmp_path/'old-home'))
    with pytest.raises(RuntimeError,match='measurement could not start'):namespace['_require_deployment_clearance']('restart')
    assert calls==['composition-getter']
