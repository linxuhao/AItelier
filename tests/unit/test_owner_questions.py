"""Step 22 (design-steward 7.6): the directors' open questions pushed to the owner's default
chat channel, reminded, and answered there (core/state_owner_questions.py).

The clock is the module's own (`_now`, with core.state_channels' for the channel messages'
received time); a question's time is set on its row. Unless a test says otherwise it is
daytime in the owner's zone (Europe/Paris)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from core import drivers, state_channels, state_owner_questions as oq
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

HOME = "aitelier-home"
OWNER = "owner@example.com"
PEPPER = "owner-questions-pepper-" * 3
WX, SLACK = "wx-owner", "U0OWNER"
T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)   # 11:00 in Paris


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(drivers, "read_pepper", lambda: PEPPER)
    monkeypatch.delenv("AITELIER_OWNER_TZ", raising=False)
    clock = {"now": T0}
    monkeypatch.setattr(oq, "_now", lambda: clock["now"])
    monkeypatch.setattr(state_channels, "_now", lambda: clock["now"])
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    registry = DriverRegistry(db, PEPPER)
    registry.seed("owner-questions-cli-token")
    seeder = StateService(db, actor="fixture", project_read_trusted=True)
    for project, title in (("alpha", "武侠存档"), ("beta", "开票服务"), (HOME, "Home")):
        seeder.store.create_project(project, title)
    runtime = DshDrivers(db, registry, tmp_path / "drivers",
                         command=[sys.executable, "-c", "import time; time.sleep(90)"])
    rows = {p: runtime._provision(p, "aitelier", "driver", "fixture") for p in (HOME, "alpha", "beta")}

    def as_driver(row):
        return StateService(db, actor="driver:" + row["driver_id"], driver_id=row["driver_id"],
                            project_read_trusted=True)

    owner = StateService(db, actor="owner:" + OWNER, project_read_trusted=True)
    w = type("World", (), dict(db=db, clock=clock, owner=owner, rows=rows, steward=as_driver(rows[HOME]),
                               alpha=as_driver(rows["alpha"]), beta=as_driver(rows["beta"])))
    for channel, sender in (("wechat", WX), ("slack", SLACK)):
        code = owner.channels.issue_code(channel)["code"]
        execute(w.steward, "bind_owner_channel", {"request_key": f"bind-{channel}", "channel": channel,
                                                  "sender_ref": sender, "code": code}, allow_write=True)
    owner.channels.set_default("wechat")
    return w


def tick(world, **delta):
    world.clock["now"] = world.clock["now"] + timedelta(**delta)


def ask(world, director, project, qid, text, context="", ago=timedelta(minutes=5)):
    """A director's question (as ask_owner writes it), dated `ago` before now."""
    out = execute(director, "send_user_message", {"request_key": f"ask-{project}-{qid}-{text[:8]}", "project_id": project,
                                                  "subject": "Question for you", "body": context,
                                                  "segments": [{"kind": "question", "id": qid, "text": text}]},
                  allow_write=True)
    with world.db.get_connection() as conn:
        conn.execute("UPDATE driver_inbox_messages SET created_at=? WHERE message_id=?",
                     ((world.clock["now"] - ago).isoformat(), out["message_id"]))
        conn.commit()
    return out["message_id"]


def sync(world):
    return execute(world.steward, "sync_owner_questions", {}, allow_write=True)


def channel_message(world, body, ref, channel="wechat", sender=WX):
    """An owner message from a channel, received now (relay_owner_message, 7.2)."""
    return execute(world.steward, "relay_owner_message", {
        "request_key": f"relay-{ref}", "channel": channel, "sender_ref": sender, "channel_message_ref": ref,
        "body": body, "channel_at": world.clock["now"].isoformat()}, allow_write=True)["message_id"]


def relay(world, message_id, key=None):
    return execute(world.steward, "relay_owner_answer",
                   {"request_key": key or f"relay-answer:{message_id}", "source_message_id": message_id},
                   allow_write=True)


def ledger(world):
    with world.db.get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM state_owner_question_pushes ORDER BY code_no")]


def pushed_rows(world):
    with world.db.get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT message_id,body,created_at FROM driver_inbox_messages "
                                              "WHERE subject=? ORDER BY created_at,rowid", (oq.PUSH_SUBJECT,))]


def deliveries(world, driver_row):
    with world.db.get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT d.seq,m.subject,m.body,m.answers_json,m.reply_to_message_id,m.message_id FROM driver_inbox_deliveries d "
            "JOIN driver_inbox_messages m USING(message_id) WHERE d.target_driver_id=? AND m.channel='user' "
            "ORDER BY d.seq", (driver_row["driver_id"],))]


def record_delivery(world, message_id, error=None):
    execute(world.steward, "record_channel_delivery", {"request_key": f"d-{message_id}", "message_id": message_id,
                                                       "channel": "wechat", **({"error": error} if error else {})},
            allow_write=True)


def open_questions(world, project):
    return execute(world.owner, "list_user_questions", {"project_id": project})["questions"]


def refused(call, code):
    with pytest.raises(StateGraphError) as caught:
        call()
    assert str(caught.value).split(":", 1)[0] == code, str(caught.value)


# --------------------------------------------------------------------------- sync
def test_sync_without_a_default_channel_writes_nothing_and_assigns_no_code(world):
    world.owner.channels.set_default(None)
    ask(world, world.alpha, "alpha", "q1", "用方案 A 还是 B？")
    assert sync(world) == {"default_channel": None, "messages": [], "unmirrored": []}
    assert ledger(world) == [] and pushed_rows(world) == []


