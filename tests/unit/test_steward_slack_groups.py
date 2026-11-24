"""Step 23, its Slack step: private channels as steward groups (design-steward 7.7.19).

The same turn audience, guards, quotas, storage and family memory as a WeChat group; only the
platform layer changes. What the backend does differently for a Slack group: the reply is not
prefixed with the WeChat quote line (Slack answers in the asker's thread, D8) and names the
question's channel ref for the plugin to thread under; a Slack group has no media switch (only
mentions reach the app: un-mentioned files are out of scope). The Slack plugin itself (private
channels only, Slack Connect refused and paused, app_mention only, messageRef <channel>:<ts>,
unfurls off) is tested in integrations/dsh-channel-slack/test/slack.test.mjs."""
from __future__ import annotations

from core import owner_text, state_channels, state_groups
from core.state_commands import execute
from tests.unit.test_steward_channels import world_baseline  # noqa: F401 (the world fixture needs it)
from tests.unit.test_steward_channels import OWNER_HEADERS, bind, client, refused, world  # noqa: F401
from tests.unit.test_steward_groups import projects, rows  # noqa: F401 (projects is a fixture)
from tests.unit.test_steward_groups import ask as wechat_ask
from tests.unit.test_steward_groups import bound_group as wechat_group

OWNER, MEMBER, ROOM, ROOM2 = "U0OWNER", "U0MEMBER", "C0PRIVATE", "C0SECOND"
PREFIX = "<@U0BOT>"
TS = "1763481600.000300"


def slack_group(world, room=ROOM, label="家庭频道", key=None):
    if not rows(world, "SELECT 1 FROM state_owner_channels WHERE channel='slack' AND revoked_at IS NULL"):
        bind(world, "slack", OWNER, key="bind-slack")
    code = world.owner.groups.issue_group_code("slack", label)["code"]
    return execute(world.steward, "bind_owner_group",
                   {"request_key": key or f"sg-{room}", "channel": "slack", "group_ref": room, "sender_ref": OWNER,
                    "code": code, "mention_prefix": PREFIX, "channel_message_ref": f"{room}:1763481500.000100"},
                   allow_write=True)


def ask(world, ts, body="武侠那个项目做到哪了", sender=MEMBER, room=ROOM):
    """A mention as the plugin hands it in: messageRef <channel>:<ts>, the bot's mention taken off."""
    return execute(world.steward, "relay_group_message",
                   {"request_key": f"g-{room}-{ts}", "channel": "slack", "group_ref": room, "sender_ref": sender,
                    "channel_message_ref": f"{room}:{ts}", "body": body,
                    "channel_at": state_channels._now().isoformat()}, allow_write=True)


def reply(world, question_id, body, turn="t-slack", room=ROOM, sender=MEMBER):
    execute(world.steward, "set_steward_turn_audience",
            {"turn": turn, "audience": "group", "group": {"channel": "slack", "group_ref": room},
             "sender_refs": [sender], "question_ids": [question_id]}, allow_write=True)
    return execute(world.steward, "post_group_reply", {"request_key": f"group-reply:{question_id}",
                                                       "question_id": question_id, "body": body, "turn": turn},
                   allow_write=True)


def test_a_slack_channel_binds_with_the_bots_mention_as_its_prefix_and_the_host_lists_it_for_the_plugin(world):
    bound = slack_group(world)
    assert bound["bound"] is True and bound["channel"] == "slack" and bound["mention_prefix"] == PREFIX
    listed = execute(world.steward, "list_owner_groups", {})
    assert listed["owner_refs"]["slack"] == OWNER
    assert [(g["channel"], g["group_ref"], g["mention_prefix"]) for g in listed["groups"]] == [("slack", ROOM, PREFIX)]
    # A member's repost of a fresh code binds nothing; the code is not spent.
    code = world.owner.groups.issue_group_code("slack", "第二个")["code"]
    refused(lambda: execute(world.steward, "bind_owner_group",
                            {"request_key": "m", "channel": "slack", "group_ref": ROOM2, "sender_ref": MEMBER,
                             "code": code, "mention_prefix": PREFIX}, allow_write=True), "channel_sender_not_bound")
    # A WeChat group code never binds a Slack channel, nor the other way round.
    bind(world, "wechat", "wxid_owner", key="bind-wx")
    wx_code = world.owner.groups.issue_group_code("wechat", "微信群")["code"]
    refused(lambda: execute(world.steward, "bind_owner_group",
                            {"request_key": "x", "channel": "slack", "group_ref": ROOM2, "sender_ref": OWNER,
                             "code": wx_code, "mention_prefix": PREFIX}, allow_write=True), "invalid_code")


def test_d11_the_slack_group_and_member_quotas_are_the_wechat_groups(world):
    """60 accepted questions per group and hour, 20 per member, one quota notice an hour (owner 2026-11-20)."""
    slack_group(world)
    for i in range(20):
        assert "message_id" in ask(world, f"1763481600.{i:06d}")
    over = ask(world, "1763481600.000020")
    assert over["refused"] == "member_quota" and over["notice"] == owner_text.texts()["group_quota"]
    assert "notice" not in ask(world, "1763481600.000021")
    for i in range(40):
        assert "message_id" in ask(world, f"1763481700.{i:06d}", sender=OWNER)
    assert ask(world, "1763481700.000040", sender=OWNER)["refused"] == "group_quota"
    assert rows(world, "SELECT COUNT(*) FROM state_group_messages WHERE channel='slack' AND kind='quota_notice'") == [(1,)]
    # The owner of the channel is stamped owner by code, everyone else member.
    assert rows(world, "SELECT DISTINCT sender_role FROM state_group_messages WHERE channel='slack' AND sender_ref=?",
                (OWNER,)) == [("owner",)]


