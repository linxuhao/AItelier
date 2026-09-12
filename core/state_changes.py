"""Durable State event waits; notifications are hints, SQLite is authoritative."""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager

_lock = threading.Lock()
_waiters = {}
# Routine execution progress does not require another model turn.
_QUIET = {"host_contract_pinned", "attempt_reserved", "external_attempt_registered",
          "attempt_launching", "attempt_bound"}


@contextmanager
def subscribe(db_path):
    loop, event = asyncio.get_running_loop(), asyncio.Event()
    token = (loop, event)
    with _lock:
        _waiters.setdefault(str(db_path), set()).add(token)
    try:
        yield event
    finally:
        with _lock:
            group = _waiters[str(db_path)]
            group.discard(token)
            if not group:
                del _waiters[str(db_path)]


def notify(db_path):
    with _lock:
        subscribers = list(_waiters.get(str(db_path), ()))
    for loop, event in subscribers:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # Closing caller loop; durable event remains replayable.


def scan(store, project_id, after, node_keys, attempt_ids, note_after_revision,
         filter_mode, actionable_only, limit):
    # Filter in SQL so an unbounded quiet backlog never becomes an unbounded read.
    with store.transaction() as conn:
        store._project(conn, project_id)
        high = conn.execute("SELECT COALESCE(MAX(seq),0) FROM state_events WHERE project_id=?",
                            (project_id,)).fetchone()[0]
        clauses, args = ["project_id=?", "seq>?", "seq<=?"], [project_id, after, high]
        filters, filter_args = [], []
        if node_keys is not None:
            marks = ",".join("?" for _ in node_keys)
            filters.append("(node_key IS NULL OR node_key IN (WITH RECURSIVE relevant(k) AS ("
                "SELECT node_key FROM state_nodes WHERE project_id=? AND node_key IN (" + marks + ") "
                "UNION SELECT d.dependency_key FROM state_dependencies d JOIN relevant r ON d.node_key=r.k "
                "WHERE d.project_id=?) SELECT k FROM relevant) OR EXISTS "
                "(SELECT 1 FROM json_each(payload_json,'$.invalidated') WHERE value IN (" + marks + ")))")
            filter_args.extend([project_id, *node_keys, project_id, *node_keys])
        if attempt_ids is not None:
            filters.append("json_extract(payload_json,'$.attempt_id') IN (" +
                           ",".join("?" for _ in attempt_ids) + ")")
            filter_args.extend(attempt_ids)
        if note_after_revision is not None:
            filters.append("(event_type='driver_note_updated' AND "
                           "COALESCE(json_extract(payload_json,'$.revision'),0)>?)")
            filter_args.append(note_after_revision)
        if filters:
            joiner = " OR " if filter_mode == "any" else " AND "
            clauses.append("(" + joiner.join(filters) + ")")
            args.extend(filter_args)
        if actionable_only:
            clauses.append("event_type NOT IN (" + ",".join("?" for _ in _QUIET) + ")")
            args.extend(sorted(_QUIET))
            clauses.append("(event_type NOT IN ('attempt_observed','external_attempt_observed') "
                           "OR COALESCE(json_extract(payload_json,'$.status'),'') "
                           "NOT IN ('running','reserved','launching'))")
        rows = conn.execute("SELECT * FROM state_events WHERE " + " AND ".join(clauses) +
                            " ORDER BY seq LIMIT ?", [*args, limit + 1]).fetchall()
    events = []
    for row in rows[:limit]:
        event = dict(row)
        event["payload"] = json.loads(event.pop("payload_json"))
        events.append(event)
    cursor = events[-1]["seq"] if len(rows) > limit else max(after, high)
    return events, cursor


