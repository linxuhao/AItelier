"""A project's open questions have ONE definition, and the list badge is it.

The conversation's questions box already decides what "open" means: a
`question` segment that no row's `answers` cites with its exact
`(message_id, segment_id)` pair. The project list must show that same number
next to the unread badge, so this file pins the server rule
(`core/driver_questions.py`), the endpoint that carries its count
(`user_conversation_unread`), and the agreement between the rule and its stored
rows.

What is deliberately NOT a way to answer a question:

* naming the message in `reply_to_message_id` - a reply cites nothing unless it
  writes `answers`, so a question stays open through any number of prose replies;
* being an `info` segment - those are never questions and never counted;
* having no segment id - a citation names an id, so such a question cannot be
  closed and is skipped by the rule rather than listed as open forever (the same
  decision the web rule makes, and the shared fixture proves they agree);
* reading the thread - the unread marker is about MESSAGES. Reading does not
  answer a question, so the two badges move independently.

PRESENTATION, NEVER AUTHORITY: a count here is a badge. It answers no checkpoint
and moves no node.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.driver_questions import conversation_rows, open_question_count, open_questions
from core.drivers import DriverRegistry
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

REPO = Path(__file__).resolve().parents[2]
FAMILY = REPO / "tests" / "fixtures" / "driver_questions_open_family.json"
NODE = {"goal": "G", "acceptance": [{"id": "c", "kind": "test", "description": "d"}]}


def message(message_id, *, segments=(), answers=None, reply_to=None, body=None,
            created_at="2026-11-13T10:00:00+00:00"):
    """One row in the shape the rule reads. `answers` is ABSENT unless passed."""
    entry = {"message_id": message_id, "created_at": created_at, "segments": list(segments)}
    if body is not None:
        entry["body"] = body
    if answers is not None:
        entry["answers"] = answers
    if reply_to is not None:
        entry["reply_to_message_id"] = reply_to
    return entry


def question(segment_id):
    return {"id": segment_id, "kind": "question", "text": "question " + segment_id}


def answer(message_id, segment_id):
    return {"message_id": message_id, "segment_id": segment_id}


def counted(rows):
    return sorted((q["message_id"], q["segment_id"]) for q in open_questions(rows))


@pytest.fixture
def world(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    seed.create_project("beta", "Beta")
    seed.store.add_nodes("alpha", [{"key": "a", **NODE}])
    seed.store.add_nodes("beta", [{"key": "b", **NODE}])
    registry = DriverRegistry(db, "open-question-pepper-not-a-secret")
    for driver in ("ada", "bob"):
        registry.register(driver, driver, actor="test")
        registry.set_membership("alpha", driver, "member", 0, "fixture", actor="test")
    return {"db": db, "path": str(tmp_path / "state.sqlite"), "seed": seed,
            "ada": StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=True),
            "user": StateService(db, actor="owner:user@example.test", project_read_trusted=True)}


def say_driver(world, request_key="ask", segments=None, project_id="alpha", driver="ada"):
    args = {"request_key": request_key, "project_id": project_id, "subject": "Report",
            "body": "body text"}
    if segments is not None:
        args["segments"] = segments
    return execute(world[driver], "send_user_message", args, allow_write=True)


def say_user(world, request_key="reply", answers=None, project_id="alpha", **kw):
    args = {"request_key": request_key, "project_id": project_id, "body": "my answer", **kw}
    if answers is not None:
        args["answers"] = answers
    return execute(world["user"], "send_user_conversation_message", args, allow_write=True)


def rows_of(path, project_id):
    """The stored rows of one project's conversation, as the rule reads them."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conversation_rows(conn, project_id)
    finally:
        conn.close()


# ------------------------------------------------------ the shared family
def test_the_shared_family_yields_the_one_open_count_it_declares():
    """The same fixture file drives the web rule (StateProjectsQuestions.test.ts).

    If this fails, the server and the browser no longer agree on what "open"
    means, whatever either number says on its own.
    """
    family = json.loads(FAMILY.read_text(encoding="utf-8"))
    assert open_question_count(family["rows"]) == family["open_count"]
    # Named, so a rule that happens to produce the right COUNT for the wrong
    # reasons still fails here: only the uncited questions of m1 and m6 survive.
    assert counted(family["rows"]) == [("m1", "q2"), ("m6", "q5")]


