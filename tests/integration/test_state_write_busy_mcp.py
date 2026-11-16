"""An MCP `state_graph_write` refused as busy delivers its code and its wait.

THE DEFECT: a State write that cannot take SQLite's write lock inside its budget
is a RETRYABLE refusal, and the HTTP command route answers it as 503 with
`Retry-After`, `code` and `retry_after` (`api/state_http.py`). The MCP transport
answered the same refusal with `isError: true` and no machine-readable code and
no retry hint at all, because `api/state_graph_tools.invoke` caught
`StateGraphError` - and therefore its `StateWriteBusy` subclass - and re-raised
`MCPToolError(str(exc))`: the typed refusal was flattened into prose before any
ASGI middleware could see it. A client could learn "busy" only by reading the
message, and could not tell how long to wait.

What is pinned here, through the REAL MCP transport (`POST /mcp` on the real
State-only app, which mounts `FastMCP.streamable_http_app()`, see
`api/state_only.create_app`) with a real SQLite database in `tmp_path`:

Arm A - a FOREIGN sqlite3 connection holds the write lock past the budget: HTTP
        200 and `isError: true`, and the failed call result carries
        `code == "state_write_busy"` plus the `retry_after` OF THAT REFUSAL in
        `structuredContent`. The expected wait is read off the raised exception,
        and the refusal is built with a hint that differs from the module
        constant, so a hardcoded number cannot satisfy the assertion. Nothing
        was written.
Arm B - no foreign holder: the same action succeeds over the same transport and
        the row it writes is there.
Shape - the values arrive in `structuredContent` of the failed-call result; the
        message text keeps its old prose, leading code included.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.state_only import create_app
from core import state_graph
from core.state_database import StateDatabase
from core.state_service import StateService

TOKEN = "mcp-busy-state-only-token-" + "x" * 40
HEADER = {"Authorization": "Bearer " + TOKEN}
# Distinctive on purpose: NOT `state_graph.STATE_WRITE_BUSY_RETRY_AFTER`, so an
# implementation that echoes the constant instead of the exception fails Arm A.
RETRY_HINT = state_graph.STATE_WRITE_BUSY_RETRY_AFTER + 2


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The real State-only app, over a real State database holding one project."""
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db_path = tmp_path / "state.sqlite"
    StateService(StateDatabase(str(db_path)), actor="fixture",
                 project_read_trusted=True).create_project("alpha", "Alpha")
    return SimpleNamespace(db_path=db_path, app=create_app(str(db_path), TOKEN))


def rpc(client, action, arguments):
    """One real `tools/call` over the streamable-HTTP MCP transport."""
    return client.post(
        "/mcp/",
        headers={**HEADER, "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "state_graph_write",
                         "arguments": {"action": action, "arguments": arguments}}})


def hold_write_lock(world):
    """A FOREIGN sqlite3 connection (not the store's) holding the write lock."""
    holder = sqlite3.connect(str(world.db_path), check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def record_refusals(monkeypatch):
    """Capture the StateWriteBusy instances the store really raised.

    `core.state_graph._begin_write` names the class at call time, so a subclass
    installed here is what the production code raises; it keeps the production
    budget check and only pins the advertised wait to a distinctive value. The
    recorded instance is where Arm A derives the expected `retry_after`, which is
    why implementation and assertion cannot drift apart.
    """
    refusals = []

    class RecordedBusy(state_graph.StateWriteBusy):
        def __init__(self, message, retry_after=RETRY_HINT):
            super().__init__(message, retry_after=retry_after)
            refusals.append(self)

    monkeypatch.setattr(state_graph, "StateWriteBusy", RecordedBusy)
    return refusals


def projects(world):
    with sqlite3.connect(str(world.db_path)) as conn:
        return [row[0] for row in conn.execute("SELECT project_id FROM state_projects")]


def test_a_foreign_lock_past_the_budget_delivers_the_code_and_the_wait(world, monkeypatch):
    """Arm A: isError, machine-readable code, and the refusal's OWN retry_after."""
    monkeypatch.setenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV, "0.4")
    refusals = record_refusals(monkeypatch)
    holder = hold_write_lock(world)
    try:
        with TestClient(world.app) as http:
            response = rpc(http, "create_project", {"project_id": "late", "title": "Late"})
    finally:
        holder.rollback()
        holder.close()

    # The transport still answers 200: MCP reports a failed CALL in the result,
    # not in the HTTP status. This is NOT the HTTP command route.
    assert response.status_code == 200, response.text
    envelope = response.json()["result"]
    assert envelope["isError"] is True, envelope

    # The refusal really happened, came from the production acquisition path,
    # and advertised a wait that is not the module constant.
    assert len(refusals) == 1, refusals
    raised = refusals[0]
    assert isinstance(raised, state_graph.StateGraphError)
    expected_wait = raised.retry_after
    assert expected_wait == RETRY_HINT != state_graph.STATE_WRITE_BUSY_RETRY_AFTER

    delivered = envelope["structuredContent"]
    assert delivered == {"code": "state_write_busy", "retry_after": expected_wait}, envelope
    assert delivered["code"] == state_graph.STATE_WRITE_BUSY_CODE
    # The prose is unchanged, and still names the code.
    assert envelope["content"][0]["type"] == "text"
    assert "state_write_busy" in envelope["content"][0]["text"]

    # A busy write wrote nothing.
    assert "late" not in projects(world)


def test_no_contention_succeeds_and_writes_over_the_same_transport(world, monkeypatch):
    """Arm B: with the lock free the very same call lands."""
    monkeypatch.setenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV, "3")
    with TestClient(world.app) as http:
        response = rpc(http, "create_project", {"project_id": "late", "title": "Late"})

    assert response.status_code == 200, response.text
    envelope = response.json()["result"]
    assert envelope.get("isError") is not True, envelope
    assert json.loads(envelope["content"][0]["text"])["result"]["project_id"] == "late"
    assert "late" in projects(world)
