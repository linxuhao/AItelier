"""After a release the ledger closes: no hole, no double claim, no stray byte.

Criterion W5 of ``evidence/bundle-store-release-20261120/report.json``. A release
subtracts bytes from the ledger; these are the identities that make that
subtraction checkable afterwards instead of merely asserted:

1. ``state_git_bundle_custody`` reports 0 ``missing`` - every identity is either
   present or released, never neither;
2. no digest is in the store and in the release ledger at once;
3. no store row exists without the identity row that names it;
4. no delta survives without its base keyframe;
5. ``PRAGMA integrity_check`` is ``ok`` and ``PRAGMA foreign_key_check`` is empty;
6. the arithmetic closes - store + releases equals what the store held before -
   for payload bytes AND for raw (logical) bytes;
7. the release records form a chain (each row's after = the next row's before,
   the first before = the store before, the last after = the store today).

The fixture is a real one: a host DBManager file with the real State schema, real
raw keyframes and real zstd deltas written through the product writer, with two
consumer classes planted so the release has to pin a keyframe and hold a delta.
"""
from __future__ import annotations

import hashlib
import random
import sqlite3
from contextlib import contextmanager

import pytest

from core.db_manager import DBManager
from core.state_git_artifacts import (
    BUNDLE_STORE, CUSTODY_VIEW, RELEASE_TABLE, release_bundles, release_plan,
    store_candidate, typed_ref)
from core.state_privacy import PRIVATE_STATE_TABLES
from core.state_service import StateService

PROJECT = "accounting"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def blob(seed, size):
    return random.Random(seed).randbytes(size)


def edit(data, seed, edits=8):
    rng = random.Random(seed)
    out = bytearray(data)
    for _ in range(edits):
        at = rng.randrange(len(out) - 64)
        out[at:at + 16] = rng.randbytes(16)
    return bytes(out)


