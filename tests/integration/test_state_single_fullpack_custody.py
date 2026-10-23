"""Single full-pack custody: real intake, get_attempt matrix, DB-only handoff.

Future external Git candidates keep their normalized pack only in the State DB
(typed ``state-db:git-bundle:sha256:<digest>`` reference); legacy rows keep
their bytes and path. Corrupt rows are made by dropping the immutability
triggers of a disposable test database only, never of a production database.
"""
import hashlib
import json
import os
import sqlite3
import shutil
import subprocess

import pytest

import core.state_git_artifacts as sga
from core import datadir
from core.state_commands import ProjectPrivate, execute
from core.state_database import StateDatabase
from core.state_git_artifacts import git_custody, materialize_candidate
from core.state_graph import StateConflict
from core.state_service import StateService
from tests.integration.test_state_git_artifact_durability import fixture, prune_producer
from tests.integration.test_state_git_candidate_only_custody import (
    broad_bundle, git, has_object, set_unrelated)

PREFIX = "state-db:git-bundle:sha256:"
META_KEYS = {"commit_sha", "tree_sha", "bundle_sha256", "retained_ref", "custody", "legacy_cas_path"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def store_files():
    root = datadir.state_report_store_dir()
    if not root.exists():
        return {}
    return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def rows(db):
    with sqlite3.connect(db) as raw:
        raw.row_factory = sqlite3.Row
        return [dict(r) for r in raw.execute("SELECT * FROM state_git_artifacts ORDER BY commit_sha")]


def corrupt(db, sql, args=(), trigger="state_git_artifacts_no_update"):
    """Disposable-database corruption fixture: impossible rows need the trigger gone."""
    with sqlite3.connect(db) as raw:
        raw.execute("DROP TRIGGER IF EXISTS " + trigger)
        raw.execute(sql, args)


def second_candidate(source, producer, tmp_path, name="second"):
    (producer / "value").write_bytes((name + " candidate\n").encode())
    git(producer, "add", "value")
    git(producer, "commit", "-qm", name)
    commit = git(producer, "rev-parse", "HEAD")
    unrelated_commit, unrelated_blob = set_unrelated(source, os.urandom(256 * 1024), name="u-" + name)
    return commit, broad_bundle(producer, tmp_path / (name + ".bundle")), unrelated_commit, unrelated_blob


def report(service, attempt, obs, commit, bundle, tmp_path, name):
    body = json.dumps({"status": "candidate", "settled": True, "usable": True, "artifact": commit,
                       "note": name, "git_bundle": {"path": str(bundle),
                                                    "sha256": sha(bundle.read_bytes())}}).encode()
    path = tmp_path / (name + ".json")
    path.write_bytes(body)
    return service.report_external_attempt(attempt["attempt_id"], obs, 0, attempt["context_hash"],
                                           "candidate", str(path), sha(body), True, commit, "git-sha1"), sha(body)


def start(service, node="review"):
    return service.start_external_attempt("durable", node, 1, "fixture", node, node)


def read(service, attempt):
    return execute(service, "get_attempt", {"attempt_id": attempt["attempt_id"]})


@pytest.mark.parametrize("value,custody,path", [
    (PREFIX + "a" * 64, "state_db_fullpack", None),
    ("/abs/report-store/ab/" + "a" * 64, "legacy_db_plus_cas_path", "/abs/report-store/ab/" + "a" * 64),
    ("refs/vault/canary", "unrecognized", None),
])
def test_custody_classifier_accepts_the_three_shapes(value, custody, path):
    result = git_custody(value)
    assert result[0] == custody and result[1] == path


@pytest.mark.parametrize("value", [
    PREFIX + "A" * 64, PREFIX + "a" * 63, PREFIX + "a" * 65, "state-db:other:sha256:" + "a" * 64,
    PREFIX + "a" * 64 + "x", "state-db:", "state-db:git-bundle:sha1:" + "a" * 64])
def test_malformed_reserved_prefix_fails_closed_without_echo(value):
    with pytest.raises(StateConflict, match="malformed state-db custody reference") as raised:
        git_custody(value)
    assert value not in str(raised.value)


def test_new_intake_stores_one_pack_and_writes_no_second_bundle_file(tmp_path):
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fx")
    before = store_files()  # the first intake already ran
    commit2, broad, unrelated_commit, unrelated_blob = second_candidate(source, producer, tmp_path)
    new_attempt = start(service)
    _, report_sha = report(service, new_attempt, "obs", commit2, broad, tmp_path, "r2")
    after = store_files()
    added = {k: v for k, v in after.items() if k not in before}
    assert [os.path.basename(k) for k in added] == [report_sha]  # report JSON only
    row = next(r for r in rows(db) if r["commit_sha"] == commit2)
    stored = bytes(row["bundle_bytes"])
    assert not any(stored in data or data.startswith(b"# v2 git bundle") for data in after.values())
    # The reference is truthful: digest of stored bytes == column == reference; not a path.
    assert sha(stored) == row["bundle_sha256"]
    assert row["retained_ref"] == PREFIX + row["bundle_sha256"]
    assert not os.path.isabs(row["retained_ref"]) and not os.path.exists(row["retained_ref"])
    meta = read(service, new_attempt)["git_artifact"]
    assert set(meta) == META_KEYS
    assert meta["custody"] == "state_db_fullpack" and meta["legacy_cas_path"] is None
    dest = tmp_path / "dest.git"
    git(tmp_path, "init", "-q", "--bare", str(dest))
    materialize_candidate(service.store, commit2, str(dest))
    assert git(dest, "rev-parse", "refs/aitelier/artifacts/" + commit2 + "^{commit}") == commit2
    assert not has_object(dest, unrelated_blob) and not has_object(dest, unrelated_commit)
    git(dest, "fsck", "--strict")


def test_legacy_row_is_reattested_without_rewrite_or_bundle_file(tmp_path):
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fx")
    legacy = "/legacy/report-store/ab/" + "c" * 64
    corrupt(db, "UPDATE state_git_artifacts SET retained_ref=?", (legacy,))
    old = rows(db)[0]
    before = store_files()
    again = start(service)
    # Same commit, a fresh report; the producer bundle is still present.
    _, report_sha = report(service, again, "obs", commit, bundle, tmp_path, "reattest")
    assert rows(db) == [old]
    assert old["retained_ref"] == legacy
    added = [os.path.basename(k) for k in store_files() if k not in before]
    assert added == [report_sha]
    meta = read(service, again)["git_artifact"]
    assert meta["custody"] == "legacy_db_plus_cas_path" and meta["legacy_cas_path"] == legacy
    assert meta["retained_ref"] == legacy
    # The CAS file is never consulted: bytes still materialize.
    dest = tmp_path / "legacy-dest.git"
    git(tmp_path, "init", "-q", "--bare", str(dest))
    assert materialize_candidate(service.store, commit, str(dest))["tree_sha"] == tree


def test_get_attempt_matrix_trusted_public_and_private(tmp_path):
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fx")
    public = StateService(StateDatabase(str(db)), actor="anyone", project_read_trusted=False)
    trusted = read(service, attempt)["git_artifact"]
    assert set(trusted) == META_KEYS and trusted["custody"] == "state_db_fullpack"
    with pytest.raises(ProjectPrivate):  # project not opened
        read(public, attempt)
    with public.store.transaction() as conn:
        assert conn.execute("SELECT * FROM state_git_artifacts").fetchall() == []
    service.open_project("durable")
    opened = read(public, attempt)["git_artifact"]
    assert opened == trusted and "bundle_bytes" not in opened
    # Legacy row: same derived view for trusted and opened-public callers.
    legacy = "/legacy/report-store/ab/" + "d" * 64
    corrupt(db, "UPDATE state_git_artifacts SET retained_ref=?", (legacy,))
    for caller in (service, public):
        meta = read(caller, attempt)["git_artifact"]
        assert set(meta) == META_KEYS
        assert meta["custody"] == "legacy_db_plus_cas_path" and meta["legacy_cas_path"] == legacy


def test_malformed_mismatched_and_altered_references_fail_closed(tmp_path):
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fx")
    original = rows(db)[0]
    dest = tmp_path / "dest.git"
    git(tmp_path, "init", "-q", "--bare", str(dest))
    refs_before = subprocess.check_output(["git", "-C", str(dest), "for-each-ref"])
    cases = [("malformed", "UPDATE state_git_artifacts SET retained_ref=?", (PREFIX + "A" * 64,), True),
             ("other-digest", "UPDATE state_git_artifacts SET retained_ref=?", (PREFIX + "b" * 64,), True),
             ("altered-bytes", "UPDATE state_git_artifacts SET bundle_bytes=?",
              (original["bundle_bytes"][:-1] + b"x",), False)]
    for name, sql, args, metadata_refused in cases:
        corrupt(db, "UPDATE state_git_artifacts SET retained_ref=?,bundle_bytes=?",
                (original["retained_ref"], original["bundle_bytes"]))
        corrupt(db, sql, args)
        if metadata_refused:
            with pytest.raises(StateConflict):
                read(service, attempt)
        with pytest.raises(StateConflict):
            materialize_candidate(service.store, commit, str(dest))
        assert subprocess.check_output(["git", "-C", str(dest), "for-each-ref"]) == refs_before, name


def test_missing_or_corrupt_blob_is_refused(tmp_path):
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fx")
    dest = tmp_path / "dest.git"
    git(tmp_path, "init", "-q", "--bare", str(dest))
    corrupt(db, "UPDATE state_git_artifacts SET bundle_bytes=?", (b"corrupt",))
    with pytest.raises(StateConflict, match="expected Git artifact"):
        materialize_candidate(service.store, commit, str(dest))
    corrupt(db, "DELETE FROM state_git_artifacts", trigger="state_git_artifacts_no_delete")
    with pytest.raises(StateConflict, match="durable bundle missing"):
        materialize_candidate(service.store, commit, str(dest))


@pytest.mark.parametrize("damage", ["empty", "digest"])
def test_rejected_candidate_rolls_back_and_valid_retry_succeeds(tmp_path, monkeypatch, damage):
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fx")
    commit2, broad, _, _ = second_candidate(source, producer, tmp_path)
    new_attempt = start(service)
    real = sga.retain_candidate

    def damaged(report_bytes, target):
        value = real(report_bytes, target)
        return {**value, "bundle_bytes": b""} if damage == "empty" else {**value, "bundle_sha256": "0" * 64}

    def counts():
        with sqlite3.connect(db) as raw:
            return tuple(raw.execute(q).fetchone()[0] for q in (
                "SELECT count(*) FROM state_git_artifacts",
                "SELECT count(*) FROM state_external_observations",
                "SELECT count(*) FROM state_external_report_blobs",
                "SELECT observation_version FROM state_attempts WHERE attempt_id='%s'" % new_attempt["attempt_id"]))

    before_counts, before_files = counts(), store_files()
    monkeypatch.setattr(sga, "retain_candidate", damaged)
    with pytest.raises(StateConflict):
        report(service, new_attempt, "obs", commit2, broad, tmp_path, "r2")
    assert counts() == before_counts
    added = [v for k, v in store_files().items() if k not in before_files]
    assert len(added) <= 1 and not any(v.startswith(b"# v2 git bundle") for v in added)  # report JSON only
    monkeypatch.setattr(sga, "retain_candidate", real)
    result, _ = report(service, new_attempt, "obs", commit2, broad, tmp_path, "r2")
    assert result["status"] == "candidate"
    assert counts()[0] == before_counts[0] + 1
    dest = tmp_path / "dest.git"
    git(tmp_path, "init", "-q", "--bare", str(dest))
    assert materialize_candidate(service.store, commit2, str(dest))["commit_sha"] == commit2


def test_backup_only_database_materializes_exact_candidate(tmp_path):
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fx")
    commit2, broad, unrelated_commit, unrelated_blob = second_candidate(source, producer, tmp_path)
    report(service, start(service), "obs", commit2, broad, tmp_path, "r2")
    tree2 = git(producer, "rev-parse", commit2 + "^{tree}")
    portable = tmp_path / "portable.sqlite"
    with sqlite3.connect(db) as a, sqlite3.connect(portable) as b:
        a.backup(b)
    prune_producer(source, producer, commit2)
    broad.unlink()
    bundle.unlink()
    shutil.rmtree(tmp_path / "home")
    shutil.rmtree(source)
    receiver = StateService(StateDatabase(str(portable)), actor="fixture", project_read_trusted=True)
    dest = tmp_path / "receiver.git"
    git(tmp_path, "init", "-q", "--bare", str(dest))
    artifact = materialize_candidate(receiver.store, commit2, str(dest))
    assert artifact["retained_ref"].startswith(PREFIX)
    assert git(dest, "rev-parse", "refs/aitelier/artifacts/" + commit2 + "^{commit}") == commit2
    assert git(dest, "rev-parse", "refs/aitelier/artifacts/" + commit2 + "^{tree}") == tree2
    assert not has_object(dest, unrelated_blob) and not has_object(dest, unrelated_commit)
    git(dest, "fsck", "--strict")
    # A tampered typed digest in the portable copy is refused.
    corrupt(portable, "UPDATE state_git_artifacts SET retained_ref=? WHERE commit_sha=?",
            (PREFIX + "e" * 64, commit2))
    fresh = tmp_path / "tampered.git"
    git(tmp_path, "init", "-q", "--bare", str(fresh))
    with pytest.raises(StateConflict):
        materialize_candidate(receiver.store, commit2, str(fresh))