def test_a_question_open_less_than_two_minutes_waits_then_is_pushed_with_a_code(world):
    ask(world, world.alpha, "alpha", "q1", "用方案 A 还是 B？", ago=timedelta(seconds=90))
    assert sync(world)["messages"] == [] and ledger(world) == []
    tick(world, seconds=31)
    out = sync(world)
    assert out["default_channel"] == "wechat" and len(out["messages"]) == 1
    body = out["messages"][0]["body"]
    code = ledger(world)[0]["code_no"]
    assert f"\nQ{code} · 武侠存档\n问：\n｜用方案 A 还是 B？" in body
    assert ledger(world)[0]["push_message_id"] == out["messages"][0]["message_id"]
    # The push is the steward's own message in its conversation, under the fixed subject.
    assert [r["message_id"] for r in pushed_rows(world)] == [out["messages"][0]["message_id"]]
    assert sync(world)["messages"] == [], "pushed once"


def test_the_push_and_its_ledger_commit_together_and_an_unmirrored_push_is_handed_back(world, monkeypatch):
    ask(world, world.alpha, "alpha", "q1", "问题一")

    def broken(chunks, limit=oq.PUSH_CHARS):
        raise RuntimeError("the write failed")
    monkeypatch.setattr(oq, "pack", broken)
    with pytest.raises(RuntimeError):
        sync(world)
    assert ledger(world) == [] and pushed_rows(world) == [], "no code without its message"
    monkeypatch.undo()
    monkeypatch.setattr(oq, "_now", lambda: world.clock["now"])
    monkeypatch.setattr(state_channels, "_now", lambda: world.clock["now"])
    monkeypatch.setattr(drivers, "read_pepper", lambda: PEPPER)
    first = sync(world)["messages"][0]
    assert sync(world)["unmirrored"] == [], "not before 2 minutes"
    tick(world, minutes=3)
    assert sync(world)["unmirrored"] == [first], "written, never mirrored: handed back"
    record_delivery(world, first["message_id"])
    assert sync(world)["unmirrored"] == []


def test_a_steward_reply_under_the_push_subject_is_not_handed_back_as_unmirrored(world):
    # A reply to a dashboard message stays on the dashboard (the mirror records nothing for it).
    dash = execute(world.owner, "send_user_conversation_message",
                   {"request_key": "d1", "project_id": HOME, "body": "有什么要我答的？"}, allow_write=True)["message_id"]
    reply = execute(world.steward, "send_user_message", {"request_key": "r1", "project_id": HOME,
                                                         "subject": oq.PUSH_SUBJECT, "body": "没有",
                                                         "reply_to_message_id": dash}, allow_write=True)["message_id"]
    with world.db.get_connection() as conn:
        conn.execute("UPDATE driver_inbox_messages SET created_at=? WHERE message_id=?", (T0.isoformat(), reply))
        conn.commit()
    tick(world, minutes=3)
    assert sync(world)["unmirrored"] == []


def test_codes_are_monotonic_never_reused_and_one_per_message_and_segment(world):
    ask(world, world.alpha, "alpha", "same", "第一个")
    ask(world, world.alpha, "alpha", "same", "第二个，同一个段 id", ago=timedelta(minutes=4))
    sync(world)
    first, second = ledger(world)
    assert second["code_no"] == first["code_no"] + 1 and first["segment_id"] == second["segment_id"] == "same"
    for row in (first, second):
        execute(world.owner, "send_user_conversation_message", {
            "request_key": f"dash-{row['code_no']}", "project_id": "alpha", "body": "好",
            "answers": [{"message_id": row["message_id"], "segment_id": "same"}]}, allow_write=True)
    ask(world, world.alpha, "alpha", "q3", "第三个")
    tick(world, minutes=20)
    sync(world)
    assert [r["code_no"] for r in ledger(world)] == [first["code_no"], second["code_no"], second["code_no"] + 1]


def test_a_backlog_older_than_a_day_is_one_line_each_once_at_most_twenty_and_never_reminded(world):
    for n in range(23):
        ask(world, world.alpha, "alpha", f"old{n}", f"旧问题 {n}\n第二行不出现", ago=timedelta(hours=30, minutes=n))
    out = sync(world)
    text = "\n".join(m["body"] for m in out["messages"])
    assert "较早的问题：" in text and "另有 3 个，见面板。" in text and "第二行不出现" not in text
    rows = [r for r in ledger(world) if r["pushed_at"] is not None]
    assert len(rows) == 20 and all(r["older"] == 1 for r in rows)
    assert out["messages"][0]["body"].startswith(f"【待你回答】20 个较早的问题。回复时以编号开头，例如「Q{rows[0]['code_no']} 用方案 A」")
    assert all(len(m["body"]) <= oq.PUSH_CHARS for m in out["messages"])
    # Once: the 3 named only as a count are never pushed later, listed, or noticed.
    rest = [r for r in ledger(world) if r["pushed_at"] is None]
    assert len(rest) == 3 and all(r["older"] == 1 and r["push_message_id"] is None for r in rest)
    assert not any(f"Q{r['code_no']} " in text for r in rest)
    tick(world, minutes=1)
    assert sync(world)["messages"] == [], "the rest stay on the dashboard"
    assert len(execute(world.steward, "list_pushed_owner_questions", {})["questions"]) == 20
    execute(world.owner, "send_user_conversation_message", {
        "request_key": "dash-rest", "project_id": "alpha", "body": "面板上答",
        "answers": [{"message_id": rest[0]["message_id"], "segment_id": rest[0]["segment_id"]}]}, allow_write=True)
    tick(world, minutes=20)
    assert sync(world)["messages"] == [], "no notice for a code never shown"
    tick(world, hours=25)
    later = sync(world)
    assert "【提醒】" not in "\n".join(m["body"] for m in later["messages"]), "an older question is never reminded"


