"""Indexer coverage for normal repositories and run-owned linked worktrees."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, check=True, text=True, capture_output=True,
    )


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _run("git", "init", "-q", str(path))
    _run("git", "config", "user.email", "test@example.invalid", cwd=path)
    _run("git", "config", "user.name", "Test", cwd=path)
    (path / "tracked.txt").write_text("tracked\n")
    _run("git", "add", "tracked.txt", cwd=path)
    _run("git", "commit", "-qm", "seed", cwd=path)


def _stub_zg(bin_dir: Path) -> Path:
    log = bin_dir / "zg.log"
    zg = bin_dir / "zg"
    zg.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$ZG_TEST_LOG\"\n"
        "[ \"${ZG_TEST_FAIL:-0}\" = 1 ] && { echo forced failure >&2; exit 7; }\n"
        "[ \"$1\" = index ] && mkdir -p \"$2/.zvec-grep\"\n"
        "echo indexed\n"
    )
    zg.chmod(0o755)
    return log


def _index(projects: Path, worktrees: Path, bin_dir: Path, **extra: str):
    from core.semantic_index_control import index_project_once
    directory = bin_dir / 'control'
    directory.mkdir(exist_ok=True)
    env = {**os.environ, "ZG_TEST_LOG": str(bin_dir / 'zg.log'),
           "PATH": f"{bin_dir}:{os.environ['PATH']}", **extra}
    def execute(command, *, timeout):
        return subprocess.run(command, check=True, capture_output=True,
                              text=True, timeout=timeout, env=env)
    return index_project_once(directory, projects, worktrees, execute=execute)


def test_indexes_project_without_dirtying_git_and_preserves_linked_worktree(tmp_path):
    projects, worktrees = tmp_path / 'projects', tmp_path / 'worktrees'
    source = projects / 'source'; linked = worktrees / 'run-1'
    _git_repo(source); worktrees.mkdir()
    _run('git', 'worktree', 'add', '-qb', 'run-1', str(linked), cwd=source)
    outside = tmp_path / 'outside'; _git_repo(outside)
    (projects / 'redirect').symlink_to(outside, target_is_directory=True)
    bin_dir = tmp_path / 'bin'; bin_dir.mkdir(); log = _stub_zg(bin_dir)
    _index(projects, worktrees, bin_dir)
    calls = log.read_text().splitlines()
    assert [line.split()[1] for line in calls] == [str(source), str(source)]
    assert '--hidden --glob !**/.zvec-grep/**' in calls[0]
    assert _run('git', 'status', '--porcelain', cwd=source).stdout == ''
    assert _run('git', 'status', '--porcelain', cwd=linked).stdout == ''
    assert not (linked / '.zvec-grep').exists()
    assert not (outside / '.zvec-grep').exists()
    _index(projects, worktrees, bin_dir)
    assert len(log.read_text().splitlines()) == 2


def test_project_backlog_is_bounded_to_one_and_does_not_scan_run_history(tmp_path):
    projects, worktrees = tmp_path / 'projects', tmp_path / 'worktrees'
    for i in range(3): _git_repo(projects / f'project-{i}')
    _git_repo(worktrees / 'historic')
    bin_dir = tmp_path / 'bin'; bin_dir.mkdir(); log = _stub_zg(bin_dir)
    for i in range(3):
        _index(projects, worktrees, bin_dir)
        assert len(log.read_text().splitlines()) == (i + 1) * 2
    assert not (worktrees / 'historic/.zvec-grep').exists()


def test_failed_project_index_is_visible_and_repaired_without_success_marker(tmp_path):
    import json
    import pytest
    projects, worktrees = tmp_path / 'projects', tmp_path / 'worktrees'
    root = projects / 'failure'; _git_repo(root)
    bin_dir = tmp_path / 'bin'; bin_dir.mkdir(); _stub_zg(bin_dir)
    with pytest.raises(subprocess.CalledProcessError):
        _index(projects, worktrees, bin_dir, ZG_TEST_FAIL='1')
    marker = bin_dir / 'control/project-owner.json'
    assert json.loads(marker.read_text())['status'] == 'error'
    assert not (root / '.zvec-grep').exists()
    _index(projects, worktrees, bin_dir)
    assert json.loads(marker.read_text())['status'] == 'idle'
