"""Node claims, leases and heartbeats for multi-driver State projects (P1).

Design: design/multi-driver-coop.md §4.1-4.3a and §4.5, phase P1 ("leases,
alert only"). A claim is a driver's recorded declaration "I am working on this
node" with a purpose; a lease says how long that declaration stays believable
without a heartbeat; a fence is a per-node monotonic integer that changes with
every new holder, so a writer presenting an old fence is refused (stale_fence).

What P1 does NOT do, on purpose:

* enforce anything: dispatch, observation and structural writes behave exactly
  as before whether or not a claim exists (enforcement is P3);
* terminate, cancel or abandon anything when a lease runs out. An expired lease
  only produces a ``lease_expired`` event and a derived ``lease_state``; an
  attempt whose lease lapsed stays ACTIVE (State never infers that a worker
  stopped). Only a CLAIM - a declaration, not a worker - leaves ``live`` once
  its grace has passed, which is what makes the node claimable again;
* record anything on a heartbeat except the two lease columns: no event, no
  observation_version bump, no history row. A heartbeat is not a state change.

Everything is opt-in per project through ``state_project_policy.multi_driver``
(default ``off``): with it off no claim can be taken and new attempts carry no
lease, so nothing here changes what an existing project sees.

Lease expiry is detected lazily: ``sweep`` runs on the private claim reads
(list_claims, get_claim), inside claim_node, and on every iteration of
wait_for_state_change, and emits each expiry event once. Public reads never
write; they derive lease_state from the clock.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from core.state_graph import (
    StateConflict,
    StateGraphError,
    StateNotFound,
    digest,
    key,
    text,
)
from core.state_privacy import UntrustedDatabase, writer_only_read

PURPOSES = ("implement", "review", "investigate", "plan")
EXCLUSIVE_PURPOSES = ("implement", "plan")
CLAIM_STATUSES = ("live", "released", "expired", "transferred", "revoked")
DEFAULT_LEASE_SECONDS = 7200          # D3: two hours, renewable without limit
MIN_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 86400
GRACE_SECONDS = 900                   # Q7: fifteen minutes after expiry
MAX_HEARTBEAT_ITEMS = 100
LEASE_EVENTS = ("claim_acquired", "claim_released", "lease_expired")
# What a PUBLIC read (overview, get_node, run_summary) shows of a claim, for a
# trusted and an anonymous reader alike: who holds which node, why and until
# when. The declared workspace and request key stay in the private reads
# (list_claims, get_claim); core.state_privacy projects exactly these columns.
PUBLIC_CLAIM_COLUMNS = ("claim_id", "project_id", "node_key", "driver_id", "subagent", "purpose", "status",
                        "fence", "node_revision", "lease_seconds", "lease_expires_at", "last_heartbeat_at",
                        "created_at")
_ACTIVE_ATTEMPT = ("reserved", "launching", "running", "paused", "unknown")

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_node_claims (
    claim_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL, node_key TEXT NOT NULL,
    driver_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('implement','review','investigate','plan')),
    status TEXT NOT NULL CHECK(status IN ('live','released','expired','transferred','revoked')),
    fence INTEGER NOT NULL CHECK(fence >= 1),
    node_revision INTEGER NOT NULL,
    attempt_id TEXT,
    workspace TEXT NOT NULL DEFAULT '',
    subagent TEXT,
    lease_seconds INTEGER NOT NULL CHECK(lease_seconds BETWEEN 60 AND 86400),
    lease_expires_at TEXT NOT NULL, last_heartbeat_at TEXT NOT NULL,
    request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project_id, node_key, driver_id, request_key),
    FOREIGN KEY(project_id, node_key) REFERENCES state_nodes(project_id, node_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS state_node_claims_one_exclusive
ON state_node_claims(project_id, node_key) WHERE status='live' AND purpose IN ('implement','plan');
CREATE INDEX IF NOT EXISTS state_node_claims_live
ON state_node_claims(project_id, status, lease_expires_at);
CREATE TRIGGER IF NOT EXISTS state_node_claims_no_delete BEFORE DELETE ON state_node_claims
BEGIN SELECT RAISE(ABORT,'claims are never deleted; release them'); END;
CREATE TABLE IF NOT EXISTS state_claim_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL, project_id TEXT NOT NULL, node_key TEXT NOT NULL,
    driver_id TEXT NOT NULL, purpose TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('live','released','expired','transferred','revoked')),
    fence INTEGER NOT NULL, lease_expires_at TEXT NOT NULL,
    actor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    FOREIGN KEY(claim_id) REFERENCES state_node_claims(claim_id)
);
CREATE INDEX IF NOT EXISTS state_claim_history_claim ON state_claim_history(claim_id, seq);
CREATE TRIGGER IF NOT EXISTS state_claim_history_no_update BEFORE UPDATE ON state_claim_history
BEGIN SELECT RAISE(ABORT,'claim history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_claim_history_no_delete BEFORE DELETE ON state_claim_history
BEGIN SELECT RAISE(ABORT,'claim history is append-only'); END;
"""
# driver_id has no FOREIGN KEY to `drivers` on purpose: that registry exists only
# while driver identity is enabled, and a claim's driver_id is taken from the
# transport, which already resolved it from an ACTIVE driver token.


