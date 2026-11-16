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
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

# Closes are short once the checkpoint has run; a handful of closers keeps a
# burst of them from queueing behind one stalled disk while staying far below
# the number of connections a request burst opens.
CLOSER_THREADS = 4
# Named so a stack or a thread dump says what a `sqlite-close-*` thread is.
CLOSER_THREAD_PREFIX = "sqlite-close"

_CLOSER: ThreadPoolExecutor | None = None
_CLOSER_LOCK = threading.Lock()


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
