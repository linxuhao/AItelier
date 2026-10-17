"""Real native graph and application pytest sessions, inside the disposable CPU suite.

Only the outer Docker launch is substituted: the actual entry subprocess, host
identity attestation and socket response adaptation all run. No live launcher.
"""
from contextlib import contextmanager
import json
import os
import shutil
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import coding_impl_outcome as outcome, repository_executor as executor
from core import repository_host_launcher as launcher, run_isolation
from core.db_manager import DBManager
from core.meta_agent import MetaAgent
from core.run_driver import summarise_run
from tests.skillflow.test_coding_impl_gate import _wire
from tests.code_output_fixture import write_claim_code
from skillflow.core import StepResult


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def app(repo, green=False):
    (repo / 'pytest.ini').write_text('[pytest]\n')
    (repo / 'catalog.py').write_text('def total():\n    return '+('10' if green else '9')+'\n')
    (repo / 'feature.py').write_text('def tag(value):\n    return value.lower()\n')
    (repo / 'test_app.py').write_text('''import pytest
from catalog import total
from feature import tag

def test_total():
    print("captured measurement " * 1000)
    assert total() == 10

def test_tag():
    assert tag('A') == 'a'

@pytest.mark.skip(reason='optional integration remains unmeasured')
def test_optional():
    pass
''')
    git(repo, 'add', 'pytest.ini', 'catalog.py', 'feature.py', 'test_app.py')
    git(repo, 'commit', '-qm', 'ordinary application baseline')


