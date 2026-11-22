"""Step 23: the steward in the owner's family group (design-steward 7.7, WeChat first).

The steward's HOST relays a bound group's @-messages (relay_group_message) and posts the
reply a group turn ended with (post_group_reply, exit-checked); the owner binds the group
with a group code, names members and writes the family note on the dashboard; the family
memory is accepted only from a group turn (the public-read header and a recorded group
audience). Every number in GROUP_LIMITS is pinned here (platform director, 2026-11-20)."""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from core import state_channels, state_groups
from core.state_commands import ProjectPrivate, execute
from core.state_graph import StateConflict, StateGraphError, StateNotFound
from core.state_service import StateService
from tests.unit.test_steward_channels import (OWNER_HEADERS, bind, client, conversation, refused,  # noqa: F401
                                              world)

OWNER_WX, MEMBER, MEMBER2, ROOM, ROOM2 = "wxid_owner", "wxid_m1", "wxid_m2", "r1@chatroom", "r2@chatroom"
NOW = object()


def bound_group(world, room=ROOM, key="bg-1"):
    bind(world, "wechat", OWNER_WX, key="bind-wx")
    code = world.owner.groups.issue_group_code("wechat", "家庭群")["code"]
    return execute(world.steward, "bind_owner_group", {"request_key": key, "channel": "wechat", "group_ref": room,
                                                       "sender_ref": OWNER_WX, "code": code, "mention_prefix": "@管家"},
                   allow_write=True)


def ask(world, ref, body="武侠那个项目做到哪了", sender=MEMBER, room=ROOM, at=NOW):
    if at is NOW:
        at = state_channels._now().isoformat()
    return execute(world.steward, "relay_group_message",
                   {"request_key": f"g-{ref}", "channel": "wechat", "group_ref": room, "sender_ref": sender,
                    "channel_message_ref": ref, "body": body, **({"channel_at": at} if at else {})}, allow_write=True)


def reply(world, question_id, body, key=None):
    return execute(world.steward, "post_group_reply", {"request_key": key or f"group-reply:{question_id}",
                                                       "question_id": question_id, "body": body}, allow_write=True)


def rows(world, sql, args=()):
    with world.db.get_connection() as conn:
        return [tuple(r) for r in conn.execute(sql, args)]


def public_view(world, service=None):
    """The steward's credential with X-AItelier-Read-As, the way get_service builds it."""
    base = service or world.steward
    return StateService(world.db, actor=base.actor, driver_id=base.driver_id, project_read_trusted=False,
                        public_view=True)


def family_view(world):
    """... for the family memory pair (get_service makes only that one trusted)."""
    return StateService(world.db, actor=world.steward.actor, driver_id=world.steward.driver_id,
                        project_read_trusted=True, public_view=True)


def audience(world, kind="group", room=ROOM, senders=(MEMBER,), turn="t1"):
    group = {"channel": "wechat", "group_ref": room} if kind == "group" else None
    return execute(world.steward, "set_steward_turn_audience",
                   {"turn": turn, "audience": kind, "group": group, "sender_refs": list(senders)}, allow_write=True)


# --- the director's ruling: the numbers live in code -------------------------------------------

def test_group_limits_are_pinned_code_constants_that_no_environment_variable_changes(monkeypatch):
    for name in ("AITELIER_GROUP_PER_HOUR", "AITELIER_MEMBER_PER_HOUR", "AITELIER_ACCOUNT_OUT_PER_HOUR",
                 "GROUP_PER_HOUR", "group_per_hour"):
        monkeypatch.setenv(name, "999")
    import importlib
    fresh = importlib.reload(state_groups)
    assert dict(fresh.GROUP_LIMITS) == {"group_per_hour": 60, "member_per_hour": 20, "account_out_per_hour": 120,
                                        "notice_per_hour": 1, "fresh_minutes": 10, "retention_days": 30,
                                        "reply_chars": 1400,
                                        # Step 25 C11 (7.9.4): the owner's group reminders, apart.
                                        "group_notices_per_hour": 6,
                                        # Step 24 (7.8.9): confirmation texts and their lines, apart.
                                        "group_sends_per_hour": 20}
    with pytest.raises(TypeError):
        fresh.GROUP_LIMITS["group_per_hour"] = 1
    assert dict(fresh.FAMILY_LIMITS) == {"entries": 100, "entry_chars": 200, "note_chars": 1000, "show_chars": 20000}


