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
SUBJECT_FIELDS = ("attempt_id", "claim_id", "subagent_id", "handoff_id", "node_key", "run_id")

# -- P2 driver-inbox adapter contract (design §5.2; P2 branch core/driver_inbox.py) --
# P2's send_driver_message(request_key, subject, body, target_driver_id | project_members,
# project_id, kind, delivery_mode, refs, _system) accepts ONLY: system kinds
# lease_notice / takeover_notice / subagent_orphaned / handoff_offer (handoff_reply
# is a DRIVER kind and needs a sender), subject 1..200 chars, body <= 8000, refs
# restricted to attempt_id / issue_id / claim_id / subagent_id (+ node_key with a
# project) whose values exist, and it opens its OWN write transaction. P3 notices
# carry two more kinds and richer refs, and are written inside the ownership
# transaction. `inbox_message` is the normalization: it maps a P3 notice to the
# exact keyword set P2 takes; wiring it means calling P2's row-level insert with
# the caller's connection (a `_deliver`-shaped helper on the P2 side) instead of
# P2's public method, which would start a nested transaction.
INBOX_KIND = {"lease_notice": "lease_notice", "takeover_notice": "takeover_notice",
              "subagent_orphaned": "subagent_orphaned", "handoff_offer": "handoff_offer",
              "handoff_reply": "handoff_reply", "override_notice": "lease_notice", "break_glass": "lease_notice"}
INBOX_REF_KEYS = ("attempt_id", "issue_id", "claim_id", "subagent_id", "node_key")
INBOX_SUBJECT_CHARS = 200
INBOX_BODY_CHARS = 8000


def inbox_message(notice: dict, *, project_members: str | None = None) -> dict:
    """Normalize one P3 notice into P2 ``send_driver_message`` keywords.

    ONE input shape: the complete stored notice - a ``driver_notices`` row
    (``refs_json``) or ``notify``'s return value (``refs``), both carrying
    ``project_id``, ``sender_driver_id``, ``kind``, ``delivery_mode``,
    ``subject``, ``body``. A notice without the sender key is refused rather
    than silently turned into a system notice."""
    for required in ("notice_id", "target_driver_id", "kind", "subject", "body"):
        if required not in notice:
            raise StateGraphError(f"inbox_message needs the complete stored notice ({required} missing)")
    if "sender_driver_id" not in notice:
        raise StateGraphError("inbox_message needs sender_driver_id (None for a system notice), not a guess")
    refs = notice["refs_json"] if "refs_json" in notice else notice.get("refs")
    if isinstance(refs, (str, bytes)):
        refs = json.loads(refs or "{}")
    refs = {k: v for k, v in (refs or {}).items() if k in INBOX_REF_KEYS and isinstance(v, str) and v}
    if "node_key" in refs and not notice.get("project_id"):
        refs.pop("node_key")
    kind = INBOX_KIND[notice["kind"]]
    system = notice.get("sender_driver_id") is None
    if kind == "handoff_reply" and system:
        kind = "takeover_notice"
    subject = (notice.get("subject") or "")[:INBOX_SUBJECT_CHARS] or "notice"
    body = (notice.get("body") or "")
    if notice["kind"] in ("override_notice", "break_glass"):
        body = f"[{notice['kind']}] " + body
    return {"request_key": notice["notice_id"], "subject": subject, "body": body[:INBOX_BODY_CHARS],
            "target_driver_id": None if project_members else notice["target_driver_id"],
            "project_members": project_members, "project_id": notice.get("project_id"), "kind": kind,
            "delivery_mode": notice.get("delivery_mode", "transient"), "refs": refs,
            "reply_to_message_id": None, "_system": system}

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


# -- the P2 inbox adapter seam ----------------------------------------------
# Both hooks take the CALLER'S connection: a notice is delivered and resolved
# inside the ownership transaction that caused it, never in a transaction of
# its own. ``deliver(conn, message, notice)`` receives the ``inbox_message``
# keyword set AND the complete stored notice - ``notice_id``,
# ``target_driver_id``, ``sender_driver_id`` (None for a system notice; P2
# derives the sender from its instance, so the adapter selects it per notice),
# ``project_id``, ``kind``, ``delivery_mode``, ``subject``, ``body``, ``refs``
# (``attempt_id`` / ``claim_id`` / ``subagent_id`` / ``handoff_id`` /
# ``node_key`` correlation) - and returns what it stored (kept in the notice's
# return value under ``inbox``); ``resolve(conn, notices, reason)`` receives
# the full notice rows being resolved, each still carrying its ``refs``,
# which is the correlation key P2 needs to mark deliveries resolved.
_ADAPTER = {"deliver": None, "resolve": None}


def set_inbox_adapter(*, deliver=None, resolve=None) -> None:
    """Install (or clear, with None) the connection-sharing P2 inbox hooks."""
    _ADAPTER["deliver"], _ADAPTER["resolve"] = deliver, resolve


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
        payload = {"notice_id": notice_id, "target_driver_id": target_driver_id, "sender_driver_id": sender_driver_id,
                   "kind": kind, "delivery_mode": delivery_mode, "subject": subject, "refs": refs, "actor": actor}
        # The subject ids ALSO sit at the top level of the payload: a wait scoped
        # with attempt_ids filters on `$.attempt_id`, so a notice about that
        # attempt must be visible to it, not only to an unscoped wait.
        for field in SUBJECT_FIELDS:
            if isinstance(refs.get(field), str):
                payload[field] = refs[field]
        store._event(conn, project_id, refs.get("node_key"), EVENT, payload)
    stored = {"notice_id": notice_id, "target_driver_id": target_driver_id, "sender_driver_id": sender_driver_id,
              "project_id": project_id, "kind": kind, "delivery_mode": delivery_mode, "subject": subject,
              "body": body, "refs": refs, "status": "pending", "created_at": created}
    if _ADAPTER["deliver"] is not None:
        # The hook gets the P2 keyword set AND the complete stored notice
        # (sender_driver_id, project_id, refs...): P2 derives the sender from
        # its instance, so the adapter must be able to select it per notice.
        stored["inbox"] = _ADAPTER["deliver"](conn, inbox_message(stored), stored)
    return stored


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
    where = " AND ".join(clauses)
    resolving = [_view(r) for r in conn.execute("SELECT * FROM driver_notices WHERE " + where, args).fetchall()]
    cursor = conn.execute("UPDATE driver_notices SET status='resolved',resolved_at=?,resolved_reason=? WHERE "
                          + where, [now(), reason, *args])
    if resolving and _ADAPTER["resolve"] is not None:
        _ADAPTER["resolve"](conn, resolving, reason)
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
