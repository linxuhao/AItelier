"""Reclaiming an attempt nobody renews: abandon or take over (P3, design §4.4).

State never infers that a remote worker stopped (core/state_external.py), and
P1 kept to that: a lapsed lease only produced an event. This module adds the
explicit, audited middle ground between "somebody is responsible" and "nobody
is": once an attempt's lease has expired AND its grace (900 s) has passed, any
member driver may either

* ``abandon_external_attempt`` - end the attempt in the new terminal status
  ``abandoned`` (not ``failed``: no criterion diagnosis, no acceptance change),
  freeing the node's one active slot. ``abandon_kind=confirmed_stopped`` is an
  attestation that the old worker is quiescent and must carry a report;
  ``unknown`` says so honestly, and the node's NEXT attempt must then declare a
  base_sha and a different workspace (one writer per checkout);
* ``take_over_attempt`` - become the owner (and controller) of the still-active
  attempt, inheriting its registered subagents (D8), each of which the taker
  then classifies with ``adopt_subagent``.

Both raise the owner fence, so every later write presenting the old fence is
``stale_fence``; a late observation on an abandoned attempt is recorded as
``late_after_abandon`` / ``superseded`` (core.state_external). Both notify the
previous owner through core.driver_notices. Neither waits beyond the grace
(Q12). An admin may act before the grace or on a legacy (unleased) attempt; the
write is then ``break_glass``. A legacy attempt otherwise needs an explicit
``override_reason`` (design §9.2 step 4).
"""
from __future__ import annotations

import uuid

from core import driver_notices
from core.state_attempts import ACTIVE, _public
from core.state_claims import (DEFAULT_LEASE_SECONDS, GRACE_SECONDS, ClaimError, add_seconds, lease_state,
                               multi_driver_on, now_stamp)
from core.state_enforcement import override_reason_text
from core.state_graph import StateGraphError, StateNotFound, digest, key, text

ABANDON_KINDS = ("confirmed_stopped", "unknown")


