"""Retention plans its delta on a READ connection; the write lock sees no CPU work.

The report path used to hold the single global State write lock while it read a
keyframe, zstd-encoded a delta and decoded it to compare (measured 8.0-10.5 s
for a 266 MB bundle). The instrumented tests below assert that the encode, the
decode and the keyframe read all happen in the planning phase, that the write
phase calls none of them, and that a keyframe released or damaged after the
plan is never used as a delta base. The stored row is byte-identical to the
same candidate stored without a plan.
"""
from __future__ import annotations

import hashlib
import random
import sqlite3

import pytest

import core.state_git_artifacts as sga
from core.state_database import StateDatabase
from core.state_git_artifacts import (
    BUNDLE_STORE, load_stored_bundle, plan_candidate, store_bundle, store_candidate, typed_ref)
from core.state_service import StateService
from tests.integration.test_state_git_artifact_durability import fixture
from tests.integration.test_state_git_bundle_store import _shared_history_candidates


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


def state_service(tmp_path, name="state.db"):
    path = tmp_path / name
    service = StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    return path, service


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def artifact_for(raw):
    digest = sha(raw)
    return {"commit_sha": hashlib.sha1(raw).hexdigest(), "tree_sha": "e" * 40,
            "bundle_sha256": digest, "retained_ref": typed_ref(digest), "bundle_bytes": raw}


