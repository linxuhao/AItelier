"""Claim ENFORCEMENT for multi-driver State projects (P3, design §7.3).

P1 recorded claims and leases and enforced nothing. This module holds every
rule that turns them into refusals, and the one switch behind which those rules
live: ``state_project_policy.claim_enforcement`` (default ``off``). Owner
decision 2026-10-09: ``set_multi_driver(on)`` alone keeps P1's record-only
behaviour; a project enforces only when BOTH ``multi_driver=on`` and
``claim_enforcement=on``. With the switch off every function here is a no-op
that returns "nothing to enforce", so such a project behaves exactly as before.

Rules (design §7.3), each with a stable error code:

1. dispatch - ``start_attempt`` / ``start_external_attempt`` need the caller's
   live ``implement`` claim on the node (``claim_required``); a supplied
   ``claim_id``/``fence`` must name that claim (``stale_fence``).
2. observe - ``report_external_attempt`` needs the attempt's owner and its
   current fence (``not_attempt_owner``, ``fence_required``, ``stale_fence``);
   implemented in core.state_external.
4. structural writes - revise/split/supersede/facet over a node (or one of its
   dependents, which the write invalidates) that another driver holds a live
   claim or active attempt on need ``override_reason``
   (``override_reason_required``); with it the write proceeds and the holder is
   notified.
5. hold - releasing another driver's hold needs ``override_reason``.
8. one writer per checkout - a live exclusive claim already declaring the same
   ``workspace`` refuses a second (``workspace_in_use``); in core.state_claims.
9. one controller per run - a SkillFlow checkpoint is answered only by the
   attempt's owner (``not_attempt_owner``).
Q13. an admin (owner e-mail, ``owner-cli``) is never refused by a claim rule;
   its write is flagged ``break_glass`` and the affected driver is notified.
"""
from __future__ import annotations

from core.state_claims import ClaimError, lease_state, now_stamp
from core.state_graph import text

_ACTIVE = ("reserved", "launching", "running", "paused", "unknown")
MAX_OVERRIDE_REASON = 2000