# --- codes and binding -------------------------------------------------------------------------

def test_group_and_account_codes_never_retire_or_stand_for_each_other(world):
    bind(world, "wechat", OWNER_WX, key="bind-wx")
    account = world.owner.channels.issue_code("wechat")["code"]
    group = world.owner.groups.issue_group_code("wechat")["code"]
    assert group.startswith("G-") and len(group) == 11
    # Issuing either kind leaves the other live.
    assert rows(world, "SELECT kind FROM state_owner_channel_codes WHERE used_at IS NULL ORDER BY kind") == \
        [("account",), ("group",)]
    world.owner.channels.issue_code("wechat")
    assert rows(world, "SELECT COUNT(*) FROM state_owner_channel_codes WHERE kind='group' AND used_at IS NULL") == [(1,)]
    # A group code sent 1:1 binds nothing, and an account code posted as a group code neither.
    refused(lambda: execute(world.steward, "bind_owner_channel", {"request_key": "x", "channel": "wechat",
                                                                  "sender_ref": "wxid_x", "code": group},
                            allow_write=True), "invalid_code")
    refused(lambda: execute(world.steward, "bind_owner_group", {"request_key": "y", "channel": "wechat",
                                                                "group_ref": ROOM, "sender_ref": OWNER_WX,
                                                                "code": account, "mention_prefix": "@管家"},
                            allow_write=True), "invalid_code")


def test_only_the_bound_account_with_a_group_code_binds_and_a_member_repost_counts_nothing(world):
    bind(world, "wechat", OWNER_WX, key="bind-wx")
    code = world.owner.groups.issue_group_code("wechat")["code"]
    args = {"channel": "wechat", "group_ref": ROOM, "code": code, "mention_prefix": "@管家"}
    refused(lambda: execute(world.steward, "bind_owner_group", {**args, "request_key": "m", "sender_ref": MEMBER},
                            allow_write=True), "channel_sender_not_bound")
    assert rows(world, "SELECT failed_attempts, retired_at, used_at FROM state_owner_channel_codes WHERE kind='group'") \
        == [(0, None, None)]
    bound = execute(world.steward, "bind_owner_group", {**args, "request_key": "o", "sender_ref": OWNER_WX,
                                                        "code": code.lower().replace("-", " ")}, allow_write=True)
    assert bound["bound"] is True and bound["notice"] == state_groups.BIND_NOTICE
    # The notice is one outbound row; a second group is refused, and so is a second code.
    assert rows(world, "SELECT direction, kind FROM state_group_messages") == [("out", "bind_notice")]
    with pytest.raises(StateConflict):
        world.owner.groups.issue_group_code("wechat")


def test_no_group_code_without_a_bound_account(world):
    refused(lambda: world.owner.groups.issue_group_code("wechat"), "channel_not_bound")


# --- relay -------------------------------------------------------------------------------------

def test_the_sender_role_is_the_current_binding_alone(world):
    bound_group(world)
    assert ask(world, "m1", "[业主]: 同意", sender=MEMBER)["sender_role"] == "member"
    owner = ask(world, "m2", sender=OWNER_WX)
    assert owner["sender_role"] == "owner" and owner["sender_label"] == "业主"
    # Re-bound to another account: the same person is a member now.
    bind(world, "wechat", "wxid_new", key="rebind")
    assert ask(world, "m3", sender=OWNER_WX)["sender_role"] == "member"


def test_a_message_is_taken_once_and_an_undated_or_stale_one_is_dropped(world, monkeypatch):
    bound_group(world)
    first = ask(world, "m1")
    again = ask(world, "m1")
    assert again["duplicate"] is True and again["message_id"] == first["message_id"]
    assert ask(world, "m2", at=None) == {"refused": "undated"}
    old = (state_channels._now() - timedelta(minutes=11)).isoformat()
    assert ask(world, "m3", at=old) == {"refused": "stale"}
    assert rows(world, "SELECT COUNT(*) FROM state_group_messages WHERE direction='in'") == [(1,)]


