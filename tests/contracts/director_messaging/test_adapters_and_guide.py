import json

import pytest

from api import authz, mcp_router
from api.state_graph_tools import register_state_tools
from core.state_commands import READ_REQUESTS, WRITE_REQUESTS, describe, execute
from core.state_database import StateDatabase
from core.state_driver_guide import STATE_DRIVER_GUIDE
from core.state_service import StateService


def _service(tmp_path, actor="transport@example.test"):
    service = StateService(StateDatabase(str(tmp_path / "state.sqlite")), actor=actor)
    service.create_project("alpha", "alpha")
    service.create_project("beta", "beta")
    return service


def test_state_command_vocabulary_and_closed_envelopes(tmp_path):
    service = _service(tmp_path)
    assert "list_director_messages" in READ_REQUESTS
    for action in ("send_director_message", "acknowledge_director_message",
                   "resolve_director_message"):
        assert action in WRITE_REQUESTS
    operations = describe()["operations"]
    assert operations["list_director_messages"]["mutates"] is False
    assert operations["send_director_message"]["mutates"] is True
    invalid = execute(service, "send_director_message", {"sender_project_id": "alpha"},
                      allow_write=True)
    assert invalid == {"schema": "aitelier.director-messaging.v1",
                       "code": "invalid_request", "detail": {"message": "invalid_request"}}
    sent = execute(service, "send_director_message", {
        "sender_project_id": "alpha", "director_identity": "director", "request_key": "one",
        "subject": "subject", "body": "", "target_project_id": "beta"}, allow_write=True)
    assert set(sent) == {"schema", "result"}


@pytest.mark.asyncio
async def test_mcp_adapter_returns_same_inner_envelope(tmp_path):
    service = _service(tmp_path)
    registered = {}

    def tool(name, kind, description):
        def decorate(fn):
            registered[name] = fn
            return fn
        return decorate

    class MCP:
        def prompt(self, **kwargs):
            return lambda fn: fn
        def resource(self, *args, **kwargs):
            return lambda fn: fn

    register_state_tools(tool, MCP(), service_factory=lambda: service)
    sent = registered["state_graph_write"]("send_director_message", {
        "sender_project_id": "alpha", "director_identity": "director", "request_key": "one",
        "subject": "subject", "body": "", "target_project_id": "beta"})
    assert set(sent["result"]) == {"schema", "result"}
    listed = await registered["state_graph_read"]("list_director_messages", {"project_id": "beta"})
    assert [item["delivery"]["delivery_seq"] for item in
            listed["result"]["result"]["items"]] == [1]
    assert listed["result"]["result"]["items"][0]["message"]["message_id"] == \
           sent["result"]["result"]["message"]["message_id"]


@pytest.mark.asyncio
async def test_mcp_direct_action_authorization_returns_closed_envelope(monkeypatch):
    class MCP:
        @staticmethod
        def get_context():
            return object()

    def denied(name, context):
        raise mcp_router.ToolDenied("denied")

    monkeypatch.setattr(mcp_router, "_authorize", denied)

    def action(action, arguments):
        raise AssertionError("authorization must run before the action")

    wrapped = mcp_router._wrap(MCP(), action, "state_graph_write")
    result = await wrapped(action="send_director_message", arguments={"malformed": object()})
    assert result == {"result": {
        "schema": "aitelier.director-messaging.v1", "code": "unauthorized",
        "detail": {"message": "unauthorized"}}}


def test_rest_writer_gate_precedes_body_parse_and_returns_closed_envelope(client, monkeypatch):
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "write_denial_reason",
                        lambda request: authz.WRITE_DENIED_NOT_AUTHENTICATED)
    client.app.state._test_mode = False
    response = client.post(
        "/api/state/director-messages/send_director_message",
        content=b"not-json", headers={"Content-Type": "application/json"})
    assert response.status_code == 403
    assert response.json() == {
        "schema": "aitelier.director-messaging.v1", "code": "unauthorized",
        "detail": {"message": "unauthorized"}}


def test_rest_direct_actions_return_closed_success_and_invalid_request(client):
    for project_id in ("alpha", "beta"):
        assert client.post("/api/state/commands/create_project", json={
            "project_id": project_id, "title": project_id}).status_code == 200
    sent = client.post("/api/state/director-messages/send_director_message", json={
        "sender_project_id": "alpha", "director_identity": "director",
        "request_key": "rest-one", "subject": "subject", "body": "",
        "target_project_id": "beta"})
    assert sent.status_code == 200
    assert set(sent.json()) == {"schema", "result"}
    invalid = client.post("/api/state/director-messages/send_director_message",
                          content=b"not-json", headers={"Content-Type": "application/json"})
    assert invalid.status_code == 200
    assert invalid.json() == {
        "schema": "aitelier.director-messaging.v1", "code": "invalid_request",
        "detail": {"message": "invalid_request"}}


def test_mcp_wire_exposes_direct_action_with_closed_envelope(client, tmp_path, monkeypatch):
    from api import dependencies
    db = StateDatabase(str(tmp_path / "wire.sqlite"))
    service = StateService(db)
    service.create_project("alpha", "alpha")
    service.create_project("beta", "beta")
    monkeypatch.setattr(dependencies, "get_db_manager", lambda: db)
    monkeypatch.setattr(dependencies, "get_workspace_manager", lambda: None)
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda request: True)
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    initialized = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "director-proof", "version": "1"}}}, headers=headers)
    assert initialized.status_code == 200
    response = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "state_graph_write", "arguments": {
                "action": "send_director_message", "arguments": {
                    "sender_project_id": "alpha", "director_identity": "director",
                    "request_key": "mcp-wire", "subject": "subject", "body": "",
                    "target_project_id": "beta"}}}}, headers=headers)
    result = response.json()["result"]
    assert result.get("isError") is not True
    envelope = json.loads(result["content"][0]["text"])["result"]
    assert set(envelope) == {"schema", "result"}
    assert envelope["result"]["message"]["actor"] == "authorized-state-operator"


def test_mcp_wire_authorizes_before_parsing_director_action_body(client, monkeypatch):
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda request: False)
    monkeypatch.setattr(mcp_router.authz, "write_denial_reason",
                        lambda request: authz.WRITE_DENIED_NOT_AUTHENTICATED)
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    assert client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "director-proof", "version": "1"}}},
        headers=headers).status_code == 200
    response = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "state_graph_write", "arguments": {
                "action": "send_director_message", "arguments": {"malformed": True}}}},
        headers=headers)
    result = response.json()["result"]
    assert result.get("isError") is not True
    assert json.loads(result["content"][0]["text"])["result"] == {
        "schema": "aitelier.director-messaging.v1", "code": "unauthorized",
        "detail": {"message": "unauthorized"}}


def test_guide_is_agent_neutral_and_documents_messaging():
    assert "send_director_message" in STATE_DRIVER_GUIDE
    assert "director_message_received" in STATE_DRIVER_GUIDE
    assert "/api/state/director-messages/<action>" in STATE_DRIVER_GUIDE
    assert "## Transport examples" in STATE_DRIVER_GUIDE
    normative, examples = STATE_DRIVER_GUIDE.split("## Transport examples", 1)
    for product in ("Codex", "Claude", "AItelier"):
        assert product not in normative
        assert product in examples
