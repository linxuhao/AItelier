"""P3 notices land in the P2 per-driver inbox (design/multi-driver-coop.md §5.2).

The notifier's default adapter is the real inbox: every driver-addressed P3
notice becomes one `driver_inbox_messages` row with one delivery to the target,
written on the ownership transaction's connection, and the system marks that
delivery `resolved` when it resolves the notice. Everything here goes through
`execute` (the MCP/REST action surface), not the modules.
"""
from __future__ import annotations

import asyncio
import inspect
import sqlite3

import pytest

from core import driver_notices
from core.drivers import DriverRegistry
from core.state_database import StateDatabase
from core.state_graph import StateGraphStore
from tests.unit.test_state_p3_enforced_claims import (claim, clock, db, external, notices, project,  # noqa: F401
                                                      read, reclaimable, svc, write)


@pytest.fixture(autouse=True)
def _real_inbox():
    driver_notices.set_inbox_adapter()
    yield
    driver_notices.set_inbox_adapter()


def registered(db, *ids):
    registry = DriverRegistry(StateDatabase(str(db)), "inbox-wiring-test-pepper-not-a-secret")
    for who in ids:
        registry.register(who, who.title(), actor="t")
        registry.set_membership("p", who, "member", 0, "member of p", actor="t")


def inbox(service):
    return read(service, "list_driver_messages")["messages"]


def rows(db, sql, *args):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def test_handoff_offer_is_an_inbox_message_the_receiver_acks_and_the_system_resolves(db, clock):
    project(db)
    registered(db, "codex", "grok")
    codex, grok = svc(db, "codex"), svc(db, "grok")
    attempt = external(codex, "a")
    offer = write(codex, "offer_handoff", project_id="p", request_key="h", expected_owner_fence=1,
                  attempt_id=attempt["attempt_id"], to_driver_id="grok",
                  package={"next_step": "take it", "workers": {"quiescent": True}})
    notice = offer["notified"][0]
    delivered = notice["inbox"]
    assert delivered["deliveries"][0]["target_driver_id"] == "grok" and delivered["deliveries"][0]["status"] == "unread"

    messages = inbox(grok)
    assert len(messages) == 1
    message = messages[0]
    assert message["message_id"] == delivered["message_id"]
    assert message["kind"] == "handoff_offer" and message["sender_driver_id"] == "codex"
    assert message["project_id"] == "p" and message["delivery_mode"] == "transient"
    assert message["subject"] == notice["subject"] and message["body"] == notice["body"]
    assert message["status"] == "unread" and message["version"] == 1
    import json
    assert json.loads(message["refs_json"]) == {"attempt_id": attempt["attempt_id"], "node_key": "a"}
    # The waiting read returns it at once, and the sender cannot read it.
    waited = read(grok, "wait_for_driver_inbox", timeout_seconds=0)
    waited = asyncio.run(waited) if inspect.isawaitable(waited) else waited
    assert [m["message_id"] for m in waited["messages"]] == [message["message_id"]]
    assert inbox(codex) == []
    # Idempotency/correlation row: notice_id -> message_id.
    assert rows(db, "SELECT request_key FROM driver_inbox_requests WHERE actor='system' AND operation='send'"
                ) == [{"request_key": notice["notice_id"]}]

    # The receiver acknowledges through the P2 API like any other message...
    ack = write(grok, "acknowledge_driver_message", delivery_id=message["delivery_id"], expected_version=1,
                request_key="ack-1")
    assert ack["status"] == "acknowledged" and ack["version"] == 2
    # ...and accepting the handoff resolves notice AND delivery in one write.
    write(grok, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"], expected_owner_fence=1)
    after = inbox(grok)[0]
    assert after["status"] == "resolved" and after["version"] == 3
    assert notices(grok, kinds=["handoff_offer"], statuses=["resolved"])[0]["notice_id"] == notice["notice_id"]
    assert rows(db, "SELECT operation FROM driver_inbox_requests WHERE actor='system' AND request_key=? ORDER BY operation",
                notice["notice_id"]) == [{"operation": "resolved"}, {"operation": "send"}]
    # A standing read no longer lists it as open.
    assert grok.driver_inbox.active_standing()["matched_total"] == 0


def test_a_system_notice_has_no_sender_and_a_mapped_kind(db, clock):
    project(db, enforce=True)
    registered(db, "codex", "grok", "owner-cli")
    grok, codex, owner = svc(db, "grok"), svc(db, "codex"), svc(db, "owner-cli", admin=True)
    held = claim(grok, workspace="h:/w#g")
    attempt = external(grok, "a", claim_id=held["claim_id"], fence=1)
    reclaimable(clock)
    write(codex, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="grok vanished")
    takeover = [m for m in inbox(grok) if m["kind"] == "takeover_notice"]
    assert len(takeover) == 1 and takeover[0]["sender_driver_id"] is None
    # An admin writing around codex's claim: override_notice has no inbox kind
    # of its own and arrives as lease_notice with the kind in the body.
    write(owner, "revise_node", project_id="p", node_key="a", expected_revision=1, reason="admin edit",
          goal="changed")
    lease = [m for m in inbox(codex) if m["kind"] == "lease_notice"]
    assert lease and lease[-1]["body"].startswith("[break_glass] ") and lease[-1]["sender_driver_id"] is None
    assert [n["kind"] for n in notices(codex, kinds=["break_glass"])] == ["break_glass"]


def test_an_unregistered_target_keeps_the_row_and_event_but_gets_no_inbox_message(db, clock):
    project(db)
    codex = svc(db, "codex")  # neither codex nor grok is in the driver registry
    attempt = external(codex, "a")
    offer = write(codex, "offer_handoff", project_id="p", request_key="h", expected_owner_fence=1,
                  attempt_id=attempt["attempt_id"], to_driver_id="grok",
                  package={"next_step": "x", "workers": {"quiescent": True}})
    assert offer["notified"][0]["inbox"] == {"skipped": "unregistered_driver"}
    assert notices(svc(db, "grok"), kinds=["handoff_offer"])[0]["notice_id"] == offer["notified"][0]["notice_id"]
    assert rows(db, "SELECT COUNT(*) AS n FROM driver_inbox_messages") == [{"n": 0}]


def test_a_rolled_back_ownership_write_leaves_no_inbox_message(db, clock):
    project(db)
    registered(db, "codex", "grok")
    store = StateGraphStore(StateDatabase(str(db)), project_read_trusted=True)
    with pytest.raises(RuntimeError):
        with store.transaction(write=True) as conn:
            stored = driver_notices.notify(conn, store, target_driver_id="grok", kind="lease_notice",
                                           subject="s", body="b", project_id="p", refs={"node_key": "a"})
            assert stored["inbox"]["deliveries"][0]["target_driver_id"] == "grok"
            raise RuntimeError("the ownership write failed after delivery")
    assert rows(db, "SELECT COUNT(*) AS n FROM driver_inbox_messages") == [{"n": 0}]
    assert rows(db, "SELECT COUNT(*) AS n FROM driver_inbox_deliveries") == [{"n": 0}]
    assert rows(db, "SELECT COUNT(*) AS n FROM driver_notices") == [{"n": 0}]
    assert inbox(svc(db, "grok")) == []
