"""Trusted bounded CPU transport for repository commands.

When ``AITELIER_HOST_LAUNCHER_SOCKET`` is set, the bounded Docker launch is
performed by the operator-enabled host launcher service
(``core.repository_host_launcher``) over that 0600 local Unix socket — the
backend itself holds neither the Docker CLI nor the Docker socket, and a
host-launcher failure is an honest non-pass with no local fallback.
Otherwise (host-local operator runs), the direct Docker adapter below is
used. Repository commands never fall back to the host Python.

The normal Node gate uses the same transport through two closed profiles,
``node_fetch`` and ``node_run`` (`validate_node_request`): the container sees
only the gate's own ticket (a snapshot of the repository plus an npm cache),
never the repository, runs the operator's node executor image, and its argv
comes from a fixed table.
"""
from __future__ import annotations
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

HOST_LAUNCHER_ENV = "AITELIER_HOST_LAUNCHER_SOCKET"

class IsolationUnavailable(RuntimeError):
    pass


class NodeRefused(IsolationUnavailable):
    """A Node request outside the closed profiles: nothing was executed."""


class PreparationCancelled(IsolationUnavailable):
    """The launch's client went away while its Git metadata was prepared."""


class GitEntryRefused(IsolationUnavailable):
    """The repository's `.git` can be neither normalized nor masked: nothing runs."""


# ── normal Node gate profiles ──────────────────────────────────────────────
#
# Both profiles run the PINNED upstream npm (NODE_NPM_VERSION under
# NODE_BIN_DIR) from the operator's separate node executor image, never the
# npm 9 the pytest image ships, and mount the gate's own ticket — the full
# private snapshot, its npm cache and the gate-owned npm user config.
#
# `node_fetch` is the only container that gets a network (the operator's,
# not egress-filtered). It runs only that npm, and the gate's command line —
# which npm ranks above the project's own .npmrc — turns off every lifecycle
# script and every git or URL dependency: measured on npm 12.2.0, `npm ci
# --ignore-scripts` runs no hook, including `prepare` of packed directory and
# git dependencies (npm 9 still ran those), but `--ignore-scripts` ALONE let a
# project .npmrc `git=` run a program from the repository, which
# `--allow-git=none --git=/bin/false` stops. npm, pacote and tar still parse
# registry data and the project's own files with that network.
#
# `node_run` is the real install, build and test with `--network none`. Its
# install allows every dependency script (`--dangerously-allow-all-scripts`,
# npm 9's behaviour): npm 12 otherwise SKIPS unapproved dependency scripts
# with only a warning, and this container is already the boundary for the
# project's own arbitrary build and test code.
NODE_PROFILES = ("node_fetch", "node_run")
NODE_TICKET_PREFIX = "node-"
NODE_FETCH_NETWORK_ENV = "AITELIER_NODE_FETCH_NETWORK"
NODE_EXECUTOR_IMAGE_ENV = "AITELIER_NODE_EXECUTOR_IMAGE"
NODE_NPM_VERSION = "12.2.0"
NODE_BIN_DIR = "/opt/aitelier-node/bin"
NODE_USERCONFIG = "npmrc"           # in the ticket; written by the gate
NODE_USERCONFIG_TEXT = "install-links=true\n"
NODE_RUN_FORMS = frozenset({("npm", "ci", "--offline"), ("npm", "install", "--offline"),
                            ("npm", "run", "build"), ("npm", "test")})
NODE_FETCH_COMMANDS = frozenset({("npm", "ci"), ("npm", "install")})
NODE_FETCH_FLAGS = ("--ignore-scripts", "--allow-git=none", "--git=/bin/false",
                    "--allow-remote=none")
NODE_INSTALL_RUN_FLAGS = ("--dangerously-allow-all-scripts",)
NODE_COMMON_FLAGS = ("--globalconfig=/dev/null", "--no-audit", "--no-fund",
                     "--update-notifier=false")
NODE_MANIFEST_CAP = 1 << 20
NODE_LOCK_CAP = 64 << 20
NODE_LOCKFILES = ("npm-shrinkwrap.json", "package-lock.json")
_NETWORK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
IMAGE_DIGEST = re.compile(r"(?:[A-Za-z0-9._:/-]+@)?sha256:[0-9a-f]{64}")


