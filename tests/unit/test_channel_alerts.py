"""Step 25 C12: the channel-down alert (design-steward 7.9.2, r44g).

The status row holds facts only: connected, detail, down_since (this outage's start),
up_since (the last real reconnect) and live (whether the last reading was a real one; the
three placeholders - "bridge not reached yet", "connecting", "channel plugin stopped" - are
not). Each outage has at most one ledger row per kind in state_channel_alerts, keyed
(channel, kind, down_since): claimed / sent / failed, attempt 1 or 2. Which alert is due is
one pure function of the facts, the bindings, the ledger and the time (due_alerts): the host
reads it (due_channel_alerts), claims an item (claim_channel_alert: judged again, granted or
{granted: false} with nothing written) and records how the send went (record_channel_alert:
only that outage's row, only while it is that attempt's claim).
The clock is state_channels._now; the owner's zone is Europe/Paris."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest

from core import state_channels
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService
from tests.unit.test_steward_channels import HOME, SLACK_USER, bind, refused, world  # noqa: F401 (fixture)

WECHAT_USER = "wxid_owner"
# A Monday noon in Paris (CET, UTC+1): far from the quiet hours.
T0 = datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
HOUR = 60 * MIN


class Clock:
    def __init__(self, moment):
        self.now = moment

    def __call__(self):
        return self.now

    def at(self, moment):
        self.now = moment

    def advance(self, delta):
        self.now += delta


@pytest.fixture
def alerts(world, monkeypatch):  # noqa: F811 (the imported fixture)
    monkeypatch.setenv("AITELIER_OWNER_TZ", "Europe/Paris")
    clock = Clock(T0)
    monkeypatch.setattr(state_channels, "_now", clock)
    bind(world, "slack", SLACK_USER, "bind-slack")
    bind(world, "wechat", WECHAT_USER, "bind-wechat")
    keys = count()

    # Each call carries exactly the keys and fields the host sends (integrations/dsh-driver/
    # steward-host.mjs channelOps; test_the_hosts_request_keys_and_fields_are_these_ below).
    def status(channel, connected, detail=None, service=None):
        return execute(service or world.steward, "report_channel_status",
                       {"request_key": f"channel-status:{channel}:{next(keys)}", "project_id": HOME,
                        "channel": channel, "connected": connected, **({"detail": detail} if detail else {})},
                       allow_write=True)

    def due(service=None):
        return execute(service or world.steward, "due_channel_alerts", {})["due"]

    def claim(channel, kind, down_since, attempt=1, service=None):
        return execute(service or world.steward, "claim_channel_alert",
                       {"request_key": f"channel-alert:{channel}:{kind}:{down_since}:{attempt}", "project_id": HOME,
                        "channel": channel, "kind": kind, "down_since": down_since, "attempt": attempt},
                       allow_write=True)

    def record(channel, kind, down_since, attempt=1, error=None, service=None):
        return execute(service or world.steward, "record_channel_alert",
                       {"request_key": f"channel-alert-result:{channel}:{kind}:{down_since}:{attempt}",
                        "project_id": HOME, "channel": channel, "kind": kind, "down_since": down_since,
                        "attempt": attempt, "ok": error is None, **({"error": error} if error else {})},
                       allow_write=True)

    def row(channel="wechat"):
        with world.db.get_connection() as conn:
            found = conn.execute("SELECT * FROM state_owner_channel_status WHERE channel=?", (channel,)).fetchone()
            return dict(found) if found else None

    def ledger(channel="wechat"):
        with world.db.get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM state_channel_alerts WHERE channel=? "
                                                  "ORDER BY claimed_at", (channel,))]

    def requests():
        with world.db.get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM driver_inbox_requests").fetchone()[0]

    def listing():
        return world.owner.channels.list_owner_channels(with_status=True)

    def view(channel="wechat"):
        return {c["channel"]: c for c in listing()["channels"]}[channel]["status"]

    status("slack", True)
    return type("Alerts", (), dict(world=world, clock=clock, status=staticmethod(status), due=staticmethod(due),
                                   claim=staticmethod(claim), record=staticmethod(record), row=staticmethod(row),
                                   ledger=staticmethod(ledger), requests=staticmethod(requests),
                                   listing=staticmethod(listing), view=staticmethod(view)))


def down(a, detail="bridge unreachable", channel="wechat"):
    """The channel truly goes down now; its outage's down_since."""
    a.status(channel, False, detail)
    return a.row(channel)["down_since"]


