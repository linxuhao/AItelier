"""Where a SQLite connection may be CLOSED, so the backend event loop is never it.

Closing a connection is not a tidy-up. In WAL mode it runs
``sqlite3WalClose -> sqlite3WalCheckpoint -> fdatasync``: the file handle cannot
be released until the WAL has been checkpointed and synced. On the event-loop
thread that is disk sleep the whole process waits behind (measured wchan
``submit_bio_wait``, `iss-ecea53516eb543c4`), and every database operation in
this codebase opens and closes its OWN connection, so the cost is paid per
operation.

The fix is placement, not behaviour. The transaction was already committed (or
never opened) before the close runs, so durability, write ordering and error
reporting are all untouched: only the THREAD that finishes the file handle
changes. A caller on the event-loop thread hands the close to a small dedicated
thread; a connection that still holds an open transaction is closed INLINE on
every thread, because ``close()`` is what rolls that transaction back and a
rollback must not wait behind other callers' work.

This module holds no database, no schema and no legacy import: it exists so
both connection providers (`core.db_manager.DBManager` and the State-only
`core.state_database.StateDatabase`) share ONE rule instead of two copies of it.

The second rule here answers the same question for a different failure - WHEN
it may be paid for. `retry_while_locked` is the bounded retry the two
best-effort bookkeeping writes give SQLite's "database is locked" (the
answering-model row the completion route writes, and the stamp that ends a
driver wait - see the importers): a writer that loses that race usually lost it
to a transaction that is finishing, so one or two more attempts land the row the
caller would otherwise lose. The backoff is a BLOCKING sleep, so the helper
belongs to a worker thread; both callers are already off the event loop
(`asyncio.to_thread`), and nothing may call it from the loop thread.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# Closes are short once the checkpoint has run; a handful of closers keeps a
# burst of them from queueing behind one stalled disk while staying far below
# the number of connections a request burst opens.
CLOSER_THREADS = 4
# Named so a stack or a thread dump says what a `sqlite-close-*` thread is.
CLOSER_THREAD_PREFIX = "sqlite-close"

_CLOSER: ThreadPoolExecutor | None = None
_CLOSER_LOCK = threading.Lock()

# Three attempts and 0.1 s / 0.2 s between them: the measured lock holders in
# `iss-0297fe77883241b5` are other writers finishing a transaction, so a row
# lost on the first refusal is usually recovered by the second, while a lock
# that is truly held still costs the caller a bounded 0.3 s of waiting.
LOCKED_ATTEMPTS = 3
LOCKED_BACKOFF_SECONDS = 0.1
LOCKED_BACKOFF_MAX_SECONDS = 0.2


def on_event_loop() -> bool:
    """True when the caller is running on an asyncio event-loop thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _closer() -> ThreadPoolExecutor:
    global _CLOSER
    with _CLOSER_LOCK:
        if _CLOSER is None:
            _CLOSER = ThreadPoolExecutor(max_workers=CLOSER_THREADS,
                                         thread_name_prefix=CLOSER_THREAD_PREFIX)
        return _CLOSER


def _close_quietly(conn) -> None:
    """Close on a thread nobody is waiting on, so nothing escapes to nobody."""
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - a deferred close has no caller to report to
        logging.getLogger("aitelier").debug("deferred sqlite close failed", exc_info=True)


def _defer_close(conn) -> None:
    try:
        _closer().submit(_close_quietly, conn)
    except RuntimeError:
        # Interpreter shutdown: no worker will run this, so pay it here.
        _close_quietly(conn)


def close_connection(conn, on_loop: bool | None = None) -> None:
    """Close `conn`, never making the event loop wait for the checkpoint.

    `on_loop` is the answer `on_event_loop()` gave when the connection was
    OPENED - passed in because the same caller is still on that thread here, and
    one call site may hold a connection across an await. `None` re-measures.
    """
    if on_loop is None:
        on_loop = on_event_loop()
    if not on_loop or conn.in_transaction:
        conn.close()
        return
    _defer_close(conn)


def locked_database(exc: BaseException) -> bool:
    """True ONLY for SQLite's "another connection holds the write lock".

    The condition, not the class: every other ``OperationalError`` - no such
    table, a read-only file, a full disk, a malformed schema - is permanent, and
    retrying it would triple the latency and delay the report without ever
    landing the row. ``database table is locked`` is deliberately NOT this: it
    names a different, longer-lived refusal.
    """
    return (isinstance(exc, sqlite3.OperationalError)
            and "database is locked" in str(exc).lower())


def _sleep(seconds: float) -> None:
    """The backoff. A plain blocking sleep - which is why this is worker-thread
    only, and why it is a module attribute a test can substitute to place the
    release of a real lock exactly between two attempts."""
    time.sleep(seconds)


def retry_while_locked(write, *, attempts: int = LOCKED_ATTEMPTS, sleep=None):
    """Call ``write()``, retrying a HELD WRITE LOCK at most ``attempts`` times.

    ``write`` must be a complete unit of work (open, write, commit) - it is
    called again from the start on a lock refusal, never resumed mid-transaction,
    so a retried attempt can neither half-apply nor double-count.

    Returns what the last successful call returned and re-raises whatever the
    final attempt raised, so a caller keeps its own permanent-failure branch: the
    retry narrows the window in which a row is lost, it does not swallow the
    failure when the lock really is held.

    Call it from a worker thread. The backoff blocks, and the event loop must
    never wait for it.
    """
    sleeper = sleep or _sleep
    delay = LOCKED_BACKOFF_SECONDS
    for attempt in range(1, attempts + 1):
        try:
            return write()
        except sqlite3.OperationalError as exc:
            if attempt >= attempts or not locked_database(exc):
                raise
            sleeper(delay)
            delay = min(delay * 2, LOCKED_BACKOFF_MAX_SECONDS)
    raise AssertionError("unreachable: the last attempt returns or raises")