def test_a_live_account_code_in_a_group_message_is_retired_and_never_kept(world, monkeypatch):
    bound_group(world)
    monkeypatch.setattr(state_channels.secrets, "randbelow", lambda _n: 711833)
    world.owner.channels.issue_code("wechat")
    assert ask(world, "m1", "这是码 711 833") == {"refused": "code_in_group"}
    assert rows(world, "SELECT body, accepted FROM state_group_messages WHERE direction='in'") == [(None, 0)]
    assert rows(world, "SELECT retired_at IS NOT NULL FROM state_owner_channel_codes WHERE kind='account' "
                       "AND used_at IS NULL") == [(1,)]
    # The member who saw it cannot bind with it 1:1.
    refused(lambda: execute(world.steward, "bind_owner_channel", {"request_key": "steal", "channel": "wechat",
                                                                  "sender_ref": MEMBER, "code": "711833"},
                            allow_write=True), "code_retired")


def test_group_writes_write_no_state_event_and_leave_the_owner_conversation_alone(world):
    bound_group(world)
    events = rows(world, "SELECT COUNT(*) FROM state_events")
    before = conversation(world)
    question = ask(world, "m1", "Q12 用方案 A", sender=OWNER_WX)
    reply(world, question["message_id"], "好的")
    assert rows(world, "SELECT COUNT(*) FROM state_events") == events
    assert conversation(world) == before


def test_the_group_and_member_bounds_and_the_one_notice_per_hour(world):
    bound_group(world)
    for i in range(20):
        assert "message_id" in ask(world, f"a{i}")
    over = ask(world, "a20")
    assert over["refused"] == "member_quota" and over["notice"] == state_groups.QUOTA_NOTICE
    assert "notice" not in ask(world, "a21")
    # The owner is not held to 20, only to the group's 60.
    for i in range(40):
        assert "message_id" in ask(world, f"o{i}", sender=OWNER_WX)
    assert ask(world, "o40", sender=OWNER_WX)["refused"] == "group_quota"
    # The over-quota rows carry no body and a replay of one is the same row.
    assert rows(world, "SELECT COUNT(*) FROM state_group_messages WHERE accepted=0 AND body IS NULL") == [(3,)]
    assert ask(world, "a20")["duplicate"] is True
    assert rows(world, "SELECT COUNT(*) FROM state_group_messages WHERE kind='quota_notice'") == [(1,)]


def test_a_rename_on_the_dashboard_does_not_reset_the_members_window(world):
    bound_group(world)
    for i in range(20):
        ask(world, f"a{i}")
    world.owner.groups.name_group_member("wechat", MEMBER, "妈妈")
    assert ask(world, "a20")["refused"] == "member_quota"
    refused(lambda: world.owner.groups.name_group_member("wechat", "wxid_never", "x"), "unknown_member")
    with pytest.raises(StateGraphError):
        world.steward.groups.name_group_member("wechat", MEMBER, "x")


def test_the_account_outbound_cap_counts_every_group_of_the_channel_and_logs(world, caplog):
    bound_group(world)
    moment = state_channels._now().isoformat()
    with world.db.get_connection() as conn:
        # 119 outbound rows in the last hour (the bind notice is the 120th... minus one), split
        # between the bound group and a group unbound since.
        for i in range(118):
            conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,at) "
                         "VALUES(?,?,?,'out','reply',1,?)", (f"o{i}", "wechat", ROOM if i % 2 else ROOM2, moment))
        conn.commit()
    assert "message_id" in ask(world, "m1")
    with world.db.get_connection() as conn:
        conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,at) "
                     "VALUES('o-last','wechat',?,'out','reply',1,?)", (ROOM2, moment))
        conn.commit()
    caplog.set_level(logging.WARNING, logger="core.state_groups")
    over = ask(world, "m2", "第二问")
    assert over == {"refused": "group_quota_account", "sender_role": "member"}
    assert rows(world, "SELECT body FROM state_group_messages WHERE channel_message_ref='m2'") == [(None,)]
    assert "group_quota_account" in caplog.text and "第二问" not in caplog.text


def test_thirty_day_retention_and_unbind_deletes_everything(world, monkeypatch):
    bound_group(world)
    ask(world, "m1")
    real = state_channels._now
    monkeypatch.setattr(state_channels, "_now", lambda: real() + timedelta(days=31))
    ask(world, "m2")
    assert rows(world, "SELECT channel_message_ref FROM state_group_messages WHERE direction='in'") == [("m2",)]
    world.owner.groups.unbind_owner_group("wechat")
    assert rows(world, "SELECT COUNT(*) FROM state_group_messages") == [(0,)]
    assert ask(world, "m3") == {"refused": "group_not_bound"}


