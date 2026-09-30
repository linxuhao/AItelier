"""Host-owned index demand and the sidecar's serial, generation-fenced worker.

This module uses only the standard library so the same implementation runs in
AItelier and in its zvec sidecar. Only immediate projects are auto-discovered.
The ledger contains one small row per retained run, not index data. A fixed lock
shard set bounds lock-file growth; a hash collision delays work, never mixes roots.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@contextmanager
def service_lock(directory: Path, name: str):
    """Short operation lock or lifetime worker lock; never remove live locks."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("control directory must not be a symlink")
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def project_root(root: Path, projects: Path, worktrees: Path) -> Path:
    """Only an immediate, nonsymlink project checkout can be auto-indexed."""
    if (not root.is_absolute() or root.parent != projects or root.resolve() != root
            or projects.resolve() != projects or root.is_relative_to(worktrees)
            or not (root / ".git").exists()):
        raise ValueError("not an owned project root")
    result = git(root, "rev-parse", "--show-toplevel")
    if result.returncode or result.stdout.strip() != str(root):
        raise ValueError("project checkout ownership is unavailable")
    cache = root / ".zvec-grep"
    if cache.is_symlink() or (cache.exists() and not cache.is_dir()):
        raise ValueError("project index storage is not an owned directory")
    return root


def immutable_project(root: Path) -> bool:
    """Positive exclusion only: no index, no write bits, and worker cannot write.

    An ACL/access failure alone is not evidence of an immutable snapshot.
    Existing indexes remain available; missing or redirected roots stay unknown.
    """
    return (not (root / ".zvec-grep").exists()
            and root.stat().st_mode & 0o222 == 0
            and not os.access(root, os.W_OK, effective_ids=True))


def quiet_daemon(status: dict) -> bool:
    return (type(status) is dict
            and all(type(status.get(key)) is int and status[key] == 0
                    for key in ("queued_jobs", "running_jobs"))
            and status.get("shutting_down") is False)


def validate_project_owner(owner: dict) -> None:
    """Shared worker/observer schema; excluded is a terminal non-work outcome."""
    def identity(row):
        return (type(row) is dict and type(row.get("root")) is str
                and bool(row["root"]) and Path(row["root"]).is_absolute()
                and type(row.get("updated_at")) in (int, float)
                and math.isfinite(row["updated_at"]) and row["updated_at"] >= 0
                and type(row.get("error")) is str)
    if (not identity(owner) or owner.get("status") not in {"idle", "active", "error", "excluded"}
            or type(owner.get("excluded_owners", [])) is not list):
        raise ValueError("project operation owner is malformed")
    first = owner.get("first_failure")
    if first is not None and (not identity(first) or first.get("status") != "error"
                              or first["root"] != owner["root"] or not first["error"]):
        raise ValueError("project first failure is malformed")
    if owner["status"] == "excluded":
        settlement = owner.get("settlement")
        if type(settlement) is not dict:
            raise ValueError("excluded project owner has no settlement evidence")
        failure, proof = settlement.get("failure"), settlement.get("proof")
        if (settlement.get("reason") != "immutable-root-without-index"
                or not identity(failure) or failure.get("status") != "error"
                or failure["root"] != owner["root"] or not failure["error"]
                or owner["error"] != failure["error"] or first is None
                or owner["updated_at"] < failure["updated_at"]
                or not quiet_daemon(settlement.get("daemon"))
                or type(proof) is not dict or proof.get("cache_absent") is not True
                or proof.get("effective_write_access") is not False
                or type(proof.get("uid")) is not int or proof["uid"] < 0
                or type(proof.get("root_mode")) is not int
                or proof["root_mode"] < 0 or proof["root_mode"] & 0o222):
            raise ValueError("excluded project owner settlement is malformed")