def read_regular(root: Path, rel, cap: int, *, single_link: bool = False) -> bytes | None:
    """Bytes of ``root/rel`` without following a link at ANY component.

    None when the final file is absent. A link, a non-regular file or a file
    over ``cap`` is refused, and the refusal names the path only, never bytes.
    """
    parts = PurePosixPath(str(rel)).parts
    if not parts or ".." in parts or PurePosixPath(str(rel)).is_absolute():
        raise NodeRefused(f"{rel} is not a contained relative path")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    fd = os.open(root, flags | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise NodeRefused(f"{rel}: a parent is a link or not a directory") from exc
            os.close(fd)
            fd = nxt
        try:
            f = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise NodeRefused(f"{rel} must be a regular file, not a link") from exc
            raise
        try:
            info = os.fstat(f)
            if not stat.S_ISREG(info.st_mode):
                raise NodeRefused(f"{rel} must be a regular file")
            if single_link and info.st_nlink != 1:
                raise NodeRefused(f"{rel} must not share a hard-linked inode")
            if info.st_size > cap:
                raise NodeRefused(f"{rel} exceeds {cap} bytes")
            with os.fdopen(f, "rb", closefd=False) as stream:
                data = stream.read(cap + 1)
            if len(data) > cap:
                raise NodeRefused(f"{rel} exceeds {cap} bytes")
            return data
        finally:
            os.close(f)
    finally:
        os.close(fd)


def _real_dir(path: Path) -> None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise NodeRefused(f"{path} must be a real directory, not a link")


def node_report_root(aitelier_home, run_id: str) -> Path:
    return Path(aitelier_home) / "gate-reports" / hashlib.sha256(run_id.encode()).hexdigest()


def validate_node_request(report_root: Path, request: dict, network: str, image: str) -> dict:
    """The one validator for both Node profiles (launcher and direct adapter).

    The ticket must be a ``node-`` direct child of the run's report root and
    hold ``src/`` (the snapshot, where both profiles work), ``cache/`` and the
    gate's npm user config. ``image`` is the operator's node executor image;
    without one nothing runs. Returns the fields the Docker argv is built
    from; anything else is `NodeRefused`.
    """
    profile = request.get("profile")
    if profile not in NODE_PROFILES:
        raise NodeRefused(f"unknown node profile {profile!r}")
    if not image or not IMAGE_DIGEST.fullmatch(image):
        raise NodeRefused("no node executor image (an immutable sha256 digest) is configured "
                          "(node_executor_image)")
    if (request.get("report_dir") or request.get("relay_socket")
            or request.get("import_module") or request.get("pytest_timeout")):
        raise NodeRefused("node profiles take no report_dir/relay/import/pytest fields")
    dirs = request.get("writable_dirs")
    if not isinstance(dirs, list) or len(dirs) != 1 or not isinstance(dirs[0], str):
        raise NodeRefused("node profiles take exactly the gate's own node ticket")
    ticket = Path(dirs[0])
    if (not ticket.is_absolute() or str(ticket) != os.path.normpath(str(ticket))
            or ticket.parent != Path(report_root)
            or not ticket.name.startswith(NODE_TICKET_PREFIX)):
        raise NodeRefused("writable dir is not a node ticket of this run")
    src, cache = ticket / "src", ticket / "cache"
    for directory in (ticket, src, cache):
        _real_dir(directory)
    if read_regular(ticket, NODE_USERCONFIG, 4096) != NODE_USERCONFIG_TEXT.encode():
        raise NodeRefused("the ticket's npm user config is not the gate's")
    if request.get("npm_cache") != str(cache):
        raise NodeRefused("npm_cache must be the ticket's own cache")
    workdir = request.get("workdir")
    if not isinstance(workdir, str) or os.path.normpath(workdir) != workdir:
        raise NodeRefused("workdir must be a normalized absolute path")
    try:
        rel = Path(workdir).relative_to(src)
    except ValueError as exc:
        raise NodeRefused("workdir must lie inside the ticket's src/") from exc
    current = src
    for part in rel.parts:
        current = current / part
        _real_dir(current)
    if read_regular(src, PurePosixPath(rel.as_posix()) / "package.json", NODE_MANIFEST_CAP) is None:
        raise NodeRefused("workdir has no package.json")
    args = request.get("args")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise NodeRefused("args must be a list of strings")
    config = (f"--userconfig={ticket / NODE_USERCONFIG}",) + NODE_COMMON_FLAGS
    if profile == "node_run":
        if tuple(args) not in NODE_RUN_FORMS:
            raise NodeRefused(f"node_run command not allow-listed: {args[:4]!r}")
        argv, net = list(args), "none"
        if args[1] in ("ci", "install"):
            argv += [*NODE_INSTALL_RUN_FLAGS, *config]
    else:
        if tuple(args) not in NODE_FETCH_COMMANDS:
            raise NodeRefused(f"node_fetch command not allow-listed: {args[:4]!r}")
        if not network:
            raise NodeRefused("networked dependency fetch is not enabled by the operator "
                              "(node_fetch_network)")
        if (not _NETWORK_NAME.fullmatch(network) or network in ("host", "none")):
            raise NodeRefused("operator node_fetch_network is not an allowed network name")
        argv, net = list(args) + [*NODE_FETCH_FLAGS, *config], network
    return {"profile": profile, "ticket": str(ticket), "workdir": workdir,
            "npm_cache": str(cache), "network": net, "args": argv, "image": image}


def node_mount_args(node: dict) -> list[str]:
    return ["-v", f"{node['ticket']}:{node['ticket']}:rw"]


def node_payload(node: dict, timeout: int) -> dict:
    """Executor-entry payload: cwd is the snapshot workdir, never the repo."""
    return {"args": node["args"], "timeout": timeout, "repo": node["workdir"],
            "npm_cache": node["npm_cache"], "node_bin": NODE_BIN_DIR,
            "expect_npm": NODE_NPM_VERSION, "relay_socket": "", "import_module": "",
            "pytest_timeout": False, "report_dir": ""}


# One input, parsed by the baked official CLI. Only the host launcher may bind it.
LINT_PROFILE = "javascript_lint"
LINT_INPUT_CAP = 8 << 20
LINT_NODE = "/opt/aitelier-node/bin/node"
LINT_ESLINT = "/opt/aitelier-eslint/node_modules/eslint/bin/eslint.js"


def lint_relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if (not isinstance(value, str) or not value or str(path) != value
            or path.is_absolute() or ".." in path.parts
            or any(part.startswith("-") for part in path.parts)
            or path.suffix.lower() not in {".js", ".mjs", ".cjs"}):
        raise NodeRefused("lint requires one normalized relative .js/.mjs/.cjs file")
    return "input" + path.suffix.lower()


def validate_lint_request(repo: Path, report_root: Path, request: dict, image: str) -> dict:
    if request.get("profile") != LINT_PROFILE or not image or not IMAGE_DIGEST.fullmatch(image):
        raise NodeRefused("lint requires the operator's immutable Node/ESLint image")
    if any(request.get(k) for k in ("report_dir", "relay_socket", "import_module",
                                    "pytest_timeout", "workdir", "npm_cache")):
        raise NodeRefused("lint takes no report/relay/import/npm execution fields")
    args, dirs = request.get("args"), request.get("writable_dirs")
    if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], str):
        raise NodeRefused("lint takes exactly one owned source file")
    filename = lint_relative_file(args[0])
    if not isinstance(dirs, list) or len(dirs) != 1 or not isinstance(dirs[0], str):
        raise NodeRefused("lint requires exactly one private input ticket")
    ticket = Path(dirs[0])
    if (not ticket.is_absolute() or str(ticket) != os.path.normpath(dirs[0])
            or ticket.parent != report_root or not ticket.name.startswith("lint-")):
        raise NodeRefused("lint input ticket does not belong to the existing run")
    if (str(repo) != request.get("repo") or report_root.resolve(strict=True) != report_root
            or ticket.resolve(strict=True) != ticket):
        raise NodeRefused("lint source and ticket roots must not be aliases or symlinks")
    _real_dir(ticket)
    original = read_regular(repo, args[0], LINT_INPUT_CAP, single_link=True)
    snapshot = read_regular(ticket, filename, LINT_INPUT_CAP, single_link=True)
    if original is None or snapshot is None or original != snapshot:
        raise NodeRefused("lint snapshot must equal the bound source file bytes")
    return {"profile": LINT_PROFILE, "ticket": str(ticket), "image": image,
            "lint_input": "/lint-input/" + filename,
            "input_sha256": hashlib.sha256(snapshot).hexdigest(),
            "args": [LINT_NODE, LINT_ESLINT, "--no-config-lookup", "--no-inline-config",
                     "--stdin", "--stdin-filename", filename]}