# --- the reply and its exit check (director 418: two arms) ---------------------------------------

@pytest.fixture
def projects(world):
    writer = StateService(world.db, actor="fixture", project_read_trusted=True)
    writer.store.create_project("wuxia-myth", "武虾传奇")
    writer.open_project("wuxia-myth")
    writer.store.create_project("secret-plan", "家庭账本")
    writer.store.create_project("menu", "菜谱")
    writer.store.create_project("aitelier", "AItelier")
    return writer


def test_exit_check_arm_one_a_body_with_a_code_a_private_project_and_a_path_sends_the_fixed_sentence(
        world, projects, monkeypatch):
    bound_group(world)
    monkeypatch.setattr(state_channels.secrets, "randbelow", lambda _n: 482913)
    world.owner.channels.issue_code("wechat")
    question = ask(world, "m1", "爸爸在忙什么")
    body = "码是 482913，他在做 secret-plan（家庭账本），文件在 /home/linxuhao/x"
    sent = reply(world, question["message_id"], body)
    assert sent["withheld"] is True
    assert sent["text"] == "「爸爸在忙什么」\n" + state_groups.WITHHELD_TEXT
    # The sentence the group gets is, byte for byte, the one steward.md tells the model to use.
    steward_md = (Path(__file__).resolve().parents[2] / "integrations/dsh-director/steward.md").read_text(encoding="utf-8")
    assert "「" + state_groups.WITHHELD_TEXT + "」" in steward_md
    assert rows(world, "SELECT body, original, withheld_reason FROM state_group_messages WHERE direction='out' "
                       "AND kind='reply'") == [(sent["text"], body, "live_code")]
    # The code that leaked into a reply is not secret any more: retired.
    assert rows(world, "SELECT COUNT(*) FROM state_owner_channel_codes WHERE kind='account' AND used_at IS NULL "
                       "AND retired_at IS NULL") == [(0,)]


def test_exit_check_arm_two_a_body_with_only_a_public_project_id_goes_out_as_written(world, projects):
    bound_group(world)
    question = ask(world, "m1")
    sent = reply(world, question["message_id"], "wuxia-myth 已经完成了 12 个节点。")
    assert sent["withheld"] is False and sent["text"].endswith("wuxia-myth 已经完成了 12 个节点。")
    assert rows(world, "SELECT withheld_reason, original FROM state_group_messages WHERE kind='reply'") == [(None, None)]


@pytest.mark.parametrize("body,reason", [
    ("在做 secret-plan", "private_project"), ("在弄家庭账本", "private_project"), ("SECRET-PLAN 呢", "private_project"),
    ("最近在弄菜谱", "private_project"), ("路径 /app/core", "host_path"), ("在 /tmp/x 里", "host_path")])
def test_each_shape_alone_is_withheld(world, projects, body, reason):
    bound_group(world)
    question = ask(world, "m1", "家庭账本和菜谱是什么")  # a word the question used is NOT exempt (two characters too)
    assert reply(world, question["message_id"], body)["withheld"] is True
    assert rows(world, "SELECT withheld_reason FROM state_group_messages WHERE kind='reply'") == [(reason,)]


@pytest.mark.parametrize("body", [
    "资料在 ~/.AItelier/director/reports 里", "装在 /opt/aitelier-dsh 下", "服务在 /srv/app", "日志 /var/log/x",
    "配置 /etc/systemd/x", "密钥 /run/aitelier-secrets", "在 /root/x", "全角的 ～／.AItelier／director", "目录 ／ｏｐｔ／x",
    "看 https://apps.apple.com/cn/app/id1，文件在~/.AItelier", "https://example.com，/opt/x"])
def test_the_host_path_forms_the_steward_context_carries_are_withheld(world, body):
    """Director 477 N2: the enumeration covers the home forms and the system roots, full-width
    forms fold to the same path, and a URL right before a path does not hide it."""
    bound_group(world)
    question = ask(world, "m1")
    assert reply(world, question["message_id"], body)["withheld"] is True, body
    assert rows(world, "SELECT withheld_reason FROM state_group_messages WHERE kind='reply'") == [("host_path",)]


