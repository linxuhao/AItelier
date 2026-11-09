"""Actual State owner, Coding Mode SDK workspace, delivery and normal push.

Everything here runs against the SHARED Coding Mode fixture
(`tests.integration.test_coding_workspace_integration`), and that fixture has two
environment preconditions this file inherits:

  * the tree is mounted at ``/src`` — the fixture hashes ``/src/<module>`` into the
    runtime identity it records as evidence, so a tree mounted anywhere else
    (e.g. ``-v ...:/w``) raises FileNotFoundError in session setup, not in a test;
  * ``CODING_WORKSPACE_FIXTURE`` names an owned, writable, disposable root.

The invocation that runs them, one worker:

    docker run --rm --network none --cpus 2 --memory 2g -u 1000:1000 \
        --pids-limit 512 --init -v <tree>:/src:ro -w /src \
        -e EXPECTED_SOURCE=/src -e CODING_WORKSPACE_FIXTURE=/tmp/cwf \
        -e CODING_WORKSPACE_RAW=/tmp/raw <image> \
        python3 -m pytest tests/integration/test_dsh_project_work.py -q

``test_real_host_executor_runs_child_tests`` needs more than that: an external
stdlib controller serving a disposable owned launcher socket
(``DSH_WORK_HOST_FIXTURE``) and a test image (``DSH_WORK_TEST_IMAGE``), because
the child bash/test execution runs as a real container. The row carries its own
``skipif`` on those two variables, so a default run reports it as skipped and
names them.
"""
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest

from tests.integration.test_coding_workspace_integration import env, project, git
from core.dsh_work import project_work, pipeline_source
from core.state_commands import execute
from core.state_service import StateService
from core.drivers import DriverRegistry

_CAPSULE = Path("/src/core/coding_workspace.py").is_file()
pytestmark = pytest.mark.skipif(
    not (_CAPSULE and os.environ.get("CODING_WORKSPACE_FIXTURE")),
    reason=("requires the Coding Mode fixture: the tree mounted at /src and "
            "CODING_WORKSPACE_FIXTURE=<owned disposable root> (see module "
            "docstring for the full invocation)"))


@pytest.fixture
def work(env, project):
    pid, source = project(dirty=True)
    dag = 'dag-' + pid
    registry = DriverRegistry(env['db'], 'dsh-work-fixture-pepper-' * 3)
    driver = 'dsh-fixture-' + uuid.uuid4().hex[:8]
    registry.register(driver, driver, actor='fixture')
    service = StateService(env['db'], actor='driver:' + driver, driver_id=driver,
                           project_read_trusted=True)
    service.store.create_project(dag, dag)
    registry.set_membership(dag, driver, 'member', 0, 'fixture', actor='fixture', project_exists=lambda _: True)
    service.bind_source(dag, str(source))
    execute(service, 'add_nodes', {'project_id':dag,'nodes':[{'key':'work','facet':'content','goal':'fixture change',
        'acceptance':[{'id':'changed','kind':'test','description':'edited file is delivered'}]}]}, allow_write=True)
    claim = execute(service, 'claim_node', {'project_id':dag,'node_key':'work','purpose':'implement','expected_revision':1,'request_key':'fixture-claim'}, allow_write=True)
    attempt = execute(service, 'start_external_attempt', {'project_id':dag,'node_key':'work','expected_revision':1,
        'harness':'fixture','external_id':'fixture','request_key':'fixture-start','claim_id':claim['claim_id'],'fence':claim['fence']}, allow_write=True)
    def call(action, arguments=None, session='native-parent', **changes):
        return project_work(env['db'], service, project_id=dag, driver_id=driver, session_id=session,
            attempt_id=attempt['attempt_id'], fence=attempt['owner_fence'], action=action,
            arguments=arguments, sf=env['sf'], **changes)
    return {'call':call,'source':source,'dag':dag,'service':service,'driver':driver,'attempt':attempt}


def test_parent_and_native_child_have_distinct_real_workspaces(work):
    call = work['call']
    parent, child = call('open'), call('open', session='native-child')
    assert parent['workspace'] != child['workspace']
    assert parent['run_id'] != child['run_id']
    assert parent['source'] == child['source'] == str(work['source'])
    call('edit', {'path':'hello.py','old_str':'helo','new_str':'hello'}, session='native-child')
    assert 'hello world' in call('read',{'path':'hello.py'},session='native-child')['content']
    assert 'helo world' in call('read',{'path':'hello.py'})['content']
    assert 'helo world' in (work['source']/'hello.py').read_text()
    call('deliver')
    receipt = call('deliver', {'commit_message':'fix child-owned hello'}, session='native-child')
    assert receipt['status'] == 'applied', receipt
    assert 'hello world' in (work['source']/'hello.py').read_text()
    assert (work['source']/'scratch.txt').read_text() == 'user untracked scratch\n'
    assert (work['source']/'notes.md').read_text() == 'notes\nstaged line\n'
    assert git(work['source'],'show','HEAD:hello.py').stdout == "print('hello world')\n"
    assert 'staged line' not in git(work['source'],'show','HEAD:notes.md').stdout


