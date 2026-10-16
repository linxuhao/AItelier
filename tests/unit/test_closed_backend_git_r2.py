"""Correction controls: every allowed native Git operation's program authority.

The first closed profile disabled commit/tag signing but an independent review
measured `repo_push` executing a repository-selected `gpg.program` through
`push.gpgSign` against a remote advertising push certificates. These controls
re-derive the authority from the image's own Git configuration catalogue and
drive the ordinary entry points with marker programs:

* push certificate signing in every signature format;
* the RECEIVING repository's programs on a local transport (its hooks,
  alternate-refs command and push-to-deploy filters run in a separate
  `receive-pack` that the command-line profile does not reach);
* the common-repository lock around the backend's own config writer and every
  native operation whose safety rests on a configuration preflight;
* no-follow bootstrap of `.gitignore`/`.git`;
* complete index settlement before the ref moves.

Real temporary repositories only; markers are programs that record their own
execution. No key, credential or network endpoint is used.
"""

import errno
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager

import pytest

from core import closed_git
from core.workspace_manager import WorkspaceManager
from test_closed_backend_git import (  # noqa: F401 -- shared fixtures
    add_filters, db, g, home, init, marker_program, markers, project, ran, sha256)


def _ws(tmp_path):
    return WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))


# ── push certificate signing ─────────────────────────────────────────

def _signing_source(tmp_path, markers, fmt, sign):
    src = tmp_path / "src"
    init(src, {"a.txt": "ordinary raw content\n"})
    remote = tmp_path / "remote.git"
    g(tmp_path, "init", "-q", "--bare", str(remote))
    # A synthetic nonce seed makes the local receiver advertise push certificates.
    g(remote, "config", "receive.certNonceSeed", "synthetic-test-only-not-a-secret")
    g(src, "remote", "add", "origin", str(remote))
    prog = lambda n: str(marker_program(markers, n))  # noqa: E731
    # Every signing program is wired, whatever the selected format.
    g(src, "config", "gpg.program", prog("gpg"))
    g(src, "config", "gpg.openpgp.program", prog("gpg-openpgp"))
    g(src, "config", "gpg.x509.program", prog("gpgsm"))
    g(src, "config", "gpg.ssh.program", prog("ssh-keygen"))
    g(src, "config", "gpg.ssh.defaultKeyCommand", prog("ssh-default-key"))
    if fmt != "openpgp":
        g(src, "config", "gpg.format", fmt)
    if fmt != "ssh":
        g(src, "config", "user.signingkey", "synthetic-no-real-key")
    g(src, "config", "push.gpgSign", sign)
    return src, remote


@pytest.mark.parametrize("sign", ["true", "if-asked"])
@pytest.mark.parametrize("fmt", ["openpgp", "x509", "ssh"])
def test_push_never_runs_a_repository_selected_signing_program(
        db, home, tmp_path, markers, fmt, sign):
    src, remote = _signing_source(tmp_path, markers, fmt, sign)
    project(db, "signed", src)
    ws = _ws(tmp_path)
    errors = []
    for push in (lambda: ws.repo_push("signed", "main"),
                 lambda: ws.repo_push_head("signed", "feature")):
        try:
            push()
        except RuntimeError as exc:
            errors.append(str(exc))
    assert ran(markers) == [], f"push ran a repository-selected signer: {ran(markers)} {errors}"
    # Useful: the ordinary push still happens, unsigned (as commits are).
    assert errors == []
    head = g(src, "rev-parse", "HEAD")
    assert g(remote, "rev-parse", "main") == head and g(remote, "rev-parse", "feature") == head


def test_ordinary_unsigned_local_push_stays_useful(db, home, tmp_path, markers):
    src, remote = _signing_source(tmp_path, markers, "openpgp", "false")
    project(db, "plainpush", src)
    ws = _ws(tmp_path)
    result = ws.repo_push("plainpush", "main")
    assert result["pushed"] and g(remote, "rev-parse", "main") == g(src, "rev-parse", "HEAD")
    assert g(src, "config", "branch.main.remote") == "origin"  # --set-upstream still written
    assert ran(markers) == []


