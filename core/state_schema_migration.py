"""Additive column migrations that tolerate a concurrent writer.

State modules add a column they find missing with a PRAGMA check followed by
``ALTER TABLE ... ADD COLUMN``. The check and the ALTER are two statements, so
two connections that both see the column missing both run the ALTER, and the
loser gets ``sqlite3.OperationalError: duplicate column name: <column>``. The
schema is then exactly what the loser was about to make, so that one outcome is
absorbed here and nothing else is: ``database is locked``, ``no such table``,
disk errors and every other message propagate unchanged. The caller keeps its
own connection, transaction and commit.
"""
from __future__ import annotations

import re
import sqlite3

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a plain SQL identifier, not {value!r}")
    return value


def column_names(conn, table: str) -> set[str]:
    """The column names ``table`` has right now, from ``PRAGMA table_info``."""
    _identifier(table, "table")
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def add_column_if_missing(conn, table: str, column: str, definition: str) -> bool:
    """Run ``ALTER TABLE <table> ADD COLUMN <column> <definition>`` unless it is already there.

    ``definition`` is a literal from the migration that calls this, never input.
    Returns True only when this call added the column. A duplicate-column error
    is accepted only when it names the column being added; any other error,
    including another duplicate, is raised as it was.
    """
    _identifier(table, "table")
    _identifier(column, "column")
    if column in column_names(conn, table):
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as exc:
        if str(exc) == f"duplicate column name: {column}":
            # Another writer added it between the check and the ALTER: the
            # schema is what this migration wanted, so the caller carries on.
            return False
        raise
    return True
