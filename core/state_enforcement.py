"""Claim ENFORCEMENT for multi-driver State projects (P3, design §7.3).

P1 recorded claims and leases and enforced nothing. This module holds every
rule that turns them into refusals, and the one switch behind which those rules
live: ``state_project_enforcement.claim_enforcement`` (default ``off``). Owner
decision 2026-10-09: ``set_multi_driver(on)`` alone keeps P1's record-only
behaviour; a project enforces only when BOTH ``multi_driver=on`` and
``claim_enforcement=on``. With the switch off every function here is a no-op
that returns "nothing to enforce", so such a project behaves exactly as before:
no claim is bound to an attempt and none is released by a report unless the
caller named it explicitly.

Rules (design §7.3), each with a stable error code:

1. dispatch - ``start_attempt`` / ``start_external_attempt`` need the caller's
   live ``implement`` claim on the node (``claim_required``); a supplied
   ``claim_id``/``fence`` must name that claim (``stale_fence``). "Live" is
   judged by the lease at the moment of the write, not by whether a sweep has
   run: a claim past its grace authorizes nothing. The same check guards the
   real ``reserved -> launching`` transition (``launch_authorization``), so a
   recovery or a replayed start cannot launch another driver's reservation.
2. observe - ``report_external_attempt`` needs the attempt's owner and its
   current fence (``not_attempt_owner``, ``fence_required``, ``stale_fence``);
   implemented in core.state_external.
4. structural writes - revise/split/supersede/facet over a node (or one of its
   dependents, which the write invalidates) that another driver holds a live
   claim or active attempt on need ``override_reason``
   (``override_reason_required``); check, write, audit event and notices share
   ONE write transaction (``StructuralGuard``).
5. hold - releasing another driver's hold needs ``override_reason``.
8. one writer per checkout - a live exclusive claim or an unsettled subagent
   that already declares the same CHECKOUT (``host:path``; the ``#branch``
   suffix does not make a second writer safe) refuses another
   (``workspace_in_use``). An orphaned subagent keeps its checkout reserved
   until its origin driver reports it settled.
9. one controller per run - a SkillFlow checkpoint is answered only by the
   attempt's owner (``not_attempt_owner``).
Q13. an admin (owner e-mail, ``owner-cli``) is never refused by a claim rule;
   its write is flagged ``break_glass`` and the affected driver is notified.
"""
from __future__ import annotations

import uuid

from core.state_claims import ClaimError, add_seconds, lease_state, now_stamp
from core.state_graph import text

_ACTIVE = ("reserved", "launching", "running", "paused", "unknown")
MAX_OVERRIDE_REASON = 2000
# A checkpoint decision holds the attempt's ownership still while the engine is
# told; a crashed decider releases it by expiry.
CHECKPOINT_DECISION_SECONDS = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_checkpoint_decisions (
    decision_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL, run_id TEXT NOT NULL, project_id TEXT NOT NULL,
    driver_id TEXT, owner_fence INTEGER NOT NULL, break_glass INTEGER NOT NULL CHECK(break_glass IN (0,1)),
    started_at TEXT NOT NULL, expires_at TEXT NOT NULL, finished_at TEXT,
    FOREIGN KEY(attempt_id) REFERENCES state_attempts(attempt_id)
);
CREATE INDEX IF NOT EXISTS driver_checkpoint_decisions_open ON driver_checkpoint_decisions(attempt_id, finished_at, expires_at);
"""


def initialize(db) -> None:
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()
# Subagent rows that still (may) write their checkout.
OPEN_SUBAGENT_STATUSES = ("active", "adopted", "orphaned_unobservable")


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


# -- checkouts -----------------------------------------------------------------
def checkout_of(workspace: str) -> str:
    """``host:path#branch`` -> ``host:path``: the thing exactly one writer may own."""
    return (workspace or "").split("#", 1)[0]