# ── the receiving repository on a local transport ────────────────────

RECEIVE_HOOKS = ("pre-receive", "update", "post-receive", "post-update",
                 "reference-transaction", "proc-receive", "push-to-checkout",
                 "pre-auto-gc", "post-checkout", "post-merge")


def _quiet_marker(markers, name):
    """A marker that never reads stdin: on a local transport a receiver-side
    program can inherit the pack protocol pipe, and must not consume it."""
    p = markers.parent / "bin" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(f'#!/bin/sh\necho "$0 $*" >> "{markers}/{name}.ran"\nexit 0\n')
    p.chmod(0o755)
    return p


def _hostile_local_repository(repo, markers, label, *, hooks_dir):
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for h in RECEIVE_HOOKS:
        target = hooks_dir / h
        target.write_text(f'#!/bin/sh\necho "$0" >> "{markers}/{label}-hook-{h}.ran"\nexit 0\n')
        target.chmod(0o755)
    # An alternate object store makes receive-pack consult the alternate-refs command.
    alt = repo.parent / f"{label}-alternate.git"
    g(repo.parent, "init", "-q", "--bare", str(alt))
    objects = (repo / "objects") if (repo / "objects").is_dir() else (repo / ".git" / "objects")
    (objects / "info").mkdir(parents=True, exist_ok=True)
    (objects / "info" / "alternates").write_text(str(alt / "objects") + "\n")
    g(repo, "config", "core.alternateRefsCommand",
      str(_quiet_marker(markers, f"{label}-alternate-refs")))
    g(repo, "config", "core.fsmonitor", str(_quiet_marker(markers, f"{label}-fsmonitor")))
    g(repo, "config", "uploadpack.packObjectsHook",
      str(_quiet_marker(markers, f"{label}-pack-objects-hook")))


def test_local_push_runs_no_program_of_the_receiving_repository(db, home, tmp_path, markers):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n"})
    remote = tmp_path / "remote.git"
    g(tmp_path, "init", "-q", "--bare", str(remote))
    _hostile_local_repository(remote, markers, "receiver", hooks_dir=remote / "hooks")
    g(src, "remote", "add", "origin", str(remote))
    project(db, "recv", src)
    ws = _ws(tmp_path)
    errors = []
    try:
        ws.repo_push("recv", "main")
        ws.repo_push_head("recv", "topic")
    except RuntimeError as exc:
        errors.append(str(exc))
    assert ran(markers) == [], f"the receiving repository's programs ran: {ran(markers)}"
    assert errors == []
    head = g(src, "rev-parse", "HEAD")
    assert g(remote, "rev-parse", "main") == head and g(remote, "rev-parse", "topic") == head


def test_local_push_into_a_checked_out_branch_never_deploys_through_its_filters(
        db, home, tmp_path, markers):
    dest = tmp_path / "dest"
    init(dest, {"a.txt": "one\n", ".gitattributes": "*.txt filter=evil\n"})
    add_filters(dest, markers, attributes=False)
    g(dest, "config", "receive.denyCurrentBranch", "updateInstead")
    dest_head = g(dest, "rev-parse", "HEAD")
    src = tmp_path / "src"
    g(tmp_path, "clone", "-q", str(dest), str(src))
    (src / "a.txt").write_text("two\n")
    g(src, "commit", "-qam", "two")
    project(db, "deploy", src)
    ws = _ws(tmp_path)
    try:
        ws.repo_push("deploy", "main")
        outcome = "pushed"
    except RuntimeError as exc:
        outcome = str(exc)
    assert ran(markers) == [], f"push-to-deploy ran the receiver's filters: {ran(markers)}"
    # Updating another repository's working tree is not something the backend
    # does: the receiver refuses explicitly and its checkout is untouched.
    assert outcome != "pushed" and "refusing" in outcome.lower()
    assert g(dest, "rev-parse", "HEAD") == dest_head
    assert (dest / "a.txt").read_text() == "one\n"


