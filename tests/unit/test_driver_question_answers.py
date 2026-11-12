"""Question blocks have identities, and a user's reply can cite the one it answers.

The owner asked for two things on one conversation: a `question` block in a
driver message stays visible as an OPEN question until the user answers it,
and each question is answered on its own (a reply cites exactly one block).

These tests pin:

* a segment gets a stable id when none is given; a caller id is kept; an
  unknown kind, a duplicate id, a malformed or over-long id stores NOTHING;
* a reply's `answers` round-trip through the conversation read, and every
  refusal (another project's message, a missing segment, an info segment, a
  repeated ref, too many refs, a malformed ref) writes NOTHING;
* answering never modifies the answered message;
* two separate replies may cite the same question (only a repeat INSIDE one
  request is refused), and the question still closes exactly once;
* a legacy row (written before ids and answers existed) reads back with no
  id and no `answers` key, and is not rewritten;
* the conversation read carries each segment's id and each message's answers.

PRESENTATION, NEVER AUTHORITY: nothing here answers a checkpoint or becomes
evidence. The rows are durable messages and nothing else.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.drivers import DriverRegistry
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

NODE = {"goal": "G", "acceptance": [{"id": "c", "kind": "test", "description": "d"}]}


@pytest.fixture
def world(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    seed.create_project("beta", "Beta")
    seed.store.add_nodes("alpha", [{"key": "a", **NODE}])
    seed.store.add_nodes("beta", [{"key": "b", **NODE}])
    registry = DriverRegistry(db, "question-answers-pepper-not-a-secret")
    for driver in ("ada", "bob"):
        registry.register(driver, driver, actor="test")
        registry.set_membership("alpha", driver, "member", 0, "fixture", actor="test")
    return {"db": db, "path": str(tmp_path / "state.sqlite"),
            "ada": StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=True),
            "user": StateService(db, actor="owner:user@example.test", project_read_trusted=True),
            "reader": StateService(db, actor="owner:user@example.test", project_read_trusted=True)}


def ask(world, request_key="ask", segments=None, project_id="alpha", driver="ada"):
    """A driver message carrying the given segments."""
    args = {"request_key": request_key, "project_id": project_id, "subject": "Report",
            "body": "body text"}
    if segments is not None:
        args["segments"] = segments
    return execute(world[driver], "send_user_message", args, allow_write=True)


def reply(world, request_key="reply", answers=None, project_id="alpha", **kw):
    args = {"request_key": request_key, "project_id": project_id, "body": "my answer", **kw}
    if answers is not None:
        args["answers"] = answers
    return execute(world["user"], "send_user_conversation_message", args, allow_write=True)


def stored_segments(path, message_id):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT segments_json FROM driver_inbox_messages WHERE message_id=?",
                           (message_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None or row[0] is None else json.loads(row[0])


def count_rows(path, table="driver_inbox_messages"):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def row_of(world, message_id):
    page = execute(world["reader"], "list_user_conversation", {"project_id": "alpha", "limit": 100})
    return next(m for m in page["messages"] if m["message_id"] == message_id)


# ── segment identity ────────────────────────────────────────────────────────

def test_a_segment_without_an_id_gets_a_stable_short_one(world):
    sent = ask(world, segments=[{"kind": "question", "text": "may I restart?"}])
    [block] = stored_segments(world["path"], sent["message_id"])
    assert block["kind"] == "question" and block["text"] == "may I restart?"
    assert isinstance(block["id"], str) and len(block["id"]) == 12
    assert all(c in "0123456789abcdef" for c in block["id"])
    # The id is stored once and read back unchanged: nothing regenerates it.
    assert row_of(world, sent["message_id"])["segments"][0]["id"] == block["id"]


def test_a_caller_supplied_id_is_kept(world):
    sent = ask(world, segments=[{"id": "q-restart.1:a", "kind": "question", "text": "restart?"}])
    assert stored_segments(world["path"], sent["message_id"])[0]["id"] == "q-restart.1:a"


def test_an_id_less_replay_is_the_same_message_even_though_ids_are_generated(world):
    """Idempotency compares what the caller sent, not the random id generated per call."""
    first = ask(world, request_key="same", segments=[{"kind": "question", "text": "again?"}])
    second = ask(world, request_key="same", segments=[{"kind": "question", "text": "again?"}])
    assert first == second
    assert count_rows(world["path"]) == 1


@pytest.mark.parametrize("bad", [
    {"kind": "demand", "text": "do it"},                 # kind outside info|question
    {"kind": "question", "text": ""},                    # empty text
    {"kind": "question", "text": "x", "id": "has space"},  # id outside the allowed alphabet
    {"kind": "question", "text": "x", "id": "a" * 65},   # over-long id
    {"kind": "question", "text": "x", "id": ""},         # empty id
])
def test_a_malformed_segment_is_refused_and_stores_nothing(world, bad):
    before = count_rows(world["path"])
    with pytest.raises(StateGraphError):
        ask(world, request_key="bad", segments=[{"kind": "info", "text": "ok"}, bad])
    assert count_rows(world["path"]) == before


def test_a_duplicate_segment_id_in_one_message_is_refused_and_stores_nothing(world):
    before = count_rows(world["path"])
    with pytest.raises(StateGraphError):
        ask(world, request_key="dup", segments=[
            {"id": "same", "kind": "question", "text": "one?"},
            {"id": "same", "kind": "info", "text": "two"}])
    assert count_rows(world["path"]) == before


def test_too_many_segments_are_refused(world):
    before = count_rows(world["path"])
    with pytest.raises(StateGraphError):
        ask(world, request_key="many", segments=[{"kind": "info", "text": str(i)} for i in range(21)])
    assert count_rows(world["path"]) == before


# ── answers: round-trip ─────────────────────────────────────────────────────

def test_a_reply_citing_one_question_round_trips(world):
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "restart?"},
                                 {"id": "i1", "kind": "info", "text": "coordinated"}])
    answer = reply(world, answers=[{"message_id": asked["message_id"], "segment_id": "q1"}])
    assert row_of(world, answer["message_id"])["answers"] == [
        {"message_id": asked["message_id"], "segment_id": "q1"}]


def test_answering_does_not_modify_the_answered_message(world):
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "restart?"}])
    before = row_of(world, asked["message_id"])
    reply(world, answers=[{"message_id": asked["message_id"], "segment_id": "q1"}])
    after = row_of(world, asked["message_id"])
    assert after == before
    assert "answers" not in after


def test_a_reply_with_no_citation_has_no_answers_key(world):
    """Reading returns `answers` only when the row has any, never an empty list."""
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "restart?"}])
    answer = reply(world, request_key="plain")
    assert "answers" not in row_of(world, answer["message_id"])
    assert "answers" not in row_of(world, asked["message_id"])


def test_a_reply_that_only_threads_on_a_message_closes_no_question(world):
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "restart?"}])
    reply(world, request_key="thread", reply_to_message_id=asked["message_id"])
    assert "answers" not in row_of(world, asked["message_id"])


def test_several_questions_of_one_message_are_answered_by_separate_replies(world):
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "first?"},
                                 {"id": "q2", "kind": "question", "text": "second?"}])
    reply(world, request_key="r1", answers=[{"message_id": asked["message_id"], "segment_id": "q1"}])
    second = reply(world, request_key="r2", answers=[{"message_id": asked["message_id"], "segment_id": "q2"}])
    assert row_of(world, second["message_id"])["answers"] == [
        {"message_id": asked["message_id"], "segment_id": "q2"}]


def test_two_separate_replies_may_cite_the_same_question_and_it_closes_once(world):
    """Decided on purpose: a second citation of the same question is ACCEPTED.

    Only the repeat INSIDE one request is refused (`invalid_answers: the same
    question is cited twice`), because there one caller named the same block
    twice. Two separate replies are two things the user said: "yes, restart" and
    a later "I already restarted the second one" are both real messages, and
    refusing the second would either lose it or push the user into writing an
    uncited reply that claims nothing.

    The question still closes ONCE, because it has no status of its own: it is
    closed by the set of citations pointing at it, and its own row is never
    touched - no `answered_at`, no cleared segments, no rewrite. Both replies
    keep their own citation, so a reader shows one question and both answers.
    """
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "restart?"}])
    before = row_of(world, asked["message_id"])
    first = reply(world, request_key="yes", answers=[{"message_id": asked["message_id"], "segment_id": "q1"}])
    second = reply(world, request_key="again", answers=[{"message_id": asked["message_id"], "segment_id": "q1"}])
    cited = {"message_id": asked["message_id"], "segment_id": "q1"}
    assert row_of(world, first["message_id"])["answers"] == [cited]
    assert row_of(world, second["message_id"])["answers"] == [cited]
    # Closed once: the answered message is byte-identical and carries no answer
    # of its own - the linkage lives only on the two replies.
    assert row_of(world, asked["message_id"]) == before
    assert "answers" not in row_of(world, asked["message_id"])
    assert count_rows(world["path"]) == 3


# ── answers: every refusal writes nothing ───────────────────────────────────

def test_a_ref_naming_another_projects_message_is_refused(world):
    other = ask(world, request_key="other", project_id="beta", segments=[])
    rows_before = count_rows(world["path"])
    with pytest.raises(StateGraphError, match="unknown_answer_message"):
        reply(world, answers=[{"message_id": other["message_id"], "segment_id": "q1"}])
    assert count_rows(world["path"]) == rows_before


def test_a_ref_naming_a_missing_message_is_refused(world):
    rows_before = count_rows(world["path"])
    with pytest.raises(StateGraphError, match="unknown_answer_message"):
        reply(world, answers=[{"message_id": "no-such-message", "segment_id": "q1"}])
    assert count_rows(world["path"]) == rows_before


def test_a_ref_naming_a_segment_the_message_lacks_is_refused(world):
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "restart?"}])
    rows_before = count_rows(world["path"])
    with pytest.raises(StateGraphError, match="unknown_answer_segment"):
        reply(world, answers=[{"message_id": asked["message_id"], "segment_id": "nope"}])
    assert count_rows(world["path"]) == rows_before


def test_a_ref_naming_an_info_segment_is_refused(world):
    asked = ask(world, segments=[{"id": "i1", "kind": "info", "text": "context"}])
    rows_before = count_rows(world["path"])
    with pytest.raises(StateGraphError, match="answer_not_a_question"):
        reply(world, answers=[{"message_id": asked["message_id"], "segment_id": "i1"}])
    assert count_rows(world["path"]) == rows_before


def test_a_ref_to_a_user_message_is_refused(world):
    """Only the driver's message carries segments; the user's own message is never cited."""
    user_row = reply(world, request_key="user-first", body="hello")
    rows_before = count_rows(world["path"])
    with pytest.raises(StateGraphError):
        reply(world, request_key="cites-user", answers=[{"message_id": user_row["message_id"], "segment_id": "q1"}])
    assert count_rows(world["path"]) == rows_before


def test_the_same_ref_twice_is_refused_and_writes_nothing(world):
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "restart?"}])
    rows_before = count_rows(world["path"])
    with pytest.raises(StateGraphError, match="cited twice"):
        reply(world, request_key="twice", answers=[
            {"message_id": asked["message_id"], "segment_id": "q1"},
            {"message_id": asked["message_id"], "segment_id": "q1"}])
    assert count_rows(world["path"]) == rows_before


def test_a_refusal_in_the_second_ref_leaves_the_first_unwritten(world):
    """One bad ref refuses the whole reply: the good one is not half-applied."""
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "restart?"}])
    rows_before = count_rows(world["path"])
    with pytest.raises(StateGraphError):
        reply(world, request_key="partial", answers=[
            {"message_id": asked["message_id"], "segment_id": "q1"},
            {"message_id": asked["message_id"], "segment_id": "missing"}])
    assert count_rows(world["path"]) == rows_before
    assert "answers" not in row_of(world, asked["message_id"])


def test_more_than_twenty_answers_are_refused(world):
    rows_before = count_rows(world["path"])
    with pytest.raises(StateGraphError):
        reply(world, request_key="many", answers=[
            {"message_id": f"m{i}", "segment_id": "q"} for i in range(21)])
    assert count_rows(world["path"]) == rows_before


def test_a_malformed_ref_is_refused(world):
    rows_before = count_rows(world["path"])
    with pytest.raises(StateGraphError):
        reply(world, request_key="shape", answers=[{"message_id": "m1"}])
    with pytest.raises(StateGraphError):
        reply(world, request_key="extra", answers=[{"message_id": "m1", "segment_id": "q", "x": 1}])
    assert count_rows(world["path"]) == rows_before


def test_a_refused_reply_can_be_retried_with_a_corrected_citation(world):
    """A refusal is not recorded as the request's answer, so a fixed retry goes through."""
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "restart?"}])
    with pytest.raises(StateGraphError):
        reply(world, request_key="retry", answers=[{"message_id": asked["message_id"], "segment_id": "bad"}])
    ok = reply(world, request_key="retry", answers=[{"message_id": asked["message_id"], "segment_id": "q1"}])
    assert row_of(world, ok["message_id"])["answers"] == [
        {"message_id": asked["message_id"], "segment_id": "q1"}]