@pytest.mark.parametrize("body", [
    "A/B 测试做完了", "周末和/或周日都行", "比分 3/4", "下载：https://apps.apple.com/cn/app/id1234",
    "官网首页 https://example.com/home/ 有说明", "https://example.org/opt/in?x=1 是退订链接"])
def test_ordinary_slashes_and_web_addresses_are_not_host_paths(world, body):
    """... while ordinary words with slashes pass, and so do web addresses: an http(s) URL is
    taken out before the path match (7.7.7 item 3, as documented on _PATHS)."""
    bound_group(world)
    question = ask(world, "m1")
    sent = reply(world, question["message_id"], body)
    assert sent["withheld"] is False and sent["text"].endswith(body), (body, sent)


def test_the_platform_name_and_a_public_project_pass_and_a_live_group_code_is_withheld(world, projects, monkeypatch):
    bound_group(world, key="bg-a")
    question = ask(world, "m1")
    assert reply(world, question["message_id"], "AItelier 上的武虾传奇进展不错")["withheld"] is False
    world.owner.groups.unbind_owner_group("wechat")
    monkeypatch.setattr(state_groups.secrets, "choice", lambda alphabet: "7")
    code = world.owner.groups.issue_group_code("wechat")["code"]
    assert code == "G-7777-7777"
    # Another group bound directly; the group code issued above stays live.
    with world.db.get_connection() as conn:
        conn.execute("INSERT INTO state_owner_groups(channel,group_ref,mention_prefix,bound_at) "
                     "VALUES('wechat',?,'@管家',?)", (ROOM2, state_channels._now().isoformat()))
        conn.commit()
    question = ask(world, "m2", room=ROOM2)
    assert reply(world, question["message_id"], "码 g7777 7777")["withheld"] is True


def test_a_reply_goes_to_the_questions_group_once(world):
    bound_group(world)
    question = ask(world, "m1")
    first = reply(world, question["message_id"], "答")
    again = reply(world, question["message_id"], "答")
    assert again["duplicate"] is True and again["message_id"] == first["message_id"]
    assert first["group_ref"] == ROOM
    assert rows(world, "SELECT COUNT(*) FROM state_group_messages WHERE direction='out' AND kind='reply'") == [(1,)]
    with pytest.raises(StateNotFound):
        reply(world, "gmsg-nope", "x")
    long = reply(world, ask(world, "m2")["message_id"], "长" * 3000)
    assert len(long["text"]) == 1400 and long["text"].endswith("…")


# --- the public-read header (7.7.6) --------------------------------------------------------------

def test_a_public_view_service_reads_only_public_projects_and_writes_nothing(world, projects):
    view = public_view(world)
    listed = execute(view, "project_catalog", {})
    ids = {p["project_id"] for p in (listed.get("projects") if isinstance(listed, dict) else listed)}
    assert ids == {"wuxia-myth"}
    with pytest.raises(ProjectPrivate) as private:
        execute(view, "project_overview", {"project_id": "secret-plan"})
    with pytest.raises(ProjectPrivate) as missing:
        execute(view, "project_overview", {"project_id": "no-such-project"})
    assert str(private.value) == str(missing.value) == "This State DAG record is not available."
    for action in ("list_user_questions", "list_user_conversation", "list_director_messages"):
        with pytest.raises(ProjectPrivate):
            execute(view, action, {"project_id": "aitelier-home"})
    for action, args in (("send_user_message", {"project_id": "aitelier-home", "subject": "s", "body": "b"}),
                         ("create_steward_proposal", {"request_key": "k", "kind": "start_driver",
                                                      "payload": {"project_id": "alpha"}, "reason": "r"}),
                         ("relay_group_message", {})):
        refused(lambda: execute(view, action, args, allow_write=True), "public_view_read_only")
    # The next service of the same credential without the header is the writer again.
    assert execute(world.steward, "project_overview", {"project_id": "secret-plan"})


# --- the family memory (7.7.8a, director 420) ---------------------------------------------------

def test_family_memory_without_the_header_is_refused_to_the_steward_and_the_owner(world):
    bound_group(world)
    audience(world)
    refused(lambda: execute(world.steward, "note_family_memory", {"request_key": "k", "text": "周六去外婆家", "turn": "t1"},
                            allow_write=True), "family_memory_group_turn_only")
    refused(lambda: execute(world.steward, "list_family_memory", {"turn": "t1"}), "family_memory_group_turn_only")
    with pytest.raises((StateGraphError, ProjectPrivate)):
        execute(world.owner, "list_family_memory", {"turn": "t1"})