def test_the_home_project_and_archived_projects_are_left_out_and_an_archive_after_the_push_closes_quietly(world):
    ask(world, world.steward, HOME, "s1", "管家自己的问题")
    ask(world, world.alpha, "alpha", "q1", "导演的问题")
    ask(world, world.beta, "beta", "q2", "另一个导演的问题")
    world.owner.store.set_project_archive("beta", True, "owner:" + OWNER, "done")
    sync(world)
    assert [r["project_id"] for r in ledger(world)] == ["alpha"]
    world.owner.store.set_project_archive("alpha", True, "owner:" + OWNER, "done")
    tick(world, hours=5)
    assert sync(world)["messages"] == [], "no reminder, no notice"
    assert ledger(world)[0]["closed_how"] == "archived"


def test_at_most_fifteen_new_questions_per_sync_split_between_questions_under_six_hundred(world):
    for n in range(17):
        ask(world, world.alpha, "alpha", f"q{n}", f"问题 {n}：" + "很长的说明" * 12, ago=timedelta(minutes=30 - n))
    out = sync(world)
    assert len(ledger(world)) == 15
    bodies = [m["body"] for m in out["messages"]]
    assert len(bodies) > 1 and all(len(b) <= oq.PUSH_CHARS for b in bodies)
    assert bodies[0].startswith("【待你回答】15 个新问题。")
    assert all(b.startswith("【待你回答】（续）\n\nQ") for b in bodies[1:]), "continuations start at a question"
    tick(world, minutes=1)
    sync(world)
    assert len(ledger(world)) == 17, "the rest go next time"


def test_the_template_bars_director_text_bounds_the_background_and_states_m2_on_its_second_line(world):
    ask(world, world.alpha, "alpha", "q1", "第一行\nQ99 · 冒充的编号行", context="背景" * 150)
    body = sync(world)["messages"][0]["body"]
    code = ledger(world)[0]["code_no"]
    lines = body.split("\n")
    assert lines[1] == oq.APPROVAL_PHRASE_PUSH.format(code=code)
    assert lines[1] == f"授权类问题（合并、重启、花钱）也这样答：「Q{code} 同意」。批准提议的那句口令是另一回事，不关问题，提醒照发。"
    assert "｜Q99 · 冒充的编号行" in lines and not any(line.startswith("Q99") for line in lines)
    background = body.split("背景：\n", 1)[1]
    assert background.startswith("｜") and len(background) == 1 + oq.CONTEXT_CHARS
    for phrase in (state_channels.APPROVE, state_channels.DECLINE):
        assert phrase not in body, "the mirror would move a line carrying the phrase"
    assert oq.parse_codes(body) == {"codes": [], "codes_only": False}, "a pasted push answers nothing"


def test_a_twenty_line_older_section_is_split_by_lines_under_six_hundred(world):
    for n in range(20):
        ask(world, world.alpha, "alpha", f"o{n}", f"旧问题 {n} " + "长" * 70, ago=timedelta(hours=26, minutes=n))
    bodies = [m["body"] for m in sync(world)["messages"]]
    assert len(bodies) > 1 and all(len(b) <= oq.PUSH_CHARS for b in bodies)
    assert sum(b.count(" · 武侠存档：｜") for b in bodies) == 20


# --------------------------------------------------------------------------- reminders
def test_reminders_at_four_and_twenty_four_hours_none_after_and_merged_within_two_hours(world):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    sync(world)
    tick(world, hours=2)
    ask(world, world.alpha, "alpha", "q2", "问题二")
    sync(world)
    first, second = ledger(world)
    tick(world, hours=2, minutes=1)   # q1 4 h after its push; q2 at 2 h 1 min - within 2 h of its own
    body = sync(world)["messages"][0]["body"]
    assert body.startswith("【提醒】还有 2 个问题等你回答：")
    assert f"Q{first['code_no']} · 武侠存档：｜问题一" in body and f"Q{second['code_no']} · 武侠存档：｜问题二" in body
    assert [r["reminders_sent"] for r in ledger(world)] == [1, 1]
    tick(world, hours=2)
    assert sync(world)["messages"] == [], "q2 was reminded with q1: no second message an hour later"
    tick(world, hours=18)             # q1 at 24 h 1 min, q2 within 2 h of its own 24 h
    assert "【提醒】" in sync(world)["messages"][0]["body"]
    assert [r["reminders_sent"] for r in ledger(world)] == [2, 2]
    tick(world, days=3)
    assert sync(world)["messages"] == [], "no third reminder"


@pytest.mark.parametrize("local_day", ["2026-11-20", "2027-03-28", "2027-10-31"])
def test_quiet_hours_by_the_owners_wall_clock_across_both_dst_changes(world, local_day):
    from zoneinfo import ZoneInfo
    paris = ZoneInfo("Europe/Paris")

    def at(hhmm, day=local_day):
        hour, minute = map(int, hhmm.split(":"))
        return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=paris).astimezone(timezone.utc)
    next_day = (datetime.fromisoformat(local_day) + timedelta(days=1)).date().isoformat()
    for moment, quiet in ((at("22:59"), False), (at("23:00"), True), (at("07:59", next_day), True),
                          (at("08:00", next_day), False)):
        assert oq.is_quiet(moment) is quiet, (moment, quiet)
    world.clock["now"] = at("23:00")
    ask(world, world.alpha, "alpha", "q1", "夜里的问题")
    assert sync(world)["messages"] == [] and ledger(world) == [], "quiet: nothing written"
    world.clock["now"] = at("08:00", next_day)
    assert len(sync(world)["messages"]) == 1, "the first look after 08:00 sends it"