def test_fetch_and_clone_from_a_local_repository_run_none_of_its_programs(
        db, home, tmp_path, markers):
    upstream = tmp_path / "upstream"
    init(upstream, {"a.txt": "v1\n"})
    remote = tmp_path / "remote.git"
    g(tmp_path, "clone", "-q", "--bare", str(upstream), str(remote))
    src = tmp_path / "src"
    g(tmp_path, "clone", "-q", str(remote), str(src))
    (upstream / "a.txt").write_text("v2\n")
    g(upstream, "commit", "-qam", "v2")
    g(upstream, "push", "-q", str(remote), "main")
    # Hostile only after the test's own setup push: that push starts the
    # remote's receive-pack itself and would fire its hooks.
    _hostile_local_repository(remote, markers, "sender", hooks_dir=remote / "hooks")
    project(db, "fetcher", src)
    ws = _ws(tmp_path)
    assert ws.repo_pull("fetcher")["pulled"]
    assert (src / "a.txt").read_text() == "v2\n"
    synced = ws.repo_force_sync("fetcher", "main")
    assert synced["head"] == g(upstream, "rev-parse", "HEAD")
    ws.setup_workspace("cloned", repo_type="clone", repo_url=str(remote))
    assert (tmp_path / "pb" / "cloned" / "a.txt").read_text() == "v2\n"
    assert ran(markers) == []


# ── the common-repository lock ───────────────────────────────────────

def test_remote_config_writer_waits_for_the_common_repository_lock(db, home, tmp_path):
    src = tmp_path / "src"
    init(src)
    remote = tmp_path / "remote.git"
    g(tmp_path, "init", "-q", "--bare", str(remote))
    project(db, "locked", src)
    ws = _ws(tmp_path)
    holding = threading.Event()
    release = threading.Event()

    def preflight_in_progress():
        # Stands in for a push/pull/native-removal preflight holding the lock.
        with closed_git.repo_lock(src):
            holding.set()
            release.wait(30)

    holder = threading.Thread(target=preflight_in_progress)
    holder.start()
    assert holding.wait(10)
    done = []
    writer = threading.Thread(target=lambda: done.append(ws.repo_set_remote("locked", str(remote))))
    writer.start()
    time.sleep(0.8)
    during = g(src, "config", "--get", "remote.origin.url", check=False)
    finished_early = bool(done)
    release.set()
    holder.join(10)
    writer.join(30)
    assert not finished_early and during == "", (
        "repo_set_remote wrote the common configuration while another backend "
        "consumer held the repository lock")
    assert done and done[0]["action"] == "added"
    assert g(src, "config", "--get", "remote.origin.url") == str(remote)


def test_config_writers_and_preflighted_native_operations_refuse_outside_the_lock(home, tmp_path):
    src = tmp_path / "src"
    init(src)
    remote = tmp_path / "remote.git"
    g(tmp_path, "init", "-q", "--bare", str(remote))
    wt = tmp_path / "wt"
    file_profile = ("file", ("protocol.file.allow=always",))
    for attempt in (
            lambda: closed_git.run(src, "remote", "add", "origin", str(remote)),
            lambda: closed_git.run(src, "remote", "set-url", "origin", str(remote)),
            lambda: closed_git.run(src, "worktree", "add", "--no-checkout", "--detach",
                                   str(wt), "HEAD"),
            lambda: closed_git.run(src, "worktree", "remove", str(wt)),
            lambda: closed_git.transport(src, "push", str(remote), "main", profile=file_profile),
            lambda: closed_git.transport(src, "fetch", str(remote), profile=file_profile)):
        with pytest.raises(closed_git.GitRefused, match="lock"):
            attempt()
    assert g(src, "remote") == "" and not wt.exists()
    assert g(remote, "rev-parse", "--verify", "main", check=False) == ""
    with closed_git.repo_lock(src):  # the same calls inside the lock are the supported route
        assert closed_git.run(src, "remote", "add", "origin", str(remote)).returncode == 0
        closed_git.transport(src, "push", "origin", "main", profile=file_profile)
    assert g(remote, "rev-parse", "main") == g(src, "rev-parse", "HEAD")


