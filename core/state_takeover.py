"""Taking over the claim of an OFFLINE driver (presence v2, iss-ff73340f04c24858).

The ONE place where presence is authority. Everything else reads presence as
information only (core.driver_presence; guarded by
tests/unit/test_driver_offline_takeover.py).

``take_over_claim(project_id, claim_id, expected_fence, reason, request_key)``
lets an active member of the project move another driver's LIVE claim to
itself when, at the moment of the write transaction, the owner's EFFECTIVE
presence is ``offline`` (declared offline and not waiting, or unseen for
longer than the v1 stale bound). ``online`` and ``away`` owners are never taken
over. In one transaction the old claim becomes ``taken_over`` (terminal) and
the caller gets a NEW live claim on the same node with the same purpose,
workspace and lease length, fence = node max + 1, a fresh lease and no bound
attempt; history rows, ``claim_taken_over`` + ``claim_acquired`` events and a
takeover notice in the previous owner's driver inbox record it. Its later
heartbeat or release of the old claim is ``stale_fence``; a dispatch naming
the old claim/fence is ``stale_fence``; claim_node is ``claimed_by_other``.

What it does NOT do (minimum consistent scope): attempts keep their owner,
owner fence and lease. The previous owner may still report on, and renew, an
attempt it started (the attempt fence is separate from the claim fence), and
while that attempt is active the node admits no new attempt (one active
attempt per node). Moving an attempt stays take_over_attempt /
abandon_external_attempt, after its lease lapsed. The result lists such
attempts under ``active_attempts``. Node holds and their reasons are untouched.
"""
from __future__ import annotations

import uuid

from core import driver_notices, driver_presence
from core.state_claims import (EXCLUSIVE_PURPOSES, ClaimError, _claim_view, add_seconds, digest, now_stamp)
from core.state_graph import StateGraphError, key, text

MIN_REASON_CHARS = 20
MAX_REASON_CHARS = 300
_ACTIVE_ATTEMPT = ("reserved", "launching", "running", "paused", "unknown")


def _member(conn, project_id, driver_id) -> bool:
    """An ACTIVE driver that is a member of the project, read in this transaction.
    Fails closed: no registry tables means no membership."""
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                          "AND name IN ('drivers','project_drivers')")}
    if present != {"drivers", "project_drivers"}:
        return False
    return conn.execute("SELECT 1 FROM project_drivers m JOIN drivers d USING(driver_id) WHERE m.project_id=? "
                        "AND m.driver_id=? AND m.status='member' AND d.status='active'",
                        (project_id, driver_id)).fetchone() is not None


def _evidence(presence) -> dict:
    """The presence facts the decision used, as recorded in the event."""
    return {key_: presence[key_] for key_ in ("status", "basis", "declared", "reason", "since", "expected_return",
                                              "last_seen_at", "seconds_since_seen", "waiting", "last_wait_end")}