def test_a_closed_question_is_not_reminded_and_a_channel_switched_off_and_back_catches_up_once(world):
    ask(world, world.alpha, "alpha", "q1", "问题一", ago=timedelta(minutes=6))
    ask(world, world.alpha, "alpha", "q2", "问题二")
    sync(world)
    first, second = ledger(world)
    execute(world.owner, "send_user_conversation_message", {
        "request_key": "dash-1", "project_id": "alpha", "body": "A",
        "answers": [{"message_id": first["message_id"], "segment_id": "q1"}]}, allow_write=True)
    world.owner.channels.set_default(None)
    tick(world, hours=30)
    assert sync(world)["messages"] == []
    world.owner.channels.set_default("slack")
    body = sync(world)["messages"][0]["body"]
    assert f"Q{second['code_no']} · " in body and f"Q{first['code_no']} · " not in body
    assert body.count("【提醒】") == 1 and [r["reminders_sent"] for r in ledger(world)] == [0, 2]
    tick(world, hours=1)
    assert sync(world)["messages"] == [], "caught up once, to the count it should have"


def test_a_failed_push_is_resent_in_full_on_the_next_inbound_message_or_at_its_reminder(world):
    ask(world, world.alpha, "alpha", "q1", "问题一", context="背景一")
    ask(world, world.beta, "beta", "q2", "问题二", context="背景二")
    message = sync(world)["messages"][0]
    record_delivery(world, message["message_id"], error="recipient_unknown")
    tick(world, minutes=5)
    assert sync(world)["messages"] == [], "a failing channel is not written to every minute"
    channel_message(world, "在吗", "wx-1")   # the bound account wrote: the channel works again
    resent = sync(world)["messages"][0]["body"]
    assert resent.startswith("【待你回答】2 个新问题。") and "｜背景一" in resent and "｜背景二" in resent
    assert all(r["push_failed"] == 0 for r in ledger(world))
    record_delivery(world, pushed_rows(world)[-1]["message_id"], error="send_timeout")
    tick(world, hours=4)
    body = sync(world)["messages"][0]["body"]
    assert "【提醒】" in body and "｜背景一" in body, "owed at its reminder: in full"


def test_a_failed_push_resent_when_its_reminder_is_due_is_that_reminder(world):
    ask(world, world.alpha, "alpha", "q1", "问题一", context="背景一")
    message = sync(world)["messages"][0]
    record_delivery(world, message["message_id"], error="recipient_unknown")
    tick(world, hours=4, minutes=1)
    channel_message(world, "在吗", "wx-1")   # owed now, and its 4 h reminder is due
    resent = sync(world)["messages"][0]["body"]
    assert "｜背景一" in resent
    assert ledger(world)[0]["reminders_sent"] == 1 and ledger(world)[0]["last_reminded_at"] is not None
    tick(world, minutes=1)
    assert sync(world)["messages"] == [], "the resend was the reminder: none a minute later"


# --------------------------------------------------------------------------- notices
def test_closed_notices_say_where_once_and_wait_for_a_message_or_fifteen_minutes(world):
    ask(world, world.alpha, "alpha", "q1", "问题一", ago=timedelta(minutes=7))
    ask(world, world.beta, "beta", "q2", "问题二", ago=timedelta(minutes=6))
    ask(world, world.alpha, "alpha", "q3", "问题三")
    sync(world)
    one, two, three = ledger(world)
    execute(world.owner, "send_user_conversation_message", {
        "request_key": "dash-1", "project_id": "alpha", "body": "A",
        "answers": [{"message_id": one["message_id"], "segment_id": "q1"}]}, allow_write=True)
    execute(world.beta, "withdraw_user_question", {"request_key": "w-2", "project_id": "beta",
                                                   "message_id": two["message_id"], "segment_id": "q2",
                                                   "reason": "不用了"}, allow_write=True)
    # q3 answered from the other bound channel (Slack while WeChat is the default).
    relay(world, channel_message(world, f"Q{three['code_no']} 好", "s-1", channel="slack", sender=SLACK))
    tick(world, minutes=1)
    assert sync(world)["messages"] == [], "a notice alone waits"
    tick(world, minutes=15)
    body = sync(world)["messages"][0]["body"]
    assert body == (f"已在面板回答：Q{one['code_no']}（武侠存档）。已撤回：Q{two['code_no']}（开票服务）。"
                    f"已在 Slack 回答：Q{three['code_no']}（武侠存档）。")
    tick(world, hours=1)
    assert sync(world)["messages"] == [], "each once"


def test_an_answer_relayed_from_the_default_channel_gets_no_notice_and_a_notice_rides_a_push(world):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    sync(world)
    (one,) = ledger(world)
    relay(world, channel_message(world, f"Q{one['code_no']} 用 A", "wx-1"))
    ask(world, world.beta, "beta", "q2", "问题二")
    tick(world, minutes=3)
    body = sync(world)["messages"][0]["body"]
    assert "已在" not in body, "its receipt already went to that channel"
    assert ledger(world)[0]["noticed_at"] is not None


