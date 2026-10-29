"""Driver presence v2: declared offline, waits in progress, effective presence.

Presence v1 (core.drivers.presence_of) derives online/away/offline from
``drivers.last_seen_at``. This module adds two FACTS and one combination:

* a DECLARATION: a driver may say "I am offline" (reason, expected return) for
  itself - ``declare`` - and an ``online`` declaration clears it. One row per
  driver in ``driver_presence_declarations``; created lazily on first write.
* a WAIT MARKER: a driver blocked in ``wait_for_driver_inbox``,
  ``wait_for_state_change`` or ``wait_for_run`` is present. ``driver_waiting``
  counts the waits in progress in ``driver_presence_waits`` (one row per
  driver: how many are open, until when the longest one can last, when the
  last one ended).

``effective_presence`` combines them, in this order:

1. ``online`` while a wait is in progress or ended at most
   ``AITELIER_PRESENCE_WAIT_GRACE_SECONDS`` (default 120) ago. This overrides a
   declaration; starting a wait also clears the declaration (audited);
2. ``offline`` while a declaration says so - even against recent
   ``last_seen_at`` stamps from ordinary reads; claim_node, heartbeat,
   start_external_attempt and report_external_attempt by that driver clear it
   (``mark_back``);
3. otherwise the v1 derived presence.

WHY THE WAIT MARKER IS IN THE DATABASE, not an in-process counter: presence
now carries ONE authority (core.state_takeover: another member may take over
the claims of an OFFLINE owner), and that decision must see a wait running in
any process that shares the database. Production runs one uvicorn process for
/api/state and MCP, but the State-only service (api.state_only), the host CLI
and ``docker exec`` tools open the same SQLite file from other processes, and a
second worker would split an in-process registry silently - in the UNSAFE
direction (a waiting owner judged offline). The marker is bounded by the
wait's own timeout (``active_until``), so a crashed process cannot keep a
driver online for longer than its wait could have lasted plus the grace.

Authority: presence is information EXCEPT for take_over_claim. Every other
caller only describes it (tests/unit/test_driver_presence.py and
tests/unit/test_driver_offline_takeover.py guard that).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from core import drivers

WAIT_GRACE_ENV = "AITELIER_PRESENCE_WAIT_GRACE_SECONDS"
WAIT_GRACE_DEFAULT = 120
WAIT_SLACK_SECONDS = 30          # a wait's own bookkeeping after its timeout
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
    " driver_id TEXT PRIMARY KEY,"
    " active INTEGER NOT NULL CHECK(active >= 0),"
    " active_until TEXT NOT NULL,"
    " last_wait_start TEXT NOT NULL,"
    " last_wait_end TEXT)",
)
_log = logging.getLogger(__name__)


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
    row = conn.execute("SELECT active, active_until, last_wait_end FROM driver_presence_waits WHERE driver_id=?",
                       (driver_id,)).fetchone()
    if row is None:
        return False, None
    until, ended = _parse(row[1]), _parse(row[2])
    waiting = bool(row[0]) and until is not None and current <= until
    if row[0] and until is not None and current > until:
        # A wait whose process never recorded its end lasted at most until then.
        ended = until if ended is None else max(ended, until)
    return waiting, ended


def effective_presence(conn, driver_id, now=None) -> dict:
    """The presence of ``driver_id`` as one dict, read on ``conn``.

    Keeps the v1 fields (``status``, ``last_seen_at``, ``seconds_since_seen``)
    and adds ``basis`` (wait | declared | last_seen), ``derived_status`` (the
    v1 answer), ``declared`` with ``declared_status``/``reason``/``since``/
    ``expected_return``, ``waiting`` and ``last_wait_end``.
    """
    current = now if isinstance(now, datetime) else _clock()
    seen = None
    if _has(conn, "drivers"):
        row = conn.execute("SELECT last_seen_at FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()
        seen = row[0] if row is not None else None
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
    return {**derived, "status": status, "basis": basis, "derived_status": derived["status"],
            "declared": declaration is not None,
            "declared_status": declaration["status"] if declaration else None,
            "reason": declaration["reason"] if declaration else None,
            "since": declaration["declared_at"] if declaration else None,
            "expected_return": declaration["expected_return"] if declaration else None,
            "waiting": waiting, "last_wait_end": _stamp(ended) if ended is not None else None}


def presence_for(db, driver_id, now=None) -> dict:
    with db.get_connection() as conn:
        return effective_presence(conn, driver_id, now)


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
    """
    if not isinstance(driver_id, str) or not driver_id:
        raise drivers.DriverError("driver_identity_required: presence is declared by a registered driver for itself")
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


