"""Validate and retain terminal State report bytes before they become evidence."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from core import datadir
from core.state_graph import StateConflict


def _read_regular_nofollow(path: Path) -> bytes:
    """Read one stable regular-file object without following its final name."""
    try:
        parent = path.parent.resolve(strict=True)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise StateConflict(f"local report is unreadable: {exc}") from exc
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path.name, flags, dir_fd=directory)
        except OSError as exc:
            raise StateConflict(
                "local report is unavailable or is a symlink; "
                "record an explicit failed observation") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise StateConflict("local report must be a regular file")
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise StateConflict("local report changed while it was being retained")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _structured_terminal(report_bytes: bytes) -> dict:
    try:
        value = json.loads(report_bytes)
    except (UnicodeDecodeError, ValueError) as exc:
        raise StateConflict(
            "terminal report must be a complete structured JSON envelope; "
            "partial or malformed reports are unusable") from exc
    if not isinstance(value, dict) or not value:
        raise StateConflict("terminal report must be a non-empty structured JSON envelope")
    if value.get("settled") is not True or value.get("usable") is not True:
        raise StateConflict(
            "terminal report must explicitly declare settled=true and usable=true")
    if value.get("status") not in {"candidate", "completed", "failed", "pass", "fail"}:
        raise StateConflict("terminal report lacks an explicit terminal status")
    return value


_DIRECTORY_FLAGS = (os.O_RDONLY | os.O_DIRECTORY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0))


def _verify_directory_entry(parent_fd: int, name: str,
                            identity: tuple[int, int], path: Path) -> None:
    """Reject a replaced directory entry without resolving it by pathname."""
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise StateConflict(
            f"persistent report directory changed while retaining evidence: {path}") from exc
    if (current.st_dev, current.st_ino) != identity or not stat.S_ISDIR(current.st_mode):
        raise StateConflict(
            f"persistent report directory changed while retaining evidence: {path}")


def _open_store_shard(store: Path, shard_name: str) -> tuple[
        int, int, int, tuple[int, int], tuple[int, int]]:
    """Open the evidence root and shard without following directory links."""
    store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_fd = os.open(store.parent, _DIRECTORY_FLAGS)
    try:
        try:
            os.mkdir(store.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            root_fd = os.open(store.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise StateConflict(
                f"persistent report store is unavailable or is a symlink: {store}") from exc
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise StateConflict(f"persistent report store is not a directory: {store}")
        root_identity = (root_stat.st_dev, root_stat.st_ino)
        _verify_directory_entry(parent_fd, store.name, root_identity, store)
        try:
            os.mkdir(shard_name, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            shard_fd = os.open(shard_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        except OSError as exc:
            raise StateConflict(
                f"persistent report store directory is a symlink or unavailable: "
                f"{store / shard_name}") from exc
        shard_stat = os.fstat(shard_fd)
        if not stat.S_ISDIR(shard_stat.st_mode):
            raise StateConflict(f"persistent report shard is not a directory: {store / shard_name}")
        shard_identity = (shard_stat.st_dev, shard_stat.st_ino)
        _verify_store_shard(root_fd, shard_name, shard_identity, store)
        if root_stat.st_dev != shard_stat.st_dev:
            raise StateConflict("persistent report store crosses filesystem devices")
        return parent_fd, root_fd, shard_fd, root_identity, shard_identity
    except BaseException:
        for fd in locals().get("shard_fd", None), locals().get("root_fd", None), parent_fd:
            if fd is not None:
                os.close(fd)
        raise


def _verify_store_shard(root_fd: int, shard_name: str,
                        identity: tuple[int, int], store: Path) -> None:
    """Reject a directory entry that changed while evidence was retained."""
    try:
        current = os.stat(shard_name, dir_fd=root_fd, follow_symlinks=False)
    except OSError as exc:
        raise StateConflict(
            f"persistent report shard changed while retaining evidence: "
            f"{store / shard_name}") from exc
    if (current.st_dev, current.st_ino) != identity or not stat.S_ISDIR(current.st_mode):
        raise StateConflict(
            f"persistent report shard changed while retaining evidence: "
            f"{store / shard_name}")


def _open_temp_at(directory: int, prefix: str) -> tuple[int, str]:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
    for _ in range(32):
        name = f".{prefix}.{secrets.token_hex(12)}"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory), name
        except FileExistsError:
            continue
    raise StateConflict("could not create a private evidence-store temporary file")


def _read_regular_at(directory: int, name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory)
    except OSError as exc:
        raise StateConflict("persistent report store entry is unavailable or is a symlink") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise StateConflict("persistent report store entry must be a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise StateConflict("persistent report store entry changed while reading")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _regular_identity_at(directory: int, name: str) -> tuple[int, int, int]:
    """Return a regular entry's device, inode, and mode without following it."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory)
    except OSError as exc:
        raise StateConflict("persistent report store entry is unavailable or is a symlink") from exc
    try:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode):
            raise StateConflict("persistent report store entry must be a regular file")
        return value.st_dev, value.st_ino, value.st_mode
    finally:
        os.close(fd)