def item(channel, kind, down_since, attempt=1):
    return {"channel": channel, "kind": kind, "down_since": down_since, "attempt": attempt}


def send(a, channel, kind, down_since, attempt=1, error=None):
    """The host's one pass for one item: it is due, granted, and recorded."""
    assert item(channel, kind, down_since, attempt) in a.due()
    granted = a.claim(channel, kind, down_since, attempt)
    assert granted["granted"] is True, granted
    assert a.record(channel, kind, down_since, attempt, error)["recorded"] is True
    return granted


# ---------------------------------------------------------------- facts

def test_a_detail_change_keeps_the_outage_clock_and_placeholders_change_no_fact(alerts):
    a = alerts
    since = down(a)
    assert since == T0.isoformat()
    a.clock.advance(3 * MIN)
    a.status("wechat", False, "no QR (logged_out)")
    assert a.row()["down_since"] == since and a.row()["live"] == 1
    # A host restart's first reading of a channel that is down, and of one that is up (D10).
    for placeholder in ("bridge not reached yet", "connecting", "channel plugin stopped"):
        a.clock.advance(MIN)
        a.status("wechat", False, placeholder)
        row = a.row()
        assert (row["connected"], row["down_since"], row["up_since"], row["live"], row["detail"], row["at"]) == \
            (0, since, None, 0, placeholder, a.clock.now.isoformat())
        before = a.row("slack")
        a.status("slack", False, placeholder)
        after = a.row("slack")
        assert (after["connected"], after["down_since"], after["up_since"], after["live"]) == \
            (1, before["down_since"], before["up_since"], 0), "a placeholder on a connected row"
        a.status("slack", True)
        assert a.row("slack")["live"] == 1 and a.row("slack")["up_since"] == before["up_since"]
    a.status("wechat", False, "bridge unreachable")
    assert a.row()["live"] == 1 and a.row()["down_since"] == since, "the next real reading"


def test_a_channel_that_is_not_live_is_neither_alerted_about_nor_carries_an_alert(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    a.status("slack", False, "channel plugin stopped")  # the Slack plugin taken back: not a via
    assert a.due() == []
    a.status("slack", True)
    assert a.due() == [item("wechat", "down", since)]
    a.status("wechat", False, "bridge not reached yet")  # a host restart: WeChat not read yet
    assert a.due() == []
    assert a.claim("wechat", "down", since)["granted"] is False


def test_a_first_report_that_is_a_placeholder_is_never_due_and_its_connect_owes_nothing(alerts):
    a = alerts
    a.status("wechat", False, "bridge not reached yet")
    row = a.row()
    assert (row["connected"], row["live"], row["down_since"]) == (0, 0, None)
    a.clock.advance(HOUR)
    assert a.due() == []
    a.status("wechat", True)
    assert a.row()["up_since"] == a.clock.now.isoformat()
    a.clock.advance(HOUR)
    assert a.due() == []


def test_the_ledger_refuses_an_outage_without_its_start(alerts):
    # SQLite's non-INTEGER primary key does not bring NOT NULL with it; the column does.
    with alerts.world.db.get_connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO state_channel_alerts(channel,kind,down_since,state,attempt,via,claimed_at) "
                     "VALUES('wechat','down',NULL,'claimed',1,'slack','t')")


def test_connected_reported_again_moves_neither_clock(alerts):
    a = alerts
    first = a.row("slack")
    a.clock.advance(HOUR)
    a.status("slack", True, "socket ok")
    again = a.row("slack")
    assert (again["down_since"], again["up_since"], again["detail"]) == \
        (first["down_since"], first["up_since"], "socket ok")