async def wait_for_state_change(service, project_id, after=0, node_keys=None, attempt_ids=None,
                                note_after_revision=None, filter_mode="all", actionable_only=True,
                                timeout_seconds=30.0, limit=100, return_when_idle=False):
    # Service callers receive the same strict contract as REST/MCP callers.
    from core.state_commands import WaitForStateChange
    from core.state_graph import key
    args = WaitForStateChange(project_id=project_id, after=after, node_keys=node_keys,
        attempt_ids=attempt_ids, note_after_revision=note_after_revision, filter_mode=filter_mode,
        actionable_only=actionable_only, timeout_seconds=timeout_seconds,
        limit=limit, return_when_idle=return_when_idle)
    key(project_id, "project_id")
    for value in (node_keys or []) + (attempt_ids or []):
        key(value)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + args.timeout_seconds
    cursor = after
    recovery_after = 0
    # Subscribe BEFORE reading: a commit between the read and sleep sets this
    # event. Clear BEFORE the next read, never after it.
    with subscribe(service.db.db_path) as signal:
        while True:
            signal.clear()
            events, cursor = await asyncio.to_thread(
                scan, service.store, project_id, cursor, node_keys, attempt_ids,
                note_after_revision, filter_mode, actionable_only, limit)
            if events:
                return {"events": events, "next_after": cursor, "timed_out": False}
            recovery_ok = True
            if timeout_seconds > 0 and (service.sf is not None or service.runtime_factory is not None):
                # One bounded page per observation cycle; rotated to prevent
                # starvation. No executor is composed for State-only deployments.
                remaining = deadline - loop.time()
                if remaining > 0:
                    try:
                        recovery_after, recovery_ok = await asyncio.wait_for(asyncio.to_thread(
                            recover_page, service, project_id, recovery_after, node_keys, attempt_ids),
                            timeout=remaining)
                    except asyncio.TimeoutError:
                        return {"events": [], "next_after": cursor, "timed_out": True}
            if args.return_when_idle and not recovery_ok:
                # Recovery failures cannot authorize a decision from cached state.
                # Preserve actionable events committed by other rows in the page.
                events, cursor = await asyncio.to_thread(
                    scan, service.store, project_id, cursor, node_keys, attempt_ids,
                    note_after_revision, filter_mode, actionable_only, limit)
                if events:
                    return {"events": events, "next_after": cursor, "timed_out": False}
                return {"events": [], "next_after": cursor, "timed_out": False,
                        "reason": "observation_unavailable"}
            # Recovery may have committed an actionable event; read it before sleeping.
            if signal.is_set():
                continue
            # Finish a bounded recovery sweep before interpreting cached status.
            # In particular a resumed checkpoint may still be projected paused.
            if recovery_after:
                if loop.time() >= deadline:
                    return {"events": [], "next_after": cursor, "timed_out": True}
                continue
            if args.return_when_idle:
                outcome = await asyncio.to_thread(
                    wait_disposition, service.store, project_id, cursor, node_keys,
                    attempt_ids, note_after_revision, filter_mode)
                if outcome == "rescan":
                    continue
                if outcome is not None:
                    return {"events": [], "next_after": cursor, "timed_out": False, **outcome}
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {"events": [], "next_after": cursor, "timed_out": True}
            try:
                # Other processes do not share the notification registry. This
                # bounded server-side fallback also replays commits after restart.
                await asyncio.wait_for(signal.wait(), timeout=min(1.0, remaining))
            except asyncio.TimeoutError:
                pass


