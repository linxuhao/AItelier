"""The one backend Git boundary: repository programs never run with backend authority.

Every privileged backend consumer of a project repository — the Source
repository panel (status, commit, remote/branch mutations, bootstrap) and run
isolation (provisioning, validation, reaping, removal) — reaches Git through
this module. Untrusted bytes are delivered back into Source by runs, so a
repository's own configuration, attributes and hook directory are DATA here,
never instructions:

* the environment is built from nothing: no inherited ``GIT_*`` routing,
  object, index or ``GIT_CONFIG_*`` injection, and global/system configuration
  are suppressed; a fixed command-line profile turns hooks, fsmonitor, pagers,
  signing, external diff, alternate-refs commands, credential helpers and
  lazy fetch off;
* only a closed set of metadata/object/ref subcommands is accepted. Native
  working-tree commands (``status``, ``add``, ``diff``, ``checkout``,
  ``reset``, ``pull``, ``commit``) are not in it, because each of them can run
  a configured clean/smudge filter, fsmonitor hook or commit hook;
* working-file state is read and written in Python through no-follow paths,
  compared against raw blobs and a raw index, and a commit is a private index,
  ``write-tree``, ``commit-tree`` and a compare-and-swap ``update-ref`` with
  index settlement under an owned ``index.lock``;
* config inspection and native operations on one repository serialise on a
  short kernel lock keyed by its validated common Git directory. Nested
  trusted callers in the same thread reuse the held lock.

What cannot be done with that closed set and an allowed transport is refused
explicitly (``GitRefused``); nothing falls back to the callback-capable
command. A shell running ``git`` in a checkout by hand, or an out-of-band edit
of repository configuration, is outside any process boundary and stays an
explicit limit.
"""

from __future__ import annotations

import errno
import hashlib
import os
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path


class GitRefused(RuntimeError):
    """A requested Git operation is unsupported or unsafe inside the closed profile."""


_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_GIT = shutil.which("git", path=_SAFE_PATH) or "git"
_HOME = "/nonexistent-aitelier-closed-git-home"

# Command-line configuration is read after every repository file, so these win
# over anything a checkout carries. `credential.helper=` empties the helper
# list (including URL-scoped repository helpers); transport re-adds only the
# host's own helper after it.
_FIXED_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "core.pager=cat",
    "core.askPass=",
    "core.alternateRefsCommand=",
    "core.useReplaceRefs=false",
    "credential.helper=",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "log.showSignature=false",
    "diff.external=",
    "gc.auto=0",
    "maintenance.auto=false",
    "fetch.recurseSubmodules=false",
    "fetch.writeCommitGraph=false",
    "submodule.recurse=false",
    "push.recurseSubmodules=no",
    "protocol.allow=never",
)

_TRANSPORT_PASSTHROUGH = (
    "AITELIER_GITHUB_TOKEN_FILE", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
)

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
ZERO_SHA = "0" * 40


def closed_env(*, protocols: str = "", transport: bool = False) -> dict:
    """A Git environment built from nothing inherited that can route or configure Git."""
    env = {
        "PATH": _SAFE_PATH,
        "HOME": _HOME,
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_EDITOR": ":",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ALLOW_PROTOCOL": protocols,
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_ADVICE": "0",
    }
    if transport:
        for name in _TRANSPORT_PASSTHROUGH:
            if os.environ.get(name):
                env[name] = os.environ[name]
    return env


# ── the closed command set ───────────────────────────────────────────

_METADATA = frozenset({
    "rev-parse", "symbolic-ref", "show-ref", "cat-file",
    "ls-tree", "ls-files", "log", "rev-list", "merge-base", "config",
    "hash-object", "mktree", "write-tree", "commit-tree", "update-ref",
    "worktree", "init", "remote", "check-ref-format", "read-tree",
})
_TRANSPORT = frozenset({"fetch", "push", "clone"})
# Options that make a metadata reader compute or render content through
# configured programs (textconv, external diff, signature verification).
_RENDERING = ("-p", "-u", "--patch", "--stat", "--numstat", "--shortstat",
              "--ext-diff", "--textconv", "--show-signature", "--follow", "-L",
              "--check", "--word-diff", "--color-moved", "--function-context",
              "-G", "-S", "--pickaxe", "--dirstat", "--summary", "--raw",
              "--name-status", "--name-only", "--cc", "-c", "-m")


def _validate(args: tuple[str, ...], *, transport: bool) -> None:
    if not args:
        raise GitRefused("empty Git command")
    sub = args[0]
    rest = args[1:]
    if sub in _TRANSPORT:
        if not transport:
            raise GitRefused(f"git {sub} is a transport; use the closed transport entry")
        return
    if sub not in _METADATA:
        raise GitRefused(
            f"git {sub} is not in the closed backend profile: it can run "
            f"repository-configured programs or convert working files")
    if sub in ("log", "rev-list"):
        for a in rest:
            if a in _RENDERING or any(a.startswith(p + "=") for p in _RENDERING if p.startswith("--")):
                raise GitRefused(f"git {sub} {a} renders content through configurable programs")
    elif sub == "hash-object":
        if "--no-filters" not in rest or any(a.startswith("--path") for a in rest):
            raise GitRefused("git hash-object must hash raw bytes (--no-filters, no --path)")
    elif sub == "commit-tree":
        if "--no-gpg-sign" not in rest or any(a.startswith("-S") or a == "--gpg-sign" for a in rest):
            raise GitRefused("git commit-tree must not sign")
    elif sub == "read-tree":
        if any(a in ("-u", "--reset", "--merge", "-m") or a.startswith("--recurse") for a in rest):
            raise GitRefused("git read-tree may only populate a private index")
    elif sub == "ls-files":
        if any(a in ("-m", "--modified", "-d", "--deleted", "--debug", "--eol") for a in rest):
            raise GitRefused("git ls-files may only list index/untracked names")
    elif sub == "worktree":
        verb = rest[0] if rest else ""
        if verb == "add":
            if "--no-checkout" not in rest:
                raise GitRefused("worktree registration must be --no-checkout")
        elif verb == "list":
            pass
        elif verb == "remove":
            if any(a in ("-f", "--force") for a in rest):
                raise GitRefused("forced worktree removal is never used")
        else:
            raise GitRefused(f"git worktree {verb} is not in the closed profile")
    elif sub == "init":
        if not any(a.startswith("--template=") for a in rest):
            raise GitRefused("git init must not copy a template directory")
    elif sub == "remote":
        verb = rest[0] if rest else ""
        if verb not in ("", "get-url", "add", "set-url", "-v"):
            raise GitRefused(f"git remote {verb} is not in the closed profile")
        if verb == "add" and any(a in ("-f", "--fetch") for a in rest):
            raise GitRefused("git remote add --fetch is a transport")
    elif sub == "config":
        if not any(a in ("--list", "-l", "--get", "--get-all", "--get-regexp") for a in rest):
            raise GitRefused("backend configuration is only read as data here")


def _argv(args, *, git_dir=None, work_tree=None, extra_config=()) -> list[str]:
    argv = [_GIT, "--no-pager", "--no-replace-objects"]
    for item in _FIXED_CONFIG + tuple(extra_config):
        argv += ["-c", item]
    if git_dir is not None:
        argv.append(f"--git-dir={git_dir}")
    if work_tree is not None:
        argv.append(f"--work-tree={work_tree}")
    return argv + list(args)