def checkout_in_use(conn, project_id, workspace, *, exclude_claim=None, exclude_subagent=None,
                    same_executor=None, exclude_attempt=None, reclaiming=None):
    """The holder of this checkout (``host:path``), if any. A checkout is held by:

    * a live exclusive claim declaring it;
    * the executor of an ACTIVE attempt: the workspace of the claim the attempt
      was dispatched or transferred on, whatever that claim's status is now - a
      lapsed or transferred claim does not un-reserve a running worker's tree;
    * an open subagent (active, adopted or ORPHANED until settled);
    * any claim ever held FOR an unresolved orphan (its revoked side claims
      included) - the worker may still write every checkout it was given.

    ``same_executor`` names the subagent whose own records are not a second
    writer (its registry row and the claims held for it); ``reclaiming`` is a
    ``(driver_id, node_key)`` pair naming the ONE executor a claim explicitly
    re-associates with - the driver's own ACTIVE attempt on that very node,
    dispatched for the same executor (the claim it rode carried the same
    ``subagent``, or none) - which is not a second writer either. Common
    ownership alone is: the same driver's attempt on another node, or the same
    node's attempt dispatched for a different worker, is another executor, and
    an attempt abandoned with quiescence unknown is never exempt.
    ``exclude_claim`` / ``exclude_subagent`` / ``exclude_attempt`` skip the
    caller's own records.

    An attempt abandoned with ``abandon_kind=unknown`` keeps its executor
    checkout reserved until its owner row is SETTLED by a verified quiescence
    report: nobody attested that worker stopped.
    None when the checkout is free or undeclared."""
    checkout = checkout_of(workspace)
    if not checkout:
        return None
    claims = _has_table(conn, "state_node_claims")
    subagents = _has_table(conn, "driver_subagents")
    if claims:
        for row in conn.execute("SELECT claim_id,driver_id,node_key,workspace,subagent FROM state_node_claims "
                                "WHERE project_id=? AND status='live' AND purpose IN ('implement','plan') AND workspace!=''",
                                (project_id,)):
            if row["claim_id"] == exclude_claim or (same_executor and row["subagent"] == same_executor):
                continue
            if checkout_of(row["workspace"]) == checkout:
                return {"kind": "claim", "id": row["claim_id"], "driver_id": row["driver_id"],
                        "node_key": row["node_key"], "workspace": row["workspace"]}
        # Executor checkouts of active attempts, independent of claim status; plus
        # attempts abandoned with quiescence UNKNOWN whose owner row is not settled.
        for row in conn.execute(
                "SELECT c.claim_id,c.driver_id,c.node_key,c.workspace,c.attempt_id,c.subagent,a.owner_driver_id,a.status "
                "FROM state_node_claims c "
                "JOIN state_attempts a ON a.attempt_id=c.attempt_id WHERE c.project_id=? AND c.workspace!='' "
                "AND (a.status IN (" + ",".join("?" for _ in _ACTIVE) + ") OR (a.status='abandoned' "
                "AND a.abandon_kind='unknown' AND EXISTS (SELECT 1 FROM state_external_owners o "
                "WHERE o.attempt_id=a.attempt_id AND o.status!='settled')))", (project_id, *_ACTIVE)):
            if row["attempt_id"] == exclude_attempt or row["claim_id"] == exclude_claim:
                continue
            if (reclaiming is not None and row["status"] in _ACTIVE
                    and (row["owner_driver_id"], row["node_key"]) == tuple(reclaiming)
                    and row["subagent"] == same_executor):
                continue
            if checkout_of(row["workspace"]) == checkout:
                return {"kind": "attempt", "id": row["attempt_id"], "driver_id": row["owner_driver_id"] or row["driver_id"],
                        "node_key": row["node_key"], "workspace": row["workspace"], "claim_id": row["claim_id"]}
    if subagents:
        for row in conn.execute("SELECT subagent_id,owner_driver_id,node_key,workspace,status FROM driver_subagents "
                                "WHERE project_id=? AND status IN (" + ",".join("?" for _ in OPEN_SUBAGENT_STATUSES) + ")",
                                (project_id, *OPEN_SUBAGENT_STATUSES)):
            if row["subagent_id"] in (exclude_subagent, same_executor):
                continue
            if checkout_of(row["workspace"]) == checkout:
                return {"kind": "subagent", "id": row["subagent_id"], "driver_id": row["owner_driver_id"],
                        "node_key": row["node_key"], "workspace": row["workspace"], "status": row["status"]}
        if claims:
            # Every checkout an unresolved orphan was ever given stays reserved.
            for row in conn.execute(
                    "SELECT c.claim_id,c.node_key,c.workspace,c.subagent,s.owner_driver_id FROM state_node_claims c "
                    "JOIN driver_subagents s ON s.subagent_id=c.subagent WHERE c.project_id=? AND c.workspace!='' "
                    "AND s.status='orphaned_unobservable'", (project_id,)):
                if row["subagent"] == same_executor:
                    continue
                if checkout_of(row["workspace"]) == checkout:
                    return {"kind": "subagent", "id": row["subagent"], "driver_id": row["owner_driver_id"],
                            "node_key": row["node_key"], "workspace": row["workspace"],
                            "status": "orphaned_unobservable", "claim_id": row["claim_id"]}
    return None


