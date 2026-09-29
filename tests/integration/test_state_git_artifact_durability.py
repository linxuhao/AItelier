"""Real Git pruning and public State dispatch, with no model or production DB."""
import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.state_database import StateDatabase
from core.state_graph import StateConflict
from core.state_service import StateService


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def node(key):
    return {"key": key, "goal": "Durable " + key, "acceptance": [
        {"id": "exact", "kind": "test", "description": "Exact bytes survive"}]}


def fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    source, producer = root / "source", root / "producer"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.name", "fixture")
    git(source, "config", "user.email", "fixture@localhost")
    (source / "value").write_text("baseline\n")
    git(source, "add", "value")
    git(source, "commit", "-qm", "baseline")
    baseline = git(source, "rev-parse", "HEAD")
    git(source, "worktree", "add", "--detach", str(producer), baseline)
    payload = b"exact candidate bytes\n\x00\xff"
    (producer / "value").write_bytes(payload)
    git(producer, "add", "value")
    git(producer, "commit", "-qm", "external candidate")
    commit, tree = git(producer, "rev-parse", "HEAD"), git(producer, "rev-parse", "HEAD^{tree}")
    bundle = root / "candidate.bundle"
    git(producer, "bundle", "create", str(bundle), "HEAD")
    db_path = root / "state.sqlite"
    service = StateService(StateDatabase(str(db_path)), actor="fixture", project_read_trusted=True)
    service.create_project("durable", "Durability")
    service.store.add_nodes("durable", [node("producer"), node("review"), node("correction")])
    service.bind_source("durable", str(source))
    attempt = service.start_external_attempt("durable", "producer", 1, "fixture", "produce", "produce")
    report = root / "report.json"
    body = json.dumps({"status": "candidate", "settled": True, "usable": True, "artifact": commit,
        "git_bundle": {"path": str(bundle), "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}}).encode()
    report.write_bytes(body)
    service.report_external_attempt(attempt["attempt_id"], "done", 0, attempt["context_hash"], "candidate",
        str(report), hashlib.sha256(body).hexdigest(), True, commit, "git-sha1")
    return service, source, producer, db_path, attempt, commit, tree, payload, bundle


def prune_producer(source, producer, commit):
    git(source, "worktree", "remove", str(producer))
    git(source, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
    git(source, "gc", "--prune=now")
    absent = subprocess.run(["git", "-C", str(source), "cat-file", "-e", commit], capture_output=True)
    assert absent.returncode != 0, "test must actually prune the original candidate"


def test_fresh_repo_recovers_exact_bytes_from_portable_state_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home-a"))
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "host-a")
    identity = service.get_attempt(attempt["attempt_id"])["git_artifact"]
    assert identity["commit_sha"] == commit and identity["tree_sha"] == tree
    failure = tmp_path / "review-failure.json"
    failed_bytes = json.dumps({"status": "completed", "settled": True, "usable": True,
        "criterion_id": "exact", "verdict": "fail", "artifact": commit}).encode()
    failure.write_bytes(failed_bytes)
    service.attempts.record_evidence(attempt["attempt_id"], "review-failed", "exact", "fail",
        commit, str(failure), hashlib.sha256(failed_bytes).hexdigest(), "independent-reviewer")
    retraction = tmp_path / "retraction.json"
    retract_bytes = b'{"status":"failed","settled":true,"usable":true}'
    retraction.write_bytes(retract_bytes)
    service.report_external_attempt(attempt["attempt_id"], "retract", 1, attempt["context_hash"],
        "failed", str(retraction), hashlib.sha256(retract_bytes).hexdigest(), True)
    assert service.get_attempt(attempt["attempt_id"])["artifact_ref"] == commit
    prune_producer(source, producer, commit)
    bundle.unlink()
    shutil.rmtree(tmp_path / "home-a")  # DB alone carries retained bundle bytes.
    shutil.rmtree(source)
    fresh = tmp_path / "host-b-repo"
    fresh.mkdir()
    git(fresh, "init", "-q")
    portable = tmp_path / "host-b.sqlite"
    with sqlite3.connect(db) as a, sqlite3.connect(portable) as b:
        a.backup(b)
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home-b"))
    successor = StateService(StateDatabase(str(portable)), actor="fixture", project_read_trusted=True)
    successor.create_project("receiver", "Fresh host receiver")
    successor.store.add_nodes("receiver", [node("review"), node("correction")])
    successor.bind_source("receiver", str(fresh))
    review = successor.start_external_attempt("receiver", "review", 1, "fixture", "review", "review", base_sha=commit)
    assert review["status"] == "running" and review["context"]["base_sha"] == commit
    assert git(fresh, "rev-parse", commit + "^{tree}") == tree
    assert subprocess.check_output(["git", "-C", str(fresh), "show", commit + ":value"]) == payload
    assert not (fresh / "value").exists()  # producer was never merged into main.
    git(fresh, "gc", "--prune=now")
    correction = successor.start_external_attempt("receiver", "correction", 1, "fixture", "correction", "correction", base_sha=commit)
    assert correction["context"]["base_sha"] == commit
    with successor.store.transaction(write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="retained"):
            conn.execute("DELETE FROM state_git_artifacts WHERE commit_sha=?", (commit,))


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_unavailable_candidate_refuses_before_any_external_reservation(tmp_path, monkeypatch, damage):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "fixture")
    prune_producer(source, producer, commit)
    # Simulate external backup/media damage, bypassing immutability deliberately.
    with service.store.transaction(write=True) as conn:
        if damage == "missing":
            conn.execute("DROP TRIGGER state_git_artifacts_no_delete")
            conn.execute("DELETE FROM state_git_artifacts")
        else:
            conn.execute("DROP TRIGGER state_git_artifacts_no_update")
            conn.execute("UPDATE state_git_artifacts SET bundle_bytes=?", (b"corrupted media",))
        before = conn.execute("SELECT count(*) FROM state_attempts").fetchone()[0]
    refs_before = git(source, "show-ref")
    with pytest.raises(StateConflict, match="expected Git artifact.*actual"):
        service.start_external_attempt("durable", "review", 1, "fixture", "review", "review", base_sha=commit)
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM state_attempts").fetchone()[0] == before
        assert conn.execute("SELECT count(*) FROM state_external_owners WHERE status='active'").fetchone()[0] == 0
    assert git(source, "show-ref") == refs_before
    assert not producer.exists()
    event = service.store.events("durable")[-1]
    assert event["event_type"] == "artifact_preflight_failed"
    assert event["payload"]["expected"] == commit and "actual" in event["payload"]["actual"]


def test_skillflow_base_refusal_retires_reservation_before_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    service, source, producer, db, old, commit, tree, payload, bundle = fixture(tmp_path / "fixture")
    with service.store.transaction(write=True) as conn:
        conn.execute("DROP TRIGGER state_git_artifacts_no_delete")
        conn.execute("DELETE FROM state_git_artifacts")
    reserved = service.attempts.reserve("durable", "review", 1, "fixture", "review", base_sha=commit)
    service.sf = SimpleNamespace(list_runs=lambda **kw: [])
    monkeypatch.setattr("core.run_isolation.request_base", lambda *args, **kw: pytest.fail("no base admission before bundle validation"))
    refused = service._launch_or_recover(reserved, SimpleNamespace(), str(source))
    assert refused["status"] == "failed" and refused["run_id"] is None
    assert "expected Git artifact" in refused["error"]


@pytest.mark.parametrize("damage", ["undeclared", "hash", "incomplete", "wrong-commit"])
def test_candidate_intake_requires_exact_self_contained_bundle(tmp_path, monkeypatch, damage):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    service, source, producer, db, old, commit, tree, payload, bundle = fixture(tmp_path / "fixture")
    attempt = service.start_external_attempt("durable", "review", 1, "fixture", "review", "review")
    if damage == "incomplete":
        bundle.unlink()
        git(producer, "bundle", "create", str(bundle), "HEAD", "^" + git(source, "rev-parse", "HEAD"))
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    body = {"status": "candidate", "settled": True, "usable": True,
        "git_bundle": {"path": str(bundle), "sha256": "0" * 64 if damage == "hash" else digest}}
    if damage == "undeclared":
        del body["git_bundle"]
    report = tmp_path / "bad-candidate.json"
    raw = json.dumps(body).encode()
    report.write_bytes(raw)
    with pytest.raises(StateConflict):
        service.report_external_attempt(attempt["attempt_id"], "bad", 0, attempt["context_hash"], "candidate",
            str(report), hashlib.sha256(raw).hexdigest(), True,
            "f" * 40 if damage == "wrong-commit" else commit, "git-sha1")
    current = service.get_attempt(attempt["attempt_id"])
    assert current["artifact_ref"] is None and current["observation_version"] == 0


@pytest.mark.parametrize("refusal", ["stale-revision", "active-owner"])
def test_refused_external_registration_does_not_import_candidate(tmp_path, monkeypatch, refusal):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    service, source, producer, db, old, commit, tree, payload, bundle = fixture(tmp_path / "fixture")
    prune_producer(source, producer, commit)
    if refusal == "active-owner":
        service.start_external_attempt("durable", "review", 1, "fixture", "active", "active")
    refs = git(source, "show-ref")
    with pytest.raises(StateConflict, match="revision changed|active attempt"):
        service.start_external_attempt("durable", "review", 2 if refusal == "stale-revision" else 1,
            "fixture", "new", "new", base_sha=commit)
    assert git(source, "show-ref") == refs
    assert subprocess.run(["git", "-C", str(source), "cat-file", "-e", commit], capture_output=True).returncode != 0


def test_accepted_observation_and_registration_retries_need_no_producer_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    service, source, producer, db, old, commit, tree, payload, bundle = fixture(tmp_path / "fixture")
    report = db.parent / "report.json"
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
    prune_producer(source, producer, commit)
    bundle.unlink()
    report.unlink()
    repeated = service.report_external_attempt(old["attempt_id"], "done", 0, old["context_hash"], "candidate",
        str(report), report_sha, True, commit, "git-sha1")
    assert repeated["idempotent"] and repeated["artifact_ref"] == commit
    registration = service.start_external_attempt("durable", "review", 1, "fixture", "review", "review", base_sha=commit)
    with service.store.transaction(write=True) as conn:
        conn.execute("DROP TRIGGER state_git_artifacts_no_delete")
        conn.execute("DELETE FROM state_git_artifacts")
    refs = git(source, "show-ref")
    retry = service.start_external_attempt("durable", "review", 1, "fixture", "review", "review", base_sha=commit)
    assert retry == registration and git(source, "show-ref") == refs
