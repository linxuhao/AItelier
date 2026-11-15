"""The credential guard covers every State writer that persists a free-text body.

core/message_secret_guard.py refuses a body that embeds a configured credential
value. Two writers call it (send_driver_message, send_director_message). This
module covers the other writers that store a body verbatim, each through its
REAL entry point, before any row is written:

  1. send_user_message                  POST /api/state/commands/send_user_message
  2. send_user_conversation_message     POST /api/state/commands/send_user_conversation_message
  3. deliver_notice (P3 notice)         set_node_hold override -> driver_notices.notify
  4. write_entry / supersede_entry      POST /api/state/commands/write_driver_note_entry
                                        POST /api/state/commands/supersede_driver_note_entry

For each refused write: the row counts of the writer's tables, read over an
INDEPENDENT sqlite3 connection, are equal before and after, and the refusal text
names the field class while containing neither the value nor any 8-character
substring of it. Control arms: a body that only NAMES the field, and an unrelated
48-character random string, are stored byte-identical; and one ordinary P3 notice
still delivers. The credential values are synthetic and set by monkeypatch on the
auth modules' own attributes; no real .env is read.
"""
from __future__ import annotations

import random
import sqlite3
import string

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import authz, mcp_router
from api import state_graph_routers as routes
from core.drivers import DriverRegistry
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

ADMIN = "adm-Quartz9-Lantern-2026-secret"
EXTERNAL = "ext-Harbor4-Meridian-7741-token"
NAMING = "Set AITELIER_ADMIN_TOKEN in the env file; never paste it here."
NODE = {"goal": "G", "acceptance": [{"id": "c", "kind": "test", "description": "d"}]}
TABLES = ("driver_inbox_messages", "driver_inbox_deliveries", "driver_inbox_requests",
          "state_driver_note_entries", "state_events")


def random_48(seed):
    rng = random.Random(seed)
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(48))


def snapshot(path):
    """Row counts over an INDEPENDENT connection, never the service's own."""
    conn = sqlite3.connect(path)
    try:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}
    finally:
        conn.close()


def column(path, sql, *args):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def assert_refusal_is_clean(text, field, value):
    """The field class is named; neither the value nor an 8-character piece of it is."""
    assert field in text, text
    assert value not in text, text
    for start in range(len(value) - 7):
        piece = value[start:start + 8]
        assert piece not in text, (field, piece, text)


def service(path, actor, **kwargs):
    return StateService(StateDatabase(path), actor=actor, project_read_trusted=True, **kwargs)


def client_for(svc):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: svc
    return TestClient(app)


def post(client, action, body):
    return client.post(f"/api/state/commands/{action}", json=body)


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setattr(authz, "ADMIN_TOKEN", ADMIN)
    monkeypatch.setattr(mcp_router, "_EXTERNAL_TOKEN", EXTERNAL)


@pytest.fixture
def world(tmp_path, secrets):
    path = str(tmp_path / "state.sqlite")
    seed = service(path, "fixture")
    seed.create_project("alpha", "Alpha")
    registry = DriverRegistry(StateDatabase(path), "message-guard-other-writers-pepper-not-a-secret")
    for did in ("ada", "bob"):
        registry.register(did, did, actor="test")
        registry.set_membership("alpha", did, "member", 0, "fixture", actor="test")
    return path


@pytest.fixture
def p3_world(tmp_path, secrets):
    """Project p with nodes a, b, c, and two registered drivers, for the P3 notice path."""
    path = str(tmp_path / "p3.sqlite")
    owner = service(path, "authorized-state-operator", is_admin=True)
    owner.create_project("p", "P")
    owner.store.add_nodes("p", [{"key": "a", **NODE}, {"key": "b", **NODE},
                                {"key": "c", **NODE, "dependencies": ["a"]}])
    # Releasing another driver's hold notifies the placer only under enforcement
    # (core/state_enforcement.py hold_release_check), so the project is enforced.
    execute(owner, "set_claim_enforcement", {"project_id": "p", "claim_enforcement": "on",
                                             "expected_revision": 0, "reason": "P3 guard test"},
            allow_write=True)
    registry = DriverRegistry(StateDatabase(path), "p3-guard-pepper-not-a-secret-padded-to-32")
    for who in ("codex", "grok"):
        registry.register(who, who.title(), actor="t")
    return path


