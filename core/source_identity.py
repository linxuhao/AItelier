"""The digest rule behind /health's ``source_identity``, written down exactly once.

A serving process binds a deployed checkout into its container, so a merge changes
the files on disk while the process keeps the modules it already imported. /health
answers which source bytes the process loaded - the checkout root, a digest over
the covered modules' files captured ONCE at import, the commit and working-tree
state of that checkout captured at the same moment, and the moment of that capture
- and this module is both halves of that contract: the fixed file list, and the
computation every reader must share to agree with it. ``api/main.py`` captures with
``capture`` at import; ``python -m deploy.loaded_source`` recomputes with
``compute_digest`` over the file list an answer itself reports.

`root` is the checkout the `api` and `core` packages it belongs to live in, so the
digest's file names are repo-relative to it.

The digest covers exactly the files in SOURCE_IDENTITY_MODULES, in that order, and
nothing else. Each file contributes its own repo-relative path, a NUL byte, the
lowercase hex SHA-256 of its bytes and a newline, fed in that order into one
SHA-256; the reported `digest` is that outer hash, lowercase hex. A reader
recomputes it from any tree thus (root taken from the answer):

    from core.source_identity import compute_digest
    assert compute_digest(root, answer["files"]) == answer["digest"]

What a reader compares it WITH: a digest computed the same way from the commit
they meant to deploy. A process started before a merge keeps reporting the OLD
digest after the disk holds the new bytes - that is the witness this field exists
to give, and the one thing `gate_harness` cannot say, since that field is re-read
per request and so follows the disk. `process.started_at` says from when to
compare.

The four covered files cannot say whether the whole tree moved on without them.
`vcs` says it: the commit `git rev-parse HEAD` reports from the checkout `root`
names, and whether `git status --porcelain` in that same directory shows changes
(tracked edits and untracked non-ignored files both count). Both are read at
capture time only, alongside the digest, so a later merge leaves both reported
facts alone. `vcs.commit` is a 40-character lowercase hex id or None, `vcs.dirty`
is a bool or None, and `vcs.reason` is None on success or names what failed. A git
executable missing from PATH, a directory that is not a repository, a non-zero
exit and a timeout are declared there rather than raised: /health must still
answer when the git metadata is unavailable, and the digest keeps its own reason
for unreadable covered files. Every git call has a short timeout and a small,
fixed environment, so no pager or interactive prompt can hold up an import.

A missing or unreadable file is DECLARED by ``capture`` (null digest plus a reason
naming the path) and never raised: it runs at import, where raising would take the
whole backend down over a cosmetic field. ``compute_digest`` is the reader's half
and raises ``SourceUnreadable`` naming the file instead, so a verifier can report
WHAT it could not read rather than fail silently.
"""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import os
import re
import subprocess
import time

__all__ = [
    "SOURCE_IDENTITY_MODULES",
    "SourceUnreadable",
    "capture",
    "compute_digest",
    "head_commit",
    "repository_root",
]

# Read-only git work, bounded so an import can never wait on a prompt or a pager.
GIT_TIMEOUT_SECONDS = 5.0
_COMMIT_ID = re.compile(r"[0-9a-f]{40}\Z")

# The repository modules the digest covers: the entry module that serves /health,
# the write gate, the read cache and the Cloudflare Access identity. All four are
# imported by api/main.py at import time, so a process able to answer /health
# necessarily loaded their code. Written out and fixed on purpose - a set
# discovered at run time (walking the tree, or reading sys.modules) would differ
# between two processes of the same commit and make the digest incomparable, which
# is the only thing it is good for.
SOURCE_IDENTITY_MODULES: tuple[str, ...] = (
    "api/main.py", "api/authz.py", "api/_read_cache.py", "core/cf_access.py")


class SourceUnreadable(OSError):
    """A covered file could not be read; ``path`` names which one."""

    def __init__(self, path: Path, cause: OSError):
        super().__init__(f"could not read {path}: {type(cause).__name__}")
        self.path = path
        self.cause = cause


def repository_root() -> Path:
    """The checkout this module was imported from."""
    return Path(__file__).resolve().parent.parent