def refuse_checkout_in_use(conn, project_id, workspace, **exclude):
    holder = checkout_in_use(conn, project_id, workspace, **exclude)
    if holder is not None:
        what = (f"{holder['kind']} {holder['id']} (driver {holder['driver_id']}, node {holder['node_key']}"
                + (f", {holder['status']}" if holder.get("status") else "") + ")")
        raise ClaimError("workspace_in_use", f"checkout {checkout_of(workspace)} is already written by {what}; "
                         "one writer per checkout - a different branch in the same checkout is not a different "
                         "writer", holder=holder["driver_id"], holder_kind=holder["kind"], holder_id=holder["id"],
                         node_key=holder["node_key"], workspace=holder["workspace"])


# -- rule 1: dispatch -----------------------------------------------------------
def live_implement_claim(conn, project_id, node_key, current=None):
    """The node's live implement claim judged by its LEASE now: a claim past
    expiry + grace is ``reclaimable`` and authorizes nothing, whether or not a
    sweep has retired it yet. Returns (claim_or_None, lapsed_claim_or_None)."""
    if not _has_table(conn, "state_node_claims"):
        return None, None
    row = conn.execute("SELECT * FROM state_node_claims WHERE project_id=? AND node_key=? AND status='live' "
                       "AND purpose='implement'", (project_id, node_key)).fetchone()
    if row is None:
        return None, None
    row = dict(row)
    if lease_state(row["lease_expires_at"], current or now_stamp()) == "reclaimable":
        return None, row
    return row, None


