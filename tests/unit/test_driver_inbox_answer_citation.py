"""The answer citation reaches the driver's OWN inbox, from ONE projection.

A user's reply to a driver's question stores the citation on the USER's message
row: `answers: [{message_id, segment_id}]`, next to whatever `segments` that
message carries of its own. `list_user_conversation` projected both. The same
message reaching the driver's private inbox did not, because
`list_driver_messages` selected the message's readable columns without
`segments_json`/`answers_json` - so the one reader that OWNS the question was
the one reader that could not see the answer.

What is pinned here:

* ONE shared projection (`core.driver_inbox.citation_fields`) feeds BOTH reads,
  so one message id projects the same `segments` and `answers` in the
  conversation read and in the inbox read. The two dicts are compared as
  EQUALITY, field by field - "both are non-empty" would pass on two different
  projections;
* each arm asserts its OWN projection, not only their equality: a projection
  that dropped `answers` on both sides at once would still read as "equal", so
  equality alone could not notice it. That is what makes a single mutant at the
  shared point redden both arms;
* an inbox row that carries neither keeps EXACTLY the shape it had before - the
  same key set and the same values, no `segments: []`, no `answers: []` - while
  a row that carries them gains exactly those two keys and never the raw
  `segments_json`/`answers_json` strings.

PRESENTATION, NEVER AUTHORITY: a citation is a durable label on a message. It
answers no checkpoint, becomes no evidence and changes no node.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from core.drivers import DriverRegistry
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

# The inbox row as deployed before this card: `d.*` plus the message's readable
# columns. Pinned as a literal, so an accidental extra key (or a missing one)
# fails here rather than sliding through a superset comparison.
BASE_ROW_KEYS = {
    "delivery_id", "message_id", "target_driver_id", "seq", "status", "version",
    "thread_id", "sender_driver_id", "project_id", "kind", "delivery_mode",
    "subject", "body", "refs_json"}


@pytest.fixture
def world(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    registry = DriverRegistry(db, "inbox-citation-pepper-not-a-secret")
    registry.register("ada", "ada", actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    return {"db": db, "path": str(tmp_path / "state.sqlite"),
            "ada": StateService(db, actor="driver:ada", driver_id="ada",
                                project_read_trusted=True),
            "user": StateService(db, actor="owner:user@example.test",
                                 project_read_trusted=True)}


def ask(world, request_key="ask", segments=None):
    """The driver asks the user something, with tagged blocks."""
    args = {"request_key": request_key, "project_id": "alpha", "subject": "Report",
            "body": "body text"}
    if segments is not None:
        args["segments"] = segments
    return execute(world["ada"], "send_user_message", args, allow_write=True)


def user_message(world, request_key="say", body="plain body", **kw):
    """The user's half: the row that lands in the driver's inbox as a delivery."""
    return execute(world["user"], "send_user_conversation_message",
                   {"request_key": request_key, "project_id": "alpha", "body": body, **kw},
                   allow_write=True)


def conversation(world):
    return execute(world["user"], "list_user_conversation",
                   {"project_id": "alpha", "limit": 100})["messages"]


def inbox(world):
    return execute(world["ada"], "list_driver_messages", {"limit": 100})["messages"]


def conversation_row(world, message_id):
    return next(m for m in conversation(world) if m["message_id"] == message_id)


def inbox_row(world, message_id):
    return next(m for m in inbox(world) if m["message_id"] == message_id)


def store_own_segments(path, message_id, segments):
    """Give the reply's OWN row some blocks.

    No write action stores segments on the user's half yet - `send_user_message`
    is the only writer of `segments_json`, and it writes the driver's half - so
    "a user reply that carries segments of its own" is exercised through the
    column itself. The READ must project whatever the column holds, from one
    place, no matter which half wrote it.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE driver_inbox_messages SET segments_json=? WHERE message_id=?",
                     (json.dumps(segments), message_id))
        conn.commit()
    finally:
        conn.close()


def answered(world):
    """A driver question, the user's citing reply to it, and blocks on the reply."""
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "may I restart?"},
                                 {"id": "i1", "kind": "info", "text": "one hour left"}])
    reply = user_message(world, request_key="answer", body="yes, restart",
                         answers=[{"message_id": asked["message_id"], "segment_id": "q1"}])
    own = [{"id": "u1", "kind": "info", "text": "restarted at 10:04"}]
    store_own_segments(world["path"], reply["message_id"], own)
    return asked, reply, own