def initialize(db) -> None:
    """Create the claim tables on a real handle; idempotent, never touches rows."""
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


# -- time --------------------------------------------------------------------
# ONE clock for every lease decision, replaceable in tests. Timestamps are
# written in a fixed-width UTC form so that string order IS time order, which
# lets SQL compare them directly.
def _clock() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def now_stamp() -> str:
    return stamp(_clock())


def add_seconds(value: str, seconds: int) -> str:
    return stamp(datetime.fromisoformat(value) + timedelta(seconds=seconds))


def lease_state(lease_expires_at: str | None, current: str | None = None) -> str:
    """healthy | expired | reclaimable | legacy_unleased (no lease ever recorded)."""
    if lease_expires_at is None:
        return "legacy_unleased"
    current = current or now_stamp()
    if current < lease_expires_at:
        return "healthy"
    if current < add_seconds(lease_expires_at, GRACE_SECONDS):
        return "expired"
    return "reclaimable"


def attempt_lease_view(row, current: str | None = None) -> dict:
    """Lease fields of one attempt row; None for an attempt that is not active."""
    data = dict(row)
    if data.get("status") not in _ACTIVE_ATTEMPT:
        return {"owner_driver_id": data.get("owner_driver_id"), "lease_expires_at": data.get("lease_expires_at"),
                "lease_state": None}
    expires = data.get("lease_expires_at")
    return {"owner_driver_id": data.get("owner_driver_id"), "owner_fence": data.get("owner_fence", 0),
            "lease_expires_at": expires, "lease_state": lease_state(expires, current),
            "reclaimable_at": add_seconds(expires, GRACE_SECONDS) if expires else None}


def multi_driver_on(conn, project_id) -> bool:
    row = conn.execute("SELECT * FROM state_project_policy WHERE project_id=?", (project_id,)).fetchone()
    return bool(row) and dict(row).get("multi_driver") == "on"


class ClaimError(StateConflict):
    """A refused claim/lease request. The message starts with a stable code."""

    def __init__(self, code: str, detail: str, **facts):
        self.code, self.facts = code, facts
        super().__init__(f"{code}: {detail}")


def _claim_view(row, current: str) -> dict:
    data = dict(row)
    data.pop("request_hash", None)
    live = data["status"] == "live"
    data["lease_state"] = lease_state(data["lease_expires_at"], current) if live else None
    data["reclaimable_at"] = add_seconds(data["lease_expires_at"], GRACE_SECONDS)
    return data


