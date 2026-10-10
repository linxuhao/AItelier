"""Voluntary handoff of an attempt or claim between drivers (P3, design §6).

A takeover (core.state_recovery) is for an owner that is gone. A handoff is for
an owner that is present and wants out - going offline, context nearly full,
a reviewer with the right judge model - and it moves ownership ATOMICALLY: the
attempt's owner, its fence (+1), its lease, its live bound claims (transferred,
and a fresh live claim for the receiver) and its registered subagents all change
in one transaction when the receiver accepts. Until then the offerer stays the
owner; an offer that nobody accepts within its 24-hour lease simply lapses and
transfers nothing.

The package the offerer attaches is structured and BOUNDED (16 KiB canonical),
holds references rather than content (report refs with digests, note:// and
dnote:// addresses, subagent ids, open issue ids, ≤ 2000 characters of
"where I am / what is next"), and is stored in ``state_handoffs`` - never in
the shared notebook (in-flight state does not go there, owner ruling
2026-10-06). Where the package names something checkable against State, State
checks it: the attempt's frozen context hash, its current observation version,
the project's event high-water mark, the listed subagents, and each report's
retained bytes.

§6.3: when the package says the workers are NOT quiescent, the receiver must
be able to observe them - its registered ``capabilities.observable_hosts``
must cover every host the attempt's open subagents (and the package) name;
otherwise ``accept_handoff`` is refused with ``receiver_cannot_observe_workers``
and the owner either waits for its workers to settle or lets the lease lapse so
the takeover path (with its orphan handling) applies.
"""
from __future__ import annotations

import json
import re
import uuid

from core import driver_notices
from core.state_attempts import ACTIVE, _public
from core.state_claims import ClaimError, add_seconds, digest, multi_driver_on, now_stamp
from core.state_graph import StateGraphError, StateNotFound, canonical, key, text
from core.state_privacy import UntrustedDatabase, writer_only_read

