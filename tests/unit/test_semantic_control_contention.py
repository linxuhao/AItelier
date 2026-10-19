"""Real SQLite locks and normal serial worker recovery, without a live index service."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from core import semantic_index_control as control

SCRIPT = Path(control.__file__).resolve()


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def demand(tmp):
    source = tmp / 'source'; source.mkdir()
    git(source, 'init', '-q'); git(source, 'config', 'user.name', 'fixture')
    git(source, 'config', 'user.email', 'fixture@localhost')
    (source / 'code.py').write_text('VALUE = 42\n')
    git(source, 'add', 'code.py'); git(source, 'commit', '-qm', 'fixture')
    runs = tmp / 'runs'; runs.mkdir(); root = runs / 'owned'
    git(source, 'worktree', 'add', '-qb', 'owned', str(root))
    ctl = control.IndexControl(tmp / 'control', runs)
    before = ctl.request({'run_id': 'owned', 'worktree_path': str(root), 'source_repo': str(source)}, 'ready')
    # The offline command fixture performs and verifies a real hash-index write.
    # No installed ZG client, daemon, model, canonical root or fake health is used.
    binary = tmp / 'bin'; binary.mkdir(); provider = binary / 'zg'
    provider.write_text(f'#!{sys.executable}\n' + '''import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[2]);cache=root/'.zvec-grep';expected=hashlib.sha256((root/'code.py').read_bytes()).hexdigest()
if sys.argv[1]=='index':
 cache.mkdir(exist_ok=True);(cache/'manifest.json').write_text(json.dumps({'code.py':expected}))
else:
 assert json.loads((cache/'manifest.json').read_text())=={'code.py':expected}
''')
    provider.chmod(0o700)
    env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH']}
    return ctl, root, before, env


def lock_database(db):
    # Independent process, actual SQLite exclusive WAL lock, existing 2s busy
    # timeout left unchanged. stdin EOF releases the lock deterministically.
    p = subprocess.Popen([sys.executable, '-u', '-c', '''import sqlite3,sys
c=sqlite3.connect(sys.argv[1],timeout=2)
c.execute('PRAGMA locking_mode=EXCLUSIVE');c.execute('BEGIN EXCLUSIVE')
print('LOCKED',flush=True);sys.stdin.read();c.rollback();c.close()
''', str(db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == 'LOCKED'
    return p


def release(p):
    if p.poll() is None:
        p.stdin.close(); p.wait(timeout=5)
    assert p.returncode == 0, p.stderr.read()


def await_file(path, worker=None, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert worker is None or worker.poll() is None, 'worker exited before the next real phase'
        assert time.monotonic() < deadline, str(path)
        time.sleep(.02)


def start_worker(tmp, ctl, env, phase='startup', once=False):
    # For later phases only, a barrier immediately BEFORE the real method lets
    # the parent lock SQLite. No replacement SQL result or raised error is used.
    runner = tmp / 'worker.py'
    runner.write_text('''import importlib.util,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('worker_control',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
phase=sys.argv[2];directory=Path(sys.argv[3]);sent=False
if phase!='startup':
 name={'scan':'process_once','current':'get','finish':'_finish'}[phase];original=getattr(m.IndexControl,name)
 def gated(self,*a,**kw):
  global sent
  if not sent:
   sent=True;(directory.parent/'gate-ready').write_text(str(__import__('os').getpid()))
   deadline=time.monotonic()+10
   while not (directory.parent/'gate-continue').exists():
    if time.monotonic()>deadline:raise RuntimeError('fixture barrier timeout')
    time.sleep(.02)
  return original(self,*a,**kw)
 setattr(m.IndexControl,name,gated)
sys.argv=['semantic-worker','--control-dir',str(directory),'--worktrees-root',sys.argv[4],*sys.argv[5:]]
try:m.main()
except KeyboardInterrupt:pass
''')
    output = (tmp / 'worker.stdout').open('w'); error = (tmp / 'worker.stderr').open('w')
    worker = subprocess.Popen([sys.executable, '-u', str(runner), str(SCRIPT), phase,
                               str(ctl.directory), str(ctl.managed_root), *(['--once'] if once else [])],
                              env=env, stdout=output, stderr=error)
    output.close(); error.close()
    return worker


@pytest.mark.parametrize('phase', ['startup', 'scan', 'current', 'finish'])
def test_worker_survives_real_contention_and_reconciles(tmp_path, phase):
    ctl, root, before, env = demand(tmp_path)
    locker = worker = None
    try:
        if phase == 'startup':
            locker = lock_database(ctl.database)
        worker = start_worker(tmp_path, ctl, env, phase)
        pid = worker.pid
        if phase != 'startup':
            await_file(tmp_path / 'gate-ready', worker)
            locker = lock_database(ctl.database)
            (tmp_path / 'gate-continue').touch()
        # Two original busy timeouts plus the existing loop sleep. A daemon
        # which only restarts under an external supervisor cannot pass this.
        time.sleep(6.3)
        stdout = (tmp_path / 'worker.stdout').read_text()
        stderr = (tmp_path / 'worker.stderr').read_text()
        print('LOCKED_WORKER', json.dumps({'phase':phase,'pid':pid,'returncode':worker.poll(),'stdout':stdout,'stderr':stderr}))
        assert worker.poll() is None, 'same worker died under real SQLite contention'
        assert 'database is locked' in stdout
        assert stdout.count('[zg-control]') == 1, 'one diagnostic per contention episode, not log spam'
        release(locker); locker = None
        deadline = time.monotonic() + 12
        while not ctl.settled(ctl.get('owned'), 'ready'):
            assert worker.poll() is None
            assert time.monotonic() < deadline, 'worker did not resume useful owned work'
            time.sleep(.05)
        assert worker.pid == pid
        after = ctl.get('owned')
        assert after['revision'] == before['revision'] == after['done_revision']
        assert after['root'] == before['root'] and after['source'] == before['source']
        assert after['desired'] == 'ready' and after['outcome'] == 'ready' and after['error'] == ''
        assert json.loads((root / '.zvec-grep/manifest.json').read_text()) == {
            'code.py': hashlib.sha256((root / 'code.py').read_bytes()).hexdigest()}
        assert 'database is locked' in (tmp_path / 'worker.stdout').read_text()
        print('RECOVERED_SAME_WORKER', json.dumps({'phase':phase,'pid':pid,'before':before,'after':after}))
    finally:
        if worker is not None:
            if worker.poll() is None: worker.terminate()
            worker.wait(timeout=8)
            print('FINAL_WORKER_STREAMS', json.dumps({'phase':phase,'stdout':(tmp_path/'worker.stdout').read_text(),'stderr':(tmp_path/'worker.stderr').read_text()}))
        if locker is not None: release(locker)
    # SIGTERM/cancellation releases all existing ownership boundaries.
    with control.service_lock(ctl.directory, 'worker.lock'), control.service_lock(ctl.directory, 'operation.lock'):
        fence=ctl.directory.parent/'godot-control/deployment-admission.lock'
        with fence.open('r+') as f: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)


def test_once_reports_real_contention_and_preserves_demand(tmp_path):
    ctl, root, before, env = demand(tmp_path); locker = lock_database(ctl.database)
    try:
        worker=start_worker(tmp_path,ctl,env,once=True);worker.wait(timeout=6)
        assert worker.returncode != 0
        assert 'database is locked' in (tmp_path/'worker.stderr').read_text()
    finally: release(locker)
    assert ctl.get('owned') == before and not (root/'.zvec-grep').exists()


def test_once_reports_an_existing_command_error_without_settling(tmp_path):
    ctl, root, before, env = demand(tmp_path)
    (tmp_path/'bin/zg').write_text(f'#!{sys.executable}\nimport sys\nprint("owned fixture failure",file=sys.stderr)\nsys.exit(7)\n')
    worker=start_worker(tmp_path,ctl,env,once=True);worker.wait(timeout=8)
    assert worker.returncode != 0
    after=ctl.get('owned')
    assert after['outcome']=='error' and after['desired']=='ready'
    assert after['revision']==before['revision'] and after['done_revision']!=after['revision']
    assert 'exit=7' in after['error'] and 'owned fixture failure' in after['error']
    assert list(ctl.directory.glob('command-*.raw'))


def test_once_reports_existing_project_failure_without_creating_ledger(tmp_path):
    root=tmp_path/'projects/p';root.mkdir(parents=True)
    git(root,'init','-q');git(root,'config','user.name','fixture');git(root,'config','user.email','fixture@localhost')
    (root/'code.py').write_text('value=1');git(root,'add','code.py');git(root,'commit','-qm','fixture')
    binary=tmp_path/'bin';binary.mkdir();provider=binary/'zg'
    provider.write_text(f'#!{sys.executable}\nimport sys\nprint("owned project failure",file=sys.stderr)\nsys.exit(7)\n');provider.chmod(0o700)
    directory=tmp_path/'control'
    r=subprocess.run([sys.executable,str(SCRIPT),'--control-dir',str(directory),'--worktrees-root',str(tmp_path/'runs'),
                      '--projects-root',str(root.parent),'--once'],capture_output=True,text=True,timeout=8,
                     env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH']})
    assert r.returncode != 0 and 'retained for repair' in r.stdout
    marker=json.loads((directory/'project-owner.json').read_text())
    assert marker['status']=='error' and marker['first_failure']['error']==marker['error']
    assert 'owned project failure' in marker['error']
    assert not (directory/'control.sqlite3').exists()


def test_once_reports_existing_resource_busy_without_losing_demand(tmp_path):
    ctl, root, before, env = demand(tmp_path)
    with ctl.lock('owned'):
        worker=start_worker(tmp_path,ctl,env,once=True);worker.wait(timeout=6)
    assert worker.returncode != 0
    assert 'incomplete' in (tmp_path/'worker.stderr').read_text()
    assert ctl.get('owned') == before and not (root/'.zvec-grep').exists()


@pytest.mark.parametrize('fault', ['schema', 'corrupt', 'permission', 'symlink'])
def test_noncontention_faults_fail_visibly_without_recovery(tmp_path, fault):
    directory=tmp_path/'control';directory.mkdir();db=directory/'control.sqlite3'
    if fault=='schema':
        with sqlite3.connect(db) as c:c.execute('CREATE TABLE unrelated(value)')
    elif fault=='corrupt':db.write_bytes(b'not a SQLite database')
    elif fault=='permission':db.write_text('private');db.chmod(0)
    else:
        outside=tmp_path/'outside';outside.write_text('untouched');db.symlink_to(outside)
    before=db.lstat()
    try:
        r=subprocess.run([sys.executable,str(SCRIPT),'--control-dir',str(directory),'--worktrees-root',str(tmp_path/'runs')],capture_output=True,text=True,timeout=6)
        assert r.returncode != 0 and r.stderr
        assert '[zg-control]' not in r.stdout
        assert db.lstat().st_ino==before.st_ino
    finally:
        if fault=='permission':db.chmod(0o600)


def test_read_inspection_does_not_migrate_or_recreate_ledger(tmp_path):
    directory=tmp_path/'control';ctl=control.IndexControl(directory,tmp_path/'runs')
    with ctl.connection() as c:c.execute('PRAGMA user_version=71');c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    before=hashlib.sha256(ctl.database.read_bytes()).hexdigest();inode=ctl.database.stat().st_ino
    r=subprocess.run([sys.executable,str(SCRIPT),'--control-dir',str(directory),'--worktrees-root',str(tmp_path/'runs'),'--once'],capture_output=True,text=True,timeout=6)
    assert r.returncode==0,r.stderr
    assert ctl.database.stat().st_ino==inode and hashlib.sha256(ctl.database.read_bytes()).hexdigest()==before
    with ctl.connection() as c:assert c.execute('PRAGMA user_version').fetchone()[0]==71


def test_classifier_uses_real_sqlite_codes_not_error_prose(tmp_path):
    uri='file:'+str(tmp_path/'locked.sqlite')+'?cache=shared'
    a=sqlite3.connect(uri,uri=True);b=sqlite3.connect(uri,uri=True)
    try:
        a.execute('CREATE TABLE fixture(x)');a.commit();a.execute('BEGIN');a.execute('SELECT * FROM fixture').fetchall()
        with pytest.raises(sqlite3.OperationalError) as locked:b.execute('DROP TABLE fixture')
        assert locked.value.sqlite_errorcode & 255 == sqlite3.SQLITE_LOCKED
        assert control._sqlite_contention(locked.value)
        with pytest.raises(sqlite3.OperationalError) as unrelated:b.execute('SELECT * FROM absent')
        assert not control._sqlite_contention(unrelated.value)
        assert not control._sqlite_contention(sqlite3.OperationalError('database is locked'))
    finally:a.close();b.close()
