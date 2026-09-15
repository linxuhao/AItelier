"""Mandatory sidecar effect admission, shared by the host and both images.

SQLite records intent BEFORE effects; flock survives fork/exec when explicitly
passed to children. A crash/exception never clears intent. No PID, command line,
heartbeat expiry or automatic restart is evidence that an effect has ended.
This cooperative boundary is not a sandbox against code bypassing our launchers.
"""
from __future__ import annotations

import argparse
import contextvars
import fcntl
import functools
import hashlib
import hmac
import json
import os
import sqlite3
import signal
import socket
import sys
import threading
import stat
import subprocess
import tempfile
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

RESOURCES = ("godot", "godot-service", "semantic", "semantic-service", "semantic-request")
def effect_lock(resource, owner_id=None):
    return f"request-{owner_id}.effect.lock" if resource == "semantic-request" else resource + ".effect.lock"


_CURRENT = contextvars.ContextVar("resource_lease", default=None)


def directory() -> Path:
    override = os.environ.get("AITELIER_OWNERSHIP_DIR")
    if override:
        return Path(override)
    # Standalone images must supply the same mounted authority explicitly.
    from core import datadir
    return datadir.godot_control_dir()


class Authority:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else directory()
        self.database = self.root / "resource-owners.sqlite3"

    def _check(self):
        if self.root.is_symlink() or not self.root.is_dir():
            raise RuntimeError("resource authority directory unavailable or redirected")
        info = self.database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("resource authority database is not an independent regular file")
        return info.st_dev, info.st_ino

    @contextmanager
    def connection(self, *, write=False):
        identity = self._check()
        conn = sqlite3.connect(self.database.as_uri() + "?mode=rw" if write
                               else self.database.as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise RuntimeError("resource authority schema is unavailable")
            roots = conn.execute("SELECT * FROM authority").fetchall()
            if len(roots) != 1 or not all(roots[0][field] for field in ("id", "actor", "reason")):
                raise RuntimeError("resource authority commissioning identity is inconsistent")
            yield conn
            if self._check() != identity:
                raise RuntimeError("resource authority was replaced")
            if write:
                conn.commit()
        finally:
            conn.close()

    def _lock(self, name, *, shared=False, create=False, blocking=False):
        flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
        fd = os.open(self.root / name, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("resource lock is not an independent regular file")
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                        | (0 if blocking else fcntl.LOCK_NB))
            if os.stat(self.root / name, follow_symlinks=False).st_ino != info.st_ino:
                raise RuntimeError("resource lock was replaced")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _capability(self):
        """Create an inherited capability that cannot be reopened by path."""
        fd, path = tempfile.mkstemp(prefix=".resource-capability-", dir=self.root)
        try:
            os.unlink(path)
            secret = os.urandom(32)
            os.write(fd, secret)
            os.fsync(fd)
            return fd, hashlib.sha256(secret).hexdigest()
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def fence(self, *, exclusive=False):
        fd = self._lock("deployment-admission.lock", shared=not exclusive, blocking=True)
        try:
            yield
        finally:
            os.close(fd)

    def initialize(self, *, actor, reason):
        """Explicit commissioning only, after all legacy owners are stopped.

        No implicit missing-ledger repair exists in launch or measurement paths.
        """
        if not str(actor).strip() or not str(reason).strip():
            raise ValueError("commissioning requires actor and settlement evidence")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise RuntimeError("resource authority directory is redirected")
        fence = self._lock("deployment-admission.lock", create=True, blocking=True)
        try:
            # Never overwrite, migrate, or adopt an existing authority implicitly.
            fd = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            locks = []
            try:
                for resource in RESOURCES:
                    if resource == "semantic-request":
                        continue
                    locks.append(self._lock(resource + ".effect.lock", create=True))
                with sqlite3.connect(self.database) as conn:
                    conn.execute("PRAGMA synchronous=FULL")
                    conn.execute("CREATE TABLE authority (id TEXT PRIMARY KEY, actor TEXT NOT NULL, reason TEXT NOT NULL)")
                    conn.execute("INSERT INTO authority VALUES (?,?,?)", (uuid.uuid4().hex, actor, reason))
                    conn.execute("""CREATE TABLE owners (
                        generation INTEGER PRIMARY KEY AUTOINCREMENT,
                        owner_id TEXT NOT NULL UNIQUE, resource TEXT NOT NULL,
                        operation_id TEXT NOT NULL, project_id TEXT NOT NULL,
                        run_id TEXT NOT NULL, runtime_id TEXT NOT NULL, status TEXT NOT NULL,
                        actor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                        UNIQUE(resource,operation_id))""")
                    conn.execute("CREATE UNIQUE INDEX live_resource ON owners(resource) WHERE status='active' AND resource != 'semantic-request'")
                    conn.execute("PRAGMA user_version=1")
            finally:
                for lock in locks:
                    os.close(lock)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(fence)

    def acquire(self, resource, *, operation_id=None, project_id="direct", run_id="direct"):
        if resource not in RESOURCES:
            raise ValueError("unknown protected resource")
        operation_id = operation_id or uuid.uuid4().hex
        if not all(isinstance(value, str) and value.strip() for value in
                   (operation_id, project_id, run_id)):
            raise ValueError("operation identity must be nonempty text")
        fd = capability_fd = None
        try:
            # Resident registration cannot wait behind cutover: deployment waits
            # for service health while holding that fence. Services may start,
            # but every effect/request still crosses the operation fence.
            with (nullcontext() if resource.endswith("-service") else self.fence()):
                _rows, errors = self.snapshot(family=resource.split("-")[0])
                if errors:
                    raise RuntimeError("resource authority requires recovery: " + "; ".join(errors))
                with self.connection(write=True) as conn:
                    if resource != "semantic-request" and conn.execute("SELECT 1 FROM owners WHERE resource=? AND status='active'", (resource,)).fetchone():
                        raise RuntimeError("resource has an unsettled owner; explicit recovery required")
                    owner_id = uuid.uuid4().hex
                    fd = self._lock(effect_lock(resource, owner_id), create=resource == "semantic-request")
                    capability_fd, capability_digest = self._capability()
                    generation = conn.execute(
                        "INSERT INTO owners(owner_id,resource,operation_id,project_id,run_id,runtime_id,status,actor,reason) VALUES(?,?,?,?,?,?,'active',?,?)",
                        (owner_id, resource, operation_id, project_id, run_id,
                         socket.gethostname(), "launcher:" + str(os.getpid()),
                         "capability-sha256:" + capability_digest)).lastrowid
        except BaseException:
            if fd is not None:
                os.close(fd)
            if capability_fd is not None:
                os.close(capability_fd)
            raise
        return Lease(self, resource, owner_id, generation, fd, capability_fd)

    def recover(self, owner_id, generation, *, actor, reason):
        if not str(actor).strip() or not str(reason).strip():
            raise ValueError("recovery requires actor and settlement evidence")
        # Exclusive admission fence prevents resurrection between lock check and CAS.
        with self.fence(exclusive=True), self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM owners WHERE owner_id=? AND generation=? AND status='active'",
                               (owner_id, generation)).fetchone()
            if row is None:
                raise RuntimeError("recovery requires the exact unsettled generation")
            locks = []
            try:
                # A daemon may still own asynchronous work after its client died.
                family = row["resource"].split("-")[0]
                for resource in (family, family + "-service"):
                    locks.append(self._lock(effect_lock(resource)))
                if family == "semantic":
                    for path in self.root.glob("request-*.effect.lock"):
                        locks.append(self._lock(path.name))
                conn.execute("UPDATE owners SET status='reconciled',actor=?,reason=? WHERE owner_id=? AND generation=?",
                             (actor, reason, owner_id, generation))
            finally:
                for fd in locks:
                    os.close(fd)

    def snapshot(self, *, family=None):
        """Read authority and fixed locks; missing/unknown/inconsistent blocks."""
        rows, errors = [], []
        try:
            with self.connection() as conn:
                owners = [dict(r) for r in conn.execute("SELECT * FROM owners ORDER BY generation")]
                active_by_lock = {}
                for row in owners:
                    if (row["resource"] not in RESOURCES or row["status"] not in {"active", "released", "reconciled"}
                            or not all(row[k] for k in ("owner_id", "operation_id", "project_id", "run_id", "runtime_id", "actor"))):
                        raise RuntimeError("malformed resource ownership row")
                    if row["status"] == "active":
                        active_by_lock.setdefault(effect_lock(row["resource"], row["owner_id"]), []).append(row)
                resources = [(r, effect_lock(r)) for r in RESOURCES if r != "semantic-request"]
                resources.extend(("semantic-request", path.name) for path in self.root.glob("request-*.effect.lock"))
                resources = [(r, name) for r, name in resources if family is None or r.split("-")[0] == family]
                known_requests = {effect_lock(r["resource"], r["owner_id"]) for r in owners if r["resource"] == "semantic-request" and r["status"] == "active"}
                if family in (None, "semantic") and known_requests - {name for _, name in resources}:
                    errors.append("active request effect lock missing")
                for resource, lock_name in resources:
                    active = active_by_lock.get(lock_name, [])
                    try:
                        fd = self._lock(lock_name)
                    except BlockingIOError:
                        held = True
                    else:
                        held = False
                        os.close(fd)
                    if len(active) > 1 or (held and not active):
                        errors.append("resource lock/ledger mismatch: " + resource)
                    for row in active:
                        if not held:
                            errors.append("resource owner lost; explicit recovery required: " + row["owner_id"])
                        rows.append({**row, "kind": "resource", "id": row["owner_id"],
                                     "active": not resource.endswith("-service") or not held,
                                     "lock_held": held})
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            errors.append("resource authority unavailable: " + str(exc))
        return rows, errors


