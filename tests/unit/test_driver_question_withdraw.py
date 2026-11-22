"""`withdraw_user_question`: a driver takes back its own open question, as a record.

The withdrawal is a row of its own that cites the question (`withdraws`), so the
question stays in the thread and every surface reads one rule
(`core/driver_questions.py`): the unread badge's count, `list_user_questions`,
and the dashboard rail (`web/src/lib/questions.ts`, which reads the same rows'
`withdraws`; the rows this test builds are exported for its vitest twin).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.driver_questions import all_questions, conversation_rows, open_question_count
from core.drivers import DriverRegistry
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService
from tests.support import state_template


@pytest.fixture
def world(tmp_path, state_db_copy):
    db = StateDatabase(state_db_copy(state_template.state_registry, tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    registry = DriverRegistry(db, "withdraw-question-pepper-not-a-secret-123")
    for name in ("ada", "bob"):
        registry.register(name, name, actor="test")
        registry.set_membership("alpha", name, "member", 0, "fixture", actor="test")
    return {"db": db, "path": str(tmp_path / "state.sqlite"),
            "ada": StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=True),
            "bob": StateService(db, actor="driver:bob", driver_id="bob", project_read_trusted=True),
            "user": StateService(db, actor="owner:user@example.test", project_read_trusted=True)}


def ask(world, who, key, *segment_ids):
    return execute(world[who], "send_user_message",
                   {"request_key": key, "project_id": "alpha", "subject": "S", "body": "b",
                    "segments": [{"kind": "question", "id": s, "text": "question " + s} for s in segment_ids]},
                   allow_write=True)["message_id"]


def withdraw(world, who, key, message_id, segment_id, reason="no longer needed"):
    return execute(world[who], "withdraw_user_question",
                   {"request_key": key, "project_id": "alpha", "message_id": message_id,
                    "segment_id": segment_id, "reason": reason}, allow_write=True)


def answer(world, key, *refs):
    return execute(world["user"], "send_user_conversation_message",
                   {"request_key": key, "project_id": "alpha", "body": "answer",
                    "answers": [{"message_id": m, "segment_id": s} for m, s in refs]}, allow_write=True)


def surfaces(world):
    """The three readers of 'open': badge count, list_user_questions, and the rule
    over the conversation read's rows (what the dashboard rail is handed)."""
    badge = execute(world["ada"], "user_conversation_unread", {"project_id": "alpha"})["questions"].get("alpha", 0)
    listed = execute(world["ada"], "list_user_questions", {"project_id": "alpha"})
    conversation = execute(world["user"], "list_user_conversation", {"project_id": "alpha", "limit": 200})["messages"]
    return badge, listed, conversation


def test_withdrawing_an_own_open_question_closes_it_everywhere_and_keeps_the_record(world):
    m = ask(world, "ada", "k1", "q-a", "q-b")
    before_badge, before_list, _ = surfaces(world)
    assert before_badge == 2 and before_list["counts"] == {"open": 2, "answered": 0, "withdrawn": 0}

    result = withdraw(world, "ada", "w1", m, "q-a", "resolved by the hotfix")
    assert result["withdrawn"] is True and result["message_id"] == m and result["segment_id"] == "q-a"

    badge, listed, conversation = surfaces(world)
    assert badge == 1 == listed["counts"]["open"]
    assert [q["segment_id"] for q in listed["questions"]] == ["q-b"]
    withdrawn = execute(world["ada"], "list_user_questions", {"project_id": "alpha", "status": "withdrawn"})["questions"]
    assert [(q["segment_id"], q["withdrawn"]["reason"], q["withdrawn"]["message_id"]) for q in withdrawn] == [
        ("q-a", "resolved by the hotfix", result["withdrawal_message_id"])]
    # The question row is untouched; the withdrawal is a row of its own in the thread.
    asked = next(r for r in conversation if r["message_id"] == m)
    assert [s["id"] for s in asked["segments"]] == ["q-a", "q-b"] and "withdraws" not in asked
    record = next(r for r in conversation if r["message_id"] == result["withdrawal_message_id"])
    assert record["sender_kind"] == "driver" and record["subject"] == "Question withdrawn"
    assert record["withdraws"] == [{"message_id": m, "segment_id": "q-a", "reason": "resolved by the hotfix"}]
    assert "resolved by the hotfix" in record["body"] and "question q-a" in record["body"]
    # The rail's rows: the same rule over the conversation read gives the same open set.
    rows = [{"message_id": r["message_id"], "created_at": r["created_at"], "segments": r.get("segments") or [],
             "answers": r.get("answers") or [], "withdraws": r.get("withdraws") or []} for r in conversation]
    assert [q["segment_id"] for q in all_questions(rows) if q["status"] == "open"] == ["q-b"]
    export = os.environ.get("AITELIER_WITHDRAW_FIXTURE_OUT")
    if export:
        with open(export, "w") as handle:
            json.dump({"rows": conversation, "open": ["q-b"], "withdrawn": ["q-a"]}, handle)