@pytest.mark.parametrize("setup", ["none", "owner", "other_group"])
def test_family_memory_with_the_header_but_no_group_turn_is_refused(world, setup):
    bound_group(world)
    if setup == "owner":
        audience(world, "owner")
    elif setup == "other_group":
        audience(world, "group", room=ROOM2)
    refused(lambda: execute(family_view(world), "note_family_memory", {"request_key": "k", "text": "x", "turn": "t1"},
                            allow_write=True), "family_memory_group_turn_only")


def test_family_memory_in_a_group_turn_takes_group_and_author_from_the_record(world, monkeypatch):
    bound_group(world)
    ask(world, "m1")
    world.owner.groups.name_group_member("wechat", MEMBER, "妈妈")
    audience(world, senders=(MEMBER,))
    view = family_view(world)
    noted = execute(view, "note_family_memory", {"request_key": "k1", "text": " 周六去外婆家 ", "turn": "t1"}, allow_write=True)
    assert noted["added_by"] == "妈妈"
    assert execute(view, "list_family_memory", {"turn": "t1"})["entries"][0]["text"] == "周六去外婆家"
    refused(lambda: execute(view, "note_family_memory", {"request_key": "k2", "text": "长" * 201, "turn": "t1"},
                            allow_write=True), "entry_too_long")
    for i in range(99):
        execute(view, "note_family_memory", {"request_key": f"n{i}", "text": f"事 {i}", "turn": "t1"}, allow_write=True)
    refused(lambda: execute(view, "note_family_memory", {"request_key": "full", "text": "第101条", "turn": "t1"},
                            allow_write=True), "family_memory_full")
    # Thirty days later the next write removes the old entries.
    real = state_channels._now
    monkeypatch.setattr(state_channels, "_now", lambda: real() + timedelta(days=31))
    execute(view, "note_family_memory", {"request_key": "late", "text": "新的", "turn": "t1"}, allow_write=True)
    assert rows(world, "SELECT text FROM state_family_memory") == [("新的",)]
    # A service trusted for the family pair serves nothing else.
    with pytest.raises(ProjectPrivate):
        execute(view, "list_owner_channels", {})
    world.owner.groups.set_family_note("wechat", "外婆家在城南")
    assert execute(view, "list_family_memory", {"turn": "t1"})["family_note"] == "外婆家在城南"
    world.owner.groups.unbind_owner_group("wechat")
    assert rows(world, "SELECT COUNT(*) FROM state_family_memory") == [(0,)]
    assert rows(world, "SELECT COUNT(*) FROM state_family_note") == [(0,)]


def test_family_memory_in_a_merged_turn_of_several_askers_names_nobody(world):
    bound_group(world)
    ask(world, "m1")
    ask(world, "m2", sender=MEMBER2)
    world.owner.groups.name_group_member("wechat", MEMBER, "妈妈")
    audience(world, senders=(MEMBER, MEMBER2))
    noted = execute(family_view(world), "note_family_memory", {"request_key": "k1", "text": "周六去外婆家", "turn": "t1"},
                    allow_write=True)
    assert noted["added_by"] is None
    assert rows(world, "SELECT added_by_ref FROM state_family_memory") == [(None,)]
    # The same asker twice is still that one person.
    audience(world, senders=(MEMBER, MEMBER))
    assert execute(family_view(world), "note_family_memory", {"request_key": "k2", "text": "周日在家", "turn": "t1"},
                   allow_write=True)["added_by"] == "妈妈"


