"""Bounded causal process-reader classification controls."""
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
import pytest
import os
import importlib.util
from core import deployment_quiescence as dq
if os.environ.get('READONLY_BASELINE') == '1':
    spec=importlib.util.spec_from_file_location('readonly_baseline', '/reports/baseline.py')
    dq=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dq)


def fixture(tmp_path, monkeypatch):
    root=tmp_path/'proc'
    raw=json.loads(Path('/reports/original-attribution.json').read_text())
    shell=['/bin/bash','-c', raw['cmdline'].split('/bin/bash -c ',1)[1]]
    # Attribution stores flattened argv: the shell code remains one argument.
    cmds={3233295:shell,3233297:['rg','-n','-i','reference video|参考视频|<Video','--glob','*.md','--glob','*.py','-l'],3233298:['head']}
    for pid,argv in cmds.items():
        p=root/str(pid);p.mkdir(parents=True)
        (p/'cmdline').write_bytes(b'\0'.join(x.encode() for x in argv)+b'\0')
        (p/'comm').write_text(Path(argv[0]).name+'\n')
        (p/'exe').symlink_to('/usr/bin/'+Path(argv[0]).name)
        (p/'cwd').symlink_to('/home/linxuhao/agentmcp')
        (p/'stat').write_text(f'{pid} ({Path(argv[0]).name}) S ' + ('2298422' if pid==3233295 else '3233295') + ' ' + ' '.join(['0']*17+['33352255']))
    p=root/'3233295';(p/'wchan').write_text('do_wait\n'); t=p/'task'/'3233295';t.mkdir(parents=True);(t/'children').write_text('3233297 3233298')
    monkeypatch.setattr(dq,'PROC_ROOT',root)
    line='3233295 2298422 '+' '.join(shell)
    return root,cmds,line


def probe(lines,registered=None):
    return dq.external_owners(runner=lambda cmd: SimpleNamespace(returncode=0,stdout='' if cmd[0]=='docker' else '\n'.join(lines),stderr=''),registered_external_owners=registered)


def test_actual_readonly_wrapper(tmp_path,monkeypatch):
    root,cmds,line=fixture(tmp_path,monkeypatch)
    assert dq._measurement_subject(line)=='/bin/bash'
    owners,errors=probe([line]+[f'{pid} 3233295 '+ ' '.join(argv) for pid,argv in cmds.items() if pid!=3233295])
    assert owners==[] and errors==[]


@pytest.mark.parametrize('failure',['missing_exe','spoof_exe','missing_cwd','missing_child','child_parent','child_preprocessor','unknown_child','not_waiting','partial_argv','missing_comm','partial_stat','different_cwd','future_worker'])
def test_unproven_wrapper_remains_full_scan(tmp_path,monkeypatch,failure):
    root,cmds,line=fixture(tmp_path,monkeypatch);p=root/'3233295';c=root/'3233297'
    if failure=='missing_exe':(p/'exe').unlink()
    elif failure=='spoof_exe':(p/'exe').unlink();(p/'exe').symlink_to('/tmp/bash')
    elif failure=='missing_cwd':(p/'cwd').unlink()
    elif failure=='missing_child':(c/'cmdline').unlink()
    elif failure=='child_parent':(c/'stat').write_text('3233297 (rg) S 77 0')
    elif failure=='child_preprocessor':(c/'cmdline').write_bytes(b'rg\0--pre=python\0metrics\0')
    elif failure=='unknown_child':(c/'exe').unlink();(c/'exe').symlink_to('/tmp/rg')
    elif failure=='not_waiting':(p/'wchan').write_text('running')
    elif failure=='partial_argv':(p/'cmdline').write_bytes(b'/bin/bash\0-c')
    elif failure=='missing_comm':(p/'comm').unlink()
    elif failure=='partial_stat':(c/'stat').write_text('3233297 (rg) S 3233295')
    elif failure=='different_cwd':(c/'cwd').unlink();(c/'cwd').symlink_to('/other')
    elif failure=='future_worker':
        changed=cmds[3233295][:];changed[2]=changed[2].replace('tail -3','python /tmp/evaluator_worker.py; tail -3')
        (p/'cmdline').write_bytes(b'\0'.join(x.encode() for x in changed)+b'\0')
        line='3233295 2298422 '+' '.join(changed)
    assert dq._measurement_subject(line)==line


@pytest.mark.parametrize('argv,exe',[(['rg','--pre=python','metrics'],'/usr/bin/rg'),(['rg','metrics'],'/tmp/rg'),(['head','metrics'],'/tmp/head'),(['docker','exec','godot-builder','godot','--headless'],'/usr/bin/docker'),(['python','/tmp/evaluator_worker.py'],'/usr/bin/python3'),(['godot','--headless','--render'],'/usr/bin/godot')])
def test_runtime_and_spoof_controls(tmp_path,monkeypatch,argv,exe):
    root,cmds,line=fixture(tmp_path,monkeypatch);p=root/'3233297'
    (p/'cmdline').write_bytes(b'\0'.join(x.encode() for x in argv)+b'\0');(p/'comm').write_text(Path(argv[0]).name);(p/'exe').unlink();(p/'exe').symlink_to(exe)
    owners,errors=probe(['3233297 3233295 '+' '.join(argv)])
    assert owners and owners[0]['active']


