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


def relay(world, body, ref, key=None, channel="slack", sender=SLACK_USER, service=None):
    return execute(service or world.steward, "relay_owner_message",
                   {"request_key": key or f"relay-{ref}", "channel": channel, "sender_ref": sender,
                    "channel_message_ref": ref, "body": body}, allow_write=True)


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
    assert relay(world, "hello", "ts-1", key="k1") == first
    again = relay(world, "hello", "ts-1", key="k2")
    assert again == {**first, "duplicate": True}
    with pytest.raises(StateGraphError, match="idempotency_conflict"):
        relay(world, "other", "ts-9", key="k1")
    assert len(conversation(world)) == 1


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