def test_family_memory_names_the_live_turn_and_a_stale_record_serves_no_other_turn(world):
    """Director 477 N1: the audience record is one overwritten row; the family memory pair is
    accepted only for the turn it was written for. A group turn t1 ended and the next turn t2
    is not recorded yet (the stale window): a request of t2 meets t1's row and is refused; a
    request naming no turn or another turn is refused; once t2 is recorded as the owner's,
    even t1's own late request is refused."""
    bound_group(world)
    ask(world, "m1")
    audience(world, turn="t1")
    view = family_view(world)
    note = lambda key, **turn: execute(view, "note_family_memory", {"request_key": key, "text": "周六去外婆家", **turn},
                                       allow_write=True)
    assert note("k1", turn="t1")["noted"] is True
    # The stale window: t1's group row stands, the live turn is t2.
    refused(lambda: note("k2", turn="t2"), "family_memory_group_turn_only")
    refused(lambda: execute(view, "list_family_memory", {"turn": "t2"}), "family_memory_group_turn_only")
    refused(lambda: execute(view, "list_family_memory", {"turn": "t0"}), "family_memory_group_turn_only")
    with pytest.raises(StateGraphError):
        note("k3")  # no turn at all: invalid_request
    with pytest.raises(StateGraphError):
        execute(view, "list_family_memory", {})
    # t2 recorded as an owner turn: neither t2 nor t1's late request gets the family memory.
    audience(world, "owner", turn="t2")
    refused(lambda: note("k4", turn="t2"), "family_memory_group_turn_only")
    refused(lambda: note("k5", turn="t1"), "family_memory_group_turn_only")
    refused(lambda: execute(view, "list_family_memory", {"turn": "t1"}), "family_memory_group_turn_only")
    # A later group turn t3 is served, under its own id only.
    audience(world, turn="t3")
    assert execute(view, "list_family_memory", {"turn": "t3"})["entries"][0]["text"] == "周六去外婆家"
    refused(lambda: execute(view, "list_family_memory", {"turn": "t1"}), "family_memory_group_turn_only")
    assert rows(world, "SELECT turn, audience FROM state_steward_turn_audience") == [("t3", "group")]


def test_the_family_note_is_the_owners_route_alone(world, client):
    bound_group(world)
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    assert client.put("/api/steward/groups/wechat/family-note", json={"text": "x"}, headers=steward).status_code == 403
    r = client.put("/api/steward/groups/wechat/family-note", json={"text": "外婆家在城南"}, headers=OWNER_HEADERS)
    assert r.status_code == 200, r.text
    view = client.get("/api/steward/groups?channel=wechat", headers=OWNER_HEADERS).json()["groups"][0]
    assert view["family_note"]["text"] == "外婆家在城南" and view["label"] == "家庭群"


def test_the_turn_audience_is_the_stewards_overwritten_and_writes_no_event(world):
    bound_group(world)
    events = rows(world, "SELECT COUNT(*) FROM state_events")
    audience(world)
    audience(world, "owner")
    assert rows(world, "SELECT audience, channel FROM state_steward_turn_audience") == [("owner", None)]
    assert rows(world, "SELECT COUNT(*) FROM state_events") == events
    with pytest.raises(StateGraphError):
        execute(world.director, "set_steward_turn_audience", {"turn": "t", "audience": "owner"}, allow_write=True)


# --- the pick-a-group fallback (7.7.9 last item, only when step 0 item 1 fails) ----------------

def test_the_owner_picks_the_one_pending_group_and_never_one_of_two(world, monkeypatch):
    monkeypatch.setattr(state_groups, "PICK_PENDING_GROUPS", True)
    bind(world, "wechat", OWNER_WX, key="bind-wx")
    code = world.owner.groups.issue_group_code("wechat")["code"]
    args = {"channel": "wechat", "code": code, "mention_prefix": "@管家", "sender_ref": "wxid_owner_in_group"}
    refused(lambda: execute(world.steward, "bind_owner_group", {**args, "request_key": "p1", "group_ref": ROOM},
                            allow_write=True), "pending_owner_pick")
    with pytest.raises(StateNotFound):
        world.owner.groups.pick_pending_group("wechat", "typed-by-hand")
    refused(lambda: execute(world.steward, "bind_owner_group", {**args, "request_key": "p2", "group_ref": ROOM2},
                            allow_write=True), "pending_owner_pick")
    with pytest.raises(StateConflict):
        world.owner.groups.pick_pending_group("wechat", ROOM)
    # Only rooms the bridge reported in the last 10 minutes count: ROOM2's report ages out.
    with world.db.get_connection() as conn:
        conn.execute("UPDATE state_pending_groups SET at=? WHERE room_ref=?",
                     ((state_channels._now() - timedelta(minutes=11)).isoformat(), ROOM2))
        conn.commit()
    with pytest.raises(StateNotFound):
        world.owner.groups.pick_pending_group("wechat", ROOM2)
    picked = world.owner.groups.pick_pending_group("wechat", ROOM)
    assert picked["bound"] is True and picked["group_ref"] == ROOM
