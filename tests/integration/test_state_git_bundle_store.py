"""Retained Git bundles stored once: raw keyframes plus depth-1 zstd deltas.

The store must restore byte-identical bundles, refuse every defect (payload,
base, codec metadata) with StateConflict, stay immutable, and be migrated from
inline ``bundle_bytes`` rows by ``tools/migrate_bundle_store.py`` without
changing any row's identity. Corrupt rows are made by dropping the immutability
triggers of disposable test databases only.
"""
import hashlib
import importlib.util
import os
import random
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import core.state_git_artifacts as sga
from core.state_attempt_schema import EXTRA_SCHEMA, _statements
from core.state_git_artifacts import (
    custody_metadata, load_stored_bundle, materialize_candidate, retain_candidate, retained_bundle_bytes,
    store_bundle)
from core.state_graph import StateConflict
from core.state_service import StateService
from core.state_database import StateDatabase
from tests.integration.test_state_git_artifact_durability import fixture, prune_producer
from tests.integration.test_state_git_candidate_only_custody import git, make_repo, report_for
from tests.integration.test_state_single_fullpack_custody import report, start

PREFIX = "state-db:git-bundle:sha256:"
ROOT = Path(__file__).resolve().parents[2]


def _tool():
    spec = importlib.util.spec_from_file_location("migrate_bundle_store",
                                                  ROOT / "tools" / "migrate_bundle_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(data):
    return hashlib.sha256(data).hexdigest()


def blob(seed, size):
    return random.Random(seed).randbytes(size)


def edit(data, seed, edits=8):
    """A near-identical successor: a few small in-place changes and an insert."""
    rng = random.Random(seed)
    out = bytearray(data)
    for _ in range(edits):
        at = rng.randrange(len(out) - 64)
        out[at:at + 16] = rng.randbytes(16)
    at = rng.randrange(len(out))
    return bytes(out[:at] + rng.randbytes(300) + out[at:])


def schema_db(path):
    """A database with exactly the product DDL for the artifact and store tables."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    for statement in _statements(EXTRA_SCHEMA):
        if "state_git_" in statement:
            conn.execute(statement)
    return conn


def store_rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT bundle_sha256,codec,base_sha256,raw_size,length(payload) AS n FROM "
        "state_git_bundle_store ORDER BY rowid")]


def tamper(conn, sql, args=()):
    conn.execute("DROP TRIGGER IF EXISTS state_git_bundle_store_no_update")
    conn.execute(sql, args)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))


# ------------------------------------------------------------- the store itself
def test_keyframe_then_depth_one_deltas_round_trip_exactly(tmp_path):
    conn = schema_db(tmp_path / "s.db")
    v1 = blob(1, 2 * 1024 * 1024)
    v2 = edit(v1, 2)
    v3 = edit(v2, 3)
    assert store_bundle(conn, sha(v1), v1)[0] == "raw"
    codec2, size2 = store_bundle(conn, sha(v2), v2)
    codec3, size3 = store_bundle(conn, sha(v3), v3)
    assert codec2 == codec3 == "zstd-prefix"
    assert size2 < len(v2) // 100 and size3 < len(v3) // 100
    rows = {r["bundle_sha256"]: r for r in store_rows(conn)}
    # Depth 1: the third bundle's base is the keyframe, never the second delta.
    assert rows[sha(v2)]["base_sha256"] == sha(v1)
    assert rows[sha(v3)]["base_sha256"] == sha(v1)
    assert rows[sha(v1)]["codec"] == "raw" and rows[sha(v1)]["base_sha256"] is None
    for data in (v1, v2, v3):
        assert load_stored_bundle(conn, sha(data)) == data
    # Insert-once: storing the same bytes again adds nothing.
    assert store_bundle(conn, sha(v3), v3) == ("zstd-prefix", size3)
    assert len(store_rows(conn)) == 3


def test_keyframe_rule_size_band_ratio_and_dependents(tmp_path, monkeypatch):
    conn = schema_db(tmp_path / "s.db")
    big = blob(10, 1024 * 1024)
    store_bundle(conn, sha(big), big)
    # Unrelated bytes of similar size: the delta exceeds 25%, so a new keyframe.
    other = blob(11, 1024 * 1024)
    assert store_bundle(conn, sha(other), other)[0] == "raw"
    # A much smaller bundle never deltas against an out-of-band keyframe.
    small = big[:200 * 1024]
    assert store_bundle(conn, sha(small), small)[0] == "raw"
    # A keyframe with KEYFRAME_MAX_DEPENDENTS deltas takes no more.
    monkeypatch.setattr(sga, "KEYFRAME_MAX_DEPENDENTS", 2)
    successors = [edit(big, 20 + i) for i in range(3)]
    codecs = [store_bundle(conn, sha(s), s)[0] for s in successors]
    assert codecs == ["zstd-prefix", "zstd-prefix", "raw"]
    by_digest = {r["bundle_sha256"]: r for r in store_rows(conn)}
    assert [by_digest[sha(s)]["base_sha256"] for s in successors[:2]] == [sha(big)] * 2
    for data in (big, other, small, *successors):
        assert load_stored_bundle(conn, sha(data)) == data


def test_encoded_row_is_verified_before_insert(tmp_path, monkeypatch):
    conn = schema_db(tmp_path / "s.db")
    v1 = blob(30, 512 * 1024)
    v2 = edit(v1, 31)
    store_bundle(conn, sha(v1), v1)
    real = sga.decode_delta
    monkeypatch.setattr(sga, "decode_delta", lambda base, payload, size: real(base, payload, size)[::-1])
    # A delta that would not restore the exact bytes is never inserted.
    assert store_bundle(conn, sha(v2), v2)[0] == "raw"
    monkeypatch.setattr(sga, "decode_delta", real)
    assert load_stored_bundle(conn, sha(v2)) == v2


def test_bundle_over_the_former_128_mib_cap_is_retained_and_stored(tmp_path):
    repo, _ = make_repo(tmp_path)
    (repo / "big.bin").write_bytes(os.urandom(130 * 1024 * 1024))
    git(repo, "add", "big.bin")
    git(repo, "commit", "-qm", "big")
    candidate = git(repo, "rev-parse", "HEAD")
    bundle = tmp_path / "big.bundle"
    git(repo, "bundle", "create", str(bundle), "HEAD")
    assert bundle.stat().st_size > 128 * 1024 * 1024  # the producer file alone was over the old cap
    artifact = retain_candidate(report_for(bundle), candidate)
    raw = artifact["bundle_bytes"]
    assert 128 * 1024 * 1024 < len(raw) <= sga.MAX_BUNDLE_BYTES
    conn = schema_db(tmp_path / "s.db")
    assert store_bundle(conn, artifact["bundle_sha256"], raw) == ("raw", len(raw))
    assert load_stored_bundle(conn, artifact["bundle_sha256"]) == raw


def test_store_created_under_the_former_cap_is_widened_in_place(tmp_path):
    path = tmp_path / "state.db"
    StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    v1 = blob(70, 256 * 1024)
    v2 = edit(v1, 71)
    store_bundle(conn, sha(v1), v1)
    assert store_bundle(conn, sha(v2), v2)[0] == "zstd-prefix"
    # Make the store what a database created before the cap change holds: the same table, the smaller cap.
    insert = "INSERT INTO state_git_bundle_store VALUES(?,'raw',NULL,?,x'00','2026-11-01T00:00:00')"
    version = conn.execute("PRAGMA schema_version").fetchone()[0]
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute("UPDATE sqlite_schema SET sql=replace(sql,?,?) WHERE type='table' AND name='state_git_bundle_store'",
                 (str(sga.MAX_BUNDLE_BYTES), str(sga.MAX_BUNDLE_BYTES // 2)))
    conn.execute(f"PRAGMA schema_version={version + 1}")
    conn.execute("PRAGMA writable_schema=OFF")
    conn.close()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    with pytest.raises(sqlite3.IntegrityError, match="raw_size"):
        conn.execute(insert, ("a" * 64, sga.MAX_BUNDLE_BYTES))
    rows, shape = store_rows(conn), triggers(path)
    conn.close()
    for _ in range(2):  # the upgrade runs once; a second initialization finds nothing to do
        StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store_rows(conn) == rows
    assert {k: v for k, v in triggers(path).items() if k in shape} == shape
    for data in (v1, v2):
        assert load_stored_bundle(conn, sha(data)) == data
    conn.execute(insert, ("a" * 64, sga.MAX_BUNDLE_BYTES))  # exactly the cap is admitted
    with pytest.raises(sqlite3.IntegrityError, match="raw_size"):
        conn.execute(insert, ("b" * 64, sga.MAX_BUNDLE_BYTES + 1))


def _delta_store(tmp_path):
    conn = schema_db(tmp_path / "s.db")
    v1 = blob(40, 1024 * 1024)
    v2 = edit(v1, 41)
    other = blob(42, 1024 * 1024)
    store_bundle(conn, sha(v1), v1)
    assert store_bundle(conn, sha(v2), v2)[0] == "zstd-prefix"
    assert store_bundle(conn, sha(other), other)[0] == "raw"
    return conn, v1, v2, other


@pytest.mark.parametrize("damage", [
    "delta-payload", "delta-truncated", "delta-trailing", "keyframe-payload",
    "codec-raw", "base-other-keyframe", "base-missing", "base-is-delta", "raw-size"])
def test_every_store_defect_fails_closed(tmp_path, damage):
    conn, v1, v2, other = _delta_store(tmp_path)
    payload = bytes(conn.execute("SELECT payload FROM state_git_bundle_store WHERE bundle_sha256=?",
                                 (sha(v2),)).fetchone()[0])
    target = sha(v2)
    if damage == "delta-payload":
        middle = len(payload) // 2
        tamper(conn, "UPDATE state_git_bundle_store SET payload=? WHERE bundle_sha256=?",
               (payload[:middle] + bytes([payload[middle] ^ 0xFF]) + payload[middle + 1:], target))
    elif damage == "delta-truncated":
        tamper(conn, "UPDATE state_git_bundle_store SET payload=? WHERE bundle_sha256=?",
               (payload[:-5], target))
    elif damage == "delta-trailing":
        tamper(conn, "UPDATE state_git_bundle_store SET payload=? WHERE bundle_sha256=?",
               (payload + b"junk", target))
    elif damage == "keyframe-payload":
        tamper(conn, "UPDATE state_git_bundle_store SET payload=? WHERE bundle_sha256=?",
               (v1[:-1] + b"x", sha(v1)))
    elif damage == "codec-raw":
        tamper(conn, "UPDATE state_git_bundle_store SET codec='raw',base_sha256=NULL "
                     "WHERE bundle_sha256=?", (target,))
    elif damage == "base-other-keyframe":
        tamper(conn, "UPDATE state_git_bundle_store SET base_sha256=? WHERE bundle_sha256=?",
               (sha(other), target))
    elif damage == "base-missing":
        conn.execute("PRAGMA foreign_keys=OFF")
        tamper(conn, "UPDATE state_git_bundle_store SET base_sha256=? WHERE bundle_sha256=?",
               ("f" * 64, target))
    elif damage == "base-is-delta":
        v3 = edit(v1, 43)
        store_bundle(conn, sha(v3), v3)
        tamper(conn, "UPDATE state_git_bundle_store SET base_sha256=? WHERE bundle_sha256=?",
               (sha(v3), target))
    elif damage == "raw-size":
        tamper(conn, "UPDATE state_git_bundle_store SET raw_size=raw_size+1 WHERE bundle_sha256=?",
               (target,))
    with pytest.raises(StateConflict):
        load_stored_bundle(conn, target)
    if damage == "keyframe-payload":
        with pytest.raises(StateConflict):
            load_stored_bundle(conn, sha(v1))
        # A damaged keyframe is never chosen as a new base either.
        v4 = edit(v1, 44)
        assert store_bundle(conn, sha(v4), v4)[0] == "raw"
    # The undamaged keyframe of another family still restores exactly.
    assert load_stored_bundle(conn, sha(other)) == other


def test_store_rows_are_immutable_undeletable_and_depth_one(tmp_path):
    conn, v1, v2, other = _delta_store(tmp_path)
    for sql in ("UPDATE state_git_bundle_store SET payload=x'00'",
                "UPDATE state_git_bundle_store SET created_at='x'"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError, match="retained"):
        conn.execute("DELETE FROM state_git_bundle_store WHERE bundle_sha256=?", (sha(v2),))
    v3 = edit(v1, 45)
    insert = ("INSERT INTO state_git_bundle_store(bundle_sha256,codec,base_sha256,raw_size,payload,"
              "created_at) VALUES(?,?,?,?,?,'t')")
    # A delta whose base is itself a delta (depth 2) is refused by the schema.
    with pytest.raises(sqlite3.IntegrityError, match="raw keyframe"):
        conn.execute(insert, (sha(v3), "zstd-prefix", sha(v2), len(v3), b"x"))
    for args in ((sha(v3), "raw", sha(v1), len(v3), v3),          # raw with a base
                 (sha(v3), "zstd-prefix", None, len(v3), b"x"),   # delta without one
                 (sha(v3), "lz4", None, len(v3), v3),              # unknown codec
                 (sha(v3).upper(), "raw", None, len(v3), v3)):     # not a lowercase digest
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, args)
    assert len(store_rows(conn)) == 3


# --------------------------------------------- real candidates through the State
def _shared_history_candidates(service, source, producer, tmp_path, count):
    """Candidates sharing a large blob, so their normalized bundles are near-identical."""
    (producer / "large.bin").write_bytes(blob(50, 512 * 1024))
    commits = []
    for index in range(count):
        (producer / "value").write_bytes(f"candidate {index}\n".encode())
        git(producer, "add", "value", "large.bin")
        git(producer, "commit", "-qm", f"shared {index}")
        commit = git(producer, "rev-parse", "HEAD")
        bundle = tmp_path / f"shared-{index}.bundle"
        git(producer, "bundle", "create", str(bundle), "HEAD")
        report(service, start(service, ("review", "correction", "producer")[index]), "obs",
               commit, bundle, tmp_path, f"shared-{index}")
        commits.append(commit)
    return commits


def _artifact(db, commit):
    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        return dict(raw.execute("SELECT * FROM state_git_artifacts WHERE commit_sha=?",
                                (commit,)).fetchone())


def test_real_candidates_store_keyframe_and_deltas_and_materialize(tmp_path):
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fx")
    commits = _shared_history_candidates(service, source, producer, tmp_path, 3)
    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        stored = {r["bundle_sha256"]: r for r in store_rows(raw)}
        rows = [dict(r) for r in raw.execute("SELECT * FROM state_git_artifacts")]
        assert all(r["bundle_bytes"] == b"" for r in rows)
        assert {r["bundle_sha256"] for r in rows} == set(stored)
        restored = {r["commit_sha"]: retained_bundle_bytes(raw, r) for r in rows}
    first = _artifact(db, commits[0])
    assert stored[first["bundle_sha256"]]["codec"] == "raw"
    for later in commits[1:]:
        entry = stored[_artifact(db, later)["bundle_sha256"]]
        assert entry["codec"] == "zstd-prefix" and entry["base_sha256"] == first["bundle_sha256"]
        assert entry["n"] < entry["raw_size"] // 10
    for row in rows:
        assert sha(restored[row["commit_sha"]]) == row["bundle_sha256"]
        assert custody_metadata(row)["custody"] == "state_db_fullpack"
    prune_producer(source, producer, commits[-1])
    for target in commits:
        dest = tmp_path / ("dest-" + target[:8] + ".git")
        git(tmp_path, "init", "-q", "--bare", str(dest))
        assert materialize_candidate(service.store, target, str(dest))["commit_sha"] == target
        assert git(dest, "rev-parse", "refs/aitelier/artifacts/" + target + "^{commit}") == target
        git(dest, "fsck", "--strict")


@pytest.mark.parametrize("damage", ["delta-payload", "keyframe-payload", "payload-text", "codec", "base",
                                    "missing"])
def test_materialize_refuses_store_damage_before_destination_mutation(tmp_path, damage):
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fx")
    first, second = _shared_history_candidates(service, source, producer, tmp_path, 2)
    delta = _artifact(db, second)["bundle_sha256"]
    keyframe = _artifact(db, first)["bundle_sha256"]
    with sqlite3.connect(db) as raw:
        if damage == "delta-payload":
            tamper(raw, "UPDATE state_git_bundle_store SET payload=CAST(substr(payload,1,length(payload)-3) "
                        "|| x'000000' AS BLOB) WHERE bundle_sha256=?", (delta,))
        elif damage == "keyframe-payload":
            tamper(raw, "UPDATE state_git_bundle_store SET payload=CAST(substr(payload,1,length(payload)-1) "
                        "|| x'00' AS BLOB) WHERE bundle_sha256=?", (keyframe,))
        elif damage == "payload-text":
            # Damage that also changes the storage class (past the schema CHECK)
            # is refused like any other damage, not raised as a decoding error.
            raw.execute("PRAGMA ignore_check_constraints=ON")
            tamper(raw, "UPDATE state_git_bundle_store SET payload=substr(payload,1,length(payload)-1) "
                        "|| x'ff' WHERE bundle_sha256=?", (keyframe,))
            assert raw.execute("SELECT typeof(payload) FROM state_git_bundle_store WHERE bundle_sha256=?",
                               (keyframe,)).fetchone()[0] == "text"
        elif damage == "codec":
            tamper(raw, "UPDATE state_git_bundle_store SET codec='raw',base_sha256=NULL "
                        "WHERE bundle_sha256=?", (delta,))
        elif damage == "base":
            tamper(raw, "UPDATE state_git_bundle_store SET base_sha256=? WHERE bundle_sha256=?",
                   (_artifact(db, commit)["bundle_sha256"], delta))
        else:
            raw.execute("DROP TRIGGER state_git_bundle_store_no_delete")
            raw.execute("DELETE FROM state_git_bundle_store WHERE bundle_sha256=?", (delta,))
    dest = tmp_path / "dest.git"
    git(tmp_path, "init", "-q", "--bare", str(dest))
    with pytest.raises(StateConflict, match="expected Git artifact.*actual"):
        materialize_candidate(service.store, second, str(dest))
    assert subprocess.check_output(["git", "-C", str(dest), "for-each-ref"]) == b""
    # Metadata stays truthful and unchanged: custody is derived from the row only.
    assert custody_metadata(_artifact(db, second))["custody"] == "state_db_fullpack"


# ------------------------------------------------------------ the migration tool
def legacy_db(path):
    """A pre-store database: every row carries its bytes inline."""
    conn = schema_db(path)
    a = blob(60, 1024 * 1024)
    family = [a, edit(a, 61), edit(a, 62), edit(a, 63)]
    other = blob(64, 300 * 1024)
    rows = []

    def add(commit, data, ref=None, digest=None):
        digest = digest or sha(data)
        conn.execute("INSERT INTO state_git_artifacts VALUES(?,?,?,?,?,?)",
                     (commit, "e" * 40, digest, ref or PREFIX + digest, data, "2026-10-01T00:00:00"))
        rows.append(commit)

    add("1" * 40, family[0])
    add("2" * 40, other)
    add("3" * 40, family[1])
    add("4" * 40, family[2], ref="/legacy/report-store/ab/" + "c" * 64)
    add("5" * 40, family[3])
    add("6" * 40, edit(other, 65), ref="refs/vault/unrecognized")
    add("7" * 40, b"corrupt media", digest=sha(b"something else"))
    add("8" * 40, edit(family[3], 66))
    conn.close()
    return path


def snapshot(path):
    with sqlite3.connect(path) as raw:
        raw.row_factory = sqlite3.Row
        return {r["commit_sha"]: dict(r) for r in raw.execute("SELECT * FROM state_git_artifacts")}


def triggers(path):
    with sqlite3.connect(path) as raw:
        return dict(raw.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'").fetchall())


def quiet(*args, **kwargs):
    pass


def test_migration_dry_run_writes_nothing_and_projects_sizes(tmp_path):
    db = legacy_db(tmp_path / "legacy.db")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    summary = _tool().migrate(str(db), dry_run=True, batch_size=3, log=quiet)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not Path(str(db) + "-wal").exists() or Path(str(db) + "-wal").stat().st_size == 0
    assert summary["migrated"] == 5 and summary["keyframes"] == 2 and summary["deltas"] == 3
    assert summary["skipped"] == {"legacy_not_requested": 1, "unrecognized": 1}
    assert [f["commit_sha"] for f in summary["failed"]] == ["7" * 40]
    assert summary["stored_bytes_added"] < summary["raw_bytes_moved"] // 2
    # Every row the tool would move, including the corrupt one it will refuse.
    assert summary["to_migrate_remaining"] == 6


def test_migration_moves_bytes_exactly_is_idempotent_and_keeps_legacy(tmp_path):
    db = legacy_db(tmp_path / "legacy.db")
    original, original_triggers = snapshot(db), triggers(db)
    tool = _tool()
    summary = tool.migrate(str(db), batch_size=3, verify=True, log=quiet)
    assert summary["migrated"] == 5 and summary["keyframes"] == 2 and summary["deltas"] == 3
    assert [f["commit_sha"] for f in summary["failed"]] == ["7" * 40]
    assert [f["commit_sha"] for f in summary["verify_failed"]] == ["7" * 40]
    assert summary["verified"] == 7 and summary["to_migrate_remaining"] == 1  # the corrupt row
    after = snapshot(db)
    assert triggers(db) == original_triggers
    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        for commit, row in after.items():
            old = original[commit]
            assert {k: v for k, v in row.items() if k != "bundle_bytes"} == \
                   {k: v for k, v in old.items() if k != "bundle_bytes"}
            moved = old["retained_ref"].startswith(PREFIX) and commit != "7" * 40
            assert (row["bundle_bytes"] == b"") is moved
            if moved:
                assert retained_bundle_bytes(raw, row) == old["bundle_bytes"]
                assert custody_metadata(row) == custody_metadata(old)
            else:  # legacy, unrecognized and corrupt rows are untouched
                assert row["bundle_bytes"] == old["bundle_bytes"]
        stored = store_rows(raw)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE state_git_artifacts SET bundle_bytes=x'00' WHERE commit_sha=?", ("1" * 40,))
    # Rerun: nothing to do, nothing changes.
    again = tool.migrate(str(db), batch_size=2, log=quiet)
    assert again["migrated"] == 0 and again["already_migrated"] == 5
    assert snapshot(db) == after and triggers(db) == original_triggers
    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        assert store_rows(raw) == stored
    assert tool.main([str(db), "--dry-run"]) == 1  # the corrupt row is still reported


def test_migration_resumes_after_interruption_and_can_include_legacy(tmp_path, monkeypatch):
    db = legacy_db(tmp_path / "legacy.db")
    original = snapshot(db)
    tool = _tool()
    first = tool.migrate(str(db), batch_size=1, limit=2, log=quiet)
    assert first["migrated"] == 2 and first["to_migrate_remaining"] == 4
    # A failure inside a batch rolls the whole batch back, trigger included.
    real = tool.store_bundle
    monkeypatch.setattr(tool, "store_bundle", lambda *a: (_ for _ in ()).throw(StateConflict("boom")))
    with pytest.raises(StateConflict):
        tool.migrate(str(db), batch_size=8, log=quiet)
    assert "state_git_artifacts_no_update" in triggers(db)
    assert sum(1 for r in snapshot(db).values() if r["bundle_bytes"] == b"") == 2
    monkeypatch.setattr(tool, "store_bundle", real)
    rest = tool.migrate(str(db), batch_size=2, include_legacy=True, verify=True, log=quiet)
    assert rest["migrated"] == 4 and rest["already_migrated"] == 2
    after = snapshot(db)
    legacy = after["4" * 40]
    assert legacy["bundle_bytes"] == b"" and legacy["retained_ref"] == original["4" * 40]["retained_ref"]
    assert custody_metadata(legacy)["custody"] == "legacy_db_plus_cas_path"
    with sqlite3.connect(db) as raw:
        assert retained_bundle_bytes(raw, legacy) == original["4" * 40]["bundle_bytes"]
    assert after["6" * 40]["bundle_bytes"] == original["6" * 40]["bundle_bytes"]  # unrecognized


def test_migrated_backup_only_database_materializes_exact_candidate(tmp_path):
    """A real pre-store database (inline bytes), migrated, then carried alone."""
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fx")
    commits = [commit, *_shared_history_candidates(service, source, producer, tmp_path, 2)]
    with sqlite3.connect(db) as raw:  # rewrite to the pre-store shape
        raw.row_factory = sqlite3.Row
        inline = {r["commit_sha"]: retained_bundle_bytes(raw, r)
                  for r in raw.execute("SELECT * FROM state_git_artifacts")}
        raw.execute("DROP TRIGGER state_git_artifacts_no_update")
        for target, data in inline.items():
            raw.execute("UPDATE state_git_artifacts SET bundle_bytes=? WHERE commit_sha=?", (data, target))
        raw.execute("DROP TRIGGER state_git_bundle_store_no_delete")
        raw.execute("DELETE FROM state_git_bundle_store WHERE codec<>'raw'")
        raw.execute("DELETE FROM state_git_bundle_store")
    StateService(StateDatabase(str(db)), actor="fixture", project_read_trusted=True)  # recreates triggers
    summary = _tool().migrate(str(db), batch_size=2, verify=True, log=quiet)
    assert summary["migrated"] == 3 and not summary["failed"] and not summary["verify_failed"]
    portable = tmp_path / "portable.sqlite"
    with sqlite3.connect(db) as a, sqlite3.connect(portable) as b:
        a.backup(b)
    prune_producer(source, producer, commits[-1])
    shutil.rmtree(tmp_path / "home")
    shutil.rmtree(source)
    receiver = StateService(StateDatabase(str(portable)), actor="fixture", project_read_trusted=True)
    for target in commits:
        dest = tmp_path / ("receiver-" + target[:8] + ".git")
        git(tmp_path, "init", "-q", "--bare", str(dest))
        artifact = materialize_candidate(receiver.store, target, str(dest))
        assert artifact["retained_ref"] == PREFIX + sha(inline[target])
        assert git(dest, "rev-parse", "refs/aitelier/artifacts/" + target + "^{commit}") == target
        git(dest, "fsck", "--strict")
    assert os.path.getsize(portable) > 0
