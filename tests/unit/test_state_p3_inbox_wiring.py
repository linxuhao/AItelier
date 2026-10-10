"""P3 notices land in the P2 per-driver inbox (design/multi-driver-coop.md §5.2).

The thin notifier (core/driver_notices.py) writes every driver-addressed P3
notice as one `driver_inbox_messages` row with one delivery to the target, on
the ownership transaction's connection. There is no second copy: no pending
notice table and no notice event. Everything except the rollback check goes
through `execute` (the MCP/REST action surface), not the modules.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3

import pytest

from core import driver_notices
from core.state_database import StateDatabase
from core.state_graph import StateGraphStore
from tests.unit.test_state_p3_enforced_claims import (claim, clock, db, events, external, project,  # noqa: F401
                                                      read, reclaimable, registered, svc, write)


def inbox(service):
    return read(service, "list_driver_messages")["messages"]


def rows(db, sql, *args):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def test_handoff_offer_is_an_inbox_message_the_receiver_reads_and_acks(db, clock):
    project(db)
    registered(db, "codex", "grok")
    codex, grok = svc(db, "codex"), svc(db, "grok")
    attempt = external(codex, "a")
    offer = write(codex, "offer_handoff", project_id="p", request_key="h", expected_owner_fence=1,
                  attempt_id=attempt["attempt_id"], to_driver_id="grok",
                  package={"next_step": "take it", "workers": {"quiescent": True}})
    delivered = offer["notified"]
    assert delivered["target_driver_id"] == "grok" and delivered["seq"] == 1

    messages = inbox(grok)
    assert len(messages) == 1
    message = messages[0]
    assert message["message_id"] == delivered["message_id"] and message["delivery_id"] == delivered["delivery_id"]
    assert message["kind"] == "handoff_offer" and message["sender_driver_id"] == "codex"
    assert message["project_id"] == "p" and message["delivery_mode"] == "transient"
    assert offer["handoff_id"] in message["body"] and message["status"] == "unread" and message["version"] == 1
    assert json.loads(message["refs_json"]) == {"attempt_id": attempt["attempt_id"], "node_key": "a",
                                                "handoff_id": offer["handoff_id"]}
    # The waiting read returns it at once, and the sender cannot read it.
    waited = read(grok, "wait_for_driver_inbox", timeout_seconds=0)
    waited = asyncio.run(waited) if inspect.isawaitable(waited) else waited
    assert [m["message_id"] for m in waited["messages"]] == [message["message_id"]]
    assert inbox(codex) == []
    # The receiver acknowledges through the P2 API like any other message.
    ack = write(grok, "acknowledge_driver_message", delivery_id=message["delivery_id"], expected_version=1,
                request_key="ack-1")
    assert ack["status"] == "acknowledged" and ack["version"] == 2
    # One copy only: no pending-notice table, no notice event.
    assert not rows(db, "SELECT name FROM sqlite_master WHERE name='driver_notices'")
    assert events(db, "driver_notice") == []
    write(grok, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"], expected_owner_fence=1)
    reply = [m for m in inbox(codex) if m["kind"] == "handoff_reply"]
    assert len(reply) == 1 and reply[0]["sender_driver_id"] == "grok"


def test_a_system_notice_has_no_sender_and_a_mapped_kind(db, clock):
    project(db, enforce=True)
    registered(db, "codex", "grok")
    grok, codex = svc(db, "grok"), svc(db, "codex")
    held = claim(grok, workspace="h:/w#g")
    attempt = external(grok, "a", claim_id=held["claim_id"], fence=1)
    reclaimable(clock)
    write(codex, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="grok vanished")
    takeover = [m for m in inbox(grok) if m["kind"] == "takeover_notice"]
    assert len(takeover) == 1 and takeover[0]["sender_driver_id"] is None
    # override_notice has no inbox kind of its own and arrives as lease_notice.
    write(codex, "set_node_hold", project_id="p", node_key="b", held=True, expected_revision=0, reason="wait")
    write(grok, "set_node_hold", project_id="p", node_key="b", held=False, expected_revision=1, reason="go",
          override_reason="owner asked")
    lease = [m for m in inbox(codex) if m["kind"] == "lease_notice"]
    assert len(lease) == 1 and lease[0]["sender_driver_id"] is None and "owner asked" in lease[0]["body"]


def test_an_unregistered_target_gets_no_inbox_message_and_the_write_proceeds(db, clock):
    project(db)
    codex = svc(db, "codex")  # neither codex nor grok is in the driver registry
    attempt = external(codex, "a")
    offer = write(codex, "offer_handoff", project_id="p", request_key="h", expected_owner_fence=1,
                  attempt_id=attempt["attempt_id"], to_driver_id="grok",
                  package={"next_step": "x", "workers": {"quiescent": True}})
    assert offer["status"] == "offered" and offer["notified"] == {"skipped": "unregistered_driver"}
    assert rows(db, "SELECT COUNT(*) AS n FROM driver_inbox_messages") == [{"n": 0}]


def test_a_rolled_back_ownership_write_leaves_no_inbox_message(db, clock):
    project(db)
    registered(db, "codex", "grok")
    store = StateGraphStore(StateDatabase(str(db)), project_read_trusted=True)
    with pytest.raises(RuntimeError):
        with store.transaction(write=True) as conn:
            stored = driver_notices.notify(conn, target_driver_id="grok", kind="takeover_notice",
                                           subject="s", body="b", project_id="p", refs={"node_key": "a"})
            assert stored["target_driver_id"] == "grok"
            raise RuntimeError("the ownership write failed after delivery")
    assert rows(db, "SELECT COUNT(*) AS n FROM driver_inbox_messages") == [{"n": 0}]
    assert rows(db, "SELECT COUNT(*) AS n FROM driver_inbox_deliveries") == [{"n": 0}]
    assert inbox(svc(db, "grok")) == []