# --------------------------------------------------------------------------- relay_owner_answer
def test_only_the_steward_relays(world):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    sync(world)
    source = channel_message(world, f"Q{ledger(world)[0]['code_no']} 好", "wx-1")
    for service in (world.alpha, world.owner):
        with pytest.raises(StateGraphError) as caught:
            execute(service, "relay_owner_answer", {"project_id": HOME, "request_key": "x", "source_message_id": source},
                    allow_write=True)
        assert "steward" in str(caught.value)
    for service in (world.alpha, world.owner):
        with pytest.raises(StateGraphError):
            execute(service, "sync_owner_questions", {"project_id": HOME}, allow_write=True)


@pytest.mark.parametrize("body, codes", [
    ("Q12用A", [12]), ("Q12、Q13 都同意", [12, 13]), ("Ｑ１２ 用方案 A", [12]), ("q12 好", [12]),
    ("  Q12, Q13/Q14，Q15 都行", [12, 13, 14, 15]), ("Q12 同意合并\nQ13 不要合并", [12, 13]),
    ("Q012 好", []), ("我觉得 Q12 可以", []), ("Q12 · 武侠存档", []), ("Q12 · 武侠存档：｜问题一\nQ13 好", [13]),
    ("Q12", [12]), ("Q12 Q13", [12, 13]), ("Q12！", [12]), ("Q120用A", [120]), ("Q12\n我同意", []),
])
def test_the_numbering_rule(body, codes):
    assert oq.parse_codes(body)["codes"] == codes, body


def test_the_codes_only_shape():
    assert oq.parse_codes("Q12 Q13")["codes_only"] is True
    assert oq.parse_codes("Q12。")["codes_only"] is True
    assert oq.parse_codes("Q12 好")["codes_only"] is False
    assert oq.parse_codes("Q12\n我同意")["codes_only"] is False


def test_the_wechat_quote_format_is_not_guessed_and_a_measured_one_is_stripped(monkeypatch):
    # NOT MEASURED yet (design 7.6.11 step 0): nothing is taken for a quote, and the guessed
    # dashed separator of the earlier drafts is not the rule.
    assert oq.WECHAT_QUOTE_SEPARATOR is None and oq.WECHAT_QUOTE_BEFORE_OWN_WORDS is None
    quoted = "「管家：Q12 · 武侠存档」\nQ13 那条也行\n<<sep>>\nQ12 用 A"
    assert oq.parse_codes(quoted, "wechat")["codes"] == [13, 12]
    # Once measured (a placeholder format here, not WeChat's): the quoted part answers nothing.
    monkeypatch.setattr(oq, "WECHAT_QUOTE_SEPARATOR", "<<sep>>")
    monkeypatch.setattr(oq, "WECHAT_QUOTE_BEFORE_OWN_WORDS", True)
    assert oq.parse_codes(quoted, "wechat")["codes"] == [12]
    assert oq.parse_codes(quoted, "slack")["codes"] == [13, 12], "Slack has no such quote"
    monkeypatch.setattr(oq, "WECHAT_QUOTE_BEFORE_OWN_WORDS", False)
    assert oq.parse_codes("Q12 用 A\n<<sep>>\nQ13 那条也行", "wechat")["codes"] == [12]


def test_relay_refusals(world):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    early = channel_message(world, "Q1 提前写的", "wx-early")
    tick(world, minutes=1)
    sync(world)
    code = ledger(world)[0]["code_no"]
    # A dashboard message never came from a channel.
    dash = execute(world.owner, "send_user_conversation_message",
                   {"request_key": "d1", "project_id": HOME, "body": f"Q{code} 好"}, allow_write=True)["message_id"]
    refused(lambda: relay(world, dash), "not_from_channel")
    # No code at all: answered with the candidates, never guessed.
    out = relay(world, channel_message(world, "用方案 A", "wx-2"))
    assert out["refused"] == "no_code" and f"Q{code} · 武侠存档：｜问题一" in out["receipt"]
    assert out["receipt"].split("\n")[-2] == f"回「Q{code}」即把上一条原样转过去。"
    assert len(out["receipt"]) <= oq.PUSH_CHARS
    # Unknown codes: one that does not exist, and one the message was received before.
    results = relay(world, early)["results"]
    assert [r["outcome"] for r in results] == ["unknown_code"]
    assert relay(world, channel_message(world, "Q9999 好", "wx-3"))["results"][0]["outcome"] == "unknown_code"
    # A sender unbound since, or the channel rebound to another account.
    later = channel_message(world, f"Q{code} 用 B", "wx-4")
    world.owner.channels.unbind("wechat")
    refused(lambda: relay(world, later), "channel_sender_not_bound")
    new_code = world.owner.channels.issue_code("wechat")["code"]
    execute(world.steward, "bind_owner_channel", {"request_key": "rebind", "channel": "wechat",
                                                  "sender_ref": "wx-other", "code": new_code}, allow_write=True)
    refused(lambda: relay(world, later), "channel_sender_not_bound")


def test_a_live_binding_code_in_the_message_is_refused(world, monkeypatch):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    sync(world)
    code = ledger(world)[0]["code_no"]
    source = channel_message(world, f"Q{code} 用 A", "wx-1")
    live = world.owner.channels.issue_code("slack")["code"]
    with world.db.get_connection() as conn:   # the stored words now carry a live code
        conn.execute("UPDATE driver_inbox_messages SET body=? WHERE message_id=?", (f"Q{code} {live}", source))
        conn.commit()
    refused(lambda: relay(world, source), "code_sent_to_bound_account")
    assert deliveries(world, world.rows["alpha"]) == []


