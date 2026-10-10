"""Claim ENFORCEMENT for multi-driver State projects (P3, design §7.3).

P1 recorded claims and leases and enforced nothing. This module holds every
rule that turns them into refusals, behind ONE switch: ``enforced(conn,
project_id)``, which reads ``claim_enforcement`` (default ``off``; it requires
``multi_driver=on`` and reads ``off`` whenever multi_driver is off,
core.state_metadata.claim_enforcement). Every rule here asks that helper and
nothing else. With the switch off every function here is a no-op that returns
"nothing to enforce", so such a project behaves exactly as P1: no claim is
bound to an attempt and none is released by a report unless the caller named
it explicitly.

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
   claim or active attempt on are refused (``not_owner``). Owner check only.
5. hold - releasing another driver's hold needs ``override_reason``.
9. one controller per run - a SkillFlow checkpoint (and the failed-run rescue
   behind the same route) is answered only by the attempt's owner
   (``not_attempt_owner``), checked once at the route's entry.
Q13. an admin (owner e-mail, ``owner-cli``) is never refused by a claim rule;
   its write carries ``break_glass`` in its audit event. That flag is the whole
   mechanism: no notice, no separate path.
"""
from __future__ import annotations

from core.state_claims import ClaimError, lease_state, now_stamp
from core.state_graph import text

_ACTIVE = ("reserved", "launching", "running", "paused", "unknown")
MAX_OVERRIDE_REASON = 2000


def _has_table(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def enforced(conn, project_id) -> bool:
    """THE enforcement switch: claim_enforcement=on (which requires multi_driver=on)."""
    from core.state_metadata import claim_enforcement
    return claim_enforcement(conn, project_id) == "on"


def override_reason_text(value):
    if value is None:
        return None
    return text(value, "override_reason", MAX_OVERRIDE_REASON)


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


def structural_owner_check(conn, project_id, node_key, driver_id, is_admin, action) -> list[dict] | None:
    """Rule 4, owner check only: a structural write over work another driver
    holds is refused (``not_owner``). Returns the holders an admin wrote
    around (the caller records ``break_glass``), None when nothing is held."""
    if not enforced(conn, project_id):
        return None
    held = holders(conn, project_id, dependents(conn, project_id, node_key), driver_id)
    if not held:
        return None
    if not is_admin:
        names = sorted({h["driver_id"] for h in held})
        raise ClaimError("not_owner", f"{action} on {node_key} affects work held by driver(s) {', '.join(names)}; "
                         "only they change it until they release or hand it off", holders=held)
    return held


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
    """Who may answer the checkpoint (or rescue the failed run) bound to a State attempt.

    ONE owner check at the entry of the route: returns ``{"enforced": bool,
    "attempt_id", "owner_driver_id", "owner_fence", "break_glass"}``; raises
    ``ClaimError('not_attempt_owner')`` for a non-owner driver in an enforced
    project. A run no State attempt binds, a project that does not enforce, or
    an attempt with no owner (legacy) is not constrained. An admin passes with
    ``break_glass`` recorded in a ``checkpoint_break_glass`` event. Ownership
    moving between this check and the engine call is an accepted, documented
    gap (checkpoint answers are rare and human-paced).
    """
    from core.state_graph import StateGraphStore
    if not run_id:
        return {"enforced": False}
    store = StateGraphStore(db, project_read_trusted=True)
    with store.transaction() as conn:
        if not _has_table(conn, "state_attempts"):
            return {"enforced": False}
        row = conn.execute("SELECT attempt_id,project_id,node_key,owner_driver_id,owner_fence "
                           "FROM state_attempts WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return {"enforced": False}
        if not enforced(conn, row["project_id"]) or not row["owner_driver_id"]:
            return {"enforced": False, "attempt_id": row["attempt_id"]}
        attempt = dict(row)
    result = {"enforced": True, "attempt_id": attempt["attempt_id"], "owner_driver_id": attempt["owner_driver_id"],
              "owner_fence": attempt["owner_fence"], "break_glass": False}
    if attempt["owner_driver_id"] == driver_id:
        return result
    if not is_admin:
        raise ClaimError("not_attempt_owner", f"checkpoints of run {run_id} are decided by the owner of attempt "
                         f"{attempt['attempt_id']} (driver {attempt['owner_driver_id']}); take_over_attempt "
                         "or a handoff changes the controller", owner_driver_id=attempt["owner_driver_id"])
    with store.transaction(write=True) as conn:
        store._event(conn, attempt["project_id"], attempt["node_key"], "checkpoint_break_glass", {
            "attempt_id": attempt["attempt_id"], "run_id": run_id, "actor": actor,
            "owner_driver_id": attempt["owner_driver_id"], "owner_fence": attempt["owner_fence"],
            "break_glass": True})
    return {**result, "break_glass": True}
