"""Driver-addressed notices of multi-driver P3, delivered to the P2 driver inbox.

An abandon or takeover tells the previous owner; a handoff offer tells its
receiver and a decision tells the offerer; releasing another driver's hold
tells the placer. Each is ONE message in the target's per-driver inbox
(core/driver_inbox.py, design/multi-driver-coop.md §5.2: list_driver_messages /
wait_for_driver_inbox), written inside the caller's transaction, so a
rolled-back write leaves no message. A system notice has no sender. A target
that is not a registered driver has no inbox: nothing is stored and the return
value says so (``{"skipped": ...}``).
"""
from __future__ import annotations

from core.state_graph import StateGraphError, text

# P3 notice kind -> the inbox kind it is stored as (driver_inbox_messages.kind).
KINDS = {"takeover_notice": "takeover_notice", "handoff_offer": "handoff_offer",
         "handoff_reply": "handoff_reply", "override_notice": "lease_notice"}
REF_KEYS = ("attempt_id", "claim_id", "handoff_id", "node_key")


def notify(conn, *, target_driver_id, kind, subject, body, refs=None, project_id=None,
           sender_driver_id=None) -> dict | None:
    """Address one driver inside the caller's write transaction; None when
    there is nobody to address (a legacy attempt without an owner)."""
    if not target_driver_id:
        return None
    if kind not in KINDS:
        raise StateGraphError(f"unknown notice kind {kind!r}")
    from core.driver_inbox import deliver_notice
    return deliver_notice(conn, {
        "target_driver_id": target_driver_id, "sender_driver_id": sender_driver_id, "project_id": project_id,
        "kind": KINDS[kind], "subject": text(subject, "notice subject", 300)[:200],
        "body": text(body, "notice body", 8000),
        "refs": {k: v for k, v in (refs or {}).items() if k in REF_KEYS and isinstance(v, str) and v}})