def live_claims(conn, project_id, node_key=None, current: str | None = None) -> list[dict]:
    """Live claims of a project (or one node), oldest first, with lease_state.

    Public columns only, so the answer is identical for every reader of an
    opened project. A claim past its grace that no sweep has retired yet is
    still listed, as reclaimable: that is what it is.
    """
    current = current or now_stamp()
    sql, args = ("SELECT " + ",".join(PUBLIC_CLAIM_COLUMNS) +
                 " FROM state_node_claims WHERE project_id=? AND status='live'", [project_id])
    if node_key is not None:
        sql += " AND node_key=?"
        args.append(node_key)
    return [_claim_view(r, current) for r in conn.execute(sql + " ORDER BY created_at, claim_id", args)]


class StateClaims:
    """The ONE writer of ``state_node_claims`` / ``state_claim_history`` and of the
    attempt lease columns after an attempt is created."""

    def __init__(self, store, actor: str, driver_id: str | None = None, is_admin: bool = False):
        self.store = store
        if not isinstance(store.db, UntrustedDatabase):
            initialize(store.db)
        self.actor = text(actor, "authenticated actor", 320)
        self.driver_id = driver_id
        self.is_admin = is_admin is True
        self.project_read_trusted = store.project_read_trusted

    # -- helpers ---------------------------------------------------------
    def _require_driver(self) -> str:
        if not self.driver_id:
            raise ClaimError("driver_identity_required",
                             "claims and leases belong to a registered driver; call driver_whoami")
        return self.driver_id

    @staticmethod
    def _claim(conn, project_id, claim_id):
        row = conn.execute("SELECT * FROM state_node_claims WHERE claim_id=? AND project_id=?",
                           (key(claim_id, "claim_id"), project_id)).fetchone()
        if row is None:
            raise StateNotFound(f"claim {claim_id!r} not found in project {project_id!r}")
        return dict(row)

    def _history(self, conn, claim, status, reason=""):
        conn.execute("INSERT INTO state_claim_history(claim_id,project_id,node_key,driver_id,purpose,status,"
                     "fence,lease_expires_at,actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (claim["claim_id"], claim["project_id"], claim["node_key"], claim["driver_id"],
                      claim["purpose"], status, claim["fence"], claim["lease_expires_at"],
                      self.actor, reason, now_stamp()))

    # -- lazy expiry -------------------------------------------------------
    @staticmethod
    def _expiry_seen(conn, project_id, node_key, lease_id, expires, phase) -> bool:
        return conn.execute(
            "SELECT 1 FROM state_events WHERE project_id=? AND node_key=? AND event_type='lease_expired' "
            "AND json_extract(payload_json,'$.lease_id')=? AND json_extract(payload_json,'$.lease_expires_at')=? "
            "AND json_extract(payload_json,'$.phase')=? LIMIT 1",
            (project_id, node_key, lease_id, expires, phase)).fetchone() is not None

    def _due(self, conn, project_id, current, node_key=None):
        """Leases past expiry whose current phase has not produced its event yet."""
        node_sql, node_args = (" AND node_key=?", [node_key]) if node_key is not None else ("", [])
        due = []
        for row in conn.execute("SELECT * FROM state_node_claims WHERE project_id=? AND status='live' "
                                "AND lease_expires_at<=?" + node_sql, [project_id, current, *node_args]):
            phase = lease_state(row["lease_expires_at"], current)
            if phase == "reclaimable" or not self._expiry_seen(conn, project_id, row["node_key"],
                                                                row["claim_id"], row["lease_expires_at"], phase):
                due.append(("claim", dict(row), phase))
        for row in conn.execute("SELECT * FROM state_attempts WHERE project_id=? AND lease_expires_at IS NOT NULL "
                                "AND lease_expires_at<=? AND status IN (" + ",".join("?" * len(_ACTIVE_ATTEMPT)) + ")"
                                + node_sql, [project_id, current, *_ACTIVE_ATTEMPT, *node_args]):
            phase = lease_state(row["lease_expires_at"], current)
            if not self._expiry_seen(conn, project_id, row["node_key"], row["attempt_id"],
                                     row["lease_expires_at"], phase):
                due.append(("attempt", dict(row), phase))
        return due

    def _sweep_conn(self, conn, project_id, current, node_key=None) -> list[dict]:
        emitted = []
        for subject, row, phase in self._due(conn, project_id, current, node_key):
            lease_id = row["claim_id"] if subject == "claim" else row["attempt_id"]
            payload = {"subject": subject, "lease_id": lease_id, "phase": phase,
                       "lease_expires_at": row["lease_expires_at"],
                       "reclaimable_at": add_seconds(row["lease_expires_at"], GRACE_SECONDS)}
            if subject == "claim":
                payload.update(claim_id=lease_id, driver_id=row["driver_id"], purpose=row["purpose"],
                               fence=row["fence"])
                if phase == "reclaimable":
                    # The DECLARATION lapses; no worker is touched. Only this
                    # frees an exclusive slot for another driver.
                    conn.execute("UPDATE state_node_claims SET status='expired',updated_at=? WHERE claim_id=?",
                                 (current, lease_id))
                    self._history(conn, row, "expired", "lease expired and its grace passed")
            else:
                payload.update(attempt_id=lease_id, driver_id=row["owner_driver_id"], fence=row["owner_fence"],
                               attempt_status=row["status"])
            self.store._event(conn, project_id, row["node_key"], "lease_expired", payload)
            emitted.append(payload)
        return emitted

    def sweep(self, project_id) -> list[dict]:
        """Emit each due lease_expired event once; a cheap read decides first.

        Never changes an attempt and never cancels anything. An untrusted reader
        cannot write, so it only ever sees the derived lease_state.
        """
        if not self.store.project_read_trusted:
            return []
        current = now_stamp()
        with self.store.transaction() as conn:
            if not self._due(conn, project_id, current):
                return []
        with self.store.transaction(write=True) as conn:
            return self._sweep_conn(conn, project_id, now_stamp())

    # -- writes ------------------------------------------------------------
    def claim_node(self, project_id, node_key, purpose, expected_revision, request_key,
                   lease_seconds=DEFAULT_LEASE_SECONDS, workspace="", subagent=None):
        driver = self._require_driver()
        if purpose not in PURPOSES:
            raise StateGraphError("purpose must be implement, review, investigate or plan")
        if type(lease_seconds) is not int or not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
            raise StateGraphError(f"lease_seconds must be an integer in [{MIN_LEASE_SECONDS}, {MAX_LEASE_SECONDS}]")
        key(request_key, "request_key")
        if not isinstance(workspace, str) or len(workspace) > 500:
            raise StateGraphError("workspace must be text of at most 500 characters")
        if subagent is not None and not (isinstance(subagent, str) and "/" in subagent
                                         and len(subagent) > 2 and len(subagent) <= 320):
            raise ClaimError("not_your_subagent", f"subagent must be '{driver}/<label>' (or an inherited "
                             "'<origin>/<label>' the registry says you own)")
        own_prefix = subagent is None or (subagent.startswith(driver + "/") and len(subagent) > len(driver) + 1)
        request_hash = digest({"purpose": purpose, "revision": expected_revision, "lease_seconds": lease_seconds,
                               "workspace": workspace, "subagent": subagent})
        with self.store.transaction(write=True) as conn:
            node = self.store._node(conn, project_id, node_key)
            old = conn.execute("SELECT * FROM state_node_claims WHERE project_id=? AND node_key=? "
                               "AND driver_id=? AND request_key=?",
                               (project_id, node_key, driver, request_key)).fetchone()
            if old is not None:
                if old["request_hash"] != request_hash:
                    raise ClaimError("request_key_reused", "this request_key was used for a different claim")
                return {**_claim_view(old, now_stamp()), "idempotent": True}
            if not multi_driver_on(conn, project_id):
                raise ClaimError("multi_driver_off",
                                 "claims are recorded only in projects whose policy has multi_driver=on")
            if node["revision"] != expected_revision:
                raise ClaimError("revision_changed", f"node revision is {node['revision']}; reload")
            if node["status"] == "SUPERSEDED" or (purpose == "implement" and node["status"] == "VERIFIED"):
                raise ClaimError("node_closed", f"node is {node['status']}")
            if not own_prefix:
                # An INHERITED worker (codex/w1 taken over by grok): the registry's
                # current ownership, judged in this transaction, makes it yours.
                owned = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='driver_subagents'"
                                     ).fetchone() and conn.execute(
                    "SELECT 1 FROM driver_subagents WHERE project_id=? AND subagent_id=? AND owner_driver_id=? "
                    "AND status IN ('active','adopted')", (project_id, subagent, driver)).fetchone()
                if not owned:
                    raise ClaimError("not_your_subagent", f"subagent must be '{driver}/<label>', or a registered "
                                     f"worker you currently own; {subagent} is neither")
            current = now_stamp()
            self._sweep_conn(conn, project_id, current, node_key)
            if purpose in EXCLUSIVE_PURPOSES:
                self._refuse_exclusive_holder(conn, project_id, node_key, driver, current)
            from core.state_enforcement import enforced, refuse_checkout_in_use, require_registered_subagent
            if enforced(conn, project_id):
                # Q8 first: a claim held FOR a subagent names one registered to you ...
                require_registered_subagent(conn, project_id, driver, subagent, "a claim")
                # ... then §7.3 rule 8: one writer per CHECKOUT (host:path), across
                # live exclusive claims, active executors and open or orphaned
                # subagents, project-wide - where that worker's own registration
                # is the same executor, not a second writer.
                if purpose in EXCLUSIVE_PURPOSES and workspace:
                    refuse_checkout_in_use(conn, project_id, workspace, same_executor=subagent,
                                           reclaiming=(driver, node_key))
            fence = 1 + conn.execute("SELECT COALESCE(MAX(fence),0) FROM state_node_claims "
                                     "WHERE project_id=? AND node_key=?", (project_id, node_key)).fetchone()[0]
            claim = {"claim_id": "claim-" + uuid.uuid4().hex, "project_id": project_id, "node_key": node_key,
                     "driver_id": driver, "purpose": purpose, "status": "live", "fence": fence,
                     "node_revision": node["revision"], "attempt_id": None, "workspace": workspace,
                     "subagent": subagent, "lease_seconds": lease_seconds,
                     "lease_expires_at": add_seconds(current, lease_seconds), "last_heartbeat_at": current,
                     "request_key": request_key, "request_hash": request_hash,
                     "created_at": current, "updated_at": current}
            try:
                conn.execute("INSERT INTO state_node_claims(" + ",".join(claim) + ") VALUES("
                             + ",".join("?" for _ in claim) + ")", tuple(claim.values()))
            except sqlite3.IntegrityError:
                # The partial unique index is the authority; the check above only
                # names the holder. A writer that bypassed it still loses here.
                self._refuse_exclusive_holder(conn, project_id, node_key, driver, current)
                raise ClaimError("claimed_by_other", "another exclusive claim won this node; reload") from None
            self._history(conn, claim, "live")
            self.store._event(conn, project_id, node_key, "claim_acquired", {
                "claim_id": claim["claim_id"], "driver_id": driver, "purpose": purpose, "fence": fence,
                "lease_expires_at": claim["lease_expires_at"], "workspace": workspace, "subagent": subagent,
                "actor": self.actor})
            return {**_claim_view(claim, current), "idempotent": False}

    @staticmethod
    def _refuse_exclusive_holder(conn, project_id, node_key, driver, current):
        holder = conn.execute("SELECT * FROM state_node_claims WHERE project_id=? AND node_key=? AND status='live' "
                              "AND purpose IN ('implement','plan')", (project_id, node_key)).fetchone()
        if holder is None:
            return
        facts = {"holder": holder["driver_id"], "claim_id": holder["claim_id"], "purpose": holder["purpose"],
                 "lease_expires_at": holder["lease_expires_at"],
                 "lease_state": lease_state(holder["lease_expires_at"], current)}
        if holder["driver_id"] == driver:
            raise ClaimError("already_held", f"you already hold {holder['purpose']} claim {holder['claim_id']} "
                             f"(fence {holder['fence']})", **facts)
        raise ClaimError("claimed_by_other", f"node is claimed for {holder['purpose']} by driver "
                         f"{holder['driver_id']} until {holder['lease_expires_at']} "
                         f"(lease_state {facts['lease_state']})", **facts)

    def release_claim(self, project_id, claim_id, fence, reason):
        reason = text(reason, "release reason", 2000)
        if not self.is_admin:
            self._require_driver()
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            claim = self._claim(conn, project_id, claim_id)
            own = claim["driver_id"] == self.driver_id
            if not own and not self.is_admin:
                raise ClaimError("not_claim_owner", f"claim belongs to driver {claim['driver_id']}")
            if type(fence) is not int or fence != claim["fence"]:
                raise ClaimError("stale_fence", f"claim fence is {claim['fence']}, not {fence}; reload")
            if claim["status"] == "released":
                return {**_claim_view(claim, now_stamp()), "idempotent": True}
            if claim["status"] != "live":
                raise ClaimError("claim_not_live", f"claim is {claim['status']}")
            current = now_stamp()
            conn.execute("UPDATE state_node_claims SET status='released',updated_at=? WHERE claim_id=?",
                         (current, claim_id))
            claim.update(status="released", updated_at=current)
            self._history(conn, claim, "released", reason)
            self.store._event(conn, project_id, claim["node_key"], "claim_released", {
                "claim_id": claim_id, "driver_id": claim["driver_id"], "purpose": claim["purpose"],
                "fence": claim["fence"], "reason": reason, "actor": self.actor, "break_glass": not own})
            return {**_claim_view(claim, current), "idempotent": False}

    def heartbeat(self, project_id, claims=None, attempts=None, subagents=None):
        """Extend leases the caller owns. Writes ONLY the two lease columns.

        Each item succeeds or is refused on its own (a parent renewing its
        subagents must not lose 99 renewals to one stale entry); refusals carry
        a stable code. A subagent item renews every live claim the caller holds
        for that ``<driver_id>/<label>``.
        """
        driver = self._require_driver()
        claims, attempts, subagents = claims or [], attempts or [], subagents or []
        total = len(claims) + len(attempts) + len(subagents)
        if not 1 <= total <= MAX_HEARTBEAT_ITEMS:
            raise StateGraphError(f"heartbeat takes 1 to {MAX_HEARTBEAT_ITEMS} items")
        renewed, refused = [], []
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            current = now_stamp()

            def renew_claim(row):
                expires = add_seconds(current, row["lease_seconds"])
                conn.execute("UPDATE state_node_claims SET lease_expires_at=?,last_heartbeat_at=? WHERE claim_id=?",
                             (expires, current, row["claim_id"]))
                renewed.append({"kind": "claim", "id": row["claim_id"], "fence": row["fence"],
                                "lease_expires_at": expires})

            for item in claims:
                row = conn.execute("SELECT * FROM state_node_claims WHERE claim_id=? AND project_id=?",
                                   (item["claim_id"], project_id)).fetchone()
                code = ("not_found" if row is None else "not_claim_owner" if row["driver_id"] != driver
                        else "stale_fence" if row["fence"] != item["fence"]
                        else "claim_not_live" if row["status"] != "live" else None)
                if code:
                    refused.append({"kind": "claim", "id": item["claim_id"], "error": code})
                else:
                    renew_claim(row)
            for item in attempts:
                row = conn.execute("SELECT * FROM state_attempts WHERE attempt_id=? AND project_id=?",
                                   (item["attempt_id"], project_id)).fetchone()
                code = ("not_found" if row is None
                        else "attempt_not_active" if row["status"] not in _ACTIVE_ATTEMPT
                        else "legacy_unleased" if row["owner_driver_id"] is None or row["lease_expires_at"] is None
                        else "not_attempt_owner" if row["owner_driver_id"] != driver
                        else "stale_fence" if row["owner_fence"] != item["fence"] else None)
                if code:
                    refused.append({"kind": "attempt", "id": item["attempt_id"], "error": code})
                    continue
                expires = add_seconds(current, DEFAULT_LEASE_SECONDS)
                conn.execute("UPDATE state_attempts SET lease_expires_at=?,last_heartbeat_at=? WHERE attempt_id=?",
                             (expires, current, item["attempt_id"]))
                renewed.append({"kind": "attempt", "id": item["attempt_id"], "fence": row["owner_fence"],
                                "lease_expires_at": expires})
            for item in subagents:
                label = item["subagent_id"]
                if "/" not in label:
                    refused.append({"kind": "subagent", "id": label, "error": "not_your_subagent"})
                    continue
                if not label.startswith(driver + "/") or len(label) <= len(driver) + 1:
                    # Not under your id: it may still be a subagent you INHERITED
                    # (take_over / handoff); the registry decides below.
                    inherited = conn.execute("SELECT owner_driver_id FROM driver_subagents WHERE subagent_id=? "
                                             "AND project_id=?", (label, project_id)).fetchone()
                    if inherited is None or inherited["owner_driver_id"] != driver:
                        refused.append({"kind": "subagent", "id": label, "error": "not_your_subagent"})
                        continue
                # The registry row (P3, §4.6) is judged FIRST: a subagent that was
                # transferred away or orphaned renews nothing for this caller,
                # not even the claims still carrying its label.
                registered = conn.execute("SELECT * FROM driver_subagents WHERE subagent_id=? AND project_id=?",
                                          (label, project_id)).fetchone()
                if registered is not None:
                    code = ("not_subagent_owner" if registered["owner_driver_id"] != driver
                            else "orphaned" if registered["status"] == "orphaned_unobservable"
                            else "subagent_closed" if registered["status"] not in ("active", "adopted") else None)
                    if code:
                        refused.append({"kind": "subagent", "id": label, "error": code})
                        continue
                rows = conn.execute("SELECT * FROM state_node_claims WHERE project_id=? AND driver_id=? "
                                    "AND subagent=? AND status='live' ORDER BY claim_id",
                                    (project_id, driver, label)).fetchall()
                for row in rows:
                    renew_claim(row)
                if registered is not None:
                    expires = add_seconds(current, DEFAULT_LEASE_SECONDS)
                    conn.execute("UPDATE driver_subagents SET lease_expires_at=?,last_heartbeat_at=? "
                                 "WHERE subagent_id=?", (expires, current, label))
                    renewed.append({"kind": "subagent", "id": label, "fence": registered["fence"],
                                    "lease_expires_at": expires})
                elif not rows:
                    refused.append({"kind": "subagent", "id": label, "error": "no_live_claims"})
        return {"renewed": renewed, "refused": refused, "heartbeat_at": current}

    # -- reads -------------------------------------------------------------
    @writer_only_read("list_claims")
    def list_claims(self, project_id, node_keys=None, statuses=None, driver_id=None, limit=100):
        if node_keys is not None:
            node_keys = [key(k, "node_key") for k in node_keys]
        statuses = statuses or ["live"]
        self.sweep(project_id)
        current = now_stamp()
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            sql = ("SELECT * FROM state_node_claims WHERE project_id=? AND status IN ("
                   + ",".join("?" * len(statuses)) + ")")
            args = [project_id, *statuses]
            if node_keys is not None:
                sql += " AND node_key IN (" + ",".join("?" * len(node_keys)) + ")"
                args.extend(node_keys)
            if driver_id is not None:
                sql += " AND driver_id=?"
                args.append(driver_id)
            rows = conn.execute(sql + " ORDER BY created_at DESC, claim_id LIMIT ?", [*args, limit + 1]).fetchall()
        return {"claims": [_claim_view(r, current) for r in rows[:limit]], "truncated": len(rows) > limit,
                "observed_at": current, "grace_seconds": GRACE_SECONDS}

    @writer_only_read("get_claim")
    def get_claim(self, project_id, claim_id):
        self.sweep(project_id)
        current = now_stamp()
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            claim = self._claim(conn, project_id, claim_id)
            history = [dict(r) for r in conn.execute(
                "SELECT * FROM state_claim_history WHERE claim_id=? ORDER BY seq", (claim_id,))]
        return {"claim": _claim_view(claim, current), "history": history, "observed_at": current}