def lint_payload(fields: dict) -> dict:
    return {"repo": "/tmp", "args": fields["args"], "timeout": fields["timeout"],
            "lint_input": fields["lint_input"], "input_sha256": fields["input_sha256"]}


# ── private Git metadata for repository commands ───────────────────────────
#
# A run's repository is mounted read-only, but its Git metadata is not part of
# it: a linked worktree's `.git` is a pointer into the source repository's
# common dir (absent in the container: `git rev-parse HEAD` exited 128), and a
# direct repository's `.git` holds the source's config, hooks, remotes and
# every other ref. The container instead gets a private repository holding
# HEAD with ALL of its history, an index of HEAD (so status and diff show the
# current tree against it) and git's own default config.
#
# The host only reads HEAD's ref files; no git runs in the source repository,
# so none of its config, hooks or helpers is read or executed. The history is
# copied by git in a preparation container bounded like the test container
# (no network, 2 CPU, 2 GiB, 512 pids, read-only root) that mounts nothing of
# the source but its object directory, read-only: an object the source keeps
# anywhere else (a chained alternate, a promisor remote) is missing there and
# preparation fails instead of reaching for it. GIT_META_MAX_BYTES bounds the
# copy three times: HEAD's objects must fit before anything is transferred
# (`rev-list --disk-usage`), no single file written may exceed it
# (RLIMIT_FSIZE; the fetch keeps one pack), and the finished metadata must fit.
# RLIMIT_FSIZE is per file, so while preparation runs the pack, its .idx and
# the index can each approach the budget (about three budgets on disk at the
# peak); only the finished total is held to one budget, and checked after it
# is written. The whole preparation (`_run_prep`) has one deadline; on it, or
# on cancellation, its container is removed.
#
# Whatever `.git` the repository holds must never reach a repository command
# as it is. Presence is lexical (`lstat`: a dangling link is present, and
# nothing is opened or followed). A real directory or a regular file is
# replaced in the container by the private metadata or, when that cannot be
# built, by an empty directory or a pointer to nowhere, so git answers "not a
# git repository". Docker follows a link at a mount target, so any other entry
# (a symlink, a FIFO, a device...) cannot be covered reliably: the command is
# refused (`GitEntryRefused`) and nothing runs.
GIT_META_MAX_BYTES = 256 << 20
GIT_PREP_DEADLINE = 120
GIT_CONTAINER_DIR = "/aitelier-git"   # a linked worktree's pointer names it
_REFNAME = re.compile(r"refs/(?!.*\.\.)(?!.*//)[A-Za-z0-9._/+-]+")
_PER_WORKTREE_REFS = ("refs/bisect/", "refs/worktree/", "refs/rewritten/")