def dispatch_claim(conn, project_id, node_key, driver_id, is_admin, claim_id=None, fence=None, current=None):
    """The live implement claim a dispatch rides on.

    Returns ``(claim_row_or_None, break_glass)``. Outside an enforced project
    nothing is required and nothing is bound unless the caller NAMED a claim
    (then it is checked and bound, as asked). In an enforced project the
    caller's own live claim is required and bound; an admin passes without one
    (``break_glass``).
    """
    live, lapsed = live_implement_claim(conn, project_id, node_key, current)
    if claim_id is not None:
        if live is None or live["claim_id"] != claim_id:
            if lapsed is not None and lapsed["claim_id"] == claim_id:
                raise ClaimError("claim_required", f"claim {claim_id} lapsed (expired and past its grace); claim "
                                 "the node again before starting an attempt", claim_id=claim_id)
            raise ClaimError("stale_fence", f"claim {claim_id} is not the live implement claim of {node_key}; "
                             "reload with list_claims")
        if live["driver_id"] != driver_id:
            raise ClaimError("not_claim_owner", f"claim {claim_id} belongs to driver {live['driver_id']}")
        if fence is not None and fence != live["fence"]:
            raise ClaimError("stale_fence", f"claim fence is {live['fence']}, not {fence}; reload")
    if not enforced(conn, project_id):
        # P1 behaviour: nothing bound unless the caller named a claim.
        return (live if claim_id is not None else None), False
    if live is not None and live["driver_id"] == driver_id:
        if fence is not None and fence != live["fence"]:
            raise ClaimError("stale_fence", f"claim fence is {live['fence']}, not {fence}; reload")
        return live, False
    if is_admin:
        return None, True
    if live is None:
        if lapsed is not None and lapsed["driver_id"] == driver_id:
            raise ClaimError("claim_required", f"your implement claim {lapsed['claim_id']} on {node_key} lapsed "
                             f"(lease {lapsed['lease_expires_at']} + grace); claim the node again",
                             node_key=node_key, lapsed_claim_id=lapsed["claim_id"])
        raise ClaimError("claim_required", f"this project enforces claims: claim_node(purpose=implement) on "
                         f"{node_key} before starting an attempt", node_key=node_key)
    raise ClaimError("claimed_by_other", f"node {node_key} is claimed for implement by driver {live['driver_id']} "
                     f"until {live['lease_expires_at']}", holder=live["driver_id"], claim_id=live["claim_id"],
                     lease_expires_at=live["lease_expires_at"],
                     lease_state=lease_state(live["lease_expires_at"], current or now_stamp()))


def launch_authorization(conn, attempt, driver_id, is_admin, claim_id=None, fence=None):
    """Rule 1 at the real ``reserved -> launching`` transition.

    A recovery (`recover_attempt`) or a replayed `start_attempt` reaches the
    launch with an EXISTING reservation, so the reservation-time check alone
    would let any member launch another driver's attempt. In an enforced
    project the launcher must be the attempt's owner and hold the node's live
    implement claim. The claim the caller NAMED (``claim_id``/``fence``, carried
    into this transaction from the request) must be that live claim
    (``stale_fence`` otherwise). If the claim BOUND to the attempt is still live
    the launch rides it; if it was released or lapsed and the owner holds a NEW
    live claim, the launch binds that replacement only when the caller named it
    - a deliberate re-authorization, never a silent rebind (``claim_required``).
    Returns ``(break_glass, claim_to_bind)``; an admin passes with
    ``break_glass``. Legacy reservations without an owner are unconstrained.
    """
    if not enforced(conn, attempt["project_id"]) or not attempt.get("owner_driver_id"):
        if claim_id is not None or fence is not None:
            dispatch_claim(conn, attempt["project_id"], attempt["node_key"], driver_id, is_admin,
                           claim_id=claim_id, fence=fence)
        return False, None
    if attempt["owner_driver_id"] == driver_id:
        live, lapsed = live_implement_claim(conn, attempt["project_id"], attempt["node_key"])
        if live is None or live["driver_id"] != driver_id:
            if is_admin:
                return True, None
            raise ClaimError("claim_required", f"launching attempt {attempt['attempt_id']} needs your live implement "
                             f"claim on {attempt['node_key']}" + (" (yours lapsed)" if lapsed else ""),
                             node_key=attempt["node_key"])
        if claim_id is not None and claim_id != live["claim_id"]:
            raise ClaimError("stale_fence", f"claim {claim_id} is not your live implement claim on "
                             f"{attempt['node_key']} ({live['claim_id']} is); reload", claim_id=live["claim_id"])
        if fence is not None and fence != live["fence"]:
            raise ClaimError("stale_fence", f"claim fence is {live['fence']}, not {fence}; reload",
                             claim_id=live["claim_id"])
        bound = live["attempt_id"] == attempt["attempt_id"]
        if bound:
            return False, None
        if claim_id == live["claim_id"]:
            return False, live
        raise ClaimError("claim_required", f"the claim attempt {attempt['attempt_id']} was reserved on is gone; "
                         f"your current implement claim {live['claim_id']} (fence {live['fence']}) is not bound to "
                         "it - name it (claim_id, fence) to launch on it deliberately", claim_id=live["claim_id"],
                         fence=live["fence"])
    if is_admin:
        return True, None
    raise ClaimError("not_attempt_owner", f"attempt {attempt['attempt_id']} belongs to driver "
                     f"{attempt['owner_driver_id']}; take_over_attempt or a handoff changes the controller",
                     owner_driver_id=attempt["owner_driver_id"])


