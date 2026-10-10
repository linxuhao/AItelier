"""Subagent registry and takeover classification (P3, design §4.6, D8, Q8, Q9).

A subagent is a worker a driver started (a tmux process, a Codex thread, a
SkillFlow run, a remote session). It has no credentials; its parent driver owns
it, renews it and answers for it. State starts, probes and stops none of them;
what it records is enough for ANOTHER driver to inherit the work when the
parent vanishes: where it runs, which checkout/branch it writes, the frozen
instructions it was given and the last checkpoint it can be continued from.

Lifecycle::

  register_subagent ──► active ──heartbeat──► active …
        │                 │
        │                 ├─ (attempt taken over / handed off) owner changes, fence+1, still `active`
        │                 │       └─ adopt_subagent(observability)
        │                 │             controllable | observable_only ──► adopted (new owner renews)
        │                 │             unobservable ──► orphaned_unobservable (nobody renews; fenced out;
        │                 │                              standing notice to the ORIGIN driver)
        │                 │                                └─ report_subagent_settled by the origin driver
        │                 │                                   (the one write an OLD fence may make) ──► terminated
        │                 └─ attempt abandoned(confirmed_stopped) ──► settled

Q8: in an enforced project (multi_driver=on AND claim_enforcement=on) a
subagent that writes a checkout or produces evidence must be registered; the
server checks the two places it can see that - a claim held for a subagent and
evidence recorded under a ``<driver>/<label>`` identity (core.state_enforcement).
Q9: the taker of an orphan continues at once from ``checkpoint_ref`` on a NEW
branch and workspace; the old branch is reference only.
"""
from __future__ import annotations

import re

from core import driver_notices
from core.state_attempts import ACTIVE
from core.state_claims import (DEFAULT_LEASE_SECONDS, ClaimError, add_seconds, digest, lease_state,
                               multi_driver_on, now_stamp)
from core.state_graph import StateGraphError, StateNotFound, key, text
from core.state_privacy import UntrustedDatabase, writer_only_read

RUNTIMES = ("local_process", "server_process", "skillflow_run", "remote_session")
OBSERVABILITIES = ("controllable", "observable_only", "unobservable")
STATUSES = ("active", "settled", "adopted", "orphaned_unobservable", "terminated")
OPEN_STATUSES = ("active", "adopted")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_SUBAGENT_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


def subagent_id_text(value) -> str:
    if not isinstance(value, str) or not _SUBAGENT_ID.match(value):
        raise StateGraphError("subagent_id must be '<driver_id>/<label>'")
    return value
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")

SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_subagents (
    subagent_id TEXT PRIMARY KEY,
    owner_driver_id TEXT NOT NULL,
    origin_driver_id TEXT NOT NULL,
    project_id TEXT NOT NULL, attempt_id TEXT NOT NULL, node_key TEXT NOT NULL,
    host TEXT NOT NULL,
    runtime TEXT NOT NULL CHECK(runtime IN ('local_process','server_process','skillflow_run','remote_session')),
    control_handle TEXT NOT NULL DEFAULT '',
    workspace TEXT NOT NULL,
    context_ref TEXT NOT NULL, context_sha256 TEXT NOT NULL,
    checkpoint_ref TEXT, checkpoint_sha256 TEXT,
    observability TEXT CHECK(observability IN ('controllable','observable_only','unobservable')),
    status TEXT NOT NULL CHECK(status IN ('active','settled','adopted','orphaned_unobservable','terminated')),
    fence INTEGER NOT NULL CHECK(fence >= 1),
    lease_expires_at TEXT NOT NULL, last_heartbeat_at TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    settled_report_ref TEXT, settled_report_sha256 TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(attempt_id) REFERENCES state_attempts(attempt_id)
);
CREATE TABLE IF NOT EXISTS state_subagent_contexts (
    context_sha256 TEXT PRIMARY KEY, context_bytes BLOB NOT NULL,
    retained_ref TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS state_subagent_contexts_no_update BEFORE UPDATE ON state_subagent_contexts
BEGIN SELECT RAISE(ABORT,'subagent contexts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS state_subagent_contexts_no_delete BEFORE DELETE ON state_subagent_contexts
BEGIN SELECT RAISE(ABORT,'subagent contexts are retained'); END;
CREATE INDEX IF NOT EXISTS driver_subagents_attempt ON driver_subagents(attempt_id, status);
CREATE INDEX IF NOT EXISTS driver_subagents_project ON driver_subagents(project_id, status, owner_driver_id);
CREATE TRIGGER IF NOT EXISTS driver_subagents_no_delete BEFORE DELETE ON driver_subagents
BEGIN SELECT RAISE(ABORT,'subagent records are settled or terminated, never deleted'); END;
"""


def initialize(db) -> None:
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _view(row, current=None) -> dict:
    data = dict(row)
    data.pop("request_hash", None)
    data["lease_state"] = (lease_state(data["lease_expires_at"], current or now_stamp())
                           if data["status"] in OPEN_STATUSES else None)
    return data


def settle_open_subagents(conn, store, attempt, actor, reason) -> list[str]:
    """A terminal, quiescent report by the owner covers the attempt's open
    workers: they are settled with it and stop reserving their checkouts
    (orphans are NOT: nobody attested anything about them)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='driver_subagents'").fetchone():
        return []
    rows = conn.execute("SELECT subagent_id,node_key FROM driver_subagents WHERE attempt_id=? "
                        "AND status IN ('active','adopted') ORDER BY subagent_id", (attempt["attempt_id"],)).fetchall()
    settled = []
    for row in rows:
        conn.execute("UPDATE driver_subagents SET status='settled',updated_at=? WHERE subagent_id=?",
                     (now_stamp(), row["subagent_id"]))
        store._event(conn, attempt["project_id"], row["node_key"], "subagent_settled", {
            "subagent_id": row["subagent_id"], "attempt_id": attempt["attempt_id"], "closing": "settled",
            "reason": reason, "actor": actor})
        settled.append(row["subagent_id"])
    return settled


def orphan_count(conn, project_id) -> int:
    """Subagents taken over but never confirmed stopped: two writers may exist."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='driver_subagents'").fetchone():
        return 0
    return conn.execute("SELECT COUNT(*) FROM driver_subagents WHERE project_id=? AND status='orphaned_unobservable'",
                        (project_id,)).fetchone()[0]


class StateSubagents:
    """The ONE writer of ``driver_subagents`` rows after a takeover/handoff moved them."""

    def __init__(self, store, actor, driver_id=None, is_admin=False, recovery=None):
        self.store, self.actor = store, text(actor, "authenticated actor", 320)
        self.driver_id, self.is_admin, self.recovery = driver_id, is_admin is True, recovery
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            initialize(store.db)

    def _require_driver(self) -> str:
        if not self.driver_id:
            raise ClaimError("driver_identity_required", "subagents belong to a registered driver")
        return self.driver_id

    @staticmethod
    def _store_context(conn, context_ref, context_sha256, context_bytes):
        """The frozen instructions handed to a subagent are PRIVATE (they are a
        driver's working context): retained in state_subagent_contexts, never in
        the public external-report blob table."""
        from core.state_graph import now
        existing = conn.execute("SELECT context_bytes FROM state_subagent_contexts WHERE context_sha256=?",
                                (context_sha256,)).fetchone()
        if existing is not None:
            if bytes(existing[0]) != context_bytes:
                raise StateGraphError("retained subagent context bytes differ for this digest")
            return
        conn.execute("INSERT INTO state_subagent_contexts(context_sha256,context_bytes,retained_ref,created_at) "
                     "VALUES(?,?,?,?)", (context_sha256, context_bytes, context_ref, now()))

    @staticmethod
    def _row(conn, project_id, subagent_id):
        row = conn.execute("SELECT * FROM driver_subagents WHERE subagent_id=? AND project_id=?",
                           (subagent_id_text(subagent_id), project_id)).fetchone()
        if row is None:
            raise StateNotFound(f"subagent {subagent_id!r} not found in project {project_id!r}")
        return dict(row)

    # -- register ----------------------------------------------------------
    def register_subagent(self, project_id, attempt_id, label, host, runtime, workspace, context_ref,
                          context_sha256, control_handle=""):
        driver = self._require_driver()
        if not isinstance(label, str) or not _LABEL.match(label):
            raise StateGraphError("label must match [A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
        if runtime not in RUNTIMES:
            raise StateGraphError("runtime must be local_process, server_process, skillflow_run or remote_session")
        host = text(host, "host", 200)
        workspace = text(workspace, "workspace", 500)
        if "#" not in workspace:
            raise StateGraphError("workspace must be host:path#branch - every subagent writes its own branch (design §4.6)")
        control_handle = control_handle if isinstance(control_handle, str) else None
        if control_handle is None or len(control_handle) > 500:
            raise StateGraphError("control_handle must be text of at most 500 characters (no credentials)")
        if not isinstance(context_sha256, str) or not _HEX64.match(context_sha256):
            raise StateGraphError("context_sha256 must be 64 lowercase hexadecimal characters")
        from core.state_report_integrity import retain_report
        context_ref, context_bytes = retain_report(context_ref, context_sha256, completed=False)
        subagent_id = f"{driver}/{label}"
        request_hash = digest({"attempt_id": attempt_id, "host": host, "runtime": runtime, "workspace": workspace,
                               "context_sha256": context_sha256, "control_handle": control_handle})
        current = now_stamp()
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            if not multi_driver_on(conn, project_id):
                raise ClaimError("multi_driver_off", "subagents are registered only in projects with multi_driver=on")
            old = conn.execute("SELECT * FROM driver_subagents WHERE subagent_id=?", (subagent_id,)).fetchone()
            if old is not None:
                if old["request_hash"] == request_hash and old["project_id"] == project_id:
                    return {**_view(old, current), "idempotent": True}
                raise ClaimError("subagent_exists", f"{subagent_id} is already registered "
                                 f"({old['status']}, attempt {old['attempt_id']}); choose another label")
            attempt = conn.execute("SELECT * FROM state_attempts WHERE attempt_id=? AND project_id=?",
                                   (key(attempt_id, "attempt_id"), project_id)).fetchone()
            if attempt is None:
                raise StateNotFound(f"attempt {attempt_id!r} not found in project {project_id!r}")
            if attempt["status"] not in ACTIVE:
                raise ClaimError("attempt_not_active", f"attempt is {attempt['status']}")
            if attempt["owner_driver_id"] is None:
                raise ClaimError("legacy_unleased", "this attempt has no owner; take_over_attempt it first")
            if attempt["owner_driver_id"] != driver:
                raise ClaimError("not_attempt_owner", f"attempt belongs to driver {attempt['owner_driver_id']}")
            # One writer per CHECKOUT, across claims and subagents, including an
            # orphan nobody has confirmed stopped (design §4.6: its checkout stays
            # reserved until the origin driver reports it settled).
            from core.state_enforcement import refuse_checkout_in_use
            refuse_checkout_in_use(conn, project_id, workspace)
            self._store_context(conn, context_ref, context_sha256, context_bytes)
            row = {"subagent_id": subagent_id, "owner_driver_id": driver, "origin_driver_id": driver,
                   "project_id": project_id, "attempt_id": attempt_id, "node_key": attempt["node_key"], "host": host,
                   "runtime": runtime, "control_handle": control_handle, "workspace": workspace,
                   "context_ref": context_ref, "context_sha256": context_sha256, "checkpoint_ref": None,
                   "checkpoint_sha256": None, "observability": None, "status": "active", "fence": 1,
                   "lease_expires_at": add_seconds(current, DEFAULT_LEASE_SECONDS), "last_heartbeat_at": current,
                   "request_hash": request_hash, "settled_report_ref": None, "settled_report_sha256": None,
                   "created_at": current, "updated_at": current}
            conn.execute("INSERT INTO driver_subagents(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")",
                         tuple(row.values()))
            self.store._event(conn, project_id, attempt["node_key"], "subagent_registered", {
                "subagent_id": subagent_id, "attempt_id": attempt_id, "owner_driver_id": driver, "host": host,
                "runtime": runtime, "workspace": workspace, "context_sha256": context_sha256, "fence": 1,
                "actor": self.actor})
            return {**_view(row, current), "idempotent": False}

    # -- checkpoint --------------------------------------------------------
    def update_subagent_checkpoint(self, project_id, subagent_id, fence, checkpoint_ref, checkpoint_sha256):
        driver = self._require_driver()
        checkpoint_ref = text(checkpoint_ref, "checkpoint_ref", 2000)
        if not isinstance(checkpoint_sha256, str) or not _HEX64.match(checkpoint_sha256):
            raise StateGraphError("checkpoint_sha256 must be 64 lowercase hexadecimal characters")
        current = now_stamp()
        with self.store.transaction(write=True) as conn:
            row = self._row(conn, project_id, subagent_id)
            if row["owner_driver_id"] != driver:
                raise ClaimError("not_subagent_owner", f"subagent belongs to driver {row['owner_driver_id']}")
            if type(fence) is not int or fence != row["fence"]:
                raise ClaimError("stale_fence", f"subagent fence is {row['fence']}, not {fence}; reload")
            if row["status"] not in OPEN_STATUSES:
                raise ClaimError("subagent_closed", f"subagent is {row['status']}")
            conn.execute("UPDATE driver_subagents SET checkpoint_ref=?,checkpoint_sha256=?,updated_at=? WHERE subagent_id=?",
                         (checkpoint_ref, checkpoint_sha256, current, subagent_id))
            self.store._event(conn, project_id, row["node_key"], "subagent_checkpoint_updated", {
                "subagent_id": subagent_id, "attempt_id": row["attempt_id"], "checkpoint_ref": checkpoint_ref,
                "checkpoint_sha256": checkpoint_sha256, "fence": fence, "actor": self.actor})
            return _view(self._row(conn, project_id, subagent_id), current)

    # -- adopt (after take_over / handoff) ----------------------------------
    def adopt_subagent(self, project_id, subagent_id, fence, observability, reason):
        driver = self._require_driver()
        reason = text(reason, "adoption reason", 4000)
        if observability not in OBSERVABILITIES:
            raise StateGraphError("observability must be controllable, observable_only or unobservable")
        current = now_stamp()
        with self.store.transaction(write=True) as conn:
            row = self._row(conn, project_id, subagent_id)
            if row["owner_driver_id"] != driver:
                raise ClaimError("not_subagent_owner", f"subagent belongs to driver {row['owner_driver_id']}; "
                                 "take_over_attempt / accept_handoff moves it first")
            attempt = conn.execute("SELECT owner_driver_id,node_key FROM state_attempts WHERE attempt_id=?",
                                   (row["attempt_id"],)).fetchone()
            if attempt is None or attempt["owner_driver_id"] != driver:
                raise ClaimError("not_attempt_owner", "adoption belongs to the attempt's current owner")
            if type(fence) is not int or fence != row["fence"]:
                raise ClaimError("stale_fence", f"subagent fence is {row['fence']}, not {fence}; reload")
            if row["status"] not in OPEN_STATUSES:
                raise ClaimError("subagent_closed", f"subagent is {row['status']}")
            new_fence = row["fence"] + 1
            notice = None
            if observability == "unobservable":
                # Not renewed, not assumed stopped: fenced out and declared.
                conn.execute("UPDATE driver_subagents SET status='orphaned_unobservable',observability=?,fence=?,"
                             "updated_at=? WHERE subagent_id=?", (observability, new_fence, current, subagent_id))
                revoked = self.recovery.revoke_subagent_claims(conn, project_id, subagent_id, current,
                                                               f"subagent {subagent_id} orphaned") if self.recovery else []
                event = "subagent_orphaned"
                if row["origin_driver_id"] != driver:
                    notice = driver_notices.notify(
                        conn, self.store, target_driver_id=row["origin_driver_id"], kind="subagent_orphaned",
                        delivery_mode="standing", project_id=project_id,
                        subject=f"your subagent {subagent_id} was taken over and cannot be observed",
                        body=(f"{self.actor} took over attempt {row['attempt_id']} and classified your subagent "
                              f"{subagent_id} (host {row['host']}, {row['runtime']}, workspace {row['workspace']}) as "
                              "unobservable. Its State writes are fenced out. When you are back: stop it, confirm "
                              f"nothing writes {row['workspace']} any more, then call report_subagent_settled"
                              f"(subagent_id={subagent_id}, quiescent=true, report). Until then the project shows an "
                              "unconfirmed old writer."),
                        refs={"subagent_id": subagent_id, "attempt_id": row["attempt_id"], "node_key": row["node_key"],
                              "workspace": row["workspace"], "fence": new_fence}, actor=self.actor)
            else:
                conn.execute("UPDATE driver_subagents SET status='adopted',observability=?,fence=?,lease_expires_at=?,"
                             "last_heartbeat_at=?,updated_at=? WHERE subagent_id=?",
                             (observability, new_fence, add_seconds(current, DEFAULT_LEASE_SECONDS), current, current,
                              subagent_id))
                event = "subagent_adopted"
            self.store._event(conn, project_id, row["node_key"], event, {
                "subagent_id": subagent_id, "attempt_id": row["attempt_id"], "observability": observability,
                "origin_driver_id": row["origin_driver_id"], "owner_driver_id": driver, "fence": new_fence,
                "reason": reason, "actor": self.actor,
                "revoked_claims": revoked if observability == "unobservable" else []})
            view = _view(self._row(conn, project_id, subagent_id), current)
        if observability == "unobservable":
            view["continue_from"] = {"checkpoint_ref": row["checkpoint_ref"], "checkpoint_sha256": row["checkpoint_sha256"],
                                     "old_workspace": row["workspace"],
                                     "rule": "continue at once on a NEW branch and workspace (Q9); the old branch is "
                                             "reference only and is not merged unreviewed"}
        view["notified"] = notice
        return view

    # -- the one old-fence write -------------------------------------------
    def report_subagent_settled(self, project_id, subagent_id, quiescent, report_ref, report_sha256, fence=None):
        driver = self._require_driver()
        if quiescent is not True:
            raise ClaimError("quiescence_required", "report_subagent_settled records that the subagent STOPPED; "
                             "quiescent must be true (there is nothing else to report here)")
        from core.state_report_integrity import retain_report, store_report_blob
        report_ref, report_bytes = retain_report(report_ref, report_sha256, completed=False)
        current = now_stamp()
        with self.store.transaction(write=True) as conn:
            row = self._row(conn, project_id, subagent_id)
            if row["status"] in ("terminated", "settled"):
                return {**_view(row, current), "idempotent": True}
            if row["status"] == "orphaned_unobservable":
                # The one old-fence write: the ORIGIN driver closes its orphan.
                if row["origin_driver_id"] != driver:
                    raise ClaimError("not_origin_driver", f"only the driver that started {subagent_id} "
                                     f"({row['origin_driver_id']}) can report it settled")
                closing = "terminated"
            else:
                # An open worker of an attempt that is no longer active (candidate,
                # failed, superseded, abandoned): its CURRENT owner, with the
                # current fence, releases the checkout it still reserves.
                attempt = conn.execute("SELECT status FROM state_attempts WHERE attempt_id=?",
                                       (row["attempt_id"],)).fetchone()
                if row["owner_driver_id"] != driver:
                    raise ClaimError("not_subagent_owner", f"subagent belongs to driver {row['owner_driver_id']}; "
                                     "only an orphan is closed by its origin driver")
                if fence is not None and fence != row["fence"]:
                    raise ClaimError("stale_fence", f"subagent fence is {row['fence']}, not {fence}; reload")
                if attempt is not None and attempt["status"] in ACTIVE:
                    raise ClaimError("not_orphaned", f"subagent is {row['status']} on an active attempt; report the "
                                     "attempt terminal (which settles its workers) or adopt/orphan it after a takeover")
                closing = "settled"
            store_report_blob(conn, report_ref, report_sha256, report_bytes)
            conn.execute("UPDATE driver_subagents SET status=?,settled_report_ref=?,settled_report_sha256=?,"
                         "updated_at=? WHERE subagent_id=?", (closing, report_ref, report_sha256, current, subagent_id))
            resolved = driver_notices.resolve(conn, project_id=project_id, kind="subagent_orphaned",
                                              ref_key="subagent_id", ref_value=subagent_id,
                                              reason="origin driver reported the orphan settled")
            self.store._event(conn, project_id, row["node_key"], "subagent_settled", {
                "subagent_id": subagent_id, "attempt_id": row["attempt_id"], "origin_driver_id": row["origin_driver_id"],
                "owner_driver_id": row["owner_driver_id"], "settled_by": driver, "closing": closing,
                "presented_fence": fence, "current_fence": row["fence"],
                "report_sha256": report_sha256, "actor": self.actor})
            notice = None
            if row["owner_driver_id"] != driver:
                notice = driver_notices.notify(
                    conn, self.store, target_driver_id=row["owner_driver_id"], kind="subagent_orphaned",
                    project_id=project_id, subject=f"orphaned subagent {subagent_id} is confirmed stopped",
                    body=f"{self.actor} confirmed that {subagent_id} (workspace {row['workspace']}) no longer runs; "
                         f"report {report_sha256}. The old branch may now be inspected and compared.",
                    refs={"subagent_id": subagent_id, "attempt_id": row["attempt_id"], "node_key": row["node_key"],
                          "report_sha256": report_sha256}, actor=self.actor)
            view = _view(self._row(conn, project_id, subagent_id), current)
        return {**view, "idempotent": False, "resolved_notices": resolved, "notified": notice}

    # -- reads -------------------------------------------------------------
    @writer_only_read("list_subagents")
    def list_subagents(self, project_id, attempt_id=None, owner_driver_id=None, statuses=None, limit=100):
        current = now_stamp()
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            sql, args = "SELECT * FROM driver_subagents WHERE project_id=?", [project_id]
            if attempt_id is not None:
                sql += " AND attempt_id=?"
                args.append(attempt_id)
            if owner_driver_id is not None:
                sql += " AND owner_driver_id=?"
                args.append(owner_driver_id)
            if statuses:
                sql += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
                args.extend(statuses)
            rows = conn.execute(sql + " ORDER BY created_at, subagent_id LIMIT ?", [*args, limit + 1]).fetchall()
        return {"subagents": [_view(r, current) for r in rows[:limit]], "truncated": len(rows) > limit,
                "observed_at": current}