def run(cwd, *args: str, check: bool = False, text: bool = True, input=None,
        stdin=None, git_dir=None, work_tree=None, index_file=None,
        identity: dict | None = None, timeout: float = 300,
        _transport: tuple[str, tuple[str, ...]] | None = None) -> subprocess.CompletedProcess:
    """Run ONE closed-profile Git command. Raises GitRefused before running anything else."""
    _validate(tuple(args), transport=_transport is not None)
    if _transport is not None:
        protocols, extra = _transport
        env = closed_env(protocols=protocols, transport=True)
    else:
        extra = ()
        env = closed_env()
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    if identity:
        env.update(identity)
    return subprocess.run(
        _argv(args, git_dir=git_dir, work_tree=work_tree, extra_config=extra),
        cwd=str(cwd), capture_output=True, text=text, input=input,
        stdin=stdin, env=env, check=check, timeout=timeout)


def out(cwd, *args: str, **kw) -> str:
    """stdout of a closed command, RuntimeError(stderr) on failure."""
    res = run(cwd, *args, **kw)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or f"git {args[0]} failed").strip())
    return res.stdout.strip() if isinstance(res.stdout, str) else res.stdout


# ── repository identity and its kernel lock ──────────────────────────

@dataclass(frozen=True)
class Repo:
    top: Path
    git_dir: Path
    common_dir: Path


def discover(path) -> Repo:
    """The working tree, own git dir and common dir of the repository at *path*."""
    p = Path(path)
    if not p.is_dir():
        raise RuntimeError(f"{p} is not a directory")
    res = run(p, "rev-parse", "--path-format=absolute", "--show-toplevel",
              "--absolute-git-dir", "--git-common-dir")
    lines = res.stdout.splitlines() if res.returncode == 0 else []
    if len(lines) != 3 or not all(lines):
        raise RuntimeError("Not a git repository")
    return Repo(Path(lines[0]), Path(lines[1]), Path(os.path.realpath(lines[2])))


def common_identity(path) -> tuple[str, int, int]:
    """Validated identity of the shared Git directory: real path plus inode."""
    common = discover(path).common_dir
    st = os.stat(common)
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"Git common directory {common} is not a directory")
    return str(common), st.st_dev, st.st_ino


_held = threading.local()


@contextmanager
def repo_lock(path):
    """Serialise config inspection and native operations on one repository.

    Keyed by the real common Git directory and its inode, so a symlinked alias,
    a subdirectory and a linked worktree all contend on the same kernel lock.
    Re-entrant within one thread: nested trusted helpers reuse the held lock.
    """
    import fcntl
    from core import datadir
    key = common_identity(path)
    held = getattr(_held, "keys", None)
    if held is None:
        held = _held.keys = {}
    if key in held:
        held[key] += 1
        try:
            yield key
        finally:
            held[key] -= 1
        return
    root = datadir.isolation_locks_dir() / "git-common"
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{key[1]}:{key[2]}:{key[0]}".encode()).hexdigest()[:32]
    fd = os.open(root / f"{digest}.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        held[key] = 1
        try:
            yield key
        finally:
            del held[key]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── configuration as data ────────────────────────────────────────────

def config_list(path, *, git_dir=None) -> list[tuple[str, str]]:
    """Effective repository configuration (no global/system), read as data."""
    res = run(path, "config", "--list", "-z", "--show-scope", git_dir=git_dir)
    if res.returncode not in (0, 1):
        raise RuntimeError(f"cannot read repository configuration: {res.stderr.strip()[:200]}")
    items = []
    fields = res.stdout.split("\0")
    for scope, chunk in zip(fields[0::2], fields[1::2]):
        if scope == "command":
            continue  # the closed profile's own overrides, not repository data
        # Git prints section/variable names lowercased and subsections as written.
        key, _, value = chunk.partition("\n")
        items.append((key, value))
    return items


def _filter_drivers(cfg) -> set[str]:
    out_ = set()
    for key, value in cfg:
        if key.startswith("filter.") and key.rsplit(".", 1)[-1] in ("clean", "smudge", "process") and value:
            out_.add(key[len("filter."):].rsplit(".", 1)[0])
    return out_


_ATTR_TOKEN = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)(?:=(?P<value>\S*))?$")


def attribute_transforms(texts) -> dict:
    """Which conversions attribute files can request: filter drivers, eol, ident, encoding."""
    kinds = {"filters": set(), "eol": False, "ident": False, "encoding": False}
    for text in texts:
        for line in text.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for token in line.split()[1:]:
                if token.startswith(("-", "!")):
                    continue
                m = _ATTR_TOKEN.match(token)
                if not m:
                    continue
                name, value = m.group("name"), m.group("value")
                if name == "filter" and value:
                    kinds["filters"].add(value)
                elif name in ("text", "eol", "crlf"):
                    kinds["eol"] = True
                elif name == "ident":
                    kinds["ident"] = True
                elif name == "working-tree-encoding":
                    kinds["encoding"] = True
    return kinds


@dataclass
class TransformProfile:
    active_filters: set = field(default_factory=set)
    eol: bool = False
    ident: bool = False
    encoding: bool = False

    def notes(self) -> list[str]:
        notes = []
        if self.active_filters:
            notes.append("configured filter drivers apply to paths: "
                         + ", ".join(sorted(self.active_filters)))
        if self.eol:
            notes.append("text/eol conversion is configured; working bytes are compared raw")
        if self.ident:
            notes.append("ident expansion is configured; working bytes are compared raw")
        if self.encoding:
            notes.append("working-tree-encoding is configured; working bytes are compared raw")
        return notes


def transform_profile(repo: Repo, cfg, attribute_texts) -> TransformProfile:
    texts = list(attribute_texts)
    for p in (repo.common_dir / "info" / "attributes",):
        data = _read_regular(p)
        if data is not None:
            texts.append(data)
    for key, value in cfg:
        if key == "core.attributesfile" and value:
            data = _read_regular(Path(os.path.expanduser(value)) if value.startswith("~") else
                                 (repo.top / value if not os.path.isabs(value) else Path(value)))
            if data is not None:
                texts.append(data)
    kinds = attribute_transforms(texts)
    autocrlf = next((v for k, v in reversed(cfg) if k == "core.autocrlf"), "false").lower()
    return TransformProfile(
        active_filters=kinds["filters"] & _filter_drivers(cfg),
        eol=kinds["eol"] or autocrlf in ("true", "input", "1", "yes", "on"),
        ident=kinds["ident"], encoding=kinds["encoding"])


def _read_regular(path: Path, limit: int = 1 << 20) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit)
    finally:
        os.close(fd)


def commit_content_refusal(profile: TransformProfile, rel: str, data: bytes) -> str | None:
    """Why raw bytes of *rel* cannot be committed as Git's own conversion would store them."""
    if profile.encoding:
        return f"{rel}: working-tree-encoding conversion is not supported by the raw commit"
    if profile.eol and b"\r\n" in data and b"\0" not in data[:8000]:
        return (f"{rel}: CRLF text under text/eol conversion would be normalised by Git; "
                f"the raw commit refuses rather than store different bytes")
    if profile.ident and b"$Id" in data:
        return f"{rel}: ident expansion would be collapsed by Git; not supported by the raw commit"
    return None