def test_an_old_status_table_gains_the_facts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(state_channels, "_now", lambda: T0)
    db = StateDatabase(str(tmp_path / "old.sqlite"))
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE state_owner_channel_status (channel TEXT PRIMARY KEY CHECK(channel IN "
                     "('slack','wechat')), connected INTEGER NOT NULL CHECK(connected IN (0,1)), detail TEXT, "
                     "qr_svg TEXT, at TEXT NOT NULL)")
        conn.execute("INSERT INTO state_owner_channel_status VALUES('wechat',0,'bridge unreachable',NULL,'2026-11-01')")
        conn.execute("INSERT INTO state_owner_channel_status VALUES('slack',0,'connecting',NULL,'2026-11-01')")
        conn.commit()
    StateService(db, actor="fixture", project_read_trusted=True).channels  # noqa: B018 (builds the schema)
    with db.get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = {r["channel"]: dict(r) for r in conn.execute("SELECT * FROM state_owner_channel_status")}
    assert (rows["wechat"]["down_since"], rows["wechat"]["live"], rows["wechat"]["up_since"]) == (T0.isoformat(), 1, None)
    assert (rows["slack"]["down_since"], rows["slack"]["live"]) == (None, 0), "a placeholder waits for a real reading"
    # Once: a later start leaves what the rows say alone.
    monkeypatch.setattr(state_channels, "_now", lambda: T0 + HOUR)
    StateService(db, actor="fixture", project_read_trusted=True).channels  # noqa: B018
    with db.get_connection() as conn:
        assert conn.execute("SELECT down_since FROM state_owner_channel_status WHERE channel='wechat'").fetchone()[0] \
            == T0.isoformat()


def test_a_migrated_outage_is_due_ten_minutes_after_the_migration(alerts):
    a = alerts
    with a.world.db.get_connection() as conn:  # a row as an older backend left it
        conn.execute("INSERT INTO state_owner_channel_status(channel,connected,detail,at) "
                     "VALUES('wechat',0,'bridge unreachable','2026-11-01')")
        conn.execute("ALTER TABLE state_owner_channel_status DROP COLUMN down_since")
        conn.execute("ALTER TABLE state_owner_channel_status DROP COLUMN up_since")
        conn.execute("ALTER TABLE state_owner_channel_status DROP COLUMN live")
        conn.commit()
    StateService(a.world.db, actor="fixture", project_read_trusted=True).channels  # noqa: B018
    since = a.row()["down_since"]
    assert since == T0.isoformat()
    a.clock.advance(10 * MIN - timedelta(seconds=1))
    assert a.due() == []
    a.clock.advance(timedelta(seconds=1))
    assert a.due() == [item("wechat", "down", since)]


@pytest.mark.parametrize("detail, alerted", [
    ("not configured", False),                                                # WeChat's
    ("not configured: slack-app-token / slack-bot-token missing", False),     # Slack's
    ("slack-app-token is not an app-level (xapp-) token", True),              # the owner must fix it
])
def test_a_channel_that_says_it_is_not_configured_is_never_alerted_about(alerts, detail, alerted):
    a = alerts
    since = down(a, detail)
    a.clock.advance(HOUR)
    assert a.due() == ([item("wechat", "down", since)] if alerted else [])


def test_an_unbound_channel_is_never_alerted_about_nor_carries_an_alert(alerts):
    a = alerts
    a.world.owner.channels.unbind("wechat")
    down(a)
    a.clock.advance(HOUR)
    assert a.due() == []
    bind(a.world, "wechat", WECHAT_USER, "bind-wechat-2")
    a.world.owner.channels.unbind("slack")
    assert a.due() == [], "no other bound channel to carry it"


# ---------------------------------------------------------------- time

def test_due_ten_minutes_in_not_a_second_before(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN - timedelta(seconds=1))
    assert a.due() == []
    assert a.claim("wechat", "down", since) == {"granted": False}
    a.clock.advance(timedelta(seconds=1))
    assert a.due() == [item("wechat", "down", since)]


def test_back_within_ten_minutes_owes_neither_alert(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(9 * MIN)
    a.status("wechat", True)
    for _ in range(3):
        a.clock.advance(HOUR)
        assert a.due() == []
    assert a.claim("wechat", "up", since) == {"granted": False}


@pytest.mark.parametrize("kind", ["down", "up"])
@pytest.mark.parametrize("stamp, due", [
    ("2026-11-23T21:59:00+00:00", True),    # 22:59 local
    ("2026-11-23T22:00:00+00:00", False),   # 23:00
    ("2026-11-23T22:30:00+00:00", False),   # 23:30
    ("2026-11-24T06:59:00+00:00", False),   # 07:59
    ("2026-11-24T07:00:00+00:00", True),    # 08:00
])
def test_the_quiet_hours_hold_both_kinds_from_eleven_to_eight(alerts, kind, stamp, due):
    a = alerts
    a.clock.at(datetime.fromisoformat("2026-11-23T20:00:00+00:00"))  # 21:00 local
    since = down(a)
    if kind == "up":
        a.clock.advance(10 * MIN)
        send(a, "wechat", "down", since)
        a.clock.advance(5 * MIN)
        a.status("wechat", True)
    a.clock.at(datetime.fromisoformat(stamp))
    assert a.due() == ([item("wechat", kind, since)] if due else [])


@pytest.mark.parametrize("night, before_eight, eight", [
    # 2027-03-28: Paris springs forward at 02:00 - 08:00 is then 06:00 UTC.
    ("2027-03-27T22:30:00+00:00", "2027-03-28T05:59:00+00:00", "2027-03-28T06:00:00+00:00"),
    # 2027-10-31: Paris falls back at 03:00 - 08:00 is then 07:00 UTC.
    ("2027-10-30T21:30:00+00:00", "2027-10-31T06:59:00+00:00", "2027-10-31T07:00:00+00:00"),
])
def test_the_quiet_hours_follow_the_owners_clock_across_both_switches(alerts, night, before_eight, eight):
    a = alerts
    a.clock.at(datetime.fromisoformat(night) - 20 * MIN)
    since = down(a)
    for moment in (night, before_eight):
        a.clock.at(datetime.fromisoformat(moment))
        assert a.due() == []
        assert a.listing()["quiet_until"] == eight
    a.clock.at(datetime.fromisoformat(eight))
    assert a.due() == [item("wechat", "down", since)]


# ---------------------------------------------------------------- claim and record

def test_a_claim_that_is_not_due_writes_nothing_and_the_same_key_is_granted_once_due(alerts):
    a = alerts
    a.clock.at(datetime.fromisoformat("2026-11-23T21:50:00+00:00"))  # 22:50 local
    since = down(a)
    a.clock.advance(20 * MIN)  # 23:10: due but for the quiet hours (D1, D3)
    before = a.requests()
    assert a.claim("wechat", "down", since) == {"granted": False}
    assert a.requests() == before and a.ledger() == [], "no request record, no ledger row"
    a.clock.at(datetime.fromisoformat("2026-11-24T07:00:00+00:00"))
    granted = a.claim("wechat", "down", since)
    assert granted == {"granted": True, "via": "slack", "to": SLACK_USER, "minutes": 550,
                       "detail": "bridge unreachable"}
    assert a.claim("wechat", "down", since) == granted, "a granted claim's replay answers the same"
    assert [(r["state"], r["attempt"], r["via"], r["claimed_at"], r["finished_at"]) for r in a.ledger()] == \
        [("claimed", 1, "slack", "2026-11-24T07:00:00+00:00", None)]


def test_a_claim_must_name_the_due_outage_kind_and_attempt(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    for wrong in (("wechat", "down", "2026-01-01T00:00:00+00:00", 1), ("wechat", "down", since, 2),
                  ("wechat", "up", since, 1), ("slack", "down", since, 1)):
        assert a.claim(*wrong) == {"granted": False}, wrong
    assert a.ledger() == [] and a.ledger("slack") == []
    assert a.claim("wechat", "down", since)["granted"] is True


def test_a_record_replay_answers_the_same_and_settles_nothing_twice(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", since)
    first = a.record("wechat", "down", since)
    assert first == {"recorded": True, "channel": "wechat", "kind": "down"}
    finished = a.ledger()[0]["finished_at"]
    a.clock.advance(MIN)
    assert a.record("wechat", "down", since) == first
    assert a.ledger()[0]["finished_at"] == finished
    # Another result for the same attempt under a fresh key: the row is no longer a claim.
    assert execute(a.world.steward, "record_channel_alert",
                   {"request_key": "other", "project_id": HOME, "channel": "wechat", "kind": "down",
                    "down_since": since, "attempt": 1, "ok": False, "error": "late"},
                   allow_write=True)["recorded"] is False
    assert a.ledger()[0]["state"] == "sent"


def test_reading_the_due_alerts_writes_no_request_record(alerts):
    a = alerts
    down(a)
    a.clock.advance(10 * MIN)
    before = a.requests()
    for _ in range(5):
        assert len(a.due()) == 1
    assert a.requests() == before


def test_the_record_must_be_ok_or_an_error_and_its_error_is_cut(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", since)
    for extra in ({"ok": True, "error": "both"}, {"ok": False}):
        refused(lambda: execute(a.world.steward, "record_channel_alert",
                                {"request_key": "r", "project_id": HOME, "channel": "wechat", "kind": "down",
                                 "down_since": since, "attempt": 1, **extra}, allow_write=True), "invalid_request")
    a.record("wechat", "down", since, error="e" * 1000)
    assert a.ledger()[0]["error"] == "e" * state_channels.ALERT_ERROR_MAX


def test_only_the_steward_claims_or_records_and_only_it_or_the_owner_reads_what_is_due(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    assert a.due(service=a.world.owner) == [item("wechat", "down", since)]
    refused(lambda: a.due(service=a.world.director), "owner_or_steward_required")
    for caller in (a.world.owner, a.world.director):
        refused(lambda: a.claim("wechat", "down", since, service=caller), "steward_role_required")
    assert a.ledger() == []
    a.claim("wechat", "down", since)
    for caller in (a.world.owner, a.world.director):
        refused(lambda: a.record("wechat", "down", since, service=caller), "steward_role_required")
    assert a.ledger()[0]["state"] == "claimed"


# ---------------------------------------------------------------- via

def test_the_alert_goes_through_the_other_channel_to_its_bound_account_even_when_the_down_one_is_default(alerts):
    a = alerts
    a.world.owner.channels.set_default("wechat")
    since = down(a)
    a.clock.advance(10 * MIN)
    granted = a.claim("wechat", "down", since)
    assert (granted["via"], granted["to"]) == ("slack", SLACK_USER), "never through itself, never a group"


def test_both_down_neither_is_due_and_the_first_back_carries_the_others(alerts):
    a = alerts
    slack = down(a, "socket closed", channel="slack")
    wechat = down(a)
    a.clock.advance(HOUR)
    assert a.due() == []
    a.status("slack", True)  # Slack back first: its own outage was never told, so no down and no up for it
    assert a.due() == [item("wechat", "down", wechat)]
    assert a.claim("wechat", "down", wechat)["to"] == SLACK_USER
    assert all(d["channel"] != "slack" for d in a.due())
    assert slack is not None and a.claim("slack", "up", slack) == {"granted": False}


def test_a_down_one_back_before_anything_could_carry_its_alert_owes_neither(alerts):
    a = alerts
    a.world.owner.channels.unbind("slack")
    down(a)
    a.clock.advance(HOUR)
    assert a.due() == []
    a.status("wechat", True)
    bind(a.world, "slack", SLACK_USER, "bind-slack-2")
    a.clock.advance(HOUR)
    assert a.due() == [], "never told it was down: no recovery notice either"


def test_a_recovery_notice_waits_for_another_channel_and_never_goes_through_itself(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    send(a, "wechat", "down", since)
    a.status("slack", False, "socket closed")
    a.clock.advance(MIN)
    a.status("wechat", True)
    a.clock.advance(HOUR)
    assert [d for d in a.due() if d["channel"] == "wechat"] == [], "the only channel up is the one that came back"
    a.status("slack", True)
    assert a.due() == [item("wechat", "up", since)]


# ---------------------------------------------------------------- attempts

def test_a_failed_first_send_is_due_again_30_minutes_after_its_claim_and_a_failed_second_ends_it(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    send(a, "wechat", "down", since, error="409 recipient_unknown")
    a.clock.advance(30 * MIN - timedelta(seconds=1))
    assert a.due() == []
    a.clock.advance(timedelta(seconds=1))
    assert a.due() == [item("wechat", "down", since, 2)]
    send(a, "wechat", "down", since, 2, error="409 recipient_unknown")
    for _ in range(4):
        a.clock.advance(3 * HOUR)
        assert a.due() == []
    assert a.view()["alert_down"]["state"] == "failed" and a.view()["alert_down"]["attempt"] == 2


def test_a_claim_never_reported_counts_as_failed_after_ten_minutes(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", since)  # the host died after the claim
    a.clock.advance(10 * MIN - timedelta(seconds=1))
    assert a.due() == []
    a.clock.advance(20 * MIN + timedelta(seconds=1))
    assert a.due() == [item("wechat", "down", since, 2)]
    a.claim("wechat", "down", since, 2)  # and died again: given up after ten more minutes
    a.clock.advance(HOUR)
    assert a.due() == []


def test_a_late_success_of_the_first_send_before_the_second_claim_ends_it(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", since)
    a.clock.advance(25 * MIN)  # counted as failed by now (D2)...
    assert a.record("wechat", "down", since)["recorded"] is True  # ...but it went out
    a.clock.advance(10 * MIN)
    assert a.due() == []
    assert a.view()["alert_down"]["state"] == "sent"


@pytest.mark.parametrize("down_attempts", [1, 2])
def test_a_recovery_notice_gets_two_attempts_whichever_attempt_delivered_the_down(alerts, down_attempts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    if down_attempts == 2:
        send(a, "wechat", "down", since, error="socket closed")
        a.clock.advance(30 * MIN)
    send(a, "wechat", "down", since, down_attempts)
    a.clock.advance(5 * MIN)
    a.status("wechat", True)
    send(a, "wechat", "up", since, error="409 recipient_unknown")
    a.clock.advance(29 * MIN)
    assert a.due() == []
    a.clock.advance(MIN)
    send(a, "wechat", "up", since, 2)
    a.clock.advance(HOUR)
    assert a.due() == []


# ---------------------------------------------------------------- one outage, one identity

def test_an_outage_whose_alert_failed_twice_does_not_silence_the_next_one(alerts):
    """F2 (r44b): the six hours count from a DELIVERED down alert."""
    a = alerts
    first = down(a)
    a.clock.advance(10 * MIN)
    send(a, "wechat", "down", first, error="409 recipient_unknown")
    a.clock.advance(30 * MIN)
    send(a, "wechat", "down", first, 2, error="409 recipient_unknown")
    a.clock.advance(5 * MIN)
    a.status("wechat", True)
    a.clock.advance(2 * HOUR)
    second = down(a)
    a.clock.advance(10 * MIN)
    assert a.due() == [item("wechat", "down", second)]
    assert a.claim("wechat", "down", second)["granted"] is True


def test_a_late_failure_settles_only_its_own_outage(alerts):
    a = alerts
    first = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", first)
    a.status("wechat", True)
    a.clock.advance(MIN)
    second = down(a)
    assert a.record("wechat", "down", first, error="socket closed")["recorded"] is True
    a.clock.advance(10 * MIN)
    assert a.due() == [item("wechat", "down", second)]
    assert a.view()["alert_down"] is None, "the card reads the current outage's row"


def test_a_late_success_for_an_old_outage_holds_the_new_down_and_owes_the_old_ones_recovery(alerts):
    """D7 / 455 B: the owner was told about d1, so d2's down is not news; the recovery that
    is owed is d1's."""
    a = alerts
    d1 = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", d1)
    a.status("wechat", True)
    a.clock.advance(MIN)
    d2 = down(a)
    assert a.record("wechat", "down", d1)["recorded"] is True
    assert a.row()["down_since"] == d2, "a late result never touches the facts"
    a.clock.advance(HOUR)
    assert a.due() == []
    assert a.view()["last_told"] == {"kind": "down", "down_since": d1, "at": (T0 + 11 * MIN).isoformat()}
    a.clock.advance(7 * HOUR)  # past the six hours too: still not news
    assert a.due() == []
    a.status("wechat", True)
    assert a.due() == [item("wechat", "up", d1)]


def test_after_a_delivered_down_and_up_a_new_outage_waits_six_hours_from_the_down_report(alerts):
    a = alerts
    first = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", first)
    a.clock.advance(timedelta(seconds=40))  # the record arrives 40 s after the claim
    a.record("wechat", "down", first)
    reported = a.clock.now
    a.clock.advance(20 * MIN)
    a.status("wechat", True)
    send(a, "wechat", "up", first)
    a.clock.advance(HOUR)
    second = down(a)
    a.clock.at(reported + 6 * HOUR - timedelta(seconds=1))
    assert a.due() == [], "within six hours of the delivered down"
    assert a.view()["last_down_sent_at"] == reported.isoformat()
    a.clock.at(reported + 6 * HOUR)
    assert a.due() == [item("wechat", "down", second)]
    send(a, "wechat", "down", second)
    for _ in range(3):
        a.clock.advance(4 * HOUR)
        assert a.due() == [], "once, until it comes back"


def test_down_again_before_the_recovery_went_out_owes_one_recovery_from_the_first_outage(alerts):
    """D11 / 453-3a."""
    a = alerts
    first = down(a)
    a.clock.advance(10 * MIN)
    send(a, "wechat", "down", first)
    a.status("slack", False, "socket closed")  # nothing to carry the recovery yet
    a.clock.advance(10 * MIN)
    a.status("wechat", True)
    a.clock.advance(5 * MIN)
    down(a, "no QR (logged_out)")
    a.status("slack", True)
    a.clock.advance(HOUR)
    assert a.due() == [], "down again: no up, and the new outage is not news"
    a.status("wechat", True)
    assert a.due() == [item("wechat", "up", first)]
    assert a.claim("wechat", "up", first)["minutes"] == 85, "from the first outage's start to the last reconnect"


def test_a_down_claimed_then_reconnected_then_reported_owes_its_recovery(alerts):
    """455 B / D6: the result arrives after the channel is back."""
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", since)
    a.status("wechat", True)
    assert a.due() == []
    a.record("wechat", "down", since)
    assert a.due() == [item("wechat", "up", since)]


def test_a_first_connect_owes_no_recovery(alerts):
    a = alerts
    a.status("wechat", True)
    a.clock.advance(HOUR)
    assert a.due() == []


@pytest.mark.parametrize("how", ["failed", "unreported"])
def test_a_recovery_given_up_does_not_silence_the_next_outage(alerts, how):
    a = alerts
    first = down(a)
    a.clock.advance(10 * MIN)
    send(a, "wechat", "down", first)
    a.clock.advance(MIN)
    a.status("wechat", True)
    send(a, "wechat", "up", first, error="socket closed")
    a.clock.advance(30 * MIN)
    if how == "failed":
        send(a, "wechat", "up", first, 2, error="socket closed")
    else:
        a.claim("wechat", "up", first, 2)  # the host died with the claim
        a.clock.advance(10 * MIN)
    a.clock.advance(7 * HOUR)
    second = down(a)
    a.clock.advance(10 * MIN)
    assert a.due() == [item("wechat", "down", second)]


# ---------------------------------------------------------------- the text's fields and the dashboard

def test_the_minutes_and_the_detail_the_text_is_built_from(alerts):
    a = alerts
    a.clock.at(datetime.fromisoformat("2026-11-23T21:30:00+00:00"))  # 22:30 local
    since = down(a, "x" * 200)
    a.clock.advance(17 * MIN)
    granted = a.claim("wechat", "down", since)
    assert (granted["minutes"], granted["detail"]) == (17, "x" * 80)
    a.record("wechat", "down", since)
    a.clock.at(datetime.fromisoformat("2026-11-23T23:30:00+00:00"))  # back at 00:30 local, in the quiet hours
    a.status("wechat", True)
    a.clock.at(datetime.fromisoformat("2026-11-24T07:00:00+00:00"))
    a.claim("wechat", "up", since)
    a.record("wechat", "up", since, error="socket closed")
    a.clock.advance(30 * MIN)
    granted = a.claim("wechat", "up", since, 2)
    assert (granted["minutes"], granted["detail"]) == (120, None), "up_since minus down_since, after quiet and a retry"


def test_the_dashboard_answer_carries_the_server_clock_the_quiet_hours_and_the_owners_zone(alerts):
    a = alerts
    a.clock.at(datetime.fromisoformat("2026-11-23T22:30:00+00:00"))  # 23:30 local
    listed = a.listing()
    assert (listed["now"], listed["quiet_now"], listed["quiet_until"], listed["owner_tz"]) == \
        ("2026-11-23T22:30:00+00:00", True, "2026-11-24T07:00:00+00:00", "Europe/Paris")
    a.clock.at(T0)
    listed = a.listing()
    assert (listed["quiet_now"], listed["quiet_until"]) == (False, None)
    assert all(key not in execute(a.world.owner, "list_owner_channels", {}) for key in ("now", "owner_tz"))


def test_the_card_reads_the_ledgers_projections_and_the_same_due_function(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    view = a.view()
    assert (view["live"], view["down_since"], view["alert_down"], view["alert_due"]) == \
        (True, since, None, {"kind": "down", "attempt": 1})
    a.claim("wechat", "down", since)
    view = a.view()
    assert view["alert_due"] is None
    assert view["alert_down"] == {"kind": "down", "down_since": since, "state": "claimed", "attempt": 1, "via": "slack",
                                  "claimed_at": (T0 + 10 * MIN).isoformat(), "finished_at": None, "error": None}
    a.record("wechat", "down", since)
    a.clock.advance(MIN)
    a.status("wechat", True)
    view = a.view()
    assert view["alert_down"]["state"] == "sent" and view["alert_up"] is None
    assert view["last_told"] == {"kind": "down", "down_since": since, "at": (T0 + 10 * MIN).isoformat()}
    assert view["alert_due"] == {"kind": "up", "attempt": 1} and view["up_since"] == (T0 + 11 * MIN).isoformat()
    send(a, "wechat", "up", since)
    view = a.view()
    assert view["alert_up"]["state"] == "sent" and view["last_told"]["kind"] == "up" and view["alert_due"] is None


# ---------------------------------------------------------------- the host's side of the contract

def test_the_hosts_request_keys_and_fields_are_these(alerts):
    """T3: the plugin's sequence runs against this backend through the host's channelOps; its
    request keys and fields are the ones the fixture above sends, so this file exercises the
    real contract. A change on either side reds here."""
    host = (Path(__file__).resolve().parents[2] / "integrations/dsh-driver/steward-host.mjs").read_text(encoding="utf-8")
    assert "request('/api/state/query/due_channel_alerts', {}, signal)" in host
    assert "`channel-alert:${channel}:${kind}:${downSince}:${attempt}`, channel, kind, down_since: downSince, attempt" in host
    assert re.search(r"`channel-alert-result:\$\{channel\}:\$\{kind\}:\$\{downSince\}:\$\{attempt\}`, channel, kind, "
                     r"down_since: downSince, attempt,\s+ok: !error", host)