def test_a_live_binding_code_in_the_words_a_codes_only_message_relays_is_refused(world):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    sync(world)
    code = ledger(world)[0]["code_no"]
    words = channel_message(world, "用方案 A", "wx-1")
    live = world.owner.channels.issue_code("slack")["code"]
    with world.db.get_connection() as conn:   # the words to be relayed now carry a live code
        conn.execute("UPDATE driver_inbox_messages SET body=? WHERE message_id=?", (f"用方案 A {live}", words))
        conn.commit()
    tick(world, minutes=1)
    refused(lambda: relay(world, channel_message(world, f"Q{code}", "wx-2")), "code_sent_to_bound_account")
    assert deliveries(world, world.rows["alpha"]) == []


def test_a_withdrawn_question_is_refused_with_its_reason_and_the_askers_new_code(world):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    sync(world)
    (one,) = ledger(world)
    execute(world.alpha, "withdraw_user_question", {"request_key": "w1", "project_id": "alpha",
                                                    "message_id": one["message_id"], "segment_id": "q1",
                                                    "reason": "问错了"}, allow_write=True)
    ask(world, world.alpha, "alpha", "q1b", "改问：用 C 吗？")
    tick(world, minutes=3)
    sync(world)
    new = ledger(world)[1]["code_no"]
    out = relay(world, channel_message(world, f"Q{one['code_no']} 用 A", "wx-1"))
    assert out["results"][0]["outcome"] == "question_withdrawn"
    assert out["results"][0]["line"] == f"Q{one['code_no']} 已撤回（问错了）；导演改问了 Q{new}：改问：用 C 吗？"
    assert deliveries(world, world.rows["alpha"]) == []


def test_no_recipients_and_recipients_when_the_asker_left(world):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    ask(world, world.alpha, "alpha", "q2", "问题二")
    sync(world)
    one, two = ledger(world)
    alpha_id, beta_id = world.rows["alpha"]["driver_id"], world.rows["beta"]["driver_id"]
    with world.db.get_connection() as conn:
        conn.execute("UPDATE project_drivers SET status='removed' WHERE project_id='alpha' AND driver_id=?", (alpha_id,))
        conn.commit()
    out = relay(world, channel_message(world, f"Q{one['code_no']} 好", "wx-1"))
    assert out["results"][0]["outcome"] == "no_recipients"
    with world.db.get_connection() as conn:
        conn.execute("INSERT INTO project_drivers(project_id,driver_id,status,revision,actor,updated_at) "
                     "VALUES('alpha',?,'member',1,'t','t')", (beta_id,))
        conn.commit()
    out = relay(world, channel_message(world, f"Q{two['code_no']} 好", "wx-2"))
    assert out["results"][0]["outcome"] == "relayed" and out["results"][0]["recipients"] == [beta_id]


def test_m1_two_relayed_answers_to_one_question_arrive_in_written_order_with_exact_subjects(world):
    """M1 (410): the later answer decides; both reach the asker in the order the owner wrote
    them, the second named a further answer, each citing only its own question."""
    message = ask(world, world.alpha, "alpha", "s1", "要不要合并？")
    sync(world)
    code = ledger(world)[0]["code_no"]
    world.clock["now"] = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    yes = channel_message(world, f"Q{code} 同意", "wx-1")
    world.clock["now"] = datetime(2026, 11, 20, 10, 5, tzinfo=timezone.utc)
    no = channel_message(world, f"Q{code} 不要合并", "wx-2")
    first, second = relay(world, yes), relay(world, no)
    rows = deliveries(world, world.rows["alpha"])
    assert [r["body"] for r in rows] == [f"Q{code} 同意", f"Q{code} 不要合并"]
    assert rows[0]["seq"] < rows[1]["seq"]
    assert rows[0]["subject"] == (f"Q{code} = your question s1: your owner's words, relayed verbatim from WeChat "
                                  "(received 2026-11-20T10:00:00Z)")
    assert rows[1]["subject"] == (f"Q{code} = your question s1: a further answer, your owner's words, relayed verbatim "
                                  "from WeChat (received 2026-11-20T10:05:00Z)")
    assert [json.loads(r["answers_json"]) for r in rows] == [[{"message_id": message, "segment_id": "s1"}]] * 2
    assert first["results"][0]["outcome"] == "relayed" and second["results"][0]["outcome"] == "relayed_further"
    assert second["receipt"].split("\n")[0] == f"已转达 Q{code}（补充回答）→ 武侠存档：要不要合并？"