def wait_disposition(store, project_id, cursor, node_keys, attempt_ids,
                     note_after_revision=None, filter_mode="all"):
    """An idle decision is a snapshot, never proof of remote quiescence."""
    with store.transaction() as conn:
        # Check event watermark and attempts in one snapshot. A commit after the
        # scan must be replayed before returning an idle/action-required result.
        high = conn.execute("SELECT COALESCE(MAX(seq),0) FROM state_events WHERE project_id=?",
                            (project_id,)).fetchone()[0]
        if high > cursor:
            return "rescan"
        note_pending = note_after_revision is not None
        if note_pending:
            row = conn.execute("SELECT revision FROM state_driver_notes WHERE project_id=?",
                               (project_id,)).fetchone()
            current = row["revision"] if row else 0
            if current > note_after_revision:
                return {"reason": "driver_note_changed", "note_revision": current}
        filters, filter_args = [], []
        if node_keys is not None:
            marks = ",".join("?" for _ in node_keys)
            filters.append("node_key IN (WITH RECURSIVE relevant(k) AS ("
                "SELECT node_key FROM state_nodes WHERE project_id=? AND node_key IN (" + marks + ") "
                "UNION SELECT d.dependency_key FROM state_dependencies d JOIN relevant r ON d.node_key=r.k "
                "WHERE d.project_id=?) SELECT k FROM relevant)")
            filter_args.extend([project_id, *node_keys, project_id])
        if attempt_ids is not None:
            filters.append("attempt_id IN (" + ",".join("?" for _ in attempt_ids) + ")")
            filter_args.extend(attempt_ids)
        clauses, args = ["project_id=?"], [project_id]
        if filters:
            joiner = " OR " if filter_mode == "any" else " AND "
            clauses.append("(" + joiner.join(filters) + ")")
            args.extend(filter_args)
        rows = conn.execute("SELECT attempt_id,status FROM state_attempts WHERE " +
                            " AND ".join(clauses), args).fetchall()
        paused = [dict(row) for row in rows if row["status"] == "paused"]
        if paused:
            return {"reason": "action_required", "attempts": paused}
        # An allowlist of terminal states fails conservatively for unknown or
        # future statuses. Reservations and external registrations count as work.
        if any(row["status"] not in {"candidate", "failed", "superseded"} for row in rows):
            return None
        if note_pending:
            return None
        return {"reason": "nothing_to_wait"}


def reconcile_workflow_project(db, ws, sf, project_id):
    """Lifecycle projection only: never start, approve, or verify execution."""
    # Avoid creating State tables for legacy-only hosts.
    with db.get_connection() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='state_attempts'").fetchone():
            return []
        ids = [r[0] for r in conn.execute("SELECT attempt_id FROM state_attempts "
               "WHERE execution_project_id=? AND run_id IS NOT NULL AND execution_kind='skillflow'",
               (project_id,))]
    if not ids:
        return []
    from core.state_service import StateService
    # Reconciliation needs no workflow registry: the attempt pins its contract.
    service = StateService(db, ws, sf, registry={})
    results = []
    for aid in ids:
        try:
            results.append(service.reconcile_attempt(aid))
        except Exception:
            import logging
            logging.getLogger(__name__).exception("State workflow reconciliation failed for %s", aid)
    return results


_recovering = set()


def recover_page(service, project_id, after, node_keys, attempt_ids):
    identity = str(service.db.db_path)
    with _lock:
        if identity in _recovering:
            return after, False
        _recovering.add(identity)
    try:
        return _recover_page(service, project_id, after, node_keys, attempt_ids)
    finally:
        with _lock:
            _recovering.discard(identity)


def _recover_page(service, project_id, after, node_keys, attempt_ids):
    with service.store.transaction() as conn:
        service.store._project(conn, project_id)
        clauses = ["project_id=?", "seq>?", "run_id IS NOT NULL", "execution_kind='skillflow'",
                   "(status IN ('running','paused','unknown') OR (status='candidate' AND artifact_ref IS NULL))"]
        args = [project_id, after]
        if node_keys is not None:
            marks = ",".join("?" for _ in node_keys)
            clauses.append("node_key IN (WITH RECURSIVE relevant(k) AS ("
                "SELECT node_key FROM state_nodes WHERE project_id=? AND node_key IN (" + marks + ") "
                "UNION SELECT d.dependency_key FROM state_dependencies d JOIN relevant r ON d.node_key=r.k "
                "WHERE d.project_id=?) SELECT k FROM relevant)")
            args.extend([project_id, *node_keys, project_id])
        if attempt_ids is not None:
            clauses.append("attempt_id IN (" + ",".join("?" for _ in attempt_ids) + ")")
            args.extend(attempt_ids)
        rows = conn.execute("SELECT seq,attempt_id FROM state_attempts WHERE " +
                            " AND ".join(clauses) + " ORDER BY seq LIMIT 10", args).fetchall()
    recovered = True
    for row in rows:
        try:
            service.reconcile_attempt(row["attempt_id"])
        except Exception:
            import logging
            logging.getLogger(__name__).exception("State wait recovery failed for %s", row["attempt_id"])
            recovered = False
    return (rows[-1]["seq"] if len(rows) == 10 else 0), recovered