STATUSES = ("offered", "accepted", "declined", "withdrawn", "expired")
OFFER_LEASE_SECONDS = 24 * 3600
MAX_PACKAGE_BYTES = 16 * 1024
MAX_NEXT_STEP_CHARS = 2000
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_LIST_CAPS = {"reports": 20, "open_issue_ids": 100, "note_entries": 50, "private_notes": 50, "subagents": 100}
_ALLOWED_KEYS = {"context_hash", "observation_version", "event_cursor", "source", "workspace", "workers",
                 "pending_checkpoint", "next_step", *_LIST_CAPS}

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_handoffs (
    handoff_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('attempt','claim')),
    attempt_id TEXT, claim_id TEXT, node_key TEXT NOT NULL,
    from_driver_id TEXT NOT NULL, to_driver_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('offered','accepted','declined','withdrawn','expired')),
    package_json TEXT NOT NULL, expected_owner_fence INTEGER NOT NULL,
    request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    decided_by TEXT, decision_reason TEXT,
    UNIQUE(project_id, from_driver_id, request_key),
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id),
    FOREIGN KEY(attempt_id) REFERENCES state_attempts(attempt_id),
    FOREIGN KEY(claim_id) REFERENCES state_node_claims(claim_id)
);
CREATE INDEX IF NOT EXISTS state_handoffs_project ON state_handoffs(project_id, status, created_at);
CREATE TRIGGER IF NOT EXISTS state_handoffs_no_delete BEFORE DELETE ON state_handoffs
BEGIN SELECT RAISE(ABORT,'handoffs are decided or expire, never deleted'); END;
"""


def initialize(db) -> None:
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _view(row, current=None) -> dict:
    data = dict(row)
    data.pop("request_hash", None)
    data["package"] = json.loads(data.pop("package_json"))
    current = current or now_stamp()
    if data["status"] == "offered" and current >= data["lease_expires_at"]:
        data["status"] = "expired"
    return data


def _members(store, project_id) -> list[str]:
    """Member drivers of the project from the P0 registry; [] when identity is off."""
    try:
        from core.drivers import registry_for
        registry = registry_for(store.db)
    except Exception:  # noqa: BLE001 - a notification fan-out never fails the write
        registry = None
    if registry is None:
        return []
    try:
        # project_drivers.status is member|removed (core.drivers); the driver's own
        # activity is a separate attribute and is not consulted here.
        return sorted(m["driver_id"] for m in registry.project_members(project_id) if m.get("status") == "member")
    except Exception:  # noqa: BLE001
        return []


def _observable_hosts(store, driver_id) -> set[str] | None:
    """The hosts this driver DECLARED it can observe (design §4.6); None = unknown."""
    try:
        from core.drivers import registry_for
        registry = registry_for(store.db)
        if registry is None:
            return None
        caps = (registry.get(driver_id) or {}).get("capabilities") or {}
    except Exception:  # noqa: BLE001
        return None
    hosts = caps.get("observable_hosts")
    return {str(h) for h in hosts} if isinstance(hosts, list) else set()


class StateHandoffs:
    """The ONE writer of ``state_handoffs``; ownership moves through StateRecovery's helpers."""

    def __init__(self, store, actor, driver_id=None, is_admin=False, recovery=None):
        self.store, self.actor = store, text(actor, "authenticated actor", 320)
        self.driver_id, self.is_admin, self.recovery = driver_id, is_admin is True, recovery
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            initialize(store.db)

    def _require_driver(self) -> str:
        if not self.driver_id:
            raise ClaimError("driver_identity_required", "handoffs move ownership between registered drivers")
        return self.driver_id

    @staticmethod
    def _handoff(conn, project_id, handoff_id):
        row = conn.execute("SELECT * FROM state_handoffs WHERE handoff_id=? AND project_id=?",
                           (key(handoff_id, "handoff_id"), project_id)).fetchone()
        if row is None:
            raise StateNotFound(f"handoff {handoff_id!r} not found in project {project_id!r}")
        return dict(row)

    # -- package -------------------------------------------------------------
    def _check_package(self, conn, project_id, package, attempt, driver):
        if not isinstance(package, dict):
            raise StateGraphError("package must be an object")
        unknown = sorted(set(package) - _ALLOWED_KEYS)
        if unknown:
            raise StateGraphError(f"package has unknown keys {unknown}; allowed: {sorted(_ALLOWED_KEYS)}")
        encoded = canonical(package)
        if len(encoded.encode("utf-8")) > MAX_PACKAGE_BYTES:
            raise StateGraphError(f"package exceeds {MAX_PACKAGE_BYTES} bytes; it carries references, not content")
        for name, cap in _LIST_CAPS.items():
            value = package.get(name)
            if value is not None and (not isinstance(value, list) or len(value) > cap):
                raise StateGraphError(f"package.{name} must be a list of at most {cap} items")
        if "next_step" in package and (not isinstance(package["next_step"], str)
                                       or len(package["next_step"]) > MAX_NEXT_STEP_CHARS):
            raise StateGraphError(f"package.next_step must be text of at most {MAX_NEXT_STEP_CHARS} characters")
        workers = package.get("workers")
        if workers is not None and (not isinstance(workers, dict) or type(workers.get("quiescent")) is not bool):
            raise StateGraphError("package.workers must be {quiescent: bool, detail?, host?}")
        if attempt is not None and workers is None:
            # §6.3 needs an explicit attestation: an undeclared quiescence is not
            # "quiescent", it is unknown, and unknown workers cannot be handed to
            # a driver that cannot see them.
            raise ClaimError("quiescence_required", "an attempt handoff package must declare "
                             "workers={quiescent: true|false, host?, detail?}: whether your workers still run "
                             "decides who may accept")
        if attempt is not None:
            if "context_hash" in package and package["context_hash"] != digest(json.loads(attempt["context_json"])):
                raise ClaimError("package_mismatch", "package.context_hash is not this attempt's frozen context")
            if "observation_version" in package and package["observation_version"] != attempt["observation_version"]:
                raise ClaimError("package_mismatch", f"package.observation_version is not the current "
                                 f"{attempt['observation_version']}")
        if "event_cursor" in package:
            high = conn.execute("SELECT COALESCE(MAX(seq),0) FROM state_events WHERE project_id=?",
                                (project_id,)).fetchone()[0]
            if type(package["event_cursor"]) is not int or not 0 <= package["event_cursor"] <= high:
                raise ClaimError("package_mismatch", f"package.event_cursor exceeds the project's high-water mark {high}")
        for ref in package.get("reports") or []:
            if (not isinstance(ref, dict) or not isinstance(ref.get("ref"), str)
                    or not isinstance(ref.get("sha256"), str) or not _HEX64.match(ref["sha256"])):
                raise StateGraphError("package.reports items are {ref, sha256}")
            from core.state_report_integrity import retain_report
            retain_report(ref["ref"], ref["sha256"], completed=False)
        for subagent_id in package.get("subagents") or []:
            row = conn.execute("SELECT attempt_id,owner_driver_id FROM driver_subagents WHERE subagent_id=? "
                               "AND project_id=?", (subagent_id, project_id)).fetchone() if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='driver_subagents'").fetchone() else None
            if row is None or (attempt is not None and row["attempt_id"] != attempt["attempt_id"]) \
                    or row["owner_driver_id"] != driver:
                raise ClaimError("package_mismatch", f"package.subagents names {subagent_id}, which is not a "
                                 "registered subagent of yours on this attempt")
        return encoded

    # -- offer -------------------------------------------------------------
    def offer_handoff(self, project_id, request_key, expected_owner_fence, package, attempt_id=None,
                      claim_id=None, to_driver_id=None):
        driver = self._require_driver()
        key(request_key, "request_key")
        if (attempt_id is None) == (claim_id is None):
            raise StateGraphError("offer exactly one subject: attempt_id or claim_id")
        if to_driver_id is not None and (to_driver_id == driver or not isinstance(to_driver_id, str)):
            raise StateGraphError("to_driver_id names another driver, or is omitted for any member")
        if type(expected_owner_fence) is not int:
            raise StateGraphError("expected_owner_fence must be an integer")
        current = now_stamp()
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            if not multi_driver_on(conn, project_id):
                raise ClaimError("multi_driver_off", "handoffs need a project with multi_driver=on")
            attempt = claim = None
            if attempt_id is not None:
                attempt = conn.execute("SELECT * FROM state_attempts WHERE attempt_id=? AND project_id=?",
                                       (key(attempt_id, "attempt_id"), project_id)).fetchone()
                if attempt is None:
                    raise StateNotFound(f"attempt {attempt_id!r} not found in project {project_id!r}")
                attempt = dict(attempt)
                if attempt["status"] not in ACTIVE:
                    raise ClaimError("attempt_not_active", f"attempt is {attempt['status']}")
                if attempt["owner_driver_id"] != driver:
                    raise ClaimError("not_attempt_owner", f"attempt belongs to driver {attempt['owner_driver_id']!r}")
                if attempt["owner_fence"] != expected_owner_fence:
                    raise ClaimError("stale_fence", f"attempt owner fence is {attempt['owner_fence']}")
                node_key = attempt["node_key"]
            else:
                claim = conn.execute("SELECT * FROM state_node_claims WHERE claim_id=? AND project_id=?",
                                     (key(claim_id, "claim_id"), project_id)).fetchone()
                if claim is None:
                    raise StateNotFound(f"claim {claim_id!r} not found in project {project_id!r}")
                claim = dict(claim)
                if claim["status"] != "live":
                    raise ClaimError("claim_not_live", f"claim is {claim['status']}")
                if claim["driver_id"] != driver:
                    raise ClaimError("not_claim_owner", f"claim belongs to driver {claim['driver_id']}")
                if claim["fence"] != expected_owner_fence:
                    raise ClaimError("stale_fence", f"claim fence is {claim['fence']}")
                node_key = claim["node_key"]
            encoded = self._check_package(conn, project_id, package, attempt, driver)
            request_hash = digest({"attempt_id": attempt_id, "claim_id": claim_id, "to": to_driver_id,
                                   "fence": expected_owner_fence, "package": package})
            old = conn.execute("SELECT * FROM state_handoffs WHERE project_id=? AND from_driver_id=? AND request_key=?",
                               (project_id, driver, request_key)).fetchone()
            if old is not None:
                if old["request_hash"] != request_hash:
                    raise ClaimError("request_key_reused", "this request_key was used for a different offer")
                return {**_view(old, current), "idempotent": True}
            pending = conn.execute("SELECT handoff_id FROM state_handoffs WHERE project_id=? AND status='offered' "
                                   "AND lease_expires_at>? AND ((attempt_id IS NOT NULL AND attempt_id=?) OR "
                                   "(claim_id IS NOT NULL AND claim_id=?))",
                                   (project_id, current, attempt_id, claim_id)).fetchone()
            if pending:
                raise ClaimError("handoff_pending", f"handoff {pending['handoff_id']} is already offered for this "
                                 "subject; withdraw it first", handoff_id=pending["handoff_id"])
            row = {"handoff_id": "handoff-" + uuid.uuid4().hex, "project_id": project_id,
                   "subject_kind": "attempt" if attempt else "claim", "attempt_id": attempt_id, "claim_id": claim_id,
                   "node_key": node_key, "from_driver_id": driver, "to_driver_id": to_driver_id, "status": "offered",
                   "package_json": encoded, "expected_owner_fence": expected_owner_fence,
                   "request_key": request_key, "request_hash": request_hash,
                   "lease_expires_at": add_seconds(current, OFFER_LEASE_SECONDS),
                   "created_at": current, "updated_at": current, "decided_by": None, "decision_reason": None}
            conn.execute("INSERT INTO state_handoffs(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")",
                         tuple(row.values()))
            self.store._event(conn, project_id, node_key, "handoff_offered", {
                "handoff_id": row["handoff_id"], "subject_kind": row["subject_kind"], "attempt_id": attempt_id,
                "claim_id": claim_id, "from_driver_id": driver, "to_driver_id": to_driver_id,
                "lease_expires_at": row["lease_expires_at"], "actor": self.actor})
            targets = [to_driver_id] if to_driver_id else [m for m in _members(self.store, project_id) if m != driver]
            notified = []
            for target in targets:
                notified.append(driver_notices.notify(
                    conn, self.store, target_driver_id=target, kind="handoff_offer", project_id=project_id,
                    sender_driver_id=driver,
                    subject=f"{driver} offers you {row['subject_kind']} {attempt_id or claim_id} on {node_key}",
                    body=(f"accept_handoff(project_id={project_id}, handoff_id={row['handoff_id']}, "
                          f"expected_owner_fence={expected_owner_fence}) before {row['lease_expires_at']}; "
                          f"get_handoff shows the package. {package.get('next_step', '')[:500]}"),
                    refs={"handoff_id": row["handoff_id"], "attempt_id": attempt_id, "claim_id": claim_id,
                          "node_key": node_key}, actor=self.actor))
            return {**_view(row, current), "idempotent": False, "notified": notified}

    # -- accept ------------------------------------------------------------
    def _eligible(self, handoff, driver):
        if handoff["from_driver_id"] == driver:
            raise ClaimError("not_handoff_target", "the offerer does not accept or decline its own offer; withdraw it")
        if handoff["to_driver_id"] is not None and handoff["to_driver_id"] != driver:
            raise ClaimError("not_handoff_target", f"this handoff was offered to driver {handoff['to_driver_id']}")

    def _open(self, conn, handoff, current):
        if handoff["status"] == "offered" and current >= handoff["lease_expires_at"]:
            conn.execute("UPDATE state_handoffs SET status='expired',updated_at=? WHERE handoff_id=?",
                         (current, handoff["handoff_id"]))
            handoff["status"] = "expired"
        if handoff["status"] != "offered":
            raise ClaimError("handoff_closed" if handoff["status"] != "expired" else "handoff_expired",
                             f"handoff is {handoff['status']}")

    def accept_handoff(self, project_id, handoff_id, expected_owner_fence):
        driver = self._require_driver()
        current = now_stamp()
        with self.store.transaction(write=True) as conn:
            handoff = self._handoff(conn, project_id, handoff_id)
            self._eligible(handoff, driver)
            self._open(conn, handoff, current)
            package = json.loads(handoff["package_json"])
            if handoff["subject_kind"] == "attempt":
                attempt = self.recovery._attempt(conn, handoff["attempt_id"])
                if attempt["status"] not in ACTIVE:
                    raise ClaimError("attempt_not_active", f"attempt is {attempt['status']}")
                if attempt["owner_driver_id"] != handoff["from_driver_id"] or attempt["owner_fence"] != expected_owner_fence \
                        or attempt["owner_fence"] != handoff["expected_owner_fence"]:
                    raise ClaimError("stale_fence", f"attempt owner fence is {attempt['owner_fence']} (owner "
                                     f"{attempt['owner_driver_id']}); the offer no longer matches", owner_fence=attempt["owner_fence"])
                self._check_observability(conn, project_id, package, attempt, driver)
                fence = attempt["owner_fence"] + 1
                expires = add_seconds(current, 7200)
                conn.execute("UPDATE state_attempts SET owner_driver_id=?,owner_fence=?,lease_expires_at=?,"
                             "last_heartbeat_at=?,updated_at=? WHERE attempt_id=?",
                             (driver, fence, expires, current, current, attempt["attempt_id"]))
                _, claim = self.recovery._release_bound_claims(conn, attempt, current,
                                                              f"handoff {handoff_id} accepted", new_holder=driver)
                subagents = self.recovery._transfer_subagents(conn, attempt, current, expires)
                self.store._event(conn, project_id, attempt["node_key"], "attempt_ownership_transferred", {
                    "attempt_id": attempt["attempt_id"], "mode": "handoff", "handoff_id": handoff_id,
                    "from_driver_id": handoff["from_driver_id"], "to_driver_id": driver,
                    "previous_fence": attempt["owner_fence"], "fence": fence, "lease_expires_at": expires,
                    "subagents": [s["subagent_id"] for s in subagents], "actor": self.actor})
                moved = {"attempt": _public(self.recovery._attempt(conn, attempt["attempt_id"])), "claim": claim,
                         "subagents": subagents}
            else:
                claim = conn.execute("SELECT * FROM state_node_claims WHERE claim_id=?", (handoff["claim_id"],)).fetchone()
                claim = dict(claim)
                if claim["status"] != "live" or claim["driver_id"] != handoff["from_driver_id"] \
                        or claim["fence"] != expected_owner_fence or claim["fence"] != handoff["expected_owner_fence"]:
                    raise ClaimError("stale_fence", f"claim is {claim['status']} with fence {claim['fence']}; "
                                     "the offer no longer matches")
                conn.execute("UPDATE state_node_claims SET status='transferred',updated_at=? WHERE claim_id=?",
                             (current, claim["claim_id"]))
                claim.update(status="transferred", updated_at=current)
                self.recovery.claims._history(conn, claim, "transferred", f"handoff {handoff_id} accepted")
                self.store._event(conn, project_id, claim["node_key"], "claim_transferred", {
                    "claim_id": claim["claim_id"], "driver_id": claim["driver_id"], "to_driver_id": driver,
                    "purpose": claim["purpose"], "fence": claim["fence"], "handoff_id": handoff_id, "actor": self.actor})
                pseudo = {"attempt_id": claim["attempt_id"], "project_id": project_id, "node_key": claim["node_key"],
                          "node_revision": claim["node_revision"]}
                new_claim = self.recovery.new_claim_for(conn, pseudo, driver, current, f"handoff {handoff_id} accepted",
                                                        workspace=claim["workspace"], purpose=claim["purpose"],
                                                        subagent=claim["subagent"])
                moved = {"claim": new_claim}
            conn.execute("UPDATE state_handoffs SET status='accepted',decided_by=?,decision_reason='accepted',"
                         "updated_at=? WHERE handoff_id=?", (driver, current, handoff_id))
            driver_notices.resolve(conn, project_id=project_id, kind="handoff_offer", ref_key="handoff_id",
                                   ref_value=handoff_id, reason="accepted")
            notice = driver_notices.notify(
                conn, self.store, target_driver_id=handoff["from_driver_id"], kind="handoff_reply",
                project_id=project_id, sender_driver_id=driver,
                subject=f"{driver} accepted handoff {handoff_id} ({handoff['subject_kind']} on {handoff['node_key']})",
                body=f"Ownership moved to {driver}; your fence is stale. Stop renewing and stop writing that subject.",
                refs={"handoff_id": handoff_id, "attempt_id": handoff["attempt_id"], "claim_id": handoff["claim_id"],
                      "node_key": handoff["node_key"], "decision": "accepted"}, actor=self.actor)
            view = _view(self._handoff(conn, project_id, handoff_id), current)
        return {**view, **moved, "notified": notice}

    def _check_observability(self, conn, project_id, package, attempt, driver):
        """§6.3: non-quiescent workers may only be handed to a driver that can observe them."""
        workers = package.get("workers") or {}
        if workers.get("quiescent") is True:
            return
        hosts = set()
        if isinstance(workers.get("host"), str):
            hosts.add(workers["host"])
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='driver_subagents'").fetchone():
            for row in conn.execute("SELECT host FROM driver_subagents WHERE attempt_id=? AND status IN ('active','adopted')",
                                    (attempt["attempt_id"],)):
                hosts.add(row["host"])
        observable = _observable_hosts(self.store, driver)
        unseen = sorted(hosts - (observable or set()))
        if not hosts or unseen:
            raise ClaimError("receiver_cannot_observe_workers",
                             "the package says the workers are not quiescent and you have not declared that you can "
                             f"observe {unseen or 'them (no host is named)'}: register capabilities.observable_hosts, "
                             "or wait until the offerer reports them settled", hosts=sorted(hosts),
                             observable_hosts=sorted(observable or []))

    # -- decline / withdraw ------------------------------------------------
    def decline_handoff(self, project_id, handoff_id, reason):
        driver = self._require_driver()
        reason = text(reason, "decline reason", 2000)
        current = now_stamp()
        with self.store.transaction(write=True) as conn:
            handoff = self._handoff(conn, project_id, handoff_id)
            self._eligible(handoff, driver)
            self._open(conn, handoff, current)
            closes = handoff["to_driver_id"] is not None
            if closes:
                conn.execute("UPDATE state_handoffs SET status='declined',decided_by=?,decision_reason=?,updated_at=? "
                             "WHERE handoff_id=?", (driver, reason, current, handoff_id))
                driver_notices.resolve(conn, project_id=project_id, kind="handoff_offer", ref_key="handoff_id",
                                       ref_value=handoff_id, reason="declined")
            self.store._event(conn, project_id, handoff["node_key"], "handoff_declined", {
                "handoff_id": handoff_id, "by_driver_id": driver, "reason": reason, "closes_offer": closes,
                "actor": self.actor})
            notice = driver_notices.notify(
                conn, self.store, target_driver_id=handoff["from_driver_id"], kind="handoff_reply",
                project_id=project_id, sender_driver_id=driver,
                subject=f"{driver} declined handoff {handoff_id}" + ("" if closes else " (offer stays open to others)"),
                body=reason, refs={"handoff_id": handoff_id, "node_key": handoff["node_key"], "decision": "declined",
                                   "attempt_id": handoff["attempt_id"], "claim_id": handoff["claim_id"]},
                actor=self.actor)
            view = _view(self._handoff(conn, project_id, handoff_id), current)
        return {**view, "notified": notice}

    def withdraw_handoff(self, project_id, handoff_id, reason=""):
        driver = self._require_driver()
        reason = reason if isinstance(reason, str) and len(reason) <= 2000 else None
        if reason is None:
            raise StateGraphError("reason must be text of at most 2000 characters")
        current = now_stamp()
        with self.store.transaction(write=True) as conn:
            handoff = self._handoff(conn, project_id, handoff_id)
            if handoff["from_driver_id"] != driver and not self.is_admin:
                raise ClaimError("not_handoff_owner", "only the offerer withdraws an offer")
            if handoff["status"] == "withdrawn":
                return {**_view(handoff, current), "idempotent": True}
            self._open(conn, handoff, current)
            conn.execute("UPDATE state_handoffs SET status='withdrawn',decided_by=?,decision_reason=?,updated_at=? "
                         "WHERE handoff_id=?", (driver, reason, current, handoff_id))
            driver_notices.resolve(conn, project_id=project_id, kind="handoff_offer", ref_key="handoff_id",
                                   ref_value=handoff_id, reason="withdrawn")
            self.store._event(conn, project_id, handoff["node_key"], "handoff_withdrawn", {
                "handoff_id": handoff_id, "by_driver_id": driver, "reason": reason, "actor": self.actor,
                "break_glass": handoff["from_driver_id"] != driver})
            return {**_view(self._handoff(conn, project_id, handoff_id), current), "idempotent": False}

    # -- reads -------------------------------------------------------------
    @writer_only_read("list_handoffs")
    def list_handoffs(self, project_id, statuses=None, limit=100):
        current = now_stamp()
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            sql, args = "SELECT * FROM state_handoffs WHERE project_id=?", [project_id]
            rows = conn.execute(sql + " ORDER BY created_at DESC, handoff_id LIMIT ?", [*args, 1000]).fetchall()
        views = [_view(r, current) for r in rows]
        if statuses:
            views = [v for v in views if v["status"] in statuses]
        return {"handoffs": views[:limit], "truncated": len(views) > limit, "observed_at": current}

    @writer_only_read("get_handoff")
    def get_handoff(self, project_id, handoff_id):
        current = now_stamp()
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            return {"handoff": _view(self._handoff(conn, project_id, handoff_id), current), "observed_at": current}
