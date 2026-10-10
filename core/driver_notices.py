"""The ONE notifier for driver-addressed system notices (multi-driver P3).

Every P3 situation that must reach a specific DRIVER - its attempt was abandoned
or taken over, one of its subagents became an orphan, a handoff was offered to
or answered for it, somebody overrode its live claim, an admin wrote around its
claim (break glass) - goes through ``notify``. Nothing else in P3 addresses a
driver directly.

The per-driver inbox of design/multi-driver-coop.md §5.2 (phase P2) is being
built in parallel and is not on main yet, so this module delivers in the two
durable ways the current schema has: one ``driver_notice`` row in the project's
``state_events`` stream (payload carries ``target_driver_id`` and ``kind``, so a
driver waiting on the project with ``wait_for_state_change`` wakes), and one
pending row in ``driver_notices``, which the target driver reads back with
``list_driver_notices`` and the system resolves when the matter is settled.

Re-pointing to the P2 inbox is ONE edit: ``_deliver`` is the only place that
knows how a notice is stored; its callers hand it the same (target, kind,
subject, body, refs, delivery_mode) tuple the inbox takes. ``sender_driver_id``
is NULL for every system notice, as §5.2 specifies.
"""
from __future__ import annotations

import json
import uuid

from core.state_graph import StateGraphError, canonical, now, text
from core.state_privacy import UntrustedDatabase, writer_only_read

KINDS = ("lease_notice", "takeover_notice", "subagent_orphaned", "handoff_offer", "handoff_reply",
         "override_notice", "break_glass")
