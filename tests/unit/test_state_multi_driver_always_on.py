"""multi_driver is always on (owner decision 2026-10-10).

A fresh database and an existing one whose legacy state_project_policy.multi_driver
column says 'off' both behave as multi_driver=on, re-opening changes nothing, and
set_multi_driver is no longer an action on any surface.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.state_commands import READ_REQUESTS, REQUESTS, WRITE_REQUESTS, describe
from core.state_graph import StateGraphError
from tests.unit.test_state_claims import NODE, claim, external, read, svc, write

REPO = Path(__file__).resolve().parents[2]


def snapshot(db):
    conn = sqlite3.connect(str(db))
    try:
        return (sorted(conn.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()),
                conn.execute("SELECT * FROM state_project_policy ORDER BY project_id").fetchall(),
                conn.execute("SELECT * FROM state_project_enforcement ORDER BY project_id").fetchall())
    finally:
        conn.close()


def assert_behaves_on(db):
    grok = svc(db, "grok")
    assert read(grok, "project_overview", project_id="p")["policy"]["multi_driver"] == "on"
    held = claim(grok, "a")
    assert held["status"] == "live" and held["fence"] == 1
    attempt = external(svc(db, "codex"), "b", "rk-b")
    assert attempt["owner_driver_id"] == "codex" and attempt["lease_state"] == "healthy"


def test_a_fresh_database_behaves_as_on_without_any_policy_write(tmp_path):
    db = tmp_path / "fresh.sqlite"
    owner = svc(db, "owner-cli", admin=True)
    owner.create_project("p", "P")
    owner.store.add_nodes("p", [{"key": k, **NODE} for k in ("a", "b")])
    assert snapshot(db)[1] == [], "no policy row is needed"
    assert_behaves_on(db)


def test_an_existing_database_with_legacy_off_behaves_as_on_and_reopening_is_idempotent(tmp_path):
    db = tmp_path / "existing.sqlite"
    owner = svc(db, "owner-cli", admin=True)
    owner.create_project("p", "P")
    owner.store.add_nodes("p", [{"key": k, **NODE} for k in ("a", "b")])
    write(owner, "set_review_independence", project_id="p", review_independence="advisory",
          expected_revision=0, reason="kept")
    conn = sqlite3.connect(str(db))      # what every pre-change project row holds
    conn.execute("UPDATE state_project_policy SET multi_driver='off'")
    conn.commit()
    conn.close()
    before = snapshot(db)
    svc(db, "codex")                      # re-open: schema init runs again
    assert snapshot(db) == before, "re-initialization changes nothing and loses no row"
    assert [row[-1] for row in before[1]] == ["off"], "the legacy column stays, ignored"
    policy = read(owner, "project_overview", project_id="p")["policy"]
    assert policy == {**policy, "multi_driver": "on", "claim_enforcement": "off", "review_independence": "advisory"}
    assert_behaves_on(db)


def test_set_multi_driver_is_no_longer_an_action(tmp_path):
    assert "set_multi_driver" not in REQUESTS
    assert "set_multi_driver" not in WRITE_REQUESTS and "set_multi_driver" not in READ_REQUESTS
    assert "set_multi_driver" not in describe()["operations"]
    assert "set_multi_driver" not in (REPO / "api" / "state_graph_tools.py").read_text(encoding="utf-8")
    owner = svc(tmp_path / "s.sqlite", "owner-cli", admin=True)
    owner.create_project("p", "P")
    with pytest.raises(StateGraphError, match="unknown state graph action"):
        write(owner, "set_multi_driver", project_id="p", multi_driver="on", expected_revision=0, reason="r")