def _git_text(root: Path, rel: str, cap: int = 4096) -> str | None:
    raw = read_regular(root, rel, cap)
    return None if raw is None else raw.decode("utf-8").strip()


def _source_gitdirs(repo: Path, common: Path | None) -> tuple[Path, Path]:
    """(gitdir, common dir) of ``repo``'s own `.git`, never a parent's.

    A linked worktree's pointer is accepted only when its gitdir lies in a
    ``worktrees/`` directory of its common dir and points back at this very
    `.git` (git's own record, outside the worktree); ``common``, when given,
    is the common dir the run binding expects.
    """
    dot = repo / ".git"
    info = os.lstat(dot)
    if stat.S_ISDIR(info.st_mode):
        gitdir = found = dot
    elif stat.S_ISREG(info.st_mode):
        pointer = _git_text(repo, ".git") or ""
        if not pointer.startswith("gitdir: "):
            raise ValueError(".git file is not a gitdir pointer")
        gitdir = (repo / pointer[len("gitdir: "):]).resolve(strict=True)
        back = _git_text(gitdir, "gitdir") or ""
        found = (gitdir / (_git_text(gitdir, "commondir") or "")).resolve(strict=True)
        if (gitdir.parent.name != "worktrees" or found != gitdir.parent.parent
                or (gitdir / back).resolve() != dot):
            raise ValueError("the linked worktree's gitdir is not this worktree's own")
    else:
        raise ValueError(".git is neither a directory nor a gitdir file")
    if common is not None and found != Path(common).resolve():
        raise ValueError("the repository's Git common dir is not the run's source repository")
    return gitdir, found


def _source_head(gitdir: Path, common: Path) -> tuple[str, str]:
    """(commit, branch or "") of HEAD, read from ref files without running git."""
    name = "HEAD"
    for _ in range(5):
        base = gitdir if name == "HEAD" or name.startswith(_PER_WORKTREE_REFS) else common
        text = _git_text(base, name)
        if text is None:
            packed = _git_text(common, "packed-refs", 64 << 20) or ""
            text = next((line.split(" ", 1)[0] for line in packed.splitlines()
                         if line.endswith(" " + name) and not line.startswith(("#", "^"))), None)
        if text is None:
            raise ValueError(f"{name} does not resolve to a commit")
        if re.fullmatch(r"[0-9a-f]{40}", text):
            branch = name[len("refs/heads/"):] if name.startswith("refs/heads/") else ""
            return text, branch
        if not text.startswith("ref: ") or not _REFNAME.fullmatch(text[5:]):
            raise ValueError(f"{name} is not a SHA-1 commit or a symbolic ref")
        name = text[5:]
    raise ValueError("HEAD is a symbolic ref chain longer than 5")


# Run by `sh -c` in the preparation container: $1 the source object directory
# (mounted read-only, alone), $2 HEAD's commit, $3 its branch or "", $4 the
# byte budget, $5 the gitdir to create. Prints the bytes HEAD's objects use.
PREP_SCRIPT = r'''set -eu
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 LC_ALL=C HOME=/tmp
out=$5
stage=$(dirname "$out")/stage.git
git init -q --bare --template= "$stage"
mkdir -p "$stage/objects/info"
printf '%s\n' "$1" > "$stage/objects/info/alternates"
git --git-dir="$stage" rev-parse -q --verify "$2^{commit}" >/dev/null
used=$(git --git-dir="$stage" rev-list --objects --disk-usage "$2")
if [ "$used" -gt "$4" ]; then
  echo "HEAD's history uses $used bytes of objects (budget $4)" >&2
  exit 3
fi
git --git-dir="$stage" update-ref refs/heads/snapshot "$2"
git init -q --bare --template= --initial-branch="${3:-snapshot}" "$out"
git --git-dir="$out" -c protocol.file.allow=always -c fetch.unpackLimit=1 fetch -q --no-tags \
  --no-write-fetch-head --no-recurse-submodules "file://$stage" refs/heads/snapshot
if [ -n "$3" ]; then
  git --git-dir="$out" update-ref "refs/heads/$3" "$2"
else
  git --git-dir="$out" update-ref --no-deref HEAD "$2"
fi
git --git-dir="$out" read-tree "$2"
git --git-dir="$out" config core.bare false
rm -rf "$stage"
echo "$used"
'''


