"""Backend Git consumers never run repository-selected programs.

Real temporary repositories only. Every hostile repository wires each
callback Git could run — hooks (default and tracked core.hooksPath), clean/
smudge/process filters, fsmonitor, pager, external diff, textconv, signing,
alternate-refs command, credential/askpass helpers — to a marker program that
records its own execution. The Source repository panel (status, commit,
mutations, bootstrap) and run isolation (provisioning in both modes,
validation, seeding, reaping, native removal) are then driven through their
ordinary entry points, and the markers must stay absent while the useful
result (status, raw commit, served bytes) is still produced.
"""

import hashlib
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from core import closed_git, run_isolation as ri
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager
from skillflow.exceptions import IsolationUnavailable


_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def g(repo, *args, check=True, env=None):
    """The TEST's own git, in a fixed environment unaffected by hostile settings."""
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo),
            "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            **_ID}
    # The test's own setup git must not trip the hostile programs either.
    own = ["core.hooksPath=/dev/null", "safe.directory=*", "core.fsmonitor=false",
           "core.untrackedCache=false", "commit.gpgSign=false", "log.showSignature=false",
           "diff.external=", "core.pager=cat", "credential.helper="]
    for i, item in enumerate(own):
        key, _, value = item.partition("=")
        base[f"GIT_CONFIG_KEY_{i}"], base[f"GIT_CONFIG_VALUE_{i}"] = key, value
    base["GIT_CONFIG_COUNT"] = str(len(own))
    base.update(env or {})
    res = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=base)
    if check and res.returncode:
        raise AssertionError(f"git {args}: {res.stderr}")
    return res.stdout.strip()


def init(path: Path, files=None) -> str:
    path.mkdir(parents=True, exist_ok=True)
    g(path, "init", "-q", "-b", "main")
    for rel, data in (files or {"seed.txt": "seed\n"}).items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)
    g(path, "add", "-A")
    g(path, "commit", "-qm", "seed")
    return g(path, "rev-parse", "HEAD")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(h))
    monkeypatch.setenv("AITELIER_ZVEC_LIFECYCLE", "0")
    return h


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = DBManager(str(tmp_path / "aitelier.db"))
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", d)
    return d


@pytest.fixture
def markers(tmp_path):
    m = tmp_path / "markers"
    m.mkdir()
    return m


def marker_program(markers: Path, name: str) -> Path:
    """A program that leaves evidence of its own execution, then passes stdin through."""
    p = markers.parent / "bin" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(f'#!/bin/sh\necho "$0 $*" >> "{markers}/{name}.ran"\ncat\n')
    p.chmod(0o755)
    return p


def ran(markers: Path) -> list[str]:
    return sorted(p.name for p in markers.iterdir())


HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-checkout",
         "post-merge", "pre-push", "reference-transaction", "post-rewrite", "pre-auto-gc",
         "post-index-change", "fsmonitor-watchman")


def make_hostile(repo: Path, markers: Path, *, tracked_hooks: bool = True,
                 attributes: bool = True) -> None:
    """Every callback Git can run from repository config, except filters (see below)."""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for h in HOOKS:
        target = hooks / h
        target.write_text(f'#!/bin/sh\necho "$0" >> "{markers}/hook-{h}.ran"\n')
        target.chmod(0o755)
    if tracked_hooks:
        # A hook directory INSIDE the checkout: bytes a run could deliver.
        tracked = repo / "githooks"
        tracked.mkdir(exist_ok=True)
        for h in HOOKS:
            target = tracked / h
            target.write_text(f'#!/bin/sh\necho "$0" >> "{markers}/tracked-{h}.ran"\n')
            target.chmod(0o755)
        g(repo, "add", "githooks")
        g(repo, "commit", "-qm", "tracked hooks")
        g(repo, "config", "core.hooksPath", "githooks")
    prog = lambda n: str(marker_program(markers, n))  # noqa: E731
    for key, name in (("core.fsmonitor", "fsmonitor"), ("core.pager", "pager"),
                      ("pager.log", "pager-log"), ("pager.status", "pager-status"),
                      ("diff.external", "extdiff"), ("diff.evil.textconv", "textconv"),
                      ("gpg.program", "gpg"), ("gpg.ssh.program", "gpg-ssh"),
                      ("core.alternateRefsCommand", "alternate-refs"),
                      ("credential.helper", "credential"), ("core.askPass", "askpass"),
                      ("core.sshCommand", "ssh"), ("core.editor", "editor"),
                      ("sequence.editor", "seq-editor")):
        g(repo, "config", key, prog(name))
    for key in ("commit.gpgSign", "log.showSignature", "tag.gpgSign"):
        g(repo, "config", key, "true")
    if not attributes:
        return
    (repo / ".gitattributes").write_text("*.txt diff=evil\n")
    g(repo, "add", ".gitattributes")
    g(repo, "commit", "-qm", "attributes", "--no-verify", "--no-gpg-sign")