class Lease:
    def __init__(self, authority, resource, owner_id, generation, fd, capability_fd):
        self.authority, self.resource = authority, resource
        self.owner_id, self.generation = owner_id, generation
        self.fd, self.capability_fd = fd, capability_fd
        self.uncertain = False

    def close(self, *, settled):
        # Do not LOCK_UN: inherited child descriptors must retain the effect lock.
        os.close(self.fd)
        self.fd = None
        os.close(self.capability_fd)
        self.capability_fd = None
        if not settled:
            return
        with self.authority.connection(write=True) as conn:
            fd = self.authority._lock(effect_lock(self.resource, self.owner_id))
            try:
                if conn.execute("UPDATE owners SET status='released',reason='launcher and inherited effects settled' WHERE owner_id=? AND generation=? AND status='active'",
                                (self.owner_id, self.generation)).rowcount != 1:
                    raise RuntimeError("resource owner settlement lost its generation")
                conn.commit()
            finally:
                os.close(fd)


@contextmanager
def operation(resource, *, authority=None, **identity):
    lease = (authority or Authority()).acquire(resource, **identity)
    token = _CURRENT.set(lease)
    settled = False
    try:
        yield lease
        settled = True
    finally:
        _CURRENT.reset(token)
        lease.close(settled=settled and not lease.uncertain)


