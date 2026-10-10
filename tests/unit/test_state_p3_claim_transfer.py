"""Multi-driver P3: moving an attempt to a driver that already holds the node.

After the old claim's lease and grace lapse, the taker can claim_node the node
(acquisition sweeps the expired claim). A later take_over_attempt or
accept_handoff must then bind that live claim to the attempt with a fresh
fence instead of inserting a second live exclusive claim (which the unique
index refuses, rolling the whole ownership move back). Independent
verification p3-slim-verify-codex, criteria 3 and 4.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.unit.test_state_p3_enforced_claims import (GRACE, LEASE, claim, clock, code_of, db, external,  # noqa: F401
                                                      project, read, report, svc, write)


def live_exclusive(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT claim_id, driver_id, fence, attempt_id FROM state_node_claims WHERE project_id='p' "
                            "AND node_key='a' AND status='live' AND purpose IN ('implement','plan')").fetchall()
    finally:
        conn.close()


def max_fence(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT MAX(fence) FROM state_node_claims WHERE project_id='p' AND node_key='a'").fetchone()[0]
    finally:
        conn.close()


def move(new, operation, attempt, offer):
    if operation == "takeover":
        return write(new, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
                     reason="lease and grace elapsed")
    out = write(new, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"], expected_owner_fence=1)
    return {**out["attempt"], "claim": out["claim"]}


def setup_attempt(db, operation):
    project(db, enforce=True)
    old, new = svc(db, "codex"), svc(db, "grok")
    held = claim(old, workspace="h:/old#main")
    attempt = external(old, claim_id=held["claim_id"], fence=held["fence"])
    offer = None
    if operation == "handoff":
        offer = write(old, "offer_handoff", project_id="p", request_key="offer", expected_owner_fence=1,
                      attempt_id=attempt["attempt_id"], to_driver_id="grok",
                      package={"workers": {"quiescent": True}, "next_step": "continue"})
    return old, new, held, attempt, offer


def assert_old_owner_fenced_out(old, new, attempt):
    beat = write(old, "heartbeat", project_id="p", attempts=[{"attempt_id": attempt["attempt_id"], "fence": 1}])
    assert beat["refused"][0]["error"] == "not_attempt_owner"
    assert code_of(lambda: report(old, attempt, "running", fence=1)) == "stale_fence"
    assert code_of(lambda: report(new, attempt, "running", fence=1)) == "stale_fence"
    assert report(new, attempt, "running", fence=2)["observation"]["fence"] == 2


@pytest.mark.parametrize("operation", ["takeover", "handoff"])
def test_transfer_binds_the_receivers_own_live_claim(db, clock, operation):
    old, new, held, attempt, offer = setup_attempt(db, operation)
    clock.advance(LEASE + GRACE + 1)
    mine = claim(new, workspace="h:/new#main")
    assert mine["status"] == "live" and mine["fence"] == 2
    assert read(new, "get_claim", project_id="p", claim_id=held["claim_id"])["claim"]["status"] == "expired"

    moved = move(new, operation, attempt, offer)

    assert moved["owner_driver_id"] == "grok" and moved["owner_fence"] == 2
    # The receiver's claim is reused: one live exclusive claim, bound, fence bumped.
    assert moved["claim"]["claim_id"] == mine["claim_id"] and moved["claim"]["driver_id"] == "grok"
    assert moved["claim"]["fence"] == 3 == max_fence(db)
    assert moved["claim"]["workspace"] == "h:/new#main"
    assert live_exclusive(db) == [(mine["claim_id"], "grok", 3, attempt["attempt_id"])]
    got = read(new, "get_attempt", attempt_id=attempt["attempt_id"])
    assert got["owner_driver_id"] == "grok" and got["owner_fence"] == 2
    assert_old_owner_fenced_out(old, new, attempt)
    # The bound claim is still the receiver's to release, with its new fence.
    assert code_of(lambda: write(new, "release_claim", project_id="p", claim_id=mine["claim_id"], fence=2,
                                 reason="r")) == "stale_fence"
    assert write(new, "release_claim", project_id="p", claim_id=mine["claim_id"], fence=3,
                 reason="done")["status"] == "released"


@pytest.mark.parametrize("operation", ["takeover", "handoff"])
def test_transfer_without_a_receiver_claim_creates_one(db, clock, operation):
    old, new, held, attempt, offer = setup_attempt(db, operation)
    clock.advance(LEASE + GRACE + 1)

    moved = move(new, operation, attempt, offer)

    assert moved["owner_driver_id"] == "grok" and moved["owner_fence"] == 2
    assert moved["claim"]["claim_id"] != held["claim_id"] and moved["claim"]["driver_id"] == "grok"
    assert moved["claim"]["fence"] == 2 == max_fence(db) and moved["claim"]["workspace"] == "h:/old#main"
    assert live_exclusive(db) == [(moved["claim"]["claim_id"], "grok", 2, attempt["attempt_id"])]
    assert read(new, "get_claim", project_id="p", claim_id=held["claim_id"])["claim"]["status"] == "transferred"
    assert_old_owner_fenced_out(old, new, attempt)