def test_scope_and_traversal_refused_before_source_mutation(work, env):
    call = work['call'];call('open')
    from core.coding_workspace import Unavailable
    for path in ('../escape.txt',str(work['source']/'hello.py'),'.git/config'):
        with pytest.raises(Unavailable,match='Path traversal|Repository metadata'):
            call('write',{'path':path,'content':'forbidden','create':True})
    assert 'helo world' in (work['source']/'hello.py').read_text()
    for driver, fence in [('other-driver',work['attempt']['owner_fence']), (work['driver'],work['attempt']['owner_fence']+1)]:
        with pytest.raises(Exception,match='own project attempt'):
            project_work(env['db'],work['service'],project_id=work['dag'],driver_id=driver,
                session_id='foreign',attempt_id=work['attempt']['attempt_id'],fence=fence,action='open',sf=env['sf'])
    call('deliver')


def test_parallel_children_commit_disjoint_changes_without_folding_source_edits(work):
    call=work['call']
    a=call('open',session='child-a');b=call('open',session='child-b')
    assert a['base_sha']==b['base_sha']
    for session,path in [('child-a','a.txt'),('child-b','b.txt')]:
        call('write',{'path':path,'content':session,'create':True},session=session)
    for session in ('child-a','child-b'):
        receipt=call('deliver',{'commit_message':'deliver '+session},session=session)
        assert receipt['status']=='applied',receipt
        assert git(work['source'],'log','-1','--format=%s').stdout.strip()=='deliver '+session,receipt
    assert git(work['source'],'show','HEAD:a.txt').stdout=='child-a'
    assert git(work['source'],'show','HEAD:b.txt').stdout=='child-b'
    assert 'staged line' not in git(work['source'],'show','HEAD:notes.md').stdout


def test_delivered_commit_pushes_to_configured_local_origin(work, tmp_path):
    source = work['source'];remote = tmp_path/'origin.git'
    git(remote.parent,'init','--bare',str(remote))
    git(source,'remote','add','origin',str(remote))
    call=work['call'];call('open',session='native-child')
    call('write',{'path':'child.txt','content':'native child delivery\n','create':True},session='native-child')
    assert call('deliver',{'commit_message':'deliver child'},session='native-child')['status']=='applied'
    head=git(source,'rev-parse','HEAD').stdout.strip()
    with pytest.raises(Exception,match='expected_head'):call('push',{'expected_head':'0'*40},session='native-child')
    result=call('push',{'expected_head':head},session='native-child')
    assert result['status']=='pushed' and result['commit']==head
    assert git(remote,'show','refs/heads/main:child.txt').stdout=='native child delivery\n'
    assert pipeline_source(work['service'],work['dag'])==str(source)
    assert not work['service'].db.get_project(work['dag'])  # State id is not a run id.


@pytest.mark.skipif(not (os.environ.get('DSH_WORK_HOST_FIXTURE') and os.environ.get('DSH_WORK_TEST_IMAGE')), reason=(
    'needs DSH_WORK_HOST_FIXTURE=<disposable owned launcher root with launcher.sock> and '
    'DSH_WORK_TEST_IMAGE=<test image> (plus the Coding Mode fixture above)'))