# ── bootstrap writes no-follow ───────────────────────────────────────

@pytest.mark.parametrize("which", ["project", "dps"])
def test_bootstrap_never_writes_through_a_preexisting_gitignore_link(home, tmp_path, which):
    outside = tmp_path / "outside-precious.txt"
    outside.write_text("precious host bytes\n")
    ws = _ws(tmp_path)
    target = (tmp_path / "pb" / "linkp") if which == "project" else (tmp_path / "ws" / "linkp")
    target.mkdir(parents=True)
    os.symlink(outside, target / ".gitignore")
    try:
        ws.setup_workspace("linkp", repo_type="new" if which == "project" else "none")
        error = None
    except RuntimeError as exc:
        error = exc
    assert outside.read_text() == "precious host bytes\n", "bootstrap wrote through the link"
    assert isinstance(error, closed_git.GitRefused) and ".gitignore" in str(error)
    assert (target / ".gitignore").is_symlink()
    assert not (target / ".git").exists(), "refused before a half-initialised repository"


def test_bootstrap_never_initialises_through_a_git_link(home, tmp_path):
    elsewhere = tmp_path / "elsewhere-gitdir"
    ws = _ws(tmp_path)
    target = tmp_path / "pb" / "gitlinkp"
    target.mkdir(parents=True)
    os.symlink(elsewhere, target / ".git")
    try:
        ws.setup_workspace("gitlinkp", repo_type="new")
        error = None
    except RuntimeError as exc:
        error = exc
    assert not elsewhere.exists(), "git init created a repository at the link's target"
    assert isinstance(error, closed_git.GitRefused) and ".git" in str(error)


def test_bootstrap_with_a_regular_gitignore_keeps_its_useful_behavior(home, tmp_path):
    ws = _ws(tmp_path)
    target = tmp_path / "pb" / "regularp"
    target.mkdir(parents=True)
    (target / ".gitignore").write_text("old rules\n")
    (target / "keep.txt").write_text("kept\n")
    ws.setup_workspace("regularp", repo_type="new")
    assert (target / ".gitignore").read_text() == "__pycache__/\n*.pyc\n"
    assert set(g(target, "ls-files").splitlines()) == {".gitignore", "keep.txt"}
    assert g(target, "status", "--porcelain") == ""


# ── index settlement is complete before the ref moves ────────────────

@contextmanager
def _index_writes(*, limit=None, fail=False):
    """Short or failing writes, only on descriptors that are writing an index.

    Its own MonkeyPatch scope: undoing it must not undo the db/home fixtures.
    """
    real_write, real_close = os.write, os.close
    tracked = set()

    def write(fd, data):
        if fd in tracked or bytes(data[:4]) == b"DIRC":
            tracked.add(fd)
            if fail:
                raise OSError(errno.ENOSPC, "No space left on device (injected)")
            return real_write(fd, bytes(data[:limit]))
        return real_write(fd, data)

    def close(fd):
        tracked.discard(fd)
        return real_close(fd)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "write", write)
        mp.setattr(os, "close", close)
        yield


def test_short_index_writes_still_settle_a_complete_index(db, home, tmp_path):
    src = tmp_path / "src"
    base = init(src, {"a.txt": "a\n", "b/c.txt": "c\n"})
    (src / "a.txt").write_text("changed\n")
    (src / "new.txt").write_text("new\n")
    project(db, "shortw", src)
    ws = _ws(tmp_path)
    with _index_writes(limit=7):
        result = ws.repo_commit("shortw", "short writes")
    assert result["committed"] and g(src, "rev-parse", "HEAD^") == base
    idx = closed_git.parse_index((src / ".git" / "index").read_bytes())
    assert {e.path for e in idx.entries} == {"a.txt", "b/c.txt", "new.txt"}
    assert g(src, "status", "--porcelain") == ""


