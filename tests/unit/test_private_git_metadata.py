"""Repository commands see their run's real HEAD through private Git metadata.

The live failure: a linked worktree's `.git` points into the source repo's
common dir, which the isolated container never mounts, so `git rev-parse HEAD`
exited 128. `private_git` builds a self-contained copy of HEAD and its whole
history; these tests reproduce the container's overlay with a view directory
and run the actual git commands a test suite would run.

There is no Docker here: a `docker` shim runs the preparation container's own
command (the real `PREP_SCRIPT` and git) directly, at the same paths the
container mounts. The container's bounds themselves (no network, rlimit,
memory, the source object directory as its only source mount) are measured
with real Docker by the operator controls, not here.
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
        # Uncovered: the container sees the repository's own `.git` as it is.
        dot = repo / ".git"
        if dot.is_symlink():
            (view / ".git").symlink_to(os.readlink(dot))
        elif dot.is_file():
            shutil.copy(dot, view / ".git")
        elif dot.is_dir():
            (view / ".git").symlink_to(dot)
        return view
    if Path(target).is_dir():
        (view / ".git").symlink_to(target)
    else:
        text = Path(target).read_text()
        # Not mounted when no metadata could be built: the pointer leads nowhere.
        private = pairs.get(executor.GIT_CONTAINER_DIR, str(tmp_path / "unmounted"))
        (view / ".git").write_text(text.replace(executor.GIT_CONTAINER_DIR, private))
    return view


SHIM = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log = os.environ["DOCKER_SHIM_LOG"]
with open(log, "a") as stream:
    stream.write(json.dumps(args) + "\\n")
store = log + ".containers"
if args[:2] == ["rm", "-f"]:
    sys.exit(0)
if args[0] == "create":
    os.makedirs(store, exist_ok=True)
    at = args.index("--entrypoint")
    with open(os.path.join(store, args[args.index("--name") + 1]), "w") as stream:
        json.dump([args[at + 1]] + args[at + 3:], stream)
    sys.exit(0)
if args[:2] == ["start", "-a"]:
    with open(os.path.join(store, args[2])) as stream:
        command = json.load(stream)
    os.execvp(command[0], command)
sys.exit(2)
"""


@pytest.fixture
def docker(tmp_path, monkeypatch):
    shim = tmp_path / "bin" / "docker"
    shim.parent.mkdir()
    shim.write_text(SHIM)
    shim.chmod(0o755)
    monkeypatch.setenv("DOCKER_SHIM_LOG", str(tmp_path / "docker.log"))
    return str(shim)


def docker_calls(tmp_path):
    log = tmp_path / "docker.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def provision(repo, tmp_path, common=None):
    dest = tmp_path / "dest"
    dest.mkdir()
    return executor.private_git(repo, dest, common, image="sha256:" + "b" * 64,
                                docker=str(tmp_path / "bin" / "docker"))


def test_linked_worktree_head_is_the_actual_run_head(linked, source, tmp_path, docker):
    expected = git(linked, "rev-parse", "HEAD")
    mounts, info = provision(linked, tmp_path, source / ".git")
    assert info["provided"] is True and info["head"] == expected
    view = view_of(linked, mounts, tmp_path)
    assert git(view, "rev-parse", "HEAD") == expected


def test_linked_worktree_reads_head_file_ancestry_and_diff(linked, source, tmp_path, docker):
    mounts, _ = provision(linked, tmp_path, source / ".git")
    view = view_of(linked, mounts, tmp_path)
    assert git(view, "show", "HEAD:m.py") == "print(3)"
    assert git(view, "log", "--format=%s") == "c3 run\nc2\nc1"
    assert git(view, "diff", "--name-only", "HEAD~1", "HEAD") == "m.py"
    assert git(view, "rev-parse", "--abbrev-ref", "HEAD") == "aitelier/run-1"


def test_linked_worktree_status_is_the_current_tree(linked, source, tmp_path, docker):
    mounts, _ = provision(linked, tmp_path, source / ".git")
    view = view_of(linked, mounts, tmp_path)
    status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=view, env=ENV, check=True,
                            capture_output=True, text=True).stdout.splitlines()
    assert sorted(status) == sorted([" M f.txt", "?? .gitattributes"])


def test_private_metadata_has_no_source_config_refs_or_remotes(linked, source, tmp_path, docker):
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


def test_provisioning_never_runs_source_programs(linked, source, tmp_path, docker):
    provision(linked, tmp_path, source / ".git")
    assert sorted(p.name for p in (tmp_path / "markers").glob("*-RAN")) == []