RULE = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "questionRule.ts"
RAIL = """
const { railQuestions } = await import(process.argv[1]);
const rows = JSON.parse(await new Response(process.stdin).text());
const rail = railQuestions(rows);
process.stdout.write(JSON.stringify({
  open: rail.filter(q => q.answered === null && q.withdrawn === null).map(q => q.segment_id),
  answered: rail.filter(q => q.answered !== null).map(q => q.segment_id),
  withdrawn: rail.filter(q => q.withdrawn !== null).map(q => q.segment_id)}));
"""


def test_the_badge_the_list_and_the_dashboard_rail_agree_before_and_after_a_withdrawal(world):
    """The three readers of 'open', on the same conversation: the badge count and
    list_user_questions (server rule) and railQuestions (the dashboard's rule, the
    TypeScript file itself, run by Node on the conversation read's rows)."""
    node = shutil.which("node")
    assert node, "node is part of the shipped image; without it the dashboard rule is unmeasured"
    m = ask(world, "ada", "k1", "q-a", "q-b", "q-c")
    answer(world, "r1", (m, "q-c"))

    def measured():
        badge, listed, conversation = surfaces(world)
        every = execute(world["ada"], "list_user_questions", {"project_id": "alpha", "status": "all"})["questions"]
        server = {state: sorted(q["segment_id"] for q in every if q["status"] == state) for state in ("open", "answered", "withdrawn")}
        out = subprocess.run([node, "--input-type=module", "-e", RAIL, RULE.as_uri()], input=json.dumps(conversation),
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        rail = {state: sorted(ids) for state, ids in json.loads(out.stdout).items()}
        return badge, listed["counts"], server, rail

    badge, counts, server, rail = measured()
    assert badge == counts["open"] == len(rail["open"]) == 2
    assert server == rail == {"open": ["q-a", "q-b"], "answered": ["q-c"], "withdrawn": []}
    withdraw(world, "ada", "w1", m, "q-a")
    badge, counts, server, rail = measured()
    assert badge == counts["open"] == len(rail["open"]) == 1
    assert server == rail == {"open": ["q-b"], "answered": ["q-c"], "withdrawn": ["q-a"]}


def test_the_withdrawal_scans_its_reason_and_quotes_the_question_unscanned(world, monkeypatch):
    """The reason is fresh driver text and goes through the credential guard; the
    question it quotes was already accepted once and is not scanned again."""
    import core.driver_inbox as inbox
    scanned = []
    real = inbox.refuse_credential_body
    monkeypatch.setattr(inbox, "refuse_credential_body", lambda *a: (scanned.append(a), real(*a))[1])
    m = ask(world, "ada", "k1", "q-a")
    scanned.clear()
    withdraw(world, "ada", "w1", m, "q-a", "overtaken")
    assert scanned == [("Question withdrawn", "overtaken", None)]


def test_withdraw_twice_is_a_no_op_naming_the_first_withdrawal(world):
    m = ask(world, "ada", "k1", "q-a")
    first = withdraw(world, "ada", "w1", m, "q-a")
    again = withdraw(world, "ada", "w2", m, "q-a", "another reason")
    assert again == {"withdrawn": False, "already_withdrawn": True, "project_id": "alpha", "message_id": m,
                     "segment_id": "q-a", "withdrawal_message_id": first["withdrawal_message_id"]}
    assert withdraw(world, "ada", "w1", m, "q-a") == first, "a replay of the same request answers the same"
    with pytest.raises(StateGraphError, match="idempotency_conflict"):
        withdraw(world, "ada", "w1", m, "q-a", "other text under the same key")
    with world["db"].connect() if hasattr(world["db"], "connect") else world["ada"].store.transaction() as conn:
        n = conn.execute("SELECT COUNT(*) FROM driver_inbox_messages WHERE withdraws_json IS NOT NULL").fetchone()[0]
    assert n == 1


def test_refusals_write_nothing(world):
    mine = ask(world, "ada", "k1", "q-a")
    theirs = ask(world, "bob", "k2", "q-b")
    info = execute(world["ada"], "send_user_message", {"request_key": "k3", "project_id": "alpha", "subject": "S", "body": "b",
                   "segments": [{"kind": "info", "id": "i-1", "text": "fyi"}]}, allow_write=True)["message_id"]
    answered = ask(world, "ada", "k4", "q-c")
    answer(world, "r1", (answered, "q-c"))
    cases = [((theirs, "q-b"), "not_own_question"), ((mine, "nope"), "unknown_question"),
             (("no-such-message", "q-a"), "unknown_question"), ((info, "i-1"), "not_a_question"),
             ((answered, "q-c"), "question_answered")]
    for index, ((message_id, segment_id), code) in enumerate(cases):
        with pytest.raises(StateGraphError, match=code):
            withdraw(world, "ada", f"bad-{index}", message_id, segment_id)
    with pytest.raises(StateGraphError, match="driver_identity_required"):
        execute(world["user"], "withdraw_user_question", {"request_key": "u", "project_id": "alpha", "message_id": mine,
                "segment_id": "q-a", "reason": "x"}, allow_write=True)
    with world["ada"].store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM driver_inbox_messages WHERE withdraws_json IS NOT NULL").fetchone()[0] == 0
    assert execute(world["ada"], "list_user_questions", {"project_id": "alpha"})["counts"] == {"open": 2, "answered": 1, "withdrawn": 0}


def test_an_answer_after_a_withdrawal_wins(world):
    m = ask(world, "ada", "k1", "q-a")
    withdraw(world, "ada", "w1", m, "q-a")
    answer(world, "r1", (m, "q-a"))
    listed = execute(world["ada"], "list_user_questions", {"project_id": "alpha", "status": "all"})["questions"]
    assert [q["status"] for q in listed] == ["answered"]
    with world["ada"].store.transaction() as conn:
        assert open_question_count(conversation_rows(conn, "alpha")) == 0


def test_ask_owner_is_a_thin_wrapper_the_stored_bytes_equal_a_direct_question_block(world):
    """The adapter's ask_owner request (pinned in a fixture its node test asserts it
    produces) stores the same row bytes as a direct send_user_message carrying the same
    question block: segment id = question id, nothing added, nothing renamed."""
    fixture = json.loads((Path(__file__).resolve().parents[2] / "integrations" / "dsh-driver" / "test-fixtures"
                          / "ask-owner-request.json").read_text())
    request = dict(fixture["request"])
    via_wrapper = execute(world["ada"], "send_user_message", request, allow_write=True)["message_id"]
    direct = execute(world["bob"], "send_user_message", {
        "request_key": "direct", "project_id": "alpha", "subject": "Question for you",
        "body": fixture["question"]["context"],
        "segments": [{"kind": "question", "id": fixture["question"]["id"], "text": fixture["question"]["text"]}]},
        allow_write=True)["message_id"]
    with world["ada"].store.transaction() as conn:
        rows = {r["message_id"]: r for r in conn.execute(
            "SELECT message_id,subject,body,segments_json,answers_json,withdraws_json,refs_json,audience,kind "
            "FROM driver_inbox_messages WHERE message_id IN (?,?)", (via_wrapper, direct))}
    stored = lambda mid: {k: rows[mid][k] for k in ("subject", "body", "segments_json", "answers_json", "withdraws_json",
                                                   "refs_json", "audience", "kind")}
    assert stored(via_wrapper) == stored(direct)
    assert json.loads(rows[via_wrapper]["segments_json"]) == [
        {"id": fixture["question"]["id"], "kind": "question", "text": fixture["question"]["text"]}]