def hold(svc_driver, node):
    return execute(svc_driver, "set_node_hold",
                   {"project_id": "p", "node_key": node, "held": True, "expected_revision": 0,
                    "reason": "wait"}, allow_write=True)


def release(svc_driver, node, override_reason):
    return execute(svc_driver, "set_node_hold",
                   {"project_id": "p", "node_key": node, "held": False, "expected_revision": 1,
                    "reason": "go", "override_reason": override_reason}, allow_write=True)


# --------------------------------------------------------------- 1. send_user_message

def test_send_user_message_refuses_a_credential_value_before_any_row(world):
    ada = service(world, "driver:ada", driver_id="ada")
    with client_for(ada) as client:
        for label, value in (("admin", ADMIN), ("external", EXTERNAL)):
            before = snapshot(world)
            field = "AITELIER_ADMIN_TOKEN" if label == "admin" else "AITELIER_MCP_EXTERNAL_TOKEN"
            response = post(client, "send_user_message", {
                "request_key": f"su-{label}", "project_id": "alpha", "subject": "Progress",
                "body": f"the token is {value} please keep it"})
            assert response.status_code == 422, response.text
            assert_refusal_is_clean(response.text, field, value)
            assert snapshot(world) == before


def test_send_user_message_control_arms_are_stored_byte_identical(world):
    ada = service(world, "driver:ada", driver_id="ada")
    unrelated = random_48(1115)
    with client_for(ada) as client:
        for index, body in enumerate((NAMING, f"Build ref {unrelated} is the artifact digest.")):
            response = post(client, "send_user_message", {
                "request_key": f"su-ctl-{index}", "project_id": "alpha", "subject": "Note", "body": body})
            assert response.status_code == 200, response.text
    stored = column(world, "SELECT body FROM driver_inbox_messages WHERE audience='user'")
    assert NAMING in stored and f"Build ref {unrelated} is the artifact digest." in stored, stored


# ------------------------------------------------- 2. send_user_conversation_message

def test_send_user_conversation_message_refuses_a_credential_value_before_any_row(world):
    owner = service(world, "owner:user@example.test")
    with client_for(owner) as client:
        for label, value, field in (("admin", ADMIN, "AITELIER_ADMIN_TOKEN"),
                                    ("external", EXTERNAL, "AITELIER_MCP_EXTERNAL_TOKEN")):
            before = snapshot(world)
            response = post(client, "send_user_conversation_message", {
                "request_key": f"uc-{label}", "project_id": "alpha",
                "body": f"use {value} to log in"})
            assert response.status_code == 422, response.text
            assert_refusal_is_clean(response.text, field, value)
            assert snapshot(world) == before


def test_send_user_conversation_message_control_arms_are_stored_byte_identical(world):
    owner = service(world, "owner:user@example.test")
    unrelated = random_48(2115)
    with client_for(owner) as client:
        for index, body in enumerate((NAMING, f"Ref {unrelated} is the build digest.")):
            response = post(client, "send_user_conversation_message", {
                "request_key": f"uc-ctl-{index}", "project_id": "alpha", "body": body})
            assert response.status_code == 200, response.text
    stored = column(world, "SELECT body FROM driver_inbox_messages WHERE audience='driver'")
    assert NAMING in stored and f"Ref {unrelated} is the build digest." in stored, stored


# ------------------------------------------------------------- 3. deliver_notice (P3)

def test_p3_notice_refused_through_hold_release_leaves_no_row_and_no_release(p3_world):
    codex = service(p3_world, "driver:codex", driver_id="codex")
    grok = service(p3_world, "driver:grok", driver_id="grok")
    hold(codex, "b")
    before = snapshot(p3_world)
    with pytest.raises(StateGraphError) as caught:
        release(grok, "b", f"the token is {ADMIN} please keep it")
    assert_refusal_is_clean(str(caught.value), "AITELIER_ADMIN_TOKEN", ADMIN)
    assert snapshot(p3_world) == before
    # The refused release did not happen: an ordinary release of the same hold still
    # goes through, at the revision the refusal left in place.
    release(grok, "b", "owner asked")
    assert column(p3_world, "SELECT body FROM driver_inbox_messages WHERE kind='lease_notice'") == [
        "Reason: go. override_reason: owner asked"]


