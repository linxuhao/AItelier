"""Actual MCP wire refusals must explain safe fields without reflecting inputs."""
import json
from pathlib import Path
from jsonschema import Draft202012Validator
import pytest
from api import authz, dependencies, mcp_router, state_graph_routers
from core.state_database import StateDatabase
from core.state_service import StateService

@pytest.fixture
def wire(client, tmp_path, monkeypatch):
    db = StateDatabase(str(tmp_path / "isolated.sqlite"))
    svc = StateService(db, actor="owner:test", project_read_trusted=True)
    svc.create_project("alpha", "alpha")
    svc.create_project("beta", "beta")
    monkeypatch.setattr(dependencies, "get_db_manager", lambda: db)
    monkeypatch.setattr(dependencies, "get_workspace_manager", lambda: None)
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda request: True)
    monkeypatch.setattr(state_graph_routers, "authenticated_actor", lambda request: "owner:test")
    monkeypatch.setattr(state_graph_routers, "authenticated_driver_id", lambda request: None)
    from core.drivers import SCHEMA
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        for driver in ("sender", "recipient"):
            conn.execute("INSERT INTO drivers(driver_id,display_name,kind,token_hash,status,revision,created_at) VALUES(?,?, 'lan',?, 'active',1,'now')", (driver,driver,"dummy-"+driver))
        conn.execute("INSERT INTO project_drivers VALUES('alpha','sender','member',1,'owner:test','now')")
        conn.execute("INSERT INTO project_drivers VALUES('beta','recipient','member',1,'owner:test','now')")
        conn.commit()
    def call(arguments, action="send_director_message"):

        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 20,
            "method": "tools/call", "params": {"name": "state_graph_write",
                "arguments": {"action": action, "arguments": arguments}}},
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        result = response.json()["result"]
        assert result.get("isError") is not True
        return json.loads(result["content"][0]["text"])["result"]
    return db, call

def request():
    return dict(sender_project_id="alpha", director_identity="director", request_key="one",
                subject="hello", body="", target_project_id="beta")

def snapshot(db):
    with db.get_connection() as conn:
        return list(conn.iterdump())

@pytest.mark.parametrize("case,field", [("missing", "sender_project_id"),
    ("extra", "unexpected field"), ("invalid", "delivery_mode"),
    ("selector", "target_project_id"), ("length", "subject"), ("nested", "arguments")])
def test_actual_mcp_refusal_is_actionable_and_has_no_effects(wire, case, field):
    db, call = wire
    args = request()
    if case == "missing": del args["sender_project_id"]
    if case == "extra": args["sk-SECRET_DO_NOT_REFLECT"] = "secret-value"
    if case == "invalid": args["delivery_mode"] = "sk-SECRET_DO_NOT_REFLECT"
    if case == "selector": args["reply_to_delivery_id"] = "sk-SECRET_DO_NOT_REFLECT"
    if case == "length": args["subject"] = ""
    if case == "nested": args = "sk-SECRET_DO_NOT_REFLECT"
    before = snapshot(db)
    result = call(args)
    print(json.dumps({"case":case,"raw_envelope":result}))
    schema = json.loads((Path(__file__).parents[3] / "contracts/director_messaging/v2/schema.json").read_text())
    Draft202012Validator({"$ref": "#/$defs/error", "$defs": schema["$defs"]}).validate(result)
    assert result["code"] == "invalid_request"
    assert field in result["detail"]["message"]
    assert len(result["detail"]["message"]) <= 1000
    assert "SECRET_DO_NOT_REFLECT" not in json.dumps(result)
    assert snapshot(db) == before

def test_actual_mcp_supported_request_succeeds(wire):
    db, call = wire
    result = call(request())
    assert result["result"]["message"]["actor"] == "owner:test"
    assert len(result["result"]["deliveries"]) == 1


def test_actual_mcp_identity_refusal_and_reply_ownership(wire, monkeypatch):
    db, call = wire
    monkeypatch.setattr(state_graph_routers, "authenticated_driver_id", lambda request: "sender")
    args = request()
    args["director_identity"] = "sk-SECRET_DO_NOT_REFLECT"
    before = snapshot(db)
    refused = call(args)
    assert refused["code"] == "invalid_request"
    assert "director_identity" in refused["detail"]["message"]
    assert "SECRET_DO_NOT_REFLECT" not in json.dumps(refused)
    assert snapshot(db) == before
    args["director_identity"] = "sender/worker"
    sent = call(args)["result"]
    # A sender cannot cite its own outgoing delivery as an incoming reply.
    args.pop("target_project_id")
    args.update(reply_to_delivery_id=sent["deliveries"][0]["delivery_id"],request_key="bad-reply")
    before = snapshot(db)
    assert call(args)["code"] == "unknown_delivery"
    assert snapshot(db) == before

@pytest.mark.parametrize("version", ["v2", "v3"])
def test_actual_mcp_membership_refusal_and_valid_ack_preserve_lifecycle(wire, monkeypatch, version):
    db, call = wire
    sent = call(request())["result"]
    args = dict(project_id="beta", delivery_id=sent["deliveries"][0]["delivery_id"],
                expected_version=1, request_key="ack", protocol_version=version)
    monkeypatch.setattr(state_graph_routers, "authenticated_actor", lambda request: "driver:sender")
    monkeypatch.setattr(state_graph_routers, "authenticated_driver_id", lambda request: "sender")
    before = snapshot(db)
    refused = call(args, "acknowledge_director_message")
    assert refused["code"] == "unauthorized"
    assert "membership" in refused["detail"]["message"]
    assert snapshot(db) == before
    monkeypatch.setattr(state_graph_routers, "authenticated_actor", lambda request: "driver:recipient")
    monkeypatch.setattr(state_graph_routers, "authenticated_driver_id", lambda request: "recipient")
    accepted = call(args, "acknowledge_director_message")
    assert accepted["schema"] == "aitelier.director-messaging." + version
    assert accepted["result"]["delivery"]["status"] == "resolved"


@pytest.mark.parametrize("action", ["send_director_message", "list_director_messages",
    "acknowledge_director_message", "resolve_director_message"])
@pytest.mark.parametrize("protocol", ["synthetic-invalid", None,
    {"secret": "SECRET_DO_NOT_REFLECT"}, ["SECRET_DO_NOT_REFLECT"]])
def test_actual_mcp_prevalidation_protocol_refusal(wire, action, protocol):
    db, call = wire
    if action == "send_director_message":
        args = request()
        # Extended options must not cause an invalid protocol to escape refusal.
        args.update(ack_mode="broadcast")
    elif action == "list_director_messages":
        args = dict(project_id="beta", needs_my_ack=True)
    else:
        args = dict(project_id="beta", delivery_id="not-a-delivery",
                    expected_version=1, request_key="refused")
    args["protocol_version"] = protocol
    before = snapshot(db)
    refused = call(args, action)
    assert refused["code"] == "invalid_request"
    assert refused["schema"] == "aitelier.director-messaging.v2"
    assert refused["detail"]["message"] == "protocol_version: must be v2 or v3"
    assert "SECRET_DO_NOT_REFLECT" not in json.dumps(refused)
    assert snapshot(db) == before