def _watch(monkeypatch, calls):
    """Name every CPU-heavy call, without changing what it returns."""
    for name in ("encode_delta", "decode_delta", "_store_entry"):
        real = getattr(sga, name)

        def watched(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(sga, name, watched)


def _planned(tmp_path, monkeypatch, name="state.db"):
    path, service = state_service(tmp_path, name)
    v1, v2 = blob(1, 384 * 1024), None
    v2 = edit(v1, 2)
    store_bundle(connect(path), sha(v1), v1)
    calls = []
    _watch(monkeypatch, calls)
    with service.store.transaction() as read_conn:
        plan = plan_candidate(read_conn, artifact_for(v2))
    assert plan.codec == "zstd-prefix" and plan.base_sha256 == sha(v1)
    assert set(calls) >= {"encode_delta", "decode_delta", "_store_entry"}
    return path, service, v1, v2, plan, calls


def test_encode_decode_and_keyframe_read_never_run_in_the_write_transaction(tmp_path, monkeypatch):
    path, service, v1, v2, plan, calls = _planned(tmp_path, monkeypatch)
    calls.clear()   # the proof below is about the write phase alone
    with service.store.transaction(write=True) as conn:
        store_candidate(conn, artifact_for(v2), plan=plan)
    assert calls == []      # instrumented: no keyframe read, encode or decode in the lock
    with sqlite3.connect(path) as raw:
        row = raw.execute(
            "SELECT codec,base_sha256,raw_size,length(payload) AS n FROM state_git_bundle_store "
            "WHERE bundle_sha256=?", (sha(v2),)).fetchone()
        assert tuple(row) == ("zstd-prefix", sha(v1), len(v2), len(plan.payload))
        assert raw.execute("SELECT count(*) FROM state_git_artifacts").fetchone()[0] == 1
    assert load_stored_bundle(connect(path), sha(v2)) == v2


@pytest.mark.parametrize("damage", ["released", "payload-truncated", "raw-size", "codec"])
def test_planned_base_released_or_damaged_before_insert_falls_back_to_raw(tmp_path, monkeypatch, damage):
    path, service, v1, v2, plan, calls = _planned(
        tmp_path, monkeypatch, name=f"state-{damage}.db")
    raw = connect(path)
    raw.execute("DROP TRIGGER IF EXISTS state_git_bundle_store_no_update")
    raw.execute("DROP TRIGGER IF EXISTS state_git_bundle_store_no_delete")
    if damage == "released":
        raw.execute("DELETE FROM state_git_bundle_store WHERE bundle_sha256=?", (sha(v1),))
    elif damage == "payload-truncated":
        raw.execute("UPDATE state_git_bundle_store SET payload=substr(payload,1,length(payload)-5) "
                    "WHERE bundle_sha256=?", (sha(v1),))
    elif damage == "raw-size":
        raw.execute("UPDATE state_git_bundle_store SET raw_size=raw_size+1 WHERE bundle_sha256=?",
                    (sha(v1),))
    else:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("UPDATE state_git_bundle_store SET codec='zstd-prefix',base_sha256=? "
                    "WHERE bundle_sha256=?", ("f" * 64, sha(v1)))
    raw.close()
    calls.clear()
    with service.store.transaction(write=True) as conn:
        store_candidate(conn, artifact_for(v2), plan=plan)
    assert calls == []
    with sqlite3.connect(path) as check:
        rows = {digest: (codec, base, size, bytes(payload)) for digest, codec, base, size, payload in
                check.execute("SELECT bundle_sha256,codec,base_sha256,raw_size,CAST(payload AS BLOB) "
                              "FROM state_git_bundle_store").fetchall()}
        stored = rows[sha(v2)]
        assert stored[0] == "raw" and stored[1] is None and stored[2] == len(v2)
        assert stored[3] == v2
        # No stored row points at the planned keyframe as a delta base.
        assert not [r for r in rows.values() if r[1] == sha(v1)]
        assert check.execute("SELECT count(*) FROM state_git_artifacts "
                             "WHERE bundle_bytes=x''").fetchone()[0] == 1
    # The artifact row has bytes, and the bytes are the exact verified input.
    assert load_stored_bundle(connect(path), sha(v2)) == v2


def test_planned_path_stores_exactly_the_same_row_as_the_unplanned_path(tmp_path, monkeypatch):
    legacy, legacy_service = state_service(tmp_path, "legacy.db")
    planned, planned_service = state_service(tmp_path, "planned.db")
    v1 = blob(3, 256 * 1024)
    v2 = edit(v1, 4)
    artifact = artifact_for(v2)
    for path in (legacy, planned):
        store_bundle(connect(path), sha(v1), v1)
    with connect(legacy) as conn:
        store_candidate(conn, artifact)
    with planned_service.store.transaction() as read_conn:
        plan = plan_candidate(read_conn, artifact)
    with planned_service.store.transaction(write=True) as conn:
        store_candidate(conn, artifact, plan=plan)
    select = (f"SELECT bundle_sha256,codec,base_sha256,raw_size,CAST(payload AS BLOB) "
              f"FROM {BUNDLE_STORE} WHERE bundle_sha256=?")
    with sqlite3.connect(legacy) as a, sqlite3.connect(planned) as b:
        assert a.execute(select, (sha(v2),)).fetchone() == b.execute(select, (sha(v2),)).fetchone()
        for db in (a, b):
            row = db.execute("SELECT commit_sha,tree_sha,bundle_sha256,retained_ref,length(bundle_bytes) "
                             "FROM state_git_artifacts").fetchone()
            assert row == (artifact["commit_sha"], artifact["tree_sha"], sha(v2), typed_ref(sha(v2)), 0)


def test_report_external_attempt_plans_before_its_write_transaction(tmp_path, monkeypatch):
    phase = {"writing": False, "cpu": [], "plans": []}

    def guarded(name, real):
        def wrapped(*args, **kwargs):
            assert not phase["writing"], f"{name} ran inside the State write transaction"
            phase["cpu"].append(name)
            return real(*args, **kwargs)
        return wrapped

    for name in ("encode_delta", "decode_delta"):
        monkeypatch.setattr(sga, name, guarded(name, getattr(sga, name)))

    real_store = sga.store_candidate

    def store(conn, artifact, plan=None):
        assert isinstance(plan, sga.BundlePlan)
        phase["plans"].append(plan.codec)
        phase["writing"] = True
        try:
            return real_store(conn, artifact, plan=plan)
        finally:
            phase["writing"] = False

    monkeypatch.setattr(sga, "store_candidate", store)
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fx")
    _shared_history_candidates(service, source, producer, tmp_path, 2)
    # At least one real candidate took the delta branch, so the instrumented
    # encode/decode calls were executed, not merely skipped on a happy path.
    assert "zstd-prefix" in phase["plans"]
    assert "encode_delta" in phase["cpu"] and "decode_delta" in phase["cpu"]
