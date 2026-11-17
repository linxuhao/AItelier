"""The owner's steward (plugin steps 13-14): its proposals and the owner's memory requests.

Proposals (design-steward 5). The steward only records an `open` row
(`create_steward_proposal`); the owner decides on the dashboard, and the backend
executes exactly the version the owner approved. Status: open -> executing ->
done | failed, or declined | withdrawn | superseded. Every terminal result the
owner caused (done, failed, declined) is delivered to the steward's driver inbox
in the SAME transaction that writes it, so it survives a restart; a result that
found no steward to deliver to keeps `notified_at` NULL and is delivered by the
startup scan (`recover`).

Memory requests (design-steward 3.4). The memory lives on the DSH host, outside
the backend's reach: an owner operation is a pending row, the steward's runtime
code (not its model) runs it and writes the result back. A result can carry the
owner's profile text, so it is kept only as long as the dashboard needs it: it
is replaced by `{"expired": true}` once the owner has read it, or
`RESULT_TTL_SECONDS` after completion, whichever comes first (lazily, on every
read and write of the table - no daemon). The payload is cleared on completion.

Both tables are private State tables (core.state_privacy.PRIVATE_STATE_TABLES).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from core.state_graph import StateConflict, StateGraphError, StateNotFound, canonical, key, now, text
from core.state_privacy import UntrustedDatabase, writer_only_read

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_steward_proposals (
 proposal_id TEXT PRIMARY KEY,
 version INTEGER NOT NULL CHECK(version>0),
 kind TEXT NOT NULL CHECK(kind IN ('create_project','start_driver','stop_driver')),
 payload_json TEXT NOT NULL, reason TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','executing','done','failed','declined','withdrawn','superseded')),
 created_by TEXT NOT NULL, created_at TEXT NOT NULL, supersedes TEXT,
 decided_at TEXT, decided_by TEXT, result_json TEXT, notified_at TEXT);
CREATE INDEX IF NOT EXISTS state_steward_proposals_status ON state_steward_proposals(status,created_at);
CREATE TABLE IF NOT EXISTS state_steward_memory_requests (
 request_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('profile_read','profile_update','forgotten_list','forget','restore','purge')),
 payload_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','done')),
 result_json TEXT, created_at TEXT NOT NULL, completed_at TEXT, created_by TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS state_steward_memory_requests_status ON state_steward_memory_requests(status,created_at);
"""

KINDS = ("create_project", "start_driver", "stop_driver")
MEMORY_KINDS = ("profile_read", "profile_update", "forgotten_list", "forget", "restore", "purge")
# The payload keys each memory request kind may carry (memory-requests.mjs).
_MEMORY_KEYS = {"profile_read": set(), "profile_update": {"text", "expected_version", "reason"},
                "forgotten_list": set(), "forget": {"ids", "reason"}, "restore": {"id"},
                "purge": {"ids"}}
RESULT_TTL_SECONDS = 600
EXPIRED = {"expired": True}
INTERRUPTED = "执行中断，请重新提议"
MAX_JSON = 256 * 1024
# A driver in one of these phases with enabled=1 is (or is becoming) live.
LIVE_PHASES = ("starting", "running", "waiting", "dormant")
_UNSETTLED = ("reserved", "launching", "running", "paused", "unknown")


def steward_project():
    """The reserved project the steward runs in (core.dsh_driver.RESERVED_ROLES)."""
    from core.dsh_driver import RESERVED_ROLES
    return next(pid for pid, role in RESERVED_ROLES.items() if role == "steward")


def _bounded_json(value, label):
    encoded = canonical(value)
    if len(encoded) > MAX_JSON:
        raise StateGraphError(f"{label} is larger than {MAX_JSON} bytes")
    return encoded


