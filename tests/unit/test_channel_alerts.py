"""Step 25 C12: the channel-down alert (design-steward 7.9.2).

The steward's host reports each channel's status (report_channel_status); the backend
keeps when the outage began (`down_since`, not reset by a detail change) and, when the
host claims an alert (claim_channel_alert, steward only, called by the host's code), decides
in one transaction whether it is due and through which channel: another channel bound to
the owner and reported connected. Not yet: {held: quiet|early|no_channel} and nothing is
kept. The host records how its send went (record_channel_alert): a failed first send may be
claimed once more 30 minutes later; only a delivered down alert is followed by one `up`.
The clock is state_channels._now; the owner's zone is Europe/Paris."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from itertools import count

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

    def status(channel, connected, detail=None, service=None):
        return execute(service or world.steward, "report_channel_status",
                       {"request_key": f"status-{next(keys)}", "project_id": HOME, "channel": channel,
                        "connected": connected,
                        **({"detail": detail} if detail else {})}, allow_write=True)

    def claim(channel, kind, down_since, attempt=1, service=None):
        return execute(service or world.steward, "claim_channel_alert",
                       {"request_key": f"channel-alert:{channel}:{kind}:{down_since}:{attempt}", "project_id": HOME,
                        "channel": channel,
                        "kind": kind, "down_since": down_since}, allow_write=True)

    def record(channel, kind, down_since, via="slack", error=None, attempt=1, service=None):
        return execute(service or world.steward, "record_channel_alert",
                       {"request_key": f"channel-alert-result:{channel}:{kind}:{down_since}:{attempt}",
                        "project_id": HOME,
                        "channel": channel, "kind": kind, "down_since": down_since, "via": via, "ok": error is None,
                        **({"error": error} if error else {})}, allow_write=True)

    def row(channel="wechat"):
        with world.db.get_connection() as conn:
            return dict(conn.execute("SELECT * FROM state_owner_channel_status WHERE channel=?", (channel,)).fetchone())

    def view(channel="wechat"):
        return {c["channel"]: c for c in world.owner.channels.list_owner_channels(with_status=True)["channels"]}[
            channel]["status"]

    status("slack", True)
    return type("Alerts", (), dict(world=world, clock=clock, status=staticmethod(status), claim=staticmethod(claim),
                                   record=staticmethod(record), row=staticmethod(row), view=staticmethod(view)))


def down(a, detail="bridge unreachable"):
    """WeChat goes down now; its outage's down_since as the host was answered."""
    return a.status("wechat", False, detail)["down_since"]


def test_a_detail_change_or_a_host_restarts_first_report_keeps_the_outage_clock(alerts):
    a = alerts
    since = down(a)
    assert since == T0.isoformat()
    a.clock.advance(3 * MIN)
    answer = a.status("wechat", False, "no QR (logged_out)")
    assert answer["down_since"] == since and answer["up_due"] is False and answer["alert_attempts"] == 0
    a.clock.advance(3 * MIN)
    assert a.status("wechat", False, "bridge not reached yet")["down_since"] == since, "a restart's first report"
    assert a.row()["down_since"] == since


