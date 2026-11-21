"""The no-argument projection reports the WHOLE pass, not one payload-capped batch.

The defect this file freezes (iss-fa8328ccf08f4603, measured on the live State DB
on 2026-11-20): the dry run reported ``rows=97`` / ``payload_bytes=267,787,397``
while the ``--apply`` that followed, on the same database, released 1,036 rows /
2,916,640,194 payload bytes - the projection had taken the first payload-capped
batch and called it the pass, while the line beside it already said
``eligible_rows=1,049``. A director quotes the projection to other drivers before
a maintenance window, so it has to be the number the release will actually take.

THE NUMBERS THIS FILE COMPARES, in its own words: the projection's row count
(``rows``) against the row count the same release reports once applied
(``released_rows``), and the projection's stored-payload-byte total
(``payload_bytes``) against ``released_payload_bytes``. Both runs go through the
real ``release()`` of ``tools/release_bundle_store.py``; nothing here re-implements
its batching. The fixture fixes what the pass must be independently of the tool's
output: six 64 KiB artifacts are planted, every one is releasable, so the pass is
six rows and the payload sizes are measured from the store rows the fixture wrote.
Under a cap one third of the planted bytes, that set cannot be one batch.
"""
from __future__ import annotations

import hashlib
import importlib.util
import random
import sqlite3
from pathlib import Path

import pytest

from core.db_manager import DBManager
from core.state_git_artifacts import BUNDLE_STORE, RELEASE_TABLE, store_candidate, typed_ref
from core.state_service import StateService

ROOT = Path(__file__).resolve().parents[2]
SIZE = 64 * 1024
PLANTED = 6


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))


@pytest.fixture(scope="module")
def tool():
    """The operator tool itself, loaded by path so its ``release`` runs for real."""
    spec = importlib.util.spec_from_file_location(
        "release_bundle_store_projection", ROOT / "tools" / "release_bundle_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def raw(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def state_database(path):
    """A scratch host+State database: the shape the operator tool runs on."""
    db = DBManager(str(path))
    StateService(db, actor="fixture", project_read_trusted=True)
    return path


def plant(path, count=PLANTED):
    """Write ``count`` independent random artifacts; returns their digests."""
    digests = []
    conn = raw(path)
    try:
        for seed in range(count):
            data = random.Random(seed).randbytes(SIZE)
            digest = hashlib.sha256(data).hexdigest()
            store_candidate(conn, {"commit_sha": hashlib.sha1(data).hexdigest(),
                                   "tree_sha": "e" * 40, "bundle_sha256": digest,
                                   "retained_ref": typed_ref(digest), "bundle_bytes": data})
            digests.append(digest)
        codecs = [row["codec"] for row in conn.execute(f"SELECT codec FROM {BUNDLE_STORE}")]
    finally:
        conn.close()
    # Independent random blobs share no prefix, so every one is a raw keyframe (a
    # delta would exceed its own size's delta budget) and no row is pinned.
    assert codecs == ["raw"] * count, codecs
    return digests


def stored_payload(path, digest):
    """The payload bytes the fixture's store row holds for ``digest``."""
    conn = raw(path)
    try:
        return int(conn.execute(f"SELECT length(payload) FROM {BUNDLE_STORE} "
                                "WHERE bundle_sha256=?", (digest,)).fetchone()[0])
    finally:
        conn.close()


def ledger_length(path):
    conn = raw(path)
    try:
        return (conn.execute(f"SELECT count(*) FROM {BUNDLE_STORE}").fetchone()[0],
                conn.execute(f"SELECT count(*) FROM {RELEASE_TABLE}").fetchone()[0])
    finally:
        conn.close()


def test_the_projection_totals_are_the_payload_capped_pass_apply_takes(tmp_path, tool, capsys):
    path = state_database(tmp_path / "state.db")
    digests = plant(path)
    held = {digest: stored_payload(path, digest) for digest in digests}
    whole_pass_payload = sum(held.values())
    # The cap that broke the live dry run, scaled to the fixture: two rows fit,
    # a third never does, so six rows cannot be one payload-capped batch.
    cap = 2 * (SIZE + 1024)

    projection = tool.release(str(path), batch_size=8, max_payload_bytes=cap)
    trace = capsys.readouterr().err

    # A projection writes nothing: the store still holds every row it counted
    # and the release ledger is still empty when it reports.
    assert ledger_length(path) == (PLANTED, 0)
    assert projection["dry_run"] is True
    assert projection["rows"] == PLANTED
    assert projection["payload_bytes"] == whole_pass_payload
    # Six rows under this cap must span at least three batches; one batch is the
    # defect, whatever total it reports.
    assert projection["batches"] >= 3, projection
    # The operator-facing line names the pass it is describing, not batch one.
    assert f"{projection['batches']} batch(es)" in trace, trace

    applied = tool.release(str(path), actor="fixture:release", apply=True,
                           batch_size=8, max_payload_bytes=cap)

    # THE COMPARISON: the same pass's row count and payload bytes, projected and
    # then applied. They are equal or the projection lied about the window.
    assert applied["released_rows"] == projection["rows"], (applied, projection)
    assert applied["released_payload_bytes"] == projection["payload_bytes"], (applied, projection)
    assert applied["released_raw_size"] == projection["raw_size"]
    assert applied["batches"] == projection["batches"]
    assert sorted(applied["digests"]) == sorted(projection["digests"])
    # And the release really took the fixture's whole set, not a prefix of it.
    assert applied["released_rows"] == PLANTED
    assert ledger_length(path) == (0, PLANTED)


def test_the_projection_counts_the_batches_a_small_batch_size_forces(tmp_path, tool):
    path = state_database(tmp_path / "state.db")
    digests = plant(path)
    whole_pass_payload = sum(stored_payload(path, digest) for digest in digests)

    projection = tool.release(str(path), batch_size=2)

    assert projection["rows"] == PLANTED
    assert projection["payload_bytes"] == whole_pass_payload
    # Two rows per batch over six planted rows is three batches; the projection
    # has to name that count, and the apply that follows has to take that many.
    assert projection["batches"] == (PLANTED + 1) // 2, projection
    applied = tool.release(str(path), actor="fixture:release", apply=True, batch_size=2)
    assert applied["released_rows"] == projection["rows"]
    assert applied["released_payload_bytes"] == projection["payload_bytes"]
    assert applied["batches"] == projection["batches"]