def test_m1_an_answer_received_before_an_existing_one_is_superseded_not_relayed(world):
    """M1 (410): relayed in the reverse order, the older words never land after the newer."""
    message = ask(world, world.alpha, "alpha", "s1", "要不要合并？")
    sync(world)
    code = ledger(world)[0]["code_no"]
    world.clock["now"] = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    yes = channel_message(world, f"Q{code} 同意", "wx-1")
    world.clock["now"] = datetime(2026, 11, 20, 10, 5, tzinfo=timezone.utc)
    no = channel_message(world, f"Q{code} 不要合并", "wx-2")
    relay(world, no)
    with world.db.get_connection() as conn:
        before = conn.execute("SELECT COUNT(*) FROM state_owner_question_answers").fetchone()[0]
    out = relay(world, yes)
    assert out["results"][0]["outcome"] == "superseded"
    assert out["results"][0]["line"] == f"Q{code} 没有转：你之后又答过（11-20 11:05），以后来的为准"
    assert [r["body"] for r in deliveries(world, world.rows["alpha"])] == [f"Q{code} 不要合并"]
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_owner_question_answers").fetchone()[0] == before
    # A dashboard answer dates by its own row: written at 10:30, it supersedes a channel message
    # received at 10:00; a channel message received at 10:30 after a 10:00 dashboard answer is further.
    other = ask(world, world.alpha, "alpha", "s2", "第二个问题")
    world.clock["now"] = datetime(2026, 11, 20, 10, 40, tzinfo=timezone.utc)
    sync(world)
    code2 = ledger(world)[1]["code_no"]
    dash = execute(world.owner, "send_user_conversation_message", {
        "request_key": "dash-s2", "project_id": "alpha", "body": "面板上答",
        "answers": [{"message_id": other, "segment_id": "s2"}]}, allow_write=True)["message_id"]
    with world.db.get_connection() as conn:
        conn.execute("UPDATE driver_inbox_messages SET created_at=? WHERE message_id=?",
                     ("2026-11-20T10:50:00+00:00", dash))
        conn.commit()
    world.clock["now"] = datetime(2026, 11, 20, 10, 45, tzinfo=timezone.utc)
    early = channel_message(world, f"Q{code2} 渠道里答", "wx-3")
    world.clock["now"] = datetime(2026, 11, 20, 10, 55, tzinfo=timezone.utc)
    assert relay(world, early)["results"][0]["outcome"] == "superseded"
    late = channel_message(world, f"Q{code2} 再补一句", "wx-4")
    assert relay(world, late)["results"][0]["outcome"] == "relayed_further"
    del message


def test_the_same_message_never_answers_one_code_twice(world):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    sync(world)
    code = ledger(world)[0]["code_no"]
    source = channel_message(world, f"Q{code} 用 A", "wx-1")
    first = relay(world, source)
    assert relay(world, source) == first, "the same request is State's replay"
    again = relay(world, source, key="another-key")
    assert again["results"][0]["outcome"] == "duplicate"
    assert len(deliveries(world, world.rows["alpha"])) == 1


def test_m2_an_approval_phrase_closes_no_question_and_its_reminder_still_fires(world):
    """M2 (410, the owner's cheap option): the 7.2 phrase decides a proposal only."""
    with world.db.get_connection() as conn:
        conn.execute("INSERT INTO state_steward_proposals(proposal_id,version,kind,payload_json,reason,status,"
                     "created_by,created_at) VALUES('p-1',1,'create_project',?,'because','open','t','t')",
                     (json.dumps({"project_id": "gamma", "title": "Gamma", "goal": "g", "seed_nodes": []}),))
        conn.commit()
    ask(world, world.alpha, "alpha", "q1", "可以合并吗？")
    sync(world)
    (one,) = ledger(world)
    sent = execute(world.steward, "relay_owner_message", {
        "request_key": "relay-wx-1", "channel": "wechat", "sender_ref": WX, "channel_message_ref": "wx-1",
        "body": "同意 proposal p-1", "channel_at": world.clock["now"].isoformat()}, allow_write=True)
    assert sent["approval"]["outcome"] == "executed", "it really was the 7.2 phrase, deciding p-1"
    phrase = sent["message_id"]
    assert [q["segment_id"] for q in open_questions(world, "alpha")] == ["q1"]
    assert ledger(world)[0]["closed_at"] is None
    out = relay(world, phrase)
    assert out["refused"] == "no_code"
    assert out["receipt"].split("\n")[-1] == oq.APPROVAL_PHRASE_NOTE
    assert oq.APPROVAL_PHRASE_NOTE == "授权类问题也只认「Q编号 你的话」；批准提议的那句口令不关问题。"
    tick(world, hours=4)
    body = sync(world)["messages"][0]["body"]
    assert body.startswith("【提醒】") and f"Q{one['code_no']} · " in body and "已在" not in body
    assert ledger(world)[0]["reminders_sent"] == 1 and ledger(world)[0]["closed_at"] is None
    # ...and a receipt of a relayed answer ends with the same line.
    receipt = relay(world, channel_message(world, f"Q{one['code_no']} 同意", "wx-2"))["receipt"]
    assert receipt.split("\n")[-1] == oq.APPROVAL_PHRASE_NOTE


def test_the_words_are_copied_byte_for_byte(world):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    sync(world)
    code = ledger(world)[0]["code_no"]
    words = f"  Q{code} 用 A 👍\n\n第二段\tTAB\r\n  结尾  "
    relay(world, channel_message(world, words, "wx-1"))
    assert deliveries(world, world.rows["alpha"])[0]["body"] == words


def test_one_row_per_code_each_citing_its_own_question_across_two_projects(world):
    a = ask(world, world.alpha, "alpha", "m1", "合并吗？", ago=timedelta(minutes=6))
    b = ask(world, world.beta, "beta", "m2", "合并吗？")
    sync(world)
    one, two = ledger(world)
    words = f"Q{one['code_no']} 同意合并\nQ{two['code_no']} 不要合并"
    out = relay(world, channel_message(world, words, "wx-1"))
    assert [r["outcome"] for r in out["results"]] == ["relayed", "relayed"]
    (ra,), (rb,) = deliveries(world, world.rows["alpha"]), deliveries(world, world.rows["beta"])
    assert ra["body"] == rb["body"] == words
    assert json.loads(ra["answers_json"]) == [{"message_id": a, "segment_id": "m1"}] and ra["reply_to_message_id"] == a
    assert json.loads(rb["answers_json"]) == [{"message_id": b, "segment_id": "m2"}]
    assert ra["subject"].startswith(f"Q{one['code_no']} = your question m1: ")
    assert rb["subject"].startswith(f"Q{two['code_no']} = your question m2: ")