def test_direct_repository_git_dir_is_masked_by_private_copy(source, tmp_path, docker):
    hostile(source, tmp_path / "markers")
    expected = git(source, "rev-parse", "HEAD")
    mounts, info = provision(source, tmp_path, source / ".git")
    assert mounts[1].endswith(f":{source / '.git'}:ro") and len(mounts) == 2
    view = view_of(source, mounts, tmp_path)
    assert git(view, "rev-parse", "HEAD") == expected == info["head"]
    assert git(view, "for-each-ref", "--format=%(refname)") == "refs/heads/main"
    assert git(view, "remote") == ""
    assert sorted(p.name for p in (tmp_path / "markers").glob("*-RAN")) == []


def test_direct_repository_failure_still_masks_source_git(tmp_path, docker):
    repo = tmp_path / "projects" / "empty"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", "https://user:SECRET-TOKEN@example.invalid/x.git")
    mounts, info = provision(repo, tmp_path)
    assert info["provided"] is False and "does not resolve" in info["reason"]
    private = Path(mounts[1].split(":")[0])
    assert mounts[1].endswith(f":{repo / '.git'}:ro") and list(private.iterdir()) == []


def test_repository_without_git_gets_no_metadata_and_no_enclosing_repo(source, tmp_path, docker):
    inner = source / "subproject"
    inner.mkdir()
    (inner / "m.py").write_text("print(0)\n")
    mounts, info = provision(inner, tmp_path)
    assert mounts == [] and info == {"provided": False, "reason": "repository has no .git"}


def test_forged_pointer_to_another_worktree_is_refused(linked, source, tmp_path, docker):
    other = tmp_path / "worktrees" / "foreign"
    git(source, "worktree", "add", "-q", "-b", "foreign", str(other), "HEAD")
    foreign = (other / ".git").read_text()
    (linked / ".git").write_text(foreign)
    mounts, info = provision(linked, tmp_path)
    assert info["provided"] is False and "own" in info["reason"]
    assert mounts == ["-v", f"{tmp_path / 'dest' / 'pointer'}:{linked / '.git'}:ro"]
    view = view_of(linked, mounts, tmp_path)
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=view, env=ENV,
                          capture_output=True).returncode == 128


def test_common_dir_must_be_the_run_source(linked, tmp_path, docker):
    elsewhere = tmp_path / "projects" / "q"
    elsewhere.mkdir()
    git(elsewhere, "init", "-q")
    mounts, info = provision(linked, tmp_path, elsewhere / ".git")
    assert "source repository" in info["reason"]
    assert mounts == ["-v", f"{tmp_path / 'dest' / 'pointer'}:{linked / '.git'}:ro"]


def moved_metadata(tmp_path):
    """The independent review's layout: `.git` moved inside the checkout, with a secret."""
    repo = tmp_path / "projects" / "moved"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "first", **{"value": "one\n"})
    git(repo, "remote", "add", "origin", "https://user:MOVED-TOKEN@example.invalid/p.git")
    git(repo, "branch", "foreign-private-ref")
    (repo / ".git").rename(repo / "owned-metadata")
    return repo


def test_symlinked_git_entry_refuses_the_command(tmp_path, docker):
    repo = moved_metadata(tmp_path)
    (repo / ".git").symlink_to("owned-metadata", target_is_directory=True)
    with pytest.raises(executor.GitEntryRefused, match="neither a directory nor a regular file"):
        provision(repo, tmp_path)
    assert docker_calls(tmp_path) == []


def test_dangling_git_link_is_present_and_refused(tmp_path, docker):
    repo = tmp_path / "projects" / "dangling"
    repo.mkdir(parents=True)
    (repo / ".git").symlink_to(tmp_path / "does-not-exist")
    with pytest.raises(executor.GitEntryRefused):
        provision(repo, tmp_path)


def test_special_git_entry_is_refused_without_blocking(tmp_path, docker):
    repo = tmp_path / "projects" / "fifo"
    repo.mkdir(parents=True)
    os.mkfifo(repo / ".git")  # opening it for reading would block
    with pytest.raises(executor.GitEntryRefused, match="prw"):
        provision(repo, tmp_path)


def test_in_tree_forged_pointer_never_reaches_the_moved_metadata(tmp_path, docker):
    repo = moved_metadata(tmp_path)
    (repo / ".git").write_text("gitdir: owned-metadata\n")
    mounts, info = provision(repo, tmp_path)
    assert info["provided"] is False
    view = view_of(repo, mounts, tmp_path)
    probe = subprocess.run(["git", "remote", "-v"], cwd=view, env=ENV, capture_output=True, text=True)
    assert probe.returncode == 128 and "MOVED-TOKEN" not in probe.stdout + probe.stderr


def pinned_history(tmp_path, commits=61):
    """A small ordinary repository whose checked-in pin names its first commit."""
    repo = tmp_path / "projects" / "pinned"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for n in range(commits):
        if n == 1:
            (repo / "pin.txt").write_text(git(repo, "rev-parse", "HEAD") + "\n")
        commit(repo, f"revision {n}", **{"version.txt": f"{n}\n"})
    (repo / "version.txt").write_text("dirty current tree\n")
    return repo


