"""Step 24 (design-steward 7.8.9, tests 7.8.18 "确认"): outward actions asked for in the
family group, confirmed there by the person who asked - and 25.3 (7.9.6): a calendar's
write marked outward needs that confirmation in an owner turn and in a group turn.

The requester of a group turn is the ONE sender of the turn's questions (step 23's audience
record and the questions the turn took), its conversation the group; the text is code's,
says `@<管家> 确认 K…`, is at most one WeChat message (1400), passes the group exit check in
the same transaction (else `withheld`: nothing created to confirm, nothing sent, the text kept
for the owner) and spends the group's send budget (20 an hour, apart from the 60 questions).
The exact reply is decided in relay_group_message before the quotas, and the group gets one
line written by code."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from core import state_channels, state_groups, state_steward_confirmations as sconf
from core.state_commands import execute
from core.state_graph import StateGraphError
from tests.unit.test_steward_connections import (ADDRESSES, FakeUpstream, add, allow, body_of, discover, rpc,  # noqa: F401
                                                 server, world)
from tests.unit.test_steward_oauth_confirm import SEND, bind_slack, dashboard, mail_ready, relay, status_of

OWNER_WX, MEMBER, MEMBER2, ROOM = "wxid_owner", "wxid_m1", "wxid_m2", "r1@chatroom"


def group_ready(world, monkeypatch):
    """mailpub (public, send_message outward) and mailme (private), the owner's WeChat
    account, and the family group bound with the @ prefix 管家 answers to."""
    mail_ready(world, conn_id="mailme", visibility="private")
    mail_ready(world)
    bind_slack(world, monkeypatch, sender=OWNER_WX, channel="wechat")
    code = world.owner.groups.issue_group_code("wechat", "家庭群")["code"]
    execute(world.steward, "bind_owner_group", {"request_key": "bg-1", "channel": "wechat", "group_ref": ROOM,
                                                "sender_ref": OWNER_WX, "code": code, "mention_prefix": "@管家"},
            allow_write=True)


def gask(world, ref, body="帮我发封邮件", sender=MEMBER, at="now"):
    channel_at = state_channels._now().isoformat() if at == "now" else at
    return execute(world.steward, "relay_group_message", {
        "request_key": f"g-{ref}", "channel": "wechat", "group_ref": ROOM, "sender_ref": sender,
        "channel_message_ref": ref, "body": body, **({"channel_at": channel_at} if channel_at else {})}, allow_write=True)


def group_turn(world, senders=(MEMBER,), turn="gt1"):
    execute(world.steward, "set_steward_turn_audience", {"turn": turn, "audience": "group", "group": {
        "channel": "wechat", "group_ref": ROOM}, "sender_refs": list(senders)}, allow_write=True)


def request(world, question_id, arguments=None, name=SEND, question_ids=None):
    return execute(world.steward, "create_steward_confirmation", {
        "name": name, "arguments": arguments if arguments is not None else {"to": "a@example.com", "body": "周六聚餐"},
        "requester_message_id": question_id, "audience": "group",
        **({"question_ids": question_ids} if question_ids is not None else {})}, allow_write=True)


def out_rows(world, kind=None):
    with world.db.get_connection() as conn:
        rows = conn.execute("SELECT kind, body, request_key FROM state_group_messages WHERE direction='out' "
                            "AND kind<>'bind_notice' ORDER BY rowid").fetchall()
    return [tuple(r) for r in rows if kind is None or r[0] == kind]


def record(world, code):
    with world.db.get_connection() as conn:
        return dict(conn.execute("SELECT * FROM state_steward_confirmations WHERE code=?", (code,)).fetchone())


# --- the requester -------------------------------------------------------------------------------

def test_the_requester_of_a_group_turn_is_its_one_asker_and_the_conversation_is_the_group(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    asked = request(world, question, {"to": "stranger@example.org", "body": "周六聚餐"})
    # Any recipient: no list (owner 2026-11-20, 待定 1) - the asker confirms it.
    assert asked["channel"] == "wechat" and asked["group_ref"] == ROOM and asked["reply_to_message_id"] == question
    row = record(world, asked["code"])
    assert (row["requester_kind"], row["channel"], row["conversation_ref"], row["sender_ref"], row["audience"],
            row["status"]) == ("member", "wechat", ROOM, MEMBER, "group", "pending")
    # The owner asking in the group is the owner, in the group.
    owner_q = gask(world, "q2", sender=OWNER_WX)["message_id"]
    group_turn(world, senders=(OWNER_WX,), turn="gt2")
    owner_row = record(world, request(world, owner_q)["code"])
    assert (owner_row["requester_kind"], owner_row["conversation_ref"], owner_row["sender_ref"]) == ("owner", ROOM, OWNER_WX)


def test_two_askers_in_one_turn_are_ambiguous_and_a_question_not_of_the_turn_is_no_requester(world, monkeypatch):
    group_ready(world, monkeypatch)
    mine, theirs = gask(world, "q1")["message_id"], gask(world, "q2", sender=MEMBER2)["message_id"]
    group_turn(world, senders=(MEMBER, MEMBER2))
    with pytest.raises(StateGraphError, match="ambiguous_requester"):
        request(world, theirs)
    # The record names only the first step's sender; a question merged later from someone else
    # (the turn's question ids) makes it ambiguous all the same.
    group_turn(world, senders=(MEMBER,), turn="gt2")
    with pytest.raises(StateGraphError, match="ambiguous_requester"):
        request(world, mine, question_ids=[mine, theirs])
    with pytest.raises(StateGraphError, match="no_requester"):
        request(world, "gmsg-not-a-question")
    # The host's word "group" against a turn recorded as the owner's, and the other way round.
    execute(world.steward, "set_steward_turn_audience", {"turn": "gt3", "audience": "owner"}, allow_write=True)
    with pytest.raises(StateGraphError, match="ambiguous_requester"):
        request(world, mine)
    group_turn(world, turn="gt4")
    with pytest.raises(StateGraphError, match="ambiguous_requester"):
        execute(world.steward, "create_steward_confirmation", {"name": SEND, "arguments": {}, "requester_message_id":
                dashboard(world, "go", "o-1")["message_id"]}, allow_write=True)
    assert request(world, mine, question_ids=[mine])["code"]


def test_a_private_connection_is_not_confirmable_in_a_group_turn(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    with pytest.raises(StateGraphError, match="not_visible_in_this_turn"):
        request(world, question, name="mcp__sw__mailme__send_message")


# --- the text, the exit check, the budget ---------------------------------------------------------

def test_the_group_text_says_at_the_steward_confirm_and_is_one_wechat_message(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    asked = request(world, question)
    code, text = asked["code"], asked["text"]
    assert text.split("\n")[0] == (f"请确认（{code}，10 分钟内有效）：用「家庭公共邮箱」执行 send_message。"
                                   f"回复「@管家 确认 {code}」执行，「@管家 取消 {code}」作废。")
    assert text.count(f"确认 {code}") == 1
    # The group's copy is one outbound row with the request key confirm:<code>, the text as sent.
    assert out_rows(world) == [("confirmation", text, f"confirm:{code}")]
    # 1400 in a group (4000 elsewhere).
    with pytest.raises(StateGraphError, match="too_long_to_confirm.*at most 1400"):
        request(world, question, {"to": "a@example.com", "body": "长" * 1500})


@pytest.mark.parametrize("arguments,reason", [
    ({"to": "a@example.com", "body": "alpha 做到哪了"}, "private_project"),
    ({"to": "a@example.com", "body": "见 /home/linxuhao/x"}, "host_path"),
])
def test_the_exit_check_runs_in_the_same_transaction_and_withholds_everything(world, monkeypatch, arguments, reason):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    answer = request(world, question, arguments)
    assert answer == {"refused": "withheld", "reason": state_groups.WITHHELD_REASONS[reason]}
    assert out_rows(world) == [], "nothing goes to the group"
    # Kept for the owner's panel, never confirmable.
    listed = world.owner.steward_confirmations.list_recent()["confirmations"]
    assert len(listed) == 1 and listed[0]["status"] == "cancelled" and listed[0]["result"] == {"withheld": reason}
    code = listed[0]["code"]
    assert arguments["body"] in listed[0]["message_text"]
    assert "confirmation" not in gask(world, "c1", f"确认 {code}")


def test_a_live_code_in_the_text_is_withheld_and_retired(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    live = world.owner.channels.issue_code("slack")["code"]
    assert request(world, question, {"to": "a@example.com", "body": f"码 {live}"})["refused"] == "withheld"
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT retired_at FROM state_owner_channel_codes WHERE kind='account' AND channel='slack' "
                            "AND used_at IS NULL").fetchone()[0] is not None, "the retirement is committed"


def test_the_group_send_budget_is_twenty_an_hour_apart_from_the_questions(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    now = state_channels._now()
    with world.db.get_connection() as conn:
        for n in range(19):
            conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,body,at) "
                         "VALUES(?,?,?,'out',?,1,'x',?)", (f"o{n}", "wechat", ROOM,
                                                           "confirmation" if n % 2 else "confirmation_line", now.isoformat()))
        # Replies and an older hour count nothing here.
        conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,body,at) "
                     "VALUES('r1','wechat',?,'out','reply',1,'x',?)", (ROOM, now.isoformat()))
        conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,body,at) "
                     "VALUES('old','wechat',?,'out','confirmation',1,'x',?)", (ROOM, (now - timedelta(hours=2)).isoformat()))
        conn.commit()
    code = request(world, question)["code"]                      # the 20th
    with pytest.raises(StateGraphError, match="group_send_budget"):
        request(world, question, {"to": "b@example.com"})       # the 21st
    # The reply still decides; no line goes out over the budget.
    answer = gask(world, "c1", f"确认 {code}")
    assert answer["confirmation"]["outcome"] == "confirmed" and "confirmation_line" not in answer
    assert state_groups.GROUP_LIMITS["group_sends_per_hour"] == 20
    assert set(state_groups.GROUP_SEND_KINDS) == {"confirmation", "confirmation_line"}


def test_the_accounts_outbound_cap_holds_a_confirmation_too(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    now = state_channels._now().isoformat()
    with world.db.get_connection() as conn:
        for n in range(state_groups.GROUP_LIMITS["account_out_per_hour"]):
            conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,body,at) "
                         "VALUES(?,'wechat',?,'out','reply',1,'x',?)", (f"o{n}", ROOM, now))
        conn.commit()
    with pytest.raises(StateGraphError, match="group_quota_account"):
        request(world, question)


# --- the reply in the group -------------------------------------------------------------------------

def test_the_exact_reply_of_the_asker_in_the_group_confirms_and_the_group_gets_one_line(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    code = request(world, question)["code"]
    # Not the exact phrase (the bridge strips the @ prefix; anything else is an ordinary question).
    for n, body in enumerate((f"@管家 确认 {code}", "确认" + code, f"确认 {code.lower()}", f"好的 确认 {code}")):
        assert "confirmation" not in gask(world, f"x{n}", body)
    # Someone else in the group, or the owner in a private chat, never confirms the asker's.
    assert "confirmation" not in gask(world, "x9", f"确认 {code}", sender=MEMBER2)
    assert "confirmation" not in gask(world, "x10", f"确认 {code}", sender=OWNER_WX)
    assert "confirmation" not in relay(world, f"确认 {code}", "p1", channel="wechat", sender=OWNER_WX)
    assert status_of(world, code) == "pending"
    answer = gask(world, "ok", f"  确认 {code} ")
    assert answer["confirmation"] == {"code": code, "action": "confirm", "outcome": "confirmed"}
    assert answer["confirmation_line"] == f"已确认 {code}，正在执行。"
    assert answer["message_id"], "the reply is still a question: it wakes the steward, which runs it"
    assert out_rows(world, "confirmation_line") == [("confirmation_line", f"已确认 {code}，正在执行。", None)]
    assert record(world, code)["confirm_message_ref"] == answer["message_id"]
    assert status_of(world, code) == "confirmed"
    assert "confirmation" not in gask(world, "ok2", f"确认 {code}"), "a second reply is an ordinary question"


def test_the_reply_is_decided_before_the_quotas_and_cancel_works_the_same_way(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    code = request(world, question)["code"]
    other = request(world, question, {"to": "b@example.com"})["code"]
    for n in range(state_groups.GROUP_LIMITS["member_per_hour"] - 1):
        gask(world, f"fill{n}", f"问题 {n}")
    over = gask(world, "c1", f"确认 {code}")
    assert over["refused"] == "member_quota" and over["confirmation"]["outcome"] == "confirmed"
    assert over["confirmation_line"] == f"已确认 {code}，正在执行。"
    cancelled = gask(world, "c2", f"取消 {other}")
    assert cancelled["confirmation"]["outcome"] == "cancelled"
    assert cancelled["confirmation_line"] == f"已取消 {other}，不会执行。"
    assert status_of(world, other) == "cancelled"


def test_a_group_code_is_never_confirmed_in_a_private_chat_nor_a_private_one_in_the_group(world, monkeypatch):
    group_ready(world, monkeypatch)
    # The owner asks in the group: the code belongs to the group conversation.
    question = gask(world, "q1", sender=OWNER_WX)["message_id"]
    group_turn(world, senders=(OWNER_WX,))
    in_group = request(world, question)["code"]
    assert "confirmation" not in relay(world, f"确认 {in_group}", "p1", channel="wechat", sender=OWNER_WX)
    # The owner asks in the private chat: its code is not confirmed from the group.
    execute(world.steward, "set_steward_turn_audience", {"turn": "o1", "audience": "owner"}, allow_write=True)
    private = execute(world.steward, "create_steward_confirmation", {
        "name": SEND, "arguments": {"to": "a@example.com"}, "requester_message_id":
        relay(world, "send it", "p2", channel="wechat", sender=OWNER_WX)["message_id"]}, allow_write=True)["code"]
    assert "confirmation" not in gask(world, "g2", f"确认 {private}", sender=OWNER_WX)
    assert status_of(world, in_group) == status_of(world, private) == "pending"
    assert gask(world, "g3", f"确认 {in_group}", sender=OWNER_WX)["confirmation"]["outcome"] == "confirmed"
    assert relay(world, f"确认 {private}", "p3", channel="wechat", sender=OWNER_WX)["confirmation"]["outcome"] == "confirmed"


def test_a_late_reply_in_the_group_is_expired(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    code = request(world, question)["code"]
    late = state_channels._now() + timedelta(minutes=11)
    monkeypatch.setattr(state_channels, "_now", lambda: late)
    monkeypatch.setattr(sconf, "_utcnow", lambda: late)
    answer = gask(world, "late", f"确认 {code}", at=late.isoformat())
    assert answer["confirmation"]["outcome"] == "expired"
    assert answer["confirmation_line"] == f"{code} 已过期，未执行；请重新请求。"


# --- 25.3 calendar (7.9.6): a stub calendar MCP behind the gateway ------------------------------------

CALENDAR = [
    {"name": "list_events", "description": "List events", "inputSchema": {"type": "object", "properties": {
        "day": {"type": "string"}}}, "annotations": {"readOnlyHint": True}},
    {"name": "create_event", "description": "Create an event and invite people", "inputSchema": {"type": "object",
     "properties": {"title": {"type": "string"}, "start": {"type": "string"}, "attendees": {"type": "array"}}}},
]


@pytest.fixture
def upstream():
    return FakeUpstream(CALENDAR)


def calendar_ready(world, monkeypatch):
    """calme 「我的日历」 private and calfam 「家庭日历」 public (7.9.6): reads free, writes outward."""
    monkeypatch.setitem(ADDRESSES, "good.example", "93.184.216.35")
    for conn_id, label, visibility in (("calme", "我的日历", "private"), ("calfam", "家庭日历", "public")):
        add(world, conn_id=conn_id, label=label, url="https://good.example/mcp", auth_kind="header", visibility=visibility)
        execute(world.owner, "steward_connection_secret_set", {"conn_id": conn_id, "value": "Bearer x"}, allow_write=True)
        discover(world, conn_id, tools=CALENDAR)
        allow(world, conn_id, names=["list_events", "create_event"])
    bind_slack(world, monkeypatch, sender=OWNER_WX, channel="wechat")
    code = world.owner.groups.issue_group_code("wechat", "家庭群")["code"]
    execute(world.steward, "bind_owner_group", {"request_key": "bg-1", "channel": "wechat", "group_ref": ROOM,
                                                "sender_ref": OWNER_WX, "code": code, "mention_prefix": "@管家"},
            allow_write=True)


def test_a_calendar_write_marked_outward_needs_the_askers_confirmation_in_owner_and_group_turns(world, server, upstream,
                                                                                               monkeypatch):
    calendar_ready(world, monkeypatch)
    tools = {t["raw_name"]: t for t in execute(world.owner, "list_steward_connection_tools", {"conn_id": "calfam"})["tools"]}
    assert tools["create_event"]["outward"] is True and tools["list_events"]["outward"] is False
    event = {"title": "周六聚餐", "start": "2026-11-28T18:00:00+08:00", "attendees": ["grandma@example.org"]}

    def call(name, arguments):
        return body_of(rpc(server, world, "tools/call", {"name": name, "arguments": arguments}))[0]

    # An owner turn: reads run; a write on either calendar waits for the owner's confirmation.
    execute(world.steward, "set_steward_turn_audience", {"turn": "o1", "audience": "owner"}, allow_write=True)
    assert "result" in call("calme__list_events", {"day": "2026-11-28"})
    for conn_id in ("calme", "calfam"):
        assert call(f"{conn_id}__create_event", event)["error"]["data"]["code"] == "confirmation_required"
    code = execute(world.steward, "create_steward_confirmation", {
        "name": "mcp__sw__calme__create_event", "arguments": event,
        "requester_message_id": dashboard(world, "周六聚餐加到我的日历", "o-1")["message_id"]}, allow_write=True)["code"]
    assert dashboard(world, f"确认 {code}", "o-2")["confirmation"]["outcome"] == "confirmed"
    assert "result" in call("calme__create_event", event)
    assert status_of(world, code) == "done"

    # A group turn: the private calendar is not there at all; the family calendar reads freely
    # and writes only after the member who asked confirms in the group.
    question = gask(world, "q1", "周六聚餐加到家庭日历")["message_id"]
    group_turn(world)
    assert call("calme__list_events", {"day": "2026-11-28"})["error"]["data"]["code"] == "not_visible_in_this_turn"
    assert call("calme__create_event", event)["error"]["data"]["code"] == "not_visible_in_this_turn"
    assert "result" in call("calfam__list_events", {"day": "2026-11-28"})
    before = upstream.methods.count("tools/call")
    assert call("calfam__create_event", event)["error"]["data"]["code"] == "confirmation_required"
    assert upstream.methods.count("tools/call") == before
    with pytest.raises(StateGraphError, match="not_visible_in_this_turn"):
        request(world, question, event, name="mcp__sw__calme__create_event")
    asked = request(world, question, event, name="mcp__sw__calfam__create_event")
    assert "@管家 确认" in asked["text"] and "attendees" in asked["text"]
    assert gask(world, "c1", f"确认 {asked['code']}")["confirmation"]["outcome"] == "confirmed"
    assert "result" in call("calfam__create_event", event)
    assert upstream.methods.count("tools/call") == before + 1
    assert call("calfam__create_event", event)["error"]["data"]["code"] == "confirmation_required", "one confirmation, one write"
    assert json.loads(record(world, asked["code"])["args_json"]) == event
