"""Step 19: the owner's chat channels to the steward (design-steward 7.2).

The steward's HOST relays what Slack / WeChat received (relay_owner_message,
bind_owner_channel, record_channel_delivery - steward only); the owner binds, unbinds
and picks the default channel on the dashboard (owner session only). An exact
approval phrase in an owner message decides a proposal through the dashboard's own
path. The HTTP tests run the real app with the write gate ON, like
tests/unit/test_steward_proposals.py."""
from __future__ import annotations

import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import authz
from api.dependencies import get_db_manager
from api.main import app
from core import cf_access, state_channels
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers
from core.state_commands import STEWARD_WRITE_ACTIONS, execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

HOME = "aitelier-home"
OWNER = "owner@example.com"
PEPPER = "steward-channel-pepper-" * 3
SLACK_USER = "U0OWNER"
CREATE = {"project_id": "gamma", "title": "Gamma", "goal": "a new project", "seed_nodes": []}


@pytest.fixture
def world(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    registry = DriverRegistry(db, PEPPER)
    registry.seed("steward-channel-owner-cli-token")
    seeder = StateService(db, actor="fixture", project_read_trusted=True)
    for project in ("alpha", HOME):
        seeder.store.create_project(project, project.title())
    runtime = DshDrivers(db, registry, tmp_path / "drivers",
                         command=[sys.executable, "-c", "import time; time.sleep(90)"])
    steward = runtime._provision(HOME, "aitelier", "driver", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    tokens = {name: (runtime.root / row["driver_id"] / "token").read_text().strip()
              for name, row in (("steward", steward), ("director", director))}

    def as_driver(row):
        return StateService(db, actor="driver:" + row["driver_id"], driver_id=row["driver_id"],
                            project_read_trusted=True)

    owner = StateService(db, actor="owner:" + OWNER, project_read_trusted=True)
    return type("World", (), dict(db=db, runtime=runtime, registry=registry, tokens=tokens, owner=owner,
                                  steward_row=steward, steward=as_driver(steward),
                                  director=as_driver(director)))


def bind(world, channel="slack", sender=SLACK_USER, key="bind-1"):
    code = world.owner.channels.issue_code(channel)["code"]
    return execute(world.steward, "bind_owner_channel",
                   {"request_key": key, "channel": channel, "sender_ref": sender, "code": code}, allow_write=True)


def relay(world, body, ref, key=None, channel="slack", sender=SLACK_USER, service=None, channel_at=None):
    return execute(service or world.steward, "relay_owner_message",
                   {"request_key": key or f"relay-{ref}", "channel": channel, "sender_ref": sender,
                    "channel_message_ref": ref, "body": body,
                    **({"channel_at": channel_at} if channel_at is not None else {})}, allow_write=True)


def plant_proposal(world, proposal_id="p-1", kind="create_project", payload=None):
    with world.db.get_connection() as conn:
        conn.execute("INSERT INTO state_steward_proposals(proposal_id,version,kind,payload_json,reason,status,"
                     "created_by,created_at) VALUES(?,1,?,?,'because','open',?,'t')",
                     (proposal_id, kind, json.dumps(payload or CREATE),
                      "driver:" + world.steward_row["driver_id"]))
        conn.commit()


def proposal(world, proposal_id="p-1"):
    with world.db.get_connection() as conn:
        out = dict(conn.execute("SELECT * FROM state_steward_proposals WHERE proposal_id=?",
                                (proposal_id,)).fetchone())
    out["result"] = json.loads(out["result_json"]) if out["result_json"] else None
    return out


def conversation(world):
    return [m for m in execute(world.owner, "list_user_conversation", {"project_id": HOME})["messages"]
            if m["sender_kind"] == "user"]


def refused(call, code):
    with pytest.raises(StateGraphError) as caught:
        call()
    assert str(caught.value).split(":", 1)[0] == code, str(caught.value)


# --- binding -------------------------------------------------------------------------------

def test_a_code_the_owner_issued_binds_the_sender_once(world):
    issued = world.owner.channels.issue_code("slack")
    assert len(issued["code"]) == 6 and issued["code"].isdigit() and issued["expires_at"]
    with world.db.get_connection() as conn:
        stored = [r[0] for r in conn.execute("SELECT code_hash FROM state_owner_channel_codes")]
    assert stored and issued["code"] not in stored[0], "the code is kept hashed"
    args = {"request_key": "b1", "channel": "slack", "sender_ref": SLACK_USER, "code": issued["code"]}
    out = execute(world.steward, "bind_owner_channel", args, allow_write=True)
    assert out == {"bound": True, "channel": "slack", "sender_ref": SLACK_USER}
    assert execute(world.steward, "bind_owner_channel", args, allow_write=True) == out, "a replay"
    refused(lambda: execute(world.steward, "bind_owner_channel", {**args, "request_key": "b2"}, allow_write=True),
            "code_used")
    listed = execute(world.owner, "list_owner_channels", {})
    assert listed["channels"][0] == {"channel": "slack", "bound": True, "sender_ref": SLACK_USER,
                                     "bound_at": listed["channels"][0]["bound_at"], "revoked_at": None,
                                     "is_default": False}
    assert listed["channels"][1]["channel"] == "wechat" and not listed["channels"][1]["bound"]
    assert listed["default_channel"] is None


def test_a_wrong_expired_or_other_channel_code_binds_nothing(world, monkeypatch):
    code = world.owner.channels.issue_code("wechat")["code"]
    args = {"request_key": "b", "channel": "slack", "sender_ref": "intruder", "code": code}
    refused(lambda: execute(world.steward, "bind_owner_channel", args, allow_write=True), "invalid_code")
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    refused(lambda: execute(world.steward, "bind_owner_channel",
                            {**args, "request_key": "b2", "channel": "wechat", "code": wrong}, allow_write=True),
            "invalid_code")
    refused(lambda: execute(world.steward, "bind_owner_channel",
                            {**args, "request_key": "b3", "channel": "wechat", "code": "12345a"}, allow_write=True),
            "invalid_code")
    later = state_channels._now() + timedelta(minutes=10, seconds=1)
    monkeypatch.setattr(state_channels, "_now", lambda: later)
    refused(lambda: execute(world.steward, "bind_owner_channel",
                            {**args, "request_key": "b4", "channel": "wechat"}, allow_write=True), "code_expired")
    assert not any(c["bound"] for c in execute(world.owner, "list_owner_channels", {})["channels"])


def test_five_refused_attempts_retire_the_live_code_until_the_owner_issues_a_new_one(world, caplog):
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    code = world.owner.channels.issue_code("slack")["code"]
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    other = world.owner.channels.issue_code("wechat")["code"]
    attempt = {"channel": "slack", "sender_ref": "intruder"}
    # Any refused attempt on the channel counts: a wrong code, a malformed one, another
    # channel's code - each a guess at the live code.
    for i, guess in enumerate([wrong, "12345a", other, wrong]):
        refused(lambda: execute(world.steward, "bind_owner_channel", {**attempt, "request_key": f"g{i}", "code": guess},
                                allow_write=True), "invalid_code")
    refused(lambda: execute(world.steward, "bind_owner_channel", {**attempt, "request_key": "g4", "code": wrong},
                            allow_write=True), "invalid_code")
    assert "retired after 5 refused binding attempts" in caplog.records[-1].getMessage()
    assert wrong not in caplog.records[-1].getMessage() and code not in caplog.records[-1].getMessage()
    # The sixth try, even with the right code and from the owner's own account, binds nothing.
    for key, sender in (("g5", "intruder"), ("g6", SLACK_USER)):
        refused(lambda: execute(world.steward, "bind_owner_channel",
                                {"request_key": key, "channel": "slack", "sender_ref": sender, "code": code},
                                allow_write=True), "code_retired")
    assert not execute(world.owner, "list_owner_channels", {})["channels"][0]["bound"]
    # The other channel's code is untouched; a new slack code binds.
    assert execute(world.steward, "bind_owner_channel", {"request_key": "w", "channel": "wechat",
                                                         "sender_ref": "wxid_owner", "code": other},
                   allow_write=True)["bound"]
    bind(world, key="fresh")
    assert execute(world.owner, "list_owner_channels", {})["channels"][0]["sender_ref"] == SLACK_USER


def test_four_refused_attempts_leave_the_code_live_and_none_count_without_one(world):
    # Guesses while the channel has no live code count against no later code.
    for i in range(5):
        refused(lambda: execute(world.steward, "bind_owner_channel", {"request_key": f"n{i}", "channel": "slack",
                                                                      "sender_ref": "x", "code": "000000"},
                                allow_write=True), "invalid_code")
    code = world.owner.channels.issue_code("slack")["code"]
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    for i in range(4):
        refused(lambda: execute(world.steward, "bind_owner_channel", {"request_key": f"g{i}", "channel": "slack",
                                                                      "sender_ref": "x", "code": wrong},
                                allow_write=True), "invalid_code")
    assert execute(world.steward, "bind_owner_channel", {"request_key": "ok", "channel": "slack",
                                                         "sender_ref": SLACK_USER, "code": code},
                   allow_write=True)["bound"]


def test_a_new_code_retires_the_unused_one_and_a_rebind_replaces_the_account(world):
    first = world.owner.channels.issue_code("slack")["code"]
    world.owner.channels.issue_code("slack")
    refused(lambda: execute(world.steward, "bind_owner_channel", {"request_key": "b", "channel": "slack",
                                                                  "sender_ref": "U1", "code": first},
                            allow_write=True), "invalid_code")
    bind(world, sender="U1", key="b1")
    bind(world, sender="U2", key="b2")
    slack = execute(world.owner, "list_owner_channels", {})["channels"][0]
    assert slack["sender_ref"] == "U2" and slack["bound"]
    refused(lambda: relay(world, "hello", "m1", sender="U1"), "channel_sender_not_bound")


def test_unbind_and_the_default_channel_are_the_owners(world):
    bind(world)
    assert world.owner.channels.set_default("wechat") == {"default_channel": "wechat"}
    assert world.owner.channels.set_default("slack") == {"default_channel": "slack"}
    listed = execute(world.owner, "list_owner_channels", {})
    assert listed["default_channel"] == "slack"
    assert [c["is_default"] for c in listed["channels"]] == [True, False]
    world.owner.channels.unbind("slack")
    slack = execute(world.owner, "list_owner_channels", {})["channels"][0]
    assert not slack["bound"] and slack["revoked_at"] and slack["sender_ref"] == SLACK_USER
    world.owner.channels.set_default(None)
    assert execute(world.owner, "list_owner_channels", {})["default_channel"] is None
    for call in (lambda: world.steward.channels.issue_code("slack"), lambda: world.steward.channels.unbind("slack"),
                 lambda: world.steward.channels.set_default("slack")):
        refused(call, "owner_session_required")


# --- relay ---------------------------------------------------------------------------------

def test_an_unbound_sender_is_dropped_logged_without_its_body_and_wakes_no_one(world, caplog):
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    refused(lambda: relay(world, "secret plans for the owner", "m1"), "channel_sender_not_bound")
    bind(world)
    refused(lambda: relay(world, "secret plans for the owner", "m2", sender="U-STRANGER"),
            "channel_sender_not_bound")
    assert conversation(world) == []
    assert execute(world.steward, "list_driver_messages", {})["messages"] == []
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2 and all("channel_sender_not_bound" in line and "channel=slack" in line
                                   and "bytes=26" in line for line in lines), lines
    assert "U-STRANGER" in lines[1] and not any("secret" in line for line in lines)


def test_a_bound_owner_message_lands_like_a_dashboard_message_with_its_source(world):
    bind(world)
    out = relay(world, "what is running?", "slack-ts-1")
    assert out == {"message_id": out["message_id"], "duplicate": False, "approval": None}
    [message] = conversation(world)
    assert message["message_id"] == out["message_id"] and message["body"] == "what is running?"
    assert message["subject"] == "Message from the user"
    inbox = execute(world.steward, "list_driver_messages", {})["messages"]
    assert [m["message_id"] for m in inbox] == [out["message_id"]], "the steward is woken by the same delivery"
    dashboard = execute(world.owner, "send_user_conversation_message",
                        {"request_key": "d1", "project_id": HOME, "body": "from the dashboard"}, allow_write=True)
    sources = execute(world.owner, "message_sources",
                      {"message_ids": [out["message_id"], dashboard["message_id"]]})["sources"]
    assert list(sources) == [out["message_id"]], "no row = dashboard"
    assert sources[out["message_id"]] == {"direction": "in", "channel": "slack", "sender_ref": SLACK_USER,
                                          "channel_message_ref": "slack-ts-1",
                                          "at": sources[out["message_id"]]["at"], "error": None}
    assert execute(world.steward, "message_sources", {"message_ids": [out["message_id"]]})["sources"]


def test_a_relay_is_idempotent_by_request_key_and_by_channel_message(world):
    bind(world)
    first = relay(world, "hello", "ts-1", key="k1")
    assert first["duplicate"] is False
    assert relay(world, "hello", "ts-1", key="k1") == {**first, "duplicate": True}, \
        "a replay of the same request is a redelivery too"
    again = relay(world, "hello", "ts-1", key="k2")
    assert again == {**first, "duplicate": True}
    with pytest.raises(StateGraphError, match="idempotency_conflict"):
        relay(world, "other", "ts-9", key="k1")
    assert len(conversation(world)) == 1


def test_another_senders_message_with_the_owners_ref_is_judged_on_its_own(world):
    """The duplicate-by-ref answer belongs to the sender of the first message: an unbound
    sender relaying with the owner's channel_message_ref used to get the owner's first
    result back (the executed proposal record included) as duplicate: true."""
    bind(world)
    plant_proposal(world)
    first = relay(world, "同意 proposal p-1", "ts-1", key="owner")
    assert first["approval"]["outcome"] == "executed"
    refused(lambda: relay(world, "同意 proposal p-1", "ts-1", key="stranger", sender="U-STRANGER"),
            "channel_sender_not_bound")
    # The owner's own redelivery is still the first result.
    assert relay(world, "同意 proposal p-1", "ts-1", key="again") == {**first, "duplicate": True}
    # A new bound account's message that happens to carry the same ref is a new message.
    bind(world, sender="U-NEW", key="bind-new")
    second = relay(world, "hello", "ts-1", key="new", sender="U-NEW")
    assert second["duplicate"] is False and second["message_id"] != first["message_id"]
    assert len(conversation(world)) == 2


@pytest.mark.parametrize("body,accepted", [
    ("a" * 4096, True), ("a" * 4097, False),
    ("汉" * 1365 + "a", True), ("汉" * 1366, False)])   # 4096 / 4098 UTF-8 bytes
def test_the_body_is_bounded_at_4_kib_of_utf8(world, caplog, body, accepted):
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    bind(world)
    if accepted:
        assert relay(world, body, "ts")["message_id"]
        return
    refused(lambda: relay(world, body, "ts"), "body_too_long")
    assert conversation(world) == []
    [line] = [r.getMessage() for r in caplog.records]
    assert f"bytes={len(body.encode())}" in line and body[:20] not in line


def test_an_empty_body_is_refused(world):
    bind(world)
    refused(lambda: relay(world, " \n　", "ts"), "empty_body")


def test_a_lone_surrogate_is_refused_as_bad_input_not_a_crash_that_holds_the_lane(world, caplog):
    """JSON "\\ud800" decodes to a lone UTF-16 surrogate, which UTF-8 cannot encode: it
    used to raise UnicodeEncodeError (a 500, which the host takes as an outage and the
    WeChat plugin retries for 10 minutes, every later message held behind it). Now it is
    refused like any other bad input, before anything is hashed, measured or stored."""
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    for sender in ("U-STRANGER", SLACK_USER):
        refused(lambda: relay(world, "hi \ud800 secret", f"ts-{sender}", sender=sender), "invalid_body")
    bind(world)
    refused(lambda: relay(world, "hi \ud800 secret", "ts-b"), "invalid_body")
    # The bounded fields are refused by their schema (a validation 422), never a crash.
    for call in (lambda: relay(world, "hi", "ts-\udfff", key="k1"),
                 lambda: relay(world, "hi", "ts-s", key="k2", sender="U\ud800"),
                 lambda: execute(world.steward, "bind_owner_channel", {"request_key": "b", "channel": "slack",
                                                                       "sender_ref": "U\ud800", "code": "123456"},
                                 allow_write=True)):
        with pytest.raises(StateGraphError, match="unable to parse raw data as a unicode string"):
            call()
    refused(lambda: report(world, "s1", detail="scan \ud800"), "invalid_request")
    refused(lambda: report(world, "s2", qr_svg='<svg xmlns="http://www.w3.org/2000/svg">\ud800</svg>'),
            "qr_svg_refused")
    assert conversation(world) == []
    lines = [r.getMessage() for r in caplog.records]
    assert lines and all("invalid_body" in line for line in lines) and not any("secret" in line for line in lines)
    # A surrogate pair is ordinary text.
    assert relay(world, "ok \U0001F600", "ts-ok")["message_id"]


def test_a_lone_surrogate_over_http_is_a_422_refusal(world, client):
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"], "Content-Type": "application/json"}
    raw = ('{"request_key": "r", "channel": "slack", "sender_ref": "U-STRANGER", "channel_message_ref": "ts", '
           '"body": "hi \\ud800"}')
    r = client.post("/api/state/commands/relay_owner_message", headers=steward, content=raw.encode())
    assert r.status_code == 422 and r.json()["detail"].startswith("invalid_body"), r.text


def test_the_rate_limit_counts_accepted_messages_per_channel(world, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    bind(world)
    bind(world, channel="wechat", sender="wxid_owner", key="bind-2")
    for i in range(6):
        relay(world, f"m{i}", f"ts-{i}")
    refused(lambda: relay(world, "seventh", "ts-6"), "rate_limited")
    assert "rate_limited" in caplog.records[-1].getMessage() and "seventh" not in caplog.records[-1].getMessage()
    relay(world, "the other channel has its own budget", "w-1", channel="wechat", sender="wxid_owner")
    later = state_channels._now() + timedelta(seconds=61)
    monkeypatch.setattr(state_channels, "_now", lambda: later)
    relay(world, "a minute later", "ts-7")
    # The hour: 60 accepted in the last hour refuse the 61st; older ones do not count.
    monkeypatch.setenv("AITELIER_CHANNEL_RATE_PER_MINUTE", "1000")
    with world.db.get_connection() as conn:
        for i in range(60 - 7):
            conn.execute("INSERT INTO state_message_sources(message_id,direction,channel,sender_ref,"
                         "channel_message_ref,at) VALUES(?,'in','slack',?,?,?)",
                         (f"planted-{i}", SLACK_USER, f"p-{i}", (later - timedelta(minutes=30)).isoformat()))
        conn.execute("INSERT INTO state_message_sources(message_id,direction,channel,sender_ref,"
                     "channel_message_ref,at) VALUES('old','in','slack',?,'old',?)",
                     (SLACK_USER, (later - timedelta(minutes=61)).isoformat()))
        conn.commit()
    refused(lambda: relay(world, "sixty-first", "ts-8"), "rate_limited")
    monkeypatch.setenv("AITELIER_CHANNEL_RATE_PER_HOUR", "61")
    relay(world, "the env override raises it", "ts-9")


# --- steward only / owner only ----------------------------------------------------------------

def test_the_channel_commands_are_the_stewards_and_the_reads_the_owners_or_the_stewards(world):
    bind(world)
    args = {"relay_owner_message": {"request_key": "x", "project_id": HOME, "channel": "slack",
                                    "sender_ref": SLACK_USER, "channel_message_ref": "r", "body": "b"},
            "bind_owner_channel": {"request_key": "x", "project_id": HOME, "channel": "slack",
                                   "sender_ref": SLACK_USER, "code": "123456"},
            "record_channel_delivery": {"request_key": "x", "project_id": HOME, "channel": "slack",
                                        "message_id": "m"}}
    assert set(args) <= STEWARD_WRITE_ACTIONS
    for caller in (world.owner, world.director):
        for action, arguments in args.items():
            refused(lambda: execute(caller, action, arguments, allow_write=True), "steward_role_required")
    refused(lambda: execute(world.director, "list_owner_channels", {}), "owner_or_steward_required")
    refused(lambda: execute(world.director, "message_sources", {"message_ids": []}), "owner_or_steward_required")
    assert execute(world.steward, "list_owner_channels", {})["channels"][0]["bound"]
    # The model never approves: nothing the steward may call decides a proposal.
    assert not {a for a in STEWARD_WRITE_ACTIONS if "approve" in a or "decline" in a}


def test_a_delivery_is_recorded_once_for_the_stewards_own_message(world):
    sent = execute(world.steward, "send_user_message", {"request_key": "s1", "subject": "Hi", "body": "hello",
                                                         "project_id": HOME}, allow_write=True)
    args = {"request_key": "d1", "message_id": sent["message_id"], "channel": "slack",
            "channel_message_ref": "171.2"}
    out = execute(world.steward, "record_channel_delivery", args, allow_write=True)
    assert out == {"recorded": True, "message_id": sent["message_id"], "channel": "slack"}
    assert execute(world.steward, "record_channel_delivery", args, allow_write=True) == out
    with pytest.raises(StateGraphError, match="delivery_recorded"):
        execute(world.steward, "record_channel_delivery", {**args, "request_key": "d2", "error": "x"},
                allow_write=True)
    source = execute(world.owner, "message_sources", {"message_ids": [sent["message_id"]]})["sources"]
    assert source[sent["message_id"]]["direction"] == "out" and source[sent["message_id"]]["error"] is None
    owner_said = execute(world.owner, "send_user_conversation_message",
                         {"request_key": "o", "project_id": HOME, "body": "hi"}, allow_write=True)
    with pytest.raises(StateGraphError, match="unknown_message"):
        execute(world.steward, "record_channel_delivery",
                {"request_key": "d3", "message_id": owner_said["message_id"], "channel": "wechat",
                 "error": "send failed"}, allow_write=True)


# --- channel status (the host's report, the owner's dashboard) ---------------------------------

QR = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 29 29" shape-rendering="crispEdges">'
      '<path fill="#fff" d="M0 0h29v29H0z"/><path d="M4 4h7v1H4zM4 5h1v1H4z"/></svg>')


def report(world, key, service=None, **fields):
    return execute(service or world.steward, "report_channel_status",
                   {"request_key": key, "project_id": HOME, "channel": "wechat", "connected": False, **fields},
                   allow_write=True)


def test_the_hosts_status_report_reaches_the_owners_list_and_only_there(world):
    assert all(c["status"] is None and c["qr_svg"] is None
               for c in world.owner.channels.list_owner_channels(with_status=True)["channels"]), "nothing reported"
    out = report(world, "s1", detail="scan needed", qr_svg=QR)
    assert out == {"recorded": True, "channel": "wechat", "connected": False, "at": out["at"]}
    assert report(world, "s1", detail="scan needed", qr_svg=QR) == out, "a replay"
    listed = {c["channel"]: c for c in world.owner.channels.list_owner_channels(with_status=True)["channels"]}
    assert listed["wechat"]["status"] == {"connected": False, "detail": "scan needed", "at": out["at"]}
    assert listed["wechat"]["qr_svg"] == QR
    assert listed["slack"]["status"] is None and listed["slack"]["qr_svg"] is None
    # A later report replaces the row: logged in, the QR is gone.
    report(world, "s2", connected=True)
    wechat = world.owner.channels.list_owner_channels(with_status=True)["channels"][1]
    assert wechat["status"]["connected"] is True and wechat["status"]["detail"] is None and wechat["qr_svg"] is None
    # The State query (the steward's, and the owner's) never carries the status or the QR.
    for caller in (world.steward, world.owner):
        assert all("status" not in c and "qr_svg" not in c
                   for c in execute(caller, "list_owner_channels", {})["channels"])
    refused(lambda: world.steward.channels.list_owner_channels(with_status=True), "owner_session_required")


def test_the_wechat_bridges_own_drawing_of_a_login_qr_is_accepted(world):
    # integrations/wechat-bridge/qr-svg.test.mjs pins this file to the bridge's real output.
    drawn = (Path(__file__).resolve().parents[2] / "integrations" / "wechat-bridge" / "test-fixtures"
             / "login-qr.svg").read_text(encoding="utf-8")
    report(world, "real", detail="scan needed", qr_svg=drawn)
    assert world.owner.channels.list_owner_channels(with_status=True)["channels"][1]["qr_svg"] == drawn


def test_only_the_steward_reports_a_status(world):
    assert "report_channel_status" in STEWARD_WRITE_ACTIONS
    for caller in (world.owner, world.director):
        refused(lambda: report(world, "x", service=caller, qr_svg=QR), "steward_role_required")
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_owner_channel_status").fetchone()[0] == 0


@pytest.mark.parametrize("svg", [
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><svg:script>alert(1)</svg:script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect ONCLICK = "x"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><a href="https://example.com"><rect/></a></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><use xlink:href="#x"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div/></foreignObject></svg>',
    '<!DOCTYPE svg [<!ENTITY x "&#60;script&#62;">]><svg>&x;</svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><!ENTITY x "y"></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><?xml-stylesheet href="x.css"?></svg>',
    '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>',
    '<html><svg></svg></html>', '<svgx></svgx>', 'not an image', '<svg xmlns="x">',
])
def test_a_qr_that_is_not_a_plain_svg_is_refused_and_never_quoted(world, svg, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(StateGraphError) as caught:
        report(world, "bad", qr_svg=svg)
    assert str(caught.value).startswith("qr_svg_refused"), str(caught.value)
    assert svg not in str(caught.value) and svg not in caplog.text
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_owner_channel_status").fetchone()[0] == 0


def test_the_status_is_bounded_and_its_contents_are_never_logged(world, caplog):
    caplog.set_level(logging.DEBUG)
    head, tail = '<svg xmlns="http://www.w3.org/2000/svg">', "</svg>"
    largest = head + "<g/>" * ((state_channels.QR_SVG_MAX_BYTES - len(head) - len(tail)) // 4) + tail
    largest += " " * (state_channels.QR_SVG_MAX_BYTES - len(largest))
    assert len(largest.encode()) == state_channels.QR_SVG_MAX_BYTES
    report(world, "big", qr_svg=largest)
    refused(lambda: report(world, "bigger", qr_svg=largest + " "), "qr_svg_too_large")
    report(world, "d300", detail="d" * 300)
    refused(lambda: report(world, "d301", detail="SECRET-DETAIL" + "d" * 288), "detail_too_long")
    with pytest.raises(StateGraphError, match="'field': 'connected'"):
        execute(world.steward, "report_channel_status", {"request_key": "c", "project_id": HOME, "channel": "wechat",
                                                         "connected": "yes"},
                allow_write=True)
    report(world, "fine", detail="SECRET-DETAIL scan needed", qr_svg=QR)
    assert "SECRET-DETAIL" not in caplog.text and QR not in caplog.text and "<svg" not in caplog.text


# --- chat approval -----------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "同意 proposal p-1。", "同意proposal p-1", "同意　proposal p-1", " ok", "同意 proposal P-1",
    "同意  proposal p-1", "请 同意 proposal p-1", "同意 proposal p-1 谢谢", "approve proposal p-1"])
def test_anything_but_the_exact_phrase_is_an_ordinary_message(world, body):
    bind(world)
    plant_proposal(world)
    out = relay(world, body, "ts")
    assert out["approval"] is None and out["message_id"]
    assert proposal(world)["status"] == "open" and proposal(world)["version"] == 1


def test_the_exact_phrase_approves_as_the_bound_account_through_the_dashboards_path(world):
    bind(world)
    plant_proposal(world)
    out = relay(world, "  同意 proposal p-1\n", "ts-approve")
    approval = out["approval"]
    assert approval["proposal_id"] == "p-1" and approval["action"] == "approve"
    assert approval["outcome"] == "executed" and approval["result"]["status"] == "done", approval
    row = proposal(world)
    assert row["status"] == "done" and row["decided_by"] == f"owner:slack:{SLACK_USER}"
    assert {k: row["result"][k] for k in ("channel", "sender_ref", "channel_message_ref", "phrase")} == {
        "channel": "slack", "sender_ref": SLACK_USER, "channel_message_ref": "ts-approve",
        "phrase": "同意 proposal p-1"}
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT 1 FROM state_projects WHERE project_id='gamma'").fetchone()
    assert any("p-1" in m["subject"] for m in execute(world.steward, "list_driver_messages", {})["messages"])
    assert relay(world, "  同意 proposal p-1\n", "ts-approve", key="other") == {**out, "duplicate": True}, \
        "a redelivered message answers the first result and decides nothing again"


def test_an_approval_replayed_under_its_request_key_says_duplicate_and_decides_nothing_again(world):
    bind(world)
    plant_proposal(world)
    out = relay(world, "同意 proposal p-1", "ts-approve", key="k")
    assert out["duplicate"] is False and out["approval"]["outcome"] == "executed"
    assert relay(world, "同意 proposal p-1", "ts-approve", key="k") == {**out, "duplicate": True}
    assert len(conversation(world)) == 1 and proposal(world)["status"] == "done"


@pytest.mark.parametrize("age,outcome", [(timedelta(minutes=9, seconds=59), "executed"),
                                         (timedelta(minutes=10, seconds=1), "refused"),
                                         (timedelta(hours=3), "refused")])
def test_an_approval_written_on_the_channel_over_ten_minutes_ago_is_refused(world, age, outcome):
    bind(world)
    plant_proposal(world)
    written = (state_channels._now() - age).isoformat()
    out = relay(world, "同意 proposal p-1", "ts", channel_at=written)
    assert out["message_id"] and [m["message_id"] for m in conversation(world)] == [out["message_id"]], \
        "the message itself is written either way"
    assert out["approval"]["outcome"] == outcome, out
    if outcome == "refused":
        assert out["approval"]["refusal"] == "approval_too_old"
        assert proposal(world)["status"] == "open" and proposal(world)["decided_by"] is None
    else:
        assert proposal(world)["status"] == "done"


def test_channel_at_without_an_offset_is_utc_and_garbage_is_refused(world):
    bind(world)
    plant_proposal(world)
    naive = (state_channels._now() - timedelta(minutes=30)).replace(tzinfo=None).isoformat()
    assert relay(world, "拒绝 proposal p-1", "ts-1", channel_at=naive)["approval"]["refusal"] == "approval_too_old"
    refused(lambda: relay(world, "hello", "ts-2", channel_at="yesterday"), "invalid_request")
    # Not part of the request's identity: a redelivery stamped anew is still the same message.
    first = relay(world, "hello", "ts-3", key="k", channel_at="2026-11-18T10:00:00Z")
    assert relay(world, "hello", "ts-3", key="k", channel_at="2026-11-18T10:00:05Z") == {**first, "duplicate": True}


def test_the_decline_phrase_declines_as_the_bound_account(world):
    bind(world, channel="wechat", sender="wxid_owner")
    plant_proposal(world)
    out = relay(world, "拒绝 proposal p-1", "w-1", channel="wechat", sender="wxid_owner")
    assert out["approval"]["outcome"] == "executed" and out["approval"]["action"] == "decline"
    row = proposal(world)
    assert row["status"] == "declined" and row["decided_by"] == "owner:wechat:wxid_owner"
    assert row["result"]["phrase"] == "拒绝 proposal p-1"


def test_a_decided_or_changed_proposal_is_refused_and_left_as_it_is(world, monkeypatch):
    bind(world)
    plant_proposal(world, "p-1")
    world.owner.steward.decline("p-1", 1)
    out = relay(world, "同意 proposal p-1", "ts-1")
    assert out["approval"] == {"proposal_id": "p-1", "action": "approve", "outcome": "refused",
                               "refusal": "proposal_not_open"}
    assert proposal(world, "p-1")["status"] == "declined"
    # The version current when the message was written is the one approved: a change
    # landing between the message and the decision fails it.
    plant_proposal(world, "p-2")
    original = world.steward.steward.decide_begin

    def bumped(*args, **kwargs):
        with world.db.get_connection() as conn:
            conn.execute("UPDATE state_steward_proposals SET version=version+1 WHERE proposal_id='p-2'")
            conn.commit()
        return original(*args, **kwargs)
    monkeypatch.setattr(world.steward.steward, "decide_begin", bumped)
    out = relay(world, "同意 proposal p-2", "ts-2")
    assert out["approval"]["refusal"] == "proposal_changed"
    assert proposal(world, "p-2")["status"] == "open"


def test_m2_an_unbound_sender_never_approves(world, monkeypatch):
    bind(world)
    plant_proposal(world)
    relay(world, "hello", "ts-1")
    world.owner.channels.unbind("slack")
    refused(lambda: relay(world, "同意 proposal p-1", "ts-2"), "channel_sender_not_bound")
    assert proposal(world)["status"] == "open"
    # The re-read inside the deciding transaction: unbound between the message and
    # the decision -> refused, proposal open.
    bind(world, key="bind-again")
    original = world.steward.steward.decide_begin

    def unbound_meanwhile(*args, **kwargs):
        world.owner.channels.unbind("slack")
        return original(*args, **kwargs)
    monkeypatch.setattr(world.steward.steward, "decide_begin", unbound_meanwhile)
    out = relay(world, "同意 proposal p-1", "ts-3")
    assert out["approval"]["outcome"] == "refused" and out["approval"]["refusal"] == "channel_sender_not_bound"
    row = proposal(world)
    assert row["status"] == "open" and row["version"] == 1 and row["decided_by"] is None


def test_m2_the_deciding_transaction_rereads_the_binding_itself(world):
    bind(world)
    plant_proposal(world)
    carrier = {"channel": "slack", "sender_ref": "U-OTHER", "channel_message_ref": "x", "phrase": "p"}
    refused(lambda: world.steward.steward.decide_begin("p-1", 1, "owner:slack:U-OTHER", carrier, carrier),
            "channel_sender_not_bound")
    refused(lambda: world.steward.steward.decide_decline("p-1", 1, "owner:slack:U-OTHER", carrier, carrier),
            "channel_sender_not_bound")
    assert proposal(world)["status"] == "open"


def test_a_dashboard_phrase_decides_as_the_owner_session_unchanged(world):
    plant_proposal(world)
    out = execute(world.owner, "send_user_conversation_message",
                  {"request_key": "d1", "project_id": HOME, "body": "同意 proposal p-1"}, allow_write=True)
    assert out["approval"]["outcome"] == "executed"
    row = proposal(world)
    assert row["status"] == "done" and row["decided_by"] == "owner:" + OWNER
    assert row["result"]["phrase"] == "同意 proposal p-1" and row["result"]["message_id"] == out["message_id"]
    assert "channel" not in row["result"]
    plant_proposal(world, "p-2", payload={**CREATE, "project_id": "delta"})
    elsewhere = execute(world.owner, "send_user_conversation_message",
                        {"request_key": "d2", "project_id": "alpha", "body": "同意 proposal p-2"}, allow_write=True)
    assert "approval" not in elsewhere and proposal(world, "p-2")["status"] == "open", \
        "only the steward's conversation decides"


def test_stop_driver_with_live_work_needs_the_dashboard(world):
    bind(world)
    world.runtime.update("alpha", enabled=1, phase="running", subagents_json=json.dumps(["child-1"]))
    plant_proposal(world, kind="stop_driver", payload={"project_id": "alpha"})
    out = relay(world, "同意 proposal p-1", "ts")
    assert out["approval"]["refusal"] == "needs_dashboard_confirmation"
    assert proposal(world)["status"] == "open"


def test_a_driver_proposal_runs_the_driver_entry_or_is_refused_without_one(world):
    bind(world)
    plant_proposal(world, kind="start_driver", payload={"project_id": "alpha"})
    out = relay(world, "同意 proposal p-1", "ts-1")
    assert out["approval"]["refusal"] == "driver_runtime_unavailable" and proposal(world)["status"] == "open"
    calls = []

    def runner(kind, project_id, actor):
        calls.append((kind, project_id, actor))
        return {"project_id": project_id, "phase": "starting"}
    world.steward.steward.driver_runner = runner
    out = relay(world, "同意 proposal p-1", "ts-2")
    assert out["approval"]["result"]["status"] == "done"
    assert calls == [("start_driver", "alpha", f"owner:slack:{SLACK_USER}")]
    plant_proposal(world, "p-2", kind="stop_driver", payload={"project_id": "alpha"})
    relay(world, "同意 proposal p-2", "ts-3")
    row = proposal(world, "p-2")
    assert row["status"] == "done" and calls[-1][0] == "stop_driver"
    assert row["result"]["confirm_interrupt"] is False and row["result"]["channel"] == "slack", \
        "the confirmation record keeps the chat record"


# --- HTTP ------------------------------------------------------------------------------------

@pytest.fixture
def client(world, monkeypatch):
    import api.main as main
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "WRITERS", {OWNER})
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers",
                        lambda headers, cookies=None: headers.get("X-Test-Email"))
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
    app.dependency_overrides[get_db_manager] = lambda: world.db
    try:
        with TestClient(app) as c:
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            monkeypatch.setattr(app.state, "dsh_drivers", world.runtime, raising=False)
            assert authz.gate_enabled()
            yield c
    finally:
        app.dependency_overrides.pop(get_db_manager, None)