def add_filters(repo: Path, markers: Path, *, attributes: bool = True) -> None:
    for kind in ("clean", "smudge", "process"):
        g(repo, "config", f"filter.evil.{kind}", str(marker_program(markers, f"filter-{kind}")))
    if attributes:
        (repo / ".gitattributes").write_text("*.txt filter=evil diff=evil\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project(db, pid, src):
    db.ensure_project(pid, name=pid, repo_type="existing", repo_path=str(src))


def tree_of(repo, rev="HEAD") -> dict:
    out = g(repo, "ls-tree", "-r", "--full-tree", rev)
    return {line.split("\t", 1)[1]: tuple(line.split("\t", 1)[0].split()) for line in out.splitlines()}


def assert_tree_served(root: Path, repo: Path, rev: str) -> None:
    for path, (mode, otype, sha) in tree_of(repo, rev).items():
        p = root / path
        if mode == "120000":
            assert p.is_symlink() and closed_git.blob_sha1(os.fsencode(os.readlink(p))) == sha, path
        else:
            assert not p.is_symlink() and closed_git.blob_sha1(p.read_bytes()) == sha, path
            assert bool(p.stat().st_mode & 0o100) == (mode == "100755"), path


class _ReapSF:
    def __init__(self, rows):
        self.rows = rows

    def get_run(self, run_id):
        return self.rows.get(run_id)

    def audit_operation_owners(self, run_id):
        return {"lost": [], "unknown": [], "alive": 0}


# ── the closed profile itself ────────────────────────────────────────

def test_closed_env_inherits_no_git_routing_or_configuration(monkeypatch):
    for name, value in {"GIT_DIR": "/elsewhere", "GIT_WORK_TREE": "/elsewhere",
                        "GIT_INDEX_FILE": "/elsewhere/index", "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "/x",
                        "GIT_CONFIG_PARAMETERS": "'core.hookspath'='/x'", "GIT_EXEC_PATH": "/x",
                        "GIT_SSH_COMMAND": "/x", "GIT_EXTERNAL_DIFF": "/x", "GIT_TEMPLATE_DIR": "/x",
                        "GIT_OBJECT_DIRECTORY": "/x", "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/x",
                        "GIT_CEILING_DIRECTORIES": "/x", "LC_ALL": "fr_FR.UTF-8"}.items():
        monkeypatch.setenv(name, value)
    env = closed_git.closed_env()
    assert env["LC_ALL"] == "C" and env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    leaked = {k for k in env if k.startswith("GIT_")} - {
        "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL", "GIT_ATTR_NOSYSTEM",
        "GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT", "GIT_ASKPASS", "GIT_PAGER", "GIT_EDITOR",
        "GIT_NO_LAZY_FETCH", "GIT_NO_REPLACE_OBJECTS", "GIT_ALLOW_PROTOCOL",
        "GIT_PROTOCOL_FROM_USER", "GIT_ADVICE"}
    assert not leaked
    assert env["GIT_ALLOW_PROTOCOL"] == "" and env["GIT_ASKPASS"] == ""


@pytest.mark.parametrize("args", [
    ("status",), ("add", "-A"), ("diff",), ("checkout", "x"), ("reset", "--hard"),
    ("pull",), ("commit", "-m", "x"), ("stash",), ("merge", "x"), ("submodule", "update"),
    ("worktree", "add", "/tmp/x", "HEAD"), ("worktree", "remove", "--force", "/tmp/x"),
    ("worktree", "prune"), ("hash-object", "-w", "x"), ("log", "-p"),
    ("log", "--show-signature"), ("commit-tree", "-S", "x"), ("read-tree", "-u", "HEAD"),
    ("ls-files", "-m"), ("init",), ("fetch",), ("push",), ("clone", "x"),
    ("config", "core.fsmonitor", "x"), ("for-each-ref",),
])
def test_callback_capable_commands_are_refused_before_running(tmp_path, args):
    with pytest.raises(closed_git.GitRefused):
        closed_git.run(tmp_path, *args)


# ── Source repository panel: plain repository stays useful ───────────

def test_plain_source_status_and_raw_commit_remain_useful(db, home, tmp_path):
    src = tmp_path / "src"
    base = init(src, {"keep.txt": "keep\n", "edit.txt": "old\n", "gone.txt": "bye\n",
                      "staged.txt": "s0\n", "tool.sh": "#!/bin/sh\n"})
    (src / ".gitignore").write_text("*.log\n")
    (src / "edit.txt").write_text("new\n")
    (src / "gone.txt").unlink()
    (src / "staged.txt").write_text("s1\n")
    g(src, "add", "staged.txt")
    (src / "new.txt").write_text("hello\n")
    (src / "noise.log").write_text("ignored\n")
    os.symlink("keep.txt", src / "link")
    (src / "tool.sh").chmod(0o755)
    project(db, "plain", src)
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))

    status = ws.repo_status("plain")
    assert status["is_git"] and status["dirty"] and status["branch"] == "main"
    assert set(status["changes"]) >= {" M edit.txt", " D gone.txt", "M  staged.txt",
                                      "?? new.txt", "?? link", "?? .gitignore", " M tool.sh"}
    assert not any("noise.log" in c for c in status["changes"])
    assert status["commits"][0]["hash"] == base[:len(status["commits"][0]["hash"])]
    native = g(src, "status", "--porcelain")
    assert status["dirty_count"] == len(native.splitlines())

    result = ws.repo_commit("plain", "raw commit  \n\n\n# kept, as with git commit -m\n\n")
    assert result["committed"] and result["hash"] == g(src, "rev-parse", "HEAD")
    assert g(src, "rev-parse", "HEAD^") == base
    assert g(src, "log", "-1", "--format=%B") == "raw commit\n\n# kept, as with git commit -m"
    tree = tree_of(src)
    assert "gone.txt" not in tree and "noise.log" not in tree
    assert tree["link"][0] == "120000" and tree["tool.sh"][0] == "100755"
    assert g(src, "show", "HEAD:edit.txt") == "new" and g(src, "show", "HEAD:new.txt") == "hello"
    # The settled index is a real Git index: native status agrees it is clean.
    assert g(src, "status", "--porcelain") == ""
    assert not (src / ".git" / "index.lock").exists()
    assert ws.repo_status("plain")["dirty"] is False
    assert ws.repo_commit("plain", "again") == {"committed": False, "message": "Nothing to commit"}
    assert g(src, "cat-file", "-p", "HEAD").count("gpgsig") == 0


