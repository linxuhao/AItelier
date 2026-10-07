"""A retained candidate's ``bundle_bytes`` are a PRIVATE row, not just a tag.

Criterion ``git-artifact-rows-are-private``. ``state_git_artifacts`` is created
by the schema and holds the exact Git bytes that reconstruct a candidate. The
commit/tree identity is public (an opened project's attempt names it), but the
BLOB is the candidate's own bytes and must never be delivered by an untrusted
connection.

The verdict is not "the table appears in a private list": the actual connection
projection must replace ``bundle_bytes`` with NULL, so an armed reader that
selects the column gets ``None`` and a reader that names the database's own
table is refused. The positive controls below prove the column really holds the
bytes and that an unopened project's artifact rows are absent entirely.
"""
from __future__ import annotations

import sqlite3

import pytest

from core.state_commands import ProjectPrivate
from core.state_graph import StateGraphStore
from core.state_privacy import PRIVATE_STATE_TABLES


def _durable_candidate(tmp_path, monkeypatch):
    """A real external ``git-sha1`` candidate with retained bundle bytes."""
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    from tests.integration.test_state_git_artifact_durability import fixture
    return fixture(tmp_path / "fixture")


def _raw_row(db):
    """The whole artifact row read by an UNARMED connection: the control."""
    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        return dict(raw.execute("SELECT * FROM state_git_artifacts").fetchone())


def test_retained_bundle_bytes_are_classified_and_projected_to_null(tmp_path, monkeypatch):
    service, source, producer, db, attempt, commit, tree, payload, bundle = _durable_candidate(
        tmp_path, monkeypatch)
    # Positive control: the retained `bundle_bytes` really are the candidate's
    # exact Git bundle bytes, so the column this test guards is not a placeholder.
    control = _raw_row(db)
    assert control["commit_sha"] == commit
    assert bytes(control["bundle_bytes"]) == bundle.read_bytes()
    assert len(control["bundle_bytes"]) > 0
    # The table is classified, so `state_git_artifacts` cannot be an unclassified
    # table that silently defaults to public.
    assert "state_git_artifacts" in PRIVATE_STATE_TABLES
    # Opening the project puts the artifact's identity in the public projection.
    service.open_project("durable")
    untrusted = StateGraphStore(service.db, project_read_trusted=False)
    with untrusted.transaction() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM state_git_artifacts WHERE commit_sha=?",
                           (commit,)).fetchone()
        full = [dict(r) for r in conn.execute("SELECT * FROM state_git_artifacts").fetchall()]
    assert row is not None
    # Identity is readable; the private bytes are NULL on the actual connection.
    assert row["commit_sha"] == commit and row["tree_sha"] == tree
    assert row["bundle_bytes"] is None
    # Falsifiable verdict: the armed full-row read differs from the unarmed one
    # ONLY because the private bytes are gone. Removing the projection fails here.
    assert full and all(r["bundle_bytes"] is None for r in full)
    assert full[0]["commit_sha"] == control["commit_sha"]
    # Naming the database's own table is refused outright, not merely nulled.
    with pytest.raises(ProjectPrivate):
        with untrusted.transaction() as conn:
            conn.execute("SELECT bundle_bytes FROM main.state_git_artifacts").fetchall()


def test_unopened_project_artifact_rows_are_absent_from_an_untrusted_connection(
        tmp_path, monkeypatch):
    service, source, producer, db, attempt, commit, tree, payload, bundle = _durable_candidate(
        tmp_path, monkeypatch)
    assert bytes(_raw_row(db)["bundle_bytes"]) == bundle.read_bytes()
    # The project is never opened here: its artifact identity must be absent.
    untrusted = StateGraphStore(service.db, project_read_trusted=False)
    with untrusted.transaction() as conn:
        assert conn.execute("SELECT * FROM state_git_artifacts").fetchall() == []

