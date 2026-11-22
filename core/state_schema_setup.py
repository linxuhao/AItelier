"""State schema setup runs once per process per database FILE, not per request.

Every State request builds a StateService, and every store it builds used to
re-run its own schema setup: executescript DDL, PRAGMA table_info and additive
column checks, commits. Measured on a scratch database that was 19 connections
and ~168 statements per construction, and under a burst it filled every anyio
worker slot (backend latency study, 2026-11-22).

``once`` runs one named setup the first time this process meets a file and
skips it afterwards. The file is identified by (realpath, st_dev, st_ino), so a
different file - a fresh tmp database, a template copy, a file moved over the
old path - gets its own setup. A setup that raises is not recorded, so the next
construction runs it again. Concurrent first constructions on one file wait on
a per-file lock: one runs the setup, the rest skip it once it is recorded.

What is NOT covered here: the attempt foreign-key verification stays on every
request (``core.state_attempt_schema.initialize(..., references_only=True)``,
iss-5de9f15b64f54027), so FK-OFF corruption still reaches the loud path on the
next call. A schema mutated behind this process's back (a dropped index or
column) is repaired by the next process start, or after ``forget``.

``:memory:`` and URI names have no file identity; they keep a per-call setup.
"""
from __future__ import annotations

import os
import threading

_DONE: set = set()
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


def once(db, name: str, setup, *args) -> bool:
    """Run ``setup(*args)`` unless this process already ran ``name`` on ``db``'s file.

    Returns True when the setup ran in this call.
    """
    key = _file_key(db)
    if key is None:
        # No file yet (the setup creates it) or no file identity at all.
        setup(*args)
        key = _file_key(db)
        if key is not None:
            _DONE.add((key, name))
        return True
    if (key, name) in _DONE:
        return False
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        if (key, name) in _DONE:
            return False
        setup(*args)
        _DONE.add((key, name))
    return True


def forget(db=None) -> None:
    """Forget what ran: for ``db``'s file, or for every file (a process restart)."""
    with _GUARD:
        if db is None:
            _DONE.clear()
            return
        key = _file_key(db)
        for entry in [e for e in _DONE if e[0] == key]:
            _DONE.discard(entry)