def mark_back(store, driver_id, cause) -> None:
    """``driver_id`` acted (claim_node, heartbeat, start/report of an external
    attempt): it is back. Clears its offline declaration (audited) and refreshes
    ``last_seen_at`` (throttled, like every authenticated request). A cheap read
    decides first; a busy database never fails the caller's request."""
    if not driver_id or cause not in CLEAR_CAUSES:
        return
    from core.state_privacy import UntrustedDatabase
    db = getattr(store, "db", None)
    if db is None or isinstance(db, UntrustedDatabase):
        return
    drivers.touch_driver(db, driver_id)
    try:
        with db.get_connection() as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            if not _has(conn, "driver_presence_declarations") or conn.execute(
                    "SELECT 1 FROM driver_presence_declarations WHERE driver_id=?", (driver_id,)).fetchone() is None:
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                _clear(conn, driver_id, cause, f"driver:{driver_id}")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    except sqlite3.Error:
        _log.warning("could not clear the offline declaration of %s", driver_id, exc_info=True)


# -- waits -------------------------------------------------------------------------
def _wait_begin(db, driver_id, timeout_seconds) -> bool:
    current = _clock()
    until = _stamp(current + timedelta(seconds=float(timeout_seconds) + WAIT_SLACK_SECONDS))
    now = _stamp(current)
    try:
        with db.get_connection() as conn:
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                _ensure(conn)
                # SET expressions read the OLD row: a stale counter (its until
                # passed - a process died mid-wait) restarts at 1.
                conn.execute("INSERT INTO driver_presence_waits(driver_id,active,active_until,last_wait_start,"
                             "last_wait_end) VALUES(?,1,?,?,NULL) ON CONFLICT(driver_id) DO UPDATE SET "
                             "active=CASE WHEN active_until<? THEN 1 ELSE active+1 END,"
                             "active_until=MAX(active_until,excluded.active_until),"
                             "last_wait_start=excluded.last_wait_start",
                             (driver_id, until, now, now))
                _clear(conn, driver_id, "wait", f"driver:{driver_id}")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return True
    except sqlite3.Error:
        _log.warning("could not record the wait of %s", driver_id, exc_info=True)
        return False


def _wait_end(db, driver_id) -> None:
    try:
        with db.get_connection() as conn:
            # Short: the wait's answer is held until this returns. A miss is safe -
            # the driver stays online until active_until, never longer.
            conn.execute("PRAGMA busy_timeout=500")
            conn.execute("UPDATE driver_presence_waits SET active=MAX(active-1,0),last_wait_end=? WHERE driver_id=?",
                         (_stamp(_clock()), driver_id))
            conn.commit()
    except sqlite3.Error:
        # The marker stays bounded by active_until: online a little longer, never longer than the wait could last.
        _log.warning("could not record the end of a wait of %s", driver_id, exc_info=True)


@asynccontextmanager
async def driver_waiting(db, driver_id, timeout_seconds):
    """Mark ``driver_id`` as waiting for the duration of the block (off the event loop)."""
    from core.state_privacy import UntrustedDatabase
    if not driver_id or db is None or isinstance(db, UntrustedDatabase) or not hasattr(db, "get_connection"):
        yield
        return
    try:
        seconds = float(timeout_seconds)
    except (TypeError, ValueError):
        seconds = 0.0
    seconds = min(max(seconds, 0.0), 3600.0) if seconds == seconds else 0.0   # NaN -> 0
    started = await asyncio.to_thread(_wait_begin, db, driver_id, seconds)
    try:
        yield
    finally:
        if started:
            try:
                await asyncio.shield(asyncio.to_thread(_wait_end, db, driver_id))
            except BaseException:  # noqa: BLE001 - cancelled while recording: the thread still finishes
                pass


# -- describing a claim holder (information only) ---------------------------------------
def _hint(claim_id, fence, presence) -> str:
    why = (f"declared offline since {presence['since']}" + (f" ({presence['reason']})" if presence["reason"] else "")
           if presence["basis"] == "declared" else
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
            cache[holder] = effective_presence(conn, holder, now)
        claim["holder_presence"] = cache[holder]
        if claim.get("status") == "live" and cache[holder]["status"] == "offline":
            claim["takeover_hint"] = _hint(claim["claim_id"], claim["fence"], cache[holder])


def holder_note(conn, holder_driver_id, claim_id, fence) -> tuple[str, dict]:
    """(message suffix, facts) for a ``claimed_by_other`` refusal."""
    presence = effective_presence(conn, holder_driver_id)
    if presence["status"] == "offline":
        hint = _hint(claim_id, fence, presence)
        return f"; {hint}", {"holder_presence": presence, "takeover_hint": hint}
    return f"; holder is {presence['status']}", {"holder_presence": presence}