class CleanupUnconfirmed(IsolationUnavailable):
    """An owned container's absence could not be established: never a pass.

    ``name`` is the own UUID name, kept so the container can be reconciled.
    """

    def __init__(self, message: str, name: str = ""):
        super().__init__(message)
        self.name = name


# ── owned container lifecycle ──────────────────────────────────────────────
#
# Every container here (Git preparation, direct repository command, host
# launcher command) is created by its own UUID name and started separately:
# `docker create`, then `docker start -a`. Killing a Docker client does not
# cancel a request the daemon already holds, so a removal issued while a
# create is in flight can run first and the daemon then creates the
# container (measured on a `docker run` and on a `docker create` killed by
# KeyboardInterrupt: left `created`/`running` after their removal). So the
# create client is never killed by cancellation: it runs until it exits or
# CREATE_TIMEOUT. Its exit is an answer only when it carries the daemon's
# reply (`_create_answer`): rc 0 with the container ID (created), or rc > 0
# whose last stderr line is the CLI's rendering of the daemon's own error
# reply, "Error response from daemon: ..." (refused). Any other exit (the
# client killed by a signal, a transport EOF such as "error during connect:
# ... EOF", a nonzero rc with anything else) is NOT a refusal: the daemon may
# still be creating the container (measured: removal answered 404, then the
# create 201 about a second later), so it is handled like a create that never
# answered. A main-thread SIGINT (direct Ctrl-C) arriving while a
# client is spawned or a create is awaited is held (`_HeldInterrupt`) and
# delivered once the client's handle is recorded and the create has
# answered, before START, so a cancelled launch never starts its command.
# Launcher cancellation (the client socket) is checked at the same point.
# An unanswered create is unknown: its client is killed (on CREATE_TIMEOUT),
# the name removed, and the result is `CleanupUnconfirmed` naming it, since
# the daemon may still create it; nothing is started and `private_git`
# propagates it, so no repository command follows. Once a create has answered, `docker rm -f NAME` exiting 0
# is final (it is 0 when the name is already gone, and nothing re-creates
# it); a removal that does not succeed within REMOVE_DEADLINE is
# `CleanupUnconfirmed`. Removal waits for the start client to be killed and
# reaped first, and is itself protected from a held SIGINT.
CREATE_TIMEOUT = 30
REMOVE_DEADLINE = 30


class _HeldInterrupt:
    """Hold a main-thread SIGINT for the block; deliver it on exit.

    Only the Python-level handler is replaced (no signal mask is changed, so
    nothing is inherited by the clients spawned inside). Outside the main
    thread no signal is delivered as an exception and nothing is changed.
    """

    def __enter__(self):
        import signal
        import threading
        self.previous = self.pending = None
        if threading.current_thread() is threading.main_thread():
            current = signal.getsignal(signal.SIGINT)
            if callable(current):
                self.previous = signal.signal(signal.SIGINT, self._hold)
        return self

    def _hold(self, signum, frame):
        if self.pending is None:
            self.pending = (signum, frame)

    def __exit__(self, *exc):
        import signal
        if self.previous is not None:
            signal.signal(signal.SIGINT, self.previous)
            if self.pending is not None:
                self.previous(*self.pending)   # KeyboardInterrupt, by default
        return False


def _remove_owned(docker: str, name: str, answered: bool) -> None:
    deadline = time.monotonic() + REMOVE_DEADLINE
    while True:
        try:
            done = subprocess.run([docker, "rm", "-f", name], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=10)
            removed, why = done.returncode == 0, done.stderr.strip()[-300:]
        except subprocess.TimeoutExpired:
            removed, why = False, "docker rm did not answer within 10s"
        if removed and answered:
            return
        if removed or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    if not answered:
        raise CleanupUnconfirmed(f"owned container {name}: its create got no answer from the "
                                 "daemon, so the daemon may still create it after the removal; "
                                 "its absence is not confirmed", name)
    raise CleanupUnconfirmed(f"owned container {name} could not be removed within "
                             f"{REMOVE_DEADLINE}s: {why}", name)


_DAEMON_REFUSAL = re.compile(r"(?:docker: )?Error response from daemon: ")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


def _create_answer(returncode: int, stdout: str, stderr: str) -> str | None:
    """The daemon's answer carried by a create client's exit: created, refused or None.

    None (unknown) unless the exit carries that reply: a signal, a transport
    error or any other text is never taken as a refusal.
    """
    if returncode == 0 and _CONTAINER_ID.fullmatch(stdout.strip()):
        return "created"
    lines = [line for line in stderr.splitlines() if line.strip()]
    if returncode > 0 and lines and _DAEMON_REFUSAL.match(lines[-1]):
        return "refused"
    return None