def test_new_and_clone_bootstrap_use_no_template_hook_or_checkout_program(
        tmp_path, home, markers, monkeypatch):
    template = tmp_path / "template"
    (template / "hooks").mkdir(parents=True)
    hook = template / "hooks" / "post-commit"
    hook.write_text(f'#!/bin/sh\ntouch "{markers}/template-hook.ran"\n')
    hook.chmod(0o755)
    remote = tmp_path / "remote"
    init(remote, {"a.txt": "remote bytes\n", "bin/run.sh": "#!/bin/sh\n"})
    (remote / "bin" / "run.sh").chmod(0o755)
    g(remote, "add", "-A")
    g(remote, "commit", "-qm", "exec")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    ws.setup_workspace("newp", repo_type="new")
    code = tmp_path / "pb" / "newp"
    assert g(code, "log", "--format=%s") == "Initial commit: Project workspace"
    assert not (code / ".git" / "hooks").exists()
    assert g(code, "status", "--porcelain") == ""
    ws.setup_workspace("clonep", repo_type="clone", repo_url=str(remote))
    clone = tmp_path / "pb" / "clonep"
    assert (clone / "a.txt").read_text() == "remote bytes\n"
    assert os.stat(clone / "bin" / "run.sh").st_mode & 0o100
    assert g(clone, "rev-parse", "HEAD") == g(remote, "rev-parse", "HEAD")
    assert g(clone, "status", "--porcelain") == ""
    assert ran(markers) == []
    with pytest.raises(closed_git.GitRefused, match="transport"):
        ws.setup_workspace("extp", repo_type="clone",
                           repo_url=f"ext::sh -c touch% {markers}/ext.ran")
    with pytest.raises(closed_git.GitRefused, match="transport"):
        ws.setup_workspace("sshp", repo_type="clone", repo_url="git@example.invalid:x/y.git")
    assert ran(markers) == []


# ── hostile configured Source: nothing runs, the useful result remains ─

def test_configured_source_status_commit_and_mutations_run_no_repository_program(
        db, home, tmp_path, markers):
    src = tmp_path / "src"
    init(src, {"a.txt": "one\n", "b.txt": "two\n"})
    make_hostile(src, markers)
    remote = tmp_path / "remote.git"
    g(tmp_path, "init", "-q", "--bare", str(remote))
    g(src, "remote", "add", "origin", str(remote))
    (src / "a.txt").write_text("changed\n")
    (src / "c.txt").write_text("new\n")
    project(db, "hostile", src)
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))

    status = ws.repo_status("hostile")
    assert {" M a.txt", "?? c.txt"} <= set(status["changes"]) and status["commits"]
    result = ws.repo_commit("hostile", "hostile commit")
    assert result["committed"]
    assert g(src, "show", "HEAD:a.txt") == "changed"
    assert "gpgsig" not in g(src, "cat-file", "-p", "HEAD")
    assert ws.repo_push("hostile", "main")["pushed"]
    assert g(remote, "rev-parse", "main") == result["hash"]
    assert ws.repo_set_remote("hostile", str(remote))["action"] == "updated"
    ws.repo_status("hostile")
    assert ran(markers) == [], "a repository-selected program ran on the backend"


def test_configured_filters_refuse_commit_but_status_still_answers(db, home, tmp_path, markers):
    src = tmp_path / "src"
    init(src, {"a.txt": "one\n", ".gitattributes": "*.txt filter=evil\n"})
    add_filters(src, markers, attributes=False)
    (src / "a.txt").write_text("edited\n")
    project(db, "filtered", src)
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    head = g(src, "rev-parse", "HEAD")
    index_before = sha256(src / ".git" / "index")

    status = ws.repo_status("filtered")
    assert " M a.txt" in status["changes"]
    assert any("filter" in n for n in status["working_tree_notes"])
    with pytest.raises(closed_git.GitRefused, match="filter"):
        ws.repo_commit("filtered", "would need the clean filter")
    assert g(src, "rev-parse", "HEAD") == head
    assert sha256(src / ".git" / "index") == index_before
    assert not (src / ".git" / "index.lock").exists()
    assert ran(markers) == []


