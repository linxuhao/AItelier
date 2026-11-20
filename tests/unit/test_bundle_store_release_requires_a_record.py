"""A retained bundle leaves the store only through a RECORDED release.

Criterion W1/W2 of ``evidence/bundle-store-release-20261120/report.json``: the
narrowed guard refuses a DELETE that no release row names and admits the same
DELETE once that row exists, in one transaction; and a released KEYFRAME whose
delta still exists is refused by the foreign key, so the release path's own
order (deltas first, keyframes last) is not decoration.

Both halves are measured on a real State schema with a real raw keyframe and a
real zstd delta: the assertions are about the shipped trigger text, not about a
test-local copy of it.
"""
from __future__ import annotations

import hashlib
import random
import sqlite3

import pytest

from core.state_database import StateDatabase
from core.state_git_artifacts import (
    BUNDLE_STORE, RELEASE_TABLE, load_stored_bundle, release_bundles, release_plan,
    store_bundle, store_candidate, typed_ref)
from core.state_graph import StateConflict
from core.state_service import StateService


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def blob(seed, size):
    return random.Random(seed).randbytes(size)


def edit(data, seed, edits=8):
    """A near-identical successor, so the store keeps it as a zstd delta."""
    rng = random.Random(seed)
    out = bytearray(data)
    for _ in range(edits):
        at = rng.randrange(len(out) - 64)
        out[at:at + 16] = rng.randbytes(16)
    return bytes(out)


def state_db(tmp_path, name="state.db"):
    """A scratch State database: the SHIPPED schema, on a disposable file."""
    path = tmp_path / name
    StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    return path


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def release_row(conn, digest, **overrides):
    """Insert the release record a DELETE has to name, from the store's own row."""
    stored = conn.execute(f"SELECT codec,base_sha256,raw_size,length(payload) AS n "
                          f"FROM {BUNDLE_STORE} WHERE bundle_sha256=?", (digest,)).fetchone()
    assert stored is not None, digest
    commit = conn.execute("SELECT commit_sha FROM state_git_artifacts WHERE bundle_sha256=?",
                          (digest,)).fetchone()
    values = {"bundle_sha256": digest, "commit_sha": commit[0] if commit else "f" * 40,
              "codec": stored["codec"], "base_sha256": stored["base_sha256"],
              "raw_size": stored["raw_size"], "payload_bytes": stored["n"],
              "released_at": "2026-11-20T00:00:00+00:00", "actor": "fixture",
              "rule": "no-live-consumer@1", "rows_before": 2, "payload_before": 2,
              "rows_after": 1, "payload_after": 1}
    values.update(overrides)
    conn.execute(f"INSERT INTO {RELEASE_TABLE}({','.join(values)}) "
                 f"VALUES({','.join('?' for _ in values)})", tuple(values.values()))
    return values


def capture(conn, raw, tree="e" * 40):
    """The product write path for one artifact: bytes to the store, row to the ledger."""
    digest = sha(raw)
    commit = hashlib.sha1(raw).hexdigest()
    store_candidate(conn, {"commit_sha": commit, "tree_sha": tree, "bundle_sha256": digest,
                           "retained_ref": typed_ref(digest), "bundle_bytes": raw})
    return commit, digest


class Recorder:
    """A connection proxy that remembers every statement the release executes."""

    def __init__(self, conn):
        self.conn = conn
        self.calls = []

    def execute(self, sql, parameters=()):
        self.calls.append((sql, parameters))
        return self.conn.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.conn, name)


def keyframe_and_delta(conn):
    """The fixture W1/W2 ask for: exactly one raw keyframe and one delta on it."""
    v1 = blob(1, 512 * 1024)
    v2 = edit(v1, 2)
    assert store_bundle(conn, sha(v1), v1)[0] == "raw"
    assert store_bundle(conn, sha(v2), v2)[0] == "zstd-prefix"
    return v1, v2


def test_a_delete_without_a_release_row_is_refused_and_with_one_succeeds(tmp_path):
    conn = connect(state_db(tmp_path))
    v1, v2 = keyframe_and_delta(conn)
    conn.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError, match="released"):
        conn.execute(f"DELETE FROM {BUNDLE_STORE} WHERE bundle_sha256=?", (sha(v2),))
    # The SAME delete, after the record exists - in the same transaction.
    release_row(conn, sha(v2))
    assert conn.execute(f"DELETE FROM {BUNDLE_STORE} WHERE bundle_sha256=?",
                        (sha(v2),)).rowcount == 1
    conn.execute("COMMIT")
    # The keyframe is untouched and still restores: the refusal was about the
    # record, not about releases in general.
    assert [row[0] for row in conn.execute(f"SELECT bundle_sha256 FROM {BUNDLE_STORE}")] == [sha(v1)]
    assert load_stored_bundle(conn, sha(v1)) == v1
    conn.close()


