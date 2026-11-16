"""Minimal explicit SQLite connection provider for a State-only deployment.

No legacy run/task tables, workspace manager, model registry or SkillFlow import.
Use DBManager instead when embedding StateService in the normal AItelier host.
"""
from __future__ import annotations
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3

from core.off_loop_sqlite import close_connection, on_event_loop


class StateDatabase:
    def __init__(self, db_path: str):
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            raise ValueError('State-only database path must be explicit and absolute')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db_path = str(path.resolve())
        # New DB is private; do not overwrite an existing database or its mode.
        try:
            fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        with self.get_connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA foreign_keys=ON')

    @contextmanager
    def get_connection(self):
        # Same rule as DBManager (core/off_loop_sqlite): closing a WAL
        # connection checkpoints and fsyncs, so an event-loop caller must not be
        # the one to finish the handle. `POST /api/state/query/{action}` is an
        # async route that reads through here, and a read's close is as much
        # disk sleep on the loop as a write's.
        on_loop = on_event_loop()
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=not on_loop)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            yield conn
        finally:
            close_connection(conn, on_loop)

    def get_project(self, project_id):
        # This method denotes legacy execution projects on DBManager, NOT State
        # Projects. State-only deployment deliberately has none of those rows.
        return None