# -- membership (design §4.4, §6.2: "any project MEMBER") -------------------------
def project_membership(store, project_id, driver_id):
    """True / False from the P0 registry; None when driver identity is off (no
    registry, hence no membership to check - the legacy single-credential world)."""
    if not driver_id:
        return None
    try:
        from core.drivers import registry_for
        registry = registry_for(store.db)
    except Exception:  # noqa: BLE001 - no registry = no membership concept
        return None
    if registry is None:
        return None
    try:
        return any(m["driver_id"] == driver_id and m.get("status") == "member"
                   for m in registry.project_members(project_id))
    except Exception:  # noqa: BLE001
        return None


def require_member(store, project_id, driver_id, is_admin, what) -> bool:
    """Refuse a non-member (`not_project_member`); an admin passes and the
    caller records break_glass. Returns whether this is a break-glass pass."""
    member = project_membership(store, project_id, driver_id)
    if member is None or member:
        return False
    if is_admin:
        return True
    raise ClaimError("not_project_member", f"{what} is for members of project {project_id}; driver {driver_id} "
                     "is not one (ask an admin for set_project_driver)", project_id=project_id)


# -- rule 9b: a checkpoint decision holds ownership still ------------------------
def open_checkpoint_decision(conn, attempt, run_id, driver_id, break_glass) -> str:
    decision_id = "decision-" + uuid.uuid4().hex
    current = now_stamp()
    conn.execute("INSERT INTO driver_checkpoint_decisions(decision_id,attempt_id,run_id,project_id,driver_id,"
                 "owner_fence,break_glass,started_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 (decision_id, attempt["attempt_id"], run_id, attempt["project_id"], driver_id,
                  attempt["owner_fence"], int(break_glass), current, add_seconds(current, CHECKPOINT_DECISION_SECONDS)))
    return decision_id


def checkpoint_decision_in_flight(conn, attempt_id):
    """The unfinished, unexpired decision on this attempt, if any. Ownership
    transfers refuse while one exists (`checkpoint_in_progress`)."""
    if not _has_table(conn, "driver_checkpoint_decisions"):
        return None
    row = conn.execute("SELECT * FROM driver_checkpoint_decisions WHERE attempt_id=? AND finished_at IS NULL "
                       "AND expires_at>? ORDER BY started_at DESC LIMIT 1", (attempt_id, now_stamp())).fetchone()
    return dict(row) if row else None


def refuse_checkpoint_in_flight(conn, attempt_id):
    open_decision = checkpoint_decision_in_flight(conn, attempt_id)
    if open_decision is not None:
        raise ClaimError("checkpoint_in_progress", f"driver {open_decision['driver_id']} is answering a checkpoint of "
                         f"run {open_decision['run_id']} (until {open_decision['expires_at']}); ownership of attempt "
                         f"{attempt_id} cannot move until it is finished", decision_id=open_decision["decision_id"],
                         expires_at=open_decision["expires_at"])


