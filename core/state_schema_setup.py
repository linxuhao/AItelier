"""State schema setup runs once per database handle and FILE, not per request.

Every State request builds a StateService, and every store it builds used to
re-run its own schema setup: executescript DDL, PRAGMA table_info and additive
column checks, commits. Measured on a scratch database that was 19 connections
and ~168 statements per construction, and under a burst it filled every anyio
worker slot (backend latency study, 2026-11-22).

``once`` runs one named setup the first time a database handle (a DBManager or
StateDatabase) meets a file and skips it afterwards. Every product entry point
holds one handle per process (api.dependencies.db_instance, the State-only app,
the DSH host), so in production that is once per process per file. The file is
identified by (realpath, st_dev, st_ino), so a different file - a fresh tmp
database, a template copy, a file moved over the old path - gets its own setup,
and opening a database through a NEW handle sets it up again, as opening it
always did. A setup that raises is not recorded, so the next construction runs
it again. Concurrent first constructions on one file wait on a per-file lock:
one runs the setup, the rest skip it once it is recorded.

What is NOT covered here: the attempt foreign-key verification stays on every
request (``core.state_attempt_schema.initialize(..., references_only=True)``,
iss-5de9f15b64f54027), so FK-OFF corruption still reaches the loud path on the
next call. That verification is the one data-dependent step left in a request's
construction: after any commit it re-runs the anti-join scan over the attempt
tables, one caller per file at a time, so its cost grows with the live database.
Moving it off the request path needs an owner ruling (latency study item (d)).

A schema mutated behind a handle's back (a dropped index or column) is repaired
by the next handle (the next process start), or after ``forget``. The same holds
for a file whose CONTENT is replaced under a running process without a new inode
(``cp backup.db aitelier.db``, ``sqlite3 .restore`` into the live path): its
structural setup is not re-run until the backend restarts, so such a restore
needs a restart. Before this module the next request repaired it.
Overwriting the database file in place requires a backend restart; otherwise
structural repair (schema setup) will not run again.

``:memory:`` and URI names have no file identity; they keep a per-call setup.
"""
from __future__ import annotations

import os
import threading
import weakref

# handle -> {(file key, setup name)}; a handle that cannot be weakly referenced
# shares one set keyed by file alone.
_DONE = weakref.WeakKeyDictionary()
_SHARED: set = set()
_LOCKS: dict = {}
_GUARD = threading.Lock()


def _file_key(db):
    path = getattr(db, "db_path", None)
    if not isinstance(path, str) or not path or path == ":memory:" or path.startswith("file:"):
        return None
    try:
        real = os.path.realpath(path)
        found = os.stat(real)
    except OSError:
        return None
    return (real, found.st_dev, found.st_ino)


def _done(db) -> set:
    done = _DONE.get(db) if _weakrefable(db) else _SHARED
    if done is None:
        with _GUARD:
            done = _DONE.setdefault(db, set())
    return done


def _weakrefable(db) -> bool:
    try:
        weakref.ref(db)
        hash(db)
    except TypeError:
        return False
    return True


def once(db, name: str, setup, *args) -> bool:
    """Run ``setup(*args)`` unless ``db`` already ran ``name`` on its current file.

    Returns True when the setup ran in this call.
    """
    key = _file_key(db)
    if key is None:
        # No file yet (the setup creates it) or no file identity at all.
        setup(*args)
        key = _file_key(db)
        if key is not None:
            _done(db).add((key, name))
        return True
    done = _done(db)
    if (key, name) in done:
        return False
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        if (key, name) in done:
            return False
        setup(*args)
        done.add((key, name))
    return True


def forget(db=None) -> None:
    """Forget what ran: for ``db``, or for every handle (a process restart)."""
    with _GUARD:
        if db is None:
            _DONE.clear()
            _SHARED.clear()
            _LOCKS.clear()
        elif _weakrefable(db):
            _DONE.pop(db, None)
        else:
            _SHARED.clear()
