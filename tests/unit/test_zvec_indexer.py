"""Indexer coverage for normal repositories and run-owned linked worktrees."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

ENTRYPOINT = Path(__file__).parents[2] / "docker" / "zvec-grep-entrypoint.sh"


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
        "printf '%s\\n' \"$2\" >> \"$ZG_TEST_LOG\"\n"
        "[ \"${ZG_TEST_FAIL:-0}\" = 1 ] && { echo forced failure >&2; exit 7; }\n"
        "mkdir -p \"$2/.zvec-grep\"\n"
        "echo indexed\n"
    )
    zg.chmod(0o755)
    return log


def _index(projects: Path, worktrees: Path, bin_dir: Path, **extra: str):
    env = os.environ.copy()
    env.update({
        "AITELIER_PROJECTS_DIR": str(projects),
        "AITELIER_WORKTREES_DIR": str(worktrees),
        "AITELIER_ZG_INDEXER_LIB_ONLY": "1",
        "ZG_TEST_LOG": str(bin_dir / "zg.log"),
        "PATH": f"{bin_dir}:{env['PATH']}",
        **extra,
    })
    return subprocess.run(
        ["sh", "-c", f'. "{ENTRYPOINT}"; index_new_repos'],
        text=True, capture_output=True, env=env, check=True,
    )


def test_indexes_project_and_linked_worktree_without_dirtying_git(tmp_path):
    projects = tmp_path / "projects"
    worktrees = tmp_path / "worktrees"
    source = projects / "source"
    linked = worktrees / "run-1"
    _git_repo(source)
    worktrees.mkdir()
    _run("git", "worktree", "add", "-qb", "run-1", str(linked), cwd=source)
    (worktrees / "not-a-repo").mkdir()
    outside = tmp_path / "outside"
    _git_repo(outside)
    (worktrees / "symlink-out").symlink_to(outside, target_is_directory=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = _stub_zg(bin_dir)
    result = _index(projects, worktrees, bin_dir)

    assert result.stderr == ""
    assert log.read_text().splitlines() == [str(source), str(linked)]
    assert (source / ".zvec-grep").is_dir()
    assert (linked / ".zvec-grep").is_dir()
    assert not (outside / ".zvec-grep").exists()
    assert _run("git", "status", "--porcelain", cwd=source).stdout == ""
    assert _run("git", "status", "--porcelain", cwd=linked).stdout == ""

    # Existing indexes are not launched twice.
    _index(projects, worktrees, bin_dir)
    assert log.read_text().splitlines() == [str(source), str(linked)]

    # Lifecycle ownership stays with git/run-isolation: the indexer issues no
    # cross-worktree drop/delete, and Git can remove an indexed clean worktree.
    _run("git", "worktree", "remove", str(linked), cwd=source)
    assert not linked.exists()
    _index(projects, worktrees, bin_dir)
    assert source.exists()
    assert log.read_text().splitlines() == [str(source), str(linked)]


def test_failed_index_is_reported_and_retried_without_success_marker(tmp_path):
    projects = tmp_path / "projects"
    worktrees = tmp_path / "worktrees"
    repo = worktrees / "run-fail"
    _git_repo(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = _stub_zg(bin_dir)

    first = _index(projects, worktrees, bin_dir, ZG_TEST_FAIL="1")
    assert "forced failure" in first.stderr
    assert "index failed; will retry" in first.stderr
    assert not (repo / ".zvec-grep").exists()

    second = _index(projects, worktrees, bin_dir, ZG_TEST_FAIL="1")
    assert log.read_text().splitlines() == [str(repo), str(repo)]
    assert "index failed; will retry" in second.stderr