# ── legacy rows ─────────────────────────────────────────────────────────────

def test_a_legacy_segment_row_reads_back_with_no_id_and_is_not_rewritten(world):
    """A row written before segment ids existed keeps its exact stored JSON."""
    legacy_json = json.dumps([{"kind": "question", "text": "old question"}])
    conn = sqlite3.connect(world["path"])
    conn.execute("INSERT INTO driver_inbox_messages(message_id,thread_id,sender_driver_id,project_id,kind,"
                 "delivery_mode,subject,body,refs_json,reply_to_message_id,created_at,audience,segments_json) "
                 "VALUES('legacy1','legacy1','ada','alpha','note','transient','Old','body','{}',NULL,"
                 "'2026-01-01T00:00:00+00:00','user',?)", (legacy_json,))
    conn.commit()
    conn.close()
    assert stored_segments(world["path"], "legacy1") == [{"kind": "question", "text": "old question"}]
    row = row_of(world, "legacy1")
    assert row["segments"] == [{"kind": "question", "text": "old question"}]
    assert "id" not in row["segments"][0]
    assert stored_segments(world["path"], "legacy1") == [{"kind": "question", "text": "old question"}]


def test_a_row_with_null_answers_reads_with_no_answers_key(world):
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "restart?"}])
    assert "answers" not in row_of(world, asked["message_id"])


# ── the conversation read shape ─────────────────────────────────────────────

def test_the_conversation_read_carries_segment_ids_and_message_answers(world):
    asked = ask(world, segments=[{"id": "q1", "kind": "question", "text": "restart?"},
                                 {"kind": "info", "text": "coordinated"}])
    answer = reply(world, answers=[{"message_id": asked["message_id"], "segment_id": "q1"}])
    page = execute(world["reader"], "list_user_conversation", {"project_id": "alpha", "limit": 10})
    by_id = {m["message_id"]: m for m in page["messages"]}

    driver_row = by_id[asked["message_id"]]
    assert [s["kind"] for s in driver_row["segments"]] == ["question", "info"]
    assert driver_row["segments"][0]["id"] == "q1"
    assert isinstance(driver_row["segments"][1]["id"], str) and driver_row["segments"][1]["id"]
    assert "answers" not in driver_row

    user_row = by_id[answer["message_id"]]
    assert user_row["answers"] == [{"message_id": asked["message_id"], "segment_id": "q1"}]
