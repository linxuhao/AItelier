"""Repository commands see their run's real HEAD through private Git metadata.

The live failure: a linked worktree's `.git` points into the source repo's
common dir, which the isolated container never mounts, so `git rev-parse HEAD`
exited 128. `private_git` builds a self-contained copy holding HEAD only; these
tests reproduce the container's overlay with a view directory (no Docker here)
and run the actual git commands a test suite would run.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from core import repository_executor as executor, repository_host_launcher as host

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent",
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"}
SAFE = ["-c", "core.fsmonitor=false", "-c", "filter.evil.clean=cat",
        "-c", "core.hooksPath=/dev/null"]


def git(cwd, *args):
    return subprocess.run(["git", *SAFE, *args], cwd=cwd, env=ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(repo, message, **files):
    for name, text in files.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Source Owner", "-c", "user.email=owner@private.invalid",
        "commit", "-qm", message)


def hostile(repo: Path, markers: Path) -> None:
    """Source config/hooks that would run a program if provisioning ran git there."""
    markers.mkdir()
    for name in ("fsmonitor", "filter", "hook"):
        script = markers / f"{name}.sh"
        script.write_text(f"#!/bin/sh\ntouch {markers}/{name}-RAN\ncat\n")
        script.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(markers / "fsmonitor.sh"))
    git(repo, "config", "filter.evil.clean", str(markers / "filter.sh"))
    git(repo, "config", "user.email", "config-identity@private.invalid")
    git(repo, "remote", "add", "origin", "https://user:SECRET-TOKEN@example.invalid/x.git")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for hook in ("post-checkout", "reference-transaction", "post-index-change"):
        shutil.copy(markers / "hook.sh", hooks / hook)


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "projects" / "p"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "c1", **{"m.py": "print(1)\n", "f.txt": "v1\n"})
    commit(repo, "c2", **{"m.py": "print(2)\n"})
    git(repo, "branch", "other-ref")
    git(repo, "tag", "private-tag")
    return repo


@pytest.fixture
def linked(source, tmp_path):
    wt = tmp_path / "worktrees" / "run-1"
    git(source, "worktree", "add", "-q", "-b", "aitelier/run-1", str(wt), "HEAD")
    commit(wt, "c3 run", **{"m.py": "print(3)\n"})
    git(source, "pack-refs", "--all")  # HEAD's branch now lives in packed-refs
    hostile(source, tmp_path / "markers")
    (wt / ".gitattributes").write_text("*.txt filter=evil\n")
    (wt / "f.txt").write_text("dirty\n")
    return wt


def view_of(repo: Path, mounts: list[str], tmp_path: Path) -> Path:
    """What the container sees: the repo with `mounts` applied (paths remapped)."""
    view = tmp_path / "view"
    shutil.copytree(repo, view, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    pairs = dict(m.rsplit(":", 1)[0].split(":", 1)[::-1] for m in mounts[1::2])
    target = pairs.get(str(repo / ".git"))
    if target is None:
        return view
    if Path(target).is_dir():
        (view / ".git").symlink_to(target)
    else:
        text = Path(target).read_text()
        private = pairs[executor.GIT_CONTAINER_DIR]
        (view / ".git").write_text(text.replace(executor.GIT_CONTAINER_DIR, private))
    return view


def provision(repo, tmp_path, common=None):
    dest = tmp_path / "dest"
    dest.mkdir()
    return executor.private_git(repo, dest, common)


def test_linked_worktree_head_is_the_actual_run_head(linked, source, tmp_path):
    expected = git(linked, "rev-parse", "HEAD")
    mounts, info = provision(linked, tmp_path, source / ".git")
    assert info["provided"] is True and info["head"] == expected
    view = view_of(linked, mounts, tmp_path)
    assert git(view, "rev-parse", "HEAD") == expected


def test_linked_worktree_reads_head_file_ancestry_and_diff(linked, source, tmp_path):
    mounts, _ = provision(linked, tmp_path, source / ".git")
    view = view_of(linked, mounts, tmp_path)
    assert git(view, "show", "HEAD:m.py") == "print(3)"
    assert git(view, "log", "--format=%s") == "c3 run\nc2\nc1"
    assert git(view, "diff", "--name-only", "HEAD~1", "HEAD") == "m.py"
    assert git(view, "rev-parse", "--abbrev-ref", "HEAD") == "aitelier/run-1"


def test_linked_worktree_status_is_the_current_tree(linked, source, tmp_path):
    mounts, _ = provision(linked, tmp_path, source / ".git")
    view = view_of(linked, mounts, tmp_path)
    status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=view, env=ENV, check=True,
                            capture_output=True, text=True).stdout.splitlines()
    assert sorted(status) == sorted([" M f.txt", "?? .gitattributes"])


def test_private_metadata_has_no_source_config_refs_or_remotes(linked, source, tmp_path):
    mounts, _ = provision(linked, tmp_path, source / ".git")
    view = view_of(linked, mounts, tmp_path)
    assert git(view, "for-each-ref", "--format=%(refname)") == "refs/heads/aitelier/run-1"
    assert git(view, "remote") == ""
    config = git(view, "config", "--list", "--local")
    assert "fsmonitor" not in config and "filter" not in config and "user." not in config
    private = Path(dict(m.rsplit(":", 1)[0].split(":", 1)[::-1] for m in mounts[1::2])
                   [executor.GIT_CONTAINER_DIR])
    assert not (private / "hooks").exists() and not (private / "commondir").exists()
    raw = b"".join(p.read_bytes() for p in private.rglob("*") if p.is_file())
    assert b"SECRET-TOKEN" not in raw
    assert b"config-identity" not in raw


def test_provisioning_never_runs_source_programs(linked, source, tmp_path):
    provision(linked, tmp_path, source / ".git")
    assert sorted(p.name for p in (tmp_path / "markers").glob("*-RAN")) == []


def test_direct_repository_git_dir_is_masked_by_private_copy(source, tmp_path):
    hostile(source, tmp_path / "markers")
    expected = git(source, "rev-parse", "HEAD")
    mounts, info = provision(source, tmp_path, source / ".git")
    assert mounts[1].endswith(f":{source / '.git'}:ro") and len(mounts) == 2
    view = view_of(source, mounts, tmp_path)
    assert git(view, "rev-parse", "HEAD") == expected == info["head"]
    assert git(view, "for-each-ref", "--format=%(refname)") == "refs/heads/main"
    assert git(view, "remote") == ""
    assert sorted(p.name for p in (tmp_path / "markers").glob("*-RAN")) == []


def test_direct_repository_failure_still_masks_source_git(tmp_path):
    repo = tmp_path / "projects" / "empty"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", "https://user:SECRET-TOKEN@example.invalid/x.git")
    mounts, info = provision(repo, tmp_path)
    assert info["provided"] is False and "does not resolve" in info["reason"]
    private = Path(mounts[1].split(":")[0])
    assert mounts[1].endswith(f":{repo / '.git'}:ro") and list(private.iterdir()) == []


def test_repository_without_git_gets_no_metadata_and_no_enclosing_repo(source, tmp_path):
    inner = source / "subproject"
    inner.mkdir()
    (inner / "m.py").write_text("print(0)\n")
    mounts, info = provision(inner, tmp_path)
    assert mounts == [] and info == {"provided": False, "reason": "repository has no .git"}


def test_forged_pointer_to_another_worktree_is_refused(linked, source, tmp_path):
    other = tmp_path / "worktrees" / "foreign"
    git(source, "worktree", "add", "-q", "-b", "foreign", str(other), "HEAD")
    foreign = (other / ".git").read_text()
    (linked / ".git").write_text(foreign)
    mounts, info = provision(linked, tmp_path)
    assert mounts == [] and info["provided"] is False and "own" in info["reason"]


def test_common_dir_must_be_the_run_source(linked, tmp_path):
    elsewhere = tmp_path / "projects" / "q"
    elsewhere.mkdir()
    git(elsewhere, "init", "-q")
    mounts, info = provision(linked, tmp_path, elsewhere / ".git")
    assert mounts == [] and "source repository" in info["reason"]


def test_history_is_bounded_to_depth(tmp_path):
    repo = tmp_path / "projects" / "deep"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for i in range(executor.GIT_META_DEPTH + 5):
        commit(repo, f"d{i}", **{"n.txt": f"{i}\n"})
    mounts, info = provision(repo, tmp_path)
    view = view_of(repo, mounts, tmp_path)
    assert info["shallow"] is True
    assert len(git(view, "log", "--format=%H").splitlines()) == executor.GIT_META_DEPTH


def test_detached_head_stays_detached(source, tmp_path):
    git(source, "checkout", "-q", "--detach", "HEAD~1")
    expected = git(source, "rev-parse", "HEAD")
    mounts, info = provision(source, tmp_path)
    view = view_of(source, mounts, tmp_path)
    assert info["branch"] == "" and git(view, "rev-parse", "HEAD") == expected
    assert git(view, "for-each-ref") == ""


def _launcher(tmp_path, repo, mode, source_repo):
    home = tmp_path / "home"
    home.mkdir()
    entry = tmp_path / "entry.py"
    entry.write_text("# entry\n")
    db = home / "aitelier.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE run_isolation(run_id TEXT,mode TEXT,worktree_path TEXT,"
                     "source_repo TEXT,project_id TEXT)")
        conn.execute("INSERT INTO run_isolation VALUES(?,?,?,?,?)",
                     ("run-1", mode, str(repo) if mode == "worktree" else "", str(source_repo), "p"))
    cfg = {"allowed_roots": [str(tmp_path / "projects"), str(tmp_path / "worktrees")],
           "aitelier_home": str(home), "state_db": str(db), "executor_entry": str(entry),
           "test_image": "sha256:" + "a" * 64, "allowed_uids": [os.getuid()]}
    request = {"op": "launch", "run_id": "run-1", "repo": str(repo),
               "args": ["python3", "-m", "pytest", "-q"], "timeout": 5}
    return cfg, request


def test_launcher_mounts_private_git_and_removes_it(linked, source, tmp_path, monkeypatch):
    cfg, request = _launcher(tmp_path, linked, "worktree", source)
    seen = {}

    def run(cfg_, fields, conn=None):
        monkeypatch.setattr(host.shutil, "which", lambda _: "/controlled/docker")
        seen["argv"] = host.docker_command(cfg_, fields, "aitelier-cpu-x")
        seen["dir"] = Path(fields["git_mounts"][1].split(":")[0]).parent
        seen["existed"] = seen["dir"].is_dir()
        return {"returncode": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(host, "_run_docker", run)
    response = host.handle_request(cfg, json.dumps(request).encode())
    assert response["ok"] and response["git"]["head"] == git(linked, "rev-parse", "HEAD")
    argv = seen["argv"]
    assert f"{seen['dir'] / 'pointer'}:{linked / '.git'}:ro" in argv
    assert f"{seen['dir'] / 'git'}:{executor.GIT_CONTAINER_DIR}:ro" in argv
    assert seen["existed"] and not seen["dir"].exists()


def test_host_reply_git_reaches_the_caller(monkeypatch):
    replies = {"ok": True, "result": {"returncode": 0, "stdout": "", "stderr": ""},
               "git": {"provided": False, "reason": "repository has no .git"}}

    class Sock:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, t): pass
        def connect(self, p): pass
        def sendall(self, b): pass
        def recv(self, n, _sent=[]):
            if _sent:
                return b""
            _sent.append(1)
            return json.dumps(replies).encode() + b"\n"
    import socket
    monkeypatch.setattr(socket, "socket", Sock)
    done = executor._launch_via_host("/s.sock", Path("/p"), ["python3", "-m", "pytest"], 5)
    assert done.git == replies["git"]


def test_direct_adapter_mounts_private_git_and_removes_it(source, tmp_path, monkeypatch):
    from core import datadir
    monkeypatch.setattr(datadir, "aitelier_home", lambda: tmp_path / "home")
    seen = {}

    class Proc:
        returncode = 0
        def communicate(self, timeout):
            return json.dumps({"returncode": 0, "stdout": "", "stderr": ""}), ""
        def poll(self): return 0
    monkeypatch.setattr(executor.shutil, "which", lambda _: "/trusted/docker")
    real_popen = subprocess.Popen
    def popen(command, **kwargs):
        if command[0] != "/trusted/docker":
            return real_popen(command, **kwargs)  # provisioning's own git
        target = next(v for v in command if v.endswith(f":{source / '.git'}:ro"))
        seen["private"] = Path(target.split(":")[0])
        seen["head"] = (seen["private"] / "HEAD").read_text()
        return Proc()
    monkeypatch.setattr(executor.subprocess, "Popen", popen)
    real_run = subprocess.run
    monkeypatch.setattr(executor.subprocess, "run", lambda c, **k: (
        subprocess.CompletedProcess(c, 0) if c[1:3] == ["rm", "-f"] else real_run(c, **k)))
    done = executor._execute(source, ["python3", "-m", "pytest"], 5)
    assert done.git["head"] == git(source, "rev-parse", "HEAD")
    assert seen["head"] == "ref: refs/heads/main\n" and not seen["private"].exists()