def test_short_writes_complete_a_directly_written_index(home, tmp_path):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n", "d/e.txt": "e\n"})
    entries = closed_git.read_index(src / ".git").entries
    (src / ".git" / "index").unlink()
    with _index_writes(limit=5):
        closed_git.write_index_file(src / ".git", entries)
    assert closed_git.read_index(src / ".git").entries == entries


def test_failed_index_settlement_refuses_before_the_ref_moves(db, home, tmp_path):
    src = tmp_path / "src"
    base = init(src, {"a.txt": "a\n"})
    (src / "a.txt").write_text("changed\n")
    project(db, "enospc", src)
    ws = _ws(tmp_path)
    index_before = sha256(src / ".git" / "index")
    with _index_writes(fail=True), pytest.raises((OSError, RuntimeError)):
        ws.repo_commit("enospc", "cannot settle")
    assert g(src, "rev-parse", "HEAD") == base, "HEAD moved although the index could not settle"
    assert sha256(src / ".git" / "index") == index_before
    assert not (src / ".git" / "index.lock").exists()
    # Nothing was lost: the same commit succeeds once writes work.
    assert ws.repo_commit("enospc", "settles")["committed"]
    assert g(src, "status", "--porcelain") == ""


# ── the closed command set refuses program-reaching arguments ────────

FILE = ("file", ("protocol.file.allow=always",))


@pytest.mark.parametrize("args,transport", [
    (("cat-file", "--textconv", "HEAD:a.txt"), False),
    (("cat-file", "--filters", "HEAD:a.txt"), False),
    (("cat-file", "--batch", "--textconv"), False),
    (("log", "-1"), False),                          # format.pretty could choose %G?
    (("log", "-1", "--format=%h %G?"), False),
    (("log", "-1", "--pretty=format:%GS"), False),
    (("rev-list", "--pretty", "HEAD"), False),
    (("rev-list", "--format=%GK", "HEAD"), False),
    (("push", "--signed", "origin", "main"), True),
    (("push", "--signed=if-asked", "origin", "main"), True),
    (("push", "--receive-pack=/bin/sh", "origin", "main"), True),
    (("push", "--exec=/bin/sh", "origin", "main"), True),
    (("fetch", "--upload-pack=/bin/sh", "origin"), True),
    (("clone", "-u", "/bin/sh", "x", "y"), True),
])
def test_program_reaching_arguments_are_refused_before_running(tmp_path, args, transport):
    with pytest.raises(closed_git.GitRefused):
        if transport:
            closed_git.transport(tmp_path, *args, profile=FILE)
        else:
            closed_git.run(tmp_path, *args)


# ── derivation: every program-valued key the image's Git knows ───────

# What closes each program-valued key of `git help --config` for the commands
# the backend runs. "profile" keys must be overridden on every closed command
# line; the others are reached only by command families the closed set does not
# contain, by transports it refuses, by remotes its preflight refuses, or are
# not programs at all. The behavioral tests above and in
# test_closed_backend_git.py exercise each mechanism with markers.
PROFILE = {"core.hookspath", "core.fsmonitor", "core.pager", "core.askpass",
           "core.alternaterefscommand", "credential.helper", "commit.gpgsign",
           "tag.gpgsign", "push.gpgsign", "log.showsignature", "diff.external",
           "gpg.program", "gpg.<format>.program", "gpg.ssh.defaultkeycommand"}