def test_inherited_git_environment_cannot_route_or_configure_backend_git(
        db, home, tmp_path, markers, monkeypatch):
    src = tmp_path / "src"
    init(src, {"real.txt": "real\n"})
    decoy = tmp_path / "decoy"
    init(decoy, {"decoy.txt": "decoy\n"})
    decoy_head = g(decoy, "rev-parse", "HEAD")
    evil_hooks = tmp_path / "evil-hooks"
    evil_hooks.mkdir()
    for h in HOOKS:
        (evil_hooks / h).write_text(f'#!/bin/sh\ntouch "{markers}/env-hook-{h}.ran"\n')
        (evil_hooks / h).chmod(0o755)
    attrs = tmp_path / "global-attributes"
    attrs.write_text("* filter=genv\n")
    gconf = tmp_path / "global.gitconfig"
    gconf.write_text(
        f"[core]\n\tfsmonitor = {marker_program(markers, 'global-fsmonitor')}\n"
        f"\thooksPath = {evil_hooks}\n\tattributesFile = {attrs}\n"
        f"[filter \"genv\"]\n\tclean = {marker_program(markers, 'global-clean')}\n"
        f"\tsmudge = {marker_program(markers, 'global-smudge')}\n")
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    (fake_home / ".gitconfig").write_text(gconf.read_text())
    project(db, "envp", src)
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    (src / "real.txt").write_text("edited\n")
    for name, value in {
            "GIT_DIR": str(decoy / ".git"), "GIT_WORK_TREE": str(decoy),
            "GIT_INDEX_FILE": str(decoy / ".git" / "index"),
            "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": str(marker_program(markers, "env-fsmonitor")),
            "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": str(evil_hooks),
            "GIT_CONFIG_GLOBAL": str(gconf), "HOME": str(fake_home),
            "XDG_CONFIG_HOME": str(fake_home),
            "GIT_EXTERNAL_DIFF": str(marker_program(markers, "env-extdiff")),
            "GIT_PAGER": str(marker_program(markers, "env-pager")),
            "GIT_SSH_COMMAND": str(marker_program(markers, "env-ssh")),
            "GIT_ASKPASS": str(marker_program(markers, "env-askpass")),
            "GIT_TEMPLATE_DIR": str(tmp_path / "nonexistent-template"),
            "GIT_OBJECT_DIRECTORY": str(decoy / ".git" / "objects")}.items():
        monkeypatch.setenv(name, value)

    status = ws.repo_status("envp")
    assert status["path"] == str(src) and " M real.txt" in status["changes"]
    assert ws.repo_commit("envp", "real commit")["committed"]
    # The test's own `g` builds its environment from scratch, so these reads
    # see the real repositories even while the hostile variables stay set.
    assert g(src, "show", "HEAD:real.txt") == "edited"
    assert g(decoy, "rev-parse", "HEAD") == decoy_head and g(decoy, "status", "--porcelain") == ""
    rec = ri.ensure_for_run(db, run_id="run-env", project_id="envp",
                            config_name="coding_impl", repo_mode="code")
    assert rec["base_sha"] == g(src, "rev-parse", "HEAD")
    assert_tree_served(Path(rec["worktree_path"]), src, rec["base_sha"])
    assert ran(markers) == []


# ── no-follow working paths ──────────────────────────────────────────

