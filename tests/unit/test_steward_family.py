"""Step 25.2: reminders to the family group (C11) and the family lists (C4), the backend
(design-steward 7.9.4, 7.9.5).

C11: a schedule's target is the owner (the steward is woken) or the family group bound when
it was made; when due the steward's HOST posts the row's text with post_group_notice - no
model turn - after the row fired for that due time, through the exit check, at most 6 an hour
per group and within the account's 120. C4: the shopping and to-do lists, read and written
from a group turn (public-read header, the recorded group audience of the named turn), from
the owner's turn and on the dashboard; every add passes the exit check.

The schedules' clock (core.state_steward._now) and the channels' clock
(core.state_channels._now, the group module's) are one clock here."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import state_channels, state_groups, state_steward
from core.state_commands import ProjectPrivate, execute
from core.state_graph import StateConflict, StateGraphError
from core.state_service import StateService
from tests.unit.test_steward_channels import OWNER_HEADERS, client, refused, world  # noqa: F401
from tests.unit.test_steward_groups import (MEMBER, MEMBER2, OWNER_WX, ROOM, ROOM2, ask, audience, bound_group,
                                            family_view, public_view, reply, rows)


class Clock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment


@pytest.fixture
def clock(monkeypatch):
    fixed = Clock(datetime(2027, 1, 1, 8, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(state_steward, "_now", fixed)
    monkeypatch.setattr(state_channels, "_now", fixed)
    return fixed


@pytest.fixture
def projects(world):
    writer = StateService(world.db, actor="fixture", project_read_trusted=True)
    writer.store.create_project("wuxia-myth", "武虾传奇")
    writer.open_project("wuxia-myth")
    writer.store.create_project("secret-plan", "家庭账本")
    return writer


IN_TEN = "2027-01-01T08:10:00+00:00"


def group_reminder(world, text="该吃药了", key="c1", at=IN_TEN, service=None, **extra):
    return execute(service or world.steward, "create_steward_schedule",
                   {"request_key": key, "title": "吃药", "instruction": text, "schedule": {"kind": "once", "at": at},
                    "target": "family_group", **extra}, allow_write=True)


def fire(world, schedule, key=None):
    return execute(world.steward, "fire_steward_schedule",
                   {"request_key": key or f"fire:{schedule['schedule_id']}:{schedule['next_run_at']}",
                    "schedule_id": schedule["schedule_id"], "expected_next_run_at": schedule["next_run_at"]},
                   allow_write=True)


def notice(world, schedule_id, due_at, key=None, service=None, **extra):
    return execute(service or world.steward, "post_group_notice",
                   {"request_key": key or f"group-notice:{schedule_id}:{due_at}", "schedule_id": schedule_id,
                    "due_at": due_at, **extra}, allow_write=True)


def fired_reminder(world, clock, text="该吃药了", key="c1"):
    made = group_reminder(world, text, key=key)
    clock.moment = datetime(2027, 1, 1, 8, 10, 5, tzinfo=timezone.utc)
    shot = fire(world, made)
    assert shot["fired"] is True
    return made, shot


def out_rows(world, kind="notice"):
    return rows(world, "SELECT channel, group_ref, body, request_key FROM state_group_messages WHERE direction='out' "
                       "AND kind=?", (kind,))


# --- C11: the target column ---------------------------------------------------------------------

def test_the_target_defaults_to_the_owner_and_a_row_written_before_it_keeps_its_meaning(tmp_path, clock):
    from core.state_database import StateDatabase
    db = StateDatabase(str(tmp_path / "old.sqlite"))
    with db.get_connection() as conn:  # the table as step 16 created it, with one row
        conn.executescript(state_steward.SCHEMA)
        conn.execute("INSERT INTO state_steward_schedules(schedule_id,version,title,instruction,kind,at,tz,enabled,"
                     "next_run_at,created_by,updated_by,created_at,updated_at) VALUES('sch-old',1,'t','look','once',"
                     "'2027-02-01T00:00:00+00:00','UTC',1,'2027-02-01T00:00:00+00:00','owner','owner','x','x')")
        conn.commit()
    owner = StateService(db, actor="owner:o@example.com", project_read_trusted=True)
    listed = owner.steward.list_schedules()["schedules"]
    assert [(s["schedule_id"], s["target"], s["target_channel"], s["instruction"]) for s in listed] == [
        ("sch-old", "owner", None, "look")]
    assert "target_unbound" not in listed[0]


def test_a_reminder_for_the_owner_is_unchanged_and_without_a_bound_group_no_group_reminder(world, clock):
    made = execute(world.steward, "create_steward_schedule", {"request_key": "o1", "title": "t", "instruction": "x",
                                                              "schedule": {"kind": "once", "at": IN_TEN}},
                   allow_write=True)
    assert made["target"] == "owner" and made["target_group_ref"] is None
    refused(lambda: group_reminder(world), "no_group_bound")
    with pytest.raises(StateGraphError):
        world.owner.steward.create_schedule("t", "x", {"kind": "once", "at": IN_TEN}, as_owner=True,
                                            target="family_group")


def test_a_group_reminder_records_the_binding_and_is_bounded(world, clock):
    bound_group(world)
    made = group_reminder(world)
    group = rows(world, "SELECT channel, group_ref, bound_at FROM state_owner_groups")[0]
    assert (made["target"], made["target_channel"], made["target_group_ref"], made["target_bound_at"]) == (
        "family_group", *group)
    assert made["target_unbound"] is False
    refused(lambda: group_reminder(world, "长" * 501, key="long"), "notice_too_long")
    refused(lambda: group_reminder(world, key="slack", channel="slack"), "no_group_bound")
    for i in range(state_steward.GROUP_SCHEDULES_MAX - 1):
        group_reminder(world, f"第{i}条", key=f"n{i}")
    refused(lambda: group_reminder(world, key="21st"), "too_many_group_reminders")


def test_create_and_change_with_a_private_project_name_are_withheld_on_the_tool_and_the_dashboard(
        world, clock, projects, client):
    bound_group(world)
    refused(lambda: group_reminder(world, "记得看 secret-plan"), "withheld")
    with pytest.raises(StateGraphError) as caught:
        group_reminder(world, "家庭账本该对了", key="c2")
    assert "private_project" in str(caught.value)
    r = client.post("/api/steward/schedules", headers=OWNER_HEADERS,
                    json={"title": "t", "instruction": "文件在 /home/linxuhao/x", "tz": "UTC",
                          "schedule": {"kind": "once", "at": IN_TEN}, "target": "family_group"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "withheld", r.text
    assert "host_path" in r.json()["detail"]["reason"]
    ok = client.post("/api/steward/schedules", headers=OWNER_HEADERS,
                     json={"title": "t", "instruction": "武虾传奇今晚更新", "tz": "UTC",
                           "schedule": {"kind": "once", "at": IN_TEN}, "target": "family_group"})
    assert ok.status_code == 200 and ok.json()["target"] == "family_group", ok.text
    change = client.put(f"/api/steward/schedules/{ok.json()['schedule_id']}", headers=OWNER_HEADERS,
                        json={"expected_version": 1, "instruction": "看看 secret-plan"})
    assert change.status_code == 422 and change.json()["detail"]["code"] == "withheld", change.text
    assert rows(world, "SELECT COUNT(*) FROM state_steward_schedules WHERE target='family_group'") == [(1,)]


# --- C11: post_group_notice ---------------------------------------------------------------------

def test_the_notice_takes_text_and_group_from_the_row_and_posts_once(world, clock):
    bound_group(world)
    made, shot = fired_reminder(world, clock)
    events = rows(world, "SELECT COUNT(*) FROM state_events")
    with pytest.raises(StateGraphError):  # a group in the call is no part of the request at all
        notice(world, made["schedule_id"], shot["due_at"], group_ref=ROOM2)
    posted = notice(world, made["schedule_id"], shot["due_at"])
    assert posted["posted"] is True and (posted["channel"], posted["group_ref"], posted["text"]) == (
        "wechat", ROOM, "该吃药了")
    again = notice(world, made["schedule_id"], shot["due_at"])
    assert again["duplicate"] is True and again["message_id"] == posted["message_id"]
    assert out_rows(world) == [("wechat", ROOM, "该吃药了", f"group-notice:{made['schedule_id']}:{shot['due_at']}")]
    # No State event: nothing wakes the steward.
    assert rows(world, "SELECT COUNT(*) FROM state_events") == events
    # A once row is disabled by its fire, and still posts: `enabled` is not read.
    assert rows(world, "SELECT enabled, last_due_at FROM state_steward_schedules") == [(0, shot["due_at"])]


def test_only_the_stewards_host_posts_a_notice(world, clock):
    bound_group(world)
    made, shot = fired_reminder(world, clock)
    with pytest.raises(StateGraphError):
        notice(world, made["schedule_id"], shot["due_at"], service=world.director)
    refused(lambda: notice(world, made["schedule_id"], shot["due_at"], service=public_view(world)),
            "public_view_read_only")
    assert out_rows(world) == []


def test_a_due_time_that_did_not_fire_or_a_deleted_row_is_not_fired(world, clock):
    bound_group(world)
    made, shot = fired_reminder(world, clock)
    assert notice(world, made["schedule_id"], "2027-01-01T09:10:00+00:00")["refused"] == "not_fired"
    assert notice(world, "sch-nope", shot["due_at"])["refused"] == "not_fired"
    other = group_reminder(world, key="c2", at="2027-01-01T09:00:00+00:00")
    assert notice(world, other["schedule_id"], other["next_run_at"])["refused"] == "not_fired"  # never fired
    execute(world.steward, "delete_steward_schedule", {"request_key": "d", "schedule_id": made["schedule_id"],
                                                       "expected_version": shot["schedule"]["version"]},
            allow_write=True)
    assert notice(world, made["schedule_id"], shot["due_at"], key="after-delete")["refused"] == "not_fired"
    assert out_rows(world) == []


def test_a_project_made_private_after_the_reminder_was_written_withholds_it_when_due(world, clock, projects):
    bound_group(world)
    made, shot = fired_reminder(world, clock, text="武虾传奇今晚更新")
    projects.close_project("wuxia-myth")
    refused_notice = notice(world, made["schedule_id"], shot["due_at"])
    assert (refused_notice["refused"], refused_notice["reason"], refused_notice["title"]) == (
        "withheld", "private_project", "吃药")
    assert out_rows(world) == []


def test_the_seventh_notice_of_an_hour_is_over_the_groups_budget(world, clock):
    bound_group(world)
    shots = []
    for i in range(7):
        clock.moment = datetime(2027, 1, 1, 8, 0, tzinfo=timezone.utc)
        made = group_reminder(world, f"第{i}条", key=f"c{i}", at=f"2027-01-01T08:{10 + i}:00+00:00")
        shots.append(made)
    clock.moment = datetime(2027, 1, 1, 8, 20, tzinfo=timezone.utc)
    answers = [notice(world, s["schedule_id"], fire(world, s)["due_at"]) for s in shots]
    assert [a.get("refused") for a in answers] == [None] * 6 + ["group_notice_budget"]
    assert len(out_rows(world)) == 6
    # The questions' 60 are counted apart: a member still asks.
    assert "message_id" in ask(world, "q1")


def test_the_accounts_hourly_outbound_cap_counts_the_notices_too(world, clock):
    bound_group(world)
    made, shot = fired_reminder(world, clock)
    with world.db.get_connection() as conn:
        for i in range(state_groups.GROUP_LIMITS["account_out_per_hour"]):
            conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,body,at) "
                         "VALUES(?,'wechat',?,'out','reply',1,'x',?)",
                         (f"o{i}", ROOM if i % 2 else "gone@chatroom", clock.moment.isoformat()))
        conn.commit()
    assert notice(world, made["schedule_id"], shot["due_at"])["refused"] == "group_quota_account"
    assert out_rows(world) == []


def test_unbinding_disables_the_groups_reminders_and_a_rebinding_never_revives_them(world, clock):
    bound_group(world)
    later = group_reminder(world, key="later", at="2027-01-02T08:00:00+00:00")
    made, shot = fired_reminder(world, clock, key="c-fired")
    unbound = world.owner.groups.unbind_owner_group("wechat")
    assert unbound["reminders_disabled"] == 1
    listed = {s["schedule_id"]: s for s in world.owner.steward.list_schedules()["schedules"]}
    assert listed[later["schedule_id"]]["enabled"] is False and listed[later["schedule_id"]]["target_unbound"] is True
    # The run that already fired meets the unbound group: refused, nothing posted.
    assert notice(world, made["schedule_id"], shot["due_at"])["refused"] == "group_unbound"
    # Bound again (another group): the old reminder stays off and cannot be turned on as it is ...
    code = world.owner.groups.issue_group_code("wechat", "新群")["code"]
    execute(world.steward, "bind_owner_group", {"request_key": "bg-2", "channel": "wechat", "group_ref": ROOM2,
                                                "sender_ref": OWNER_WX, "code": code, "mention_prefix": "@管家"},
            allow_write=True)
    row = listed[later["schedule_id"]]
    with pytest.raises(StateGraphError) as caught:
        world.owner.steward.update_schedule(row["schedule_id"], row["version"], enabled=True, as_owner=True)
    assert str(caught.value).startswith("group_unbound")
    # ... only re-pointed to the group bound now.
    repointed = world.owner.steward.update_schedule(row["schedule_id"], row["version"], enabled=True, as_owner=True,
                                                    target="family_group")
    assert repointed["enabled"] is True and repointed["target_group_ref"] == ROOM2
    assert out_rows(world) == []


def test_review_f1_a_channel_alone_never_turns_an_owner_reminder_into_group_text(world, clock):
    bound_group(world)
    mine = world.owner.steward.create_schedule("医生", "call the doctor about my results",
                                               {"kind": "once", "at": IN_TEN}, as_owner=True)
    refused(lambda: world.owner.steward.update_schedule(mine["schedule_id"], mine["version"], channel="wechat",
                                                        as_owner=True), "invalid_target")
    refused(lambda: execute(world.steward, "update_steward_schedule",
                            {"request_key": "u1", "schedule_id": mine["schedule_id"],
                             "expected_version": mine["version"], "channel": "wechat"}, allow_write=True),
            "invalid_target")
    assert rows(world, "SELECT target, target_channel FROM state_steward_schedules WHERE schedule_id=?",
                (mine["schedule_id"],)) == [("owner", None)]
    # On a group reminder a channel alone re-points it to that channel's group (still family_group).
    group = group_reminder(world, key="g1")
    moved = world.owner.steward.update_schedule(group["schedule_id"], group["version"], channel="wechat",
                                                as_owner=True)
    assert (moved["target"], moved["target_channel"], moved["target_group_ref"]) == ("family_group", "wechat", ROOM)


def changed_events(world):
    return rows(world, "SELECT COUNT(*) FROM state_events WHERE event_type='steward_schedule_changed'")[0][0]


def test_review_f3_both_disabling_paths_stamp_the_row_and_tell_the_host(world, clock):
    bound_group(world)
    made = group_reminder(world, at="2027-01-01T08:10:00+00:00")
    with world.db.get_connection() as conn:
        conn.execute("UPDATE state_owner_groups SET bound_at=? WHERE group_ref=?", ("2027-01-01T07:59:00+00:00", ROOM))
        conn.commit()
    clock.moment = datetime(2027, 1, 1, 8, 11, tzinfo=timezone.utc)
    shot = fire(world, made)
    before = changed_events(world)
    assert notice(world, made["schedule_id"], shot["due_at"])["refused"] == "group_unbound"
    assert rows(world, "SELECT enabled, updated_by, updated_at FROM state_steward_schedules") == [
        (0, "steward", clock.moment.isoformat())]
    assert changed_events(world) == before + 1


def test_review_f3_unbinding_emits_a_change_per_stopped_reminder(world, clock):
    bound_group(world)
    group_reminder(world, key="a", at="2027-01-02T08:00:00+00:00")
    group_reminder(world, key="b", text="倒垃圾", at="2027-01-02T09:00:00+00:00")
    before = changed_events(world)
    assert world.owner.groups.unbind_owner_group("wechat")["reminders_disabled"] == 2
    assert changed_events(world) == before + 2


def test_a_fired_reminder_whose_group_was_rebound_is_group_unbound_and_disabled(world, clock):
    bound_group(world)
    made = group_reminder(world, at="2027-01-01T08:10:00+00:00")
    # The binding changes under the row (bound_at differs) without the unbind path.
    with world.db.get_connection() as conn:
        conn.execute("UPDATE state_owner_groups SET bound_at=? WHERE group_ref=?", ("2027-01-01T07:59:00+00:00", ROOM))
        conn.commit()
    clock.moment = datetime(2027, 1, 1, 8, 11, tzinfo=timezone.utc)
    shot = fire(world, made)
    assert notice(world, made["schedule_id"], shot["due_at"])["refused"] == "group_unbound"
    assert rows(world, "SELECT enabled FROM state_steward_schedules") == [(0,)]
    assert out_rows(world) == []


def test_a_cron_group_reminder_posts_each_run_once(world, clock):
    bound_group(world)
    made = execute(
        world.steward, "create_steward_schedule", {"request_key": "cron", "title": "早安", "instruction": "早上好",
                                                   "schedule": {"kind": "cron", "expr": "0 9 * * *"}, "tz": "UTC",
                                                   "target": "family_group"}, allow_write=True)
    clock.moment = datetime(2027, 1, 1, 9, 0, 3, tzinfo=timezone.utc)
    first = fire(world, made)
    assert notice(world, made["schedule_id"], first["due_at"])["posted"] is True
    clock.moment = datetime(2027, 1, 2, 9, 0, 3, tzinfo=timezone.utc)
    second = fire(world, first["schedule"])
    assert notice(world, made["schedule_id"], first["due_at"], key="stale")["refused"] == "not_fired"
    assert notice(world, made["schedule_id"], second["due_at"])["posted"] is True
    assert [r[2] for r in out_rows(world)] == ["早上好", "早上好"]


# --- C4: the family lists -----------------------------------------------------------------------

def lists_view(world):
    return family_view(world)


def show(service, **extra):
    return execute(service, "list_list_items", extra)


def add(service, items, key="a1", lst="shopping", **extra):
    return execute(service, "add_list_items", {"request_key": key, "list": lst, "items": items, **extra},
                   allow_write=True)


def check(service, ids, key="k1", done=True, **extra):
    return execute(service, "check_list_items", {"request_key": key, "item_ids": ids, "done": done, **extra},
                   allow_write=True)


def test_in_a_group_turn_the_lists_are_read_and_written_and_the_author_is_the_records(world, clock):
    bound_group(world)
    ask(world, "m1", "加牛奶和鸡蛋")
    audience(world, senders=(MEMBER,))
    world.owner.groups.name_group_member("wechat", MEMBER, "妈妈")
    view = lists_view(world)
    added = add(view, ["牛奶", " 鸡蛋 ", "牛奶"], turn="t1")
    assert [(i["text"], i["added_by"], i["added_via"], i["added"]) for i in added["items"]] == [
        ("牛奶", "妈妈", "group", True), ("鸡蛋", "妈妈", "group", True)]
    # A name in the call is no part of the request: the author is the audience record's.
    with pytest.raises(StateGraphError):
        add(view, ["面包"], key="a2", turn="t1", added_by="爸爸")
    listed = show(view, turn="t1")["lists"]
    assert [i["text"] for i in listed["shopping"]] == ["牛奶", "鸡蛋"] and listed["todo"] == []
    # The same fields the owner sees.
    assert listed["shopping"] == world.owner.lists.list_items(as_owner=True)["lists"]["shopping"]
    milk = listed["shopping"][0]["item_id"]
    checked = check(view, [milk, "li-nope"], turn="t1")
    assert checked["items"][0]["done_by"] == "妈妈" and checked["unknown"] == ["li-nope"]
    assert check(view, [milk], key="k2", done=False, turn="t1")["items"][0]["done"] is False
    # A merged turn of several askers names the family group.
    audience(world, senders=(MEMBER, MEMBER2), turn="t2")
    assert add(view, ["面包"], key="a3", turn="t2")["items"][0]["added_by"] == "家庭群"
    # Nothing wakes anyone.
    assert rows(world, "SELECT COUNT(*) FROM state_events WHERE event_type LIKE '%list%'") == [(0,)]


def test_with_the_header_only_the_two_list_writes_and_the_read_are_open_and_only_for_a_group_turn(world, clock):
    bound_group(world)
    view = lists_view(world)
    # No audience recorded yet, then an owner audience: refused.
    refused(lambda: show(view, turn="t1"), "lists_group_turn_only")
    audience(world, "owner", turn="t1")
    refused(lambda: show(view, turn="t1"), "lists_group_turn_only")
    refused(lambda: add(view, ["牛奶"], turn="t1"), "lists_group_turn_only")
    refused(lambda: check(view, ["x"], turn="t1"), "lists_group_turn_only")
    audience(world, turn="t2")
    item = add(view, ["牛奶"], key="ok", turn="t2")["items"][0]["item_id"]
    # remove and every other write stay closed; other private reads too.
    refused(lambda: execute(view, "remove_list_items", {"request_key": "r", "item_ids": [item]}, allow_write=True),
            "public_view_read_only")
    refused(lambda: execute(view, "create_steward_schedule", {"request_key": "s", "title": "t", "instruction": "x",
                                                              "schedule": {"kind": "once", "at": IN_TEN}},
                            allow_write=True), "public_view_read_only")
    for action in ("list_steward_schedules", "list_owner_groups"):
        with pytest.raises(ProjectPrivate):
            execute(view, action, {})
    # The anonymous form of the header (no trusted credential) reads nothing.
    with pytest.raises(ProjectPrivate):
        show(public_view(world), turn="t2")


def test_the_list_requests_name_the_live_turn_and_a_stale_record_serves_no_other_turn(world, clock):
    bound_group(world)
    audience(world, turn="t1")
    view = lists_view(world)
    assert add(view, ["牛奶"], turn="t1")["items"][0]["added"] is True
    refused(lambda: add(view, ["鸡蛋"], key="a2", turn="t2"), "lists_group_turn_only")
    refused(lambda: show(view, turn="t2"), "lists_group_turn_only")
    refused(lambda: show(view), "invalid_request")  # no turn with the header
    refused(lambda: add(view, ["鸡蛋"], key="a3"), "invalid_request")
    audience(world, turn="t2")
    refused(lambda: add(view, ["鸡蛋"], key="a4", turn="t1"), "lists_group_turn_only")  # t1 late
    assert [i["text"] for i in show(view, turn="t2")["lists"]["shopping"]] == ["牛奶"]


def test_an_owner_turn_and_the_dashboard_use_the_lists_without_the_header(world, clock):
    milk = add(world.steward, ["牛奶"])["items"][0]
    assert (milk["added_by"], milk["added_by_role"], milk["added_via"]) == ("管家", "steward", "owner_dm")
    # The steward in an owner turn checks and removes; no turn needed (and none is read).
    assert check(world.steward, [milk["item_id"]])["items"][0]["done_by"] == "管家"
    removed = execute(world.steward, "remove_list_items", {"request_key": "r1", "item_ids": [milk["item_id"]]},
                      allow_write=True)
    assert removed["removed"] == [milk["item_id"]]
    task = world.owner.lists.add_items("todo", ["交水费"], as_owner=True)["items"][0]
    assert (task["added_by"], task["added_via"]) == ("业主", "dashboard")
    with pytest.raises(StateGraphError):
        world.steward.lists.add_items("todo", ["x"], as_owner=True)  # the dashboard path is the owner's alone
    with pytest.raises(StateGraphError):
        execute(world.director, "add_list_items", {"request_key": "d", "list": "todo", "items": ["x"]},
                allow_write=True)


def test_an_owner_dm_item_naming_a_private_project_is_withheld_and_the_group_reply_stays_clear(
        world, clock, projects):
    bound_group(world)
    refused(lambda: add(world.steward, ["给家庭账本记一笔"]), "withheld")
    refused(lambda: world.owner.lists.add_items("todo", ["看 /home/x"], as_owner=True), "withheld")
    assert rows(world, "SELECT COUNT(*) FROM state_steward_list_items") == [(0,)]
    add(world.steward, ["牛奶"], key="a2")
    # The group asks for the list: the reply that lists it goes out as written.
    question = ask(world, "m1", "清单上有什么")
    listing = "、".join(i["text"] for i in world.owner.lists.list_items(as_owner=True)["lists"]["shopping"])
    assert reply(world, question["message_id"], "购物清单：" + listing)["withheld"] is False


def test_review_f3_a_group_turn_learns_only_that_an_item_was_withheld_never_why(world, clock, projects):
    bound_group(world)
    audience(world, senders=(MEMBER,), turn="t1")
    with pytest.raises(StateGraphError) as caught:
        add(lists_view(world), ["给家庭账本记一笔"], turn="t1")
    assert str(caught.value) == "withheld: nothing was added"
    # The owner's turn and the dashboard still hear the kind.
    with pytest.raises(StateGraphError) as owner:
        world.owner.lists.add_items("todo", ["给家庭账本记一笔"], as_owner=True)
    assert str(owner.value).startswith("withheld: private_project")
    assert rows(world, "SELECT COUNT(*) FROM state_steward_list_items") == [(0,)]


def test_the_list_bounds_dedupe_and_thirty_day_purge(world, clock):
    first = add(world.steward, ["牛奶"])["items"][0]
    again = add(world.steward, ["  牛奶 "], key="a2")["items"][0]
    assert again["item_id"] == first["item_id"] and again["added"] is False
    with pytest.raises(StateGraphError):
        add(world.steward, [f"x{i}" for i in range(11)], key="a3")
    refused(lambda: add(world.steward, ["长" * 101], key="a4"), "item_too_long")
    for start in range(1, 100, 10):
        add(world.steward, [f"物品{n}" for n in range(start, min(start + 10, 100))], key=f"b{start}")
    assert len(show(world.steward)["lists"]["shopping"]) == 100
    with pytest.raises(StateConflict) as full:
        add(world.steward, ["第101条"], key="full")
    assert str(full.value).startswith("list_full")
    # A checked item goes 30 days after it was checked (on the next write).
    check(world.steward, [first["item_id"]])
    clock.moment += timedelta(days=31)
    add(world.steward, ["面包"], key="later", lst="todo")
    assert rows(world, "SELECT COUNT(*) FROM state_steward_list_items WHERE item_id=?", (first["item_id"],)) == [(0,)]


def test_the_list_routes_are_the_owner_sessions_alone(world, clock, client):
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    for method, path, body in (("GET", "/api/steward/lists", None),
                               ("POST", "/api/steward/lists/shopping/items", {"items": ["牛奶"]}),
                               ("POST", "/api/steward/lists/check", {"item_ids": ["x"]}),
                               ("DELETE", "/api/steward/lists/items/x", None),
                               ("POST", "/api/steward/lists/clear-done", None)):
        r = client.request(method, path, json=body, headers=steward)
        assert r.status_code == 403, (method, path, r.status_code, r.text)
    added = client.post("/api/steward/lists/shopping/items", json={"items": ["牛奶", "鸡蛋"]}, headers=OWNER_HEADERS)
    assert added.status_code == 200, added.text
    milk, eggs = (i["item_id"] for i in added.json()["items"])
    assert client.post("/api/steward/lists/check", json={"item_ids": [milk]}, headers=OWNER_HEADERS).status_code == 200
    assert client.delete(f"/api/steward/lists/items/{eggs}", headers=OWNER_HEADERS).status_code == 200
    assert client.post("/api/steward/lists/clear-done", headers=OWNER_HEADERS).json() == {"cleared": 1}
    assert client.get("/api/steward/lists", headers=OWNER_HEADERS).json()["lists"] == {"shopping": [], "todo": []}
    bad = client.post("/api/steward/lists/shopping/items", json={"items": ["看 /home/x"]}, headers=OWNER_HEADERS)
    assert bad.status_code == 422 and bad.json()["detail"].startswith("withheld"), bad.text


def test_acceptance_four_a_member_adds_the_owner_checks_in_dm_the_dashboard_removes(world, clock):
    """7.9.7 item 4, as the backend sees it."""
    bound_group(world)
    ask(world, "m1", "@管家 加牛奶和鸡蛋")
    audience(world, senders=(MEMBER,), turn="t1")
    world.owner.groups.name_group_member("wechat", MEMBER, "妈妈")
    view = lists_view(world)
    add(view, ["牛奶", "鸡蛋"], turn="t1")
    board = world.owner.lists.list_items(as_owner=True)["lists"]["shopping"]
    assert [(i["text"], i["added_by"]) for i in board] == [("牛奶", "妈妈"), ("鸡蛋", "妈妈")]
    audience(world, "owner", turn="t2")  # the owner's DM: 「牛奶买了」
    check(world.steward, [board[0]["item_id"]])
    world.owner.lists.remove_items([board[1]["item_id"]], as_owner=True)
    audience(world, senders=(MEMBER,), turn="t3")  # the group asks again
    seen = show(view, turn="t3")["lists"]["shopping"]
    assert [(i["text"], i["done"]) for i in seen] == [("牛奶", True)]
