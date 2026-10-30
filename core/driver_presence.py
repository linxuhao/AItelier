"""Driver presence v2: declared offline, waits in progress, effective presence.

Presence v1 (core.drivers.presence_of) derives online/away/offline from
``drivers.last_seen_at``. This module adds two FACTS and one combination:

* a DECLARATION: a driver may say "I am offline" (reason, expected return) for
  itself - ``declare`` - and an ``online`` declaration clears it. One row per
  driver in ``driver_presence_declarations``; created lazily on first write.
* WAIT ROWS: a driver blocked in ``wait_for_driver_inbox``,
  ``wait_for_state_change`` or ``wait_for_run`` is present. ``driver_waiting``
  writes ONE row per wait in ``driver_presence_waits`` (started, until =
  start + timeout + slack, ended), so overlapping waits never undercount.

``effective_presence`` combines them, in this order:

1. ``online`` while a wait is in progress or ended at most
   ``AITELIER_PRESENCE_WAIT_GRACE_SECONDS`` (default 120) ago. This overrides a
   declaration; starting a wait also clears the declaration (audited);
2. ``offline`` while a declaration says so - even against recent
   ``last_seen_at`` stamps from ordinary reads; claim_node, heartbeat,
   start_external_attempt and report_external_attempt by that driver clear it
   INSIDE their own write transaction (``mark_back``);
3. otherwise the v1 derived presence. ``determinable`` is False when there is
   no registry row or no readable ``last_seen_at``: the takeover authority
   never reads such a driver as offline (fail closed).

FAILING CLOSED. Nothing that makes a driver present is best effort: the
declaration is cleared in the same transaction as the action that proves the
driver is back, and a wait that cannot record its row (after a bounded retry)
FAILS with the retryable ``wait_not_registered`` instead of waiting
unregistered.

WHY THE WAIT ROWS ARE IN THE DATABASE, not an in-process counter: presence
carries ONE authority (core.state_takeover: another member may take over the
claims and attempts of an OFFLINE owner), and that decision must see a wait
running in any process that shares the database - the State-only service, the
host CLI, ``docker exec`` tools or a second worker. A row is bounded by its
wait's timeout, so a crashed process keeps a driver online no longer than its
wait could have lasted plus the grace.

Authority: presence is information EXCEPT for the takeovers in
core.state_takeover. Every other caller only describes it
(tests/unit/test_driver_presence.py and test_driver_offline_takeover.py guard that).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from core import drivers

WAIT_GRACE_ENV = "AITELIER_PRESENCE_WAIT_GRACE_SECONDS"
WAIT_GRACE_DEFAULT = 120
WAIT_SLACK_SECONDS = 30          # a wait's own bookkeeping after its timeout
WAIT_REGISTER_SECONDS = 10.0     # bounded retry before a wait fails as wait_not_registered
WAIT_ROW_KEEP_SECONDS = 3600     # finished wait rows older than this (and the grace) are pruned
MAX_REASON_CHARS = 200
DECLARABLE = ("offline", "online")
# Why a declaration was cleared, as recorded in driver_audit (closed set).
CLEAR_CAUSES = ("declared", "wait", "claim_node", "heartbeat", "start_external_attempt",
                "report_external_attempt")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS driver_presence_declarations ("
    " driver_id TEXT PRIMARY KEY,"
    " status TEXT NOT NULL CHECK(status IN ('offline')),"
    " declared_at TEXT NOT NULL,"
    " reason TEXT NOT NULL DEFAULT '',"
    " expected_return TEXT)",
    "CREATE TABLE IF NOT EXISTS driver_presence_waits ("
    " wait_id TEXT PRIMARY KEY,"
    " driver_id TEXT NOT NULL,"
    " started_at TEXT NOT NULL,"
    " until TEXT NOT NULL,"
    " ended_at TEXT)",
    "CREATE INDEX IF NOT EXISTS driver_presence_waits_driver ON driver_presence_waits(driver_id, until)",
)
_log = logging.getLogger(__name__)


class WaitNotRegistered(Exception):
    """Raised by ``driver_waiting`` when the wait row cannot be written."""


def shared_identity(driver_id) -> bool:
    """The shared Cloudflare identity: every tunnel-token holder is ``public``.
    It never declares presence and never takes anything over."""
    return driver_id == drivers.PUBLIC_DRIVER_ID


# -- time ----------------------------------------------------------------------
def _clock() -> datetime:
    """ONE clock for presence decisions; replaced in tests."""
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    """Fixed-width UTC, so string order is time order."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def wait_grace_seconds() -> int:
    """AITELIER_PRESENCE_WAIT_GRACE_SECONDS; a non-positive or non-numeric value -> 120."""
    return drivers._positive_seconds(WAIT_GRACE_ENV, WAIT_GRACE_DEFAULT)