# ── the index, as data ───────────────────────────────────────────────

S_IFGITLINK = 0o160000


@dataclass
class IndexEntry:
    path: str
    mode: int
    sha: str
    stage: int = 0
    ctime_s: int = 0
    ctime_ns: int = 0
    mtime_s: int = 0
    mtime_ns: int = 0
    dev: int = 0
    ino: int = 0
    uid: int = 0
    gid: int = 0
    size: int = 0
    assume_valid: bool = False
    skip_worktree: bool = False
    intent_to_add: bool = False

    @property
    def key(self) -> tuple[bytes, int]:
        return os.fsencode(self.path), self.stage


@dataclass
class Index:
    entries: list
    version: int = 2
    mtime_ns: int = 0
    raw_sha256: str = ""

    def stage0(self) -> dict:
        return {e.path: e for e in self.entries if e.stage == 0}

    def unmerged_paths(self) -> set:
        return {e.path for e in self.entries if e.stage}

    def all_paths(self) -> set:
        return {e.path for e in self.entries}


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    c = buf[pos]
    pos += 1
    val = c & 127
    while c & 128:
        val += 1
        c = buf[pos]
        pos += 1
        val = (val << 7) + (c & 127)
    return val, pos


def parse_index(data: bytes) -> Index:
    if len(data) < 32 or data[:4] != b"DIRC":
        raise RuntimeError("index is not a Git DIRC index")
    if hashlib.sha1(data[:-20]).digest() != data[-20:]:
        raise RuntimeError("index checksum mismatch (sha1 indexes only)")
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3, 4):
        raise RuntimeError(f"index version {version} is unsupported")
    pos = 12
    entries = []
    prev = b""
    end = len(data) - 20
    for _ in range(count):
        start = pos
        (cs, cn, ms, mn, dev, ino, mode, uid, gid, size) = struct.unpack(">10I", data[pos:pos + 40])
        sha = data[pos + 40:pos + 60].hex()
        flags, = struct.unpack(">H", data[pos + 60:pos + 62])
        pos += 62
        ext = 0
        if flags & 0x4000:
            if version < 3:
                raise RuntimeError("extended index flags in a version-2 index")
            ext, = struct.unpack(">H", data[pos:pos + 2])
            pos += 2
        if version == 4:
            strip, pos = _varint(data, pos)
            nul = data.index(b"\0", pos)
            name = prev[:len(prev) - strip] + data[pos:nul]
            pos = nul + 1
        else:
            nul = data.index(b"\0", pos)
            name = data[pos:nul]
            pos = start + ((pos - start + len(name) + 8) & ~7)
        prev = name
        if stat.S_IFMT(mode) == stat.S_IFDIR:
            raise RuntimeError("sparse-index directory entries are unsupported")
        entries.append(IndexEntry(
            path=os.fsdecode(name), mode=mode, sha=sha, stage=(flags >> 12) & 3,
            ctime_s=cs, ctime_ns=cn, mtime_s=ms, mtime_ns=mn, dev=dev, ino=ino,
            uid=uid, gid=gid, size=size, assume_valid=bool(flags & 0x8000),
            skip_worktree=bool(ext & 0x4000), intent_to_add=bool(ext & 0x2000)))
    while pos + 8 <= end:
        sig = data[pos:pos + 4]
        size, = struct.unpack(">I", data[pos + 4:pos + 8])
        if sig in (b"link", b"sdir"):
            raise RuntimeError("split or sparse indexes are unsupported by the closed profile")
        pos += 8 + size
    return Index(entries=entries, version=version,
                 raw_sha256=hashlib.sha256(data).hexdigest())


def read_index(git_dir) -> Index:
    """The index of a repository's own git dir; absent means empty."""
    path = Path(git_dir) / "index"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return Index(entries=[], raw_sha256="")
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError("index is not a regular file")
        idx = parse_index(fh.read())
    idx.mtime_ns = st.st_mtime_ns
    return idx


def serialize_index(entries) -> bytes:
    entries = sorted(entries, key=lambda e: e.key)
    version = 3 if any(e.skip_worktree or e.intent_to_add for e in entries) else 2
    body = bytearray(b"DIRC" + struct.pack(">II", version, len(entries)))
    for e in entries:
        name = os.fsencode(e.path)
        ext = (0x4000 if e.skip_worktree else 0) | (0x2000 if e.intent_to_add else 0)
        flags = min(len(name), 0xFFF) | (e.stage << 12) | (0x8000 if e.assume_valid else 0) \
            | (0x4000 if ext else 0)
        start = len(body)
        body += struct.pack(">10I", *(v & 0xFFFFFFFF for v in (
            e.ctime_s, e.ctime_ns, e.mtime_s, e.mtime_ns, e.dev, e.ino,
            e.mode, e.uid, e.gid, e.size)))
        body += bytes.fromhex(e.sha)
        body += struct.pack(">H", flags)
        if ext:
            body += struct.pack(">H", ext)
        body += name
        body += b"\0" * (((len(body) - start + 8) & ~7) - (len(body) - start))
    body += hashlib.sha1(bytes(body)).digest()
    return bytes(body)


def entry_from_stat(path: str, mode: int, sha: str, st: os.stat_result | None) -> IndexEntry:
    if st is None:
        return IndexEntry(path=path, mode=mode, sha=sha)
    return IndexEntry(
        path=path, mode=mode, sha=sha,
        ctime_s=int(st.st_ctime), ctime_ns=st.st_ctime_ns % 1_000_000_000,
        mtime_s=int(st.st_mtime), mtime_ns=st.st_mtime_ns % 1_000_000_000,
        dev=st.st_dev, ino=st.st_ino, uid=st.st_uid, gid=st.st_gid, size=st.st_size)


class IndexLock:
    """An owned ``index.lock``: created exclusively, installed by rename, never stolen."""

    def __init__(self, git_dir):
        self.path = Path(git_dir) / "index.lock"
        self.target = Path(git_dir) / "index"
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                              | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        except FileExistsError as e:
            raise RuntimeError(
                f"Unable to create '{self.path}': File exists. Another Git process "
                f"seems to be running in this repository; refusing to remove a lock "
                f"this operation does not own") from e
        st = os.fstat(self.fd)
        self.identity = (st.st_dev, st.st_ino)
        self.done = False

    def _ours(self) -> bool:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return False
        return (st.st_dev, st.st_ino) == self.identity

    def install(self, data: bytes) -> None:
        os.write(self.fd, data)
        os.fsync(self.fd)
        os.close(self.fd)
        self.fd = None
        if not self._ours():
            raise RuntimeError("index.lock was replaced while held; refusing to install it")
        os.rename(self.path, self.target)
        self.done = True

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if not self.done and self._ours():
            os.unlink(self.path)
        self.done = True


# ── objects ──────────────────────────────────────────────────────────

def blob_sha1_stream(fh, size: int) -> str:
    h = hashlib.sha1(b"blob %d\0" % size)
    remaining = size
    while remaining:
        chunk = fh.read(min(1 << 20, remaining))
        if not chunk:
            raise OSError("file shrank while hashing")
        h.update(chunk)
        remaining -= len(chunk)
    if fh.read(1):
        raise OSError("file grew while hashing")
    return h.hexdigest()


def blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@dataclass
class TreeEntry:
    path: str
    mode: int
    type: str
    sha: str