# ------------------------------------------------------- each rule, alone
def test_a_question_a_later_reply_cites_is_not_open():
    rows = [message("m1", segments=[question("q1")]),
            message("m2", answers=[answer("m1", "q1")],
                    created_at="2026-11-13T10:05:00+00:00")]
    assert open_questions(rows) == [] and open_question_count(rows) == 0


def test_a_question_nothing_cites_is_open():
    rows = [message("m1", segments=[question("q1")])]
    [open_one] = open_questions(rows)
    assert open_one["message_id"] == "m1" and open_one["segment_id"] == "q1"
    assert open_one["text"] == "question q1"
    assert open_question_count(rows) == 1


def test_a_reply_that_only_sets_reply_to_cites_nothing():
    """The prose names the question; only `answers` closes it."""
    rows = [message("m1", segments=[question("q1")]),
            message("m2", reply_to="m1", body="yes, go ahead"),
            message("m3", answers=[], reply_to="m1")]
    assert counted(rows) == [("m1", "q1")]
    # An explicitly empty list and an absent key are the same answer: none.
    assert open_question_count(rows) == 1


def test_a_non_question_segment_is_never_counted():
    rows = [message("m1", segments=[{"id": "i1", "kind": "info", "text": "the suite is green"}])]
    assert open_question_count(rows) == 0


def test_two_questions_in_one_message_close_independently():
    rows = [message("m1", segments=[question("q1"), question("q2")]),
            message("m2", answers=[answer("m1", "q1")],
                    created_at="2026-11-13T10:05:00+00:00")]
    assert counted(rows) == [("m1", "q2")]
    assert open_question_count(rows) == 1


def test_a_question_without_an_id_cannot_be_cited_and_is_skipped():
    rows = [message("m1", segments=[{"kind": "question", "text": "who am I?"}])]
    assert open_question_count(rows) == 0


# ------------------------------------------------- the endpoint arm (RED on base)
def test_the_endpoint_carries_the_count_the_rule_computes(world):
    """`user_conversation_unread` must add the questions count, not move the rest."""
    asked = say_driver(world, request_key="ask", segments=[
        {"id": "q1", "kind": "question", "text": "may I restart the build?"},
        {"id": "q2", "kind": "question", "text": "may I deploy afterwards?"},
        {"id": "i1", "kind": "info", "text": "the suite is green"}])
    # Cited by its exact pair: closed.
    say_user(world, request_key="close-q1", answers=[{"message_id": asked["message_id"],
                                                      "segment_id": "q1"}])
    # A reply that only names the message answers nothing: q2 stays open.
    say_user(world, request_key="prose-only", reply_to_message_id=asked["message_id"])
    later = say_driver(world, request_key="ask-2", segments=[
        {"id": "q3", "kind": "question", "text": "cap the retry?"}])

    badge = execute(world["user"], "user_conversation_unread", {})
    expected = open_question_count(rows_of(world["path"], "alpha"))
    assert expected == 2
    # The unread keys keep their meaning (the two driver messages, not the two
    # user replies), and the questions keys are the rule's number, not a second
    # count of the same rows.
    assert badge == {"projects": {"alpha": 2}, "total": 2,
                     "questions": {"alpha": expected}, "questions_total": expected}
    # Both open questions survive their own message and nothing else does. Order
    # is not asserted: rows written in the same second tie on `created_at` and
    # the rule then breaks the tie on `message_id`, which is random here.
    assert set(counted(rows_of(world["path"], "alpha"))) == {
        (asked["message_id"], "q2"), (later["message_id"], "q3")}

    # Reading the thread clears the message badge and answers no question.
    execute(world["user"], "mark_user_conversation_read",
            {"project_id": "alpha", "message_id": later["message_id"]}, allow_write=True)
    after = execute(world["user"], "user_conversation_unread", {})
    assert after["total"] == 0 and after["questions"] == badge["questions"]

    # A project with no open question is ABSENT from `questions` and contributes
    # 0 to the total, which is the shape the list badge reads.
    empty = execute(world["user"], "user_conversation_unread", {"project_id": "beta"})
    assert empty == {"projects": {"beta": 0}, "total": 0,
                     "questions": {}, "questions_total": 0}
