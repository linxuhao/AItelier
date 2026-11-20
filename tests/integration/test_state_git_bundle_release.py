"""The release rule, the readers and the operator tool, on a real State database.

Criteria W3, W4, W6 and W10 of ``evidence/bundle-store-release-20261120/report.json``:

* W3 - each of the five LIVE consumer classes blocks a release ON ITS OWN, and
  removing that one consumer makes the same row releasable;
* W4 - a base request the project already honoured never blocks, a still-pending
  one does;
* W6 - every digest that survives restores to exactly its own digest through the
  product reader, and a released digest raises the released refusal (never a
  silent empty read, never the "legacy candidates need re-attestation" message);
* W10 - the operator tool with no argument writes nothing and reports the rows,
  the bytes, the digests and the pinned keyframes it would release - and that
  projection is what ``--apply`` then does.

The database is a scratch one (a host DBManager file plus the real State schema),
so the host's own ``run_isolation`` tables are the shipped ones. Artifact bytes
are written through the product writer (``store_candidate``), so the store rows
are real raw keyframes and real zstd deltas.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.db_manager import DBManager
from core.state_git_artifacts import (
    BUNDLE_STORE, RELEASE_TABLE, bundle_release, custody_metadata, load_stored_bundle,
    materialize_candidate, release_bundles, release_plan, store_candidate, typed_ref)
from core.state_graph import StateConflict
from core.state_service import StateService

PROJECT = "release"
NODE = "n"
ROOT = Path(__file__).resolve().parents[2]


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


def node(key):
    return {"key": key, "goal": "Goal " + key,
            "acceptance": [{"id": "a", "kind": "test", "description": "d"}]}


@contextmanager
def raw(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def fixture(tmp_path):
    """A scratch host+State database with one project and two nodes."""
    path = tmp_path / "state.db"
    db = DBManager(str(path))
    service = StateService(db, actor="fixture", project_read_trusted=True)
    service.create_project(PROJECT, "Release")
    service.store.add_nodes(PROJECT, [node(NODE), node("m")])
    return service, path


def artifact(conn, data, tree="e" * 40):
    """Write one artifact through the product writer; returns (commit, digest, codec)."""
    digest = sha(data)
    commit = hashlib.sha1(data).hexdigest()
    store_candidate(conn, {"commit_sha": commit, "tree_sha": tree, "bundle_sha256": digest,
                           "retained_ref": typed_ref(digest), "bundle_bytes": data})
    codec = conn.execute(f"SELECT codec FROM {BUNDLE_STORE} WHERE bundle_sha256=?",
                         (digest,)).fetchone()[0]
    return commit, digest, codec


def family(path, count=1):
    """One raw keyframe plus ``count`` zstd deltas on it; returns the (data, commit, digest)."""
    v1 = blob(1, 512 * 1024)
    data = [v1] + [edit(v1, 2 + index) for index in range(count)]
    written = []
    with raw(path) as conn:
        for index, value in enumerate(data):
            commit, digest, codec = artifact(conn, value)
            assert codec == ("raw" if index == 0 else "zstd-prefix"), (index, codec)
            written.append((value, commit, digest))
    return written


# --------------------------------------------------------------- the consumers
def plant(path, consumer, commit, *, project=PROJECT):
    """Write the ONE row that makes ``commit`` live for ``consumer``."""
    with raw(path) as conn:
        if consumer == "acceptance":
            conn.execute(
                "INSERT INTO state_attempts(attempt_id,project_id,node_key,node_revision,contract_hash,"
                "dependency_snapshot,context_json,request_key,request_hash,workflow,"
                "execution_project_id,status,artifact_ref,created_at,updated_at) "
                "VALUES('attempt-accept',?,'n',1,'c','{}','{}','rk-accept','h','dsh','sg-accept',"
                "'candidate',?,'2026-11-20T00:00:00+00:00','2026-11-20T00:00:00+00:00')",
                (project, commit))
            conn.execute(
                "INSERT INTO state_acceptances(receipt_id,project_id,node_key,node_revision,attempt_id,"
                "artifact_ref,contract_hash,dependency_snapshot,evidence_ids,reviewer,created_at) "
                "VALUES('receipt-1',?,'n',1,'attempt-accept',?,'c','{}','[]','reviewer',"
                "'2026-11-20T00:00:00+00:00')", (project, commit))
        elif consumer == "live_attempt":
            conn.execute(
                "INSERT INTO state_attempts(attempt_id,project_id,node_key,node_revision,contract_hash,"
                "dependency_snapshot,context_json,request_key,request_hash,workflow,"
                "execution_project_id,status,artifact_ref,created_at,updated_at) "
                "VALUES('attempt-live',?,'m',1,'c','{}','{}','rk-live','h','dsh','sg-live','running',?,"
                "'2026-11-20T00:00:00+00:00','2026-11-20T00:00:00+00:00')", (project, commit))
        elif consumer == "protected":
            conn.execute(
                "INSERT INTO state_history_links(project_id,reference_id,node_key,kind,ref,label,"
                "observed_status,artifact_ref,report_sha256,provenance_actor,recorded_by,node_revision,"
                "protection,payload_hash,created_at) "
                "VALUES(?,'reference-1','n','commit',?,'pinned','completed',?,NULL,'producer','fixture',1,1,"
                "'hash','2026-11-20T00:00:00+00:00')", (project, commit, commit))
        elif consumer == "isolation_open":
            conn.execute("INSERT INTO run_isolation(run_id,project_id,config_name,mode,base_sha,released_at) "
                         "VALUES('run-open',?,'dpe','worktree',?,NULL)", (project, commit))
        elif consumer == "request_pending":
            conn.execute("INSERT INTO run_isolation_requests(project_id,base_sha) VALUES('pending',?)",
                         (commit,))
        else:  # pragma: no cover - the parametrisation names every class
            raise AssertionError(consumer)


def unplant(path, consumer):
    """Remove the ONE consumer, the way the owner would - nothing else changes."""
    with raw(path) as conn:
        statements = {
            # The receipt table is append-only by trigger; this is a disposable
            # fixture, so the guard is dropped for the one delete that removes
            # the consumer under measurement.
            "acceptance": [("DROP TRIGGER state_acceptance_no_delete", ()),
                           ("DELETE FROM state_acceptances WHERE receipt_id='receipt-1'", ())],
            "live_attempt": ("UPDATE state_attempts SET status='failed' WHERE attempt_id='attempt-live'", ()),
            # The owner's REAL mechanism for lifting a protection: an audited,
            # append-only retirement row. The link itself stays pinned history.
            "protected": (
                "INSERT INTO state_context_run_protection_retirements(project_id,reference_id,"
                "node_key,node_revision,original_payload_hash,run_id,foreign_project_id,"
                "foreign_attempt_id,foreign_execution_project_id,request_key,request_hash,actor,"
                "reason,created_at) VALUES(?,'reference-1','n',1,'hash','run-foreign','other-project',"
                "'attempt-foreign','sg-foreign','rk-retire','rh','fixture:owner','retired',"
                "'2026-11-20T00:00:00+00:00')", (PROJECT,)),
            "isolation_open": ("UPDATE run_isolation SET released_at='2026-11-20T01:00:00+00:00' "
                               "WHERE run_id='run-open'", ()),
            # The request is CONSUMED: the project now has a worktree at exactly
            # that base (and it has since been released), so the request can
            # never take effect again.
            "request_pending": (
                "INSERT INTO run_isolation(run_id,project_id,config_name,mode,base_sha,released_at) "
                "VALUES('run-honoured','pending','dpe','worktree',"
                "(SELECT base_sha FROM run_isolation_requests WHERE project_id='pending'),"
                "'2026-11-20T15:00:00+00:00')", ()),
        }[consumer]
        if isinstance(statements, tuple):
            statements = [statements]
        for sql, params in statements:
            conn.execute(sql, params)


CONSUMERS = ("acceptance", "live_attempt", "protected", "isolation_open", "request_pending")


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_each_live_consumer_class_blocks_the_release_on_its_own(tmp_path, consumer):
    _service, path = fixture(tmp_path)
    value, commit, digest = family(path, count=0)[0]
    with raw(path) as conn:
        plan = release_plan(conn)
        assert plan["digests"] == [digest], plan          # positive control: releasable
        plant(path, consumer, commit)
        blocked = release_plan(conn)
        assert blocked["rows"] == 0 and blocked["digests"] == [], (consumer, blocked)
        # The store still holds the bytes, and the tool would not touch them.
        out = release_bundles(conn, actor="fixture:release", admin=True)
        assert out["released_rows"] == 0
        assert load_stored_bundle(conn, digest) == value
        row = dict(conn.execute("SELECT * FROM state_git_artifacts WHERE commit_sha=?",
                                (commit,)).fetchone())
        assert bundle_release(conn, digest) is None
        assert custody_metadata(row)["custody"] == "state_db_fullpack"
    unplant(path, consumer)
    with raw(path) as conn:
        # Removing that ONE consumer makes the SAME row releasable, and the
        # release then really takes the bytes.
        reopened = release_plan(conn)
        assert reopened["digests"] == [digest], (consumer, reopened["pinned"])
        out = release_bundles(conn, actor="fixture:release", admin=True)
        assert out["released_rows"] == 1 and out["released_payload_bytes"] > 0
        assert conn.execute(f"SELECT count(*) FROM {BUNDLE_STORE}").fetchone()[0] == 0
        assert bundle_release(conn, digest) is not None


def test_an_honoured_base_request_does_not_block_but_a_pending_one_does(tmp_path):
    _service, path = fixture(tmp_path)
    (v1, commit1, digest1), (v2, commit2, digest2) = family(path, count=1)
    with raw(path) as conn:
        # Honour the request for digest1's commit: the project already has a
        # worktree at exactly that base, so the request can never apply again.
        conn.execute("INSERT INTO run_isolation_requests(project_id,base_sha) VALUES('honoured',?)",
                     (commit1,))
        conn.execute("INSERT INTO run_isolation(run_id,project_id,config_name,mode,base_sha,released_at) "
                     "VALUES('run-1','honoured','dpe','worktree',?,'2026-11-20T00:00:00+00:00')", (commit1,))
        # A request that still has no worktree at its base: it must block.
        conn.execute("INSERT INTO run_isolation_requests(project_id,base_sha) VALUES('pending',?)",
                     (commit2,))
        plan = release_plan(conn)
        # The keyframe survives (its delta is blocked), and the delta is held by
        # the pending request.
        assert commit2 not in {row["commit_sha"] for row in plan["selected"]}
        assert digest1 not in plan["digests"] and digest2 not in plan["digests"]
        # The honoured request alone does not keep the row: drop the pending one.
        conn.execute("DELETE FROM run_isolation_requests WHERE project_id='pending'")
        released = release_plan(conn)
        assert released["rows"] == 2 and set(released["digests"]) == {digest1, digest2}
        # Highest-water proof: the honoured request row is still there and
        # still names commit1.
        assert conn.execute("SELECT base_sha FROM run_isolation_requests WHERE project_id='honoured'"
                            ).fetchone()[0] == commit1


def test_every_surviving_digest_restores_and_a_released_one_is_refused(tmp_path):
    service, path = fixture(tmp_path)
    (v1, commit1, digest1), (v2, commit2, digest2), (v3, commit3, digest3), \
        (v4, commit4, digest4), (v5, commit5, digest5) = family(path, count=4)
    with raw(path) as conn:
        # Two deltas stay live, so the keyframe they need is pinned; the other
        # two deltas are releasable.
        plant(path, "acceptance", commit2)
        plant(path, "live_attempt", commit3)
        plan = release_plan(conn)
        assert [row["bundle_sha256"] for row in plan["pinned"]] == [digest1]
        assert set(plan["digests"]) == {digest4, digest5}
        out = release_bundles(conn, actor="fixture:release", admin=True)
        assert out["released_rows"] == 2 and out["keyframes"] == 0 and out["deltas"] == 2
        # Every SURVIVING digest restores to exactly its own digest.
        for value, _commit, digest in ((v1, commit1, digest1), (v2, commit2, digest2),
                                       (v3, commit3, digest3)):
            assert sha(load_stored_bundle(conn, digest)) == digest
        # Every released digest raises the RELEASED refusal - not a silent empty
        # read and not the "no bytes for this digest" message.
        for value, _commit, digest in ((v4, commit4, digest4), (v5, commit5, digest5)):
            with pytest.raises(StateConflict) as refused:
                load_stored_bundle(conn, digest)
            assert "was released" in str(refused.value) and "fixture:release" in str(refused.value)
            assert "no bytes for this digest" not in str(refused.value)
            record = bundle_release(conn, digest)
            assert record["rule"] == "no-live-consumer@1" and record["payload_bytes"] > 0
        # The reader-side custody answer is 'released' WITH the record, and the
        # unchanged identity row still names the commit.
        row = dict(conn.execute("SELECT * FROM state_git_artifacts WHERE commit_sha=?",
                                (commit4,)).fetchone())
        assert row["commit_sha"] == commit4 and row["bundle_bytes"] == b""
        assert custody_metadata(row)["custody"] == "state_db_fullpack"
        released_metadata = custody_metadata(row, bundle_release(conn, digest4))
        assert released_metadata["custody"] == "released"
        assert released_metadata["released"]["actor"] == "fixture:release"
        # A RELEASED commit is never reported as a missing legacy bundle.
        destination = tmp_path / "dest.git"
        with pytest.raises(StateConflict) as refused:
            materialize_candidate(service.store, commit4, str(destination))
        assert "was released" in str(refused.value)
        assert "re-attestation" not in str(refused.value)


def _tool():
    spec = importlib.util.spec_from_file_location("release_bundle_store",
                                                  ROOT / "tools" / "release_bundle_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_release_tool_with_no_argument_writes_nothing_and_reports_the_counts(tmp_path, capsys):
    _service, path = fixture(tmp_path)
    (v1, commit1, digest1), (v2, commit2, digest2), (v3, commit3, digest3) = family(path, count=2)
    with raw(path) as conn:
        plant(path, "acceptance", commit1)
    before = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    tool = _tool()
    assert tool.main([str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    # Nothing was written: the file is byte-identical, no WAL was left behind,
    # and neither the store nor the ledger moved.
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == before
    wal = Path(str(path) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    with raw(path) as conn:
        assert conn.execute(f"SELECT count(*) FROM {BUNDLE_STORE}").fetchone()[0] == 3
        assert conn.execute(f"SELECT count(*) FROM {RELEASE_TABLE}").fetchone()[0] == 0
    assert summary["dry_run"] is True and summary["released_rows"] == 0
    # What it reports is exactly what --apply then does.
    assert summary["rows"] == 2 and set(summary["digests"]) == {digest2, digest3}
    assert summary["payload_bytes"] == sum(conn_payload(path, digest) for digest in (digest2, digest3))
    assert [row["bundle_sha256"] for row in summary["pinned"]] == []
    held = sum(conn_payload(path, digest) for digest in (digest1, digest2, digest3))
    applied = tool.release(str(path), actor="fixture:release", apply=True)
    assert applied["released_rows"] == summary["rows"]
    assert applied["released_payload_bytes"] == summary["payload_bytes"]
    assert applied["released_raw_size"] == summary["raw_size"]
    assert applied["digests"] == summary["digests"]
    # The ledger's own counters, surfaced for the operator: what the store held
    # before the batch and what it holds after it.
    assert applied["store_rows_before"] == 3 and applied["store_rows_after"] == 1
    assert applied["store_payload_before"] == held
    assert applied["store_payload_after"] == conn_payload(path, digest1)
    with raw(path) as conn:
        assert conn.execute(f"SELECT count(*) FROM {BUNDLE_STORE}").fetchone()[0] == 1


def conn_payload(path, digest):
    with raw(path) as conn:
        return conn.execute(f"SELECT length(payload) FROM {BUNDLE_STORE} WHERE bundle_sha256=?",
                            (digest,)).fetchone()[0]


def test_the_tool_refuses_a_database_whose_guard_was_never_narrowed(tmp_path):
    _service, path = fixture(tmp_path)
    family(path)
    with raw(path) as conn:
        # The pre-release shape: the OLD unconditional guard. Widening it here
        # would be the tool editing its own permission; it must refuse instead.
        conn.execute("DROP TRIGGER state_git_bundle_store_no_delete")
        conn.execute("CREATE TRIGGER state_git_bundle_store_no_delete BEFORE DELETE ON "
                     "state_git_bundle_store BEGIN SELECT RAISE(ABORT,'retained Git bundle bytes "
                     "are retained'); END")
    with pytest.raises(SystemExit) as refused:
        _tool().release(str(path))
    assert "state_attempt_schema" in str(refused.value)