class StateTakeover:
    """The ONE writer of presence-based claim takeover."""

    def __init__(self, store, actor, driver_id=None, claims=None):
        self.store, self.actor = store, text(actor, "authenticated actor", 320)
        self.driver_id, self.claims = driver_id, claims

    def take_over_claim(self, project_id, claim_id, expected_fence, reason, request_key):
        driver = self.claims._require_driver()
        reason = text(reason, "takeover reason", MAX_REASON_CHARS)
        if len(reason.strip()) < MIN_REASON_CHARS:
            raise StateGraphError(f"reason must say why, in {MIN_REASON_CHARS}..{MAX_REASON_CHARS} characters")
        key(request_key, "request_key")
        key(claim_id, "claim_id")
        if type(expected_fence) is not int:
            raise StateGraphError("expected_fence must be an integer")
        # A claim_node request_key cannot contain ':', so this never collides.
        stored_key = "takeover:" + request_key
        request_hash = digest({"take_over_claim": claim_id, "expected_fence": expected_fence, "reason": reason})
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            current = now_stamp()
            replay = conn.execute("SELECT * FROM state_node_claims WHERE project_id=? AND driver_id=? AND request_key=?",
                                  (project_id, driver, stored_key)).fetchone()
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise ClaimError("request_key_reused", "this request_key was used for a different takeover")
                old = self.claims._claim(conn, project_id, claim_id)
                return {"taken_over": _claim_view(old, current), "claim": _claim_view(replay, current),
                        "idempotent": True}
            old = self.claims._claim(conn, project_id, claim_id)
            if not _member(conn, project_id, driver):
                raise ClaimError("not_a_member", f"taking over a claim is for active members of project {project_id}; "
                                 f"driver {driver} is not one", project_id=project_id)
            if old["driver_id"] == driver:
                raise ClaimError("already_held", f"claim {claim_id} is yours (fence {old['fence']})")
            # Retire lapsed claims first: a claim past its grace is simply free (claim_node it).
            self.claims._sweep_conn(conn, project_id, current, old["node_key"])
            old = self.claims._claim(conn, project_id, claim_id)
            if expected_fence != old["fence"]:
                raise ClaimError("stale_fence", f"claim fence is {old['fence']}, not {expected_fence}; reload",
                                 fence=old["fence"])
            if old["status"] != "live":
                raise ClaimError("claim_not_live", f"claim is {old['status']}", status=old["status"])
            node = self.store._node(conn, project_id, old["node_key"])
            if node["status"] == "SUPERSEDED" or (old["purpose"] == "implement" and node["status"] == "VERIFIED"):
                raise ClaimError("node_closed", f"node is {node['status']}")
            # THE authority: judged here, inside the write transaction, on the
            # same snapshot as the claim. A wait, a heartbeat or a claim_node by
            # the owner that commits first makes it present (or its fence moves).
            presence = driver_presence.effective_presence(conn, old["driver_id"])
            if presence["status"] != "offline":
                raise ClaimError("owner_not_offline", f"claim holder {old['driver_id']} is {presence['status']} "
                                 f"(basis {presence['basis']}, last seen {presence['last_seen_at'] or 'never'}, "
                                 f"declared offline: {presence['declared']}); only an offline owner's claim can be "
                                 "taken over - ask the holder to release or hand it off", owner_presence=presence)
            evidence = _evidence(presence)
            owner = old["driver_id"]
            conn.execute("UPDATE state_node_claims SET status='taken_over',updated_at=? WHERE claim_id=?",
                         (current, claim_id))
            old.update(status="taken_over", updated_at=current)
            self.claims._history(conn, old, "taken_over", f"taken over by {driver}: {reason}")
            fence = 1 + conn.execute("SELECT COALESCE(MAX(fence),0) FROM state_node_claims WHERE project_id=? "
                                     "AND node_key=?", (project_id, old["node_key"])).fetchone()[0]
            claim = {"claim_id": "claim-" + uuid.uuid4().hex, "project_id": project_id, "node_key": old["node_key"],
                     "driver_id": driver, "purpose": old["purpose"], "status": "live", "fence": fence,
                     "node_revision": node["revision"], "attempt_id": None, "workspace": old["workspace"],
                     "subagent": None, "lease_seconds": old["lease_seconds"],
                     "lease_expires_at": add_seconds(current, old["lease_seconds"]), "last_heartbeat_at": current,
                     "request_key": stored_key, "request_hash": request_hash,
                     "created_at": current, "updated_at": current}
            conn.execute("INSERT INTO state_node_claims(" + ",".join(claim) + ") VALUES("
                         + ",".join("?" for _ in claim) + ")", tuple(claim.values()))
            self.claims._history(conn, claim, "live", f"took over {claim_id} from {owner}: {reason}")
            self.store._event(conn, project_id, old["node_key"], "claim_taken_over", {
                "claim_id": claim_id, "new_claim_id": claim["claim_id"], "from_driver_id": owner,
                "to_driver_id": driver, "purpose": old["purpose"], "fence": old["fence"], "new_fence": fence,
                "reason": reason, "owner_presence": evidence, "actor": self.actor})
            self.store._event(conn, project_id, old["node_key"], "claim_acquired", {
                "claim_id": claim["claim_id"], "driver_id": driver, "purpose": claim["purpose"], "fence": fence,
                "lease_expires_at": claim["lease_expires_at"], "workspace": claim["workspace"], "subagent": None,
                "taken_over_from": claim_id, "actor": self.actor})
            attempts = [dict(r) for r in conn.execute(
                "SELECT attempt_id, owner_driver_id, owner_fence, status, lease_expires_at FROM state_attempts "
                "WHERE project_id=? AND node_key=? AND status IN (" + ",".join("?" * len(_ACTIVE_ATTEMPT)) + ")",
                (project_id, old["node_key"], *_ACTIVE_ATTEMPT))]
            notice = driver_notices.notify(
                conn, target_driver_id=owner, kind="takeover_notice", project_id=project_id,
                sender_driver_id=driver,
                subject=f"{driver} took over your {old['purpose']} claim on {old['node_key']}",
                body=(f"You were {presence['status']} ({presence['basis']}). Claim {claim_id} (fence {old['fence']}) "
                      f"is now taken_over; {driver} holds {claim['claim_id']} with fence {fence}. Your fence is "
                      f"stale: stop renewing and stop writing that claim. Reason: {reason}"),
                refs={"claim_id": claim_id, "node_key": old["node_key"]})
            result = {"taken_over": _claim_view(old, current), "claim": _claim_view(claim, current),
                      "owner_presence": evidence, "notified": notice, "idempotent": False,
                      "active_attempts": attempts}
            if attempts and old["purpose"] in EXCLUSIVE_PURPOSES:
                result["note"] = ("attempts are not moved by a claim takeover: an active attempt keeps its owner and "
                                  "lease, so no new attempt starts on this node until it settles or its lease "
                                  "lapses (take_over_attempt / abandon_external_attempt)")
            return result