# ── arm 1: what the CONVERSATION read projects ──────────────────────────────

def test_the_conversation_read_projects_answers_and_the_question_blocks(world):
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "may I restart?"},
                                 {"id": "i1", "kind": "info", "text": "one hour left"}])
    reply = user_message(world, request_key="answer", body="yes, restart",
                         answers=[{"message_id": asked["message_id"], "segment_id": "q1"}])
    cited = [{"message_id": asked["message_id"], "segment_id": "q1"}]
    # The question's blocks, with the id a reply cites.
    assert conversation_row(world, asked["message_id"])["segments"] == [
        {"id": "q1", "kind": "question", "text": "may I restart?"},
        {"id": "i1", "kind": "info", "text": "one hour left"}]
    # The reply's citation.
    assert conversation_row(world, reply["message_id"])["answers"] == cited


# ── arm 2: what the DRIVER'S OWN INBOX projects ─────────────────────────────

def test_the_inbox_read_projects_the_answers_and_the_replys_own_blocks(world):
    asked, reply, own = answered(world)
    row = inbox_row(world, reply["message_id"])
    assert row["answers"] == [{"message_id": asked["message_id"], "segment_id": "q1"}]
    assert row["segments"] == own


# ── the two reads, on the SAME message id ───────────────────────────────────

def test_both_reads_project_the_same_citation_for_the_same_message(world):
    asked, reply, own = answered(world)
    delivered = {m["message_id"]: m for m in inbox(world)}
    shared = [m for m in conversation(world) if m["message_id"] in delivered]
    # Only the user's half of the conversation is delivered to the driver, so
    # exactly one message can be read through both.
    assert [m["message_id"] for m in shared] == [reply["message_id"]]
    for message in shared:
        mirrored = delivered[message["message_id"]]
        citation = ("segments", "answers")
        assert {k: message[k] for k in citation} == {k: mirrored[k] for k in citation}
    # Quoted explicitly, so a regression names what it lost.
    assert {k: shared[0][k] for k in ("segments", "answers")} == {
        "segments": own,
        "answers": [{"message_id": asked["message_id"], "segment_id": "q1"}]}


# ── the inbox row's existing contract ───────────────────────────────────────

def test_a_plain_inbox_row_keeps_its_exact_base_shape(world):
    """No segments, no answers: the delivered row IS the base row, not a superset."""
    sent = user_message(world, request_key="plain", body="no blocks here")
    delivery = sent["deliveries"][0]
    [row] = inbox(world)
    assert set(row) == BASE_ROW_KEYS
    assert row == {"delivery_id": delivery["delivery_id"],
                   "message_id": sent["message_id"],
                   "target_driver_id": "ada",
                   "seq": delivery["seq"],
                   "status": "unread",
                   "version": 1,
                   "thread_id": sent["message_id"],
                   "sender_driver_id": None,
                   "project_id": "alpha",
                   "kind": "note",
                   "delivery_mode": "transient",
                   "subject": "Message from the user",
                   "body": "no blocks here",
                   "refs_json": "{}"}
    assert "segments" not in row and "answers" not in row
    # The conversation keeps its own contract: it renders the blocks area, so it
    # always carries `segments` (empty here) and still omits `answers`.
    conversation_message = conversation_row(world, sent["message_id"])
    assert conversation_message["segments"] == []
    assert "answers" not in conversation_message


def test_a_citing_inbox_row_gains_exactly_the_two_citation_keys(world):
    asked, reply, own = answered(world)
    delivery = reply["deliveries"][0]
    row = inbox_row(world, reply["message_id"])
    assert set(row) == BASE_ROW_KEYS | {"segments", "answers"}
    # Every base key keeps the value it had before this card.
    assert {k: v for k, v in row.items() if k in BASE_ROW_KEYS} == {
        "delivery_id": delivery["delivery_id"],
        "message_id": reply["message_id"],
        "target_driver_id": "ada",
        "seq": delivery["seq"],
        "status": "unread",
        "version": 1,
        "thread_id": reply["message_id"],
        "sender_driver_id": None,
        "project_id": "alpha",
        "kind": "note",
        "delivery_mode": "transient",
        "subject": "Message from the user",
        "body": "yes, restart",
        "refs_json": "{}"}
    assert row["segments"] == own
    assert row["answers"] == [{"message_id": asked["message_id"], "segment_id": "q1"}]
    # The storage columns are not payload.
    assert "segments_json" not in row and "answers_json" not in row
