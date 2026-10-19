"""Byte-for-byte candidate boundary around the report-only final verifier."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from core.semantic_index_control import git

_SNAPSHOT = "candidate_snapshot.json"
_REPORT = "candidate_integrity_report.json"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _root(value: str, label: str, *, must_exist: bool) -> Path:
    path = Path(value) if value else Path()
    if not value or not path.is_absolute():
        raise ValueError(f"candidate_integrity requires absolute {label}")
    if must_exist:
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"candidate_integrity {label} is not a directory")
        return resolved
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ValueError(f"candidate_integrity {label} parent is not a directory")
    return parent / path.name


def _artifact_root(value: str, repo: Path) -> Path:
    """Resolve the engine artifact root and keep it outside hashed bytes."""
    if not value or not Path(value).is_absolute():
        raise ValueError("candidate_integrity requires absolute out_dir")
    output = Path(value).resolve(strict=False)
    if output == repo or output.is_relative_to(repo):
        raise ValueError("candidate_integrity out_dir must be outside project_root")
    return _root(value, "out_dir", must_exist=False)


def _file_hash(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _walk_error(error):
    raise error


def _derived_index(path: Path, repo: Path) -> bool:
    """Only untracked, ignored, nonredirected Git-owned index storage is derived."""
    if path.is_symlink() or (repo / ".git").is_symlink():
        return False
    relative = path.relative_to(repo).as_posix()
    try:
        top = git(path, "rev-parse", "--show-toplevel")
        tracked = git(repo, "ls-files", "-z", "--", f":(literal){relative}")
        committed = git(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--",
                        f":(literal){relative}")
        own = git(repo, "rev-parse", "--absolute-git-dir")
        ignored = git(repo, "-c", "core.excludesFile=/dev/null", "check-ignore",
                      "-q", "--", relative + "/")
        common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if (top.returncode or top.stdout.strip() != str(repo)
                or tracked.returncode or tracked.stdout or committed.returncode or committed.stdout
                or own.returncode or not own.stdout.strip() or ignored.returncode
                or common.returncode or not common.stdout.strip()):
            return False
        metadata = Path(common.stdout.strip())
        exclude = metadata / "info/exclude"
        index = Path(own.stdout.strip()) / "index"
        if (not metadata.is_absolute() or metadata.resolve() != metadata
                or exclude.resolve() != exclude or index.resolve() != index
                or not index.is_file() or index.stat().st_nlink != 1
                or (exclude.exists() and (not exclude.is_file()
                                         or exclude.stat().st_nlink != 1))):
            return False
        # These metadata queries consume repository/worktree config and active
        # includes. Qualify their effective file origins, not just Git's index.
        config = git(repo, "config", "--includes", "--null", "--show-origin", "--list")
        records = config.stdout.split("\0")
        if config.returncode or records[-1] != "" or len(records[:-1]) % 2:
            return False
        own_dir = index.parent
        origins = set()
        for origin in records[:-1:2]:
            if origin == "command line:":
                continue  # fixed settings from the existing closed Git helper
            if not origin.startswith("file:"):
                return False
            source = Path(origin[5:])
            origins.add(source if source.is_absolute() else repo / source)
        # Empty config files emit no entries/origins. Both standard local stores
        # must therefore be checked even when their content contributes no keys.
        for source in origins | {metadata / "config", own_dir / "config.worktree"}:
            if (source.resolve() != source
                    or not (source.is_relative_to(metadata) or source.is_relative_to(own_dir))
                    or (source in origins and not source.is_file())
                    or (source.exists() and (not source.is_file() or source.stat().st_nlink != 1))):
                return False
        # A redirect or unsupported object inside the directory is payload (or
        # a normal snapshot error), never a reason to skip the whole subtree.
        for current, directories, files in os.walk(path, followlinks=False, onerror=_walk_error):
            for name in directories + files:
                info = (Path(current) / name).lstat()
                mode = info.st_mode
                if (name == ".git" or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode))
                        or (stat.S_ISREG(mode) and info.st_nlink != 1)):
                    return False
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False  # unavailable ownership keeps the physical payload covered


def _snapshot(repo: Path) -> dict:
    entries: list[dict] = []
    for current, directories, files in os.walk(repo, topdown=True, followlinks=False, onerror=_walk_error):
        current_path = Path(current)
        relative_dir = current_path.relative_to(repo)
        if relative_dir == Path("."):
            directories[:] = sorted(name for name in directories if name != ".git")
            files = [name for name in files if name != ".git"]
        else:
            directories.sort()
        for name in sorted(files):
            path = current_path / name
            relative = path.relative_to(repo).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(path).encode("utf-8", errors="surrogateescape")
                entries.append({"path": relative, "type": "symlink",
                                "size": len(target), "sha256": _digest(target)})
            elif stat.S_ISREG(mode):
                size, sha = _file_hash(path)
                entries.append({"path": relative, "type": "file",
                                "size": size, "sha256": sha})
            else:
                raise ValueError(f"candidate contains unsupported file type: {relative}")

        # os.walk lists directory symlinks under directories even though it does
        # not descend into them. Hash their link bytes without following them.
        real_directories = []
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                target = os.readlink(path).encode("utf-8", errors="surrogateescape")
                entries.append({"path": path.relative_to(repo).as_posix(),
                                "type": "symlink", "size": len(target),
                                "sha256": _digest(target)})
            elif name != ".zvec-grep" or not _derived_index(path, repo):
                real_directories.append(name)
        directories[:] = real_directories

    entries.sort(key=lambda item: item["path"])
    readme = next((item for item in entries
                   if item["path"] == "README.md" and item["type"] == "file"), None)
    if readme is None:
        raise ValueError("candidate README.md is missing or is not a regular file")
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 2,
        "candidate_sha256": _digest(canonical),
        "readme": {"path": "README.md", "size": readme["size"],
                   "sha256": readme["sha256"]},
        "entries": entries,
    }


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, pending = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def _summary(snapshot: dict) -> dict:
    return {key: snapshot[key] for key in (
        "schema_version", "candidate_sha256", "readme")}


def candidate_integrity(*, project_root: str = "", out_dir: str = "",
                        phase: str = "", baseline_step: str = "",
                        **kwargs) -> dict:
    """Write the pre-verifier snapshot or compare the post-verifier bytes."""
    try:
        repo = _root(project_root, "project_root", must_exist=True)
        output = _artifact_root(out_dir, repo)
        if phase not in {"snapshot", "verify"}:
            raise ValueError("candidate_integrity phase must be snapshot or verify")
        current = _snapshot(repo)
        if phase == "snapshot":
            value = {"phase": "snapshot", "project_root": str(repo), **current}
            _atomic_json(output / _SNAPSHOT, value)
            return {"written": _SNAPSHOT, "passed": True,
                    "candidate_sha256": current["candidate_sha256"],
                    "readme_sha256": current["readme"]["sha256"]}

        if (not baseline_step or baseline_step in {".", ".."}
                or Path(baseline_step).name != baseline_step):
            raise ValueError("candidate_integrity baseline_step must be one sibling step id")
        baseline_path = output.parent / baseline_step / _SNAPSHOT
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if baseline.get("schema_version") != 2 or not isinstance(baseline.get("entries"), list):
            raise ValueError("candidate snapshot has an unsupported schema")
        if baseline.get("project_root") != str(repo):
            raise ValueError("candidate snapshot belongs to a different resolved project root")
        before_entries = {entry["path"]: entry for entry in baseline["entries"]}
        after_entries = {entry["path"]: entry for entry in current["entries"]}
        changed = sorted(
            path for path in set(before_entries) | set(after_entries)
            if before_entries.get(path) != after_entries.get(path)
        )
        passed = (
            baseline.get("candidate_sha256") == current["candidate_sha256"]
            and baseline.get("readme") == current["readme"]
            and not changed
        )
        report = {
            "phase": "verify",
            "passed": passed,
            "before": _summary(baseline),
            "after": _summary(current),
            "changed_paths": changed,
        }
        _atomic_json(output / _REPORT, report)
        result = {"written": _REPORT, "passed": passed,
                  "candidate_sha256": current["candidate_sha256"],
                  "readme_sha256": current["readme"]["sha256"],
                  "changed_paths": changed}
        if not passed:
            result["error"] = (
                "resolved candidate changed during the report-only verifier: "
                + (", ".join(changed) if changed else "hash mismatch")
            )
        return result
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"written": None, "passed": False, "error": str(exc)}