def ls_tree(cwd, treeish: str) -> dict:
    res = run(cwd, "ls-tree", "-r", "-z", "--full-tree", treeish, "--", text=False)
    if res.returncode != 0:
        raise RuntimeError(f"cannot read tree {treeish}: {res.stderr.decode(errors='replace').strip()[:300]}")
    entries = {}
    for item in res.stdout.split(b"\0"):
        if not item:
            continue
        meta, _, name = item.partition(b"\t")
        mode, otype, sha = meta.decode().split(" ")
        path = os.fsdecode(name)
        entries[path] = TreeEntry(path, int(mode, 8), otype, sha)
    return entries


def resolve_commit(cwd, rev: str) -> str | None:
    res = run(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}")
    sha = res.stdout.strip()
    return sha if res.returncode == 0 and _SHA1.match(sha) else None


class CatFile:
    """Raw object bytes through one `cat-file --batch` (no textconv, no filters)."""

    def __init__(self, cwd):
        self.proc = subprocess.Popen(
            _argv(("cat-file", "--batch")), cwd=str(cwd), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=closed_env())

    def read(self, sha: str) -> tuple[str, bytes]:
        self.proc.stdin.write(sha.encode() + b"\n")
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().decode().split()
        if len(header) != 3:
            raise RuntimeError(f"object {sha} is unavailable")
        size = int(header[2])
        data = self.proc.stdout.read(size)
        self.proc.stdout.read(1)
        if len(data) != size:
            raise RuntimeError(f"object {sha} was truncated")
        return header[1], data

    def close(self):
        try:
            self.proc.stdin.close()
        finally:
            self.proc.wait(timeout=30)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def missing_objects(cwd, shas) -> set:
    shas = sorted(set(shas))
    if not shas:
        return set()
    res = run(cwd, "cat-file", "--batch-check", input="\n".join(shas) + "\n")
    if res.returncode != 0:
        raise RuntimeError("cannot inspect the object database")
    return {line.split()[0] for line in res.stdout.splitlines() if line.endswith(" missing")}


# ── working files, never through a symlink ───────────────────────────

_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NOT_A_DIR = (errno.ENOTDIR, errno.ELOOP)


class _Beyond(OSError):
    """A path component is a symlink or non-directory: the path is beyond it."""


def _parts(rel: str) -> list[str]:
    parts = rel.split("/")
    if (not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts)
            or any(p.lower() == ".git" for p in parts)):
        raise GitRefused(f"{rel!r} is not a confined repository path")
    return parts


@dataclass
class WorkFile:
    kind: str  # "file" | "link" | "absent" | "dir" | "other"
    sha: str | None = None
    mode: int | None = None
    st: os.stat_result | None = None
    data: bytes | None = None


class WorkTree:
    """A working tree opened once; every path resolves under it without following links."""

    def __init__(self, root):
        self.root = Path(root)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _parent(self, parts, *, create=False, replace_nondirs=False):
        """A descriptor of the directory holding parts[-1], walked without following links."""
        fd = os.dup(self.fd)
        try:
            for name in parts[:-1]:
                try:
                    nfd = os.open(name, _O_DIR, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(name, 0o755, dir_fd=fd)
                    nfd = os.open(name, _O_DIR, dir_fd=fd)
                except OSError as e:
                    if e.errno not in _NOT_A_DIR:
                        raise
                    if not (create and replace_nondirs):
                        raise _Beyond(name) from e
                    # A file or symlink where a directory belongs: replaced, never followed.
                    os.unlink(name, dir_fd=fd)
                    os.mkdir(name, 0o755, dir_fd=fd)
                    nfd = os.open(name, _O_DIR, dir_fd=fd)
                os.close(fd)
                fd = nfd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _parent_or_none(self, parts):
        try:
            return self._parent(parts)
        except (FileNotFoundError, _Beyond):
            return None  # a missing or symlinked/non-directory parent: Git sees no file here

    def mkdir(self, rel: str, *, replace_nondirs: bool = False) -> None:
        parts = _parts(rel)
        pfd = self._parent(parts, create=True, replace_nondirs=replace_nondirs)
        try:
            try:
                os.mkdir(parts[-1], 0o755, dir_fd=pfd)
            except FileExistsError:
                st = os.lstat(parts[-1], dir_fd=pfd)
                if not stat.S_ISDIR(st.st_mode):
                    if not replace_nondirs:
                        raise GitRefused(f"{rel}: a file occupies this directory path")
                    os.unlink(parts[-1], dir_fd=pfd)
                    os.mkdir(parts[-1], 0o755, dir_fd=pfd)
        finally:
            os.close(pfd)

    def lstat(self, rel: str):
        parts = _parts(rel)
        pfd = self._parent_or_none(parts)
        if pfd is None:
            return None
        try:
            return os.lstat(parts[-1], dir_fd=pfd)
        except FileNotFoundError:
            return None
        finally:
            os.close(pfd)

    def read(self, rel: str, *, keep: bool = False, sink=None) -> WorkFile:
        """Type, raw blob identity and mode of *rel*; optional bytes or a streaming sink."""
        parts = _parts(rel)
        pfd = self._parent_or_none(parts)
        if pfd is None:
            return WorkFile("absent")
        try:
            try:
                st = os.lstat(parts[-1], dir_fd=pfd)
            except FileNotFoundError:
                return WorkFile("absent")
            if stat.S_ISLNK(st.st_mode):
                target = os.fsencode(os.readlink(parts[-1], dir_fd=pfd))
                after = os.lstat(parts[-1], dir_fd=pfd)
                if (after.st_ino, after.st_ctime_ns) != (st.st_ino, st.st_ctime_ns):
                    raise OSError(f"{rel}: symlink changed while being read")
                return WorkFile("link", blob_sha1(target), 0o120000, st, target)
            if stat.S_ISDIR(st.st_mode):
                return WorkFile("dir", st=st)
            if not stat.S_ISREG(st.st_mode):
                return WorkFile("other", st=st)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=pfd)
            with os.fdopen(fd, "rb") as fh:
                opened = os.fstat(fh.fileno())
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                    raise OSError(f"{rel}: file replaced while being opened")
                if keep:
                    data = fh.read()
                    if len(data) != opened.st_size:
                        raise OSError(f"{rel}: file changed size while being read")
                    sha = blob_sha1(data)
                else:
                    data = None
                    sha = blob_sha1_stream(fh, opened.st_size)
                after = os.fstat(fh.fileno())
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                        opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
                    raise OSError(f"{rel}: file changed while being read")
                if sink is not None:
                    fh.seek(0)
                    sink(fh, sha)
            mode = 0o100755 if opened.st_mode & 0o100 else 0o100644
            return WorkFile("file", sha, mode, opened, data)
        finally:
            os.close(pfd)

    def write(self, rel: str, mode: int, data: bytes, *, replace_nondirs: bool = False) -> os.stat_result:
        """Atomically place a file or symlink at *rel* (exclusive temporary + rename)."""
        parts = _parts(rel)
        pfd = self._parent(parts, create=True, replace_nondirs=replace_nondirs)
        try:
            name = parts[-1]
            try:
                existing = os.lstat(name, dir_fd=pfd)
            except FileNotFoundError:
                existing = None
            if existing is not None and stat.S_ISDIR(existing.st_mode):
                try:
                    os.rmdir(name, dir_fd=pfd)
                except OSError as e:
                    raise GitRefused(f"{rel}: a non-empty directory occupies this path") from e
            tmp = f".aitelier-{os.getpid()}-{threading.get_ident()}.tmp"
            if mode == 0o120000:
                os.symlink(os.fsdecode(data), tmp, dir_fd=pfd)
            else:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o755 if mode == 0o100755 else 0o644, dir_fd=pfd)
                try:
                    view = memoryview(data)
                    while view:
                        n = os.write(fd, view)
                        view = view[n:]
                    os.fchmod(fd, 0o755 if mode == 0o100755 else 0o644)
                finally:
                    os.close(fd)
            try:
                os.rename(tmp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=pfd)
                except FileNotFoundError:
                    pass
                raise
            return os.lstat(name, dir_fd=pfd)
        finally:
            os.close(pfd)

    def remove(self, rel: str) -> bool:
        """Remove a file or symlink at *rel* (never a directory); prune emptied parents."""
        parts = _parts(rel)
        pfd = self._parent_or_none(parts)
        if pfd is None:
            return False
        try:
            try:
                st = os.lstat(parts[-1], dir_fd=pfd)
            except FileNotFoundError:
                return False
            if stat.S_ISDIR(st.st_mode):
                return False
            os.unlink(parts[-1], dir_fd=pfd)
        finally:
            os.close(pfd)
        for depth in range(len(parts) - 1, 0, -1):
            gfd = self._parent_or_none(parts[:depth])
            if gfd is None:
                break
            try:
                os.rmdir(parts[depth - 1], dir_fd=gfd)
            except OSError:
                break
            finally:
                os.close(gfd)
        return True