def test_checked_in_pin_to_the_first_commit_resolves(tmp_path, docker):
    repo = pinned_history(tmp_path)
    pin = (repo / "pin.txt").read_text().strip()
    mounts, _ = provision(repo, tmp_path)
    view = view_of(repo, mounts, tmp_path)
    assert subprocess.run(["git", "merge-base", "--is-ancestor", pin, "HEAD"],
                          cwd=view, env=ENV).returncode == 0
    assert git(view, "merge-base", pin, "HEAD") == pin
    assert git(view, "show", f"{pin}:version.txt") == "0"
    assert "version.txt" in git(view, "diff", "--name-only", pin, "HEAD").splitlines()


def test_whole_head_history_is_present(tmp_path, docker):
    repo = pinned_history(tmp_path)
    mounts, _ = provision(repo, tmp_path)
    view = view_of(repo, mounts, tmp_path)
    assert git(view, "rev-list", "--count", "HEAD") == git(repo, "rev-list", "--count", "HEAD")
    assert git(view, "rev-parse", "HEAD~55") == git(repo, "rev-parse", "HEAD~55")
    assert not (Path(mounts[1].split(":")[0]) / "shallow").exists()


def test_history_over_the_budget_is_refused_before_transfer(tmp_path, docker, monkeypatch):
    repo = tmp_path / "projects" / "large"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "blob.bin").write_bytes(os.urandom(64 << 10))
    commit(repo, "large")
    monkeypatch.setattr(executor, "GIT_META_MAX_BYTES", 16 << 10)
    mounts, info = provision(repo, tmp_path)
    assert info["provided"] is False and "(budget 16384)" in info["reason"]
    assert list(Path(mounts[1].split(":")[0]).iterdir()) == []  # masked, nothing fetched


def test_preparation_container_is_bounded(source, tmp_path, docker):
    provision(source, tmp_path)
    run = next(c for c in docker_calls(tmp_path) if c[0] == "create")
    for flag, value in [("--network", "none"), ("--cpus", "2"), ("--memory", "2g"),
                        ("--pids-limit", "512"), ("--cap-drop", "ALL"),
                        ("--ulimit", f"fsize={executor.GIT_META_MAX_BYTES}:"
                                     f"{executor.GIT_META_MAX_BYTES}")]:
        assert run[run.index(flag) + 1] == value
    assert "--read-only" in run and "--rm" in run
    objects = source / ".git" / "objects"
    volumes = [run[i + 1] for i, a in enumerate(run) if a == "-v"]
    assert volumes == [f"{objects}:{objects}:ro", f"{tmp_path / 'dest'}:{tmp_path / 'dest'}:rw"]


def test_preparation_container_is_removed_by_name(source, tmp_path, docker):
    provision(source, tmp_path)
    calls = docker_calls(tmp_path)
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[0][0] == "create" and calls[1:] == [["start", "-a", name], ["rm", "-f", name]]


def test_failed_create_is_still_removed_by_name(tmp_path, docker):
    with pytest.raises(RuntimeError, match="not created"):
        executor._run_prep([docker, "unknown-subcommand"], docker, "prep-z")
    assert docker_calls(tmp_path)[-1] == ["rm", "-f", "prep-z"]


def test_preparation_deadline_stops_and_removes_it(tmp_path, docker, monkeypatch):
    import time
    monkeypatch.setattr(executor, "GIT_PREP_DEADLINE", 0.3)
    marker = tmp_path / "pid"
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="deadline"):
        executor._run_prep([docker, "create", "--name", "prep-x", "--entrypoint", "sh", "image",
                            "-c", f"echo $$ > {marker}; exec sleep 30"], docker, "prep-x")
    assert time.monotonic() - started < 3
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
    assert docker_calls(tmp_path)[1:] == [["start", "-a", "prep-x"], ["rm", "-f", "prep-x"]]


def test_client_disconnect_cancels_preparation(tmp_path, docker):
    import socket
    import time
    client, server = socket.socketpair()
    client.close()
    started = time.monotonic()
    with pytest.raises(executor.PreparationCancelled):
        executor._run_prep([docker, "create", "--name", "prep-y", "--entrypoint", "sleep", "image",
                            "30"], docker, "prep-y", server)
    server.close()
    assert time.monotonic() - started < 3
    assert docker_calls(tmp_path)[1:] == [["start", "-a", "prep-y"], ["rm", "-f", "prep-y"]]


def test_detached_head_stays_detached(source, tmp_path, docker):
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


def test_launcher_mounts_private_git_and_removes_it(linked, source, tmp_path, monkeypatch, docker):
    cfg, request = _launcher(tmp_path, linked, "worktree", source)
    seen = {}

    monkeypatch.setattr(host.shutil, "which", lambda _: docker)

    def run(cfg_, fields, conn=None):
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


