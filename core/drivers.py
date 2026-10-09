"""Drivers: durable, first-class identities for the agents that drive State DAGs.

Design: design/multi-driver-coop.md section 3 (D6, D9, D11), phase P0.

A driver is a GLOBAL row, not something owned by a State project:

* ``kind='lan'``       - reaches the origin off-tunnel (SSH to the host, then
  127.0.0.1:4444) with its OWN token. Only HMAC-SHA256(pepper, token) is
  stored; the plaintext is returned exactly once by ``register``/``rotate``.
* ``kind='public_cf'`` - exactly one row, ``public``: every caller that comes
  through the Cloudflare tunnel with the (unchanged) MCP external token.

``is_admin`` marks break-glass drivers (by default only ``owner-cli``, seeded
from the pre-existing ``AITELIER_ADMIN_TOKEN`` so the owner's CLI and scripts
keep working unchanged).

Identity isolation between LAN drivers is COOPERATIVE (D11): they share one
Linux account, so a token records WHO wrote; it cannot stop impersonation.

The feature is OFF unless ``AITELIER_DRIVER_IDENTITY=on`` AND a pepper secret
file is readable; while off nothing here creates a table or changes a verdict.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

PEPPER_SECRET_NAME = "AITELIER_DRIVER_TOKEN_PEPPER"
FEATURE_ENV = "AITELIER_DRIVER_IDENTITY"
LEGACY_ACTOR = "authorized-state-operator"
PUBLIC_DRIVER_ID = "public"
OWNER_CLI_DRIVER_ID = "owner-cli"
TOKEN_PREFIX = "aitd_"
LAST_SEEN_THROTTLE_SECONDS = 60
_DRIVER_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_STATUSES = ("active", "suspended", "retired")

SCHEMA = """
CREATE TABLE IF NOT EXISTS drivers (
    driver_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('lan','public_cf')),
    is_admin INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0,1)),
    token_hash TEXT,
    token_rotated_at TEXT,
    host_label TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('active','suspended','retired')),
    revision INTEGER NOT NULL CHECK(revision > 0),
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    CHECK((kind='lan' AND token_hash IS NOT NULL)
          OR (kind='public_cf' AND token_hash IS NULL AND is_admin=0))
);
CREATE UNIQUE INDEX IF NOT EXISTS drivers_token ON drivers(token_hash) WHERE token_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS drivers_one_public ON drivers(kind) WHERE kind='public_cf';
CREATE TABLE IF NOT EXISTS project_drivers (
    project_id TEXT NOT NULL,
    driver_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('member','removed')),
    revision INTEGER NOT NULL CHECK(revision > 0),
    actor TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, driver_id),
    FOREIGN KEY(driver_id) REFERENCES drivers(driver_id)
);
CREATE TABLE IF NOT EXISTS driver_audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS driver_audit_no_update BEFORE UPDATE ON driver_audit
BEGIN SELECT RAISE(ABORT,'driver audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS driver_audit_no_delete BEFORE DELETE ON driver_audit
BEGIN SELECT RAISE(ABORT,'driver audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS drivers_no_delete BEFORE DELETE ON drivers
BEGIN SELECT RAISE(ABORT,'drivers are never deleted; retire them'); END;
"""
# project_drivers.project_id has no FOREIGN KEY to state_projects on purpose:
# the registry must be creatable before and independently of any State project.
# Membership of an unknown project is refused in `set_membership` instead.

PUBLIC_FIELDS = ("driver_id", "display_name", "kind", "is_admin", "host_label",
                 "capabilities", "status", "revision", "created_at", "last_seen_at",
                 "token_rotated_at")


class DriverError(ValueError):
    """A refused driver-registry request; the message is safe to show."""


class DriverConflict(DriverError):
    """Stale revision or an existing row."""


@dataclass(frozen=True)
class Identity:
    """Who a request is, derived from the raw credential (never from arguments).

    ``kind``: 'driver' (a registered driver), 'owner' (allowlisted Cloudflare
    Access email), 'legacy' (feature off: the pre-P0 actor strings).
    """
    kind: str
    actor: str
    driver_id: str | None = None
    is_admin: bool = False
    email: str | None = None


def now() -> str:
    return datetime.now(UTC).isoformat()


def read_pepper() -> str | None:
    """The pepper is a SECRET FILE like every LLM key (AGENTS.md "API-key secret").

    Never an environment variable: test/build subprocesses inherit os.environ.
    """
    candidates = [os.path.join("/run/secrets", PEPPER_SECRET_NAME),
                  os.path.join("/run/aitelier-secrets", PEPPER_SECRET_NAME)]
    secrets_dir = os.getenv("AITELIER_SECRETS_DIR")
    if secrets_dir:
        candidates.append(os.path.join(secrets_dir, PEPPER_SECRET_NAME))
    for path in candidates:
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as handle:
                    value = handle.read().strip()
                if len(value) >= 32:
                    return value
        except OSError:
            continue
    return None


def feature_enabled() -> bool:
    return (os.getenv(FEATURE_ENV, "").strip().lower() in {"1", "on", "true", "yes"}
            and read_pepper() is not None)


def token_hash(pepper: str, token: str) -> str:
    return hmac.new(pepper.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def actor_continues(owner_actor: str | None, actor: str) -> bool:
    """Whether ``actor`` may continue work recorded under ``owner_actor``.

    Exact match, plus the two P0 transition rules: rows written before P0 by the
    shared ``authorized-state-operator`` belong to whoever held that shared
    credential (any driver - the same set of holders), and an owner's plain
    Access email continues as ``owner:<email>``. Nothing else.
    """
    if owner_actor == actor:
        return True
    if owner_actor == LEGACY_ACTOR and actor.startswith("driver:"):
        return True
    return bool(owner_actor) and actor == "owner:" + owner_actor


def check_director_identity(driver_id: str, value):
    """A driver's ``director_identity`` is its id, or ``<id>/<label>`` (subagents).

    Returns the value to use (the driver id when none was given).
    """
    if value is None:
        return driver_id
    if not isinstance(value, str):
        raise DriverError("director_identity must be text")
    if value == driver_id or (value.startswith(driver_id + "/") and len(value) > len(driver_id) + 1):
        return value
    raise DriverError(
        f"director_identity must be '{driver_id}' or '{driver_id}/<label>' for driver '{driver_id}'")


def _driver_id(value) -> str:
    if not isinstance(value, str) or not _DRIVER_ID.match(value):
        raise DriverError("driver_id must match [a-z0-9][a-z0-9_.-]{0,63}")
    return value


def _text(value, label: str, maximum: int, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise DriverError(f"{label} must be text of at most {maximum} characters")
    return value.strip()


def _capabilities(value) -> str:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise DriverError("capabilities must be an object")
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if len(encoded) > 8000:
        raise DriverError("capabilities must be at most 8000 characters of JSON")
    return encoded


def public_view(row) -> dict:
    """A driver row as any reader may see it: never the token hash."""
    data = dict(row)
    data["is_admin"] = bool(data.get("is_admin"))
    data["capabilities"] = json.loads(data.pop("capabilities_json", "{}") or "{}")
    return {key: data.get(key) for key in PUBLIC_FIELDS}


class DriverRegistry:
    """The ONE writer of ``drivers`` / ``project_drivers`` / ``driver_audit``."""

    def __init__(self, db, pepper: str):
        if not isinstance(pepper, str) or len(pepper) < 32:
            raise DriverError("driver registry needs a pepper secret of at least 32 characters")
        self.db = db
        self._pepper = pepper
        self._last_seen: dict[str, float] = {}
        with self.db.get_connection() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    # -- seeding / migration -------------------------------------------------
    def seed(self, legacy_admin_token: str | None, actor: str = "migration") -> dict:
        """Idempotent: create ``public`` and (given the old admin token) ``owner-cli``.

        Re-running with the same token changes nothing; a different token rotates
        owner-cli's hash to it, so the environment the owner already manages stays
        the source of that one credential. Historical State rows are never touched.
        """
        created = []
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM drivers WHERE driver_id=?",
                            (PUBLIC_DRIVER_ID,)).fetchone() is None:
                self._insert(conn, PUBLIC_DRIVER_ID, "Public (Cloudflare tunnel, external token)",
                             "public_cf", False, None, "cloudflare", "{}", actor)
                created.append(PUBLIC_DRIVER_ID)
            if legacy_admin_token:
                digest = token_hash(self._pepper, legacy_admin_token)
                row = conn.execute("SELECT * FROM drivers WHERE driver_id=?",
                                   (OWNER_CLI_DRIVER_ID,)).fetchone()
                if row is None:
                    self._insert(conn, OWNER_CLI_DRIVER_ID, "Owner CLI (legacy admin token)",
                                 "lan", True, digest, "linxuhaserver", "{}", actor)
                    created.append(OWNER_CLI_DRIVER_ID)
                elif row["token_hash"] != digest:
                    conn.execute("UPDATE drivers SET token_hash=?, token_rotated_at=?, revision=revision+1 "
                                 "WHERE driver_id=?", (digest, now(), OWNER_CLI_DRIVER_ID))
                    self._audit(conn, OWNER_CLI_DRIVER_ID, "rotate", {"source": "legacy_admin_token"}, actor)
        return {"created": created}

    # -- reads ---------------------------------------------------------------
    def get(self, driver_id: str) -> dict:
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()
            if row is None:
                raise DriverError(f"no driver '{driver_id}'")
            view = public_view(row)
            view["projects"] = self._memberships(conn, driver_id)
            return view

    def list(self) -> list[dict]:
        with self.db.get_connection() as conn:
            rows = conn.execute("SELECT * FROM drivers ORDER BY driver_id").fetchall()
            return [public_view(r) for r in rows]

    def project_members(self, project_id: str) -> list[dict]:
        with self.db.get_connection() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT project_id, driver_id, status, revision, updated_at FROM project_drivers "
                "WHERE project_id=? ORDER BY driver_id", (project_id,)).fetchall()]

    def audit(self, driver_id: str | None = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self.db.get_connection() as conn:
            if driver_id is None:
                rows = conn.execute("SELECT * FROM driver_audit ORDER BY seq DESC LIMIT ?", (limit,))
            else:
                rows = conn.execute("SELECT * FROM driver_audit WHERE driver_id=? ORDER BY seq DESC LIMIT ?",
                                    (driver_id, limit))
            return [dict(r) for r in rows.fetchall()]

    def lookup_token(self, token: str) -> dict | None:
        """The ACTIVE lan driver holding ``token``, else None."""
        if not isinstance(token, str) or not token or len(token) > 512:
            return None
        digest = token_hash(self._pepper, token)
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM drivers WHERE token_hash=? AND kind='lan'",
                               (digest,)).fetchone()
            if row is None or not hmac.compare_digest(row["token_hash"], digest):
                return None
            if row["status"] != "active":
                return None
            self._touch(conn, row["driver_id"])
            return public_view(row)

    def public_driver(self) -> dict | None:
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM drivers WHERE driver_id=?", (PUBLIC_DRIVER_ID,)).fetchone()
            if row is None or row["status"] != "active":
                return None
            self._touch(conn, PUBLIC_DRIVER_ID)
            return public_view(row)

    # -- writes (admin) ------------------------------------------------------
    def register(self, driver_id, display_name, *, host_label="", capabilities=None,
                 is_admin=False, actor: str) -> dict:
        driver_id = _driver_id(driver_id)
        if driver_id == PUBLIC_DRIVER_ID:
            raise DriverConflict("'public' is created by migration and cannot be registered")
        display_name = _text(display_name, "display_name", 200)
        host_label = _text(host_label, "host_label", 200, empty=True)
        caps = _capabilities(capabilities)
        if type(is_admin) is not bool:
            raise DriverError("is_admin must be a boolean")
        token = new_token()
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM drivers WHERE driver_id=?", (driver_id,)).fetchone():
                raise DriverConflict(f"driver '{driver_id}' already exists")
            self._insert(conn, driver_id, display_name, "lan", is_admin,
                         token_hash(self._pepper, token), host_label, caps, actor)
        return {"driver": self.get(driver_id), "token": token,
                "token_notice": "Shown once. Store it in ~/.aitelier-drivers/<driver_id>.token (0600)."}

    def rotate(self, driver_id, expected_revision, *, actor: str) -> dict:
        token = new_token()
        with self._write() as conn:
            row = self._current(conn, driver_id, expected_revision)
            if row["kind"] != "lan":
                raise DriverError("only LAN drivers hold a token")
            if row["status"] == "retired":
                raise DriverConflict("a retired driver cannot get a new token")
            conn.execute("UPDATE drivers SET token_hash=?, token_rotated_at=?, revision=revision+1 "
                         "WHERE driver_id=?", (token_hash(self._pepper, token), now(), driver_id))
            self._audit(conn, driver_id, "rotate", {}, actor)
        return {"driver": self.get(driver_id), "token": token,
                "token_notice": "Shown once; the previous token no longer authenticates."}

    def set_status(self, driver_id, status, expected_revision, reason, *, actor: str) -> dict:
        if status not in _STATUSES:
            raise DriverError("status must be active, suspended or retired")
        reason = _text(reason, "reason", 2000)
        with self._write() as conn:
            row = self._current(conn, driver_id, expected_revision)
            if row["status"] == "retired":
                raise DriverConflict("a retired driver stays retired")
            if driver_id == PUBLIC_DRIVER_ID and status == "retired":
                raise DriverConflict("'public' can be suspended, not retired")
            conn.execute("UPDATE drivers SET status=?, revision=revision+1 WHERE driver_id=?",
                         (status, driver_id))
            operation = {"active": "activate", "suspended": "suspend", "retired": "retire"}[status]
            self._audit(conn, driver_id, operation, {"reason": reason, "previous": row["status"]}, actor)
        return self.get(driver_id)

    def set_admin(self, driver_id, is_admin, expected_revision, reason, *, actor: str) -> dict:
        if type(is_admin) is not bool:
            raise DriverError("is_admin must be a boolean")
        reason = _text(reason, "reason", 2000)
        with self._write() as conn:
            row = self._current(conn, driver_id, expected_revision)
            if row["kind"] != "lan" and is_admin:
                raise DriverError("only a LAN driver can be an admin")
            conn.execute("UPDATE drivers SET is_admin=?, revision=revision+1 WHERE driver_id=?",
                         (int(is_admin), driver_id))
            self._audit(conn, driver_id, "set_admin", {"is_admin": is_admin, "reason": reason}, actor)
        return self.get(driver_id)

    def set_membership(self, project_id, driver_id, status, expected_revision, reason, *, actor: str,
                       project_exists=None) -> dict:
        if status not in ("member", "removed"):
            raise DriverError("status must be member or removed")
        if not isinstance(project_id, str) or not project_id or len(project_id) > 128:
            raise DriverError("project_id must be text")
        reason = _text(reason, "reason", 2000)
        if project_exists is not None and not project_exists(project_id):
            raise DriverError(f"no State project '{project_id}'")
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM drivers WHERE driver_id=?", (driver_id,)).fetchone() is None:
                raise DriverError(f"no driver '{driver_id}'")
            row = conn.execute("SELECT * FROM project_drivers WHERE project_id=? AND driver_id=?",
                               (project_id, driver_id)).fetchone()
            current = row["revision"] if row else 0
            if type(expected_revision) is not int or expected_revision != current:
                raise DriverConflict(f"membership revision is {current}, not {expected_revision}; reload")
            if row is None:
                conn.execute("INSERT INTO project_drivers VALUES(?,?,?,?,?,?)",
                             (project_id, driver_id, status, 1, actor, now()))
            else:
                conn.execute("UPDATE project_drivers SET status=?, revision=revision+1, actor=?, updated_at=? "
                             "WHERE project_id=? AND driver_id=?", (status, actor, now(), project_id, driver_id))
            self._audit(conn, driver_id, "membership",
                        {"project_id": project_id, "status": status, "reason": reason}, actor)
            result = dict(conn.execute("SELECT project_id, driver_id, status, revision, updated_at "
                                       "FROM project_drivers WHERE project_id=? AND driver_id=?",
                                       (project_id, driver_id)).fetchone())
        return result

    # -- internals -----------------------------------------------------------
    @contextmanager
    def _write(self):
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    def _insert(self, conn, driver_id, display_name, kind, is_admin, digest, host_label, caps, actor):
        stamp = now()
        conn.execute("INSERT INTO drivers(driver_id, display_name, kind, is_admin, token_hash, token_rotated_at, "
                     "host_label, capabilities_json, status, revision, created_at, last_seen_at) "
                     "VALUES(?,?,?,?,?,?,?,?, 'active', 1, ?, NULL)",
                     (driver_id, display_name, kind, int(is_admin), digest, stamp if digest else None,
                      host_label, caps, stamp))
        self._audit(conn, driver_id, "register",
                    {"kind": kind, "is_admin": bool(is_admin), "display_name": display_name}, actor)

    @staticmethod
    def _audit(conn, driver_id, operation, payload, actor):
        # Never a token, never a hash: a hash plus the pepper file is a token oracle.
        if {"token", "token_hash"} & set(payload):
            raise DriverError("audit payload must not carry credentials")
        conn.execute("INSERT INTO driver_audit(driver_id, operation, payload_json, actor, created_at) "
                     "VALUES(?,?,?,?,?)",
                     (driver_id, operation, json.dumps(payload, sort_keys=True, ensure_ascii=False), actor, now()))

    @staticmethod
    def _current(conn, driver_id, expected_revision):
        row = conn.execute("SELECT * FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()
        if row is None:
            raise DriverError(f"no driver '{driver_id}'")
        if type(expected_revision) is not int or row["revision"] != expected_revision:
            raise DriverConflict(f"driver revision is {row['revision']}, not {expected_revision}; reload")
        return row

    @staticmethod
    def _memberships(conn, driver_id) -> list[dict]:
        return [dict(r) for r in conn.execute(
            "SELECT project_id, status, revision FROM project_drivers WHERE driver_id=? ORDER BY project_id",
            (driver_id,)).fetchall()]

    def _touch(self, conn, driver_id):
        """Diagnostic only, throttled; never part of any lease decision."""
        clock = time.monotonic()
        if clock - self._last_seen.get(driver_id, -1e9) < LAST_SEEN_THROTTLE_SECONDS:
            return
        self._last_seen[driver_id] = clock
        try:
            conn.execute("UPDATE drivers SET last_seen_at=? WHERE driver_id=?", (now(), driver_id))
            conn.commit()
        except sqlite3.Error:
            # A busy database must never turn a valid credential into a denial.
            pass


_REGISTRIES: dict[str, DriverRegistry] = {}


def registry_for(db) -> DriverRegistry | None:
    """The registry over ``db`` when the feature is on, else None (feature off).

    Seeds idempotently on first use with the legacy admin token, so the owner's
    existing CLI credential is ``owner-cli`` before anything can be refused.
    """
    if db is None or not feature_enabled():
        return None
    pepper = read_pepper()
    key = str(getattr(db, "db_path", id(db)))
    registry = _REGISTRIES.get(key)
    if registry is None or registry._pepper != pepper:
        registry = DriverRegistry(db, pepper)
        registry.seed(os.getenv("AITELIER_ADMIN_TOKEN", "").strip() or None)
        _REGISTRIES[key] = registry
    return registry
