"""`list_user_questions`: a conversation's question blocks in one read.

Before this, the only server read of the questions was a count (the unread
badge); a driver or the dashboard that wanted the questions themselves paged the
whole conversation and re-applied the rule. This read applies the same rule
(`core/driver_questions.py`) server side and returns each question with the
replies that cite it.
"""
from __future__ import annotations

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
    registry = DriverRegistry(db, "user-questions-pepper-not-a-secret")
    registry.register("ada", "ada", actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    return {"ada": StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=True),
            "user": StateService(db, actor="owner:user@example.test", project_read_trusted=True)}


def ask(world, key, *segments):
    return execute(world["ada"], "send_user_message",
                   {"request_key": key, "project_id": "alpha", "subject": "S", "body": "b",
                    "segments": list(segments)}, allow_write=True)["message_id"]


def answer(world, key, body, *refs):
    return execute(world["user"], "send_user_conversation_message",
                   {"request_key": key, "project_id": "alpha", "body": body,
                    "answers": [{"message_id": m, "segment_id": s} for m, s in refs]},
                   allow_write=True)["message_id"]


def questions(world, **kw):
    return execute(world["ada"], "list_user_questions", {"project_id": "alpha", **kw})


def q(segment_id, text=None):
    return {"kind": "question", "id": segment_id, "text": text or "question " + segment_id}


def test_open_answered_and_all_with_the_citing_replies(world):
    first = ask(world, "k1", q("q-a"), {"kind": "info", "text": "fyi"}, q("q-b"))
    second = ask(world, "k2", q("q-c"))
    reply = answer(world, "r1", "yes to a", (first, "q-a"))
    later = answer(world, "r2", "and again", (first, "q-a"))

    open_ = questions(world)
    assert [x["segment_id"] for x in open_["questions"]] == ["q-c", "q-b"]  # newest first
    assert all(x["status"] == "open" and x["answers"] == [] for x in open_["questions"])
    assert open_["counts"] == {"open": 2, "answered": 1}
    assert open_["matched_total"] == 2 and open_["has_more"] is False

    answered = questions(world, status="answered")["questions"]
    assert len(answered) == 1 and answered[0]["message_id"] == first
    assert answered[0]["text"] == "question q-a"
    assert [(a["message_id"], a["body"]) for a in answered[0]["answers"]] == [
        (reply, "yes to a"), (later, "and again")]

    every = questions(world, status="all")["questions"]
    assert [(x["message_id"], x["segment_id"]) for x in every] == [
        (second, "q-c"), (first, "q-b"), (first, "q-a")]


def test_it_agrees_with_the_unread_badge(world):
    first = ask(world, "k1", q("q-a"), q("q-b"))
    answer(world, "r1", "a", (first, "q-a"))
    badge = execute(world["user"], "user_conversation_unread", {"project_id": "alpha"})
    assert badge["questions"] == {"alpha": questions(world)["counts"]["open"]} == {"alpha": 1}


def test_limit_keeps_the_newest_and_says_there_is_more(world):
    for n in range(3):
        ask(world, f"k{n}", q(f"q-{n}"))
    page = questions(world, limit=2)
    assert [x["segment_id"] for x in page["questions"]] == ["q-2", "q-1"]
    assert page["has_more"] is True and page["matched_total"] == 3


def test_an_empty_conversation_and_another_projects_questions(world):
    assert questions(world) == {"project_id": "alpha", "status": "open", "questions": [],
                                "matched_total": 0, "has_more": False,
                                "counts": {"open": 0, "answered": 0}}
    ask(world, "k1", q("q-a"))
    assert execute(world["user"], "list_user_questions",
                   {"project_id": "beta"})["counts"] == {"open": 0, "answered": 0}


@pytest.mark.parametrize("args", [{"status": "closed"}, {"limit": 0}, {"limit": 201}])
def test_bad_arguments_are_refused(world, args):
    with pytest.raises((StateGraphError, ValueError)):
        questions(world, **args)


def test_the_badge_path_does_not_read_message_bodies(world, monkeypatch):
    """The unread badge is polled unattended; it needs counts, never bodies."""
    import core.driver_questions as dq
    seen = []
    real = dq._decoded
    monkeypatch.setattr(dq, "_decoded", lambda row: seen.append(set(row.keys())) or real(row))
    first = ask(world, "k1", q("q-a"))
    answer(world, "r1", "a", (first, "q-a"))
    execute(world["user"], "user_conversation_unread", {"project_id": "alpha"})
    assert seen and all("body" not in keys for keys in seen)
    seen.clear()
    questions(world, status="answered")
    assert seen and all("body" in keys for keys in seen)
