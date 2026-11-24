"""Step 19, on-demand QR (design-steward 7.2 "按需二维码"): the owner asks for a new WeChat
login QR on the dashboard; the steward's host runs the request in code and writes the
bridge's answer back. The request, its event and its result never carry a QR; a request
nobody ran within 2 minutes is `expired` and is no longer handed to the host.

The fixtures (a State file with the steward's and a director's drivers, the owner's
session, the app with the write gate ON) are test_steward_channels.py's."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from core import state_channels
from core.state_commands import STEWARD_WRITE_ACTIONS, WRITER_ONLY_READS, execute
from tests.unit.test_steward_channels import world_baseline  # noqa: F401 (the world fixture needs it)
from tests.unit.test_steward_channels import HOME, OWNER_HEADERS, client, refused, world  # noqa: F401 (fixtures)


def complete(world, request_id, result, key=None, service=None):
    return execute(service or world.steward, "complete_owner_channel_login_request",
                   {"request_key": key or f"login-{request_id}", "project_id": HOME, "request_id": request_id,
                    "result": result}, allow_write=True)


def pending(world, service=None):
    return execute(service or world.steward, "list_owner_channel_login_requests", {})["requests"]


def events(world):
    with world.db.get_connection() as conn:
        return [(r[0], json.loads(r[1])) for r in conn.execute(
            "SELECT event_type,payload_json FROM state_events WHERE project_id=? "
            "AND event_type='owner_channel_login_requested' ORDER BY rowid", (HOME,))]


def login_request(world):
    return world.owner.channels.list_owner_channels(with_status=True)["channels"][1]["login_request"]


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": state_channels._now()}
    monkeypatch.setattr(state_channels, "_now", lambda: moment["now"])
    return moment


def test_the_owner_requests_a_qr_the_host_lists_runs_and_completes_it(world, clock):
    assert login_request(world) is None, "no request yet"
    made = world.owner.channels.request_login("wechat")
    assert made["status"] == "pending" and made["channel"] == "wechat" and made["result"] is None
    assert made["request_id"].startswith("qrreq-")
    # One quiet event on the steward's project, naming the request only.
    assert events(world) == [("owner_channel_login_requested", {"request_id": made["request_id"], "channel": "wechat"})]
    # Pressed again while it is pending: the same request, no second row or event.
    assert world.owner.channels.request_login("wechat") == made
    assert len(events(world)) == 1
    assert pending(world) == [{"request_id": made["request_id"], "channel": "wechat",
                               "requested_at": made["requested_at"]}]
    done = complete(world, made["request_id"], {"requested": True})
    assert done == {"request_id": made["request_id"], "status": "done"}
    assert complete(world, made["request_id"], {"requested": True}) == done, "a replay"
    assert pending(world) == []
    shown = login_request(world)
    assert (shown["status"], shown["result"]) == ("done", {"requested": True}) and shown["completed_at"]
    # Done: the next press is a new request.
    clock["now"] += timedelta(seconds=1)
    again = world.owner.channels.request_login("wechat")
    assert again["request_id"] != made["request_id"] and again["status"] == "pending"
    assert login_request(world)["request_id"] == again["request_id"], "the newest one is shown"
    refused(lambda: complete(world, made["request_id"], {"requested": True}, key="other-key"),
            "login_request_not_pending")
    refused(lambda: complete(world, "qrreq-nope", {"requested": True}), "unknown_login_request")


def test_a_request_nobody_ran_within_two_minutes_expires_and_is_not_handed_out(world, clock):
    made = world.owner.channels.request_login("wechat")
    clock["now"] += timedelta(minutes=2)
    assert pending(world) == [], "a host that comes back later never runs it"
    assert login_request(world)["status"] == "expired"
    # Expired, a press makes a new request.
    fresh = world.owner.channels.request_login("wechat")
    assert fresh["request_id"] != made["request_id"] and fresh["status"] == "pending"
    assert [r["request_id"] for r in pending(world)] == [fresh["request_id"]]
    # One that was handed out in time and answered late is still recorded: it did run.
    clock["now"] += timedelta(minutes=3)
    assert complete(world, fresh["request_id"], {"refused": "bridge_unreachable"})["status"] == "done"
    assert login_request(world)["result"] == {"refused": "bridge_unreachable"}


def test_the_result_is_requested_or_a_refusal_code_never_anything_else(world):
    made = world.owner.channels.request_login("wechat")
    secret = "https://login.weixin.qq.com/l/SECRET"
    for bad in ({"requested": True, "qr": secret}, {"refused": "rate_limited", "qr_svg": "<svg/>"},
                {"refused": secret}, {"refused": "rate_limited", "retry_after_s": 0},
                {"refused": "rate_limited", "retry_after_s": True}, {"refused": "rate_limited", "retry_after_s": "9"},
                {"requested": False}, {}, {"refused": ""}):
        refused(lambda: complete(world, made["request_id"], bad, key=json.dumps(bad)), "invalid_result")
    assert complete(world, made["request_id"], {"refused": "rate_limited", "retry_after_s": 95})["status"] == "done"
    assert login_request(world)["result"] == {"refused": "rate_limited", "retry_after_s": 95}


def test_only_the_owner_requests_and_only_the_steward_lists_and_completes(world):
    assert "complete_owner_channel_login_request" in STEWARD_WRITE_ACTIONS
    assert "list_owner_channel_login_requests" in WRITER_ONLY_READS
    for caller in (world.steward, world.director):
        refused(lambda: caller.channels.request_login("wechat"), "owner_session_required")
    refused(lambda: world.owner.channels.request_login("slack"), "invalid_channel")
    made = world.owner.channels.request_login("wechat")
    for caller in (world.owner, world.director):
        refused(lambda: pending(world, service=caller), "steward_role_required")
        refused(lambda: complete(world, made["request_id"], {"requested": True}, service=caller),
                "steward_role_required")
    # The State query never carries the login request; only the owner's dashboard list does.
    assert all("login_request" not in c for c in execute(world.owner, "list_owner_channels", {})["channels"])
    refused(lambda: world.steward.channels.list_owner_channels(with_status=True), "owner_session_required")


def test_each_channel_keeps_its_newest_twenty_requests(world, clock):
    ids = []
    for _ in range(25):
        made = world.owner.channels.request_login("wechat")
        complete(world, made["request_id"], {"requested": True})
        ids.append(made["request_id"])
        clock["now"] += timedelta(seconds=1)
    with world.db.get_connection() as conn:
        kept = [r[0] for r in conn.execute("SELECT request_id FROM state_owner_channel_login_requests "
                                           "ORDER BY requested_at")]
    assert kept == ids[-20:]


def test_the_qr_route_is_the_owner_sessions_alone_and_wechat_only(world, client):
    for name in ("steward", "director"):
        r = client.post("/api/steward/channels/wechat/qr", headers={"X-AItelier-Driver-Token": world.tokens[name]})
        assert r.status_code == 403, (name, r.text)
    assert client.post("/api/steward/channels/slack/qr", headers=OWNER_HEADERS).status_code == 422
    r = client.post("/api/steward/channels/wechat/qr", headers=OWNER_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "pending", r.text
    assert client.post("/api/steward/channels/wechat/qr", headers=OWNER_HEADERS).json() == r.json()
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    listed = client.post("/api/state/query/list_owner_channel_login_requests", headers=steward, json={})
    assert listed.status_code == 200, listed.text
    assert listed.json().get("result", listed.json())["requests"][0]["request_id"] == r.json()["request_id"]
    done = client.post("/api/state/commands/complete_owner_channel_login_request", headers=steward,
                       json={"request_key": "k", "request_id": r.json()["request_id"],
                             "result": {"refused": "qr_waiting"}})
    assert done.status_code == 200, done.text
    wechat = client.get("/api/steward/channels", headers=OWNER_HEADERS).json()["channels"][1]
    assert wechat["login_request"]["status"] == "done" and wechat["login_request"]["result"] == {"refused": "qr_waiting"}
    assert client.get("/api/steward/channels", headers=OWNER_HEADERS).json()["channels"][0]["login_request"] is None