def test_a_released_keyframe_whose_delta_survives_fails_the_foreign_key(tmp_path):
    conn = connect(state_db(tmp_path))
    v1, v2 = keyframe_and_delta(conn)
    conn.execute("BEGIN")
    release_row(conn, sha(v1))
    release_row(conn, sha(v2))
    # Both deletes are now permitted by the guard; SQLite itself refuses the one
    # that would strand the delta.
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(f"DELETE FROM {BUNDLE_STORE} WHERE bundle_sha256=?", (sha(v1),))
    assert conn.execute(f"DELETE FROM {BUNDLE_STORE} WHERE bundle_sha256=?",
                        (sha(v2),)).rowcount == 1
    assert conn.execute(f"DELETE FROM {BUNDLE_STORE} WHERE bundle_sha256=?",
                        (sha(v1),)).rowcount == 1
    conn.execute("COMMIT")
    assert conn.execute(f"SELECT count(*) FROM {BUNDLE_STORE}").fetchone()[0] == 0
    conn.close()


def test_the_release_deletes_the_delta_before_its_keyframe(tmp_path):
    conn = connect(state_db(tmp_path))
    v1 = blob(10, 512 * 1024)
    v2 = edit(v1, 11)
    commit1, digest1 = capture(conn, v1)
    commit2, digest2 = capture(conn, v2)
    stored = {row["bundle_sha256"]: row["codec"] for row in conn.execute(
        f"SELECT bundle_sha256,codec FROM {BUNDLE_STORE}")}
    assert stored == {digest1: "raw", digest2: "zstd-prefix"}
    # A State-only database has no host worktree tables; judging it twice on
    # ONE connection must not mistake the first call's own empty stand-in for a
    # projected copy (and must not refuse the second call).
    assert release_plan(conn)["rows"] == 2
    assert release_plan(conn)["rows"] == 2
    recorder = Recorder(conn)
    result = release_bundles(recorder, actor="fixture:release", admin=True)
    assert result["released_rows"] == 2 and result["keyframes"] == 1 and result["deltas"] == 1
    deletes = [(sql, args) for sql, args in recorder.calls
               if sql.lstrip().upper().startswith("DELETE")]
    assert len(deletes) == 2, deletes
    # The ORDER the function actually executed: the delta first, its keyframe second.
    assert [args for _, args in deletes] == [(digest2,), (digest1,)]
    assert conn.execute(f"SELECT count(*) FROM {BUNDLE_STORE}").fetchone()[0] == 0
    released = {row["bundle_sha256"]: dict(row) for row in conn.execute(
        f"SELECT * FROM {RELEASE_TABLE}")}
    assert set(released) == {digest1, digest2}
    assert released[digest1]["codec"] == "raw" and released[digest1]["commit_sha"] == commit1
    assert released[digest2]["base_sha256"] == digest1 and released[digest2]["commit_sha"] == commit2
    assert released[digest2]["payload_bytes"] > 0 and released[digest1]["raw_size"] == len(v1)
    conn.close()


def test_a_release_record_cannot_be_updated_or_deleted(tmp_path):
    conn = connect(state_db(tmp_path))
    v1, _ = keyframe_and_delta(conn)
    release_row(conn, sha(v1))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE {RELEASE_TABLE} SET actor='someone else' WHERE bundle_sha256=?",
                     (sha(v1),))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {RELEASE_TABLE} WHERE bundle_sha256=?", (sha(v1),))
    conn.close()


def test_the_release_needs_an_admin_actor_and_the_rule_this_code_executes(tmp_path):
    conn = connect(state_db(tmp_path))
    capture(conn, blob(20, 256 * 1024))
    with pytest.raises(Exception) as refused:
        release_bundles(conn, actor="fixture:release")
    assert getattr(refused.value, "code", None) == "admin_required", refused.value
    with pytest.raises(StateConflict, match="rule"):
        release_bundles(conn, actor="fixture:release", admin=True, rule="anything-else@2")
    with pytest.raises(StateConflict, match="actor"):
        release_bundles(conn, actor="  ", admin=True)
    # Nothing was written by any of the three refusals.
    assert conn.execute(f"SELECT count(*) FROM {RELEASE_TABLE}").fetchone()[0] == 0
    assert conn.execute(f"SELECT count(*) FROM {BUNDLE_STORE}").fetchone()[0] == 1
    conn.close()


def test_a_pre_migration_row_that_still_holds_its_bytes_is_refused(tmp_path):
    """A release removes the STORE's copy; a row holding its own copy would lie.

    ``retained_bundle_bytes`` prefers a non-empty ``bundle_bytes``, so releasing
    the store row of a not-yet-migrated artifact would leave the candidate
    readable while the ledger said "released". The plan refuses instead.
    """
    conn = connect(state_db(tmp_path))
    v1 = blob(30, 256 * 1024)
    commit, _digest = capture(conn, v1)
    conn.execute("DROP TRIGGER state_git_artifacts_no_update")
    conn.execute("UPDATE state_git_artifacts SET bundle_bytes=? WHERE commit_sha=?", (v1, commit))
    with pytest.raises(StateConflict, match="still carry their own bundle_bytes"):
        release_plan(conn)
    with pytest.raises(StateConflict, match="migrate_bundle_store"):
        release_bundles(conn, actor="fixture:release", admin=True)
    # A refused plan wrote nothing, and the bytes are still held.
    assert conn.execute(f"SELECT count(*) FROM {RELEASE_TABLE}").fetchone()[0] == 0
    assert load_stored_bundle(conn, sha(v1)) == v1
    conn.close()
