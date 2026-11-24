"""The retained Git bundle bound is ONE number, shared by every gate.

A producer bundle one byte over the bound used to be refused before any Git
step, while the SQL CHECK, the guide and the release default carried copies of
the same limit. These tests pin the single definition and prove both the Python
read gate and the SQL CHECK admit exactly the bound and refuse one byte more.
"""
import hashlib
import json
import sqlite3

import pytest

import core.state_attempt_schema as schema
import core.state_bundle_limits as limits
import core.state_git_artifacts as sga
from core.state_attempt_schema import EXTRA_SCHEMA, _RAW_CAP, _statements
from core.state_database import StateDatabase
from core.state_driver_guide import GUIDE_SECTIONS, STATE_DRIVER_GUIDE
from core.state_graph import StateConflict
from core.state_service import StateService

BOUND = 536870912  # 512 MiB


def test_python_constant_and_deployed_sql_literal_are_one_number():
    """The module constant, its two importers and the schema literal agree."""
    # EXTRA_SCHEMA is built with an f-string: a literal brace would be read as
    # interpolation. Guard that the template still has none besides our own.
    assert "{" not in EXTRA_SCHEMA and "}" not in EXTRA_SCHEMA
    assert limits.MAX_BUNDLE_BYTES == BOUND
    assert sga.MAX_BUNDLE_BYTES == BOUND
    assert schema.MAX_BUNDLE_BYTES == BOUND
    assert int(_RAW_CAP.search(EXTRA_SCHEMA).group(1)) == BOUND


def test_initialized_schema_deploys_the_derived_check(tmp_path):
    """A real State initialization stores the derived CHECK, not a stale copy."""
    path = tmp_path / "state.sqlite"
    StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='state_git_bundle_store'").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert f"raw_size <= {BOUND}" in row[0]
    assert "raw_size <=" in row[0] and "268435456" not in row[0]


def test_guide_text_served_to_drivers_advertises_the_new_bound():
    """The bound a driver reads in the guide is the new 512 MiB figure."""
    assert "512 MiB" in STATE_DRIVER_GUIDE
    section = GUIDE_SECTIONS["handle-changes-without-inventing-acceptance"]["text"]
    assert "bounded to 512 MiB" in section
    assert "256 MiB" not in section


def test_producer_read_admits_exact_bound_and_refuses_one_byte_more(tmp_path, monkeypatch):
    """The real producer-file gate admits the bound and refuses bound+1 before Git."""
    bound = 1024
    monkeypatch.setattr(sga, "MAX_BUNDLE_BYTES", bound)
    reached = []
    monkeypatch.setattr(sga, "_normalize_candidate",
                        lambda raw, sha, commit: reached.append(len(raw)) or {"ok": True})

    def report(size, name):
        payload = b"a" * size
        (tmp_path / name).write_bytes(payload)
        return json.dumps({"git_bundle": {
            "path": str(tmp_path / name),
            "sha256": hashlib.sha256(payload).hexdigest()}}).encode()

    assert sga.retain_candidate(report(bound, "exact.bundle"), "a" * 40) == {"ok": True}
    assert reached == [bound]  # the file was admitted and reached normalization
    with pytest.raises(StateConflict, match="local artifact exceeds size limit"):
        sga.retain_candidate(report(bound + 1, "over.bundle"), "b" * 40)
    assert reached == [bound]  # the SIZE refusal happened before any Git step


def test_store_check_admits_exact_bound_and_refuses_one_byte_more(tmp_path):
    """The SQL CHECK admits raw_size=BOUND and refuses BOUND+1, with a 1-byte payload."""
    conn = sqlite3.connect(str(tmp_path / "s.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        for statement in _statements(EXTRA_SCHEMA):
            if "state_git_" in statement:
                conn.execute(statement)
        insert = ("INSERT INTO state_git_bundle_store "
                  "(bundle_sha256,codec,base_sha256,raw_size,payload,created_at) "
                  "VALUES(?,'raw',NULL,?,x'00','2026-11-24T00:00:00')")
        conn.execute(insert, ("a" * 64, BOUND))
        with pytest.raises(sqlite3.IntegrityError, match="raw_size"):
            conn.execute(insert, ("b" * 64, BOUND + 1))
        row = conn.execute(
            "SELECT raw_size,length(payload) AS n FROM state_git_bundle_store "
            "WHERE bundle_sha256=?", ("a" * 64,)).fetchone()
        assert (row["raw_size"], row["n"]) == (BOUND, 1)
    finally:
        conn.close()