class StateRecovery:
    """The ONE writer of attempt ownership changes that the owner did not ask for."""

    def __init__(self, store, actor, driver_id=None, is_admin=False, claims=None):
        self.store, self.actor = store, text(actor, "authenticated actor", 320)
        self.driver_id, self.is_admin, self.claims = driver_id, is_admin is True, claims

    @staticmethod
    def _attempt(conn, attempt_id):
        row = conn.execute("SELECT * FROM state_attempts WHERE attempt_id=?", (key(attempt_id, "attempt_id"),)).fetchone()
        if row is None:
            raise StateNotFound(f"attempt {attempt_id!r} not found")
        return dict(row)

    def _reclaimable(self, conn, attempt, expected_owner_fence, override_reason, current) -> bool:
        """Every precondition of §4.4; returns whether this is a break-glass write."""
        if not multi_driver_on(conn, attempt["project_id"]):
            raise ClaimError("multi_driver_off", "reclaiming attempts needs a project with multi_driver=on")
        if attempt["status"] not in ACTIVE:
            raise ClaimError("attempt_not_active", f"attempt is {attempt['status']}; nothing to reclaim")
        if type(expected_owner_fence) is not int or expected_owner_fence != attempt["owner_fence"]:
            raise ClaimError("stale_fence", f"attempt owner fence is {attempt['owner_fence']}, "
                             f"not {expected_owner_fence}; reload", owner_fence=attempt["owner_fence"])
        expires = attempt["lease_expires_at"]
        if expires is None:
            if override_reason is None and not self.is_admin:
                raise ClaimError("override_reason_required",
                                 "this attempt predates leases (legacy_unleased); reclaiming it needs an explicit "
                                 "override_reason and the owner's confirmation (design §9.2 step 4)")
            return override_reason is None
        state = lease_state(expires, current)
        if state != "reclaimable":
            if self.is_admin:
                return True
            raise ClaimError("lease_not_expired", f"lease is {state}; reclaimable at "
                             f"{add_seconds(expires, GRACE_SECONDS)} (expiry {expires} + {GRACE_SECONDS}s grace)",
                             lease_state=state, lease_expires_at=expires,
                             reclaimable_at=add_seconds(expires, GRACE_SECONDS))
        return False

    def _release_bound_claims(self, conn, attempt, current, reason, new_holder=None):
        """Live claims bound to the attempt stop (released) or move (transferred
        plus a fresh live implement claim for the new holder)."""
        released, created = [], None
        rows = conn.execute("SELECT * FROM state_node_claims WHERE project_id=? AND attempt_id=? AND status='live'",
                            (attempt["project_id"], attempt["attempt_id"])).fetchall()
        for row in rows:
            claim = dict(row)
            status = "transferred" if new_holder else "released"
            conn.execute("UPDATE state_node_claims SET status=?,updated_at=? WHERE claim_id=?",
                         (status, current, claim["claim_id"]))
            claim.update(status=status, updated_at=current)
            self.claims._history(conn, claim, status, reason)
            self.store._event(conn, attempt["project_id"], attempt["node_key"],
                              "claim_transferred" if new_holder else "claim_released",
                              {"claim_id": claim["claim_id"], "driver_id": claim["driver_id"],
                               "purpose": claim["purpose"], "fence": claim["fence"], "reason": reason,
                               "to_driver_id": new_holder, "attempt_id": attempt["attempt_id"], "actor": self.actor})
            released.append(claim["claim_id"])
        if new_holder:
            # The replacement keeps the executor association, the purpose and
            # the WORKSPACE: a running worker's checkout stays reserved across
            # the transfer. When several claims were bound, the implement one is
            # reported. If no bound claim is live any more (a sweep retired it,
            # or it was transferred before), the LATEST claim ever bound to the
            # attempt still says which checkout the executor writes.
            sources = [dict(r) for r in rows]
            if not sources:
                last = conn.execute("SELECT * FROM state_node_claims WHERE project_id=? AND attempt_id=? "
                                    "ORDER BY created_at DESC, claim_id DESC LIMIT 1",
                                    (attempt["project_id"], attempt["attempt_id"])).fetchone()
                if last is not None:
                    sources = [dict(last)]
            from core.state_enforcement import refuse_checkout_in_use
            for claim in sources:
                if claim["workspace"]:
                    # Occupancy is re-validated in the ownership transaction: the
                    # attempt's own records are not a second writer, anyone else is.
                    refuse_checkout_in_use(conn, attempt["project_id"], claim["workspace"],
                                           exclude_attempt=attempt["attempt_id"], same_executor=claim["subagent"])
                moved = self.new_claim_for(conn, attempt, new_holder, current, reason, workspace=claim["workspace"],
                                           purpose=claim["purpose"], subagent=claim["subagent"])
                if created is None or claim["purpose"] == "implement":
                    created = moved
            if created is None:
                created = self.new_claim_for(conn, attempt, new_holder, current, reason)
        return released, created

    def new_claim_for(self, conn, attempt, driver, current, reason, workspace="", purpose="implement",
                      subagent=None, node_key=None, node_revision=None):
        """A live claim for a new holder (fence = node max + 1), inserted with its
        REAL purpose, workspace and subagent so history, event and the exclusive
        index see what it is."""
        from core.state_claims import _claim_view
        node_key = node_key or attempt["node_key"]
        fence = 1 + conn.execute("SELECT COALESCE(MAX(fence),0) FROM state_node_claims WHERE project_id=? AND node_key=?",
                                 (attempt["project_id"], node_key)).fetchone()[0]
        request_key = f"transfer:{attempt.get('attempt_id') or node_key}:{fence}"
        claim = {"claim_id": "claim-" + uuid.uuid4().hex, "project_id": attempt["project_id"],
                 "node_key": node_key, "driver_id": driver, "purpose": purpose, "status": "live",
                 "fence": fence, "node_revision": node_revision or attempt["node_revision"],
                 "attempt_id": attempt.get("attempt_id"),
                 "workspace": workspace or "", "subagent": subagent, "lease_seconds": DEFAULT_LEASE_SECONDS,
                 "lease_expires_at": add_seconds(current, DEFAULT_LEASE_SECONDS), "last_heartbeat_at": current,
                 "request_key": request_key, "request_hash": digest({"transfer": attempt.get("attempt_id"), "fence": fence,
                                                                     "node_key": node_key}),
                 "created_at": current, "updated_at": current}
        conn.execute("INSERT INTO state_node_claims(" + ",".join(claim) + ") VALUES("
                     + ",".join("?" for _ in claim) + ")", tuple(claim.values()))
        self.claims._history(conn, claim, "live", reason)
        self.store._event(conn, attempt["project_id"], node_key, "claim_acquired", {
            "claim_id": claim["claim_id"], "driver_id": driver, "purpose": purpose, "fence": fence,
            "lease_expires_at": claim["lease_expires_at"], "workspace": claim["workspace"], "subagent": subagent,
            "attempt_id": attempt.get("attempt_id"), "transfer": True, "actor": self.actor})
        return _claim_view(claim, current)

    def transfer_subagent_claims(self, conn, project_id, subagent_id, new_holder, current, reason):
        """Every live claim held FOR a subagent (any node) follows the subagent to
        its new owner: the old rows become ``transferred`` and the new owner gets
        live replacements with the same purpose, workspace and subagent."""
        moved = []
        for row in conn.execute("SELECT * FROM state_node_claims WHERE project_id=? AND subagent=? AND status='live'",
                                (project_id, subagent_id)).fetchall():
            claim = dict(row)
            if claim["driver_id"] == new_holder:
                continue
            conn.execute("UPDATE state_node_claims SET status='transferred',updated_at=? WHERE claim_id=?",
                         (current, claim["claim_id"]))
            claim.update(status="transferred", updated_at=current)
            self.claims._history(conn, claim, "transferred", reason)
            self.store._event(conn, project_id, claim["node_key"], "claim_transferred", {
                "claim_id": claim["claim_id"], "driver_id": claim["driver_id"], "purpose": claim["purpose"],
                "fence": claim["fence"], "reason": reason, "to_driver_id": new_holder, "subagent": subagent_id,
                "attempt_id": claim["attempt_id"], "actor": self.actor})
            pseudo = {"project_id": project_id, "attempt_id": claim["attempt_id"], "node_key": claim["node_key"],
                      "node_revision": claim["node_revision"]}
            moved.append(self.new_claim_for(conn, pseudo, new_holder, current, reason, workspace=claim["workspace"],
                                            purpose=claim["purpose"], subagent=subagent_id))
        return moved

    def revoke_subagent_claims(self, conn, project_id, subagent_id, current, reason):
        """An orphaned subagent's claims are revoked: nobody vouches for them."""
        revoked = []
        for row in conn.execute("SELECT * FROM state_node_claims WHERE project_id=? AND subagent=? AND status='live'",
                                (project_id, subagent_id)).fetchall():
            claim = dict(row)
            conn.execute("UPDATE state_node_claims SET status='revoked',updated_at=? WHERE claim_id=?",
                         (current, claim["claim_id"]))
            claim.update(status="revoked", updated_at=current)
            self.claims._history(conn, claim, "revoked", reason)
            self.store._event(conn, project_id, claim["node_key"], "claim_released", {
                "claim_id": claim["claim_id"], "driver_id": claim["driver_id"], "purpose": claim["purpose"],
                "fence": claim["fence"], "reason": reason, "revoked": True, "subagent": subagent_id,
                "attempt_id": claim["attempt_id"], "actor": self.actor, "break_glass": False})
            revoked.append(claim["claim_id"])
        return revoked

    @staticmethod
    def _void_open_handoffs(conn, attempt_id, current, reason):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_handoffs'").fetchone():
            return 0
        return conn.execute("UPDATE state_handoffs SET status='withdrawn',decided_by='system',decision_reason=?,"
                            "updated_at=? WHERE attempt_id=? AND status='offered'",
                            (reason, current, attempt_id)).rowcount

    # -- abandon -----------------------------------------------------------
    def abandon_external_attempt(self, attempt_id, expected_owner_fence, abandon_kind, reason,
                                 report_ref=None, report_sha256=None, override_reason=None):
        reason = text(reason, "abandon reason", 4000)
        override_reason = override_reason_text(override_reason)
        if abandon_kind not in ABANDON_KINDS:
            raise StateGraphError("abandon_kind must be confirmed_stopped or unknown")
        if not self.driver_id and not self.is_admin:
            raise ClaimError("driver_identity_required", "reclaiming belongs to a registered driver")
        report_bytes = None
        if abandon_kind == "confirmed_stopped":
            if not report_ref or not report_sha256:
                raise StateGraphError("confirmed_stopped is an attestation: it needs report_ref and report_sha256")
            from core.state_report_integrity import retain_report
            report_ref, report_bytes = retain_report(report_ref, report_sha256, completed=False)
        elif report_ref is not None or report_sha256 is not None:
            raise StateGraphError("abandon_kind=unknown carries no quiescence report; omit report_ref/report_sha256")
        current = now_stamp()
        with self.store.transaction(write=True) as conn:
            attempt = self._attempt(conn, attempt_id)
            if attempt["execution_kind"] != "external":
                raise ClaimError("not_external", "only an external attempt can be abandoned; a SkillFlow attempt's "
                                 "run is alive or not by the engine's account - take_over_attempt its controller")
            break_glass = self._reclaimable(conn, attempt, expected_owner_fence, override_reason, current)
            fence = attempt["owner_fence"] + 1
            if report_bytes is not None:
                from core.state_report_integrity import store_report_blob
                store_report_blob(conn, report_ref, report_sha256, report_bytes)
            conn.execute("UPDATE state_attempts SET status='abandoned',abandon_kind=?,error=?,owner_fence=?,"
                         "updated_at=? WHERE attempt_id=?", (abandon_kind, reason, fence, current, attempt_id))
            conn.execute("UPDATE state_external_owners SET status='abandoned',updated_at=?,settled_at=? "
                         "WHERE attempt_id=?", (current, current, attempt_id))
            released, _ = self._release_bound_claims(conn, attempt, current, f"attempt abandoned: {reason}")
            subagents = self._settle_subagents(conn, attempt, current, abandon_kind)
            voided = self._void_open_handoffs(conn, attempt_id, current, "attempt abandoned")
            payload = {"attempt_id": attempt_id, "abandon_kind": abandon_kind, "reason": reason,
                       "previous_owner_driver_id": attempt["owner_driver_id"], "previous_fence": attempt["owner_fence"],
                       "fence": fence, "actor": self.actor, "driver_id": self.driver_id, "break_glass": break_glass,
                       "override_reason": override_reason, "report_sha256": report_sha256,
                       "released_claims": released, "subagents": subagents, "voided_handoffs": voided}
            self.store._event(conn, attempt["project_id"], attempt["node_key"], "attempt_abandoned", payload)
            notice = None
            if attempt["owner_driver_id"] and attempt["owner_driver_id"] != self.driver_id:
                notice = driver_notices.notify(
                    conn, self.store, target_driver_id=attempt["owner_driver_id"],
                    kind="break_glass" if break_glass else "takeover_notice", project_id=attempt["project_id"],
                    subject=f"your attempt {attempt_id} on {attempt['node_key']} was abandoned ({abandon_kind})",
                    body=(f"{self.actor} abandoned attempt {attempt_id} (node {attempt['node_key']}) after its lease "
                          f"lapsed: {reason}. Its fence is now {fence}; a late report from you is recorded as "
                          "late_after_abandon/superseded and can no longer make the node CANDIDATE. If you really "
                          "finished, register the same artifact under a NEW attempt."
                          + (" quiescence=unknown: do not write that checkout again." if abandon_kind == "unknown" else "")),
                    refs={"attempt_id": attempt_id, "node_key": attempt["node_key"], "fence": fence,
                          "abandon_kind": abandon_kind, "break_glass": break_glass}, actor=self.actor)
            result = _public(self._attempt(conn, attempt_id))
        return {**result, "break_glass": break_glass, "released_claims": released, "subagents": subagents,
                "notified": notice}

    @staticmethod
    def _settle_subagents(conn, attempt, current, abandon_kind):
        """confirmed_stopped covers the attempt's subagents too (they are settled);
        unknown leaves them as they are - nobody attested anything about them."""
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='driver_subagents'").fetchone():
            return []
        rows = conn.execute("SELECT subagent_id,status FROM driver_subagents WHERE attempt_id=? "
                            "AND status IN ('active','adopted') ORDER BY subagent_id", (attempt["attempt_id"],)).fetchall()
        out = []
        for row in rows:
            status = "settled" if abandon_kind == "confirmed_stopped" else row["status"]
            if status != row["status"]:
                conn.execute("UPDATE driver_subagents SET status=?,updated_at=? WHERE subagent_id=?",
                             (status, current, row["subagent_id"]))
            out.append({"subagent_id": row["subagent_id"], "status": status})
        return out

    # -- take over ---------------------------------------------------------
    def take_over_attempt(self, attempt_id, expected_owner_fence, reason, override_reason=None):
        reason = text(reason, "takeover reason", 4000)
        override_reason = override_reason_text(override_reason)
        if not self.driver_id:
            raise ClaimError("driver_identity_required", "an attempt is owned by a registered driver; the owner's "
                             "e-mail cannot take one over - use an admin driver token (owner-cli)")
        current = now_stamp()
        with self.store.transaction(write=True) as conn:
            attempt = self._attempt(conn, attempt_id)
            if attempt["owner_driver_id"] == self.driver_id:
                raise ClaimError("already_owner", "you already own this attempt; heartbeat it instead")
            break_glass = self._reclaimable(conn, attempt, expected_owner_fence, override_reason, current)
            fence = attempt["owner_fence"] + 1
            expires = add_seconds(current, DEFAULT_LEASE_SECONDS)
            conn.execute("UPDATE state_attempts SET owner_driver_id=?,owner_fence=?,lease_expires_at=?,"
                         "last_heartbeat_at=?,updated_at=? WHERE attempt_id=?",
                         (self.driver_id, fence, expires, current, current, attempt_id))
            _, claim = self._release_bound_claims(conn, attempt, current, f"attempt taken over: {reason}",
                                                 new_holder=self.driver_id)
            subagents = self._transfer_subagents(conn, attempt, current, expires)
            voided = self._void_open_handoffs(conn, attempt_id, current, "attempt taken over")
            payload = {"attempt_id": attempt_id, "mode": "takeover", "from_driver_id": attempt["owner_driver_id"],
                       "to_driver_id": self.driver_id, "previous_fence": attempt["owner_fence"], "fence": fence,
                       "lease_expires_at": expires, "reason": reason, "override_reason": override_reason,
                       "break_glass": break_glass, "actor": self.actor,
                       "subagents": [s["subagent_id"] for s in subagents], "voided_handoffs": voided}
            self.store._event(conn, attempt["project_id"], attempt["node_key"], "attempt_ownership_transferred", payload)
            notice = None
            if attempt["owner_driver_id"]:
                notice = driver_notices.notify(
                    conn, self.store, target_driver_id=attempt["owner_driver_id"],
                    kind="break_glass" if break_glass else "takeover_notice", project_id=attempt["project_id"],
                    subject=f"your attempt {attempt_id} on {attempt['node_key']} was taken over by {self.driver_id}",
                    body=(f"{self.actor} took over attempt {attempt_id} (node {attempt['node_key']}) after its lease "
                          f"lapsed: {reason}. Fence is now {fence}; your observe/report calls with the old fence are "
                          f"refused (stale_fence). {len(subagents)} registered subagent(s) moved to the new owner; "
                          "you will be told about any it cannot observe."),
                    refs={"attempt_id": attempt_id, "node_key": attempt["node_key"], "fence": fence,
                          "to_driver_id": self.driver_id, "break_glass": break_glass}, actor=self.actor)
            result = _public(self._attempt(conn, attempt_id))
        return {**result, "break_glass": break_glass, "claim": claim, "subagents": subagents, "notified": notice,
                "next": "adopt_subagent each inherited subagent (controllable | observable_only | unobservable)"}

    def _transfer_subagents(self, conn, attempt, current, expires):
        """D8: the attempt's registered, still-open subagents move to the taker
        with a raised fence; classification is the taker's next step."""
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='driver_subagents'").fetchone():
            return []
        rows = conn.execute("SELECT * FROM driver_subagents WHERE attempt_id=? AND status IN ('active','adopted') "
                            "ORDER BY subagent_id", (attempt["attempt_id"],)).fetchall()
        out = []
        for row in rows:
            fence = row["fence"] + 1
            conn.execute("UPDATE driver_subagents SET owner_driver_id=?,fence=?,status='active',observability=NULL,"
                         "lease_expires_at=?,last_heartbeat_at=?,updated_at=? WHERE subagent_id=?",
                         (self.driver_id, fence, expires, current, current, row["subagent_id"]))
            claims = self.transfer_subagent_claims(conn, attempt["project_id"], row["subagent_id"], self.driver_id,
                                                   current, f"subagent {row['subagent_id']} moved with its attempt")
            self.store._event(conn, attempt["project_id"], attempt["node_key"], "subagent_transferred", {
                "subagent_id": row["subagent_id"], "attempt_id": attempt["attempt_id"],
                "from_driver_id": row["owner_driver_id"], "to_driver_id": self.driver_id, "fence": fence,
                "claims": [c["claim_id"] for c in claims], "actor": self.actor})
            out.append({"subagent_id": row["subagent_id"], "fence": fence, "host": row["host"],
                        "runtime": row["runtime"], "workspace": row["workspace"],
                        "checkpoint_ref": row["checkpoint_ref"], "status": "active", "needs": "adopt_subagent"})
        return out