def test_p3_notice_control_arms_are_stored_byte_identical_and_ordinary_notice_delivers(p3_world):
    codex = service(p3_world, "driver:codex", driver_id="codex")
    grok = service(p3_world, "driver:grok", driver_id="grok")
    unrelated = random_48(3115)
    for node, override in (("a", NAMING), ("c", f"Ref {unrelated} is the build digest.")):
        hold(codex, node)
        release(grok, node, override)
    bodies = column(p3_world, "SELECT body FROM driver_inbox_messages WHERE kind='lease_notice' ORDER BY created_at")
    assert f"Reason: go. override_reason: {NAMING}" in bodies, bodies
    assert f"Reason: go. override_reason: Ref {unrelated} is the build digest." in bodies, bodies
    # One ordinary P3 notice still delivers, to the placer's inbox with one delivery.
    hold(codex, "b")
    release(grok, "b", "owner asked")
    delivered = column(p3_world, "SELECT d.target_driver_id FROM driver_inbox_deliveries d "
                                 "JOIN driver_inbox_messages m USING(message_id) "
                                 "WHERE m.body='Reason: go. override_reason: owner asked'")
    assert delivered == ["codex"], delivered


# ------------------------------------------------- 4. write_entry / supersede_entry

def test_write_driver_note_entry_refuses_a_credential_value_before_any_row(world):
    owner = service(world, "owner:user@example.test")
    with client_for(owner) as client:
        for label, value, field in (("admin", ADMIN, "AITELIER_ADMIN_TOKEN"),
                                    ("external", EXTERNAL, "AITELIER_MCP_EXTERNAL_TOKEN")):
            before = snapshot(world)
            response = post(client, "write_driver_note_entry", {
                "project_id": "alpha", "assertion": f"Rule {label}", "body": f"the token is {value}",
                "director_identity": "aitelier-director"})
            assert response.status_code == 422, response.text
            assert_refusal_is_clean(response.text, field, value)
            assert snapshot(world) == before


def test_supersede_driver_note_entry_refuses_a_credential_value_and_keeps_the_original(world):
    owner = service(world, "owner:user@example.test")
    with client_for(owner) as client:
        first = post(client, "write_driver_note_entry", {
            "project_id": "alpha", "assertion": "Rule one", "body": "clean body",
            "director_identity": "aitelier-director"})
        assert first.status_code == 200, first.text
        entry_id = column(world, "SELECT entry_id FROM state_driver_note_entries")[0]
        before = snapshot(world)
        response = post(client, "supersede_driver_note_entry", {
            "project_id": "alpha", "entry_id": entry_id, "assertion": "Rule two",
            "body": f"the token is {ADMIN}", "reason": "replaced", "director_identity": "aitelier-director"})
        assert response.status_code == 422, response.text
        assert_refusal_is_clean(response.text, "AITELIER_ADMIN_TOKEN", ADMIN)
        assert snapshot(world) == before
        assert column(world, "SELECT superseded_by FROM state_driver_note_entries WHERE entry_id=?",
                      entry_id) == [None]


def test_notebook_control_arms_are_stored_byte_identical(world):
    owner = service(world, "owner:user@example.test")
    unrelated = random_48(4115)
    with client_for(owner) as client:
        for index, body in enumerate((NAMING, f"Ref {unrelated} is the build digest.")):
            response = post(client, "write_driver_note_entry", {
                "project_id": "alpha", "assertion": f"Control {index}", "body": body,
                "director_identity": "aitelier-director"})
            assert response.status_code == 200, response.text
        first = column(world, "SELECT entry_id FROM state_driver_note_entries WHERE body=?", NAMING)[0]
        superseded = post(client, "supersede_driver_note_entry", {
            "project_id": "alpha", "entry_id": first, "assertion": "Control successor",
            "body": NAMING, "reason": "replaced", "director_identity": "aitelier-director"})
        assert superseded.status_code == 200, superseded.text
    stored = column(world, "SELECT body FROM state_driver_note_entries")
    assert stored.count(NAMING) == 2, stored
    assert f"Ref {unrelated} is the build digest." in stored, stored