OWNER_HEADERS = {"X-Test-Email": OWNER}


def test_the_channel_routes_are_the_owner_sessions_alone(world, client):
    routes = [("GET", "/api/steward/channels", None), ("POST", "/api/steward/channels/slack/code", None),
              ("DELETE", "/api/steward/channels/slack", None),
              ("PUT", "/api/steward/channels/default", {"channel": "slack"}),
              ("GET", "/api/steward/message-sources?ids=a,b", None)]
    for name in ("steward", "director"):
        for method, path, body in routes:
            r = client.request(method, path, json=body, headers={"X-AItelier-Driver-Token": world.tokens[name]})
            assert r.status_code == 403, (name, method, path, r.status_code, r.text)
    code = client.post("/api/steward/channels/wechat/code", headers=OWNER_HEADERS)
    assert code.status_code == 200 and len(code.json()["code"]) == 6, code.text
    assert client.put("/api/steward/channels/default", json={"channel": "wechat"},
                      headers=OWNER_HEADERS).json() == {"default_channel": "wechat"}
    assert client.put("/api/steward/channels/default", json={"channel": "email"},
                      headers=OWNER_HEADERS).status_code == 422
    assert client.post("/api/steward/channels/email/code", headers=OWNER_HEADERS).status_code == 422
    listed = client.get("/api/steward/channels", headers=OWNER_HEADERS).json()
    assert listed["default_channel"] == "wechat" and [c["channel"] for c in listed["channels"]] == ["slack", "wechat"]
    assert client.delete("/api/steward/channels/wechat", headers=OWNER_HEADERS).status_code == 200
    assert client.get("/api/steward/message-sources?ids=a,b", headers=OWNER_HEADERS).json() == {"sources": {}}
    too_many = ",".join(f"m{i}" for i in range(201))
    assert client.get(f"/api/steward/message-sources?ids={too_many}", headers=OWNER_HEADERS).status_code == 422