def link_is_confined(rel: str, target: bytes) -> bool:
    """A symlink whose text, resolved lexically from its own directory, stays inside the tree."""
    text = os.fsdecode(target)
    if not text or text.startswith("/") or "\0" in text:
        return False
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), text))
    return not (joined == ".." or joined.startswith("../") or joined.startswith("/")) and \
        not any(p.lower() == ".git" for p in joined.split("/"))


# ── inventory and raw status ─────────────────────────────────────────

def untracked(top, *, directory: bool = False, ignored: bool = False, git_dir=None) -> list[str]:
    """Untracked (or untracked ignored) names, by Git's own exclude rules read as data."""
    args = ["ls-files", "-z", "--others", "--exclude-standard"]
    if ignored:
        args.append("--ignored")
    if directory:
        args += ["--directory", "--no-empty-directory"]
    res = run(top, *args, text=False, git_dir=git_dir, work_tree=top if git_dir else None)
    if res.returncode != 0:
        raise RuntimeError(f"cannot list untracked files: {res.stderr.decode(errors='replace').strip()[:200]}")
    return [os.fsdecode(p) for p in res.stdout.split(b"\0") if p]


def _stat_matches(e: IndexEntry, st, index_mtime_ns: int) -> bool:
    if st is None:
        return False
    if ((e.mtime_s, e.mtime_ns) != (int(st.st_mtime) & 0xFFFFFFFF, st.st_mtime_ns % 1_000_000_000)
            or (e.ctime_s, e.ctime_ns) != (int(st.st_ctime) & 0xFFFFFFFF, st.st_ctime_ns % 1_000_000_000)
            or e.size != st.st_size & 0xFFFFFFFF or e.ino != st.st_ino & 0xFFFFFFFF):
        return False
    # Racy entry: written in the same instant as the index, the stat cannot vouch.
    return e.mtime_s * 1_000_000_000 + e.mtime_ns < index_mtime_ns


def _kind_mode(st) -> int | None:
    if st is None:
        return None
    if stat.S_ISLNK(st.st_mode):
        return 0o120000
    if stat.S_ISREG(st.st_mode):
        return 0o100755 if st.st_mode & 0o100 else 0o100644
    if stat.S_ISDIR(st.st_mode):
        return S_IFGITLINK
    return None


@dataclass
class RawStatus:
    head: str | None
    branch: str | None
    changes: dict  # path -> two-letter porcelain-like code
    untracked: list
    notes: list

    @property
    def dirty(self) -> bool:
        return bool(self.changes or self.untracked)


def worktree_differs(wt: WorkTree, e: IndexEntry, *, trust_stat: bool, index_mtime_ns: int) -> str | None:
    """'M' / 'D' when the working path differs from its index entry, else None."""
    if e.mode == S_IFGITLINK:
        return None  # submodule contents are not inspected by the backend
    st = wt.lstat(e.path)
    if st is None:
        return None if e.skip_worktree else "D"
    if _kind_mode(st) != e.mode:
        return "M" if not stat.S_ISDIR(st.st_mode) else "D"
    if e.intent_to_add:
        return "M"
    if trust_stat and _stat_matches(e, st, index_mtime_ns):
        return None
    got = wt.read(e.path)
    if got.kind == "absent":
        return "D"
    return None if (got.sha == e.sha and got.mode == e.mode) else "M"