def _has(conn, table) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _ensure(conn) -> None:
    for statement in SCHEMA:
        conn.execute(statement)


# -- the combination ---------------------------------------------------------------
def _declaration(conn, driver_id):
    if not _has(conn, "driver_presence_declarations"):
        return None
    row = conn.execute("SELECT * FROM driver_presence_declarations WHERE driver_id=?", (driver_id,)).fetchone()
    return dict(row) if row is not None else None


def _wait_state(conn, driver_id, current: datetime):
    """(waiting now, end of the most recent finished wait or None)."""
    if not _has(conn, "driver_presence_waits"):
        return False, None
    waiting, ended = False, None
    for row in conn.execute("SELECT until, ended_at FROM driver_presence_waits WHERE driver_id=?", (driver_id,)):
        until, end = _parse(row[0]), _parse(row[1])
        if end is None and until is not None and current <= until:
            waiting = True
            continue
        # A wait whose process never recorded its end lasted at most until then.
        finish = end if end is not None else until
        if finish is not None and (ended is None or finish > ended):
            ended = finish
    return waiting, ended


def effective_presence(conn, driver_id, now=None) -> dict:
    """The presence of ``driver_id`` as one dict, read on ``conn``.

    Keeps the v1 fields (``status``, ``last_seen_at``, ``seconds_since_seen``)
    and adds ``basis`` (wait | declared | last_seen), ``derived_status`` (the
    v1 answer), ``determinable``, ``declared`` with ``declared_status``/
    ``reason``/``since``/``expected_return``, ``waiting`` and ``last_wait_end``.
    """
    current = now if isinstance(now, datetime) else _clock()
    seen, known = None, False
    if _has(conn, "drivers"):
        row = conn.execute("SELECT last_seen_at FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()
        if row is not None:
            seen, known = row[0], True
    derived = drivers.presence_now(seen, current)
    declaration = _declaration(conn, driver_id)
    waiting, ended = _wait_state(conn, driver_id, current)
    recent = ended is not None and (current - ended).total_seconds() <= wait_grace_seconds()
    if waiting or recent:
        status, basis = "online", "wait"
    elif declaration is not None:
        status, basis = "offline", "declared"
    else:
        status, basis = derived["status"], "last_seen"
    determinable = basis != "last_seen" or (known and derived["seconds_since_seen"] is not None)
    return {**derived, "status": status, "basis": basis, "derived_status": derived["status"],
            "determinable": determinable,
            "declared": declaration is not None,
            "declared_status": declaration["status"] if declaration else None,
            "reason": declaration["reason"] if declaration else None,
            "since": declaration["declared_at"] if declaration else None,
            "expected_return": declaration["expected_return"] if declaration else None,
            "waiting": waiting, "last_wait_end": _stamp(ended) if ended is not None else None}


def presence_for(db, driver_id, now=None) -> dict:
    with db.get_connection() as conn:
        return effective_presence(conn, driver_id, now)


def without_reason(presence) -> dict:
    """A presence for OTHER drivers' eyes (claims, refusals, takeover events):
    the declared reason is the owner's own prose and is not copied around."""
    return {k: v for k, v in presence.items() if k != "reason"}


# -- declarations ------------------------------------------------------------------
def _clear(conn, driver_id, cause, actor) -> bool:
    """Delete the declaration on the caller's connection; audit when one existed."""
    if not _has(conn, "driver_presence_declarations"):
        return False
    gone = conn.execute("DELETE FROM driver_presence_declarations WHERE driver_id=?", (driver_id,)).rowcount
    if gone:
        drivers.presence_audit(conn, driver_id, "online", cause, actor or f"driver:{driver_id}")
    return bool(gone)


def declare(db, driver_id, status, reason=None, expected_return=None, *, actor=None) -> dict:
    """Declare ``driver_id`` (the CALLER, from its credential) offline or online.

    Idempotent: repeating the same offline declaration changes nothing; a new
    reason or expected_return updates the row and keeps ``since``; ``online``
    deletes the row (a no-op when there is none). Every change writes one
    driver_audit line with enumerated facts only (never the reason text).
    The shared ``public`` identity cannot declare.
    """
    if not isinstance(driver_id, str) or not driver_id:
        raise drivers.DriverError("driver_identity_required: presence is declared by a registered driver for itself")
    if shared_identity(driver_id):
        raise drivers.DriverError("shared_identity: the shared public identity cannot declare presence")
    if status not in DECLARABLE:
        raise drivers.DriverError("status must be offline or online")
    if reason is None:
        reason = ""
    if not isinstance(reason, str) or len(reason) > MAX_REASON_CHARS:
        raise drivers.DriverError(f"reason must be text of at most {MAX_REASON_CHARS} characters")
    returning = None
    if expected_return is not None:
        if status != "offline":
            raise drivers.DriverError("expected_return belongs to an offline declaration")
        parsed = _parse(expected_return)
        if parsed is None:
            raise drivers.DriverError("expected_return must be an ISO-8601 timestamp")
        returning = _stamp(parsed)
    actor = actor or f"driver:{driver_id}"
    with db.get_connection() as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure(conn)
            old = _declaration(conn, driver_id)
            changed = False
            if status == "online":
                changed = _clear(conn, driver_id, "declared", actor)
            elif old is None:
                conn.execute("INSERT INTO driver_presence_declarations(driver_id,status,declared_at,reason,"
                             "expected_return) VALUES(?,?,?,?,?)",
                             (driver_id, "offline", _stamp(_clock()), reason, returning))
                drivers.presence_audit(conn, driver_id, "offline", "declared", actor)
                changed = True
            elif (old["reason"], old["expected_return"]) != (reason, returning):
                conn.execute("UPDATE driver_presence_declarations SET reason=?,expected_return=? WHERE driver_id=?",
                             (reason, returning, driver_id))
                drivers.presence_audit(conn, driver_id, "offline", "declared", actor)
                changed = True
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        presence = effective_presence(conn, driver_id)
    return {"driver_id": driver_id, "status": status, "changed": changed, "presence": presence}


def mark_back(conn, driver_id, cause) -> None:
    """``driver_id`` acted (claim_node, heartbeat, start/report of an external
    attempt): it is back. Runs on the CALLER'S write transaction, so the
    declaration is cleared (audited) and ``last_seen_at`` refreshed exactly
    when the action commits - never best effort, never separately."""
    if not driver_id or cause not in CLEAR_CAUSES:
        return
    _clear(conn, driver_id, cause, f"driver:{driver_id}")
    if _has(conn, "drivers"):
        conn.execute("UPDATE drivers SET last_seen_at=? WHERE driver_id=?", (drivers.now(), driver_id))


# -- waits -------------------------------------------------------------------------
def _wait_begin(db, driver_id, timeout_seconds) -> str:
    """Write this wait's row (and clear the declaration) in one transaction,
    retrying with backoff for at most WAIT_REGISTER_SECONDS; raise
    WaitNotRegistered when it never commits."""
    deadline = time.monotonic() + WAIT_REGISTER_SECONDS
    delay, last = 0.1, None
    wait_id = "wait-" + uuid.uuid4().hex
    while True:
        current = _clock()
        until = _stamp(current + timedelta(seconds=float(timeout_seconds) + WAIT_SLACK_SECONDS))
        keep = _stamp(current - timedelta(seconds=max(WAIT_ROW_KEEP_SECONDS, 2 * wait_grace_seconds())))
        try:
            with db.get_connection() as conn:
                remaining = max(0.05, deadline - time.monotonic())
                conn.execute(f"PRAGMA busy_timeout={int(min(2.0, remaining) * 1000)}")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _ensure(conn)
                    conn.execute("DELETE FROM driver_presence_waits WHERE driver_id=? AND COALESCE(ended_at,until)<?",
                                 (driver_id, keep))
                    conn.execute("INSERT INTO driver_presence_waits(wait_id,driver_id,started_at,until,ended_at) "
                                 "VALUES(?,?,?,?,NULL)", (wait_id, driver_id, _stamp(current), until))
                    _clear(conn, driver_id, "wait", f"driver:{driver_id}")
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            return wait_id
        except sqlite3.Error as exc:
            last = exc
        if time.monotonic() + delay >= deadline:
            _log.warning("could not register the wait of %s: %s", driver_id, last)
            raise WaitNotRegistered(str(last))
        time.sleep(delay)
        delay = min(delay * 2, 1.0)


def _wait_end(db, wait_id) -> None:
    try:
        with db.get_connection() as conn:
            # Short: the wait's answer is held until this returns. A miss is safe -
            # the driver stays online until the row's `until`, never longer.
            conn.execute("PRAGMA busy_timeout=500")
            conn.execute("UPDATE driver_presence_waits SET ended_at=? WHERE wait_id=?", (_stamp(_clock()), wait_id))
            conn.commit()
    except sqlite3.Error:
        _log.warning("could not record the end of wait %s", wait_id, exc_info=True)


@asynccontextmanager
async def driver_waiting(db, driver_id, timeout_seconds):
    """Mark ``driver_id`` as waiting for the duration of the block (off the event
    loop). Raises the retryable ``ClaimError('wait_not_registered')`` - before
    waiting - when the row cannot be written."""
    from core.state_privacy import UntrustedDatabase
    if not driver_id or db is None or isinstance(db, UntrustedDatabase) or not hasattr(db, "get_connection"):
        yield
        return
    try:
        seconds = float(timeout_seconds)
    except (TypeError, ValueError):
        seconds = 0.0
    seconds = min(max(seconds, 0.0), 3600.0) if seconds == seconds else 0.0   # NaN -> 0
    try:
        wait_id = await asyncio.to_thread(_wait_begin, db, driver_id, seconds)
    except WaitNotRegistered as exc:
        from core.state_claims import ClaimError
        raise ClaimError("wait_not_registered", "the database is busy and this wait could not be recorded, so "
                         "you would not count as present; retry the wait") from exc
    try:
        yield
    finally:
        try:
            await asyncio.shield(asyncio.to_thread(_wait_end, db, wait_id))
        except Exception:  # noqa: BLE001 - a CancelledError propagates; the row stays bounded by `until`
            _log.warning("recording the end of wait %s failed", wait_id, exc_info=True)


# -- describing a claim holder (information only) ---------------------------------------
def _hint(claim_id, fence, presence) -> str:
    why = (f"declared offline since {presence['since']}" if presence["basis"] == "declared" else
           f"last seen {presence['last_seen_at'] or 'never'}")
    return (f"holder is offline ({why}); any other member may take_over_claim(project_id, claim_id={claim_id}, "
            f"expected_fence={fence}, reason, request_key)")


def annotate_claims(conn, claims, now=None) -> None:
    """Add ``holder_presence`` to each claim view, and ``takeover_hint`` to a
    live claim whose holder is offline. Describes; decides nothing."""
    cache = {}
    for claim in claims:
        holder = claim["driver_id"]
        if holder not in cache:
            cache[holder] = without_reason(effective_presence(conn, holder, now))
        claim["holder_presence"] = cache[holder]
        if claim.get("status") == "live" and cache[holder]["status"] == "offline":
            claim["takeover_hint"] = _hint(claim["claim_id"], claim["fence"], cache[holder])


def holder_note(conn, holder_driver_id, claim_id, fence) -> tuple[str, dict]:
    """(message suffix, facts) for a ``claimed_by_other`` refusal."""
    presence = without_reason(effective_presence(conn, holder_driver_id))
    if presence["status"] == "offline":
        hint = _hint(claim_id, fence, presence)
        return f"; {hint}", {"holder_presence": presence, "takeover_hint": hint}
    return f"; holder is {presence['status']}", {"holder_presence": presence}