def test_the_owner_sees_the_reported_status_and_qr_over_http_and_no_one_else_does(world, client):
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    r = client.post("/api/state/commands/report_channel_status", headers=steward,
                    json={"request_key": "s", "channel": "wechat", "connected": False, "detail": "scan needed",
                          "qr_svg": QR})
    assert r.status_code == 200, r.text
    bad = client.post("/api/state/commands/report_channel_status", headers=steward,
                      json={"request_key": "s2", "channel": "wechat", "connected": False,
                            "qr_svg": '<svg onload="x"></svg>'})
    assert bad.status_code == 422 and bad.json()["detail"].startswith("qr_svg_refused"), bad.text
    for name in ("steward", "director"):
        r = client.get("/api/steward/channels", headers={"X-AItelier-Driver-Token": world.tokens[name]})
        assert r.status_code == 403 and "<svg" not in r.text, (name, r.text)
    wechat = client.get("/api/steward/channels", headers=OWNER_HEADERS).json()["channels"][1]
    assert wechat["channel"] == "wechat" and wechat["qr_svg"] == QR
    assert wechat["status"]["connected"] is False and wechat["status"]["detail"] == "scan needed"
    r = client.post("/api/state/query/list_owner_channels", headers=steward, json={})
    assert r.status_code == 200 and "<svg" not in r.text and "scan needed" not in r.text, r.text


