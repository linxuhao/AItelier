"""Bounded reads, immutable artifacts, and exact Git snapshots.

All roots are supplied by trusted composition. No production paths or environment
lookups live in the domain layer. JSON objects reject duplicate keys and NaN.
"""
from __future__ import annotations

from contextlib import contextmanager
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
from typing import Iterator

FILE_LIMIT = 2_000_000
TREE_LIMIT = 40_000_000
FILE_COUNT_LIMIT = 10_000


class BenchError(ValueError):
    """A refused operation with no implied approval or retry permission."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BenchError(message)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def decode(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key: " + key)
            result[key] = value
        return result

    def constant(value):
        raise BenchError("non-finite JSON value")

    require(len(raw) <= TREE_LIMIT, "JSON exceeds artifact limit")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def identifier(value: object) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", value) is not None,
            "invalid identifier")
    return value


def commit_id(value: object) -> str:
    require(isinstance(value, str) and re.fullmatch("[0-9a-f]{40}", value) is not None,
            "exact 40-character Git identity required")
    return value


def relative(value: object) -> str:
    require(isinstance(value, str) and value and "\\" not in value and "\x00" not in value,
            "relative POSIX path required")
    p = PurePosixPath(value)
    require(not p.is_absolute() and p.as_posix() == value and
            not any(v in (".", "..", ".git", "") for v in p.parts), "unsafe relative path")
    return value


def checked_root(path: Path) -> Path:
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "absolute trusted root required")
    for part in (path, *path.parents):
        require(not part.is_symlink(), "symlink root refused")
    return path


def read_file(root: Path, name: str, limit: int = FILE_LIMIT) -> bytes:
    """Open every path component with O_NOFOLLOW, including the final file."""
    root = checked_root(root)
    parts = PurePosixPath(relative(name)).parts
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "regular file required")
            raw = stream.read(limit + 1)
        require(len(raw) <= limit, "file exceeds limit: " + name)
        raw.decode("utf-8")
        return raw
    finally:
        os.close(fd)


def immutable(path: Path, raw: bytes) -> None:
    """Publish a complete file atomically; an existing different file is refused."""
    path = checked_root(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(read_file(path.parent, path.name, TREE_LIMIT) == raw,
                "immutable artifact conflict: " + path.name)
        return
    fd, temporary = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            require(read_file(path.parent, path.name, TREE_LIMIT) == raw,
                    "immutable artifact conflict: " + path.name)
    finally:
        os.unlink(temporary)


@contextmanager
def lock(path: Path) -> Iterator[None]:
    path = checked_root(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def git(repo: Path, *args: str, raw: bool = False):
    # No shell, no hooks from the manuscript checkout, no interactive credential prompts.
    # Service/parent Git overrides cannot redirect a trusted repository path.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("GIT_")}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.quotepath=false", *args],
        cwd=checked_root(repo), capture_output=True, timeout=90,
        env=environment,
    )
    require(result.returncode == 0, "local Git operation failed: " + args[0])
    return result.stdout if raw else result.stdout.decode("utf-8").strip()


def clean_head(repo: Path, branch: str, expected: str) -> None:
    require(git(repo, "rev-parse", "HEAD") == commit_id(expected), "accepted HEAD changed")
    require(git(repo, "branch", "--show-current") == branch, "wrong canonical branch")
    require(not git(repo, "status", "--porcelain", "--untracked-files=all"), "canonical checkout is dirty")


def git_files(repo: Path, revision: str) -> dict[str, bytes]:
    """Export regular novel files only; no archive extraction or symlink following."""
    revision = commit_id(revision)
    listing = git(repo, "ls-tree", "-rz", "--full-tree", revision, "--", "novel", raw=True)
    result, size = {}, 0
    rows = [row for row in listing.split(b"\0") if row]
    require(len(rows) <= FILE_COUNT_LIMIT, "novel source file count exceeds limit")
    for row in rows:
        meta, name = row.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        name = relative(name.decode("utf-8"))
        require(mode in ("100644", "100755") and kind == "blob", "nonregular novel source refused")
        blob_size = int(git(repo, "cat-file", "-s", oid))
        require(blob_size <= FILE_LIMIT, "novel source file exceeds limit")
        size += blob_size
        require(size <= TREE_LIMIT, "novel source tree exceeds limit")
        content = git(repo, "cat-file", "blob", oid, raw=True)
        content.decode("utf-8")
        result[name] = content
    require("novel/bible/overview.md" in result, "novel bible not initialized")
    return result


def materialize(destination: Path, files: dict[str, bytes]) -> None:
    for name, raw in files.items():
        immutable(destination / relative(name), raw)


@contextmanager
def checkout(repo: Path, revision: str, scratch: Path) -> Iterator[Path]:
    checked_root(scratch).mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix="candidate-", dir=scratch))
    wt = parent / "tree"
    try:
        git(repo, "worktree", "add", "--detach", str(wt), commit_id(revision))
        yield wt
    finally:
        if wt.exists():
            git(repo, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(parent)