@contextmanager
def deployment_admission(directory: Path):
    fence = directory.parent / "godot-control" / "deployment-admission.lock"
    fence.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(fence, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _failure_error(directory: Path, exc: BaseException) -> str:
    """Keep complete client output private; diagnostic I/O never hides its failure."""
    if not isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        return f"{type(exc).__name__}: {exc}"[:500]
    parts = (exc.output, getattr(exc, "stderr", None))
    raw = b"".join(p.encode("utf-8") if isinstance(p, str) else p
                   for p in parts if p is not None)
    cause = " ".join(raw[-2048:].decode("utf-8", errors="replace").split())[-180:]
    status = (f"exit={exc.returncode}" if isinstance(exc, subprocess.CalledProcessError)
              else f"timeout={exc.timeout}s")
    prefix = f"{type(exc).__name__} {status}: "
    try:
        if directory.resolve() != directory.absolute() or directory.is_symlink():
            raise ValueError("diagnostic directory must not redirect")
        with tempfile.NamedTemporaryFile(dir=directory, prefix="command-", suffix=".raw",
                                         delete=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            path = Path(stream.name)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        locator = f" raw={path} sha256={hashlib.sha256(raw).hexdigest()}"
        # Very long control paths still locate the file beside the owner ledger.
        if len(prefix) + len(locator) > 320:
            locator = f" raw={path.name} sha256={hashlib.sha256(raw).hexdigest()}"
    except Exception as diagnostic_exc:  # preserve the original provider failure
        locator = f" raw unavailable: {type(diagnostic_exc).__name__}"
    return prefix + (cause or "no client output")[:max(0, 500-len(prefix)-len(locator))] + locator


def index_project_once(directory: Path, projects: Path, worktrees: Path, **kwargs):
    """One-shot serial owner, with the same fences as the resident worker."""
    with service_lock(directory, "worker.lock"), deployment_admission(directory):
        with service_lock(directory, "operation.lock"):
            return _index_project_once(directory, projects, worktrees, **kwargs)


def _index_project_once(directory: Path, projects: Path, worktrees: Path, *,
                        execute=None, timeout=600, embedding="local/potion-code-16m-v2"):
    """Caller owns lifetime, deployment admission and operation locks."""
    execute = execute or IndexControl._execute
    marker = directory / "project-owner.json"
    if marker.is_symlink():
        raise ValueError("project owner marker must not be a symlink")
    prior = json.loads(marker.read_text()) if marker.exists() else None
    if prior is not None:
        validate_project_owner(prior)
    history = prior.get("excluded_owners", []) if prior else []
    root = None
    if prior and prior["status"] not in {"idle", "excluded"}:
        root = project_root(Path(prior["root"]), projects, worktrees)
        if immutable_project(root):
            if prior["status"] != "error" or not prior["error"]:
                raise ValueError("immutable project operation is still unknown")
            # A client timeout may leave actual daemon work. Do not replace its
            # error until the installed administration client proves no jobs.
            result = subprocess.run(["node", "/usr/local/lib/zvec-grep-status.mjs"],
                                    check=True, capture_output=True, text=True, timeout=15)
            status = json.loads(result.stdout)
            if not quiet_daemon(status):
                raise ValueError("semantic daemon operations are active or unknown")
            if not immutable_project(project_root(root, projects, worktrees)):
                raise ValueError("immutable project exclusion changed during measurement")
            settlement = {"reason": "immutable-root-without-index", "failure": prior,
                          "daemon": status, "proof": {"uid": os.geteuid(),
                          "root_mode": root.stat().st_mode & 0o777,
                          "cache_absent": True, "effective_write_access": False}}
        else:
            settlement = None
    else:
        settlement = None
        if projects.is_dir() and not projects.is_symlink():
            for candidate in sorted(projects.iterdir()):
                if candidate.is_symlink() or (candidate / ".zvec-grep").exists():
                    continue
                try:
                    candidate = project_root(candidate, projects, worktrees)
                    if immutable_project(candidate):
                        continue
                    root = candidate
                    break
                except ValueError:
                    continue
        if root is None:
            return
        if prior and prior["status"] == "excluded":
            history = [*history, {k: v for k, v in prior.items() if k != "excluded_owners"}]

    def record(status, error=""):
        value = {"root": str(root), "status": status, "error": error,
                 "updated_at": time.time()}
        if history:
            value["excluded_owners"] = history
        if prior and prior["root"] == str(root):
            first = prior.get("first_failure")
            if first is None and prior["status"] == "error":
                first = {k: prior[k] for k in ("root", "status", "error", "updated_at")}
            if first is not None:
                value["first_failure"] = first
        if status == "error" and "first_failure" not in value:
            value["first_failure"] = {k: value[k] for k in ("root", "status", "error", "updated_at")}
        if status == "excluded":
            value["settlement"] = settlement
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
            stream.write(json.dumps(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary = stream.name
        os.replace(temporary, marker)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    if settlement is not None:
        record("excluded", prior["error"])
        return
    record("active")
    try:
        # A partial index after interruption must be checked, never skipped.
        if not (root / ".zvec-grep").exists():
            IndexControl._exclude_storage(root)
            execute(["zg", "index", str(root), "--embedding", embedding,
                     "--mode", "server", "--hidden", "--glob", "!**/.zvec-grep/**"],
                    timeout=timeout)
        execute(["zg", "status", str(root), "--check-ready", "--mode", "server"],
                timeout=timeout)
        record("idle")
    except BaseException as exc:
        record("error", _failure_error(directory, exc))
        raise


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
    def __init__(self, directory: Path | str, managed_root: Path | str, *, initialize=True):
        self.directory = Path(directory)
        self.managed_root = Path(managed_root)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("control directory must not be a symlink")
        self.database = self.directory / "control.sqlite3"
        if self.database.is_symlink():
            raise ValueError("control database must not be a symlink")
        if not initialize:
            if not self.database.is_file():
                raise FileNotFoundError("owner demand has not created a control ledger")
            with self.connection() as conn:
                conn.execute("SELECT run_id,desired,revision FROM indexes LIMIT 1")
            return
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
        with self.lock(timeout=2):
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
                error = _failure_error(self.directory, exc)
                self._finish(job, "error", error, retry_seconds)
                results.append({"run_id": rid, "outcome": "error", "error": error})
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
        subprocess.run(command, check=True, timeout=timeout, stdout=subprocess.PIPE,
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
    parser.add_argument("--projects-root")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    directory = Path(args.control_dir)
    timeout = float(os.environ.get("AITELIER_ZVEC_COMMAND_TIMEOUT_SECONDS", "600"))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("command timeout must be positive and finite")
    embedding = os.environ.get("ZVEC_GREP_EMBEDDING", "local/potion-code-16m-v2")
    # A worker never manufactures an empty ledger to make a guard look quiet.
    # Only host owner demand creates indexes rows. Project operations have a
    # separate durable marker because they retain their baseline after indexing.
    with service_lock(directory, "worker.lock"):
        while True:
            try:
                with deployment_admission(directory), service_lock(directory, "operation.lock"):
                    if (directory / "control.sqlite3").exists():
                        control = IndexControl(directory, args.worktrees_root, initialize=False)
                        for result in control.process_once(timeout=timeout, embedding=embedding):
                            print(f"[zg-lifecycle] {result}", flush=True)
                    if args.projects_root:
                        try:
                            _index_project_once(directory, Path(args.projects_root),
                                               Path(args.worktrees_root), timeout=timeout,
                                               embedding=embedding)
                        except Exception as exc:
                            print(f"[zg-project] retained for repair: {exc}", flush=True)
            except BlockingIOError:
                pass  # cutover or another actual operation owns the boundary
            if args.once:
                return
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
