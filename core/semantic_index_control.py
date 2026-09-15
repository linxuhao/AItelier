"""Host-owned index demand and the sidecar's serial, generation-fenced worker.

This module uses only the standard library so the same implementation runs in
AItelier and in its zvec sidecar. It never discovers work by scanning checkouts.
The ledger contains one small row per retained run, not index data. A fixed lock
shard set bounds lock-file growth; a hash collision delays work, never mixes roots.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import math
import os
import re
import signal
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from core import resource_ownership

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def git(root: Path | str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False,
                          text=True, timeout=15, env={**os.environ, "LC_ALL": "C",
                                                     "GIT_OPTIONAL_LOCKS": "0"})


def validate_checkout(rec: dict, managed_root: Path) -> Path:
    """Require the exact recorded linked checkout, not a path-shaped guess."""
    rid = rec.get("run_id", "")
    if not isinstance(rid, str) or not _RUN_ID.fullmatch(rid):
        raise ValueError("invalid run identity")
    path = Path(rec.get("worktree_path") or rec.get("root") or "")
    parent = Path(managed_root).absolute()
    if (not path.is_absolute() or path != parent / rid or path.resolve() != path
            or parent.resolve() != parent or path.is_symlink()):
        raise ValueError("checkout is not the exact nonsymlink managed run path")
    source = Path(rec.get("source_repo") or rec.get("source") or "")
    if (not source.is_absolute() or not source.is_dir()
            or source.resolve().is_relative_to(parent)):
        raise ValueError("source checkout unavailable")
    gitfile = path / ".git"
    if not path.is_dir() or gitfile.is_symlink() or not gitfile.is_file():
        raise ValueError("recorded linked worktree is unavailable")
    a = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    b = git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if (a.returncode or b.returncode or not a.stdout.strip() or not b.stdout.strip()
            or Path(a.stdout.strip()).resolve() != Path(b.stdout.strip()).resolve()):
        raise ValueError("checkout does not belong to its recorded source")
    own = git(path, "rev-parse", "--absolute-git-dir")
    if own.returncode or not own.stdout.strip():
        raise ValueError("linked checkout ownership is unavailable")
    backlink = Path(own.stdout.strip()) / "gitdir"
    if (not backlink.is_file() or backlink.is_symlink()
            or backlink.read_text(encoding="utf-8").strip() != str(gitfile)):
        raise ValueError("linked checkout backlink does not identify its recorded root")
    cache = path / ".zvec-grep"
    if cache.is_symlink() or (cache.exists() and not cache.is_dir()):
        raise ValueError("index storage is not an owned directory")
    if any((cache / name).is_symlink() for name in
           ("manifest.json", "files.zvec", "index.zvec", "locks")):
        raise ValueError("index internals must not redirect outside owned storage")
    # An index is derived data only when no part of it is a source deliverable.
    tracked = git(path, "ls-files", "--", ".zvec-grep")
    if tracked.returncode or tracked.stdout.strip():
        raise ValueError("index storage is tracked or its ownership is unknown")
    return path


class IndexControl:
    def __init__(self, directory: Path | str, managed_root: Path | str):
        self.directory = Path(directory)
        self.managed_root = Path(managed_root)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("control directory must not be a symlink")
        self.database = self.directory / "control.sqlite3"
        if self.database.is_symlink():
            raise ValueError("control database must not be a symlink")
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS indexes (
                run_id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL, desired TEXT NOT NULL,
                revision INTEGER NOT NULL, done_revision INTEGER NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
                activity_at REAL NOT NULL, retry_after REAL NOT NULL DEFAULT 0
            )""")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.database, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def lock(self, run_id: str | None = None, *, exclusive: bool = True,
             timeout: float = 0):
        """Resource shard or short global control lock. Never unlink a live lock."""
        if run_id is None:
            name = "control.lock"
        else:
            shard = int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16) % 64
            name = f"resource-{shard:02d}.lock"
        fd = os.open(self.directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                                | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("semantic resource is busy") from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            yield
        finally:
            os.close(fd)

    def get(self, run_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM indexes WHERE run_id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def for_root(self, root: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM indexes WHERE root=?", (root,)).fetchone()
            return dict(row) if row else None

    def request(self, rec: dict, desired: str, *, touch: bool = False,
                force: bool = False) -> dict:
        if desired not in ("ready", "released"):
            raise ValueError("invalid index demand")
        rid = rec["run_id"]
        with resource_ownership.Authority().fence(), self.lock(timeout=2):
            root = str(validate_checkout(rec, self.managed_root))
            source = rec.get("source_repo") or rec.get("source")
            with self.connection() as conn:
                row = conn.execute("SELECT * FROM indexes WHERE run_id=?", (rid,)).fetchone()
                if row and (row["root"] != root or row["source"] != source):
                    raise ValueError("a run's index ownership cannot change")
                if row is None:
                    conn.execute("""INSERT INTO indexes
                        (run_id,root,source,desired,revision,activity_at)
                        VALUES (?,?,?,?,1,?)""", (rid, root, source, desired, time.time()))
                elif row["desired"] != desired or force:
                    conn.execute("""UPDATE indexes SET desired=?,revision=revision+1,
                        outcome='pending',error='',retry_after=0,
                        activity_at=CASE WHEN ? THEN ? ELSE activity_at END
                        WHERE run_id=?""", (desired, touch, time.time(), rid))
                elif touch:
                    conn.execute("UPDATE indexes SET activity_at=? WHERE run_id=?",
                                 (time.time(), rid))
            return self.get(rid)

    @staticmethod
    def settled(row: dict | None, desired: str) -> bool:
        return bool(row and row["desired"] == desired and row["outcome"] == desired
                    and row["done_revision"] == row["revision"])

    def forget(self, run_id: str) -> None:
        """Caller holds both locks and has positively removed the checkout."""
        with self.connection() as conn:
            conn.execute("DELETE FROM indexes WHERE run_id=?", (run_id,))

    def process_once(self, *, execute=None, timeout: float = 600,
                     retry_seconds: float = 60, embedding: str = "local/potion-code-16m-v2") -> list[dict]:
        """Perform a bounded batch; one sidecar owns this serial writer loop."""
        execute = execute or self._execute
        with self.connection() as conn:
            jobs = [dict(r) for r in conn.execute("""SELECT * FROM indexes
                WHERE done_revision != revision AND retry_after <= ?
                ORDER BY (desired='released') DESC, activity_at DESC LIMIT 32""",
                (time.time(),))]
        if not jobs:
            return []
        with resource_ownership.operation("semantic"):
            return self._process_jobs(jobs, execute, timeout, retry_seconds, embedding)

    def _process_jobs(self, jobs, execute, timeout, retry_seconds, embedding):
        results = []
        for job in jobs:
            rid = job["run_id"]
            try:
                with self.lock(rid):
                    with self.lock(timeout=2):
                        current = self.get(rid)
                        if not current or current["revision"] != job["revision"]:
                            continue
                        root = validate_checkout(current, self.managed_root)
                    if job["desired"] == "ready":
                        self._exclude_storage(root)
                        command = ["zg", "index", str(root), "--embedding", embedding,
                                   "--mode", "server", "--hidden", "--glob", "!**/.zvec-grep/**"]
                    else:
                        command = ["zg", "index", str(root), "--drop", "--yes", "--mode", "server"]
                    execute(command, timeout=timeout)
                    if job["desired"] == "ready":
                        execute(["zg", "status", str(root), "--check-ready", "--mode", "server"],
                                timeout=timeout)
                    validate_checkout(current, self.managed_root)
                    cache = root / ".zvec-grep"
                    if job["desired"] == "ready" and (not cache.is_dir() or cache.is_symlink()):
                        raise RuntimeError("index command returned without owned index storage")
                    if job["desired"] == "released":
                        if any((cache / name).exists() for name in
                               ("manifest.json", "files.zvec", "index.zvec")):
                            raise RuntimeError("drop returned but derived storage remains; retain for repair")
                        # Upstream drops the manifest/collections, not necessarily
                        # their empty parent or residual lock metadata. Never
                        # recursively delete unknown leftovers.
                        try:
                            cache.rmdir()
                        except (FileNotFoundError, OSError):
                            pass
                    self._finish(job, job["desired"], "", retry_seconds)
                    results.append({"run_id": rid, "outcome": job["desired"]})
            except TimeoutError as exc:
                # A busy lock can be retried immediately. A command timeout is a
                # subprocess.TimeoutExpired and follows the unknown/error path.
                results.append({"run_id": rid, "outcome": "busy", "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 -- retain/degrade on unknown provider or I/O failures
                error = f"{type(exc).__name__}: {exc}"[:500]
                resource_ownership.retain()
                self._finish(job, "error", error, retry_seconds)
                results.append({"run_id": rid, "outcome": "error", "error": error})
                break  # Do not issue more daemon work after uncertain settlement.
        return results

    def _finish(self, job: dict, outcome: str, error: str, retry_seconds: float):
        with self.lock(timeout=2), self.connection() as conn:
            conn.execute("""UPDATE indexes SET outcome=?,error=?,
                done_revision=CASE WHEN ?='error' THEN done_revision ELSE revision END,
                retry_after=? WHERE run_id=? AND revision=? AND desired=?""",
                (outcome, error, outcome, time.time()+retry_seconds if error else 0,
                 job["run_id"], job["revision"], job["desired"]))

    @staticmethod
    def _exclude_storage(root: Path):
        result = git(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
        if result.returncode or not result.stdout.strip():
            raise RuntimeError("cannot resolve Git's common exclude file")
        path = Path(result.stdout.strip())
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            text = stream.read()
            if ".zvec-grep/" not in text.splitlines():
                stream.write(("\n" if text and not text.endswith("\n") else "") + ".zvec-grep/\n")

    @staticmethod
    def _execute(command: list[str], *, timeout: float):
        # Timeout ends this client, not necessarily the daemon operation. Only
        # a successful subsequent server-mode drop can establish release.
        command = [os.environ.get("AITELIER_ZG_EXECUTABLE", "zg"), *command[1:]]
        with open(os.devnull, "w") as null:
            subprocess.run(command, pass_fds=resource_ownership.pass_fds(), check=True, timeout=timeout, stdout=null,
                           stderr=subprocess.STDOUT)


def _stop_worker(*_):
    # subprocess.run closes its current client on this exception. No successful
    # receipt is recorded for an interrupted daemon operation.
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGTERM, _stop_worker)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-dir", required=True)
    parser.add_argument("--worktrees-root", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    control = IndexControl(args.control_dir, args.worktrees_root)
    timeout = float(os.environ.get("AITELIER_ZVEC_COMMAND_TIMEOUT_SECONDS", "600"))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("command timeout must be positive and finite")
    while True:
        for result in control.process_once(timeout=timeout, embedding=os.environ.get(
                "ZVEC_GREP_EMBEDDING", "local/potion-code-16m-v2")):
            print(f"[zg-lifecycle] {result}", flush=True)
        if args.once:
            return
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