def test_a_relayed_start_approval_runs_the_driver_manager_on_the_apps_loop(world, client, monkeypatch):
    calls = []

    async def start(project_id, *, provider="aitelier", model=None, actor):
        calls.append((project_id, provider, model, actor))
        world.runtime.update(project_id, enabled=1, phase="starting")
        return world.runtime.public(world.runtime.get(project_id))
    monkeypatch.setattr(world.runtime, "start", start)
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    code = client.post("/api/steward/channels/slack/code", headers=OWNER_HEADERS).json()["code"]
    r = client.post("/api/state/commands/bind_owner_channel", headers=steward,
                    json={"request_key": "b", "channel": "slack", "sender_ref": SLACK_USER, "code": code})
    assert r.status_code == 200, r.text
    plant_proposal(world, kind="start_driver", payload={"project_id": "alpha"})
    r = client.post("/api/state/commands/relay_owner_message", headers=steward,
                    json={"request_key": "r", "channel": "slack", "sender_ref": SLACK_USER,
                          "channel_message_ref": "ts", "body": "同意 proposal p-1"})
    assert r.status_code == 200, r.text
    assert r.json()["approval"]["result"]["status"] == "done", r.text
    assert calls == [("alpha", "aitelier", None, f"owner:slack:{SLACK_USER}")]
    r = client.post("/api/state/commands/relay_owner_message", headers=steward,
                    json={"request_key": "r2", "channel": "slack", "sender_ref": "U-STRANGER",
                          "channel_message_ref": "ts2", "body": "hi"})
    assert r.status_code == 422 and r.json()["detail"].startswith("channel_sender_not_bound"), r.text
