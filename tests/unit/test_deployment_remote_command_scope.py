"""Synthetic SSH command opacity tests; no private reports or host processes."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from core import deployment_quiescence as dq


def proc(root,pid,argv,exe=None,comm=None,ppid=1):
    p=root/str(pid);p.mkdir(parents=True)
    (p/'cmdline').write_bytes(b'\0'.join(w.encode() for w in argv)+b'\0')
    (p/'comm').write_text((comm or Path(argv[0]).name)+'\n')
    (p/'exe').symlink_to(exe or argv[0])
    return f'{pid} {ppid} '+ ' '.join(argv)


def probe(module,lines,registered=None):
    return module.external_owners(runner=lambda cmd:SimpleNamespace(returncode=0,stdout='' if cmd[0]=='docker' else '\n'.join(lines),stderr=''),registered_external_owners=registered)


@pytest.mark.parametrize('payload', [
    'python3 /tmp/evaluator_worker.py',
    'python3 /tmp/a.py',
    'python3 -m skillflow --job evaluator_worker',
    'echo media; python3 /tmp/judge_fixture.py',
    'tar czf - preview/review.mp4 && python3 /tmp/evaluator_worker.py',
])
def test_remote_command_launch_remains_opaque(tmp_path,monkeypatch,payload):
    root=tmp_path/'proc';argv=['/usr/sbin/tailscaled','be-child','ssh','--cmd='+payload]
    line=proc(root,4242,argv)
    monkeypatch.setattr(dq,'PROC_ROOT',root)
    owners,errors=probe(dq,[line])
    assert dq._measurement_subject(line)==line
    assert owners[0]['active'] and owners[0]['ownership']=='unregistered'
    assert owners[0]['resource']=='external_measurement' and errors


def test_correlated_native_unrelated_file_export_remains_argument_data(tmp_path,monkeypatch):
    root=tmp_path/'proc'
    line=proc(root,4242,['/usr/bin/tar','cf','-','preview/review.mp4','docs/metrics.md'])
    monkeypatch.setattr(dq,'PROC_ROOT',root)
    assert probe(dq,[line])==([],[])
    assert dq._measurement_subject(line)=='/usr/bin/tar'
    # This native command identity control is not proof about any SSH wrapper,
    # path contents, lineage or the past foreign export's actual effects.


def test_tailscaled_daemon_settings_are_not_a_command_launch(tmp_path,monkeypatch):
    root=tmp_path/'proc';line=proc(root,4242,['/usr/sbin/tailscaled','--state=/tmp/review/metrics.json'])
    monkeypatch.setattr(dq,'PROC_ROOT',root)
    assert probe(dq,[line])==([],[])


@pytest.mark.parametrize('fault',['missing_proc','missing_comm','missing_exe','partial_argv','changed_argv','invalid_utf8','deceptive_shell'])
def test_unknown_or_deceptive_remote_lineage_never_becomes_readonly(tmp_path,monkeypatch,fault):
    root=tmp_path/'proc';argv=['/usr/sbin/tailscaled','be-child','ssh','--cmd=tar czf - review.mp4']
    line=proc(root,4242,argv);p=root/'4242'
    if fault=='missing_proc':root=tmp_path/'missing'
    elif fault=='missing_comm':(p/'comm').unlink()
    elif fault=='missing_exe':(p/'exe').unlink()
    elif fault=='partial_argv':(p/'cmdline').write_bytes(b'/usr/sbin/tailscaled\0be-child')
    elif fault=='changed_argv':(p/'cmdline').write_bytes(b'/usr/sbin/tailscaled\0be-child\0ssh\0--cmd=other\0')
    elif fault=='invalid_utf8':(p/'cmdline').write_bytes(b'/usr/sbin/tailscaled\0\xff\0')
    else:
        line='4242 1 /bin/bash -c "tar czf - review.mp4 || python evaluator_worker.py"'
        (p/'cmdline').write_bytes(b'/bin/bash\0-c\0tar czf - review.mp4 || python evaluator_worker.py\0')
        (p/'comm').write_text('bash');(p/'exe').unlink();(p/'exe').symlink_to('/usr/bin/bash')
    monkeypatch.setattr(dq,'PROC_ROOT',root)
    owners,errors=probe(dq,[line]);assert owners and owners[0]['active'] and errors


def test_related_worker_descendant_and_registered_identity_are_retained(tmp_path,monkeypatch):
    root=tmp_path/'proc'
    parent=proc(root,4242,['/usr/sbin/tailscaled','be-child','ssh','--cmd=python3 /tmp/evaluator_worker.py --job owner-exact'])
    child=proc(root,4243,['/usr/bin/python3','/tmp/evaluator_worker.py','--job','owner-exact'],ppid=4242,comm='python3')
    monkeypatch.setattr(dq,'PROC_ROOT',root)
    rows=[{'status':'active','external_id':'owner-exact','attempt_id':'attempt-exact'}]
    owners,errors=probe(dq,[parent,child],rows)
    assert len(owners)==2 and all(x['active'] and x['attempt_id']=='attempt-exact' for x in owners)
    assert errors==[]


def test_missing_remote_lineage_remains_unknown(tmp_path, monkeypatch):
    # An invented SSH command is a portability fixture, not evidence about any
    # past process, media file, project, or actual execution effects.
    line = '4242 1 /usr/sbin/tailscaled be-child ssh --cmd=tar czf - preview/review.mp4'
    monkeypatch.setattr(dq, 'PROC_ROOT', tmp_path / 'absent')
    owners, errors = probe(dq, [line])
    assert owners[0]['active'] and errors
    assert owners[0]['command'] == line