@contextmanager
def owned_container(create: list[str], docker: str, name: str, *, cancelled=None,
                    text: bool = False):
    """Create ``name`` with the ``create`` argv, start it, yield its `start -a` client.

    ``cancelled`` (optional) is called after the create has answered and
    before START; it raises to cancel. Every exit kills and reaps the start
    client, then removes ``name`` (`_remove_owned`). A create the daemon
    refused raises `IsolationUnavailable` ("not created"); one without the
    daemon's answer ends in `CleanupUnconfirmed`.
    """
    answered = False
    sent = False
    proc = None
    try:
        with _HeldInterrupt():
            made = subprocess.Popen(create, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
            sent = True
            try:
                out, err = made.communicate(timeout=CREATE_TIMEOUT)
            except subprocess.TimeoutExpired:
                made.kill()
                made.communicate()
                raise IsolationUnavailable(f"owned container {name} was not created within "
                                           f"{CREATE_TIMEOUT}s")
            answer = _create_answer(made.returncode, out, err)
            answered = answer is not None
            if answer is None:
                raise IsolationUnavailable(f"owned container {name}: create client exited "
                                           f"{made.returncode} without the daemon's answer: "
                                           f"{err.strip()[-300:]}")
            if answer == "refused":
                raise IsolationUnavailable(f"owned container not created: {err.strip()[-300:]}")
        # A SIGINT held during the create was delivered above: START is never reached.
        if cancelled is not None:
            cancelled()
        with _HeldInterrupt():
            proc = subprocess.Popen([docker, "start", "-a", name], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text,
                                    start_new_session=True)
        yield proc
    finally:
        with _HeldInterrupt():
            try:
                if proc is not None:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=10)
                    proc.stdout.close()
                    proc.stderr.close()
            finally:
                if sent:
                    _remove_owned(docker, name, answered)


def _run_prep(create: list[str], docker: str, name: str, conn=None) -> str:
    """Run the preparation container (`owned_container`); its stdout.

    ``create`` is its `docker create` argv. GIT_PREP_DEADLINE is measured
    from entry, so the create's own time counts against it, but the create
    is bounded only by CREATE_TIMEOUT and is not interrupted by the deadline
    or by cancellation. Cancellation (``conn``, the launcher's client socket,
    becoming readable, as in the launcher's own wait) is checked once the
    create has answered and while `docker start -a` runs; the deadline only
    while it runs.
    """
    import select
    deadline = time.monotonic() + GIT_PREP_DEADLINE

    def cancelled():
        if conn is not None and select.select([conn], [], [], 0)[0]:
            raise PreparationCancelled("client disconnected during Git metadata preparation")
    with owned_container(create, docker, name, cancelled=cancelled, text=True) as proc:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Git metadata preparation exceeded its "
                                       f"{GIT_PREP_DEADLINE}s deadline")
                cancelled()
        if proc.returncode:
            raise RuntimeError(f"Git metadata preparation failed: {stderr.strip()[-300:]}")
        return stdout


def private_git(repo: Path, dest: Path, common: Path | None = None, *, image: str,
                docker: str = "docker", conn=None) -> tuple[list[str], dict]:
    """Docker mounts giving ``repo`` its private Git metadata, built in ``dest``.

    Raises `PreparationCancelled` (the launch must not go on),
    `CleanupUnconfirmed` (the preparation container may remain: nothing
    goes on) and `GitEntryRefused` (a `.git` that cannot be covered: the
    command must not run). Otherwise, when no metadata can be provided,
    ``info`` says why and the mounts still cover the repository's own `.git`
    (an empty directory over a directory, a pointer to an unmounted path over
    a file), so git in the container answers "not a git repository". A
    repository without any `.git` entry gets nothing and stays one.
    """
    dot = Path(repo) / ".git"
    try:
        mode = os.lstat(dot).st_mode
    except FileNotFoundError:
        return [], {"provided": False, "reason": "repository has no .git"}
    except OSError as exc:
        raise GitEntryRefused(f"repository .git cannot be inspected ({exc}); "
                              "refusing to run against it") from exc
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise GitEntryRefused(f"repository .git is neither a directory nor a regular file "
                              f"(mode {stat.filemode(mode)}); it cannot be normalized or "
                              "covered, so the command is refused")
    own_dir = stat.S_ISDIR(mode)
    private = dest / "git"
    try:
        gitdir, source_common = _source_gitdirs(Path(repo), common)
        sha, branch = _source_head(gitdir, source_common)
        objects = source_common / "objects"
        if not stat.S_ISDIR(os.lstat(objects).st_mode):
            raise ValueError("the source object directory is not a real directory")
        budget = GIT_META_MAX_BYTES
        name = "aitelier-git-" + uuid.uuid4().hex
        used = _run_prep(
            [docker, "create", "--rm", "--init", "--network", "none",
             "--cpus", "2", "--memory", "2g", "--pids-limit", "512",
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
             "--read-only", "--tmpfs", "/tmp:size=16m", "--ulimit", f"fsize={budget}:{budget}",
             "--user", f"{os.getuid()}:{os.getgid()}", "--name", name,
             "-v", f"{objects}:{objects}:ro", "-v", f"{dest}:{dest}:rw",
             "--entrypoint", "sh", image, "-c", PREP_SCRIPT, "prep",
             str(objects), sha, branch, str(budget), str(private)],
            docker, name, conn)
        size = sum(p.stat().st_size for p in private.rglob("*")
                   if p.is_file() and not p.is_symlink())
        if size > budget:
            raise RuntimeError(f"private metadata is {size} bytes (> {budget})")
        info = {"provided": True, "head": sha, "branch": branch,
                "object_bytes": int(used.split()[-1]), "metadata_bytes": size}
    except (PreparationCancelled, CleanupUnconfirmed):
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        shutil.rmtree(private, ignore_errors=True)
        info = {"provided": False, "reason": str(exc)[:500]}
        if own_dir:
            private.mkdir(mode=0o700)
    if own_dir:
        return ["-v", f"{private}:{dot}:ro"], info
    (dest / "pointer").write_text(f"gitdir: {GIT_CONTAINER_DIR}\n")
    if not info["provided"]:
        return ["-v", f"{dest / 'pointer'}:{dot}:ro"], info   # points at nothing mounted
    return ["-v", f"{private}:{GIT_CONTAINER_DIR}:ro",
            "-v", f"{dest / 'pointer'}:{dot}:ro"], info