class Rig:
    def __init__(self, tmp, monkeypatch, green=False, source_repo=None):
        import api.dependencies as deps
        tmp.mkdir(parents=True,exist_ok=True)
        self.tmp = tmp
        self.monkeypatch = monkeypatch
        self.sf, self.rid, _ = _wire(tmp, [False], real_run_tests=True,
                                  repo_prepare=lambda repo: app(repo, green))
        self.db = DBManager(str(tmp / 'host.db'))
        self.db.ensure_project('p', name='app', repo_type='existing', repo_path=str(source_repo or tmp / 'proj' / 'p'))
        monkeypatch.setenv('AITELIER_HOME', str(tmp / 'home'))
        rec = run_isolation.ensure_for_run(self.db, run_id=self.rid, project_id='p',
                                          config_name='coding_impl', repo_mode='code')
        self.repo = Path(rec['worktree_path'])
        monkeypatch.setattr(self.sf._workspace, '_code_path_resolver', lambda *a, **kw: str(self.repo))
        monkeypatch.setattr(deps, 'get_skillflow', lambda: self.sf)
        monkeypatch.setattr(deps, 'get_db_manager', lambda: self.db)
        self.registry = SimpleNamespace(get=lambda name: SimpleNamespace(output_step='test', scheduler_owned=True,
                                                                         registers_generated_pipeline=False, registers_generated_addon=False))
        monkeypatch.setattr(deps, 'get_config_registry', lambda: self.registry)
        self.ws = SimpleNamespace(get_final_path=lambda pid, step, config: self.sf._workspace.get_step_dir(pid, config, step))
        self.agent = MetaAgent(MagicMock(), self.ws, owner_email='test@local')
        self.calls = 0
        self.identity_change = None
        self.entry_path = tmp/'executor-entry.py'
        shutil.copyfile(Path(launcher.__file__).with_name('repository_executor_entry.py'), self.entry_path)
        self.drift_entry = False

        @contextmanager
        def outer_container(command, *a, **kw):
            # Docker isolation is already supplied by the enclosing 2 CPU test container.
            proc = subprocess.Popen([sys.executable, str(self.entry_path),
                                     command[-1]], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                yield proc
            finally:
                proc.wait(timeout=20)
                proc.stdout.close()
                proc.stderr.close()
                if self.drift_entry and self.calls > 1:
                    self.entry_path.write_text(self.entry_path.read_text()+'\n# changed during launch\n')
                    self.drift_entry=False
        monkeypatch.setattr(executor, 'owned_container', outer_container)
        original_which=launcher.shutil.which
        monkeypatch.setattr(launcher.shutil,'which',lambda name: '/fixture/docker' if name=='docker' else original_which(name))

        def cpu_execute(repo, args, timeout, **kwargs):
            self.calls += 1
            path = str(tmp / 'reply.sock')
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen(1)
            errors = []
            def serve():
                try:
                    conn, _ = server.accept()
                    with conn:
                        data = b''
                        while not data.endswith(b'\n'):
                            data += conn.recv(65536)
                        request = json.loads(data)
                        cfg = {'test_image':os.environ.get('AITELIER_TEST_FIXTURE_IMAGE','sha256:'+'a'*64),
                               'executor_entry':str(self.entry_path),
                               'allowed_roots':[str(tmp)], 'aitelier_home':str(tmp/'home'),
                               'state_db':str(tmp/'host.db'),'allowed_uids':[os.getuid()]}
                        fields = {**request, 'project_id':'p', 'git_mounts':[]}
                        result = launcher._run_docker(cfg, fields)
                        if self.identity_change and self.calls > 1:
                            self.identity_change(result)
                        conn.sendall((json.dumps({'ok':True, 'result':result, 'git':None})+'\n').encode())
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    server.close()
            thread = threading.Thread(target=serve)
            thread.start()
            try:
                return executor._launch_via_host(path, repo, args, min(timeout, 30), **kwargs)
            finally:
                thread.join(40)
                Path(path).unlink(missing_ok=True)
                assert not thread.is_alive()
                assert not errors, errors
        self.cpu_execute=cpu_execute
        self.outer_container=outer_container
        monkeypatch.setattr(executor, 'execute', cpu_execute)

    def drive(self, change=None, stop_after_baseline=False):
        import api.dependencies as deps
        self.monkeypatch.setattr(deps,'get_skillflow',lambda: self.sf)
        self.monkeypatch.setattr(deps,'get_db_manager',lambda: self.db)
        self.monkeypatch.setattr(deps,'get_config_registry',lambda: self.registry)
        self.monkeypatch.setattr(executor,'execute',self.cpu_execute)
        self.monkeypatch.setattr(executor,'owned_container',self.outer_container)
        self.monkeypatch.setenv('AITELIER_HOME',str(self.tmp/'home'))
        count = 0
        for _ in range(60):
            node = self.sf.advance_run(self.rid)
            run = self.sf.get_run(self.rid)
            if run['status'] in {'completed', 'failed'}:
                if run['status']=='failed': print('NATIVE TERMINAL',run)
                return run['status'], count
            if stop_after_baseline and self.calls:
                return run['status'], count
            if node is None:
                continue
            claim = self.sf.claim_next_step(self.rid)
            if claim is None:
                continue
            assert claim.step_id == 'implement'
            count += 1
            if change:
                before={p.relative_to(self.repo).as_posix():p.read_text() for p in self.repo.iterdir()
                        if p.is_file() and p.name!='.git'}
                change(self.repo, count)
                after={p.relative_to(self.repo).as_posix():p.read_text() for p in self.repo.iterdir()
                       if p.is_file() and p.name!='.git'}
                assert before.keys() <= after.keys(), "fixture uses native edits, not unregistered deletes"
                for name, content in after.items():
                    if before.get(name)==content: continue
                    path=self.repo/name
                    if name in before:
                        path.write_text(before[name])
                        tool,params='edit',{'file':name,'old_str':before[name],'new_str':content}
                    else:
                        path.unlink()
                        tool,params='create',{'file':name,'content':content}
                    reply=self.sf.execute_tool(tool,params,run_id=self.rid,step_id=claim.step_id,
                        step_instance_id=claim.token.step_instance_id,claim_epoch=claim.token.claim_epoch)
                    assert not reply.get('error'),reply
            write_claim_code(self.sf, self.rid, claim)
            self.sf.confirm_step(claim.token, StepResult(flags={}))
        pytest.fail('native graph failed to terminate')

    def measurement(self):
        return json.loads((self.ws.get_final_path('p','test','coding_impl')/'candidate_report.json').read_text())

    def report(self):
        return json.loads((self.ws.get_final_path('p','test','coding_impl')/'test_report.json').read_text())


def useful_change(repo, n):
    (repo / 'feature.py').write_text('def tag(value):\n    return value.strip().lower().replace(" ", "-")\n')


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_actual_red_candidate_one_implement_and_all_four_complete_consumers(rig):
    status, count = rig.drive(useful_change)
    assert (status, count, rig.calls) == ('completed', 1, 2)
    report = rig.report()
    assert report['passed'] is False
    assert report['coding_outcome']['cause_attribution'] == 'undetermined'
    assert any(c['status']=='skipped' for c in rig.measurement()['cases'].values())
    from aitelier.gate_evidence import release_disposition
    assert release_disposition(report) != 'passed'
    assert git(rig.repo, 'status', '--porcelain') == ''
    assert 'strip' in git(rig.repo, 'show', 'HEAD:feature.py')
    result = subprocess.check_output([sys.executable, '-c', "from feature import tag; print(tag(' A B '))"], cwd=rig.repo, text=True)
    assert result.strip() == 'a-b'
    # Put authoritative outcome after every historical display cap.
    path = rig.ws.get_final_path('p','test','coding_impl')/'test_report.json'
    assert path.read_text().index('coding_outcome') > 8000
    summaries = [summarise_run(rig.sf, rig.ws, rig.registry, rig.rid),
                 rig.agent._tool_get_pipeline_result({'run_id':rig.rid}),
                 await rig.agent._tool_wait_until_checkpoint({'run_id':rig.rid}),
                 await rig.agent._run_pipeline_until_checkpoint(rig.rid)]
    for result in summaries:
        assert result['status'] == 'candidate_tests_failed', result
        assert result['execution_status'] == 'completed'
        assert result['tests_passed'] is result['release_passed'] is False
        assert 'Tests FAIL' in result.get('message',result.get('verdict',''))
        assert 'test_total' in result['failure_references'][0]
    # current_node still names a validator on native engines; the actual report decides.
    path.write_text('{corrupt')
    assert rig.agent._summarize_run_state(rig.rid)['status'] == 'result_evidence_invalid'


@pytest.mark.parametrize('mode', ['exception','skip','disappear','config','profile','entry-drift','mutable-image','missing','corrupt'])
def test_new_observation_or_lost_measurement_does_not_retain(rig, mode):
    if mode == 'profile':
        rig.identity_change = lambda result: result.pop('execution_identity', None)
    elif mode == 'entry-drift':
        rig.drift_entry=True
    elif mode == 'mutable-image':
        rig.identity_change = lambda result: result['execution_identity'].update(image='aitelier:latest')
    def change(repo, n):
        useful_change(repo,n)
        if mode == 'exception':
            (repo/'catalog.py').write_text('def total():\n    raise RuntimeError("new cause")\n')
        elif mode == 'skip':
            (repo/'catalog.py').write_text('import pytest\ndef total():\n    pytest.skip("lost measurement")\n')
        elif mode == 'disappear':
            (repo/'test_app.py').write_text('def test_other():\n    assert True\n')
        elif mode == 'config':
            (repo/'pytest.ini').write_text('[pytest]\naddopts = --tb=long\n')
        elif mode == 'missing':
            (rig.ws.get_final_path('p','baseline','coding_impl')/'prechange_report.json').unlink(missing_ok=True)
        elif mode == 'corrupt':
            (rig.ws.get_final_path('p','baseline','coding_impl')/'prechange_report.json').write_text('{corrupt')
    status, count = rig.drive(change)
    assert (status, count) == (('completed',1) if mode in {'skip','disappear'} else ('failed',4))
    assert not rig.report().get('coding_outcome')


def test_base_green_new_red_uses_repair(tmp_path, monkeypatch):
    rig = Rig(tmp_path,monkeypatch,green=True)
    def regression(repo,n):
        (repo/'catalog.py').write_text('def total():\n    return 8\n')
    assert rig.drive(regression) == ('failed',4)


def test_indistinguishable_cause_is_failed_not_proven_unchanged(rig):
    def changed_cause(repo,n):
        useful_change(repo,n)
        (repo/'catalog.py').write_text('def total():\n    return sum([4, 5])\n')
    assert rig.drive(changed_cause) == ('completed',1)
    assert rig.report()['coding_outcome']['cause_attribution']=='undetermined'


def test_owned_resume_and_crossrun_capture_refusal(rig):
    assert rig.drive(stop_after_baseline=True)==('running',0)
    baseline = rig.ws.get_final_path('p','baseline','coding_impl')/'prechange_report.json'
    original = baseline.read_bytes()
    assert rig.drive(useful_change)==('completed',1)
    assert baseline.read_bytes()==original
    data=json.loads(original)
    candidate=rig.measurement()
    data['binding']['run_id']='another-run'
    with pytest.raises(ValueError,match='binding'):
        outcome.compare(data,candidate,'a'*64)


def test_capture_refuses_dirty_direct_relay_and_nonoriginal(rig):
    (rig.repo/'dirty.txt').write_text('not captured')
    with pytest.raises(ValueError): outcome.owned_snapshot(rig.repo,rig.rid,original=True)
    (rig.repo/'dirty.txt').unlink()
    for column,value in [('mode','direct'),('note','relay of prior draft')]:
        with rig.db.get_connection() as conn:
            conn.execute('UPDATE run_isolation SET '+column+'=? WHERE run_id=?',(value,rig.rid));conn.commit()
        with pytest.raises(ValueError): outcome.owned_snapshot(rig.repo,rig.rid,original=True)
        with rig.db.get_connection() as conn:
            conn.execute('UPDATE run_isolation SET mode="worktree",note=NULL WHERE run_id=?',(rig.rid,));conn.commit()
    (rig.repo/"feature.py").write_text("def tag(value):\n    return value.strip()\n")
    git(rig.repo,"add","feature.py");git(rig.repo,"commit","-qm","candidate exists before recapture")
    with pytest.raises(ValueError): outcome.owned_snapshot(rig.repo,rig.rid,original=True)


@pytest.mark.asyncio
async def test_all_completed_consumers_refuse_missing_or_contradictory_evidence(rig):
    assert rig.drive(useful_change)==('completed',1)
    path=rig.ws.get_final_path('p','test','coding_impl')/'test_report.json'
    good=json.loads(path.read_text())
    for variant in ('missing','corrupt','contradictory','wrong-run'):
        if variant=='missing': path.unlink(missing_ok=True)
        elif variant=='corrupt': path.write_text('[]')
        else:
            report=json.loads(json.dumps(good))
            if variant=='contradictory': report['coding_outcome']['release_passed']=True
            else: report['run_binding']['run_id']='another-run'
            path.write_text(json.dumps(report))
        summaries=[summarise_run(rig.sf,rig.ws,rig.registry,rig.rid),
                   rig.agent._tool_get_pipeline_result({'run_id':rig.rid}),
                   await rig.agent._tool_wait_until_checkpoint({'run_id':rig.rid}),
                   await rig.agent._run_pipeline_until_checkpoint(rig.rid)]
        assert all(s['status']=='result_evidence_invalid' for s in summaries), summaries


def test_true_green_keeps_normal_completed_result(tmp_path, monkeypatch):
    rig=Rig(tmp_path,monkeypatch,green=True)
    assert rig.drive(useful_change)==('completed',1)
    assert rig.report()['passed'] is True
    assert summarise_run(rig.sf,rig.ws,rig.registry,rig.rid)['status']=='completed'


def test_native_failed_candidate_validator_requires_nested_properties(tmp_path):
    from tests.skillflow.test_coding_impl_gate import _drive
    sf,rid,_=_wire(tmp_path,[{'passed':False,'summary':'failed','failures':['real failure'],
                             'coding_outcome':{'disposition':'candidate_tests_failed'}}],
                   tool_result={'written':'test_report.json','retained_red_candidate':True})
    assert _drive(sf,rid)==('failed',1)


def test_unknown_graph_and_metadata_failure_leave_ordinary_execution(rig, monkeypatch):
    from core import repository_executor_entry as entry
    from importlib import metadata
    monkeypatch.setattr(metadata,'distributions',lambda: (_ for _ in ()).throw(ValueError('metadata unavailable')))
    result=entry.run({'repo':str(rig.repo),'args':[sys.executable,'-m','pytest',str(rig.repo),'-q'],
                      'timeout':20,'pytest_timeout':True,'import_module':''})
    assert result['returncode']==1 and not result.get('execution_identity')
    graph=rig.sf._graph_for_run(rig.rid).to_dict()
    graph['name']='gen_coding_impl'
    from skillflow import PipelineGraph
    import yaml
    generated=rig.tmp/"generated.yaml"
    generated.write_text(yaml.safe_dump(graph))
    rig.sf.register_graph(PipelineGraph.from_yaml(generated))
    rid=rig.sf.create_run('gen_coding_impl',{'project_id':'p'})
    from aitelier.tools.run_tests.impl import run_tests
    result=run_tests(project_root=str(rig.repo),out_dir=str(rig.tmp/'unsupported'),
                     run_id=rid,prechange_capture=True)
    assert result['retained_red_candidate'] is False
    assert not (rig.tmp/'unsupported'/'prechange_report.json').exists()


def test_concurrent_owned_runs_share_source_but_never_capture_authority(tmp_path,monkeypatch):
    one=Rig(tmp_path/'one',monkeypatch)
    two=Rig(tmp_path/'two',monkeypatch,source_repo=tmp_path/'one'/'proj'/'p')
    assert one.repo != two.repo
    assert git(one.repo,'rev-parse','HEAD')==git(two.repo,'rev-parse','HEAD')
    assert one.drive(stop_after_baseline=True)==('running',0)
    assert two.drive(stop_after_baseline=True)==('running',0)
    assert one.drive(useful_change)==('completed',1)
    assert two.drive(useful_change)==('completed',1)
    assert one.report()['coding_outcome']['run_id']!=two.report()['coding_outcome']['run_id']
    assert one.report()['coding_outcome']['baseline_artifact_sha256']!=two.report()['coding_outcome']['baseline_artifact_sha256']


@pytest.mark.parametrize('mode',['zero','no-runner'])
def test_unmeasured_gate_cannot_retain_candidate(rig,mode):
    if mode=='no-runner':
        def unavailable(*args,**kwargs):
            raise executor.IsolationUnavailable('owned CPU runner unavailable')
        rig.cpu_execute=unavailable
    def remove_tests(repo,n):
        if mode=='zero':
            (repo/'test_app.py').write_text('')
    assert rig.drive(remove_tests)==('failed',1)
    assert not rig.report().get('coding_outcome')