def test_real_host_executor_runs_child_tests(work, env):
    # The external stdlib controller starts only our disposable launcher. No
    # Docker socket, production data, secrets or host pytest enter this fixture.
    fixture = os.environ.get('DSH_WORK_HOST_FIXTURE')
    if not fixture:
        pytest.fail('DSH_WORK_HOST_FIXTURE is required for the actual owned executor test')
    root=Path(fixture);entry=Path('/src/core/repository_executor_entry.py')
    import hashlib
    cfg={'allowed_roots':[str(root)],'aitelier_home':str(env['home']),
         'state_db':env['db'].db_path,'coding_shell_engine_db':str(env['home']/'skillflow.db'),
         'executor_entry':str(root/'executor.py'),'coding_shell_entry_sha256':hashlib.sha256(entry.read_bytes()).hexdigest(),
         'test_image':os.environ['DSH_WORK_TEST_IMAGE'],'allowed_uids':[1000]}
    (root/'config.json').write_text(json.dumps(cfg));(root/'ready').touch()
    import time
    deadline=time.monotonic()+30
    while not (root/'launcher.sock').exists():
        assert time.monotonic()<deadline,'disposable host launcher missing'
        time.sleep(.05)
    os.environ['AITELIER_HOST_LAUNCHER_SOCKET']=str(root/'launcher.sock')
    call=work['call'];call('open',session='native-test-child')
    call('write',{'path':'test_child.py','create':True,'content':
        "from pathlib import Path\ndef test_owned_child():\n assert Path('hello.py').read_text().startswith('print')\n assert not Path('/run/aitelier-secrets').exists()\n assert not Path('/run/docker.sock').exists()\n assert Path('/sys/fs/cgroup/memory.max').read_text().strip() == '2147483648'\n"},session='native-test-child')
    result=call('bash',{'command':'python3 -m pytest -q test_child.py','timeout':60},session='native-test-child')
    assert result['exit_code']==0 and '1 passed' in result['stdout'],result
    assert call('deliver',{'commit_message':'tested child delivery'},session='native-test-child')['status']=='applied'
    assert (work['source']/'test_child.py').exists()


def test_authenticated_work_endpoint_uses_actual_child_scope(work, env, monkeypatch):
    from types import SimpleNamespace
    from api import authz
    from api.dsh_driver_routers import manager
    from api.state_graph_routers import get_service
    from api.dependencies import get_skillflow
    from api.main import app
    identity=SimpleNamespace(driver_id=work['driver'],actor='driver:'+work['driver'],kind='driver',is_admin=False)
    monkeypatch.setattr(authz,'request_identity',lambda _:identity)
    runtime=SimpleNamespace(db=env['db'],running=lambda pid:pid==work['dag'],
        get=lambda _: {'driver_id':work['driver'],'enabled':True})
    previous=dict(app.dependency_overrides)
    app.dependency_overrides.update({manager:lambda:runtime,get_service:lambda:work['service'],get_skillflow:lambda:env['sf']})
    try:
        body={'session_id':'native-http-child','attempt_id':work['attempt']['attempt_id'],
              'fence':work['attempt']['owner_fence'],'action':'open'}
        url='/api/dsh-drivers/'+work['dag']+'/work'
        r=env['client'].post(url,json=body)
        assert r.status_code==200 and r.json()['session_id']=='native-http-child',r.text
        body.update(action='write',arguments={'path':'http-child.txt','content':'real child API','create':True})
        assert env['client'].post(url,json=body).json()['status']=='written'
        assert not (work['source']/'http-child.txt').exists()
        stale={**body,'fence':body['fence']+1}
        assert 'current owner fence' in env['client'].post(url,json=stale).json()['error']
        body.update(action='deliver',arguments={'commit_message':'authenticated child API'})
        result=env['client'].post(url,json=body).json()
        assert result['status']=='applied' and len(result['source_head'])==40,result
        assert (work['source']/'http-child.txt').read_text()=='real child API'
        identity.driver_id='someone-else'
        assert env['client'].post(url,json=body).status_code==403
    finally:
        app.dependency_overrides.clear();app.dependency_overrides.update(previous)


def test_mcp_pipeline_accepts_bound_state_id_instead_of_run_id(work, env, monkeypatch):
    from types import SimpleNamespace
    import core.run_launcher as launcher
    from api import authz
    from tests.unit.test_mcp_router import _rpc, _initialized
    identity=SimpleNamespace(driver_id=work['driver'],actor='driver:'+work['driver'],kind='driver',is_admin=False)
    monkeypatch.setattr(authz,'request_identity',lambda _:identity)
    observed=[]
    def capture(*a,**kw):
        observed.append(kw)
        return {'status':'error','message':'fixture stopped before pipeline execution'}
    monkeypatch.setattr(launcher,'start_config_run',capture)
    _initialized(env['client'])
    r=_rpc(env['client'],'tools/call',{'name':'run_pipeline','arguments':{
        'config':'investigate','against_project':work['dag'],'seed_text':'fixture','checkpoints':'ask'}},rpc_id=412)
    result=json.loads(r.json()['result']['content'][0]['text'])
    assert result=={'error':'fixture stopped before pipeline execution'},result
    assert len(observed)==1 and observed[0]['repo_path']==str(work['source'])
    assert observed[0]['repo_type']=='existing'