def _has_table(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def enforced(conn, project_id) -> bool:
    """True only when multi_driver=on AND claim_enforcement=on."""
    from core.state_metadata import project_policy
    if not _has_table(conn, "state_project_policy"):
        return False
    row = project_policy(conn, project_id)
    return row.get("multi_driver") == "on" and row.get("claim_enforcement") == "on"


def override_reason_text(value):
    if value is None:
        return None
    return text(value, "override_reason", MAX_OVERRIDE_REASON)


# -- rule 1: dispatch ---------------------------------------------------------
def dispatch_claim(conn, project_id, node_key, driver_id, is_admin, claim_id=None, fence=None):
    """The live implement claim a dispatch in an enforced project rides on.

    Returns ``(claim_row_or_None, break_glass)``. Outside an enforced project
    an explicitly supplied claim is still checked (a wrong fence is a wrong
    fence) but none is required.
    """
    if not _has_table(conn, "state_node_claims"):
        # A store built by StateAttempts alone (tests, embedders) has no claim
        # tables and therefore nothing to enforce or bind.
        if claim_id is not None:
            raise ClaimError("stale_fence", f"claim {claim_id} does not exist here")
        return None, False
    live = conn.execute("SELECT * FROM state_node_claims WHERE project_id=? AND node_key=? AND status='live' "
                        "AND purpose='implement'", (project_id, node_key)).fetchone()
    live = dict(live) if live else None
    if claim_id is not None:
        if live is None or live["claim_id"] != claim_id:
            raise ClaimError("stale_fence", f"claim {claim_id} is not the live implement claim of {node_key}; "
                             "reload with list_claims")
        if live["driver_id"] != driver_id:
            raise ClaimError("not_claim_owner", f"claim {claim_id} belongs to driver {live['driver_id']}")
        if fence is not None and fence != live["fence"]:
            raise ClaimError("stale_fence", f"claim fence is {live['fence']}, not {fence}; reload")
    if not enforced(conn, project_id):
        return (live if live and live["driver_id"] == driver_id else None), False
    if live is not None and live["driver_id"] == driver_id:
        if fence is not None and fence != live["fence"]:
            raise ClaimError("stale_fence", f"claim fence is {live['fence']}, not {fence}; reload")
        return live, False
    if is_admin:
        return None, True
    if live is None:
        raise ClaimError("claim_required", f"this project enforces claims: claim_node(purpose=implement) on "
                         f"{node_key} before starting an attempt", node_key=node_key)
    raise ClaimError("claimed_by_other", f"node {node_key} is claimed for implement by driver {live['driver_id']} "
                     f"until {live['lease_expires_at']}", holder=live["driver_id"], claim_id=live["claim_id"],
                     lease_expires_at=live["lease_expires_at"],
                     lease_state=lease_state(live["lease_expires_at"], now_stamp()))


# -- rule 4/5: structural writes ---------------------------------------------
def dependents(conn, project_id, node_key) -> list[str]:
    """The node and every transitive dependent: what a revision invalidates."""
    rows = conn.execute("WITH RECURSIVE affected(k) AS (SELECT ? UNION "
                        "SELECT d.node_key FROM state_dependencies d JOIN affected a ON d.dependency_key=a.k "
                        "WHERE d.project_id=?) SELECT k FROM affected", (node_key, project_id)).fetchall()
    return sorted(r[0] for r in rows)


def holders(conn, project_id, node_keys, driver_id) -> list[dict]:
    """Other drivers' live claims and active owned attempts on these nodes."""
    if not node_keys or not _has_table(conn, "state_node_claims"):
        return []
    marks = ",".join("?" for _ in node_keys)
    found = []
    for row in conn.execute("SELECT claim_id,node_key,driver_id,purpose,fence,lease_expires_at FROM state_node_claims "
                            f"WHERE project_id=? AND status='live' AND node_key IN ({marks})", (project_id, *node_keys)):
        if row["driver_id"] != driver_id:
            found.append({"kind": "claim", "id": row["claim_id"], "node_key": row["node_key"],
                          "driver_id": row["driver_id"], "purpose": row["purpose"], "fence": row["fence"]})
    for row in conn.execute("SELECT attempt_id,node_key,owner_driver_id,owner_fence FROM state_attempts "
                            f"WHERE project_id=? AND owner_driver_id IS NOT NULL AND node_key IN ({marks}) "
                            "AND status IN (" + ",".join("?" for _ in _ACTIVE) + ")",
                            (project_id, *node_keys, *_ACTIVE)):
        if row["owner_driver_id"] != driver_id:
            found.append({"kind": "attempt", "id": row["attempt_id"], "node_key": row["node_key"],
                          "driver_id": row["owner_driver_id"], "fence": row["owner_fence"]})
    return found


class StructuralGuard:
    """Rule 4 for one structural write; constructed per service.

    ``check`` runs before the write (its own read); ``notify`` runs after the
    write succeeded. A write the engine then refuses leaves no notice behind.
    """

    def __init__(self, store, actor, driver_id, is_admin):
        self.store, self.actor, self.driver_id, self.is_admin = store, actor, driver_id, is_admin is True

    def check(self, project_id, node_key, override_reason, action) -> dict | None:
        override_reason = override_reason_text(override_reason)
        with self.store.transaction() as conn:
            if not enforced(conn, project_id):
                return None
            affected = dependents(conn, project_id, node_key)
            held = holders(conn, project_id, affected, self.driver_id)
        if not held:
            return None
        if override_reason is None and not self.is_admin:
            names = sorted({h["driver_id"] for h in held})
            raise ClaimError("override_reason_required",
                             f"{action} on {node_key} affects work held by driver(s) {', '.join(names)}; "
                             "pass override_reason to proceed (the holders are notified)", holders=held)
        return {"action": action, "node_key": node_key, "held": held, "override_reason": override_reason,
                "break_glass": override_reason is None and self.is_admin}

    def notify(self, project_id, check: dict | None) -> list[dict]:
        if not check:
            return []
        from core import driver_notices
        sent = []
        with self.store.transaction(write=True) as conn:
            for driver in sorted({h["driver_id"] for h in check["held"]}):
                mine = [h for h in check["held"] if h["driver_id"] == driver]
                kind = "break_glass" if check["break_glass"] else "override_notice"
                body = (f"{self.actor} performed {check['action']} on node {check['node_key']} while you hold "
                        + ", ".join(f"{h['kind']} {h['id']} on {h['node_key']}" for h in mine) + ". "
                        + ("Admin break-glass write; no override_reason." if check["break_glass"]
                           else f"override_reason: {check['override_reason']}")
                        + " Re-read the node before continuing; a revision supersedes in-flight attempts.")
                sent.append(driver_notices.notify(
                    conn, self.store, target_driver_id=driver, kind=kind, project_id=project_id,
                    subject=f"{check['action']} over your claim on {check['node_key']}", body=body,
                    refs={"node_key": check["node_key"], "action": check["action"],
                          "held": mine, "by_actor": self.actor, "break_glass": check["break_glass"],
                          "override_reason": check["override_reason"]},
                    actor=self.actor))
            self.store._event(conn, project_id, check["node_key"], "claim_overridden", {
                "action": check["action"], "actor": self.actor, "driver_id": self.driver_id,
                "override_reason": check["override_reason"], "break_glass": check["break_glass"],
                "held": check["held"]})
        return sent


def hold_release_check(conn, project_id, node_key, actor, driver_id, is_admin, override_reason):
    """Rule 5: releasing a hold another driver placed needs override_reason."""
    if not enforced(conn, project_id):
        return None
    row = conn.execute("SELECT * FROM state_node_holds WHERE project_id=? AND node_key=?",
                       (project_id, node_key)).fetchone()
    if row is None or not row["held"] or row["actor"] == actor:
        return None
    placer = row["actor"]
    placer_driver = placer[len("driver:"):] if placer.startswith("driver:") else None
    if placer_driver == driver_id:
        return None
    if override_reason is None and not is_admin:
        raise ClaimError("override_reason_required", f"hold on {node_key} was placed by {placer}; "
                         "pass override_reason to release it (the placer is notified)", placer=placer)
    return {"placer": placer, "placer_driver": placer_driver, "override_reason": override_reason,
            "break_glass": override_reason is None and is_admin}


# -- rule 9: one controller per run ------------------------------------------
def checkpoint_controller(db, run_id, driver_id, is_admin, actor) -> dict:
    """Who may answer the checkpoint of the run bound to a State attempt.

    Returns ``{"enforced": bool, "attempt_id", "owner_driver_id", "break_glass"}``;
    raises ``ClaimError('not_attempt_owner')`` for a non-owner driver in an
    enforced project. A run no State attempt binds, or a project that does not
    enforce, or an attempt with no owner (legacy), is not constrained. An admin
    passes with ``break_glass`` and the owner is notified.
    """
    from core.state_graph import StateGraphStore
    if not run_id:
        return {"enforced": False}
    store = StateGraphStore(db, project_read_trusted=True)
    with store.transaction() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_attempts'").fetchone():
            return {"enforced": False}
        row = conn.execute("SELECT attempt_id,project_id,node_key,owner_driver_id,owner_fence,status "
                           "FROM state_attempts WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return {"enforced": False}
        if not enforced(conn, row["project_id"]) or not row["owner_driver_id"]:
            return {"enforced": False, "attempt_id": row["attempt_id"]}
        attempt = dict(row)
    if attempt["owner_driver_id"] == driver_id:
        return {"enforced": True, "attempt_id": attempt["attempt_id"], "owner_driver_id": driver_id,
                "break_glass": False}
    if not is_admin:
        raise ClaimError("not_attempt_owner", f"checkpoints of run {run_id} are decided by the owner of attempt "
                         f"{attempt['attempt_id']} (driver {attempt['owner_driver_id']}); take_over_attempt "
                         "or a handoff changes the controller", owner_driver_id=attempt["owner_driver_id"])
    from core import driver_notices
    with store.transaction(write=True) as conn:
        driver_notices.notify(conn, store, target_driver_id=attempt["owner_driver_id"], kind="break_glass",
                              project_id=attempt["project_id"],
                              subject=f"checkpoint of run {run_id} answered by an admin",
                              body=f"{actor} answered a checkpoint of run {run_id} (attempt {attempt['attempt_id']}, "
                                   f"node {attempt['node_key']}) that you control. Admin break-glass write.",
                              refs={"attempt_id": attempt["attempt_id"], "run_id": run_id,
                                    "node_key": attempt["node_key"], "by_actor": actor, "break_glass": True},
                              actor=actor)
        store._event(conn, attempt["project_id"], attempt["node_key"], "checkpoint_break_glass", {
            "attempt_id": attempt["attempt_id"], "run_id": run_id, "actor": actor,
            "owner_driver_id": attempt["owner_driver_id"], "break_glass": True})
    return {"enforced": True, "attempt_id": attempt["attempt_id"], "owner_driver_id": attempt["owner_driver_id"],
            "break_glass": True}


def subagent_registered(conn, project_id, driver_id, label) -> bool:
    """Whether ``<driver_id>/<label>`` is a registered subagent of this project."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='driver_subagents'").fetchone():
        return False
    return conn.execute("SELECT 1 FROM driver_subagents WHERE project_id=? AND subagent_id=? "
                        "AND status IN ('active','adopted')", (project_id, label)).fetchone() is not None


def require_registered_subagent(conn, project_id, driver_id, director_identity, what):
    """Q8: in an enforced project a subagent that produces evidence or writes a
    checkout must be registered. ``director_identity`` of the form
    ``<driver>/<label>`` names one; a bare driver id names none."""
    if not driver_id or not director_identity or director_identity == driver_id:
        return
    if not director_identity.startswith(driver_id + "/"):
        return
    if not enforced(conn, project_id):
        return
    if not subagent_registered(conn, project_id, driver_id, director_identity):
        raise ClaimError("subagent_unregistered", f"{what} by subagent {director_identity} needs "
                         "register_subagent first in an enforced project (design §4.6, Q8)",
                         subagent_id=director_identity)