def _hash(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _has_drivers(conn) -> bool:
    """A State-only database has no DSH driver table (and so no managed driver)."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dsh_drivers'").fetchone() is not None


def stop_impact(conn, project_id) -> dict:
    """What stopping `project_id`'s driver would interrupt, from what the backend
    knows: the native children its runtime last reported (only while the driver
    is enabled in a live phase) and the external attempts it still owns."""
    row = conn.execute("SELECT driver_id,enabled,phase,subagents_json FROM dsh_drivers WHERE project_id=?",
                       (project_id,)).fetchone() if _has_drivers(conn) else None
    if row is None:
        return {"running_subagents": [], "unsettled_attempts": []}
    subagents = []
    if row["enabled"] and row["phase"] in LIVE_PHASES:
        try:
            subagents = [{"session_id": str(s)} for s in json.loads(row["subagents_json"] or "[]")]
        except ValueError:
            subagents = [{"session_id": "unreadable subagent list"}]
    attempts = [dict(r) for r in conn.execute(
        "SELECT attempt_id,external_id,status,lease_expires_at FROM state_attempts "
        f"WHERE project_id=? AND owner_driver_id=? AND status IN ({','.join('?' * len(_UNSETTLED))}) "
        "ORDER BY attempt_id LIMIT 200", (project_id, row["driver_id"], *_UNSETTLED))]
    return {"running_subagents": subagents, "unsettled_attempts": attempts}


def impact_is_empty(impact) -> bool:
    return not (impact["running_subagents"] or impact["unsettled_attempts"])


def _proposal(row) -> dict:
    return {"proposal_id": row["proposal_id"], "version": row["version"], "kind": row["kind"],
            "payload": json.loads(row["payload_json"]), "reason": row["reason"], "status": row["status"],
            "created_at": row["created_at"], "decided_at": row["decided_at"],
            "decided_by": row["decided_by"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None}


class ProposalConflict(StateConflict):
    """The proposal is not in the state the caller expected; carries the current row."""

    def __init__(self, row: dict):
        super().__init__("proposal_changed")
        self.row = row


class StateSteward:
    def __init__(self, store, actor, driver_id=None):
        self.store, self.actor, self.driver_id = store, actor, driver_id
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            with store.db.get_connection() as conn:
                conn.executescript(SCHEMA)
                conn.commit()

    # ---------------------------------------------------------------- identity
    def _steward(self, conn, project_id=None):
        """Fail closed: only the active steward-role driver bound to the steward's
        project (and, for a write, naming that project). Unreadable capabilities
        or binding refuse like a wrong role."""
        if not self.driver_id or not self.project_read_trusted:
            raise StateGraphError("steward_role_required")
        try:
            row = conn.execute("SELECT capabilities_json FROM drivers WHERE driver_id=? AND status='active'",
                               (self.driver_id,)).fetchone()
            capabilities = json.loads(row[0]) if row is not None else None
            bound = conn.execute("SELECT project_id FROM dsh_drivers WHERE driver_id=?",
                                 (self.driver_id,)).fetchone()
        except (sqlite3.Error, ValueError, TypeError):
            raise StateGraphError("steward_role_required") from None
        if (not isinstance(capabilities, dict) or capabilities.get("role") != "steward"
                or bound is None or bound[0] != steward_project()):
            raise StateGraphError("steward_role_required")
        if project_id is not None and project_id != steward_project():
            raise StateGraphError(f"steward_project_required: project_id must be {steward_project()}")

    def is_steward(self) -> bool:
        with self.store.transaction() as conn:
            try:
                self._steward(conn)
            except StateGraphError:
                return False
        return True

    def is_owner_session(self) -> bool:
        """The owner's own session: an `owner:` actor that is no driver (the check
        driver_inbox.send_user_conversation_message makes)."""
        return (not self.driver_id and self.project_read_trusted is True
                and isinstance(self.actor, str) and self.actor.startswith("owner:"))

    # ---------------------------------------------------------------- idempotency
    def _replay(self, conn, operation, request_key, payload_hash):
        row = conn.execute("SELECT payload_json,result_json FROM driver_inbox_requests "
                           "WHERE actor=? AND operation=? AND request_key=?",
                           (self.actor, operation, request_key)).fetchone()
        if row:
            if row["payload_json"] != payload_hash:
                raise StateGraphError("idempotency_conflict: request_key was already used with a "
                                      "different payload; use a new request_key")
            return json.loads(row["result_json"])
        return None

    def _record(self, conn, operation, request_key, payload_hash, result):
        # Only the payload's digest is kept: a memory result can carry the
        # owner's profile text, which must not outlive its own row's expiry.
        conn.execute("INSERT INTO driver_inbox_requests VALUES(?,?,?,?,?)",
                     (self.actor, operation, request_key, payload_hash, json.dumps(result)))
        return result

    # ---------------------------------------------------------------- proposals
    @staticmethod
    def _validate(kind, payload) -> dict:
        from pydantic import BaseModel, ConfigDict, Field, ValidationError
        from core.state_commands import _KEY_PATTERN, NewNode

        class Driver(BaseModel):
            model_config = ConfigDict(extra="forbid", strict=True)
            project_id: str = Field(min_length=1, max_length=128, pattern=_KEY_PATTERN)

        class CreateProject(Driver):
            title: str = Field(min_length=1, max_length=400)
            goal: str = Field(min_length=1, max_length=4000)
            # add_nodes' own node shape: {key, goal, acceptance, ...}.
            seed_nodes: list[NewNode] = Field(default_factory=list, max_length=200)

        model = CreateProject if kind == "create_project" else Driver
        try:
            parsed = model.model_validate(payload)
        except ValidationError as exc:
            details = [{"field": ".".join(map(str, e["loc"])), "error": e["msg"]}
                       for e in exc.errors(include_input=False)[:10]]
            raise StateGraphError(f"invalid_payload: {details}") from None
        out = parsed.model_dump()
        if kind == "create_project":
            out["seed_nodes"] = [node.model_dump(exclude_unset=True) for node in parsed.seed_nodes]
        return out

    def _get(self, conn, proposal_id):
        row = conn.execute("SELECT * FROM state_steward_proposals WHERE proposal_id=?",
                           (proposal_id,)).fetchone()
        if row is None:
            raise StateNotFound("unknown_proposal")
        return row

    def create_proposal(self, project_id, request_key, kind, payload, reason, supersedes=None):
        """Record one OPEN proposal by the steward. Executes nothing."""
        if kind not in KINDS:
            raise StateGraphError(f"unknown_kind: kind must be one of {', '.join(KINDS)}")
        payload = self._validate(kind, payload)
        reason = reason.strip()
        if not reason:
            raise StateGraphError("reason must be nonempty")
        request_hash = _hash([kind, payload, reason, supersedes])
        with self.store.transaction(write=True) as conn:
            self._steward(conn, project_id)
            replay = self._replay(conn, "create_steward_proposal", request_key, request_hash)
            if replay is not None:
                return replay
            target = payload["project_id"]
            exists = conn.execute("SELECT 1 FROM state_projects WHERE project_id=?", (target,)).fetchone()
            if kind == "create_project" and exists:
                raise StateConflict(f"project_exists: {target} is already a project")
            if kind != "create_project" and not exists:
                raise StateNotFound(f"unknown_project: {target}")
            if kind == "stop_driver":
                # What it would interrupt as the steward saw it; recomputed at approval.
                payload["impact"] = stop_impact(conn, target)
            stamp, proposal_id = now(), "prop-" + uuid4().hex[:20]
            if supersedes is not None:
                old = self._get(conn, supersedes)
                if old["status"] != "open" or old["created_by"] != self.actor:
                    raise StateConflict("supersedes must name one of your open proposals")
                conn.execute("UPDATE state_steward_proposals SET status='superseded',version=version+1,"
                             "decided_at=?,decided_by=?,result_json=? WHERE proposal_id=?",
                             (stamp, self.actor, canonical({"superseded_by": proposal_id}), supersedes))
            conn.execute("INSERT INTO state_steward_proposals(proposal_id,version,kind,payload_json,reason,"
                         "status,created_by,created_at,supersedes) VALUES(?,1,?,?,?,'open',?,?,?)",
                         (proposal_id, kind, _bounded_json(payload, "payload"), reason, self.actor, stamp,
                          supersedes))
            return self._record(conn, "create_steward_proposal", request_key, request_hash,
                                {"proposal_id": proposal_id, "version": 1, "status": "open"})

    def withdraw_proposal(self, project_id, request_key, proposal_id, reason):
        """The steward takes back one of its own OPEN proposals."""
        reason = reason.strip()
        if not reason:
            raise StateGraphError("reason must be nonempty")
        request_hash = _hash([proposal_id, reason])
        with self.store.transaction(write=True) as conn:
            self._steward(conn, project_id)
            replay = self._replay(conn, "withdraw_steward_proposal", request_key, request_hash)
            if replay is not None:
                return replay
            row = self._get(conn, proposal_id)
            if row["created_by"] != self.actor:
                raise StateGraphError("not_your_proposal")
            if row["status"] != "open":
                raise ProposalConflict(_proposal(row))
            conn.execute("UPDATE state_steward_proposals SET status='withdrawn',version=version+1,"
                         "decided_at=?,decided_by=?,result_json=? WHERE proposal_id=?",
                         (now(), self.actor, canonical({"reason": reason}), proposal_id))
            result = _proposal(self._get(conn, proposal_id))
            return self._record(conn, "withdraw_steward_proposal", request_key, request_hash,
                                {"proposal_id": proposal_id, "version": result["version"],
                                 "status": "withdrawn"})

    @writer_only_read("list_steward_proposals")
    def list_proposals(self, status="open", limit=50):
        where = "" if status == "all" else "WHERE status='open'"
        with self.store.transaction() as conn:
            rows = conn.execute(f"SELECT * FROM state_steward_proposals {where} "
                                "ORDER BY created_at DESC, proposal_id DESC LIMIT ?", (limit,)).fetchall()
        return {"proposals": [_proposal(r) for r in rows]}

    @writer_only_read("list_steward_proposals")
    def get_proposal(self, proposal_id):
        with self.store.transaction() as conn:
            return _proposal(self._get(conn, proposal_id))

    @writer_only_read("list_steward_proposals")
    def impact(self, proposal_id):
        """What stopping the proposal's target driver would interrupt, now."""
        with self.store.transaction() as conn:
            row = _proposal(self._get(conn, proposal_id))
            return stop_impact(conn, row["payload"]["project_id"])

    def _owner(self):
        if not self.is_owner_session():
            raise StateGraphError("owner_session_required")

    def decline(self, proposal_id, expected_version, reason=None):
        self._owner()
        with self.store.transaction(write=True) as conn:
            row = self._get(conn, proposal_id)
            if row["status"] != "open" or row["version"] != expected_version:
                raise ProposalConflict(_proposal(row))
            conn.execute("UPDATE state_steward_proposals SET status='declined',version=version+1,"
                         "decided_at=?,decided_by=?,result_json=? WHERE proposal_id=?",
                         (now(), self.actor, canonical({"reason": reason} if reason else {}), proposal_id))
            self._wake(conn, proposal_id)
            out = _proposal(self._get(conn, proposal_id))
        self._notify()
        return out

    def begin_approval(self, proposal_id, expected_version):
        """open -> executing, atomically with the status and version check. Exactly
        one of any number of concurrent approvals gets past here."""
        self._owner()
        with self.store.transaction(write=True) as conn:
            row = self._get(conn, proposal_id)
            if row["status"] != "open" or row["version"] != expected_version:
                raise ProposalConflict(_proposal(row))
            conn.execute("UPDATE state_steward_proposals SET status='executing',version=version+1,"
                         "decided_at=?,decided_by=? WHERE proposal_id=? AND status='open' AND version=?",
                         (now(), self.actor, proposal_id, expected_version))
            return _proposal(self._get(conn, proposal_id))

    def execute_create_project(self, proposal_id):
        """Run an EXECUTING create_project proposal: the project, its seed nodes and
        the proposal's `done` in ONE transaction, only if the project does not exist.
        Any failure rolls all of it back; the proposal is then `failed` (separately)."""
        try:
            with self.store.transaction(write=True) as conn:
                row = self._get(conn, proposal_id)
                if row["status"] != "executing" or row["kind"] != "create_project":
                    return _proposal(row)
                payload = json.loads(row["payload_json"])
                project_id = payload["project_id"]
                if conn.execute("SELECT 1 FROM state_projects WHERE project_id=?", (project_id,)).fetchone():
                    raise StateConflict(f"project_id {project_id} 已被占用")
                self.store._create_project(conn, key(project_id), text(payload["title"], "title", 400), None)
                created = self.store._add(conn, project_id, payload["seed_nodes"]) if payload["seed_nodes"] else []
                self._finish(conn, proposal_id, "done", {"project_id": project_id, "created_nodes": created})
                out = _proposal(self._get(conn, proposal_id))
        except Exception as exc:  # noqa: BLE001 - any failure is the proposal's result
            return self.fail(proposal_id, f"{type(exc).__name__}: {exc}" if not isinstance(exc, StateGraphError)
                             else str(exc))
        self._notify()
        return out

    def finish(self, proposal_id, status, result):
        """Write the terminal state of an EXECUTING proposal (and wake the steward)."""
        with self.store.transaction(write=True) as conn:
            row = self._get(conn, proposal_id)
            if row["status"] != "executing":
                return _proposal(row)
            self._finish(conn, proposal_id, status, result)
            out = _proposal(self._get(conn, proposal_id))
        self._notify()
        return out

    def fail(self, proposal_id, error):
        return self.finish(proposal_id, "failed", {"error": str(error)[:4000]})

    def _finish(self, conn, proposal_id, status, result):
        previous = self._get(conn, proposal_id)["result_json"]
        merged = {**(json.loads(previous) if previous else {}), **result}
        conn.execute("UPDATE state_steward_proposals SET status=?,version=version+1,result_json=? "
                     "WHERE proposal_id=?", (status, canonical(merged), proposal_id))
        self._wake(conn, proposal_id)

    def record_confirmation(self, proposal_id, confirm_interrupt, impact):
        """Keep the owner's confirmation (and what it covered) on the executing row."""
        with self.store.transaction(write=True) as conn:
            conn.execute("UPDATE state_steward_proposals SET result_json=? WHERE proposal_id=? "
                         "AND status='executing'",
                         (canonical({"confirm_interrupt": bool(confirm_interrupt), "impact": impact}),
                          proposal_id))

    # ---------------------------------------------------------------- the steward's wake
    def _wake(self, conn, proposal_id):
        """Deliver one terminal result to the steward's inbox on the caller's
        connection. No steward driver yet: `notified_at` stays NULL (see recover)."""
        from core.driver_inbox import deliver_notice
        row = self._get(conn, proposal_id)
        target = conn.execute("SELECT driver_id FROM dsh_drivers WHERE project_id=?",
                              (steward_project(),)).fetchone() if _has_drivers(conn) else None
        if target is None:
            return
        subject = f"Proposal {proposal_id} ({row['kind']}): {row['status']}"
        body = (f"Your proposal {proposal_id} ({row['kind']}) is {row['status']}.\n"
                f"Result: {row['result_json'] or '{}'}")
        message = {"target_driver_id": target[0], "sender_driver_id": None, "project_id": steward_project(),
                   "kind": "note", "subject": subject, "body": body[:8000], "refs": {}}
        try:
            delivered = deliver_notice(conn, message)
        except StateGraphError:
            # The body guard refused something in the result text: say so, never lose the wake.
            delivered = deliver_notice(conn, {**message, "body": (
                f"Your proposal {proposal_id} ({row['kind']}) is {row['status']}. The result text was "
                "withheld by the message guard; read it with list_steward_proposals.")})
        if "skipped" not in delivered:
            conn.execute("UPDATE state_steward_proposals SET notified_at=? WHERE proposal_id=?",
                         (now(), proposal_id))

    def _notify(self):
        from core.state_changes import notify
        notify(self.store.db.db_path)

    # ---------------------------------------------------------------- startup scan
    def recover(self, drivers=None):
        """After a restart: settle every proposal left `executing`, then deliver every
        terminal result no steward received. create_project runs again under "only
        if the project does not exist" (its execution was one transaction, so a crash
        left no half-made project); start/stop_driver are judged by the driver's
        current phase, else failed. `drivers` is the DshDrivers manager (or None)."""
        with self.store.transaction() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT proposal_id,kind,payload_json FROM state_steward_proposals WHERE status='executing'")]
        for row in rows:
            if row["kind"] == "create_project":
                self.execute_create_project(row["proposal_id"])
                continue
            target = json.loads(row["payload_json"])["project_id"]
            current = drivers.get(target) if drivers is not None else None
            if current is not None and row["kind"] == "start_driver" and current["enabled"] \
                    and current["phase"] in LIVE_PHASES:
                self.finish(row["proposal_id"], "done", {"driver": drivers.public(current), "recovered": True})
            elif current is not None and row["kind"] == "stop_driver" and not current["enabled"] \
                    and current["phase"] in ("stopped", "stopping"):
                self.finish(row["proposal_id"], "done", {"driver": drivers.public(current), "recovered": True})
            else:
                self.fail(row["proposal_id"], INTERRUPTED)
        with self.store.transaction(write=True) as conn:
            for (proposal_id,) in conn.execute(
                    "SELECT proposal_id FROM state_steward_proposals WHERE notified_at IS NULL "
                    "AND status IN ('done','failed','declined') ORDER BY created_at").fetchall():
                self._wake(conn, proposal_id)
        self._notify()
        return {"settled": len(rows)}

    # ---------------------------------------------------------------- memory requests
    @staticmethod
    def _expire(conn):
        """Drop results the dashboard no longer needs (RESULT_TTL_SECONDS after completion)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=RESULT_TTL_SECONDS)).isoformat()
        conn.execute("UPDATE state_steward_memory_requests SET result_json=? WHERE status='done' "
                     "AND completed_at<? AND result_json IS NOT ?", (canonical(EXPIRED), cutoff, canonical(EXPIRED)))

    def create_memory_request(self, kind, payload):
        """The owner's memory operation, as one pending row the steward's runtime runs."""
        self._owner()
        if kind not in MEMORY_KINDS:
            raise StateGraphError(f"unknown_kind: kind must be one of {', '.join(MEMORY_KINDS)}")
        if not isinstance(payload, dict) or set(payload) - _MEMORY_KEYS[kind]:
            raise StateGraphError(f"invalid_payload: {kind} takes {sorted(_MEMORY_KEYS[kind]) or 'no keys'}")
        request_id, stamp, home = "memreq-" + uuid4().hex[:20], now(), steward_project()
        with self.store.transaction(write=True) as conn:
            self._expire(conn)
            conn.execute("INSERT INTO state_steward_memory_requests(request_id,kind,payload_json,status,"
                         "created_at,created_by) VALUES(?,?,?,'pending',?,?)",
                         (request_id, kind, _bounded_json(payload, "payload"), stamp, self.actor))
            # The steward's runtime wakes on this event and runs the request; the
            # payload stays in the private row, never in the event.
            if conn.execute("SELECT 1 FROM state_projects WHERE project_id=?", (home,)).fetchone():
                self.store._event(conn, home, None, "steward_memory_request_created",
                                  {"request_id": request_id, "kind": kind, "actor": self.actor})
        self._notify()
        return {"request_id": request_id, "status": "pending"}

    def read_memory_request(self, request_id):
        """The owner reads one request. A done result is handed out once, then expires."""
        self._owner()
        with self.store.transaction(write=True) as conn:
            self._expire(conn)
            row = conn.execute("SELECT * FROM state_steward_memory_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if row is None:
                raise StateNotFound("unknown_memory_request")
            result = json.loads(row["result_json"]) if row["result_json"] else None
            if row["status"] == "done" and result != EXPIRED:
                conn.execute("UPDATE state_steward_memory_requests SET result_json=? WHERE request_id=?",
                             (canonical(EXPIRED), request_id))
        return {"request_id": row["request_id"], "kind": row["kind"], "status": row["status"],
                "result": result}

    @writer_only_read("list_steward_memory_requests")
    def list_memory_requests(self, status="pending", limit=50):
        with self.store.transaction(write=True) as conn:
            self._steward(conn)
            self._expire(conn)
            rows = conn.execute("SELECT request_id,kind,payload_json FROM state_steward_memory_requests "
                                "WHERE status=? ORDER BY created_at, request_id LIMIT ?", (status, limit)).fetchall()
        return {"requests": [{"request_id": r["request_id"], "kind": r["kind"],
                              "payload": json.loads(r["payload_json"])} for r in rows]}

    def complete_memory_request(self, project_id, request_key, request_id, result):
        """pending -> done with the steward runtime's result; the payload is cleared."""
        encoded = _bounded_json(result, "result")
        request_hash = _hash([request_id, result])
        with self.store.transaction(write=True) as conn:
            self._steward(conn, project_id)
            replay = self._replay(conn, "complete_steward_memory_request", request_key, request_hash)
            if replay is not None:
                return replay
            row = conn.execute("SELECT status FROM state_steward_memory_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if row is None:
                raise StateNotFound("unknown_memory_request")
            if row["status"] != "pending":
                raise StateConflict("memory_request_not_pending")
            conn.execute("UPDATE state_steward_memory_requests SET status='done',result_json=?,payload_json='{}',"
                         "completed_at=? WHERE request_id=?", (encoded, now(), request_id))
            self._expire(conn)
            return self._record(conn, "complete_steward_memory_request", request_key, request_hash,
                                {"request_id": request_id, "status": "done"})