def test_a_message_of_codes_alone_relays_the_message_right_before_it(world):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    sync(world)
    code = ledger(world)[0]["code_no"]
    words = channel_message(world, "用方案 A", "wx-1")
    assert relay(world, words)["refused"] == "no_code"
    tick(world, minutes=2)
    out = relay(world, channel_message(world, f"Q{code}", "wx-2"))
    assert out["relayed_message_id"] == words and out["results"][0]["outcome"] == "relayed"
    assert deliveries(world, world.rows["alpha"])[0]["body"] == "用方案 A"
    # The previous one names a code itself, or was already relayed, or is more than 10 minutes back: no_code.
    # wx-3 comes in the same second as wx-2.
    assert relay(world, channel_message(world, f"Q{code}", "wx-3"))["refused"] == "no_code"
    with world.db.get_connection() as conn:   # wx-2 again: its previous is wx-1 (already relayed), not wx-3
        relayed_from = conn.execute("SELECT message_id FROM driver_inbox_messages WHERE body=? ORDER BY rowid LIMIT 1",
                                    (f"Q{code}",)).fetchone()[0]
    assert relay(world, relayed_from, key="again")["refused"] == "no_code"
    ask(world, world.alpha, "alpha", "q2", "问题二")
    tick(world, minutes=3)
    sync(world)
    code2 = ledger(world)[1]["code_no"]
    channel_message(world, "用 B", "wx-4")
    tick(world, minutes=11)
    assert relay(world, channel_message(world, f"Q{code2}", "wx-5"))["refused"] == "no_code"
    # All in one second: the words before the codes are relayed, never words received after them.
    words2 = channel_message(world, "用 C", "wx-6")
    codes2 = channel_message(world, f"Q{code2}", "wx-7")
    channel_message(world, "还是用 D", "wx-8")
    out = relay(world, codes2)
    assert out["relayed_message_id"] == words2 and out["results"][0]["outcome"] == "relayed"
    assert deliveries(world, world.rows["alpha"])[-1]["body"] == "用 C"


def test_codes_only_times_received_before_the_push_and_superseded_use_the_relayed_message(world):
    """7.6.5 step 8: 'received before the push' judges the codes-only message; the subject time
    and `superseded` use the relayed (previous) message."""
    world.clock["now"] = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    ask(world, world.alpha, "alpha", "q1", "问题一")
    words = channel_message(world, "用方案 A", "wx-1")          # received 10:00, before the push
    world.clock["now"] = datetime(2026, 11, 20, 10, 1, tzinfo=timezone.utc)
    sync(world)
    code = ledger(world)[0]["code_no"]
    world.clock["now"] = datetime(2026, 11, 20, 10, 3, tzinfo=timezone.utc)
    out = relay(world, channel_message(world, f"Q{code}", "wx-2"))
    assert out["relayed_message_id"] == words and out["results"][0]["outcome"] == "relayed"
    assert out["results"][0]["subject"].endswith("(received 2026-11-20T10:00:00Z)")
    # The relayed message older than an existing answer: superseded, even when the codes-only
    # message itself came after that answer.
    ask(world, world.alpha, "alpha", "q2", "问题二")
    world.clock["now"] = datetime(2026, 11, 20, 10, 10, tzinfo=timezone.utc)
    sync(world)
    code2 = ledger(world)[1]["code_no"]
    world.clock["now"] = datetime(2026, 11, 20, 10, 11, tzinfo=timezone.utc)
    old = channel_message(world, "用 B", "wx-3")
    world.clock["now"] = datetime(2026, 11, 20, 10, 12, tzinfo=timezone.utc)
    newer = relay(world, channel_message(world, f"Q{code2} 用 C", "s-1", channel="slack", sender=SLACK))
    assert newer["results"][0]["outcome"] == "relayed"
    world.clock["now"] = datetime(2026, 11, 20, 10, 13, tzinfo=timezone.utc)
    out = relay(world, channel_message(world, f"Q{code2}", "wx-4"))
    assert out["relayed_message_id"] == old and out["results"][0]["outcome"] == "superseded"


def test_after_a_relay_the_question_is_answered_everywhere_and_the_inbound_rate_is_unchanged(world):
    ask(world, world.alpha, "alpha", "q1", "问题一")
    sync(world)
    code = ledger(world)[0]["code_no"]
    source = channel_message(world, f"Q{code} 用 A", "wx-1")
    with world.db.get_connection() as conn:
        inbound = conn.execute("SELECT COUNT(*) FROM state_message_sources WHERE direction='in'").fetchone()[0]
    relay(world, source)
    assert open_questions(world, "alpha") == []
    assert "alpha" not in execute(world.owner, "user_conversation_unread", {})["questions"]
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_message_sources WHERE direction='in'").fetchone()[0] == inbound
    assert ledger(world)[0]["closed_how"] == "wechat"
    listed = execute(world.steward, "list_pushed_owner_questions", {})
    assert listed == {"questions": []}


def test_the_steward_snapshot_lists_the_pushed_questions_still_waiting(world):
    ask(world, world.alpha, "alpha", "q1", "问题一\n第二行")
    sync(world)
    code = ledger(world)[0]["code_no"]
    listed = execute(world.steward, "list_pushed_owner_questions", {})["questions"]
    assert listed == [{"code": f"Q{code}", "project_id": "alpha", "project_title": "武侠存档", "question": "问题一",
                       "pushed_at": T0.isoformat(), "reminders_sent": 0}]
    assert execute(world.owner, "list_pushed_owner_questions", {})["questions"] == listed
    refused(lambda: execute(world.alpha, "list_pushed_owner_questions", {}), "owner_or_steward_required")
