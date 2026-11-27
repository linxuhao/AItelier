"""The retention's write-lock hold is logged once, after COMMIT, excluding planning.

The fix that moved keyframe read / delta encode / decode out of the single State
write transaction was guarded only by unit tests. These witnesses drive the real
report path (``StateService.report_external_attempt``) twice with distinguishable
plan-phase and transaction-phase durations and read the line the operator would
read in production: one record per retention with the stored digest, raw_size,
plan kind and the measured hold, INFO below the 5 s budget warning and WARNING
at it. The band is the one a waiting writer can survive, not the quiet-host
hold: the measured hold includes the commit's fsync, which the code does not size.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import core.state_external as se
import core.state_git_artifacts as sga
from tests.integration.test_state_git_artifact_durability import fixture, git
from tests.integration.test_state_single_fullpack_custody import start


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def candidate(producer, tmp_path, name):
    """One real Git commit and its self-contained bundle from the producer worktree."""
    (producer / "large.bin").write_bytes(random.Random(7).randbytes(512 * 1024))
    (producer / "value").write_bytes((name + "\n").encode())
    git(producer, "add", "value", "large.bin")
    git(producer, "commit", "-qm", name)
    commit = git(producer, "rev-parse", "HEAD")
    bundle = tmp_path / (name + ".bundle")
    git(producer, "bundle", "create", str(bundle), "HEAD")
    return commit, bundle


def submit(service, commit, bundle, tmp_path, name, node):
    body = json.dumps({"status": "candidate", "settled": True, "usable": True, "artifact": commit,
                       "git_bundle": {"path": str(bundle), "sha256": sha(bundle.read_bytes())}}).encode()
    report = tmp_path / (name + ".json")
    report.write_bytes(body)
    attempt = start(service, node)
    return service.report_external_attempt(attempt["attempt_id"], "obs", 0, attempt["context_hash"],
                                           "candidate", str(report), sha(body), True, commit, "git-sha1")


def stored(db, commit):
    """The digest the artifact row stores and the codec/raw_size of its payload."""
    with sqlite3.connect(db) as raw:
        digest = raw.execute("SELECT bundle_sha256 FROM state_git_artifacts WHERE commit_sha=?",
                             (commit,)).fetchone()[0]
        codec, size = raw.execute("SELECT codec,raw_size FROM state_git_bundle_store "
                                  "WHERE bundle_sha256=?", (digest,)).fetchone()
    return digest, size, codec


def hold_of(message):
    match = re.search(r"\bhold_ms=([0-9.]+)", message)
    assert match, message
    return float(match.group(1))


@pytest.mark.parametrize("hold_s,level", [(2.5, logging.INFO), (6.25, logging.WARNING)])
def test_one_line_per_retention_carries_the_four_fields_and_the_level(
        tmp_path, monkeypatch, caplog, hold_s, level):
    assert se.BUNDLE_HOLD_WARN_SEC == 5.0
    service, source, producer, db, _attempt, _commit, _tree, _payload, _bundle = fixture(tmp_path / "fx")
    # A comparable-size keyframe, so the witnessed retention takes the delta branch.
    base_commit, base_bundle = candidate(producer, tmp_path, "base")
    submit(service, base_commit, base_bundle, tmp_path, "base", "review")
    commit, bundle = candidate(producer, tmp_path, "watched")

    # Two clock reads: immediately before the write transaction and in finally,
    # after it committed. No sleeps: the level is forced at the warn boundary.
    clock = iter((100.0, 100.0 + hold_s))
    monkeypatch.setattr(se, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=se.logger.name):
        submit(service, commit, bundle, tmp_path, "watched", "correction")

    records = [r for r in caplog.records if r.name == se.logger.name]
    assert len(records) == 1, [r.getMessage() for r in records]
    record = records[0]
    assert record.levelno == level
    digest, size, codec = stored(db, commit)
    assert codec == "zstd-prefix"   # the delta half really ran, not a keyframe fallback
    message = record.getMessage()
    assert digest in message
    assert f"raw_size={size}" in message
    assert f"plan=delta" in message and f"codec={codec}" in message
    assert hold_of(message) == pytest.approx(hold_s * 1000.0, abs=1.0)


class EmitWatcher(logging.Handler):
    """Observe the connection state at the instant the record is emitted."""

    def __init__(self, open_transactions, connections):
        super().__init__()
        self.open_transactions = open_transactions
        self.connections = connections
        self.seen = []

    def emit(self, record):
        live = []
        for conn in self.connections:
            try:
                live.append(bool(conn.in_transaction))
            except sqlite3.ProgrammingError:
                pass   # the transaction's connection is already closed
        self.seen.append((record.getMessage(), len(self.open_transactions), any(live)))


def test_the_logged_hold_is_only_the_write_transaction_and_emits_after_commit(tmp_path, monkeypatch):
    service, source, producer, db, _attempt, _commit, _tree, _payload, _bundle = fixture(tmp_path / "fx")
    base_commit, base_bundle = candidate(producer, tmp_path, "base")
    submit(service, base_commit, base_bundle, tmp_path, "base", "review")
    commit, bundle = candidate(producer, tmp_path, "watched")

    plan_seconds, write_seconds = 0.25, 0.05
    planned = []

    def slow_plan(conn, artifact):
        planned.append(artifact["bundle_sha256"])
        time.sleep(plan_seconds)
        return real_plan(conn, artifact)

    def slow_store(conn, artifact, plan=None):
        time.sleep(write_seconds)
        return real_store(conn, artifact, plan=plan)

    real_plan, real_store = sga.plan_candidate, sga.store_candidate
    monkeypatch.setattr(sga, "plan_candidate", slow_plan)
    monkeypatch.setattr(sga, "store_candidate", slow_store)

    open_transactions, connections = [], []

    @contextmanager
    def watched_transaction(*args, **kwargs):
        with real_transaction(*args, **kwargs) as conn:
            connections.append(conn)
            open_transactions.append(conn)
            try:
                yield conn
            finally:
                open_transactions.pop()

    real_transaction = service.store.transaction
    monkeypatch.setattr(service.store, "transaction", watched_transaction)
    watcher = EmitWatcher(open_transactions, connections)
    previous_level = se.logger.level
    se.logger.setLevel(logging.INFO)
    se.logger.addHandler(watcher)
    try:
        begun = time.monotonic()
        submit(service, commit, bundle, tmp_path, "watched", "correction")
        elapsed_ms = (time.monotonic() - begun) * 1000.0
    finally:
        se.logger.removeHandler(watcher)
        se.logger.setLevel(previous_level)

    assert planned                         # the slow plan phase really ran
    assert len(watcher.seen) == 1, watcher.seen
    message, open_count, in_transaction = watcher.seen[0]
    assert open_count == 0 and not in_transaction
    hold_ms = hold_of(message)
    assert hold_ms >= write_seconds * 1000.0 * 0.8, watcher.seen
    # The 250 ms plan sleep happened before the clock started: it cannot appear
    # in the reported hold, even though the whole report took longer.
    assert hold_ms < elapsed_ms - plan_seconds * 1000.0 * 0.8, (hold_ms, elapsed_ms)
    digest, size, codec = stored(db, commit)
    assert f"raw_size={size}" in message and digest in message and codec in message
