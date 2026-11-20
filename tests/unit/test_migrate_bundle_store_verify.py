"""``--verify`` tells a RELEASED digest apart from a missing or corrupt one.

A release retires a digest on purpose and records it in
``state_git_bundle_releases``; the product reader refuses those bytes with a
StateConflict that names the release. A verifier that turned every StateConflict
into a failure therefore failed exactly the database the release path had just
left healthy, and could not distinguish "released" from "gone".

Two arms, one file, both on a real State schema with real stored bytes:

(a) released-counted: two bundles are captured through the product write path,
    their rows emptied the way the migration empties a legacy row, then released
    through ``release_bundles``. ``_verify`` must count both as ``released`` and
    report no failure, and ``main --dry-run --verify`` must exit 0 with the
    released counter in its JSON. The bytes are empty on purpose: a row that
    still carried its own ``bundle_bytes`` would answer from them and never
    reach the release refusal, which would make the arm vacuous.
(b) real-defects-still-fail: a row with NO release record whose bytes do not
    match its ``bundle_sha256``, and a row with no release record whose bytes
    are gone, must both appear in ``verify_failed`` with their rowids and make
    ``main`` return 1 - the fix must not silence a genuine loss.

Helpers are local copies of the shapes in
``tests/unit/test_bundle_store_release_requires_a_record.py`` (no cross-test
imports), and nothing here re-implements the release rule: the release goes
through the release function itself.
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.state_database import StateDatabase
from core.state_git_artifacts import (
    BUNDLE_STORE, RELEASE_TABLE, release_bundles, store_candidate, typed_ref)
from core.state_service import StateService
from tools.migrate_bundle_store import NO_UPDATE, _verify, main

ARTIFACTS = "state_git_artifacts"


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


def write_row_bytes(conn, rowid, value):
    """The migration's own dance: drop the no_update guard, write, restore it.

    ``state_git_artifacts`` is immutable by trigger, so both making a row legacy
    and emptying it (``UPDATE ... SET bundle_bytes=X''``) go through this. The
    guard is restored from its own stored SQL, exactly as the tool does.
    """
    trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                           (NO_UPDATE,)).fetchone()
    assert trigger is not None, "the shipped schema dropped its no_update guard"
    conn.execute(f"DROP TRIGGER {NO_UPDATE}")
    try:
        changed = conn.execute(f"UPDATE {ARTIFACTS} SET bundle_bytes=? WHERE rowid=?",
                               (value, rowid)).rowcount
    finally:
        conn.execute(trigger[0])
    return changed


def rowid_of(conn, commit):
    return conn.execute(f"SELECT rowid FROM {ARTIFACTS} WHERE commit_sha=?", (commit,)).fetchone()[0]


def test_a_released_digest_is_counted_released_not_failed(tmp_path, capsys):
    path = state_db(tmp_path)
    conn = connect(path)
    v1 = blob(1, 512 * 1024)
    v2 = edit(v1, 2)
    commit1, digest1 = capture(conn, v1)
    commit2, digest2 = capture(conn, v2)
    stored = {row["bundle_sha256"]: row["codec"]
              for row in conn.execute(f"SELECT bundle_sha256,codec FROM {BUNDLE_STORE}")}
    assert stored == {digest1: "raw", digest2: "zstd-prefix"}

    # The product write path already keeps the bytes in the store, so the rows
    # start empty. Plant the legacy shape and then empty it with the migration's
    # own statement, so "bundle_bytes is empty at verify time" is measured
    # rather than assumed.
    row1, row2 = rowid_of(conn, commit1), rowid_of(conn, commit2)
    assert write_row_bytes(conn, row1, v1) == 1
    assert write_row_bytes(conn, row2, v2) == 1
    assert write_row_bytes(conn, row1, b"") == 1
    assert write_row_bytes(conn, row2, b"") == 1
    assert [row[0] for row in conn.execute(
        f"SELECT length(bundle_bytes) FROM {ARTIFACTS} ORDER BY rowid")] == [0, 0]

    recorder = Recorder(conn)
    result = release_bundles(recorder, actor="fixture:release", admin=True)
    assert result["released_rows"] == 2 and result["keyframes"] == 1 and result["deltas"] == 1
    assert conn.execute(f"SELECT count(*) FROM {BUNDLE_STORE}").fetchone()[0] == 0
    assert {row["bundle_sha256"] for row in conn.execute(
        f"SELECT bundle_sha256 FROM {RELEASE_TABLE}")} == {digest1, digest2}
    conn.close()

    verify = connect(path)
    ok, released, failed = _verify(verify)
    verify.close()
    assert (ok, released, failed) == (0, 2, [])

    code = main([str(path), "--dry-run", "--verify"])
    summary = json.loads(capsys.readouterr().out)
    assert code == 0, summary
    assert summary["verify_released"] == 2
    assert summary["verify_failed"] == []
    assert summary["verified"] == 0


def test_a_row_with_no_release_record_that_is_wrong_or_gone_still_fails(tmp_path, capsys):
    path = state_db(tmp_path, "state2.db")
    conn = connect(path)
    v1 = blob(10, 256 * 1024)
    commit1, digest1 = capture(conn, v1)
    wrong = rowid_of(conn, commit1)
    # (i) bytes that do not hash to the row's digest, and (ii) a digest the row
    # never held anywhere - neither row has a release record, so both are real
    # defects and neither may be excused as "released".
    assert write_row_bytes(conn, wrong, b"these are not the bytes of this digest") == 1
    gone = sha(b"a bundle State never stored")
    conn.execute(f"INSERT INTO {ARTIFACTS}(commit_sha,tree_sha,bundle_sha256,retained_ref,"
                 "bundle_bytes,created_at) VALUES(?,?,?,?,X'',?)",
                 ("c" * 40, "d" * 40, gone, typed_ref(gone), "2026-11-20T00:00:00+00:00"))
    gone_rowid = rowid_of(conn, "c" * 40)
    assert conn.execute(f"SELECT count(*) FROM {RELEASE_TABLE}").fetchone()[0] == 0
    conn.close()

    verify = connect(path)
    ok, released, failed = _verify(verify)
    verify.close()
    assert released == 0
    assert ok == 0
    assert [entry["rowid"] for entry in failed] == [wrong, gone_rowid]
    assert [entry["commit_sha"] for entry in failed] == [commit1, "c" * 40]
    assert failed[0]["reason"] == "bytes do not match bundle_sha256"
    assert "no bytes" in failed[1]["reason"]

    code = main([str(path), "--dry-run", "--verify"])
    summary = json.loads(capsys.readouterr().out)
    assert code == 1, summary
    assert summary["verify_released"] == 0
    assert [entry["rowid"] for entry in summary["verify_failed"]] == [wrong, gone_rowid]
