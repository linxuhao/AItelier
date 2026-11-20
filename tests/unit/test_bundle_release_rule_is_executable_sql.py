"""The release rule is EXECUTABLE SQL, and its version string is pinned to it.

Criterion W7 of ``evidence/bundle-store-release-20261120/report.json``: a test
asserts the SQL that decides releasability still equals the text carrying the
version string ``no-live-consumer@1``, so a semantic edit MUST bump the version
the ledger records. A release row says which rule decided it; if the rule can
change silently, every record already written becomes a false statement about
the decision that was actually taken.

Three teeth, each aimed at a different way the rule can drift:

* the frozen digest of the statement text - ANY edit reds this test, which is
  the point: the edit has to be accompanied by a new ``RELEASE_RULE``;
* the five live classes named individually, so a removal says which one went;
* the statement is really prepared and executed against the shipped schema and
  selects the expected row - a text that no longer parses is not a rule.
"""
from __future__ import annotations

import hashlib
import random
import sqlite3

import pytest

from core.db_manager import DBManager
from core.state_git_artifacts import (
    BUNDLE_STORE, RELEASE_RULE, RELEASE_RULE_SQL, release_plan, store_candidate, typed_ref)
from core.state_service import StateService

# sha256 of RELEASE_RULE_SQL as shipped. Bumping RELEASE_RULE (and therefore the
# meaning the ledger records) is the ONLY reason to update this constant.
RULE_SQL_DIGEST = "104590ac969e515dd96aba7b7a305daa4da8c9da8c56ab426877d778fe5e0c34"

# The consumer classes the rule must decide on, with the tables each one reads.
LIVE_CLASSES = {
    "acceptance receipt": "FROM state_acceptances",
    "in-flight attempt": "FROM state_attempts",
    "protected history link": "FROM state_history_links",
    "retirement of that protection": "FROM state_context_run_protection_retirements",
    "open worktree base": "FROM run_isolation i",
    "base request": "FROM run_isolation_requests",
}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))


def state_db(tmp_path):
    """A host+State database: the shipped schema, host worktree tables included."""
    path = tmp_path / "state.db"
    StateService(DBManager(str(path)), actor="fixture", project_read_trusted=True)
    return path


def test_the_deciding_sql_is_frozen_under_the_rule_it_implements():
    assert RELEASE_RULE == "no-live-consumer@1"
    # The version string travels WITH the statement, so the text and the label
    # cannot be edited apart.
    assert RELEASE_RULE in RELEASE_RULE_SQL
    digest = hashlib.sha256(RELEASE_RULE_SQL.encode()).hexdigest()
    assert digest == RULE_SQL_DIGEST, (
        "the release rule text changed. If the MEANING changed, bump RELEASE_RULE "
        "(the ledger records it on every release row) and this digest together; if "
        "it did not, revert the edit.")


def test_every_live_consumer_class_is_still_a_predicate():
    normalized = " ".join(RELEASE_RULE_SQL.split())
    for consumer, table in LIVE_CLASSES.items():
        assert table in normalized, f"{consumer} no longer reads {table}"
        assert normalized.count("NOT EXISTS") >= 5, "the rule lost a live-class predicate"
    # The five live attempt statuses are named, not implied: a non-terminal
    # attempt can still be relayed, so the set may not shrink silently.
    for status in ("reserved", "launching", "running", "paused", "unknown"):
        assert f"'{status}'" in normalized
    # An honoured base request never blocks, and the rule says so.
    assert "h2.base_sha = q.base_sha" in normalized


def test_the_deciding_sql_prepares_and_selects_on_the_shipped_schema(tmp_path):
    path = state_db(tmp_path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        data = random.Random(7).randbytes(256 * 1024)
        digest = hashlib.sha256(data).hexdigest()
        store_candidate(conn, {"commit_sha": "a" * 40, "tree_sha": "e" * 40,
                               "bundle_sha256": digest, "retained_ref": typed_ref(digest),
                               "bundle_bytes": data})
        rows = [dict(row) for row in conn.execute(RELEASE_RULE_SQL, (0, "-0 days"))]
        assert [row["bundle_sha256"] for row in rows] == [digest]
        assert set(rows[0]) == {"commit_sha", "bundle_sha256", "artifact_created_at", "codec",
                                "base_sha256", "raw_size", "payload_bytes"}
        assert rows[0]["codec"] == "raw" and rows[0]["payload_bytes"] == len(data)
        # The AGE window is part of the statement, not a later filter: the
        # artifact written a moment ago is inside a 30-day window, so the SQL
        # itself selects nothing - and the same row with no window is selected.
        assert conn.execute(RELEASE_RULE_SQL, (30, "-30 days")).fetchall() == []
        assert release_plan(conn, window_days=30)["rows"] == 0
        assert release_plan(conn, window_days=0)["rows"] == 1
    finally:
        conn.close()


class _Recorder:
    """A connection proxy that remembers every statement executed through it."""

    def __init__(self, conn):
        self.conn = conn
        self.statements = []

    def execute(self, sql, parameters=()):
        self.statements.append(sql)
        return self.conn.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.conn, name)


def test_the_pinned_text_is_the_statement_the_planner_executes(tmp_path):
    """Not "a constant exists": THIS text is what decides.

    A pinned copy that the planner no longer runs would pass the digest test and
    decide nothing, which is the failure the criterion is about. So the plan is
    taken through a recording connection and the deciding statement must be
    there, verbatim.
    """
    path = state_db(tmp_path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        data = random.Random(11).randbytes(128 * 1024)
        digest = hashlib.sha256(data).hexdigest()
        store_candidate(conn, {"commit_sha": "b" * 40, "tree_sha": "e" * 40,
                               "bundle_sha256": digest, "retained_ref": typed_ref(digest),
                               "bundle_bytes": data})
        recorder = _Recorder(conn)
        plan = release_plan(recorder)
        assert plan["digests"] == [digest]
    finally:
        conn.close()
    assert RELEASE_RULE_SQL in recorder.statements, (
        "the release planner did not execute RELEASE_RULE_SQL; the pinned text is not "
        "the statement that decides releasability")