def _node_unavailable(result: dict) -> None:
    """The entry found no pinned npm: nothing was measured."""
    if result.get("unavailable"):
        raise IsolationUnavailable(str(result["unavailable"])[:500])


@contextmanager
def _slot(root=None):
    from core import datadir
    root = Path(root) if root is not None else datadir.aitelier_home() / "cpu-test-slots"
    root.mkdir(parents=True, exist_ok=True)
    for number in range(4):
        stream = (root / str(number)).open("a")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            continue
        try:
            yield
        finally:
            stream.close()
        return
    raise IsolationUnavailable("all four CPU test execution slots are occupied")


def execute(*args, **kwargs):
    socket_path = os.environ.get(HOST_LAUNCHER_ENV, "")
    if socket_path:
        return _launch_via_host(socket_path, *args, **kwargs)
    with _slot():
        return _execute(*args, **kwargs)


def _launch_via_host(socket_path: str, repo: Path, args: list[str], timeout: int, *,
                     writable_dirs=(), relay_socket: str = "", import_module: str = "",
                     pytest_timeout: bool = False, report_dir: str = "", run_id: str = "",
                     profile: str = "", workdir: str = "", npm_cache: str = "",
                     coding_grant: dict | None = None):
    """Send one validated launch request to the trusted host launcher.

    No in-backend Docker or pytest fallback: any failure is reported as
    ``IsolationUnavailable`` so callers record an honest non-pass.
    """
    from core.repository_host_launcher import MAX_REQUEST_BYTES
    request = {"op": "launch", "repo": str(repo), "args": args, "timeout": timeout,
               "writable_dirs": [str(d) for d in writable_dirs],
               "relay_socket": relay_socket, "import_module": import_module,
               "pytest_timeout": pytest_timeout, "report_dir": report_dir, "run_id": run_id}
    if profile == "coding_shell":
        request.update(profile=profile, coding_grant=coding_grant)
    elif profile:
        # Only a Node request carries these, so a launcher without the Node
        # profiles refuses it and every pytest/gate request stays byte-identical.
        request.update(profile=profile, workdir=str(workdir), npm_cache=str(npm_cache))
    payload = (json.dumps(request) + "\n").encode()
    if len(payload) > MAX_REQUEST_BYTES:
        raise IsolationUnavailable("launch request exceeds host launcher limit")
    import socket as _socket
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as client:
            client.settimeout(timeout + 60)
            client.connect(socket_path)
            client.sendall(payload)
            chunks = []
            while b"\n" not in (chunks[-1] if chunks else b""):
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks).split(b"\n")[0]
    except OSError as exc:
        raise IsolationUnavailable(
            f"host CPU launcher unavailable at {socket_path}: {exc}") from exc
    try:
        response = json.loads(raw)
    except ValueError as exc:
        raise IsolationUnavailable("host CPU launcher returned no result") from exc
    if not isinstance(response, dict):
        raise IsolationUnavailable("host CPU launcher returned a malformed response")
    if not response.get("ok"):
        refused = IsolationUnavailable(f"host CPU launcher refused: {response.get('error', 'unknown')}")
        if profile == "coding_shell":
            refused.physical_quiet = response.get("physical_quiet") is True
        raise refused
    result = response.get("result")
    if not isinstance(result, dict) or type(result.get("returncode")) is not int or not all(
            isinstance(result.get(k), str) for k in ("stdout", "stderr")):
        raise IsolationUnavailable("host CPU launcher returned a malformed result")
    _node_unavailable(result)
    if profile == "coding_shell":
        if response.get("physical_quiet") is not True:
            raise IsolationUnavailable("coding shell physical settlement is unknown")
        completed = subprocess.CompletedProcess(args, result["returncode"], result["stdout"], result["stderr"])
        completed.timed_out = result.get("timed_out") is True
        completed.output_truncated = result.get("output_truncated") is True
        completed.physical_quiet = True
        completed.git = response.get("git")
        return completed
    if result.get("timed_out"):
        raise subprocess.TimeoutExpired(args, timeout,
                                        result.get("stdout"), result.get("stderr"))
    completed = subprocess.CompletedProcess(args, result["returncode"],
                                            result["stdout"], result["stderr"])
    completed.import_error = result.get("import_error", "")
    completed.git = response.get("git")
    completed.execution_identity = result.get("execution_identity")
    return completed