CLASSIFIED = {
    **{k: "profile" for k in PROFILE},
    "pager.<cmd>": "profile: --no-pager and GIT_PAGER=cat",
    "core.editor": "no editing command; GIT_EDITOR=:",
    "sequence.editor": "no rebase", "core.sshcommand": "ssh transport refused",
    "core.gitproxy": "git:// transport refused",
    "remote.<name>.receivepack": "remote preflight refuses; local receiver gets the closed program",
    "remote.<name>.uploadpack": "remote preflight refuses; local sender gets the closed program",
    "remote.<name>.vcs": "remote preflight refuses",
    "receive.procreceiverefs": "local receiver: hooksPath=/dev/null",
    "receive.denycurrentbranch": "local receiver: refuse (no push-to-deploy)",
    "uploadpack.packobjectshook": "protected config only; global/system suppressed",
    "filter.<driver>.clean": "raw bytes only; native removal preflight refuses",
    "filter.<driver>.smudge": "raw bytes only; native removal preflight refuses",
    "diff.<driver>.command": "no diff rendering", "diff.<driver>.textconv": "no diff rendering",
    "diff.<driver>.cachetextconv": "no diff rendering",
    "diff.tool": "no difftool", "diff.guitool": "no difftool",
    "merge.<driver>.driver": "no merge", "merge.tool": "no mergetool",
    "merge.guitool": "no mergetool", "gitcvs.dbdriver": "no cvsserver",
    "gc.recentobjectshook": "no gc (gc.auto=0, maintenance.auto=false)",
    "gc.repackfilterto": "no gc", "imap.tunnel": "no imap-send",
    "interactive.difffilter": "no add -p", "help.browser": "no help",
    "web.browser": "no web--browse", "instaweb.browser": "no instaweb",
    "submodule.<name>.update": "no submodule command; submodule.recurse=false",
    "include.path": "configuration data; native removal preflight refuses",
    "includeif.<condition>.path": "configuration data; native removal preflight refuses",
    "user.signingkey": "a key name; signing is never performed",
    "core.fsmonitorhookversion": "not a program", "advice.ignoredhook": "not a program",
    "advice.waitingforeditor": "not a program", "color.pager": "not a program",
    "notes.rewrite.<command>": "not a program", "completion.commands": "not a program",
}
_PROGRAMISH = re.compile(
    r"(program|command|cmd$|hook|helper|pager|editor|driver$|textconv|external|askpass|"
    r"browser|tunnel|gitproxy|^filter\.|gpgsign|showsignature|difffilter|\.vcs$|"
    r"uploadpack$|receivepack$|procreceiverefs|denycurrentbranch|signingkey|"
    r"^include|submodule\.<name>\.update|repackfilterto|\.tool$|guitool$)")
_UNREACHABLE_FAMILIES = ("difftool", "mergetool", "guitool", "web--browse", "instaweb",
                         "help", "imap-send", "send-email", "rebase", "submodule", "gc",
                         "maintenance", "merge", "diff", "cvsserver", "add", "commit",
                         "tag", "status", "checkout", "reset", "pull", "am", "stash")


def test_every_program_valued_configuration_key_is_closed():
    catalogue = subprocess.run(["git", "help", "--config"], capture_output=True, text=True,
                               check=True).stdout.split()
    keys = {k.lower() for k in catalogue if "." in k}
    programish = {k for k in keys if _PROGRAMISH.search(k)
                  and not k.startswith(("browser.", "difftool.", "mergetool.", "guitool.",
                                        "man.", "sendemail.", "rebase."))}
    assert programish - set(CLASSIFIED) == set(), "unclassified program-valued keys"
    fixed = {item.split("=", 1)[0].lower() for item in closed_git._FIXED_CONFIG}
    for key in PROFILE:
        if key == "gpg.<format>.program":
            assert {"gpg.openpgp.program", "gpg.x509.program", "gpg.ssh.program"} <= fixed, key
        else:
            assert key in fixed, f"{key} is not overridden by the closed profile"
    assert not set(_UNREACHABLE_FAMILIES) & (closed_git._METADATA | closed_git._TRANSPORT)