def test_registered_reader_still_blocks(tmp_path,monkeypatch):
    root,cmds,line=fixture(tmp_path,monkeypatch)
    owners,errors=probe([line], [{'external_id':'reference video','attempt_id':'owner-exact','status':'active'}])
    assert owners[0]['active'] and owners[0]['attempt_id']=='owner-exact'

@pytest.mark.parametrize('name', ['rg', 'head'])
def test_native_reader_measurement_arguments_are_data(tmp_path,monkeypatch,name):
    root,cmds,line=fixture(tmp_path,monkeypatch);p=root/'3233297'
    argv=[name, 'metrics video Godot evaluator_worker']
    (p/'cmdline').write_bytes(b'\0'.join(x.encode() for x in argv)+b'\0')
    (p/'comm').write_text(name);(p/'exe').unlink();(p/'exe').symlink_to('/usr/bin/'+name)
    assert probe(['3233297 3233295 '+' '.join(argv)]) == ([], [])

def test_old_new_collector_guard_fence_and_journal(tmp_path,monkeypatch):
    from core import deployment_quiescence as current
    from tests.unit.test_deployment_quiescence import _db
    root,cmds,line=fixture(tmp_path,monkeypatch)
    spec=importlib.util.spec_from_file_location('guard_baseline','/reports/baseline.py')
    old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    monkeypatch.setattr(old,'PROC_ROOT',root)
    monkeypatch.setattr(current,'admission_fence_path',lambda:tmp_path/'admission.lock')
    sf=SimpleNamespace(list_runs=lambda:[])
    runner=lambda cmd:SimpleNamespace(returncode=0,stdout='' if cmd[0]=='docker' else line,stderr='')
    observations={}
    fence=current.acquire_cutover_fence()
    try:
        for label,module in [('old',old),('new',current)]:
            observed=module.measure(skillflow=sf,db=_db(tmp_path/(label+'.sqlite')),command_runner=runner)
            observations[label]=observed
        assert observations['old']['quiescent'] is False
        assert observations['new']['quiescent'] is True  # This isolated fixture only.
        with pytest.raises(current.DeploymentBlocked):
            current.authorize('restart',observations['old'],journal=tmp_path/'journal.json')
        refused=json.loads((tmp_path/'journal.json').read_text())['latest']
        assert refused['status']=='aborted' and refused['usable'] is False
        assert refused['blockers']==observations['old']['blockers']
        clearance=current.authorize('restart',observations['new'],journal=tmp_path/'journal.json')
        terminal=current.finalize(clearance,success=False,error='fixture performed no restart',journal=tmp_path/'journal.json')
        assert terminal['event']['pending'] is False
        Path('/reports/causal-guard.json').write_text(json.dumps({'observations':observations,'original_refusal':refused,'terminal':terminal},indent=2))
    finally:
        current.release_cutover_fence(fence)
    assert fence.closed


@pytest.mark.parametrize('payload', [
    'rg video | head || python evaluator_worker.py',
    'rg video; head || python evaluator_worker.py',
    'rg video && python evaluator_worker.py',
    'rg video; python evaluator_worker.py',
    'rg video | python evaluator_worker.py',
    'rg video |& python evaluator_worker.py',
    'rg video ||| head', 'rg video ;; head', 'rg video |',
    'rg video; head <(python evaluator_worker.py)',
    'rg video; head (python evaluator_worker.py)',
])
def test_shell_control_continuations_remain_unknown(tmp_path, monkeypatch, payload):
    from tests.unit.test_deployment_quiescence import _db
    root, cmds, line = fixture(tmp_path, monkeypatch)
    shell = cmds[3233295][:]
    prefix, rest = shell[2].split("eval ", 1)
    shell[2] = prefix + "eval " + shlex.quote(payload) + " && pwd -P >| /tmp/claude-test-cwd"
    (root / '3233295' / 'cmdline').write_bytes(b'\0'.join(x.encode() for x in shell) + b'\0')
    line = '3233295 2298422 ' + ' '.join(shell)
    assert dq._measurement_subject(line) == line
    owners, errors = probe([line])
    assert owners and errors
    observed = dq.measure(skillflow=SimpleNamespace(list_runs=lambda: []),
                          db=_db(tmp_path / 'state.sqlite'),
                          command_runner=_probe_control(line))
    assert observed['quiescent'] is False
    monkeypatch.setattr(dq, 'admission_fence_path', lambda: tmp_path / 'fence')
    fence = dq.acquire_cutover_fence()
    try:
        with pytest.raises(dq.DeploymentBlocked):
            dq.authorize('restart', observed, journal=tmp_path / 'journal.json')
        refusal = json.loads((tmp_path / 'journal.json').read_text())['latest']
        assert refusal['pending'] is False and refusal['status'] == 'aborted'
    finally:
        dq.release_cutover_fence(fence)
    assert fence.closed


def _probe_control(line):
    return lambda cmd: SimpleNamespace(returncode=0, stdout='' if cmd[0] == 'docker' else line, stderr='')