def raw_status(path, *, trust_stat: bool = True, collapse_untracked: bool = True) -> RawStatus:
    """Source status from the raw index, the HEAD tree and no-follow working bytes."""
    repo = discover(path)
    with repo_lock(repo.top):
        head = resolve_commit(repo.top, "HEAD")
        branch_res = run(repo.top, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch = branch_res.stdout.strip() if branch_res.returncode == 0 else None
        tree = ls_tree(repo.top, head) if head else {}
        idx = read_index(repo.git_dir)
        changes: dict[str, list[str]] = {}
        for p in idx.unmerged_paths():
            changes[p] = ["U", "U"]
        stage0 = idx.stage0()
        for p, e in stage0.items():
            t = tree.get(p)
            if t is None:
                x = "A"
            elif t.mode != e.mode or t.sha != e.sha:
                x = "M"
            else:
                x = " "
            if x != " ":
                changes.setdefault(p, [" ", " "])[0] = x
        for p in tree:
            if p not in stage0 and p not in changes:
                changes[p] = ["D", " "]
        with WorkTree(repo.top) as wt:
            for p, e in stage0.items():
                y = worktree_differs(wt, e, trust_stat=trust_stat, index_mtime_ns=idx.mtime_ns)
                if y:
                    changes.setdefault(p, [" ", " "])[1] = y
        extra = untracked(repo.top, directory=collapse_untracked)
        cfg = config_list(repo.top)
        attrs = []
        with WorkTree(repo.top) as wt:
            for p in sorted(set(stage0) | set(extra)):
                if p.rsplit("/", 1)[-1] == ".gitattributes":
                    got = wt.read(p, keep=True)
                    if got.kind == "file":
                        attrs.append(got.data)
        notes = transform_profile(repo, cfg, attrs).notes()
        if any(e.mode == S_IFGITLINK for e in stage0.values()):
            notes.append("submodule working trees are not inspected by the backend")
    return RawStatus(head, branch, {p: "".join(v) for p, v in sorted(changes.items())},
                     sorted(extra), notes)


# ── identity for host-made commits ───────────────────────────────────

_IDENT_BAD = re.compile(r"[<>\n\0]")


def commit_identity(cwd) -> dict:
    """Author/committer from the deployment's named identity, else repository data, else host default."""
    cfg = dict(config_list(cwd))
    ident = {}
    for role in ("AUTHOR", "COMMITTER"):
        name = os.environ.get(f"GIT_{role}_NAME") or cfg.get("user.name") or "AItelier"
        email = os.environ.get(f"GIT_{role}_EMAIL") or cfg.get("user.email") or "aitelier@localhost"
        if _IDENT_BAD.search(name) or _IDENT_BAD.search(email):
            name, email = "AItelier", "aitelier@localhost"
        ident[f"GIT_{role}_NAME"] = name
        ident[f"GIT_{role}_EMAIL"] = email
    return ident


def clean_message(message: str) -> str:
    """`git commit -m`'s whitespace cleanup: trailing spaces, blank-line runs, no empty message."""
    lines = [ln.rstrip() for ln in (message or "").replace("\r\n", "\n").split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
    if not text.strip():
        raise RuntimeError("Aborting commit due to empty commit message.")
    return text + "\n"


# ── raw commit of the current Source working state ───────────────────

def _write_tree(top, entries) -> str:
    with tempfile.TemporaryDirectory(prefix="aitelier-private-index-") as tmp:
        private = Path(tmp) / "index"
        private.write_bytes(serialize_index([e for e in entries if e.stage == 0]))
        return out(top, "write-tree", index_file=private)


def _store_bytes(top, data: bytes) -> str:
    res = run(top, "hash-object", "-t", "blob", "-w", "--no-filters", "--stdin",
              input=data, text=False)
    if res.returncode != 0:
        raise RuntimeError("raw blob could not be stored")
    return res.stdout.decode().strip()


def _cas_ref(top, ref: str, new: str, old: str | None, reason: str) -> None:
    res = run(top, "update-ref", "-m", reason, ref, new, old or ZERO_SHA)
    if res.returncode != 0:
        raise RuntimeError(f"{ref} moved during the operation; nothing was overwritten "
                           f"({res.stderr.strip()[:200]})")


def commit_all(path, message: str) -> dict:
    """Stage the whole current working state and commit it, without any Git callback.

    The equivalent of ``add -A`` + ``commit``: the index paths plus untracked
    nonignored files, as their actual no-follow working bytes, form a private
    index and a tree. A hook/signature-free ``commit-tree`` is moved onto HEAD
    by compare-and-swap, and the settled index is installed from the owned
    ``index.lock`` held since before the first read.
    """
    repo = discover(path)
    with repo_lock(repo.top):
        lock = IndexLock(repo.git_dir)
        try:
            idx = read_index(repo.git_dir)
            if idx.unmerged_paths():
                raise GitRefused("Committing is not possible because you have unmerged files.")
            stage0 = idx.stage0()
            if any(e.mode == S_IFGITLINK for e in stage0.values()):
                raise GitRefused("submodules are not supported by the backend raw commit")
            extra = untracked(repo.top)
            nested = [p for p in extra if p.endswith("/")]
            if nested:
                raise GitRefused(f"embedded repository {nested[0]!r} is not supported by the raw commit")
            cfg = config_list(repo.top)
            entries, attrs, contents = [], [], {}
            with WorkTree(repo.top) as wt:
                for rel in sorted(set(stage0) | set(extra)):
                    old = stage0.get(rel)
                    is_attr = rel.rsplit("/", 1)[-1] == ".gitattributes"
                    if old is not None and not is_attr and not old.intent_to_add:
                        st = wt.lstat(rel)
                        if _kind_mode(st) == old.mode and _stat_matches(old, st, idx.mtime_ns):
                            entries.append(old)
                            continue
                    got = wt.read(rel, keep=True)
                    if got.kind in ("absent", "dir"):
                        if old is not None and old.skip_worktree:
                            entries.append(old)  # sparse: absent on disk is not a deletion
                        continue
                    if got.kind != "file" and got.kind != "link":
                        raise GitRefused(f"{rel}: unsupported file type for a commit")
                    if is_attr and got.kind == "file":
                        attrs.append(got.data)
                    contents[rel] = got
                profile = transform_profile(repo, cfg, attrs)
                if profile.active_filters:
                    raise GitRefused(
                        "configured filter drivers ("
                        + ", ".join(sorted(profile.active_filters))
                        + ") would convert these files; the backend never runs them, "
                          "so this commit is unavailable")
                for rel, got in contents.items():
                    old = stage0.get(rel)
                    if got.kind == "file" and not (old and old.sha == got.sha):
                        why = commit_content_refusal(profile, rel, got.data)
                        if why:
                            raise GitRefused(why)
                    entries.append(entry_from_stat(rel, got.mode, got.sha, got.st))
                missing = missing_objects(repo.top, [contents[p].sha for p in contents])
                for rel, got in contents.items():
                    if got.sha in missing:
                        if _store_bytes(repo.top, got.data) != got.sha:
                            raise RuntimeError(f"{rel}: raw blob could not be stored")
                        missing.discard(got.sha)
            tree = _write_tree(repo.top, entries)
            head = resolve_commit(repo.top, "HEAD")
            head_tree = out(repo.top, "rev-parse", f"{head}^{{tree}}") if head else None
            new_index = serialize_index(entries)
            if tree == head_tree or (head is None and not entries):
                lock.install(new_index)
                return {"committed": False, "message": "Nothing to commit"}
            msg = clean_message(message)
            parents = ["-p", head] if head else []
            commit = out(repo.top, "commit-tree", tree, *parents, "--no-gpg-sign",
                         input=msg, identity=commit_identity(repo.top))
            subject = msg.split("\n", 1)[0]
            _cas_ref(repo.top, "HEAD", commit,
                     head, f"commit{' (initial)' if head is None else ''}: {subject}")
            lock.install(new_index)
            return {"committed": True, "hash": commit}
        finally:
            lock.release()


# ── materialising a commit as raw files ──────────────────────────────

def materialize(top, root, commit: str, *, prior: dict | None = None,
                replace_nondirs: bool = False) -> list:
    """Write *commit*'s tree under *root* as raw bytes and return index entries.

    Blobs come from ``cat-file --batch`` (no smudge filter, no eol/ident
    conversion), each written by exclusive temporary + rename through
    no-follow parents. Gitlinks become empty directories with gitlink index
    entries; nothing recurses into a submodule. *prior* (path -> IndexEntry) lets
    an unchanged, stat-clean file keep its bytes.
    """
    tree = ls_tree(top, commit)
    entries = []
    with WorkTree(root) as wt, CatFile(top) as cat:
        for rel in sorted(tree, key=os.fsencode):
            t = tree[rel]
            _parts(rel)
            if t.type == "commit":
                wt.mkdir(rel, replace_nondirs=replace_nondirs)
                entries.append(IndexEntry(path=rel, mode=S_IFGITLINK, sha=t.sha))
                continue
            if t.type != "blob" or t.mode not in (0o100644, 0o100755, 0o120000):
                raise GitRefused(f"{rel}: unsupported tree entry {oct(t.mode)} {t.type}")
            if prior is not None:
                got = wt.read(rel)
                if got.kind in ("file", "link") and got.sha == t.sha and got.mode == t.mode:
                    entries.append(entry_from_stat(rel, t.mode, t.sha, got.st))
                    continue
            otype, data = cat.read(t.sha)
            if otype != "blob" or blob_sha1(data) != t.sha:
                raise RuntimeError(f"{rel}: object {t.sha} is not the expected blob")
            st = wt.write(rel, t.mode, data, replace_nondirs=replace_nondirs)
            entries.append(entry_from_stat(rel, t.mode, t.sha, st))
    return entries


def write_index_file(git_dir, entries) -> None:
    lock = IndexLock(git_dir)
    try:
        lock.install(serialize_index(entries))
    finally:
        lock.release()


def checkout_tree_raw(path, target: str, *, expected_head: str | None, reason: str,
                      require_clean_paths: bool) -> dict:
    """Move HEAD to *target* and its working files/index with raw bytes only.

    ``require_clean_paths`` is the fast-forward rule (local edits to a path the
    move changes refuse); without it this is a hard reset: tracked paths take
    the target bytes, tracked paths absent from the target are removed, and
    untracked files are left alone unless they occupy a target path.
    """
    repo = discover(path)
    with repo_lock(repo.top):
        lock = IndexLock(repo.git_dir)
        try:
            head = resolve_commit(repo.top, "HEAD")
            if head != expected_head:
                raise RuntimeError("HEAD moved before the update; nothing was changed")
            cfg = config_list(repo.top)
            old_tree = ls_tree(repo.top, head) if head else {}
            new_tree = ls_tree(repo.top, target)
            attrs = []
            with CatFile(repo.top) as cat:
                for rel, t in new_tree.items():
                    if rel.rsplit("/", 1)[-1] == ".gitattributes" and t.type == "blob":
                        attrs.append(cat.read(t.sha)[1])
            profile = transform_profile(repo, cfg, attrs)
            if profile.active_filters:
                raise GitRefused("configured filter drivers would convert these files; "
                                 "the backend never runs them, so this update is unavailable")
            idx = read_index(repo.git_dir)
            stage0 = idx.stage0()
            changed = {p for p in set(old_tree) | set(new_tree)
                       if (old_tree.get(p) and (old_tree[p].mode, old_tree[p].sha))
                       != (new_tree.get(p) and (new_tree[p].mode, new_tree[p].sha))}
            with WorkTree(repo.top) as wt:
                if require_clean_paths:
                    if idx.unmerged_paths():
                        raise GitRefused("unmerged paths; resolve them before updating")
                    others = set(untracked(repo.top))
                    for p in sorted(changed):
                        e = stage0.get(p)
                        t = old_tree.get(p)
                        if (e and t and (e.mode, e.sha) != (t.mode, t.sha)) or (e and not t) or (t and not e):
                            raise GitRefused(f"Your local changes to {p} would be overwritten")
                        if e and worktree_differs(wt, e, trust_stat=False, index_mtime_ns=idx.mtime_ns):
                            raise GitRefused(f"Your local changes to {p} would be overwritten")
                        if not e and p in others:
                            raise GitRefused(f"untracked working tree file {p} would be overwritten")
                    keep = {p: e for p, e in stage0.items() if p not in changed}
                    for p in sorted(changed - set(new_tree)):
                        wt.remove(p)
                else:
                    keep = {}
                    for p in sorted(idx.all_paths() - set(new_tree)):
                        if stage0.get(p) is None or stage0[p].mode != S_IFGITLINK:
                            wt.remove(p)
            if require_clean_paths:
                sub = {p: new_tree[p] for p in changed if p in new_tree}
                written = _materialize_subset(repo.top, repo.top, sub)
                entries = list(keep.values()) + written
            else:
                entries = materialize(repo.top, repo.top, target, prior={},
                                      replace_nondirs=True)
            _cas_ref(repo.top, "HEAD", target, head, reason)
            lock.install(serialize_index(entries))
            return {"head": target, "previous": head}
        finally:
            lock.release()


def _materialize_subset(top, root, tree: dict) -> list:
    entries = []
    with WorkTree(root) as wt, CatFile(top) as cat:
        for rel in sorted(tree, key=os.fsencode):
            t = tree[rel]
            if t.type == "commit":
                entries.append(IndexEntry(path=rel, mode=S_IFGITLINK, sha=t.sha))
                continue
            otype, data = cat.read(t.sha)
            if otype != "blob" or blob_sha1(data) != t.sha:
                raise RuntimeError(f"{rel}: object {t.sha} is not the expected blob")
            st = wt.write(rel, t.mode, data)
            entries.append(entry_from_stat(rel, t.mode, t.sha, st))
    return entries


def init_repository(path) -> None:
    """``git init`` with no template directory (no sample hooks, no inherited template)."""
    out(path, "init", "--quiet", "--template=")


# ── transport: https (host credential helper) and local repositories ─

_HOST_CREDENTIAL_HELPER = Path(__file__).resolve().parents[1] / "docker" / "git-credential-helper.sh"


def _transport_profile(url: str) -> tuple[str, tuple[str, ...]]:
    url = (url or "").strip()
    if url.startswith("https://"):
        extra = ["protocol.https.allow=always"]
        if _HOST_CREDENTIAL_HELPER.is_file() and not _HOST_CREDENTIAL_HELPER.is_symlink():
            extra.append(f"credential.helper={_HOST_CREDENTIAL_HELPER}")
        return "https", tuple(extra)
    if url.startswith("file://") or (url.startswith("/") and "::" not in url):
        return "file", ("protocol.file.allow=always",)
    raise GitRefused(
        f"transport for {url!r} is unavailable to the backend: only https:// "
        f"(host credential helper) and local repository paths are supported; "
        f"ssh, git://, ext:: and remote helpers never run here")


_REMOTE_PROGRAM_KEYS = ("uploadpack", "receivepack", "vcs")


def remote_transport(top, remote: str, *, push: bool) -> tuple[str, tuple[str, ...]]:
    """Validate one named remote as data before any transport touches it."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", remote or ""):
        raise GitRefused(f"{remote!r} is not a usable remote name")
    cfg = config_list(top)
    for key, value in cfg:
        if (key.startswith("url.") and key.endswith(("insteadof", "pushinsteadof"))):
            raise GitRefused("URL rewriting (url.*.insteadOf) is configured; the backend "
                             "refuses a transport whose destination it cannot bound")
        if key.startswith(f"remote.{remote}.") and key.rsplit(".", 1)[-1] in _REMOTE_PROGRAM_KEYS:
            raise GitRefused(f"{key} is configured; remote programs/helpers never run on the backend")
    urls = [v for k, v in cfg if k == f"remote.{remote}.url"]
    if push:
        urls += [v for k, v in cfg if k == f"remote.{remote}.pushurl"]
    if not urls:
        raise RuntimeError(f"No '{remote}' remote configured")
    profiles = {_transport_profile(u) for u in urls}
    if len(profiles) != 1:
        raise GitRefused("the remote mixes transports; refusing")
    return profiles.pop()


def transport(cwd, *args: str, profile: tuple[str, tuple[str, ...]], timeout: float = 600) -> str:
    res = run(cwd, *args, _transport=profile, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or f"git {args[0]} failed").strip())
    return (res.stdout + res.stderr).strip()


def clone_no_checkout(url: str, dest) -> None:
    profile = _transport_profile(url)
    source = url
    args = ["clone", "--no-checkout", "--template=", "--no-recurse-submodules", "--quiet"]
    if profile[0] == "file":
        args.append("--no-local")
        if not url.startswith("file://"):
            source = Path(url).resolve().as_uri()
    res = run(Path(dest).parent, *args, "--", source, str(dest), _transport=profile, timeout=1800)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "git clone failed").strip())


# ── native worktree removal: its own preflight ───────────────────────

def admin_dir_of(source, worktree) -> Path:
    """The registered admin directory of *worktree*, found from the source side."""
    repo = discover(source)
    gitfile = Path(worktree) / ".git"
    root = repo.common_dir / "worktrees"
    found = []
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        names = []
    for name in names:
        back = _read_regular(root / name / "gitdir")
        if back is not None and back.decode(errors="replace").strip() == str(gitfile):
            found.append(root / name)
    if len(found) != 1:
        raise RuntimeError("the worktree has no unique registered admin directory")
    return found[0]


def native_remove_hazards(source, worktree) -> list[str]:
    """Why ``git worktree remove`` (non-force) could run a program or must not be called.

    Native removal runs its own ``status`` refresh in the worktree, which hashes
    files through any configured clean filter and consults submodules. The
    closed profile already pins hooks/fsmonitor/untracked cache off for that
    child, so what remains is checked here as data: no filter driver, no
    submodule configuration or gitlink, no config include and no relocated
    worktree in the EFFECTIVE configuration of that exact worktree.
    """
    reasons = []
    admin = admin_dir_of(source, worktree)
    gitfile = Path(worktree) / ".git"
    text = _read_regular(gitfile)
    if gitfile.is_symlink() or text is None or text.decode(errors="replace").strip() != f"gitdir: {admin}":
        reasons.append("the worktree .git file does not name its registered admin directory")
    cfg = config_list(worktree, git_dir=admin)
    for key, value in cfg:
        if key.startswith("filter."):
            reasons.append(f"filter configuration {key} could run during native cleanliness checks")
        elif key.startswith("submodule.") or key == "core.worktree":
            reasons.append(f"{key} is configured")
        elif key in ("include.path",) or key.startswith("includeif."):
            reasons.append(f"configuration include {key} cannot be bounded")
        elif key == "core.fsmonitor" and value.lower() not in ("false", "0", "no", "off", ""):
            reasons.append("core.fsmonitor is configured")
    idx = read_index(admin)
    if any(e.mode == S_IFGITLINK for e in idx.entries):
        reasons.append("the worktree index has submodule gitlinks")
    with WorkTree(worktree) as wt:
        if wt.lstat(".gitmodules") is not None:
            reasons.append(".gitmodules is present")
    return sorted(set(reasons))


def native_remove(source, worktree) -> tuple[bool, str]:
    """Non-force ``git worktree remove`` after the hazard preflight, under the repo lock."""
    with repo_lock(source):
        try:
            hazards = native_remove_hazards(source, worktree)
        except Exception as exc:  # noqa: BLE001 -- unknown configuration retains
            return False, f"native removal preflight failed; retained: {exc}"
        if hazards:
            return False, "native removal retained: " + "; ".join(hazards)[:400]
        try:
            clean, why = clean_against_head(worktree)
        except Exception as exc:  # noqa: BLE001 -- unknown is not clean
            clean, why = False, f"raw cleanliness unknown: {exc}"
        if not clean:
            # Non-force removal would refuse dirty work too; it is retained here
            # without asking Git to look, and nothing is committed or forced.
            return False, f"native removal retained: tree is not clean ({why[:300]})"
        res = run(source, "worktree", "remove", str(worktree))
        if res.returncode:
            return False, f"git worktree remove refused: {(res.stderr or res.stdout).strip()[:300]}"
        return True, f"removed {worktree}; branch and commits preserved"


# ── raw cleanliness of an owned worktree ─────────────────────────────

def clean_against_head(top, *, git_dir=None) -> tuple[bool, str]:
    """Index equals HEAD, every tracked working path equals its blob by full hash, no untracked file."""
    repo = discover(top)
    gd = git_dir or repo.git_dir
    head = resolve_commit(repo.top, "HEAD")
    if not head:
        return False, "worktree HEAD is unknown"
    tree = ls_tree(repo.top, head)
    idx = read_index(gd)
    if idx.unmerged_paths():
        return False, "unmerged index entries"
    stage0 = idx.stage0()
    if {p: (e.mode, e.sha) for p, e in stage0.items()} != {p: (t.mode, t.sha) for p, t in tree.items()}:
        return False, "the index differs from HEAD"
    with WorkTree(repo.top) as wt:
        for p, e in stage0.items():
            if e.skip_worktree or e.assume_valid:
                if wt.lstat(p) is None:
                    return False, f"{p}: hidden by an index flag and absent"
            if worktree_differs(wt, replace(e, skip_worktree=False), trust_stat=False,
                                index_mtime_ns=idx.mtime_ns):
                return False, f"{p} differs from HEAD"
    if untracked(repo.top):
        return False, "untracked files are present"
    return True, "clean"


# ── H ∪ I ∪ U seed of a dirty Source into an owned tree ──────────────

def seed_working_state(source, dest, base_sha: str) -> dict:
    """Copy Source's actual working bytes/modes/absence for HEAD ∪ index ∪ untracked paths.

    The Source index and refs are only read. Index-only (force-staged ignored)
    and unmerged paths are included by name; the bytes are always the current
    working file, never an index stage. Symlinks are copied as links and must
    stay confined; gitlinks/embedded repositories are refused. Returns the
    manifest of what was seeded.
    """
    repo = discover(source)
    with repo_lock(repo.top):
        head = resolve_commit(repo.top, "HEAD")
        if head != base_sha:
            raise GitRefused(f"seed conflict: Source HEAD is {head}, the owned tree is pinned "
                             f"to {base_sha}; nothing was seeded")
        tree = ls_tree(repo.top, head)
        idx = read_index(repo.git_dir)
        extra = untracked(repo.top)
        nested = [p for p in extra if p.endswith("/")]
        gitlinks = [p for p, t in tree.items() if t.type == "commit"] + \
            [e.path for e in idx.entries if e.mode == S_IFGITLINK]
        if nested or gitlinks:
            raise GitRefused(f"submodule/embedded repository {(nested + gitlinks)[0]!r} "
                             f"requires a separate grant; not seeded")
        paths = set(tree) | idx.all_paths() | set(extra)
        manifest = {}
        with WorkTree(repo.top) as src:
            captured = {}
            for rel in sorted(paths, key=os.fsencode):
                got = src.read(rel, keep=True)
                if got.kind == "link" and not link_is_confined(rel, got.data):
                    raise GitRefused(f"{rel}: symlink leaves the repository; not seeded")
                if got.kind == "other":
                    raise GitRefused(f"{rel}: unsupported file type; not seeded")
                captured[rel] = got
        with WorkTree(dest) as dst:
            for rel, got in captured.items():
                if got.kind in ("absent", "dir"):
                    dst.remove(rel)
                    manifest[rel] = {"kind": "absent"}
            for rel, got in captured.items():
                if got.kind in ("file", "link"):
                    dst.write(rel, got.mode, got.data, replace_nondirs=True)
                    manifest[rel] = {"kind": got.kind, "mode": oct(got.mode), "sha1": got.sha}
        after = read_index(repo.git_dir)
        if after.raw_sha256 != idx.raw_sha256:
            raise RuntimeError("the Source index changed while seeding; the seed is not coherent")
    return {"base_sha": base_sha, "paths": manifest,
            "source_index_sha256": idx.raw_sha256}