DELIVERY_MODES = ("transient", "standing")
STATUSES = ("pending", "resolved")
EVENT = "driver_notice"
MAX_BODY_CHARS = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_notices (
    notice_id TEXT PRIMARY KEY,
    target_driver_id TEXT NOT NULL,
    sender_driver_id TEXT,
    project_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('lease_notice','takeover_notice','subagent_orphaned','handoff_offer',
                                      'handoff_reply','override_notice','break_glass')),
    delivery_mode TEXT NOT NULL CHECK(delivery_mode IN ('transient','standing')),
    subject TEXT NOT NULL, body TEXT NOT NULL, refs_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('pending','resolved')),
    created_at TEXT NOT NULL, resolved_at TEXT, resolved_reason TEXT
);
CREATE INDEX IF NOT EXISTS driver_notices_target ON driver_notices(target_driver_id, status, created_at);
CREATE INDEX IF NOT EXISTS driver_notices_project ON driver_notices(project_id, status, created_at);
CREATE TRIGGER IF NOT EXISTS driver_notices_no_delete BEFORE DELETE ON driver_notices
BEGIN SELECT RAISE(ABORT,'driver notices are resolved, never deleted'); END;
"""


def initialize(db) -> None:
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _deliver(conn, store, target_driver_id, kind, subject, body, refs, delivery_mode, project_id,
             sender_driver_id, actor) -> dict:
    """Store ONE notice. The single place that knows the delivery mechanism."""
    notice_id = "notice-" + uuid.uuid4().hex
    created = now()
    conn.execute("INSERT INTO driver_notices(notice_id,target_driver_id,sender_driver_id,project_id,kind,"
                 "delivery_mode,subject,body,refs_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)",
                 (notice_id, target_driver_id, sender_driver_id, project_id, kind, delivery_mode, subject, body,
                  canonical(refs), created))
    if project_id is not None:
        store._event(conn, project_id, refs.get("node_key"), EVENT, {
            "notice_id": notice_id, "target_driver_id": target_driver_id, "sender_driver_id": sender_driver_id,
            "kind": kind, "delivery_mode": delivery_mode, "subject": subject, "refs": refs, "actor": actor})
    return {"notice_id": notice_id, "target_driver_id": target_driver_id, "kind": kind,
            "delivery_mode": delivery_mode, "subject": subject, "created_at": created}


def notify(conn, store, *, target_driver_id, kind, subject, body, refs=None, project_id=None,
           delivery_mode="transient", sender_driver_id=None, actor="system") -> dict | None:
    """Address one driver inside the caller's write transaction.

    Returns the stored notice, or None when there is nobody to address (a
    legacy attempt without an owner): a notice is never sent to nobody and
    never to the driver that caused it when that driver is the target of its
    own action's consequence is the caller's decision, not this function's.
    """
    if not target_driver_id:
        return None
    if kind not in KINDS:
        raise StateGraphError(f"unknown notice kind {kind!r}")
    if delivery_mode not in DELIVERY_MODES:
        raise StateGraphError("delivery_mode must be transient or standing")
    subject = text(subject, "notice subject", 300)
    body = text(body, "notice body", MAX_BODY_CHARS)
    refs = dict(refs or {})
    canonical(refs)
    return _deliver(conn, store, target_driver_id, kind, subject, body, refs, delivery_mode,
                    project_id, sender_driver_id, actor)


def resolve(conn, *, notice_ids=None, project_id=None, kind=None, ref_key=None, ref_value=None, reason="") -> int:
    """Close pending notices, by id or by (project, kind, one ref). Returns the count."""
    clauses, args = ["status='pending'"], []
    if notice_ids:
        clauses.append("notice_id IN (" + ",".join("?" for _ in notice_ids) + ")")
        args.extend(notice_ids)
    if project_id is not None:
        clauses.append("project_id=?")
        args.append(project_id)
    if kind is not None:
        clauses.append("kind=?")
        args.append(kind)
    if ref_key is not None:
        clauses.append("json_extract(refs_json,?)=?")
        args.extend(["$." + ref_key, ref_value])
    if len(clauses) == 1:
        raise StateGraphError("resolve needs a selector")
    cursor = conn.execute("UPDATE driver_notices SET status='resolved',resolved_at=?,resolved_reason=? WHERE "
                          + " AND ".join(clauses), [now(), reason, *args])
    return cursor.rowcount


def _view(row) -> dict:
    data = dict(row)
    data["refs"] = json.loads(data.pop("refs_json") or "{}")
    return data


class DriverNotices:
    """Read side: a driver reads its own notices; an admin may read anyone's."""

    def __init__(self, store, actor, driver_id=None, is_admin=False):
        self.store = store
        if not isinstance(store.db, UntrustedDatabase):
            initialize(store.db)
        self.actor, self.driver_id, self.is_admin = actor, driver_id, is_admin is True
        self.project_read_trusted = store.project_read_trusted

    @writer_only_read("list_driver_notices")
    def list_driver_notices(self, project_id, driver_id=None, statuses=None, kinds=None, limit=100):
        from core.state_claims import ClaimError
        target = driver_id or self.driver_id
        if not target:
            raise ClaimError("driver_identity_required", "notices are addressed to a registered driver; pass driver_id")
        # A DRIVER reads its own notices (an admin driver anyone's). A writer
        # credential that is not a driver - the owner's e-mail, the legacy
        # shared operator - already passed the transport's writer gate and has
        # no inbox of its own; it names the driver it reads for.
        if self.driver_id is not None and target != self.driver_id and not self.is_admin:
            raise ClaimError("not_notice_target", "only the addressed driver (or an admin) reads these notices")
        statuses = statuses or ["pending"]
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            sql = ("SELECT * FROM driver_notices WHERE project_id=? AND target_driver_id=? AND status IN ("
                   + ",".join("?" for _ in statuses) + ")")
            args = [project_id, target, *statuses]
            if kinds:
                sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
                args.extend(kinds)
            rows = conn.execute(sql + " ORDER BY created_at DESC, notice_id LIMIT ?", [*args, limit + 1]).fetchall()
        return {"notices": [_view(r) for r in rows[:limit]], "truncated": len(rows) > limit,
                "target_driver_id": target,
                "delivery": "state_event driver_notice + driver_notices row (P2 driver inbox not deployed)"}
