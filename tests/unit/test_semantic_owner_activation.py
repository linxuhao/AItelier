"""Service integration: real checkout ownership, faults, fences and restart."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core import deployment_quiescence as dq
from core.semantic_index_control import IndexControl, index_project_once, service_lock

ROOT = Path(__file__).resolve().parents[2]


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          text=True, capture_output=True).stdout.strip()


def repo(path):
    path.mkdir(parents=True)
    git(path, 'init', '-q')
    git(path, 'config', 'user.name', 'fixture')
    git(path, 'config', 'user.email', 'fixture@example.invalid')
    (path / 'code.py').write_text('VALUE = 1\n')
    git(path, 'add', 'code.py')
    git(path, 'commit', '-qm', 'seed')
    return path


def execute(command, *, timeout):
    root = Path(command[2])
    cache = root / '.zvec-grep'
    if command[1] == 'index' and '--drop' in command:
        (cache / 'manifest.json').unlink()
    elif command[1] == 'index':
        cache.mkdir(exist_ok=True)
        (cache / 'manifest.json').write_text('{}')
    else:
        assert (cache / 'manifest.json').exists()


def test_serial_project_discovery_retains_old_indexes_and_never_scans_runs(tmp_path):
    projects, worktrees, directory = (tmp_path / n for n in ('projects', 'worktrees', 'control'))
    directory.mkdir()
    old = repo(projects / 'a-old')
    execute(['zg', 'index', str(old)], timeout=1)
    first, second = repo(projects / 'b-new'), repo(projects / 'c-new')
    historical = repo(worktrees / 'historical')
    (projects / 'redirect').symlink_to(historical, target_is_directory=True)
    calls = []
    def provider(command, **kwargs):
        calls.append(command)
        execute(command, **kwargs)
    index_project_once(directory, projects, worktrees, execute=provider)
    assert [c[2] for c in calls] == [str(first), str(first)]
    assert not (second / '.zvec-grep').exists()
    assert not (historical / '.zvec-grep').exists()
    assert (old / '.zvec-grep/manifest.json').read_text() == '{}'
    index_project_once(directory, projects, worktrees, execute=provider)
    assert (second / '.zvec-grep').is_dir()
    assert not (directory / 'control.sqlite3').exists()
    assert dq._semantic_worker_errors(directory) == []


def test_project_failure_persists_and_restart_reconciles_same_root(tmp_path):
    projects, worktrees, directory = (tmp_path / n for n in ('projects', 'worktrees', 'control'))
    directory.mkdir()
    first, second = repo(projects / 'a'), repo(projects / 'b')
    def fail(command, **kwargs):
        execute(command, **kwargs)
        raise subprocess.TimeoutExpired(command, 1)
    with pytest.raises(subprocess.TimeoutExpired):
        index_project_once(directory, projects, worktrees, execute=fail)
    assert 'unknown' in dq._semantic_worker_errors(directory)[0]
    marker = json.loads((directory / 'project-owner.json').read_text())
    assert marker['root'] == str(first) and marker['status'] == 'error'
    calls = []
    def recovered(command, **kwargs):
        calls.append(command)
        execute(command, **kwargs)
    index_project_once(directory, projects, worktrees, execute=recovered)
    assert [c[1] for c in calls] == ['status']
    assert not (second / '.zvec-grep').exists()
    assert dq._semantic_worker_errors(directory) == []


def test_operation_lock_and_unknown_marker_fail_closed(tmp_path):
    with service_lock(tmp_path, 'operation.lock'):
        assert dq._semantic_worker_errors(tmp_path) == ['semantic indexing operation is active']
    assert dq._semantic_worker_errors(tmp_path) == []
    (tmp_path / 'project-owner.json').write_text('invalid')
    assert 'measurement failed' in dq._semantic_worker_errors(tmp_path)[0]


def test_competing_worker_is_rejected_and_idle_worker_does_not_create_ledger(tmp_path):
    command = [sys.executable, str(ROOT / 'core/semantic_index_control.py'),
               '--control-dir', str(tmp_path / 'control'), '--worktrees-root', str(tmp_path / 'runs'), '--once']
    with service_lock(tmp_path / 'control', 'worker.lock'):
        blocked = subprocess.run(command, capture_output=True, text=True)
        assert blocked.returncode and 'BlockingIOError' in blocked.stderr
    done = subprocess.run(command, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / 'control/control.sqlite3').exists()


def test_fresh_owner_prepare_release_preserves_other_owner_and_restart(tmp_path):
    source = repo(tmp_path / 'source')
    runs = tmp_path / 'runs'; runs.mkdir()
    records = []
    for rid in ('run-a', 'run-b'):
        path = runs / rid
        git(source, 'worktree', 'add', '-qb', rid, str(path))
        records.append({'run_id': rid, 'worktree_path': str(path), 'source_repo': str(source)})
    ctl = IndexControl(tmp_path / 'control', runs)
    for rec in records:
        ctl.request(rec, 'ready')
    assert len(ctl.process_once(execute=execute)) == 2
    assert all(ctl.settled(ctl.get(r['run_id']), 'ready') for r in records)
    ctl.request(records[0], 'released')
    recovered = IndexControl(tmp_path / 'control', runs, initialize=False)
    assert recovered.process_once(execute=execute) == [{'run_id': 'run-a', 'outcome': 'released'}]
    assert recovered.settled(recovered.get('run-a'), 'released')
    assert recovered.settled(recovered.get('run-b'), 'ready')
    assert (runs / 'run-b/.zvec-grep/manifest.json').exists()
    assert (runs / 'run-a/code.py').exists()


def test_cutover_fence_blocks_new_index_operation(tmp_path):
    directory = tmp_path / 'control'; directory.mkdir()
    fence = tmp_path / 'godot-control/deployment-admission.lock'
    fence.parent.mkdir()
    projects = tmp_path / 'projects'; root = repo(projects / 'new')
    with fence.open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        result = subprocess.run([sys.executable, str(ROOT / 'core/semantic_index_control.py'),
                                 '--control-dir', str(directory), '--worktrees-root', str(tmp_path / 'runs'),
                                 '--projects-root', str(projects), '--once'], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert not (root / '.zvec-grep').exists()
        assert not (directory / 'project-owner.json').exists()


def test_terminal_unowned_history_is_preserved_without_empty_ledger(tmp_path, monkeypatch):
    from core import datadir, run_isolation, run_resources
    from core.db_manager import DBManager
    monkeypatch.setenv('AITELIER_HOME', str(tmp_path))
    monkeypatch.setenv('AITELIER_ZVEC_LIFECYCLE', '1')
    source = repo(tmp_path / 'source'); runs = datadir.worktrees_dir(); runs.mkdir()
    root = runs / 'historic'; git(source, 'worktree', 'add', '-qb', 'historic', str(root))
    execute(['zg', 'index', str(root)], timeout=1)
    rec = {'run_id': 'historic', 'source_repo': str(source), 'worktree_path': str(root), 'mode': 'worktree'}
    monkeypatch.setattr(run_isolation, 'retained', lambda db: [rec])
    class Engine:
        def get_run(self, rid): return {'status': 'completed'}
        def audit_operation_owners(self, rid): return {'lost': [], 'unknown': [], 'alive': 0}
    db = DBManager(str(tmp_path / 'host.db'))
    result = run_resources.reconcile(db, Engine())
    assert result['requested'] == ['historic'] and not result['retained']
    assert (root / '.zvec-grep/manifest.json').exists()
    assert not (datadir.semantic_index_control_dir() / 'control.sqlite3').exists()


@pytest.mark.parametrize('status,blocked', [
    ({'queued_jobs': 0, 'running_jobs': 0, 'shutting_down': False}, False),
    ({'queued_jobs': 1, 'running_jobs': 0, 'shutting_down': False}, True),
    ({'queued_jobs': 0, 'running_jobs': 1, 'shutting_down': False}, True),
    ({'queued_jobs': 0, 'running_jobs': 0, 'shutting_down': True}, True),
    ({'queued_jobs': 0}, True),
    ({'queued_jobs': False, 'running_jobs': 0, 'shutting_down': False}, True),
])
def test_real_guard_observes_daemon_jobs_even_if_lifecycle_flag_is_disabled(tmp_path, monkeypatch, status, blocked):
    source = repo(tmp_path / 'source'); runs = tmp_path / 'runs'; runs.mkdir()
    root = runs / 'owned'; git(source, 'worktree', 'add', '-qb', 'owned', str(root))
    ctl = IndexControl(tmp_path / 'control', runs)
    rec = {'run_id': 'owned', 'source_repo': str(source), 'worktree_path': str(root)}
    ctl.request(rec, 'ready'); ctl.process_once(execute=execute)
    ctl.request(rec, 'released'); ctl.process_once(execute=execute)
    (ctl.directory / 'operation.lock').touch()
    monkeypatch.setenv('AITELIER_ZVEC_LIFECYCLE', '0')
    class Engine:
        def list_runs(self): return []
    def runner(command):
        assert command == ['docker', 'exec', 'fixture-zg', 'node', '/usr/local/lib/zvec-grep-status.mjs']
        return subprocess.CompletedProcess(command, 0, json.dumps(status), '')
    measured = dq.measure(skillflow=Engine(), sidecar_db=ctl.database,
                          external_probe=lambda: [{'kind': 'docker', 'name': 'fixture-zg', 'id': 'fixture',
                                                   'service': 'zvec-grep', 'active': False}],
                          command_runner=runner)
    assert measured['quiescent'] is (not blocked)
    assert bool(measured['errors']) is blocked


def test_project_marker_and_operation_symlinks_are_unknown(tmp_path):
    (tmp_path / 'operation.lock').symlink_to(tmp_path / 'missing')
    assert 'unknown' in dq._semantic_worker_errors(tmp_path)[0]
    (tmp_path / 'operation.lock').unlink()
    (tmp_path / 'project-owner.json').write_text('[]')
    assert 'malformed' in dq._semantic_worker_errors(tmp_path)[0]