def retain():
    """A caught provider failure cannot silently retire its physical owner."""
    lease = _CURRENT.get()
    if lease is not None:
        lease.uncertain = True


def pass_fds():
    lease = _CURRENT.get()
    return (lease.fd, lease.capability_fd) if lease is not None else ()


def protected(resource):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            request = kwargs.pop("_ownership", {}) or {}
            identity = {key: request[key] for key in ("operation_id", "project_id", "run_id") if request.get(key)}
            with operation(resource, **identity):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def require_inherited(resource):
    """Refuse internal shell entry points without their launcher's capability."""
    value = json.loads(os.environ.get("AITELIER_RESOURCE_LEASE", "null"))
    if not isinstance(value, dict) or value.get("resource") != resource:
        raise RuntimeError("internal effect requires inherited resource admission")
    authority = Authority()
    if type(value.get("fd")) is not int:
        raise RuntimeError("internal effect requires inherited resource capability")
    info = os.fstat(value["fd"])
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 0:
        raise RuntimeError("inherited resource capability can be reopened")
    secret = os.pread(value["fd"], 33, 0)
    if len(secret) != 32:
        raise RuntimeError("inherited resource capability is malformed")
    rows, errors = authority.snapshot(family=resource.split("-")[0])
    row = next((candidate for candidate in rows
                if candidate.get("owner_id") == value.get("owner_id")
                and candidate.get("generation") == value.get("generation")
                and candidate.get("resource") == resource), None)
    expected = "capability-sha256:" + hashlib.sha256(secret).hexdigest()
    if (errors or row is None or not row.get("lock_held")
            or not hmac.compare_digest(str(row.get("reason", "")), expected)):
        raise RuntimeError("inherited resource admission is no longer active")


def run_command(resource, command, *, authority=None):
    with operation(resource, authority=authority) as lease:
        capability = {"resource": resource, "owner_id": lease.owner_id,
                      "generation": lease.generation, "fd": lease.capability_fd}
        env = {**os.environ, "AITELIER_RESOURCE_LEASE": json.dumps(capability)}
        child = subprocess.Popen(command, pass_fds=pass_fds(), env=env)
        received, previous = [], {}
        def forward(signum, _frame):
            received.append(signum)
            child.send_signal(signum)
        try:
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    previous[signum] = signal.signal(signum, forward)
            code = child.wait()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        if code and not (resource.endswith("-service") and received and code == -received[-1]):
            raise subprocess.CalledProcessError(code, command)
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("initialize")
    init.add_argument("--actor", required=True)
    init.add_argument("--reason", required=True)
    recover = sub.add_parser("recover")
    recover.add_argument("owner_id")
    recover.add_argument("generation", type=int)
    recover.add_argument("--actor", required=True)
    recover.add_argument("--reason", required=True)
    run = sub.add_parser("run")
    run.add_argument("resource", choices=RESOURCES)
    run.add_argument("command", nargs=argparse.REMAINDER)
    hold = sub.add_parser("hold")
    hold.add_argument("resource", choices=RESOURCES)
    verify = sub.add_parser("verify")
    verify.add_argument("resource", choices=RESOURCES)
    sub.add_parser("snapshot")
    args = parser.parse_args()
    authority = Authority(args.directory)
    if args.action == "initialize":
        authority.initialize(actor=args.actor, reason=args.reason)
    elif args.action == "recover":
        authority.recover(args.owner_id, args.generation, actor=args.actor, reason=args.reason)
    elif args.action == "hold":
        with operation(args.resource, authority=authority):
            print("admitted", flush=True)
            if sys.stdin.readline() != "settled\n":
                raise RuntimeError("effect completion was not acknowledged")
    elif args.action == "verify":
        require_inherited(args.resource)
    elif args.action == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("run needs an executable")
        return run_command(args.resource, command, authority=authority)
    else:
        print(json.dumps(authority.snapshot()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
