"""AItelier's read-only probes for SkillFlow frozen prerequisites."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

from core.state_graph import StateConflict


def _arguments(arguments: dict, fields: set[str], probe: str) -> None:
    if not isinstance(arguments, dict) or set(arguments) != fields:
        raise ValueError(f"{probe} arguments must contain exactly {sorted(fields)}")


def _source_head(source: str | None):
    def probe(arguments: dict) -> str:
        _arguments(arguments, set(), "source_head")
        if not source:
            raise ValueError("State project has no source repository")
        result = subprocess.run(
            ["git", "-C", source, "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True, text=True, check=False,
        )
        value = result.stdout.strip()
        if result.returncode or len(value) != 40:
            raise ValueError("source HEAD is unavailable")
        return value
    return probe


def _sha256_file(arguments: dict) -> str:
    _arguments(arguments, {"path"}, "sha256_file")
    value = arguments["path"]
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("sha256_file path must be an absolute path")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(value, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("sha256_file path must be a regular file")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("sha256_file input changed while it was read")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _runtime_capability(sf):
    def probe(arguments: dict):
        _arguments(arguments, {"name"}, "runtime_capability")
        name = arguments["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("runtime_capability name must be nonempty text")
        return sf.capability_identity(name)
    return probe


def _available_runtime_capability(identity, arguments: dict):
    from skillflow.prerequisites import require_available_capability_identity

    return require_available_capability_identity(identity, arguments["name"])


def materialize(spec: dict, *, source: str | None, sf, trace) -> dict:
    """Verify one frozen State-attempt descriptor without mutation."""
    try:
        from skillflow.prerequisites import materialize_frozen_prerequisites
    except ImportError as exc:
        raise StateConflict(
            "installed SkillFlow lacks frozen prerequisite materialization") from exc
    return materialize_frozen_prerequisites(spec, {
        "source_head": _source_head(source),
        "sha256_file": _sha256_file,
        "runtime_capability": _runtime_capability(sf),
    }, validators={"runtime_capability": _available_runtime_capability},
       trace=trace)