@contextmanager
def raw(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def node(key):
    return {"key": key, "goal": "Goal " + key,
            "acceptance": [{"id": "a", "kind": "test", "description": "d"}]}


def artifact(conn, data):
    digest = sha(data)
    commit = hashlib.sha1(data).hexdigest()
    store_candidate(conn, {"commit_sha": commit, "tree_sha": "e" * 40, "bundle_sha256": digest,
                           "retained_ref": typed_ref(digest), "bundle_bytes": data})
    return commit, digest


def totals(conn):
    """``(rows, payload_bytes, raw_size)`` of the whole store, and its codecs."""
    row = conn.execute(f"SELECT count(*), coalesce(sum(length(payload)),0), "
                       f"coalesce(sum(raw_size),0) FROM {BUNDLE_STORE}").fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def release_ledger_totals(conn):
    row = conn.execute(f"SELECT count(*), coalesce(sum(payload_bytes),0), "
                       f"coalesce(sum(raw_size),0) FROM {RELEASE_TABLE}").fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def test_a_release_leaves_a_ledger_that_closes(tmp_path):
    path = tmp_path / "state.db"
    service = StateService(DBManager(str(path)), actor="fixture", project_read_trusted=True)
    service.create_project(PROJECT, "Accounting")
    service.store.add_nodes(PROJECT, [node("n")])
    v1 = blob(1, 512 * 1024)
    data = [v1] + [edit(v1, 2 + index) for index in range(5)]
    with raw(path) as conn:
        written = [artifact(conn, value) for value in data]
        codecs = [row[0] for row in conn.execute(
            f"SELECT codec FROM {BUNDLE_STORE} ORDER BY rowid")]
        assert codecs == ["raw"] + ["zstd-prefix"] * 5, codecs
        # Two consumers hold one delta each, so the keyframe they both need is
        # pinned and the ledger has to describe a PARTIAL release.
        commit_delta_blocked, commit_delta_live = written[1][0], written[2][0]
        conn.execute(
            "INSERT INTO state_attempts(attempt_id,project_id,node_key,node_revision,contract_hash,"
            "dependency_snapshot,context_json,request_key,request_hash,workflow,execution_project_id,"
            "status,artifact_ref,created_at,updated_at) "
            "VALUES('attempt-terminal',?,'n',1,'c','{}','{}','rk-terminal','h','dsh','sg-terminal',"
            "'candidate',?,'2026-11-20T00:00:00+00:00','2026-11-20T00:00:00+00:00')",
            (PROJECT, commit_delta_blocked))
        conn.execute(
            "INSERT INTO state_acceptances(receipt_id,project_id,node_key,node_revision,attempt_id,"
            "artifact_ref,contract_hash,dependency_snapshot,evidence_ids,reviewer,created_at) "
            "VALUES('receipt-1',?,'n',1,'attempt-terminal',?,'c','{}','[]','reviewer',"
            "'2026-11-20T00:00:00+00:00')", (PROJECT, commit_delta_blocked))
        conn.execute(
            "INSERT INTO state_attempts(attempt_id,project_id,node_key,node_revision,contract_hash,"
            "dependency_snapshot,context_json,request_key,request_hash,workflow,execution_project_id,"
            "status,artifact_ref,created_at,updated_at) "
            "VALUES('attempt-live',?,'n',1,'c','{}','{}','rk-live','h','dsh','sg-live','running',?,"
            "'2026-11-20T00:00:00+00:00','2026-11-20T00:00:00+00:00')", (PROJECT, commit_delta_live))
        rows_before, payload_before, raw_before = totals(conn)
        plan = release_plan(conn)
        assert [row["bundle_sha256"] for row in plan["pinned"]] == [written[0][1]]
        assert plan["rows"] == 3, plan["digests"]
        # Release in two batches, so the record chain crosses a transaction
        # boundary and the per-batch counters have to agree with each other.
        first = release_bundles(conn, actor="fixture:release", admin=True, limit=2)
        second = release_bundles(conn, actor="fixture:release", admin=True, limit=2)
        assert first["released_rows"] == 2 and second["released_rows"] == 1
        assert first["rows_after"] == second["rows_before"]
        assert first["payload_after"] == second["payload_before"]
        released_rows, released_payload, released_raw = release_ledger_totals(conn)
        rows_after, payload_after, raw_after = totals(conn)
        # 5. the database is structurally sound.
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # 1. no silent loss: every identity is present or released.
        assert conn.execute(f"SELECT count(*) FROM {CUSTODY_VIEW} WHERE custody='missing'"
                            ).fetchone()[0] == 0
        custody = dict(conn.execute(f"SELECT custody, count(*) FROM {CUSTODY_VIEW} GROUP BY custody"))
        artifacts = conn.execute("SELECT count(*) FROM state_git_artifacts").fetchone()[0]
        assert custody == {"present": 3, "released": 3}, custody
        assert sum(custody.values()) == artifacts == 6
        # 2. no double claim: the two sides are a partition.
        assert conn.execute(
            f"SELECT count(*) FROM {BUNDLE_STORE} s WHERE EXISTS "
            f"(SELECT 1 FROM {RELEASE_TABLE} r WHERE r.bundle_sha256=s.bundle_sha256)").fetchone()[0] == 0
        # 3. no bytes without an identity.
        assert conn.execute(
            f"SELECT count(*) FROM {BUNDLE_STORE} s WHERE NOT EXISTS "
            "(SELECT 1 FROM state_git_artifacts a WHERE a.bundle_sha256=s.bundle_sha256)"
        ).fetchone()[0] == 0
        # 4. no dangling delta: every surviving delta's base is still stored.
        assert conn.execute(
            f"SELECT count(*) FROM {BUNDLE_STORE} d WHERE d.codec='zstd-prefix' AND NOT EXISTS "
            f"(SELECT 1 FROM {BUNDLE_STORE} k WHERE k.bundle_sha256=d.base_sha256)"
        ).fetchone()[0] == 0
        # 6. the arithmetic closes, in payload bytes AND in logical bytes.
        assert (rows_after + released_rows, payload_after + released_payload,
                raw_after + released_raw) == (rows_before, payload_before, raw_before)
        # 7. the records chain: after(i) = before(i+1), and today's store is the
        # last record's "after".
        chain = [dict(row) for row in conn.execute(
            f"SELECT * FROM {RELEASE_TABLE} ORDER BY rowid")]
        assert chain[0]["rows_before"] == rows_before
        assert chain[0]["payload_before"] == payload_before
        for current, following in zip(chain, chain[1:]):
            assert (current["rows_after"], current["payload_after"]) == \
                   (following["rows_before"], following["payload_before"])
        assert (chain[-1]["rows_after"], chain[-1]["payload_after"]) == (rows_after, payload_after)
        assert all(row["rule"] == "no-live-consumer@1" and row["actor"] == "fixture:release"
                   for row in chain)
        # The identity rows the release left alone still name their commits.
        surviving = {row[0] for row in conn.execute("SELECT commit_sha FROM state_git_artifacts")}
        assert surviving == {commit for commit, _digest in written}


def test_an_untrusted_connection_cannot_read_the_release_ledger(tmp_path):
    """W9: the ledger is private on the same terms as the bytes it describes.

    The release row names a candidate's digest and commit and says who retired
    it, so an untrusted read would hand out the release history of projects the
    caller cannot open. The custody VIEW is refused for the same reason: SQLite
    expands it into reads of the tables themselves.
    """
    from core.state_commands import ProjectPrivate
    from core.state_graph import StateGraphStore

    path = tmp_path / "state.db"
    service = StateService(DBManager(str(path)), actor="fixture", project_read_trusted=True)
    service.create_project(PROJECT, "Accounting")
    service.open_project(PROJECT)
    with raw(path) as conn:
        _commit, digest = artifact(conn, blob(3, 256 * 1024))
        release_bundles(conn, actor="fixture:release", admin=True)
        assert conn.execute(f"SELECT count(*) FROM {RELEASE_TABLE}").fetchone()[0] == 1
    assert "state_git_bundle_releases" in PRIVATE_STATE_TABLES
    untrusted = StateGraphStore(service.db, project_read_trusted=False)
    for sql in (f"SELECT * FROM {RELEASE_TABLE}",
                f"SELECT * FROM main.{RELEASE_TABLE}",
                f"SELECT * FROM {CUSTODY_VIEW}",
                f"SELECT custody FROM {CUSTODY_VIEW}"):
        with pytest.raises(ProjectPrivate):
            with untrusted.transaction() as conn:
                conn.execute(sql).fetchall()
