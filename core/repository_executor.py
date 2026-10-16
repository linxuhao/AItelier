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
import uuid

HOST_LAUNCHER_ENV = "AITELIER_HOST_LAUNCHER_SOCKET"

class IsolationUnavailable(RuntimeError):
    pass


class NodeRefused(IsolationUnavailable):
    """A Node request outside the closed profiles: nothing was executed."""


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


def read_regular(root: Path, rel, cap: int) -> bytes | None:
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
# (`rev-list --disk-usage`), no file written may exceed it (RLIMIT_FSIZE; the
# fetch keeps one pack), and the finished metadata must fit. The whole
# preparation has one deadline; on it, or on cancellation, its container is
# removed.
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


def _run_prep(command: list[str], docker: str, name: str, conn=None) -> str:
    """Run the preparation container within GIT_PREP_DEADLINE; stdout.

    ``conn`` (the launcher's client socket) becoming readable is cancellation,
    as in the launcher's own wait. Any exit from here removes the container
    by its own name: killing the `docker run` client alone leaves it running.
    """
    import select
    import time
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    deadline = time.monotonic() + GIT_PREP_DEADLINE
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Git metadata preparation exceeded its "
                                       f"{GIT_PREP_DEADLINE}s deadline")
                if conn is not None and select.select([conn], [], [], 0)[0]:
                    raise RuntimeError("client disconnected during Git metadata preparation")
        if proc.returncode:
            raise RuntimeError(f"Git metadata preparation failed: {stderr.strip()[-300:]}")
        return stdout
    finally:
        try:
            subprocess.run([docker, "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def private_git(repo: Path, dest: Path, common: Path | None = None, *, image: str,
                docker: str = "docker", conn=None) -> tuple[list[str], dict]:
    """Docker mounts giving ``repo`` its private Git metadata, built in ``dest``.

    Never raises: when no metadata can be provided, ``info`` says why, a
    direct repository's own `.git` is still masked by an empty directory and
    a linked worktree's pointer keeps leading nowhere, so git in the
    container answers "not a git repository". A repository without `.git`
    gets nothing and stays one.
    """
    try:
        own_dir = stat.S_ISDIR(os.lstat(Path(repo) / ".git").st_mode)
    except FileNotFoundError:
        return [], {"provided": False, "reason": "repository has no .git"}
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
            [docker, "run", "--rm", "--init", "--network", "none",
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
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        shutil.rmtree(private, ignore_errors=True)
        info = {"provided": False, "reason": str(exc)[:500]}
        if not own_dir:
            return [], info
        private.mkdir(mode=0o700)
    if own_dir:
        return ["-v", f"{private}:{Path(repo) / '.git'}:ro"], info
    (dest / "pointer").write_text(f"gitdir: {GIT_CONTAINER_DIR}\n")
    return ["-v", f"{private}:{GIT_CONTAINER_DIR}:ro",
            "-v", f"{dest / 'pointer'}:{Path(repo) / '.git'}:ro"], info


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
                     profile: str = "", workdir: str = "", npm_cache: str = ""):
    """Send one validated launch request to the trusted host launcher.

    No in-backend Docker or pytest fallback: any failure is reported as
    ``IsolationUnavailable`` so callers record an honest non-pass.
    """
    from core.repository_host_launcher import MAX_REQUEST_BYTES
    request = {"op": "launch", "repo": str(repo), "args": args, "timeout": timeout,
               "writable_dirs": [str(d) for d in writable_dirs],
               "relay_socket": relay_socket, "import_module": import_module,
               "pytest_timeout": pytest_timeout, "report_dir": report_dir, "run_id": run_id}
    if profile:
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
        raise IsolationUnavailable(f"host CPU launcher refused: {response.get('error', 'unknown')}")
    result = response.get("result")
    if not isinstance(result, dict) or type(result.get("returncode")) is not int or not all(
            isinstance(result.get(k), str) for k in ("stdout", "stderr")):
        raise IsolationUnavailable("host CPU launcher returned a malformed result")
    _node_unavailable(result)
    if result.get("timed_out"):
        raise subprocess.TimeoutExpired(args, timeout,
                                        result.get("stdout"), result.get("stderr"))
    completed = subprocess.CompletedProcess(args, result["returncode"],
                                            result["stdout"], result["stderr"])
    completed.import_error = result.get("import_error", "")
    completed.git = response.get("git")
    return completed



def _execute(repo: Path, args: list[str], timeout: int, *,
            writable_dirs=(), relay_socket: str = "", import_module: str = "",
            pytest_timeout: bool = False, report_dir: str = "", run_id: str = "",
            profile: str = "", workdir: str = "", npm_cache: str = "") -> subprocess.CompletedProcess:
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
            git_mounts, git_info = private_git(repo, git_dir, image=image, docker=docker)
        except BaseException:
            # Cancelled while preparing: the launch's own finally is not reached yet.
            shutil.rmtree(git_dir, ignore_errors=True)
            raise
        network, mounts = "none", ["-v", f"{repo}:{repo}:ro", *git_mounts]
        workdir_arg = str(repo)
    command = [docker, "run", "--rm", "--init", "--network", network,
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
    proc = None
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        stdout, stderr = proc.communicate(timeout=timeout + 15)
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
        # UUID names are created by this invocation only. Never prune or kill peers.
        try:
            subprocess.run([docker, "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            if git_dir is not None:
                shutil.rmtree(git_dir, ignore_errors=True)