def _unlink_entry(directory: int, name: str,
                  identity: tuple[int, int, int] | None) -> None:
    """Remove one private entry and durably record the directory change."""
    if identity is None:
        return
    try:
        current = _regular_identity_at(directory, name)
    except StateConflict:
        return
    if current[:2] == identity[:2]:
        try:
            os.unlink(name, dir_fd=directory)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return
            raise StateConflict("persistent report cleanup failed") from exc
        try:
            os.fsync(directory)
        except OSError as exc:
            raise StateConflict("persistent report cleanup could not be durably synced") from exc


def retain_report(report_ref: str, report_sha256: str, *, completed: bool) -> tuple[str, bytes]:
    parsed = urlparse(report_ref)
    if parsed.scheme:
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise StateConflict(
                "report bytes must be locally inspectable; remote references need a local completed report")
        report_path = Path(unquote(parsed.path))
    else:
        report_path = Path(report_ref)
    if not report_path.is_absolute():
        raise StateConflict("report must use an absolute locally inspectable path")
    report_bytes = _read_regular_nofollow(report_path)
    if not report_bytes:
        raise StateConflict("local report is empty/interrupted and cannot become evidence")
    if hashlib.sha256(report_bytes).hexdigest() != report_sha256:
        raise StateConflict("local report bytes do not match report SHA-256")
    if completed:
        _structured_terminal(report_bytes)

    store = datadir.state_report_store_dir()
    shard_name = report_sha256[:2]
    parent_fd, root_fd, shard_fd, root_identity, shard_identity = _open_store_shard(
        store, shard_name)
    fd = None
    tmp_name = None
    linked = False
    target_identity = None
    temp_identity = None
    final_identity_started = False
    try:
        fd, tmp_name = _open_temp_at(shard_fd, report_sha256)
        temp_stat = os.fstat(fd)
        temp_identity = (temp_stat.st_dev, temp_stat.st_ino, temp_stat.st_mode)
        if temp_stat.st_dev != shard_identity[0] or temp_stat.st_dev != root_identity[0]:
            raise StateConflict("persistent report store temporary file crosses filesystem devices")
        pending = memoryview(report_bytes)
        while pending:
            written = os.write(fd, pending)
            if written <= 0:
                raise StateConflict("persistent report store write made no progress")
            pending = pending[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o400)
        os.close(fd)
        fd = None
        try:
            os.link(tmp_name, report_sha256, src_dir_fd=shard_fd,
                    dst_dir_fd=shard_fd, follow_symlinks=False)
            linked = True
        except FileExistsError:
            if _read_regular_at(shard_fd, report_sha256) != report_bytes:
                raise StateConflict("persistent report store conflicts with the supplied digest")
        target_identity = _regular_identity_at(shard_fd, report_sha256)

        def verify_target() -> None:
            current = _regular_identity_at(shard_fd, report_sha256)
            if (current[:2] != target_identity[:2]
                    or current[0] != shard_identity[0]
                    or current[0] != root_identity[0]):
                raise StateConflict("persistent report store digest changed while retaining evidence")

        _verify_directory_entry(parent_fd, store.name, root_identity, store)
        _verify_store_shard(root_fd, shard_name, shard_identity, store)
        verify_target()
        os.fsync(shard_fd)
        _verify_directory_entry(parent_fd, store.name, root_identity, store)
        _verify_store_shard(root_fd, shard_name, shard_identity, store)
        verify_target()
        os.fsync(root_fd)

        # Temp cleanup is the final pathname mutation/fsync in this
        # invocation.  The identity check below must be the last operation
        # that observes the root, shard, and published digest before return.
        if tmp_name is not None and temp_identity is not None:
            _unlink_entry(shard_fd, tmp_name, temp_identity)
            tmp_name = None

        final_identity_started = True
        _verify_directory_entry(parent_fd, store.name, root_identity, store)
        _verify_store_shard(root_fd, shard_name, shard_identity, store)
        verify_target()
    except StateConflict as failure:
        # Once final identity validation starts, no pathname mutation may
        # follow it.  A replacement detected there is therefore refused with
        # the digest left under the descriptor that was validated previously.
        if linked and not final_identity_started:
            try:
                _unlink_entry(shard_fd, report_sha256, target_identity)
            except StateConflict as cleanup_failure:
                raise cleanup_failure from failure
        raise
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_name is not None and temp_identity is not None:
            original_failure = sys.exc_info()[1]
            try:
                _unlink_entry(shard_fd, tmp_name, temp_identity)
            except StateConflict as cleanup_failure:
                if original_failure is not None:
                    raise cleanup_failure from original_failure
                raise
        os.close(shard_fd)
        os.close(root_fd)
        os.close(parent_fd)
    return str(store / shard_name / report_sha256), report_bytes