def test_a_message_ref_is_channel_and_ts_taken_once_and_the_same_ts_in_two_channels_is_two_messages(world):
    slack_group(world)
    slack_group(world, room=ROOM2, label="第二个")
    first = ask(world, TS)
    assert ask(world, TS)["duplicate"] is True
    other = ask(world, TS, room=ROOM2)
    assert other["message_id"] != first["message_id"] and other["duplicate"] is False
    assert sorted(rows(world, "SELECT channel_message_ref FROM state_group_messages WHERE direction='in'")) == \
        [(f"{ROOM}:{TS}",), (f"{ROOM2}:{TS}",)]


def test_d8_a_slack_reply_has_no_quote_line_and_names_the_question_to_thread_under(world, projects):
    slack_group(world)
    question = ask(world, TS)
    sent = reply(world, question["message_id"], "  在做第三章。 ")
    assert sent["text"] == "在做第三章。" and sent["reply_to_ref"] == f"{ROOM}:{TS}"
    assert sent["channel"] == "slack" and sent["group_ref"] == ROOM
    # Still one bounded message, cut with '…', and the exit check's fixed sentence alone when withheld.
    long = reply(world, ask(world, "1763481601.000100")["message_id"], "长" * 3000, turn="t2")
    assert len(long["text"]) == state_groups.GROUP_LIMITS["reply_chars"] and long["text"].endswith("…")
    withheld = reply(world, ask(world, "1763481602.000100")["message_id"], "家庭账本 secret-plan 的进度", turn="t3")
    assert withheld["withheld"] is True and withheld["text"] == owner_text.texts()["group_withheld"]


def test_a_wechat_reply_keeps_its_quote_line_and_names_its_question_too(world):
    wechat_group(world)
    question = wechat_ask(world, "wx-1", "爸爸在忙什么")
    execute(world.steward, "set_steward_turn_audience",
            {"turn": "t-wx", "audience": "group", "group": {"channel": "wechat", "group_ref": "r1@chatroom"},
             "sender_refs": ["wxid_m1"], "question_ids": [question["message_id"]]}, allow_write=True)
    sent = execute(world.steward, "post_group_reply", {"request_key": "gr-wx", "question_id": question["message_id"],
                                                       "body": "在写代码", "turn": "t-wx"}, allow_write=True)
    assert sent["text"] == "「爸爸在忙什么」\n在写代码" and sent["reply_to_ref"] == "wx-1"


def test_a_slack_group_has_no_media_switch_and_an_unmentioned_file_is_never_kept(world, client):
    slack_group(world)
    refused(lambda: world.owner.groups.set_group_media_keep("slack", ROOM, True), "channel_no_kept_media")
    card = world.owner.groups.owner_view()["groups"][0]
    assert card["channel"] == "slack" and "media_keep" not in card
    assert execute(world.steward, "list_owner_groups", {})["groups"][0]["media_keep"] == "off"
    r = client.put(f"/api/steward/groups/slack/{ROOM}/media-keep", json={"on": True}, headers=OWNER_HEADERS)
    assert r.status_code in (400, 409, 422) and "channel_no_kept_media" in r.text
    kept = execute(world.steward, "relay_group_message",
                   {"request_key": "k1", "channel": "slack", "group_ref": ROOM, "sender_ref": MEMBER,
                    "channel_message_ref": f"{ROOM}:{TS}", "body": "[图片]", "channel_at": state_channels._now().isoformat(),
                    "kept": True, "attachments": [{"kind": "image", "ref": "m_0123456789abcdef", "mime": "image/png",
                                                   "size": 33}]}, allow_write=True)
    assert kept == {"refused": "media_keep_off"}
    assert rows(world, "SELECT COUNT(*) FROM state_group_messages WHERE kind='kept_media'") == [(0,)]


def test_a_slack_group_turns_family_memory_is_that_channels(world):
    from tests.unit.test_steward_groups import family_view
    slack_group(world)
    ask(world, TS)
    execute(world.steward, "set_steward_turn_audience",
            {"turn": "t1", "audience": "group", "group": {"channel": "slack", "group_ref": ROOM},
             "sender_refs": [MEMBER]}, allow_write=True)
    view = family_view(world)
    execute(view, "note_family_memory", {"request_key": "k1", "text": "周六去外婆家", "turn": "t1"}, allow_write=True)
    assert rows(world, "SELECT channel, group_ref, text FROM state_family_memory") == [("slack", ROOM, "周六去外婆家")]
    assert execute(view, "list_family_memory", {"turn": "t1"})["entries"][0]["text"] == "周六去外婆家"
    world.owner.groups.unbind_owner_group("slack", ROOM)
    assert rows(world, "SELECT COUNT(*) FROM state_family_memory") == [(0,)]