def compute_digest(root, files=None) -> str:
    """The digest rule, as its one implementation: an outer SHA-256 over each file.

    ``files`` defaults to SOURCE_IDENTITY_MODULES; a caller holding an answer must
    pass the answer's own list, because that is what the reported digest was
    computed over. Raises SourceUnreadable when a file cannot be read - the caller
    that can report the failure, unlike the import-time caller, should say which.
    """
    root = Path(root)
    covered = SOURCE_IDENTITY_MODULES if files is None else tuple(files)
    outer = hashlib.sha256()
    for rel in covered:
        path = root / rel
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise SourceUnreadable(path, exc) from exc
        outer.update(rel.encode() + b"\0")
        outer.update(hashlib.sha256(payload).hexdigest().encode() + b"\n")
    return outer.hexdigest()


def _git_environment() -> dict[str, str]:
    """The small environment both git reads run with.

    PATH is carried so the executable can be found; nothing else is inherited.
    In particular a caller's GIT_DIR, GIT_WORK_TREE or GIT_INDEX_FILE cannot
    redirect `git -C root` away from the directory the answer names, and no
    caller setting can turn on a pager or an interactive prompt. LC_ALL pins
    git's wording for the reason a reader sees.
    """
    return {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat"}


def _git_output(root, args) -> tuple[str, str | None]:
    """One bounded git read; ``(stdout, None)`` or ``("", reason)``, never a raise."""
    root = Path(root)
    label = f"git -C {root} {' '.join(args)}"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS, env=_git_environment(), check=False)
    except FileNotFoundError as exc:
        return "", f"{label}: git not found on PATH ({type(exc).__name__})"
    except subprocess.TimeoutExpired:
        return "", f"{label}: timed out after {GIT_TIMEOUT_SECONDS:g}s"
    except Exception as exc:
        return "", f"{label}: {type(exc).__name__}: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return "", f"{label}: exit {result.returncode}: {detail}"
    return result.stdout.strip(), None


def head_commit(root) -> tuple[str | None, str | None]:
    """The checkout's HEAD commit, or ``(None, reason)`` when git cannot say.

    This is the same read ``capture`` makes at import and the one a verifier
    makes against the tree it is checking, so both name a commit by the same
    rule: 40 lowercase hex digits, no ambiguity about short ids or tags.
    """
    commit, reason = _git_output(root, ("rev-parse", "HEAD"))
    if reason is None and not _COMMIT_ID.fullmatch(commit):
        return None, (f"git -C {root} rev-parse HEAD did not report a "
                      "40-character lowercase hex commit")
    return (commit if reason is None else None), reason


def _vcs_identity(root) -> dict:
    """The whole-tree half of the record, declared rather than raised on failure."""
    commit, commit_reason = head_commit(root)
    status, status_reason = _git_output(
        root, ("status", "--porcelain", "--untracked-files=normal"))
    reasons = [reason for reason in (commit_reason, status_reason) if reason]
    if reasons:
        return {"commit": None, "dirty": None, "reason": "; ".join(reasons)}
    return {"commit": commit, "dirty": bool(status), "reason": None}


def capture(root=None) -> dict:
    """Which source bytes this process loaded: root, digest, vcs, and the moment.

    Called ONCE, at import (``api.main._SOURCE_IDENTITY``), because that is the
    only moment where the code the interpreter runs and the bytes on disk are
    known to be the same thing. Everything here is computed from that one reading
    and then reported verbatim, never re-read per request. The git reads in `vcs`
    are part of that same reading, so a merge after import cannot change either
    reported fact.

    ``root`` defaults to the checkout this module lives in; api/main.py passes the
    directory it was itself imported from, which is the same checkout but says so
    from the module that serves the answer. A missing or unreadable covered file,
    and any failure to read the checkout's git metadata, are declared and never
    raised (see the module docstring).
    """
    root = repository_root() if root is None else Path(root)
    files = SOURCE_IDENTITY_MODULES
    captured_at = datetime.fromtimestamp(
        time.time(), timezone.utc).isoformat(timespec="milliseconds")
    vcs = _vcs_identity(root)
    try:
        digest = compute_digest(root, files)
    except SourceUnreadable as exc:
        return {"root": str(root), "digest": None, "captured_at": captured_at,
                "files": files, "reason": str(exc), "vcs": vcs}
    return {"root": str(root), "digest": digest, "captured_at": captured_at,
            "files": files, "reason": None, "vcs": vcs}