def validate_evidence_semantics(report_bytes: bytes, criterion_id: str,
                                verdict: str, artifact: str) -> None:
    """Bind explicit structured-report fields to the evidence row they support."""
    if not report_bytes.lstrip().startswith((b"{", b"[")):
        return
    try:
        value = json.loads(report_bytes)
    except (UnicodeDecodeError, ValueError):
        return  # retain_report already rejects malformed .json files
    if not isinstance(value, dict):
        return
    if value.get("status") not in {None, "completed"}:
        raise StateConflict("evidence report status is not completed")
    if value.get("verdict") is not None and value["verdict"] != verdict:
        raise StateConflict("evidence report verdict conflicts with the evidence row")
    reported_criterion = value.get("criterion_id", value.get("criterion"))
    if reported_criterion is not None and reported_criterion != criterion_id:
        raise StateConflict("evidence report criterion conflicts with the evidence row")
    reported_artifact = value.get("artifact_ref", value.get("artifact"))
    if reported_artifact is not None and reported_artifact != artifact:
        raise StateConflict("evidence report artifact conflicts with the evidence row")


def validate_external_semantics(report_bytes: bytes, status: str, artifact: str | None) -> None:
    if not report_bytes.lstrip().startswith(b"{"):
        return
    try:
        value = json.loads(report_bytes)
    except (UnicodeDecodeError, ValueError):
        return
    if not isinstance(value, dict):
        return
    reported_status = value.get("status")
    allowed = {"candidate", "completed"} if status == "candidate" else {"failed"}
    if reported_status is not None and reported_status not in allowed:
        raise StateConflict("external report status conflicts with the terminal observation")
    reported_artifact = value.get("artifact_ref", value.get("artifact"))
    if artifact is not None and reported_artifact is not None and reported_artifact != artifact:
        raise StateConflict("external report artifact conflicts with the terminal observation")


def store_report_blob(conn, report_ref: str, report_sha256: str, report_bytes: bytes) -> None:
    retained = conn.execute(
        "SELECT report_bytes,retained_ref FROM state_external_report_blobs WHERE report_sha256=?",
        (report_sha256,),
    ).fetchone()
    if retained:
        if bytes(retained["report_bytes"]) != report_bytes or retained["retained_ref"] != report_ref:
            raise StateConflict("persistent report bytes conflict with the supplied digest")
        return
    from core.state_graph import now
    conn.execute(
        "INSERT INTO state_external_report_blobs("
        "report_sha256,report_bytes,retained_ref,created_at) VALUES(?,?,?,?)",
        (report_sha256, report_bytes, report_ref, now()),
    )
