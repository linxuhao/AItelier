"""Owned fixture only: normal retained-bundle admission, no production State."""
import json
import subprocess
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import closed_git, state_git_artifacts
from core.state_graph import StateConflict

from tests.integration.test_state_git_artifact_durability import fixture, git, prune_producer


def test_selected_retained_prior_is_observed_after_materialization(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    service, source, producer, db, prior, commit, tree, payload, bundle = fixture(tmp_path / "owned")
    prune_producer(source, producer, commit)
    fresh = service.start_external_attempt("durable", "producer", 1, "fixture", "fresh", "fresh", base_sha=commit)
    assert git(source, "rev-parse", commit + "^{commit}") == commit
    assert fresh["context"]["base_sha"] == commit
    account = fresh["context"]["prior_candidate"]
    print("OBSERVED_PRIOR_ACCOUNT=" + json.dumps(account, sort_keys=True), flush=True)
    assert account["prior_attempt_id"] == prior["attempt_id"]
    assert account["state"] == "included", "Normal admission imported selected prior before freezing context"


def test_bound_source_callback_is_not_executed_by_retained_import(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    service, source, producer, db, prior, commit, tree, payload, bundle = fixture(tmp_path / "owned")
    prune_producer(source, producer, commit)
    marker = tmp_path / "owned-benign-marker"
    hooks = tmp_path / "owned-hooks"
    hooks.mkdir()
    hook = hooks / "reference-transaction"
    hook.write_text("#!/bin/sh\nprintf '%s\\n' callback >> '" + str(marker) + "'\n")
    hook.chmod(0o700)
    git(source, "config", "core.hooksPath", str(hooks))
    git(source, "update-ref", "refs/owned/positive-control", "HEAD")
    assert marker.read_text(), "Positive control proves this Git invokes the harmless local hook"
    marker.unlink()
    fresh = service.start_external_attempt("durable", "producer", 1, "fixture", "fresh", "fresh", base_sha=commit)
    assert git(source, "rev-parse", commit + "^{commit}") == commit
    assert fresh["status"] == "running"
    print("BENIGN_BOUND_SOURCE_CALLBACK=" + str(marker.exists()), flush=True)
    assert not marker.exists(), "Retained import executed repository-configured callback under host authority"


def test_git_and_repository_lock_do_not_hold_state_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    service, source, producer, db, prior, commit, tree, payload, bundle = fixture(tmp_path / "owned")
    prune_producer(source, producer, commit)
    depth = 0
    seen = []
    transaction = service.store.transaction

    @contextmanager
    def observed_transaction(*args, **kwargs):
        nonlocal depth
        write = bool(kwargs.get("write"))
        with transaction(*args, **kwargs) as conn:
            depth += write
            try:
                yield conn
            finally:
                depth -= write

    monkeypatch.setattr(service.store, "transaction", observed_transaction)
    for owner, name in [(state_git_artifacts, "_git"), (closed_git, "run"), (closed_git, "repo_lock")]:
        original = getattr(owner, name)

        def observed(*args, _original=original, _name=name, **kwargs):
            seen.append(_name)
            assert depth == 0, "Git/locking must not monopolize the State write transaction"
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, observed)
    fresh = service.start_external_attempt("durable", "producer", 1, "fixture", "fresh", "fresh", base_sha=commit)
    assert fresh["context"]["prior_candidate"]["state"] == "included"
    assert {"_git", "run", "repo_lock"}.issubset(seen)
    # Replay neither touches Git nor changes the immutable context or owner.
    seen.clear()
    replay = service.start_external_attempt("durable", "producer", 1, "fixture", "fresh", "fresh", base_sha=commit)
    assert replay["attempt_id"] == fresh["attempt_id"]
    assert replay["context_hash"] == fresh["context_hash"]
    assert seen == []


def test_bare_receiver_keeps_closed_callback_boundary_and_exact_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    service, source, producer, db, prior, commit, tree, payload, bundle = fixture(tmp_path / "owned")
    prune_producer(source, producer, commit)
    receiver = tmp_path / "receiver.git"
    git(tmp_path, "clone", "--bare", "--no-hardlinks", str(source), str(receiver))
    marker = tmp_path / "bare-marker"
    hooks = tmp_path / "bare-hooks"
    hooks.mkdir()
    hook = hooks / "reference-transaction"
    hook.write_text("#!/bin/sh\nprintf '%s\\n' callback >> '" + str(marker) + "'\n")
    hook.chmod(0o700)
    git(receiver, "config", "core.hooksPath", str(hooks))
    git(receiver, "update-ref", "refs/owned/control", "HEAD")
    assert marker.read_text()
    marker.unlink()
    artifact = state_git_artifacts.materialize_candidate(service.store, commit, str(receiver))
    assert artifact["tree_sha"] == tree
    assert subprocess.check_output(["git", "-C", str(receiver), "show", commit + ":value"]) == payload
    assert not marker.exists()


def test_destination_url_rewrite_refuses_before_transport_or_new_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    service, source, producer, db, prior, commit, tree, payload, bundle = fixture(tmp_path / "owned")
    prune_producer(source, producer, commit)
    git(source, "config", "url.file:///owned-unreachable/.insteadOf", "/")
    monkeypatch.setattr(closed_git, "transport", lambda *a, **kw: pytest.fail("rewritten URL reached transport"))
    with pytest.raises(StateConflict, match="destination import refused"):
        service.start_external_attempt("durable", "producer", 1, "fixture", "fresh", "fresh", base_sha=commit)
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM state_external_owners WHERE status='active'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM state_attempts").fetchone()[0] == 1
    assert subprocess.run(["git", "-C", str(source), "cat-file", "-e", commit], capture_output=True).returncode != 0


def test_actual_concurrent_normal_admission_has_one_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    service, source, producer, db, prior, commit, tree, payload, bundle = fixture(tmp_path / "owned")
    prune_producer(source, producer, commit)
    head = git(source, "rev-parse", "HEAD")

    def start(number):
        try:
            return service.start_external_attempt("durable", "producer", 1, "fixture",
                "fresh-" + str(number), "fresh-" + str(number), base_sha=commit)
        except StateConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, [1, 2]))
    accepted = [v for v in results if isinstance(v, dict)]
    assert len(accepted) == 1 and sum(isinstance(v, StateConflict) for v in results) == 1
    assert accepted[0]["context"]["prior_candidate"]["state"] == "included"
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM state_external_owners WHERE status='active'").fetchone()[0] == 1
    assert git(source, "rev-parse", "HEAD") == head
    assert (source / "value").read_text() == "baseline\n"