def test_launcher_cancelled_preparation_starts_no_test_container(linked, source, tmp_path,
                                                                  monkeypatch, docker):
    cfg, request = _launcher(tmp_path, linked, "worktree", source)
    monkeypatch.setattr(host.shutil, "which", lambda _: docker)

    def cancelled(*args, **kwargs):
        raise executor.PreparationCancelled("client disconnected during Git metadata preparation")
    monkeypatch.setattr(executor, "_run_prep", cancelled)
    monkeypatch.setattr(host, "_run_docker", lambda *a, **k: pytest.fail("test container started"))
    response = host.handle_request(cfg, json.dumps(request).encode())
    assert response == {"ok": False, "error": "client disconnected during Git metadata preparation"}
    assert list((Path(cfg["aitelier_home"]) / "cpu-git").iterdir()) == []


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


def _no_test_container(monkeypatch):
    real_popen = subprocess.Popen
    def popen(command, **kwargs):
        if "python3" in command:
            pytest.fail("a repository command was launched")
        return real_popen(command, **kwargs)
    monkeypatch.setattr(executor.subprocess, "Popen", popen)


def test_direct_adapter_refuses_an_uncoverable_git_entry(tmp_path, monkeypatch, docker):
    from core import datadir
    monkeypatch.setattr(datadir, "aitelier_home", lambda: tmp_path / "home")
    monkeypatch.setattr(executor.shutil, "which", lambda _: docker)
    _no_test_container(monkeypatch)
    repo = moved_metadata(tmp_path)
    (repo / ".git").symlink_to("owned-metadata", target_is_directory=True)
    with pytest.raises(executor.IsolationUnavailable, match="refused"):
        executor._execute(repo, ["python3", "-m", "pytest"], 5)
    assert list((tmp_path / "home" / "cpu-git").iterdir()) == []


def test_launcher_refuses_an_uncoverable_git_entry(tmp_path, monkeypatch, docker):
    repo = moved_metadata(tmp_path)
    (repo / ".git").symlink_to("owned-metadata", target_is_directory=True)
    cfg, request = _launcher(tmp_path, repo, "direct", repo)
    monkeypatch.setattr(host.shutil, "which", lambda _: docker)
    monkeypatch.setattr(host, "_run_docker", lambda *a, **k: pytest.fail("test container started"))
    response = host.handle_request(cfg, json.dumps(request).encode())
    assert response["ok"] is False and "refused" in response["error"]
    assert list((Path(cfg["aitelier_home"]) / "cpu-git").iterdir()) == []


def test_direct_adapter_still_runs_a_repository_without_git(tmp_path, monkeypatch, docker):
    from core import datadir
    monkeypatch.setattr(datadir, "aitelier_home", lambda: tmp_path / "home")
    monkeypatch.setattr(executor.shutil, "which", lambda _: docker)
    repo = tmp_path / "projects" / "plain"
    repo.mkdir(parents=True)
    launched = []

    class Proc:
        returncode = 0
        def communicate(self, timeout):
            return json.dumps({"returncode": 0, "stdout": "", "stderr": ""}), ""
        def poll(self): return 0
    real_popen = subprocess.Popen
    monkeypatch.setattr(executor.subprocess, "Popen", lambda c, **k: (
        launched.append(c) or Proc()) if "python3" in c else real_popen(c, **k))
    done = executor._execute(repo, ["python3", "-m", "pytest"], 5)
    assert done.returncode == 0 and len(launched) == 1
    assert done.git == {"provided": False, "reason": "repository has no .git"}


def test_direct_adapter_mounts_private_git_and_removes_it(source, tmp_path, monkeypatch, docker):
    from core import datadir
    monkeypatch.setattr(datadir, "aitelier_home", lambda: tmp_path / "home")
    seen = {}

    class Proc:
        returncode = 0
        def communicate(self, timeout):
            return json.dumps({"returncode": 0, "stdout": "", "stderr": ""}), ""
        def poll(self): return 0
    monkeypatch.setattr(executor.shutil, "which", lambda _: docker)
    real_popen = subprocess.Popen
    def popen(command, **kwargs):
        if "python3" not in command:
            return real_popen(command, **kwargs)  # the preparation container (shim)
        target = next(v for v in command if v.endswith(f":{source / '.git'}:ro"))
        seen["private"] = Path(target.split(":")[0])
        seen["head"] = (seen["private"] / "HEAD").read_text()
        return Proc()
    monkeypatch.setattr(executor.subprocess, "Popen", popen)
    done = executor._execute(source, ["python3", "-m", "pytest"], 5)
    assert done.git["head"] == git(source, "rev-parse", "HEAD")
    assert seen["head"] == "ref: refs/heads/main\n" and not seen["private"].exists()
