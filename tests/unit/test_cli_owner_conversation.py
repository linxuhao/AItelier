"""The local CLI's own credential is the owner's terminal for the manager conversation.

Owner ruling (2026-11-18, verbatim): "CLI 不是固定 cli owner吗， 保持这个权限， 然后接上管家对话"
- the CLI already IS the fixed cli owner; keep that permission, then connect the
manager (butler) conversation. The CLI presents exactly what it presented before
(`core.driver_credentials.auth_headers`: a per-driver token when one is
configured, else the env `AITELIER_ADMIN_TOKEN`); no new token, env var or flag
was invented. What changed is the IDENTITY the env admin token is minted as:
`kind='cli_owner'`, actor `cli-owner`, no driver row (api.authz.request_identity),
and the two conversation doors admit it (core.driver_inbox.DriverInbox._project_user).

Every arm below runs on the REAL request path — the product State router and the
product steward router, the real `authz` verdicts (gate ON), the real
`api.state_graph_routers.get_service`, and a real drivers registry over a temp
database whose `owner-cli` row is seeded from the same env token production
seeds it from. Nothing here overrides an authorization dependency.

Four arms:

(a) the admin-token credential WRITES the user's half of a project's conversation
    and ADVANCES the read marker;
(b) a registered driver token is still refused - the driver writes the other half;
(c) the owner-only steward decision is still refused for the CLI credential;
(d) the feature-off path (no driver registry) is measured unchanged.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from api import authz
from core.drivers import DriverRegistry
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService
from tests.support import state_template

ADMIN = "cli-owner-admin-token-not-a-secret"
PEPPER = "c" * 48
OWNER = "owner@example.test"
SEND = "/api/state/commands/send_user_conversation_message"
MARK = "/api/state/commands/mark_user_conversation_read"


def _request(headers):
    """A request-shaped object: `request_identity` reads the raw credential only."""
    return type("Request", (), {"headers": Headers(headers), "cookies": {}})()


def _cli():
    """What the CLI presents today: its own credential in the admin-token header."""
    return {"X-AItelier-Admin-Token": ADMIN}


@pytest.fixture
def world(tmp_path, monkeypatch, state_db_copy):
    db = StateDatabase(state_db_copy(state_template.state_registry, tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    registry = DriverRegistry(db, PEPPER)
    # Seeded from the same env token production seeds it from: the CLI's own
    # credential is what `lookup_token` resolves, and its row (active/suspended,
    # rotated or not) stays the thing that governs it.
    registry.seed(ADMIN)
    token = registry.register("codex", "Codex", actor="test")["token"]
    registry.set_membership("alpha", "codex", "member", 0, "fixture", actor="test")
    # The driver's own half, so there is a message for the read marker to advance over.
    said = execute(StateService(db, actor="driver:codex", driver_id="codex",
                                project_read_trusted=True),
                   "send_user_message",
                   {"request_key": "hello", "project_id": "alpha", "subject": "Progress",
                    "body": "Half the nodes are done."}, allow_write=True)
    # The request path with its real verdicts: gate ON, the registry ON, the
    # CLI's env admin token, and the owner allowlist an Access email would use.
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "ADMIN_TOKEN", ADMIN)
    monkeypatch.setattr(authz, "WRITERS", {OWNER})
    monkeypatch.setattr(authz, "driver_registry", lambda: registry)
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers",
                        lambda headers, cookies=None: headers.get("X-Test-Email"))
    return {"db": db, "registry": registry, "codex": token, "said": said}


def app_for(world):
    """The product routers over a temp DB: nothing HERE decides authorization."""
    from api.dependencies import get_db_manager, get_workspace_manager
    from api.dsh_driver_routers import manager as driver_manager
    from api.state_graph_routers import router as state_router
    from api.steward_routers import router as steward_router

    app = FastAPI()
    app.include_router(state_router)
    app.include_router(steward_router)
    app.dependency_overrides[get_db_manager] = lambda: world["db"]
    app.dependency_overrides[get_workspace_manager] = lambda: None
    # The steward's approve route also resolves the DSH driver manager, which
    # would provision a real runtime. The refusal under test happens before the
    # manager is used, so a stand-in keeps this test off the host's processes.
    app.dependency_overrides[driver_manager] = lambda: None
    return app


def rows(world, sql, *args):
    conn = sqlite3.connect(world["db"].db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ------------------------------------------------- (a) the CLI is the owner's terminal

def test_the_env_admin_token_is_minted_as_the_owners_terminal(world):
    """The identity itself: a distinct kind+actor, and no driver row behind it."""
    identity = authz.request_identity(_request(_cli()))
    assert identity is not None
    assert (identity.kind, identity.actor, identity.driver_id) == ("cli_owner", "cli-owner", None)
    # It keeps the owner-cli row's admin permission (break-glass/admin routes).
    assert identity.is_admin is True
    # And it is NOT the old driver identity the same credential used to be.
    assert identity.actor != "driver:owner-cli"


def test_the_admin_token_credential_writes_the_users_half_and_advances_the_marker(world):
    with TestClient(app_for(world)) as client:
        # The CLI's own credential, exactly as it presents it today.
        sent = client.post(SEND, headers=_cli(), json={
            "request_key": "u1", "project_id": "alpha", "subject": "node a",
            "body": "Please look at node a first."})
        assert sent.status_code == 200, sent.text
        written = sent.json()
        assert written["sender_kind"] == "user" and written["project_id"] == "alpha"
        assert [d["target_driver_id"] for d in written["deliveries"]] == ["codex"]

        # One thread, both directions, read back through the same credential.
        page = client.post("/api/state/query/list_user_conversation", headers=_cli(),
                           json={"project_id": "alpha"}).json()
        assert {(m["sender_kind"], m["body"]) for m in page["messages"]} == {
            ("user", "Please look at node a first."), ("driver", "Half the nodes are done.")}
        assert page["unread_count"] == 1 and page["read_upto_message_id"] is None

        # The read marker advances to the message the terminal was shown.
        marked = client.post(MARK, headers=_cli(),
                             json={"project_id": "alpha",
                                   "message_id": world["said"]["message_id"]})
        assert marked.status_code == 200, marked.text
        assert marked.json()["advanced"] is True
        assert marked.json()["read_upto_message_id"] == world["said"]["message_id"]
        assert marked.json()["unread_count"] == 0
        # ...and keeps advancing to a NEWER row (the terminal's own message).
        again = client.post(MARK, headers=_cli(), json={
            "project_id": "alpha", "message_id": written["message_id"]})
        assert again.status_code == 200, again.text
        assert again.json()["advanced"] is True
        assert again.json()["read_upto_message_id"] == written["message_id"]
        assert again.json()["unread_count"] == 0

        badge = client.post("/api/state/query/user_conversation_unread", headers=_cli(),
                            json={"project_id": "alpha"}).json()
        assert badge["projects"] == {"alpha": 0} and badge["total"] == 0

    # Persisted rows, not merely answered: the user's half is a real conversation row.
    assert rows(world, "SELECT audience,channel,sender_driver_id,body FROM driver_inbox_messages "
                       "WHERE message_id=?", written["message_id"]) == [
        ("driver", "user", None, "Please look at node a first.")]
    assert rows(world, "SELECT read_message_id FROM driver_user_conversation_reads "
                       "WHERE project_id='alpha'") == [(written["message_id"],)]

def test_the_owner_cli_row_is_still_the_thing_that_governs_the_credential(world):
    """No new revocable-token mechanism: suspending `owner-cli` still revokes it."""
    world["registry"].set_status("owner-cli", "suspended", 1, "pause", actor="test")
    assert authz.request_identity(_request(_cli())) is None
    with TestClient(app_for(world)) as client:
        refused = client.post(SEND, headers=_cli(), json={
            "request_key": "u1", "project_id": "alpha", "body": "not allowed"})
        assert refused.status_code == 403
        # The write gate's own wording: an unrecognised token, not a missing one.
        assert refused.json()["detail"] == authz.DENIAL_MESSAGES[authz.WRITE_DENIED_BAD_ADMIN_TOKEN]


# ------------------------------------------- (b) a driver token still cannot write it

def test_a_registered_driver_token_is_still_refused(world):
    driver = {"X-AItelier-Driver-Token": world["codex"]}
    with TestClient(app_for(world)) as client:
        refused = client.post(SEND, headers=driver, json={
            "request_key": "d1", "project_id": "alpha", "body": "not mine to write"})
        # The write gate ADMITS the driver (it is a registered member); the
        # conversation door is what refuses, and on this route a StateGraphError
        # travels as a 422 whose detail is the code (api.state_http._call).
        assert refused.status_code == 422
        assert refused.json()["detail"] == "user_identity_required"
        marked = client.post(MARK, headers=driver, json={
            "project_id": "alpha", "message_id": world["said"]["message_id"]})
        assert marked.status_code == 422
        assert marked.json()["detail"] == "user_identity_required"
    # Nothing was written by the refused attempts.
    assert rows(world, "SELECT COUNT(*) FROM driver_inbox_messages") == [(1,)]
    assert rows(world, "SELECT COUNT(*) FROM driver_user_conversation_reads") == [(0,)]


# ------------------------------------- (c) the owner-only steward decision stays shut

def test_the_owner_only_steward_decision_still_refuses_the_cli_credential(world):
    with TestClient(app_for(world)) as client:
        refused = client.post("/api/steward/proposals/proposal-x/approve",
                              headers=_cli(), json={"expected_version": 1})
        assert refused.status_code == 403
        assert refused.json()["detail"] == "owner_session_required"
        # ...and the same route is not simply shut: the owner's BROWSER session
        # passes this door and gets to the (absent) proposal itself.
        owner = client.post("/api/steward/proposals/proposal-x/approve",
                            headers={"X-Test-Email": OWNER}, json={"expected_version": 1})
        assert owner.status_code == 404


# ------------------------------------------------ (d) feature off keeps today's path

def test_feature_off_is_unchanged(world, monkeypatch):
    """No registry: the CLI is the shared legacy actor and is refused, as before."""
    monkeypatch.setattr(authz, "driver_registry", lambda: None)
    identity = authz.request_identity(_request(_cli()))
    assert identity is not None
    assert (identity.kind, identity.actor, identity.driver_id) == (
        "legacy", "authorized-state-operator", None)
    with TestClient(app_for(world)) as client:
        refused = client.post(SEND, headers=_cli(), json={
            "request_key": "u1", "project_id": "alpha", "body": "still refused"})
        assert refused.status_code == 422
        assert refused.json()["detail"] == "user_identity_required"
    assert rows(world, "SELECT COUNT(*) FROM driver_inbox_messages") == [(1,)]
