"""Step 24 (design-steward 7.8.9, tests 7.8.18 "确认"): outward actions asked for in the
family group, confirmed there by the person who asked - and 25.3 (7.9.6): a calendar's
write marked outward needs that confirmation in an owner turn and in a group turn.

The requester of a group turn is the ONE sender of the turn's questions (step 23's audience
record and the questions the turn took), its conversation the group; the text is code's,
says `@<管家> 确认 K…`, is at most one WeChat message (1400), passes the group exit check in
the same transaction (else `withheld`: nothing created to confirm, nothing sent, the text kept
for the owner) and spends the group's send budget (20 an hour, apart from the 60 questions).
The exact reply is decided in relay_group_message past the account cap and before the group and
member quotas (a confirming reply is a question past those, so it always wakes the turn that
runs it), and the group gets one
line written by code."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from core import owner_text, state_channels, state_groups, state_steward_confirmations as sconf
from core.state_commands import execute
from core.state_graph import StateGraphError
from tests.unit.test_steward_connections import (ADDRESSES, FakeUpstream, add, allow, body_of, discover, rpc,  # noqa: F401
                                                 server, world)
from tests.unit.test_steward_oauth_confirm import SEND, bind_slack, dashboard, mail_ready, relay, status_of


@pytest.fixture(autouse=True)
def _zh_owner():
    """These tests pin the lines a 'zh' owner reads (core.owner_text; this deployment's owner).
    test_owner_text covers the table and the English default."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(owner_text.SETTING, "zh")
        yield


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
    # Step 23 Slack (D8): it names the question's channel ref, so a channel that threads answers under it.
    assert asked["reply_to_ref"] == "q1"
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
    # design/steward-group-media.md section 2: the media switch's notice is held to the same budget.
    assert set(state_groups.GROUP_SEND_KINDS) == {"confirmation", "confirmation_line", "media", "media_notice"}


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
    # Past the member quota a cancel is decided and refused: its line needs no turn.
    cancelled = gask(world, "c2", f"取消 {other}")
    assert cancelled["refused"] == "member_quota" and cancelled["confirmation"]["outcome"] == "cancelled"
    assert cancelled["confirmation_line"] == f"已取消 {other}，不会执行。"
    assert status_of(world, other) == "cancelled"
    # A reply that confirmed is a question past the quotas: 「正在执行」 always comes with a wake
    # (review F1/F2) - never a refused message the steward never sees.
    over = gask(world, "c1", f"确认 {code}")
    assert "refused" not in over and over["message_id"] and over["confirmation"]["outcome"] == "confirmed"
    assert over["confirmation_line"] == f"已确认 {code}，正在执行。"
    assert record(world, code)["confirm_message_ref"] == over["message_id"]


def test_past_the_group_quota_a_confirming_reply_is_still_the_question_that_runs_it(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    code = request(world, question)["code"]
    now = state_channels._now().isoformat()
    with world.db.get_connection() as conn:
        for n in range(state_groups.GROUP_LIMITS["group_per_hour"]):
            conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,"
                         "sender_ref,body,at) VALUES(?,'wechat',?,'in','question',1,?,'x',?)",
                         (f"i{n}", ROOM, MEMBER2, now))
        conn.commit()
    assert gask(world, "plain", "还有一个问题")["refused"] == "group_quota"
    answer = gask(world, "ok", f"确认 {code}")
    assert "refused" not in answer and answer["message_id"]
    assert answer["confirmation_line"] == f"已确认 {code}，正在执行。"


def test_past_the_account_cap_a_reply_decides_nothing_and_its_line_never_refuses_it(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    code = request(world, question)["code"]
    cap = state_groups.GROUP_LIMITS["account_out_per_hour"]
    now = state_channels._now().isoformat()

    def fill(upto):
        with world.db.get_connection() as conn:
            have = conn.execute("SELECT COUNT(*) FROM state_group_messages WHERE direction='out'").fetchone()[0]
            for n in range(have, upto):
                conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,"
                             "body,at) VALUES(?,'wechat',?,'out','reply',1,'x',?)", (f"o{n}", ROOM, now))
            conn.commit()
    fill(cap)
    # At the cap nothing can go out: the reply is refused before it decides; the record waits.
    refused = gask(world, "late", f"确认 {code}")
    assert refused["refused"] == "group_quota_account" and "confirmation" not in refused
    assert "confirmation_line" not in refused and status_of(world, code) == "pending"
    with world.db.get_connection() as conn:
        conn.execute("DELETE FROM state_group_messages WHERE message_id=?", (f"o{cap - 1}",))
        conn.commit()
    # One below the cap: the line makes the cap, and its own reply is still the accepted question.
    answer = gask(world, "ok", f"确认 {code}")
    assert "refused" not in answer and answer["message_id"]
    assert answer["confirmation_line"] == f"已确认 {code}，正在执行。" and status_of(world, code) == "confirmed"


def test_a_group_confirmation_that_never_reached_the_group_shows_as_not_delivered(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    created = request(world, question)
    code = created["code"]
    assert execute(world.steward, "cancel_steward_confirmation", {"code": code}, allow_write=True)["status"] == "cancelled"
    with world.db.get_connection() as conn:
        row = conn.execute("SELECT body, original, withheld_reason FROM state_group_messages WHERE direction='out' "
                           "AND request_key=?", (f"confirm:{code}",)).fetchone()
    # Kept (the send budget still counts it), marked for the owner's transcript.
    assert tuple(row) == (created["text"], created["text"], "not_delivered")


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


def test_a_reply_from_the_future_is_refused_in_plain_words(world, monkeypatch):
    group_ready(world, monkeypatch)
    question = gask(world, "q1")["message_id"]
    group_turn(world)
    code = request(world, question)["code"]
    ahead = (state_channels._now() + sconf.CLOCK_SKEW + timedelta(minutes=5)).isoformat()
    answer = gask(world, "skew", f"确认 {code}", at=ahead)
    assert answer["confirmation"]["refusal"] == "confirmation_clock_skew"
    # The family group never reads the internal token (review F4).
    assert answer["confirmation_line"] == f"{code} 未执行：消息时间不对，请重新回复。"


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
    assert status_of(world, code) == "confirmed"  # run_confirmed's read in o1 arms it
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
    assert status_of(world, asked["code"]) == "confirmed"  # armed in the group's own turn gt1
    assert "result" in call("calfam__create_event", event)
    assert upstream.methods.count("tools/call") == before + 1
    assert call("calfam__create_event", event)["error"]["data"]["code"] == "confirmation_required", "one confirmation, one write"
    assert json.loads(record(world, asked["code"])["args_json"]) == event


def test_two_groups_a_code_confirmed_in_one_group_runs_only_in_that_groups_turn(world, server, upstream, monkeypatch):
    """Step 23.1 (7.8.9 steps 5 and 6, 7.7.24 item 4): a code group A's asker confirmed is not
    run in group B's turn - run_confirmed's read is not_this_conversation and the gateway takes
    no confirmation of another conversation; an owner turn may run it (director 581), told which
    group and who confirmed; A's own later turn runs it."""
    calendar_ready(world, monkeypatch)
    code = world.owner.groups.issue_group_code("wechat", "老家群")["code"]
    execute(world.steward, "bind_owner_group", {"request_key": "bg-2", "channel": "wechat", "group_ref": "r2@chatroom",
                                                "sender_ref": OWNER_WX, "code": code, "mention_prefix": "@管家"},
            allow_write=True)
    event = {"title": "周六聚餐", "start": "2026-11-28T18:00:00+08:00"}

    def call(name, arguments):
        return body_of(rpc(server, world, "tools/call", {"name": name, "arguments": arguments}))[0]

    def get(code, turn):
        return execute(world.steward, "get_steward_confirmation", {"code": code, "turn": turn}, allow_write=True)

    question = gask(world, "q1", "周六聚餐加到家庭日历")["message_id"]
    group_turn(world, turn="ga")
    asked = request(world, question, event, name="mcp__sw__calfam__create_event")["code"]
    assert gask(world, "c1", f"确认 {asked}")["confirmation"]["outcome"] == "confirmed"
    # Group B's turn: someone there types A's code.
    execute(world.steward, "relay_group_message", {
        "request_key": "g-b1", "channel": "wechat", "group_ref": "r2@chatroom", "sender_ref": MEMBER2,
        "channel_message_ref": "b1", "body": f"确认 {asked}", "channel_at": state_channels._now().isoformat()},
        allow_write=True)
    execute(world.steward, "set_steward_turn_audience", {"turn": "gb", "audience": "group", "group": {
        "channel": "wechat", "group_ref": "r2@chatroom"}, "sender_refs": [MEMBER2]}, allow_write=True)
    with pytest.raises(StateGraphError, match="not_this_conversation"):
        get(asked, "gb")
    before = upstream.methods.count("tools/call")
    assert call("calfam__create_event", event)["error"]["data"]["code"] == "confirmation_required"
    assert upstream.methods.count("tools/call") == before and record(world, asked)["status"] == "confirmed"
    # A turn that is not the recorded one cannot be told: refused too.
    with pytest.raises(StateGraphError, match="not_this_conversation"):
        get(asked, "ga")
    # The owner's turn may run it, knowingly: the read names the group and who confirmed it.
    execute(world.steward, "set_steward_turn_audience", {"turn": "o1", "audience": "owner"}, allow_write=True)
    told = get(asked, "o1")
    assert (told["status"], told["group"], told["confirmed_by"]) == ("confirmed", "家庭群", "成员甲")
    # A's own later turn runs it (the read names no group there).
    group_turn(world, turn="ga2")
    mine = get(asked, "ga2")
    assert mine["status"] == "confirmed" and "group" not in mine
    assert "result" in call("calfam__create_event", event)
    assert upstream.methods.count("tools/call") == before + 1


def test_an_owner_turn_that_runs_a_groups_confirmation_is_told_whose_it_was_and_a_direct_call_takes_none(
        world, server, upstream, monkeypatch):
    """Review PRIV-1 (director 581): an owner turn may run what a family group confirmed, but
    never silently - run_confirmed's read in the owner's turn arms the group's record, and the
    call that consumes it gets, first in its result, the group and who confirmed it; on a tool
    error and on an upstream JSON-RPC error too. The owner's own record adds nothing. Director 602
    claim 7: a direct call with the same arguments that no run_confirmed armed consumes nothing."""
    calendar_ready(world, monkeypatch)
    event = {"title": "周六聚餐", "start": "2026-11-28T18:00:00+08:00"}
    line = "[This action was asked for and confirmed in the family group 「家庭群」 by 成员甲; it ran in your owner's turn.]"

    def call(name, arguments):
        return body_of(rpc(server, world, "tools/call", {"name": name, "arguments": arguments}))[0]

    def group_confirmed(ref):
        question = gask(world, f"q-{ref}", "周六聚餐加到家庭日历")["message_id"]
        group_turn(world, turn=f"g-{ref}")
        asked = request(world, question, event, name="mcp__sw__calfam__create_event")["code"]
        assert gask(world, f"c-{ref}", f"确认 {asked}")["confirmation"]["outcome"] == "confirmed"
        execute(world.steward, "set_steward_turn_audience", {"turn": f"o-{ref}", "audience": "owner"},
                allow_write=True)
        before = upstream.methods.count("tools/call")
        assert call("calfam__create_event", event)["error"]["data"]["code"] == "confirmation_required"
        assert upstream.methods.count("tools/call") == before, "a direct call that nothing armed sends nothing"
        assert status_of(world, asked) == "confirmed"  # run_confirmed's read in the owner's turn arms it
        return asked

    asked = group_confirmed("1")
    before = upstream.methods.count("tools/call")
    answer = call("calfam__create_event", event)
    assert upstream.methods.count("tools/call") == before + 1
    assert answer["result"]["content"][0] == {"type": "text", "text": line}
    assert "echo" in answer["result"]["content"][1]["text"], "the call's own content follows"
    assert status_of(world, asked) == "done"
    # A tool error: the line still comes first.
    asked = group_confirmed("2")
    upstream.call_answer = {"result": {"content": [{"type": "text", "text": "calendar full"}], "isError": True}}
    answer = call("calfam__create_event", event)
    assert answer["result"]["isError"] is True
    assert [c["text"] for c in answer["result"]["content"]] == [line, "calendar full"]
    assert status_of(world, asked) == "failed"
    # An upstream JSON-RPC error: the line is in the error message.
    asked = group_confirmed("3")
    upstream.call_answer = {"error": {"code": -32010, "message": "nope"}}
    answer = call("calfam__create_event", event)
    assert answer["error"]["data"]["code"].startswith("upstream_error") and line in answer["error"]["message"]
    assert status_of(world, asked) == "failed"
    # The owner's own confirmation: no line.
    upstream.call_answer = None
    code = execute(world.steward, "create_steward_confirmation", {
        "name": "mcp__sw__calfam__create_event", "arguments": event,
        "requester_message_id": dashboard(world, "周六聚餐加到家庭日历", "o-9")["message_id"]}, allow_write=True)["code"]
    assert dashboard(world, f"确认 {code}", "o-10")["confirmation"]["outcome"] == "confirmed"
    assert status_of(world, code) == "confirmed"
    answer = call("calfam__create_event", event)
    assert "result" in answer and "family group" not in json.dumps(answer, ensure_ascii=False)


def test_the_gateway_judges_a_consumption_by_the_calls_own_turn_never_the_one_audience_row(world, server, upstream,
                                                                                          monkeypatch):
    """Director 602 claim 7: the gateway's request carries no turn, so it never takes the one-row
    turn audience record as its scope: run_confirmed's read names the call's own turn and arms
    the record for it, and only an armed record is consumed. An owner audience never makes every
    record consumable (its scope is never empty): a group's record nobody armed is not taken by
    an owner turn's direct call, nor by another group's. An arming is consumed only in the turn
    that made it (按 turn 判, guaranteed by code, failing closed): recording the next turn - group
    B's, or the owner's - disarms it, so that turn's direct call takes nothing (the owner's would
    otherwise run a group's action silently, PRIV-1) and the record stays confirmed; an arming
    whose turn is not the recorded one is not consumed even if it was never disarmed. In its own
    turn it runs. An arming older than ARMED is not consumed."""
    calendar_ready(world, monkeypatch)
    code = world.owner.groups.issue_group_code("wechat", "老家群")["code"]
    execute(world.steward, "bind_owner_group", {"request_key": "bg-2", "channel": "wechat", "group_ref": "r2@chatroom",
                                                "sender_ref": OWNER_WX, "code": code, "mention_prefix": "@管家"},
            allow_write=True)
    event = {"title": "周六聚餐", "start": "2026-11-28T18:00:00+08:00"}

    def call():
        return body_of(rpc(server, world, "tools/call", {"name": "calfam__create_event", "arguments": event}))[0]

    def get(code, turn):
        return execute(world.steward, "get_steward_confirmation", {"code": code, "turn": turn}, allow_write=True)

    question = gask(world, "q1", "周六聚餐加到家庭日历")["message_id"]
    group_turn(world, turn="ga")
    asked = request(world, question, event, name="mcp__sw__calfam__create_event")["code"]
    assert gask(world, "c1", f"确认 {asked}")["confirmation"]["outcome"] == "confirmed"
    # An owner audience with the record not armed: nothing is consumed.
    execute(world.steward, "set_steward_turn_audience", {"turn": "o1", "audience": "owner"}, allow_write=True)
    before = upstream.methods.count("tools/call")
    assert call()["error"]["data"]["code"] == "confirmation_required"
    # So in group B's turn.
    execute(world.steward, "set_steward_turn_audience", {"turn": "gb", "audience": "group", "group": {
        "channel": "wechat", "group_ref": "r2@chatroom"}, "sender_refs": [MEMBER2]}, allow_write=True)
    assert call()["error"]["data"]["code"] == "confirmation_required"
    assert upstream.methods.count("tools/call") == before and record(world, asked)["status"] == "confirmed"
    # A's own turn arms it; group B's turn, recorded next, disarms it: B's direct call takes nothing.
    group_turn(world, turn="ga2")
    assert get(asked, "ga2")["status"] == "confirmed"
    armed = record(world, asked)
    assert (armed["armed_turn"], armed["armed_owner"]) == ("ga2", 0)
    execute(world.steward, "set_steward_turn_audience", {"turn": "gb2", "audience": "group", "group": {
        "channel": "wechat", "group_ref": "r2@chatroom"}, "sender_refs": [MEMBER2]}, allow_write=True)
    assert (record(world, asked)["armed_turn"], record(world, asked)["armed_at"]) == (None, None)
    assert call()["error"]["data"]["code"] == "confirmation_required"
    assert upstream.methods.count("tools/call") == before and record(world, asked)["status"] == "confirmed"
    # So the owner's turn, recorded next: it never takes a group's arming, silently or otherwise.
    group_turn(world, turn="ga3")
    assert get(asked, "ga3")["status"] == "confirmed"
    execute(world.steward, "set_steward_turn_audience", {"turn": "o2", "audience": "owner"}, allow_write=True)
    assert record(world, asked)["armed_turn"] is None
    assert call()["error"]["data"]["code"] == "confirmation_required"
    assert upstream.methods.count("tools/call") == before and record(world, asked)["status"] == "confirmed"
    # An arming whose turn is not the recorded one is not consumed even if it was never disarmed.
    group_turn(world, turn="ga4")
    assert get(asked, "ga4")["status"] == "confirmed"
    with world.db.get_connection() as conn:
        conn.execute("UPDATE state_steward_turn_audience SET turn='o3', audience='owner', channel=NULL, "
                     "group_ref=NULL WHERE id=1")
        conn.commit()
    assert record(world, asked)["armed_turn"] == "ga4"
    assert call()["error"]["data"]["code"] == "confirmation_required"
    assert upstream.methods.count("tools/call") == before and record(world, asked)["status"] == "confirmed"
    # In its own turn the arming runs - also after that same turn is recorded again (the host's
    # record() on a merge step, or its retry): only another turn's recording disarms.
    group_turn(world, turn="ga5")
    assert get(asked, "ga5")["status"] == "confirmed"
    group_turn(world, senders=(MEMBER, MEMBER2), turn="ga5")
    assert record(world, asked)["armed_turn"] == "ga5"
    answer = call()
    assert "result" in answer and "family group" not in json.dumps(answer, ensure_ascii=False)
    assert upstream.methods.count("tools/call") == before + 1 and record(world, asked)["status"] == "done"
    # An arming older than ARMED is not consumed.
    question = gask(world, "q2", "再加一次")["message_id"]
    group_turn(world, turn="ga6")
    late = request(world, question, event, name="mcp__sw__calfam__create_event")["code"]
    assert gask(world, "c2", f"确认 {late}")["confirmation"]["outcome"] == "confirmed"
    assert get(late, "ga6")["status"] == "confirmed"
    with world.db.get_connection() as conn:
        conn.execute("UPDATE state_steward_confirmations SET armed_at=? WHERE code=?",
                     (sconf._iso(sconf._utcnow() - sconf.ARMED - timedelta(seconds=1)), late))
        conn.commit()
    assert call()["error"]["data"]["code"] == "confirmation_required"


def test_a_read_without_a_turn_is_the_old_read_only_one_and_arms_nothing(world, server, upstream, monkeypatch):
    """Director 602 defect 8: a caller of get_steward_confirmation that cannot name a turn gets the
    read of before step 23.1 - name, arguments and status, no error - and nothing it returns can
    be consumed: the read arms nothing."""
    calendar_ready(world, monkeypatch)
    event = {"title": "周六聚餐", "start": "2026-11-28T18:00:00+08:00"}
    execute(world.steward, "set_steward_turn_audience", {"turn": "o1", "audience": "owner"}, allow_write=True)
    code = execute(world.steward, "create_steward_confirmation", {
        "name": "mcp__sw__calfam__create_event", "arguments": event,
        "requester_message_id": dashboard(world, "周六聚餐加到家庭日历", "o-1")["message_id"]}, allow_write=True)["code"]
    assert dashboard(world, f"确认 {code}", "o-2")["confirmation"]["outcome"] == "confirmed"
    read = execute(world.steward, "get_steward_confirmation", {"code": code}, allow_write=True)
    assert (read["status"], read["name"], read["arguments"]) == ("confirmed", "mcp__sw__calfam__create_event", event)
    assert record(world, code)["armed_turn"] is None
    before = upstream.methods.count("tools/call")
    answer = body_of(rpc(server, world, "tools/call", {"name": "calfam__create_event", "arguments": event}))[0]
    assert answer["error"]["data"]["code"] == "confirmation_required"
    assert upstream.methods.count("tools/call") == before and record(world, code)["status"] == "confirmed"
