"""Preserve failed direct-code work without inventing another source directory.

Recovery uses Git objects and an isolated temporary INDEX FILE, not the live
index, not a source staging tree. The resulting commit is explicitly unvalidated.
The failed run's branch, worktree and real index are never changed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from skillflow.output_targets import code_path, git


def inventory(root: Path, config_dir: Path, run_id: str) -> dict:
    root = root.resolve()
    dirty = set(filter(None, git(root, 'diff', '--name-only', '-z', 'HEAD').split('\0')))
    dirty.update(filter(None, git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')))
    by_step = {}
    for journal in (config_dir / '.code-output' / run_id).glob('*.json'):
        record = json.loads(journal.read_text())
        if record.get('run_id') != run_id or record.get('root') != str(root):
            raise ValueError('Code relay journal has different run/worktree provenance')
        for name in record.get('paths', []):
            path = code_path(root, name)
            if path.exists():
                ignored = subprocess.run(['git', 'check-ignore', '-q', '--', name], cwd=root,
                                         capture_output=True, timeout=30)
                if ignored.returncode == 0:
                    raise ValueError(f'Ignored pending code cannot be silently omitted from recovery: {name}')
                if ignored.returncode not in (0, 1):
                    raise ValueError(f'Could not check ignored pending code: {name}')
        paths = sorted(dirty & set(record.get('paths', [])))
        if paths:
            by_step[journal.stem] = paths
    known = {p for paths in by_step.values() for p in paths}
    files = {}
    for name in sorted(dirty):
        path = code_path(root, name)
        if not path.exists():
            files[name] = {'deleted': True}
        else:
            content = path.read_bytes()
            files[name] = {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content),
                           'mode': '100755' if path.stat().st_mode & stat.S_IXUSR else '100644'}
    return {'files': files, 'steps': by_step, 'unowned': sorted(dirty - known)}


def require_quiet(sf, run_id: str) -> None:
    """Terminal state alone is insufficient if an earlier operation is draining."""
    with sf._lock:
        run = sf.get_run(run_id)
        n = sf._conn.execute('SELECT COUNT(*) FROM skillflow_active_ops WHERE run_id=?',
                             (run_id,)).fetchone()[0]
    if not run or run['status'] not in sf.TERMINAL_RUN_STATUSES or n:
        raise ValueError('Code recovery requires a terminal run with zero admitted operations')


def recovery_commit(root: Path, config_dir: Path, run_id: str, head: str,
                    manifest: dict, recovery_id: str) -> str:
    """Make exactly the approved dirty files reachable as an unvalidated commit."""
    if not manifest['files']:
        return head
    if manifest.get('unowned'):
        raise ValueError('Unowned dirty code requires explicit recovery: ' + ', '.join(manifest['unowned']))
    if not re.fullmatch(r'[A-Za-z0-9_-]+', recovery_id):
        raise ValueError('Invalid recovery identity')
    if git(root, 'rev-parse', 'HEAD').strip() != head or inventory(root, config_dir, run_id) != manifest:
        raise ValueError('Failed code changed since inventory was read')
    env = {k: v for k, v in os.environ.items()
           if k not in ('GIT_INDEX_FILE', 'GIT_DIR', 'GIT_WORK_TREE')}
    with tempfile.TemporaryDirectory(prefix='aitelier-code-relay-') as tmp:
        env['GIT_INDEX_FILE'] = str(Path(tmp) / 'index')

        def command(*args, data=None):
            result = subprocess.run(['git', *args], cwd=root, env=env, input=data,
                                    capture_output=True, timeout=60)
            if result.returncode:
                raise ValueError(result.stderr.decode(errors='replace').strip())
            return result.stdout.decode().strip()

        command('read-tree', head)
        for name, entry in manifest['files'].items():
            if entry.get('deleted'):
                command('update-index', '--force-remove', '--', name)
            else:
                data = code_path(root, name).read_bytes()
                if hashlib.sha256(data).hexdigest() != entry['sha256']:
                    raise ValueError(f'Failed code changed while being recovered: {name}')
                blob = command('hash-object', '-w', '--stdin', data=data)
                command('update-index', '--add', '--cacheinfo', entry['mode'], blob, name)
        tree = command('write-tree')
        commit = command('commit-tree', tree, '-p', head, '-m',
                         f'UNVALIDATED recovery of run {run_id} for {recovery_id}')
        if git(root, 'rev-parse', 'HEAD').strip() != head or inventory(root, config_dir, run_id) != manifest:
            raise ValueError('Failed worktree moved during recovery; no recovery ref was published')
        # Unique attempt-owned ref keeps the commit alive without advancing the
        # failed run's branch. Repeated same-attempt recovery is content-checked.
        ref = 'refs/aitelier/recovery/' + recovery_id
        old = subprocess.run(['git', 'rev-parse', '--verify', ref], cwd=root,
                             capture_output=True, text=True, timeout=60)
        if old.returncode == 0:
            existing = old.stdout.strip()
            if (command('rev-parse', existing + '^{tree}') != tree
                    or command('rev-parse', existing + '^') != head):
                raise ValueError('This recovery identity already names different code')
            return existing
        command('update-ref', ref, commit, '0' * 40)
        return commit