def test_working_reads_never_follow_symlinks_out_of_the_checkout(db, home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("SECRET bytes outside the checkout\n")
    src = tmp_path / "src"
    init(src, {"dir/f.txt": "inside\n", "keep.txt": "k\n"})
    import shutil
    shutil.rmtree(src / "dir")
    os.symlink(outside, src / "dir")
    os.symlink(outside / "f.txt", src / "escape")
    raw = closed_git.raw_status(src)
    assert raw.changes["dir/f.txt"] == " D"
    project(db, "nofollow", src)
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    assert ws.repo_commit("nofollow", "links as links")["committed"]
    tree = tree_of(src)
    assert tree["dir"][0] == "120000" and tree["escape"][0] == "120000"
    assert "dir/f.txt" not in tree
    assert g(src, "cat-file", "-p", f"HEAD:escape") == str(outside / "f.txt")
    objects = g(src, "rev-list", "--objects", "--all")
    secret = closed_git.blob_sha1((outside / "f.txt").read_bytes())
    assert secret not in objects
    with closed_git.WorkTree(src) as wt:
        assert wt.read("dir/f.txt").kind == "absent"
        with pytest.raises(closed_git.GitRefused):
            wt.read("../outside/f.txt")


# ── S1 provisioning: no-checkout registration, raw bytes, both modes ─

@pytest.mark.parametrize("mode", ["worktree", "read_snapshot"])
def test_s1_provisioning_registers_without_checkout_and_serves_raw_bytes(
        db, home, tmp_path, markers, mode):
    src = tmp_path / "src"
    init(src, {"a.txt": "alpha\n", "sub/b.txt": "beta\n", "x.sh": "#!/bin/sh\n"})
    (src / "x.sh").chmod(0o755)
    os.symlink("sub/b.txt", src / "ln")
    g(src, "add", "-A")
    g(src, "commit", "-qm", "modes")
    make_hostile(src, markers)
    # A filter driver that is configured but selected by no attribute is inert.
    add_filters(src, markers, attributes=False)
    project(db, "s1", src)
    rec = ri.ensure_for_run(db, run_id=f"run-s1-{mode}", project_id="s1",
                            config_name="coding_impl" if mode == "worktree" else "review",
                            repo_mode="code" if mode == "worktree" else "none")
    assert rec["mode"] == mode
    wt = Path(rec["worktree_path"])
    assert_tree_served(wt, src, rec["base_sha"])
    assert g(wt, "ls-files", "-s").splitlines() == g(src, "ls-files", "-s").splitlines()
    ri.verify_served_tree(wt, rec["base_sha"])
    assert ri.resolve_for_resolver(db, rec["run_id"]) == str(wt)
    if mode == "worktree":
        assert g(wt, "symbolic-ref", "--short", "HEAD") == rec["branch"]
    else:
        assert g(wt, "rev-parse", "HEAD") == rec["base_sha"]
        assert g(wt, "symbolic-ref", "-q", "HEAD", check=False) == ""
    assert ran(markers) == []


def test_tree_selecting_a_configured_filter_is_refused_not_served_converted(
        db, home, tmp_path, markers):
    src = tmp_path / "src"
    init(src, {"a.txt": "alpha\n", ".gitattributes": "*.txt filter=evil\n"})
    add_filters(src, markers, attributes=False)
    project(db, "filt", src)
    with pytest.raises(IsolationUnavailable, match="filter"):
        ri.ensure_for_run(db, run_id="run-filt", project_id="filt",
                          config_name="coding_impl", repo_mode="code")
    assert ri.record(db, "run-filt") is None
    assert ran(markers) == []


def test_a_sandbox_edited_tracked_hook_does_not_run_on_the_next_provisioning(
        db, home, tmp_path, markers):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n"})
    (src / "githooks").mkdir()
    (src / "githooks" / "post-checkout").write_text("#!/bin/sh\nexit 0\n")
    (src / "githooks" / "post-checkout").chmod(0o755)
    g(src, "add", "-A")
    g(src, "commit", "-qm", "benign tracked hook")
    g(src, "config", "core.hooksPath", "githooks")
    project(db, "relay", src)
    first = ri.ensure_for_run(db, run_id="run-first", project_id="relay",
                              config_name="coding_impl", repo_mode="code")
    w1 = Path(first["worktree_path"])
    for h in ("post-checkout", "reference-transaction", "post-commit"):
        (w1 / "githooks" / h).write_text(f'#!/bin/sh\ntouch "{markers}/delivered-{h}.ran"\n')
        (w1 / "githooks" / h).chmod(0o755)
    g(w1, "add", "-A")
    g(w1, "commit", "-qm", "run delivers hostile hooks")
    g(src, "merge", "--ff-only", "-q", first["branch"])
    assert (src / "githooks" / "reference-transaction").exists()
    project(db, "relay2", src)
    second = ri.ensure_for_run(db, run_id="run-second", project_id="relay2",
                               config_name="coding_impl", repo_mode="code")
    assert_tree_served(Path(second["worktree_path"]), src, second["base_sha"])
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    (src / "a.txt").write_text("source edit\n")
    assert ws.repo_commit("relay", "source commit after delivery")["committed"]
    assert ran(markers) == []


def test_snapshot_validation_is_raw_and_refuses_index_drift(db, home, tmp_path):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n"})
    project(db, "snap", src)
    rec = ri.ensure_for_run(db, run_id="run-snap-raw", project_id="snap",
                            config_name="review", repo_mode="none")
    wt = Path(rec["worktree_path"])
    assert ri.resolve_for_resolver(db, "run-snap-raw") == str(wt)
    (wt / "a.txt").write_text("staged drift\n")
    g(wt, "add", "a.txt")
    (wt / "a.txt").write_text("a\n")  # disk restored, index still differs
    with pytest.raises(IsolationUnavailable, match="index changes"):
        ri.resolve_for_resolver(db, "run-snap-raw")
    admin = Path(g(wt, "rev-parse", "--absolute-git-dir"))
    (admin / "index").write_bytes(b"DIRC corrupted")
    with pytest.raises(IsolationUnavailable, match="cannot be verified"):
        ri.resolve_for_resolver(db, "run-snap-raw")


# ── H ∪ I ∪ U seed of a dirty Source ─────────────────────────────────

def test_seed_copies_actual_working_state_for_head_index_and_untracked(db, home, tmp_path, markers):
    src = tmp_path / "src"
    base = init(src, {"mod.txt": "base\n", "del.txt": "del\n", "conf.txt": "base\n",
                      "exec.sh": "#!/bin/sh\n", ".gitignore": "*.log\nbuild/\n",
                      "dir/keep.txt": "k\n"})
    g(src, "checkout", "-qb", "other")
    (src / "conf.txt").write_text("other side\n")
    g(src, "commit", "-qam", "other")
    g(src, "checkout", "-q", "main")
    (src / "conf.txt").write_text("main side\n")
    g(src, "commit", "-qam", "main")
    base = g(src, "rev-parse", "HEAD")
    project(db, "seed", src)
    rec = ri.ensure_for_run(db, run_id="run-seed", project_id="seed",
                            config_name="coding_impl", repo_mode="code")
    w = Path(rec["worktree_path"])
    # Now make Source dirty in every inventory class.
    g(src, "merge", "-q", "other", check=False)          # unmerged conf.txt (stages 1-3)
    (src / "conf.txt").write_text("resolved by hand, not staged\n")
    (src / "mod.txt").write_text("working bytes\n")
    (src / "del.txt").unlink()
    (src / "forced.log").write_text("staged ignored v1\n")
    g(src, "add", "-f", "forced.log")
    (src / "forced.log").write_text("ignored, working v2\n")  # bytes differ from the stage
    (src / "idxonly.txt").write_text("x\n")
    g(src, "add", "idxonly.txt")
    (src / "idxonly.txt").unlink()                      # index-only path, absent on disk
    (src / "untracked.txt").write_text("new\n")
    (src / "noise.log").write_text("ignored untracked\n")
    (src / "exec.sh").chmod(0o755)
    os.symlink("dir/keep.txt", src / "inner-link")
    make_hostile(src, markers, tracked_hooks=False, attributes=False)
    index_before = sha256(src / ".git" / "index")
    w_index_before = sha256(Path(g(w, "rev-parse", "--absolute-git-dir")) / "index")
    with pytest.raises(IsolationUnavailable, match="admission"):
        ri.seed_worktree_from_source(db, "run-seed", admission={"id": 999})
    adm = ri.admit_write(db, src, kind="coding_workspace", detail="seed run-seed")
    try:
        manifest = ri.seed_worktree_from_source(db, "run-seed", admission=adm)
    finally:
        ri.retire_write(db, adm["id"])
    assert (w / "mod.txt").read_text() == "working bytes\n"
    assert not (w / "del.txt").exists() and not (w / "idxonly.txt").exists()
    assert (w / "conf.txt").read_text() == "resolved by hand, not staged\n"
    assert (w / "forced.log").read_text() == "ignored, working v2\n"
    assert (w / "untracked.txt").read_text() == "new\n"
    assert not (w / "noise.log").exists(), "untracked ignored data is outside the seed"
    assert os.stat(w / "exec.sh").st_mode & 0o100
    assert os.readlink(w / "inner-link") == "dir/keep.txt"
    assert manifest["paths"]["idxonly.txt"] == {"kind": "absent"}
    assert manifest["paths"]["forced.log"]["kind"] == "file"
    # Source index and HEAD untouched; the owned tree's index still describes its base.
    assert sha256(src / ".git" / "index") == index_before
    assert g(src, "rev-parse", "HEAD") == base
    assert sha256(Path(g(w, "rev-parse", "--absolute-git-dir")) / "index") == w_index_before
    assert ran(markers) == []


def test_seed_refuses_head_race_unconfined_links_and_gitlinks(db, home, tmp_path):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n"})
    project(db, "seedx", src)
    rec = ri.ensure_for_run(db, run_id="run-seedx", project_id="seedx",
                            config_name="coding_impl", repo_mode="code")
    adm = ri.admit_write(db, src, kind="coding_workspace", detail="seedx")
    try:
        os.symlink("/etc/passwd", src / "abs-link")
        with pytest.raises(IsolationUnavailable, match="leaves the repository"):
            ri.seed_worktree_from_source(db, "run-seedx", admission=adm)
        assert not os.path.lexists(Path(rec["worktree_path"]) / "abs-link")
        (src / "abs-link").unlink()
        os.symlink("../../x", src / "up-link")
        with pytest.raises(IsolationUnavailable, match="leaves the repository"):
            ri.seed_worktree_from_source(db, "run-seedx", admission=adm)
        (src / "up-link").unlink()
        nested = src / "nested"
        init(nested, {"n.txt": "n\n"})
        with pytest.raises(IsolationUnavailable, match="embedded repository"):
            ri.seed_worktree_from_source(db, "run-seedx", admission=adm)
        import shutil
        shutil.rmtree(nested)
        (src / "a.txt").write_text("moved\n")
        g(src, "commit", "-qam", "head moves")
        with pytest.raises(IsolationUnavailable, match="seed conflict"):
            ri.seed_worktree_from_source(db, "run-seedx", admission=adm)
    finally:
        ri.retire_write(db, adm["id"])


# ── reaping and native removal ───────────────────────────────────────

def _release(db, run_id, disposition="auto_released_terminal_quiet"):
    ri.release(db, run_id, disposition=disposition)


def test_dirty_delivered_tree_is_retained_without_hidden_commit_or_force(db, home, tmp_path):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n"})
    project(db, "dirty", src)
    rec = ri.ensure_for_run(db, run_id="run-dirty-w", project_id="dirty",
                            config_name="coding_impl", repo_mode="code")
    w = Path(rec["worktree_path"])
    (w / "a.txt").write_text("delivered but never committed\n")
    _release(db, "run-dirty-w", "discarded")
    tip = g(src, "rev-parse", rec["branch"])
    report = ri.reap_released_worktrees(db, _ReapSF({"run-dirty-w": {"status": "completed"}}),
                                        min_idle_seconds=0)
    assert report["removed"] == []
    assert "uncommitted work" in report["retained"][0]["reason"]
    assert (w / "a.txt").read_text() == "delivered but never committed\n"
    assert g(src, "rev-parse", rec["branch"]) == tip, "no hidden cleanup commit"
    assert str(w) in g(src, "worktree", "list", "--porcelain")
    ok, why = ri._remove_run_worktree(ri.record(db, "run-dirty-w"))
    assert not ok and "not clean" in why and w.exists()


@pytest.mark.parametrize("mode", ["worktree", "read_snapshot"])
def test_clean_released_trees_are_still_reaped_by_native_non_force_removal(db, home, tmp_path, mode):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n"})
    project(db, "reap", src)
    rid = f"run-reap-{mode}"
    rec = ri.ensure_for_run(db, run_id=rid, project_id="reap",
                            config_name="coding_impl" if mode == "worktree" else "review",
                            repo_mode="code" if mode == "worktree" else "none")
    w = Path(rec["worktree_path"])
    _release(db, rid)
    report = ri.reap_released_worktrees(db, _ReapSF({rid: {"status": "completed"}}),
                                        min_idle_seconds=0)
    assert [x["run_id"] for x in report["removed"]] == [rid], report
    assert not w.exists()
    if mode == "worktree":
        assert g(src, "show-ref", "--verify", f"refs/heads/{rec['branch']}")


@pytest.mark.parametrize("hazard", ["filter", "fsmonitor", "submodule", "include"])
def test_unsafe_native_removal_configuration_retains_instead_of_calling_git(
        db, home, tmp_path, markers, hazard):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n"})
    project(db, "unsafe", src)
    rid = f"run-unsafe-{hazard}"
    rec = ri.ensure_for_run(db, run_id=rid, project_id="unsafe",
                            config_name="coding_impl", repo_mode="code")
    w = Path(rec["worktree_path"])
    # Configuration lands AFTER provisioning, as a delivered/operator change would.
    if hazard == "filter":
        add_filters(src, markers, attributes=False)
    elif hazard == "fsmonitor":
        g(src, "config", "core.fsmonitor", str(marker_program(markers, "fsmonitor")))
    elif hazard == "submodule":
        g(src, "config", "submodule.x.url", str(tmp_path / "nowhere"))
    else:
        extra = tmp_path / "extra.gitconfig"
        extra.write_text(f"[filter \"inc\"]\n\tclean = {marker_program(markers, 'inc-clean')}\n")
        g(src, "config", "include.path", str(extra))
    _release(db, rid, "discarded")
    report = ri.reap_released_worktrees(db, _ReapSF({rid: {"status": "completed"}}),
                                        min_idle_seconds=0)
    assert report["removed"] == []
    assert "native removal retained" in report["retained"][0]["reason"]
    assert w.exists() and str(w) in g(src, "worktree", "list", "--porcelain")
    assert ri.record(db, rid)["disposition"].startswith("reaping_discarded") or \
        ri.record(db, rid)["disposition"] == "discarded"
    assert ran(markers) == []


def test_hostile_configured_source_reaper_predicates_run_nothing(db, home, tmp_path, markers):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n"})
    project(db, "hreap", src)
    rec = ri.ensure_for_run(db, run_id="run-hreap", project_id="hreap",
                            config_name="coding_impl", repo_mode="code")
    make_hostile(src, markers)
    add_filters(src, markers, attributes=False)
    _release(db, "run-hreap", "discarded")
    ok, why = ri.is_disposable(db, "run-hreap", run_status="completed", admitted_ops=0)
    assert ok, why
    report = ri.reap_released_worktrees(db, _ReapSF({"run-hreap": {"status": "completed"}}),
                                        min_idle_seconds=0)
    assert report["removed"] == [] and Path(rec["worktree_path"]).exists()
    assert ran(markers) == []


# ── transports ───────────────────────────────────────────────────────

def test_unsupported_transports_and_remote_programs_refuse_explicitly(db, home, tmp_path, markers):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n"})
    project(db, "net", src)
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    ws.repo_set_remote("net", "ssh://git@example.invalid/x.git")
    with pytest.raises(closed_git.GitRefused, match="transport"):
        ws.repo_push("net", "main")
    remote = tmp_path / "remote.git"
    g(tmp_path, "init", "-q", "--bare", str(remote))
    ws.repo_set_remote("net", str(remote))
    g(src, "config", "remote.origin.receivepack", str(marker_program(markers, "receivepack")))
    with pytest.raises(closed_git.GitRefused, match="receivepack"):
        ws.repo_push("net", "main")
    g(src, "config", "--unset", "remote.origin.receivepack")
    g(src, "config", "branch.main.remote", "origin")
    g(src, "config", "branch.main.merge", "refs/heads/main")
    g(src, "config", "url.ext::sh.insteadOf", "/")
    with pytest.raises(closed_git.GitRefused, match="insteadOf"):
        ws.repo_pull("net")
    g(src, "config", "--unset", "url.ext::sh.insteadOf")
    assert ran(markers) == []


def test_pull_and_force_sync_update_files_raw_from_an_allowed_remote(db, home, tmp_path, markers):
    upstream = tmp_path / "upstream"
    init(upstream, {"a.txt": "v1\n", "old.txt": "old\n"})
    remote = tmp_path / "remote.git"
    g(tmp_path, "clone", "-q", "--bare", str(upstream), str(remote))
    src = tmp_path / "src"
    g(tmp_path, "clone", "-q", str(remote), str(src))
    make_hostile(src, markers, tracked_hooks=False)
    g(src, "reset", "-q", "--hard", "origin/main")
    project(db, "sync", src)
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    (upstream / "a.txt").write_text("v2\n")
    (upstream / "old.txt").unlink()
    (upstream / "new.sh").write_text("#!/bin/sh\n")
    (upstream / "new.sh").chmod(0o755)
    g(upstream, "add", "-A")
    g(upstream, "commit", "-qm", "v2")
    g(upstream, "push", "-q", str(remote), "main")
    (src / "local-untracked.txt").write_text("mine\n")
    result = ws.repo_pull("sync")
    assert result["pulled"]
    assert (src / "a.txt").read_text() == "v2\n" and not (src / "old.txt").exists()
    assert os.stat(src / "new.sh").st_mode & 0o100
    assert g(src, "rev-parse", "HEAD") == g(upstream, "rev-parse", "HEAD")
    assert (src / "local-untracked.txt").read_text() == "mine\n"
    (src / "a.txt").write_text("local edit\n")
    (upstream / "a.txt").write_text("v3\n")
    g(upstream, "commit", "-qam", "v3")
    g(upstream, "push", "-q", str(remote), "main")
    with pytest.raises(closed_git.GitRefused, match="local changes"):
        ws.repo_pull("sync")
    synced = ws.repo_force_sync("sync", "main")
    assert synced["synced"] and synced["backup_branch"]
    assert (src / "a.txt").read_text() == "v3\n"
    assert g(src, "rev-parse", synced["backup_branch"]) != synced["head"]
    native = g(src, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "status", "--porcelain", "--untracked-files=no")
    assert native == ""
    assert ran(markers) == []


# ── shared mechanisms ────────────────────────────────────────────────

def test_common_identity_lock_is_shared_by_aliases_and_reentrant(home, tmp_path):
    src = tmp_path / "src"
    init(src)
    alias = tmp_path / "alias"
    os.symlink(src, alias)
    (src / "sub").mkdir()
    assert closed_git.common_identity(alias) == closed_git.common_identity(src)
    assert closed_git.common_identity(src / "sub") == closed_git.common_identity(src)
    entered = threading.Event()
    order = []
    with closed_git.repo_lock(src):
        with closed_git.repo_lock(alias):  # nested trusted caller reuses the lock
            def other():
                with closed_git.repo_lock(src / "sub"):
                    order.append("other")
                entered.set()
            t = threading.Thread(target=other)
            t.start()
            time.sleep(0.3)
            order.append("holder")
    t.join(10)
    assert entered.is_set() and order == ["holder", "other"]


@pytest.mark.parametrize("version", ["2", "3", "4"])
def test_index_reader_and_writer_round_trip_with_git(tmp_path, version):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n", "deep/er/b.txt": "b\n", "deep/c.txt": "c\n"})
    g(src, "update-index", "--index-version", version)
    if version != "2":
        g(src, "update-index", "--skip-worktree", "deep/c.txt")
    idx = closed_git.read_index(src / ".git")
    assert {e.path for e in idx.entries} == {"a.txt", "deep/er/b.txt", "deep/c.txt"}
    data = closed_git.serialize_index(idx.entries)
    (src / ".git" / "index").write_bytes(data)
    assert g(src, "ls-files", "-s") == "\n".join(
        f"{oct(e.mode)[2:]} {e.sha} 0\t{e.path}" for e in sorted(idx.entries, key=lambda e: e.key))
    assert closed_git.parse_index(data).entries == idx.entries


def test_the_markers_are_live_under_native_callback_capable_git(tmp_path, markers):
    """Positive control: the same hostile repository DOES run its programs under
    the native commands this boundary replaces, so absent markers above mean
    'not executed', not 'not wired'."""
    src = tmp_path / "src"
    init(src, {"a.txt": "one\n", ".gitattributes": "*.txt filter=evil\n"})
    make_hostile(src, markers)
    add_filters(src, markers)  # working-tree attributes now select the filter again
    native = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
              "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, **_ID}

    def old_style(*args):
        return subprocess.run(["git", *args], cwd=src, capture_output=True, text=True, env=native)

    (src / "a.txt").write_text("edited\n")
    old_style("status", "--porcelain")                       # repo_status before
    old_style("add", "-A")                                    # repo_commit before
    old_style("commit", "-m", "native")
    old_style("worktree", "add", "--detach", str(tmp_path / "w"), "HEAD")  # S1 before
    fired = ran(markers)
    assert any(m.startswith("filter-") for m in fired), fired
    assert "fsmonitor.ran" in fired, fired
    assert any(m.startswith("tracked-") for m in fired), fired
    assert "gpg.ran" in fired, fired