def assert_decision_open(db, decision_id) -> dict:
    """The decision must still be open and unexpired at the moment the engine is
    told. A handler delayed past the decision's expiry finds ownership possibly
    moved; it is refused here (`checkpoint_decision_expired`) instead of
    resuming or rewinding a run it no longer controls. What remains is the gap
    between this read and the engine call: expected to be small, but NOT
    bounded by anything here - a suspended thread or engine lock contention can
    stretch it past the decision's remaining lifetime. Closing it would need a
    fence inside the engine's own transaction or a lock shared with ownership
    transfers; this is a stated limitation, not atomic safety."""
    from core.state_graph import StateGraphStore
    store = StateGraphStore(db, project_read_trusted=True)
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM driver_checkpoint_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if row is None:
            raise ClaimError("checkpoint_decision_expired", f"decision {decision_id} is unknown")
        row = dict(row)
        current = now_stamp()
        if row["finished_at"] is not None or row["expires_at"] <= current:
            raise ClaimError("checkpoint_decision_expired", f"checkpoint decision {decision_id} on attempt "
                             f"{row['attempt_id']} expired at {row['expires_at']} before the engine was told; "
                             "ownership may have moved - answer the checkpoint again", attempt_id=row["attempt_id"])
        owner = conn.execute("SELECT owner_driver_id,owner_fence FROM state_attempts WHERE attempt_id=?",
                             (row["attempt_id"],)).fetchone()
        if owner is None or owner["owner_fence"] != row["owner_fence"] or (
                not row["break_glass"] and owner["owner_driver_id"] != row["driver_id"]):
            raise ClaimError("checkpoint_decision_expired", f"attempt {row['attempt_id']} changed owner or fence "
                             "while the decision was open", attempt_id=row["attempt_id"])
        return row


def finish_checkpoint_decision(db, decision_id) -> bool:
    """Close the decision once the engine answered (or refused). Idempotent."""
    if not decision_id:
        return False
    from core.state_graph import StateGraphStore
    store = StateGraphStore(db, project_read_trusted=True)
    with store.transaction(write=True) as conn:
        return conn.execute("UPDATE driver_checkpoint_decisions SET finished_at=? WHERE decision_id=? "
                            "AND finished_at IS NULL", (now_stamp(), decision_id)).rowcount == 1


# -- rule 4/5: structural writes -------------------------------------------------
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
    """Rule 4 for one structural write, INSIDE the write's own transaction.

    ``check`` and ``notify`` take the connection of the transaction that
    performs the write, so the holders it saw are the holders that exist when
    the revision commits, and the audit event and notices commit with the
    revision or not at all.
    """

    def __init__(self, store, actor, driver_id, is_admin):
        self.store, self.actor, self.driver_id, self.is_admin = store, actor, driver_id, is_admin is True
        from core.state_privacy import UntrustedDatabase
        if not isinstance(store.db, UntrustedDatabase):
            initialize(store.db)

    def check(self, conn, project_id, node_key, override_reason, action) -> dict | None:
        override_reason = override_reason_text(override_reason)
        if not enforced(conn, project_id):
            return None
        held = holders(conn, project_id, dependents(conn, project_id, node_key), self.driver_id)
        if not held:
            return None
        if override_reason is None and not self.is_admin:
            names = sorted({h["driver_id"] for h in held})
            raise ClaimError("override_reason_required",
                             f"{action} on {node_key} affects work held by driver(s) {', '.join(names)}; "
                             "pass override_reason to proceed (the holders are notified)", holders=held)
        return {"action": action, "node_key": node_key, "held": held, "override_reason": override_reason,
                "break_glass": override_reason is None and self.is_admin}

    def notify(self, conn, project_id, check: dict | None) -> list[dict]:
        if not check:
            return []
        from core import driver_notices
        sent = []
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