def test_nothing_before_ten_minutes_then_one_alert_then_nothing(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(9 * MIN + timedelta(seconds=59))
    early = a.claim("wechat", "down", since)
    assert early == {"held": "early", "retry_at": (T0 + 10 * MIN).isoformat()}
    assert a.row()["alerted_at"] is None and a.row()["alert_attempts"] == 0, "a held answer keeps nothing"
    a.clock.advance(timedelta(seconds=1))
    got = a.claim("wechat", "down", since)
    assert got == {"channel": "wechat", "kind": "down", "down_since": since, "detail": "bridge unreachable",
                   "via": "slack", "attempt": 1}
    assert a.claim("wechat", "down", since) == got, "the same request_key is answered the same"
    a.record("wechat", "down", since)
    assert a.claim("wechat", "down", since, attempt=2) == {}, "delivered: never again for this outage"
    row = a.row()
    assert (row["alert_via"], row["alerted_at"], row["alert_attempts"], row["alerted_since"], row["alert_error"]) == \
        ("slack", (T0 + 10 * MIN).isoformat(), 1, since, None)


def test_the_detail_in_the_claim_is_cut_to_80_characters(alerts):
    a = alerts
    since = down(a, "x" * 200)
    a.clock.advance(10 * MIN)
    assert a.claim("wechat", "down", since)["detail"] == "x" * 80


def test_the_alert_goes_only_through_another_bound_connected_channel_even_when_the_down_one_is_default(alerts):
    a = alerts
    a.world.owner.channels.set_default("wechat")
    since = down(a)
    a.clock.advance(10 * MIN)
    # Slack is the only other channel: bound and connected, so it is chosen although WeChat is
    # the default (with two channels the other one is the only candidate; a down one never is).
    assert a.claim("wechat", "down", since)["via"] == "slack"


def test_no_channel_to_send_through_holds_the_alert_until_another_channel_connects(alerts):
    a = alerts
    a.status("slack", False, "socket closed")
    since = down(a)
    a.clock.advance(15 * MIN)
    assert a.claim("wechat", "down", since) == {"held": "no_channel"}
    row = a.row()
    assert row["alerted_at"] is None and row["alert_via"] is None and row["alert_attempts"] == 0, "nothing kept"
    assert a.view()["alerted_at"] is None
    a.clock.advance(20 * MIN)
    a.status("slack", True)
    got = a.claim("wechat", "down", since)
    assert got["via"] == "slack" and got["attempt"] == 1


def test_an_unbound_other_channel_is_no_channel(alerts):
    a = alerts
    a.world.owner.channels.unbind("slack")
    since = down(a)
    a.clock.advance(10 * MIN)
    assert a.claim("wechat", "down", since) == {"held": "no_channel"}


def test_a_failed_send_may_be_claimed_once_more_after_30_minutes_and_never_a_third_time(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    assert a.claim("wechat", "down", since)["attempt"] == 1
    assert a.record("wechat", "down", since, error="409 recipient_unknown")["recorded"] is True
    assert a.view()["alert_error"] == "409 recipient_unknown", "the dashboard says it was not sent"
    a.clock.advance(29 * MIN)
    assert a.claim("wechat", "down", since, attempt=2) == {"held": "early",
                                                           "retry_at": (T0 + 40 * MIN).isoformat()}
    a.clock.advance(MIN)
    second = a.claim("wechat", "down", since, attempt=2)
    assert second["attempt"] == 2 and second["via"] == "slack"
    a.record("wechat", "down", since, error="409 recipient_unknown", attempt=2)
    a.clock.advance(3 * 60 * MIN)
    assert a.claim("wechat", "down", since, attempt=3) == {}
    assert a.view()["alert_error"] == "409 recipient_unknown"


def test_a_claim_never_reported_is_not_claimed_again(alerts):
    """A host that died between claim and send may have sent it: not twice."""
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", since)
    a.clock.advance(60 * MIN)
    assert a.claim("wechat", "down", since, attempt=2) == {}


def test_an_undelivered_down_alert_gets_no_recovery_notice(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", since)
    a.record("wechat", "down", since, error="socket closed")
    a.clock.advance(5 * MIN)
    answer = a.status("wechat", True)
    assert answer["up_due"] is False and answer["down_since"] is None
    assert a.claim("wechat", "up", since) == {}


def test_a_short_outage_that_was_never_alerted_gets_no_recovery_notice(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(5 * MIN)
    assert a.status("wechat", True)["up_due"] is False
    assert a.claim("wechat", "up", since) == {}


def test_only_an_alerted_outage_gets_its_recovery_notice_and_only_once(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", since)
    a.record("wechat", "down", since)
    view = a.view()
    assert (view["down_since"], view["alerted_at"], view["alert_via"], view["alert_error"]) == \
        (since, (T0 + 10 * MIN).isoformat(), "slack", None)
    a.clock.advance(30 * MIN)
    answer = a.status("wechat", True)
    assert answer["up_due"] is True and answer["alerted_since"] == since and answer["down_since"] is None
    up = a.claim("wechat", "up", since)
    assert up == {"channel": "wechat", "kind": "up", "down_since": since, "detail": None, "via": "slack",
                  "attempt": 1, "up_at": (T0 + 40 * MIN).isoformat()}
    assert a.claim("wechat", "up", since) == up, "a replay"
    assert a.claim("wechat", "up", since, attempt=2) == {}, "taken once"
    assert a.record("wechat", "up", since)["recorded"] is True
    assert a.view()["down_since"] is None and a.view()["alerted_at"] is None, "connected: no alert line"


def test_a_second_outage_within_six_hours_gets_no_down_alert(alerts):
    a = alerts
    first = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", first)
    a.record("wechat", "down", first)
    a.clock.advance(20 * MIN)
    a.status("wechat", True)
    a.claim("wechat", "up", first)
    a.clock.advance(2 * 60 * MIN)
    second = down(a)
    assert second != first and a.row()["alert_attempts"] == 0
    a.clock.advance(30 * MIN)
    assert a.claim("wechat", "down", second) == {}, "within 6 h of the last down alert"
    # After six hours a new outage is told again.
    a.status("wechat", True)
    a.clock.advance(4 * 60 * MIN)
    third = down(a)
    a.clock.advance(10 * MIN)
    assert a.claim("wechat", "down", third)["attempt"] == 1


def test_an_unbound_or_not_configured_channel_is_never_alerted_about(alerts):
    a = alerts
    a.world.owner.channels.unbind("wechat")
    since = down(a)
    a.clock.advance(10 * MIN)
    assert a.claim("wechat", "down", since) == {}
    since = a.status("slack", False, "not configured: slack-app-token / slack-bot-token missing")["down_since"]
    bind(a.world, "wechat", WECHAT_USER, "bind-wechat-2")
    a.status("wechat", True)
    a.clock.advance(10 * MIN)
    assert a.claim("slack", "down", since) == {}


def test_a_stale_down_since_is_answered_empty(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    assert a.claim("wechat", "down", "2026-01-01T00:00:00+00:00") == {}
    assert a.row()["alert_attempts"] == 0
    assert a.record("wechat", "down", "2026-01-01T00:00:00+00:00")["recorded"] is False
    assert a.row()["alerted_since"] is None
    assert a.claim("wechat", "down", since)["attempt"] == 1


def test_only_the_steward_claims_or_records_an_alert(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    for caller in (a.world.owner, a.world.director):
        refused(lambda: a.claim("wechat", "down", since, service=caller), "steward_role_required")
    assert a.row()["alert_attempts"] == 0
    a.claim("wechat", "down", since)
    for caller in (a.world.owner, a.world.director):
        refused(lambda: a.record("wechat", "down", since, service=caller), "steward_role_required")
    assert a.row()["alerted_since"] is None


@pytest.mark.parametrize("local_day, stamp, held_until", [
    # (down at local 22:40, claim at the given UTC moment) in Paris winter time (UTC+1).
    ("2026-11-23", "2026-11-23T21:59:00+00:00", None),                         # 22:59 local
    ("2026-11-23", "2026-11-23T22:00:00+00:00", "2026-11-24T07:00:00+00:00"),  # 23:00 local
    ("2026-11-23", "2026-11-24T06:59:00+00:00", "2026-11-24T07:00:00+00:00"),  # 07:59 local
    ("2026-11-23", "2026-11-24T07:00:00+00:00", None),                         # 08:00 local
])
def test_the_quiet_hours_hold_the_alert_until_eight(alerts, local_day, stamp, held_until):
    a = alerts
    a.clock.at(datetime.fromisoformat(f"{local_day}T21:40:00+00:00"))
    since = down(a)
    a.clock.at(datetime.fromisoformat(stamp))
    got = a.claim("wechat", "down", since)
    if held_until is None:
        assert got["via"] == "slack"
    else:
        assert got == {"held": "quiet", "retry_at": held_until}
        assert a.row()["alerted_at"] is None


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
    a.clock.at(datetime.fromisoformat(night))
    assert a.claim("wechat", "down", since) == {"held": "quiet", "retry_at": eight}
    a.clock.at(datetime.fromisoformat(before_eight))
    assert a.claim("wechat", "down", since) == {"held": "quiet", "retry_at": eight}
    a.clock.at(datetime.fromisoformat(eight))
    assert a.claim("wechat", "down", since)["via"] == "slack"


def test_a_recovery_in_the_quiet_hours_waits_for_eight_and_keeps_its_own_time(alerts):
    a = alerts
    a.clock.at(datetime.fromisoformat("2026-11-23T21:00:00+00:00"))  # 22:00 local
    since = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", since)
    a.record("wechat", "down", since)
    a.clock.at(datetime.fromisoformat("2026-11-23T23:30:00+00:00"))
    assert a.status("wechat", True)["up_due"] is True
    assert a.claim("wechat", "up", since) == {"held": "quiet", "retry_at": "2026-11-24T07:00:00+00:00"}
    a.clock.at(datetime.fromisoformat("2026-11-24T07:00:00+00:00"))
    assert a.claim("wechat", "up", since)["up_at"] == "2026-11-23T23:30:00+00:00"


def test_a_new_outage_drops_a_recovery_notice_not_yet_sent(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", since)
    a.record("wechat", "down", since)
    a.status("slack", False)
    a.clock.advance(MIN)
    assert a.status("wechat", True)["up_due"] is True
    assert a.claim("wechat", "up", since) == {"held": "no_channel"}
    a.clock.advance(MIN)
    assert a.status("wechat", False)["up_due"] is False
    a.status("slack", True)
    assert a.claim("wechat", "up", since) == {}


def test_the_record_must_be_ok_or_an_error_and_its_error_is_cut(alerts):
    a = alerts
    since = down(a)
    a.clock.advance(10 * MIN)
    a.claim("wechat", "down", since)
    for extra in ({"ok": True, "error": "both"}, {"ok": False}):
        refused(lambda: execute(a.world.steward, "record_channel_alert",
                                {"request_key": "r", "project_id": HOME, "channel": "wechat", "kind": "down",
                                 "down_since": since, "via": "slack", **extra}, allow_write=True), "invalid_request")
    a.record("wechat", "down", since, error="e" * 1000)
    assert a.row()["alert_error"] == "e" * state_channels.ALERT_ERROR_MAX


def test_an_old_status_table_gains_the_alert_columns_and_its_rows_read_as_never_alerted(tmp_path):
    db = StateDatabase(str(tmp_path / "old.sqlite"))
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE state_owner_channel_status (channel TEXT PRIMARY KEY CHECK(channel IN "
                     "('slack','wechat')), connected INTEGER NOT NULL CHECK(connected IN (0,1)), detail TEXT, "
                     "qr_svg TEXT, at TEXT NOT NULL)")
        conn.execute("INSERT INTO state_owner_channel_status VALUES('wechat',0,'old',NULL,'2026-11-01T00:00:00+00:00')")
        conn.commit()
    StateService(db, actor="fixture", project_read_trusted=True).channels  # noqa: B018 (builds the schema)
    with db.get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM state_owner_channel_status").fetchone())
    assert {k: row[k] for k in ("down_since", "alerted_since", "up_due", "up_at", "alert_via", "alerted_at",
                                "alert_attempts", "alert_error")} == {
        "down_since": None, "alerted_since": None, "up_due": 0, "up_at": None, "alert_via": None,
        "alerted_at": None, "alert_attempts": 0, "alert_error": None}
