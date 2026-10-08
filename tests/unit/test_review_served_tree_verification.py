"""Direct tests: exact-candidate review ingress verifies SERVED bytes/modes.

The admission check must compare what the snapshot actually serves on disk to
the trusted candidate tree, independent of assume-unchanged, skip-worktree,
the stat cache and core.filemode=false — and must not touch the index in any
way (no refresh, no reset, no stage, no flag clearing).
"""
import os
import subprocess

import pytest

from core.run_isolation import verify_served_tree
from skillflow.exceptions import IsolationUnavailable


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture()
def served_candidate(tmp_path):
    """A repo with a commit B and a detached worktree serving exactly B."""
    repo = tmp_path / "producer"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "plain.txt").write_text("plain\n")
    (repo / "run.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(repo / "run.sh", 0o755)
    os.symlink("plain.txt", repo / "link")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "candidate B")
    commit = _git(repo, "rev-parse", "HEAD")
    snap = tmp_path / "snapshot"
    _git(repo, "worktree", "add", "--detach", str(snap), commit)
    return repo, snap, commit


def test_fresh_snapshot_serves_the_candidate(served_candidate):
    _, snap, commit = served_candidate
    report = {}
    out = verify_served_tree(snap, commit, _report=report.update)
    assert out["problems"] == []
    assert out["tracked_entries"] == 3
    assert report["commit_sha"] == commit


def test_tampered_bytes_are_refused_not_repaired(served_candidate):
    _, snap, commit = served_candidate
    (snap / "plain.txt").write_text("tampered\n")
    with pytest.raises(IsolationUnavailable) as exc:
        verify_served_tree(snap, commit)
    assert "plain.txt" in str(exc.value)


def test_served_mode_mismatch_is_refused_independently_of_filemode(served_candidate):
    _, snap, commit = served_candidate
    # Even with core.filemode=false (which would hide this from git's own
    # status) the served mode is compared against the tree mode from disk.
    _git(snap, "config", "core.filemode", "false")
    os.chmod(snap / "run.sh", 0o644)
    with pytest.raises(IsolationUnavailable) as exc:
        verify_served_tree(snap, commit)
    assert "run.sh" in str(exc.value)


def test_missing_tracked_file_is_refused(served_candidate):
    _, snap, commit = served_candidate
    (snap / "plain.txt").unlink()
    with pytest.raises(IsolationUnavailable):
        verify_served_tree(snap, commit)


def test_symlink_served_as_regular_file_is_refused(served_candidate):
    _, snap, commit = served_candidate
    (snap / "link").unlink()
    (snap / "link").write_text("plain.txt")
    with pytest.raises(IsolationUnavailable):
        verify_served_tree(snap, commit)


def test_non_commit_sha_is_refused(served_candidate):
    _, snap, _ = served_candidate
    with pytest.raises(IsolationUnavailable):
        verify_served_tree(snap, "not-a-sha")


def test_missing_snapshot_directory_is_refused(tmp_path):
    with pytest.raises(IsolationUnavailable):
        verify_served_tree(tmp_path / "absent", "a" * 40)


def test_verification_is_readonly_by_source():
    """No index refresh, reset, stage or flag clearing in the admission path."""
    import inspect
    import core.run_isolation as ri
    src = inspect.getsource(ri.verify_served_tree) + inspect.getsource(ri._blob_sha1)
    # Drop the docstring, which names the forbidden commands in prose.
    parts = src.split('"""')
    src = "".join(parts[i] for i in range(len(parts)) if i % 2 == 0)
    for forbidden in ("update-index", "read-tree", "reset", "--refresh",
                      "skip-worktree", "assume-unchanged"):
        assert forbidden not in src
