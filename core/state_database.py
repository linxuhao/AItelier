"""Minimal explicit SQLite connection provider for a State-only deployment.

No legacy run/task tables, workspace manager, model registry or SkillFlow import.
Use DBManager instead when embedding StateService in the normal AItelier host.
"""
from __future__ import annotations
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3


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
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            yield conn
        finally:
            conn.close()

    def get_project(self, project_id):
        # This method denotes legacy execution projects on DBManager, NOT State
        # Projects. State-only deployment deliberately has none of those rows.
        return None