# -- rule 9: one controller per run ----------------------------------------------
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
    initialize(db)
    with store.transaction() as conn:
        if not _has_table(conn, "state_attempts"):
            return {"enforced": False}
        row = conn.execute("SELECT attempt_id,project_id,node_key,owner_driver_id,owner_fence,status "
                           "FROM state_attempts WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return {"enforced": False}
        if not enforced(conn, row["project_id"]) or not row["owner_driver_id"]:
            return {"enforced": False, "attempt_id": row["attempt_id"]}
        attempt = dict(row)
    if attempt["owner_driver_id"] == driver_id:
        with store.transaction(write=True) as conn:
            # Re-read under the write lock: the owner must still be the owner at
            # the moment the decision opens, and the decision then holds every
            # transfer off until finish_checkpoint_decision (or expiry).
            fresh = conn.execute("SELECT attempt_id,project_id,node_key,owner_driver_id,owner_fence "
                                 "FROM state_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
            if fresh is None or fresh["owner_driver_id"] != driver_id:
                raise ClaimError("not_attempt_owner", f"attempt {attempt['attempt_id']} changed owner "
                                 f"(now {fresh['owner_driver_id'] if fresh else None}) before the decision opened",
                                 owner_driver_id=fresh["owner_driver_id"] if fresh else None)
            decision_id = open_checkpoint_decision(conn, dict(fresh), run_id, driver_id, False)
        return {"enforced": True, "attempt_id": attempt["attempt_id"], "owner_driver_id": driver_id,
                "break_glass": False, "decision_id": decision_id}
    if not is_admin:
        raise ClaimError("not_attempt_owner", f"checkpoints of run {run_id} are decided by the owner of attempt "
                         f"{attempt['attempt_id']} (driver {attempt['owner_driver_id']}); take_over_attempt "
                         "or a handoff changes the controller", owner_driver_id=attempt["owner_driver_id"])
    from core import driver_notices
    with store.transaction(write=True) as conn:
        decision_id = open_checkpoint_decision(conn, attempt, run_id, driver_id, True)
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
            "break_glass": True, "decision_id": decision_id}


# -- Q8: registered subagents ----------------------------------------------------
def subagent_registered(conn, project_id, driver_id, label, statuses=("active", "adopted")) -> bool:
    """Whether ``label`` is a subagent CURRENTLY OWNED by ``driver_id`` in this
    project with one of ``statuses``. A transferred or orphaned worker is no
    longer its origin driver's: the origin cannot claim or attest under that
    identity. Checkout writes need an OPEN worker (active/adopted); evidence
    attribution also accepts a SETTLED one - a worker that finished is exactly
    the worker whose results get attested, and settlement withdrew its right to
    write, not its history."""
    if not _has_table(conn, "driver_subagents"):
        return False
    return conn.execute("SELECT 1 FROM driver_subagents WHERE project_id=? AND subagent_id=? AND owner_driver_id=? "
                        "AND status IN (" + ",".join("?" for _ in statuses) + ")",
                        (project_id, label, driver_id, *statuses)).fetchone() is not None


EVIDENCE_SUBAGENT_STATUSES = ("active", "adopted", "settled")


def require_registered_subagent(conn, project_id, driver_id, director_identity, what, *, allow_settled=False):
    """Q8: in an enforced project a subagent that produces evidence or writes a
    checkout must be registered to the caller. ``director_identity`` of the form
    ``<driver>/<label>`` names one; a bare driver id names none."""
    if not driver_id or not director_identity or director_identity == driver_id or "/" not in director_identity:
        return
    if not enforced(conn, project_id) and director_identity.startswith(driver_id + "/"):
        return
    # Under your own prefix: your registered worker. Under ANOTHER driver's prefix
    # (an inherited worker, e.g. codex/w1 now owned by grok): the registry's
    # current ownership is the only thing that makes it yours - always checked.
    statuses = EVIDENCE_SUBAGENT_STATUSES if allow_settled else ("active", "adopted")
    if not subagent_registered(conn, project_id, driver_id, director_identity, statuses):
        raise ClaimError("subagent_unregistered", f"{what} by subagent {director_identity} needs a subagent "
                         "registered to you (register_subagent) in an enforced project (design §4.6, Q8); a "
                         "transferred or orphaned subagent is no longer yours", subagent_id=director_identity)