def _execute(repo: Path, args: list[str], timeout: int, *,
            writable_dirs=(), relay_socket: str = "", import_module: str = "",
            pytest_timeout: bool = False, report_dir: str = "", run_id: str = "",
            profile: str = "", workdir: str = "", npm_cache: str = "") -> subprocess.CompletedProcess:
    if profile == "coding_shell":
        raise IsolationUnavailable("coding shell requires a host-bound grant and the owned host launcher")
    if profile == LINT_PROFILE:
        raise IsolationUnavailable("JavaScript lint requires the existing owned host launcher")
    docker = shutil.which("docker")
    if not docker:
        raise IsolationUnavailable("Docker CPU execution facility is unavailable; local execution refused")
    repo = Path(repo).resolve()
    name = "aitelier-cpu-" + uuid.uuid4().hex
    entry = Path(__file__).with_name("repository_executor_entry.py")
    git_dir, git_info = None, None
    if profile:
        from core import datadir
        node = validate_node_request(
            node_report_root(datadir.aitelier_home(), run_id),
            {"profile": profile, "writable_dirs": [str(d) for d in writable_dirs],
             "workdir": str(workdir), "npm_cache": str(npm_cache), "args": list(args),
             "relay_socket": relay_socket, "import_module": import_module,
             "pytest_timeout": pytest_timeout, "report_dir": report_dir},
            os.environ.get(NODE_FETCH_NETWORK_ENV, ""), os.environ.get(NODE_EXECUTOR_IMAGE_ENV, ""))
        args, payload = node["args"], node_payload(node, timeout)
        network, mounts = node["network"], node_mount_args(node)
        workdir_arg, image = node["workdir"], node["image"]
    else:
        payload = {"args": args, "timeout": timeout, "repo": str(repo),
                   "relay_socket": relay_socket, "import_module": import_module,
                   "pytest_timeout": pytest_timeout, "report_dir": report_dir}
        from core import datadir
        git_root = datadir.aitelier_home() / "cpu-git"
        git_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        image = os.environ.get("AITELIER_TEST_IMAGE", "aitelier:latest")
        git_dir = Path(tempfile.mkdtemp(prefix="git-", dir=git_root))
    try:
        if git_dir is not None:
            git_mounts, git_info = private_git(repo, git_dir, image=image, docker=docker)
            network, mounts = "none", ["-v", f"{repo}:{repo}:ro", *git_mounts]
            workdir_arg = str(repo)
        command = [docker, "create", "--rm", "--init", "--network", network,
                   "--cpus", "2", "--memory", "2g", "--pids-limit", "512",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--user", f"{os.getuid()}:{os.getgid()}", "--name", name,
                   *mounts, "-v", f"{entry}:/executor.py:ro",
                   "-w", workdir_arg]
        for directory in (() if profile else writable_dirs):
            path = str(Path(directory).resolve())
            command += ["-v", f"{path}:{path}:rw"]
        if relay_socket and not profile:
            command += ["-v", f"{relay_socket}:{relay_socket}:ro"]
        command += ["--entrypoint", "python3", image,
                    "/executor.py", json.dumps(payload)]
        # The create's time counts against the launch's bound (`owned_container`).
        deadline = time.monotonic() + timeout + 15
        # UUID names are created by this invocation only. Never prune or kill peers.
        with owned_container(command, docker, name, text=True) as proc:
            stdout, stderr = proc.communicate(timeout=deadline - time.monotonic())
        if proc.returncode != 0:
            raise IsolationUnavailable(f"isolated container exited {proc.returncode}: {stderr[-2000:]}")
        try:
            result = json.loads(stdout)
        except ValueError as exc:
            raise IsolationUnavailable("isolated executor returned no result") from exc
        _node_unavailable(result)
        if result.get("timed_out"):
            raise subprocess.TimeoutExpired(args, timeout, result.get("stdout"), result.get("stderr"))
        completed = subprocess.CompletedProcess(args, result["returncode"],
                                                result["stdout"], result["stderr"])
        completed.import_error = result.get("import_error", "")
        completed.git = git_info
        return completed
    finally:
        if git_dir is not None:
            shutil.rmtree(git_dir, ignore_errors=True)
