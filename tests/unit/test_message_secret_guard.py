"""A message body may not carry this service's own credential values.

Message bodies are persisted in State and delivered to inboxes, so a body that
embeds the value of a configured credential would be copied to every recipient
and kept. One shared check (core/message_secret_guard.py) runs on both write
paths before any row is written.

C1  Through the application's own route (the real `/api/state` router, with its
    router guard), a body embedding the admin token or the MCP external token is
    refused on BOTH paths. Message and delivery row counts, read over an
    independent connection, are equal before and after. The refusal names the
    field class and contains neither the value nor any 8+ character substring of
    it. Control arm: a body that only NAMES the field, and a body with an
    unrelated long random string, are delivered and stored byte-identical.

C2  The secret set is derived, not copied: no literal credential value in the
    guard, and the set is read from the auth modules' own attributes. Replacing
    the configured value (same length) makes the old body accepted and a body
    with the NEW value refused. A long value from an unrelated configuration
    item is accepted.
"""
from __future__ import annotations

import inspect
import random
import re
import sqlite3
import string
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import authz, mcp_router
from api import state_graph_routers as routes
from core import message_secret_guard as guard
from core.drivers import DriverRegistry
from core.state_database import StateDatabase
from core.state_service import StateService

ADMIN = "adm-Quartz9-Lantern-2026-secret"
EXTERNAL = "ext-Harbor4-Meridian-7741-token"
# Same length as ADMIN (31 characters): the C2 replacement must not change length.
ADMIN_REPLACEMENT = "rep-Zephyr3-Obelisk-50123-token"
assert len(ADMIN) == len(ADMIN_REPLACEMENT) == 31


# --------------------------------------------------------------------------- fixtures

@pytest.fixture
def world(tmp_path, monkeypatch):
    """A real State database, two registered drivers in project alpha, and a
    FastAPI app that mounts the application's own state router."""
    path = str(tmp_path / "state.sqlite")
    db = StateDatabase(path)
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    seed.create_project("beta", "Beta")
    registry = DriverRegistry(db, "message-secret-guard-pepper-not-a-secret")
    for did in ("ada", "bob"):
        registry.register(did, did, actor="test")
        registry.set_membership("alpha", did, "member", 0, "fixture", actor="test")
    # beta has a member, so a valid project-mailbox send to it is deliverable.
    registry.set_membership("beta", "bob", "member", 0, "fixture", actor="test")
    ada = StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=True)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: ada
    # Admin token and external token are the real auth-module attributes.
    monkeypatch.setattr(authz, "ADMIN_TOKEN", ADMIN)
    monkeypatch.setattr(mcp_router, "_EXTERNAL_TOKEN", EXTERNAL)
    return {"path": path, "app": app}


def counts(path):
    """Row counts over an INDEPENDENT connection, not the service's own."""
    conn = sqlite3.connect(path)
    try:
        return {
            "messages": conn.execute("SELECT COUNT(*) FROM state_director_messages").fetchone()[0],
            "deliveries": conn.execute("SELECT COUNT(*) FROM state_director_deliveries").fetchone()[0],
            "driver_messages": conn.execute("SELECT COUNT(*) FROM driver_inbox_messages").fetchone()[0],
            "driver_deliveries": conn.execute("SELECT COUNT(*) FROM driver_inbox_deliveries").fetchone()[0],
        }
    finally:
        conn.close()


def stored_driver_bodies(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT body FROM driver_inbox_messages")]
    finally:
        conn.close()


def stored_director_bodies(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT body FROM state_director_messages")]
    finally:
        conn.close()


def send_driver(client, body, key):
    return client.post("/api/state/commands/send_driver_message", json={
        "request_key": key, "subject": "Status", "body": body, "target_driver_id": "bob"})


def send_director(client, body, key):
    # Target beta, not the sender's own project: a self-target is refused by the
    # existing sender==target rule before the body is ever read, which would hide
    # whether the guard ran.
    return client.post("/api/state/director-messages/send_director_message", json={
        "sender_project_id": "alpha", "director_identity": "ada", "request_key": key,
        "subject": "Status", "body": body, "target_project_id": "beta"})


def _substrings(value, n=8):
    return {value[i:i + n] for i in range(len(value) - n + 1)}


# --------------------------------------------------------------------------- C1


def test_c1_secret_values_refused_on_both_paths_through_the_route(world):
    """Admin and MCP external token, each embedded, on the driver path and on the
    project mailbox path: refused by the application's own route, no row written."""
    with TestClient(world["app"]) as client:
        before = counts(world["path"])
        for label, value in (("admin", ADMIN), ("external", EXTERNAL)):
            body = f"the token is {value} please keep it"
            driver = send_driver(client, body, f"c1-driver-{label}")
            director = send_director(client, body, f"c1-director-{label}")
            assert driver.status_code == 422, driver.text
            assert "AITELIER_ADMIN_TOKEN" in driver.text or "AITELIER_MCP_EXTERNAL_TOKEN" in driver.text
            # The director path answers its closed envelope, not an HTTP status.
            assert director.status_code == 200, director.text
            envelope = director.json()
            assert envelope["code"] == "invalid_request", envelope
            assert "AITELIER_ADMIN_TOKEN" in envelope["detail"]["message"] \
                or "AITELIER_MCP_EXTERNAL_TOKEN" in envelope["detail"]["message"], envelope
        after = counts(world["path"])
    assert before == after, (before, after)


def test_c1_refusal_text_contains_no_value_and_no_eight_char_substring(world):
    """The caller sees the field class and the remedy, and nothing derived from the
    value."""
    with TestClient(world["app"]) as client:
        for value, field in ((ADMIN, "AITELIER_ADMIN_TOKEN"),
                             (EXTERNAL, "AITELIER_MCP_EXTERNAL_TOKEN")):
            response = send_driver(client, f"x {value} y", f"c1-text-{field}")
            text = response.text
            assert field in text, text
            assert value not in text, text
            for piece in _substrings(value):
                assert piece not in text, (field, piece)
            director_text = str(send_director(client, f"x {value} y", f"c1-dtext-{field}").json())
            assert value not in director_text, director_text
            for piece in _substrings(value):
                assert piece not in director_text, (field, piece)


def test_c1_control_arm_named_field_and_random_string_are_stored_byte_identical(world):
    """A body that only NAMES the field, and a body with an unrelated long random
    string, are delivered unchanged. The stored body is byte-identical."""
    rng = random.Random(1114)
    random_blob = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(48))
    named_only = "Set AITELIER_ADMIN_TOKEN in the env file; never paste it here."
    unrelated = f"Build ref {random_blob} is the new artifact digest."
    with TestClient(world["app"]) as client:
        for index, body in enumerate((named_only, unrelated)):
            driver = send_driver(client, body, f"c1-ctl-driver-{index}")
            assert driver.status_code == 200, driver.text
            director = send_director(client, body, f"c1-ctl-director-{index}")
            assert director.status_code == 200, director.text
            # A delivered director send answers the closed success envelope.
            assert set(director.json()) == {"schema", "result"}, director.json()
    stored_driver = stored_driver_bodies(world["path"])
    stored_director = stored_director_bodies(world["path"])
    assert named_only in stored_driver and unrelated in stored_driver, stored_driver
    assert named_only in stored_director and unrelated in stored_director, stored_director


def test_c1_refused_body_leaves_no_half_written_rows_on_either_path(world):
    """The row counts are equal before and after a refusal on each path; a valid
    send after the refusal still writes exactly its own rows."""
    with TestClient(world["app"]) as client:
        baseline = counts(world["path"])
        send_driver(client, f"leak {ADMIN}", "c1-half-driver")
        send_director(client, f"leak {EXTERNAL}", "c1-half-director")
        assert counts(world["path"]) == baseline
        ok = send_driver(client, "a clean status line", "c1-half-ok")
        assert ok.status_code == 200, ok.text
        assert counts(world["path"])["driver_messages"] == baseline["driver_messages"] + 1


# --------------------------------------------------------------------------- C2


def test_c2_no_literal_credential_value_in_the_guard():
    """Grep scope: core/message_secret_guard.py and both call sites. The only
    string literals the guard may hold are the field-class names and the
    attribute names it reads. No literal credential value appears in code, and
    no long quoted string appears anywhere in the guard."""
    root = Path(guard.__file__).parent
    for name in ("message_secret_guard.py", "director_messaging.py", "driver_inbox.py"):
        source = (root / name).read_text()
        assert ADMIN not in source and EXTERNAL not in source, name
    guard_source = Path(guard.__file__).read_text()
    # A credential-shaped literal is one unbroken run of 20+ token characters that
    # mixes letters and digits. Prose (spaces) and the field-class names (letters,
    # underscores only) are not credential-shaped, so they do not match.
    token_like = re.findall(r"[\"']([A-Za-z0-9_\-]{20,})[\"']", guard_source)
    token_like = [t for t in token_like
                  if re.search(r"\d", t) and re.search(r"[A-Za-z]", t)]
    assert token_like == [], token_like


def test_c2_the_set_is_read_from_the_auth_modules_at_call_time(world, monkeypatch):
    """Replace the configured value (same length): the old body is now accepted and
    a body with the NEW value is refused."""
    with TestClient(world["app"]) as client:
        assert send_driver(client, f"old {ADMIN} tail", "c2-before").status_code == 422
        # Experiment: replace the module constant the auth code compares against.
        monkeypatch.setattr(authz, "ADMIN_TOKEN", ADMIN_REPLACEMENT)
        assert send_driver(client, f"old {ADMIN} tail", "c2-old-after").status_code == 200
        refused = send_driver(client, f"new {ADMIN_REPLACEMENT} tail", "c2-new-after")
        assert refused.status_code == 422, refused.text
        assert "AITELIER_ADMIN_TOKEN" in refused.text


def test_c2_reverse_control_long_unrelated_value_is_accepted(world, monkeypatch):
    """A long value from an unrelated configuration item, written into a body, is
    accepted. Without this the test would only prove that long strings are refused."""
    unrelated_long = "unrelated-long-setting-value-Quill8-Harbor"
    monkeypatch.setenv("AITELIER_UNRELATED_SETTING", unrelated_long)
    with TestClient(world["app"]) as client:
        response = send_driver(client, f"config {unrelated_long} recorded", "c2-reverse")
        assert response.status_code == 200, response.text


def test_c2_floor_refuses_nothing_for_a_short_or_empty_setting(world, monkeypatch):
    """Below the floor a configured value is not a secret, so a short or empty
    setting cannot refuse ordinary text."""
    assert guard.MIN_SECRET_LEN == 8
    for short in ("", "x", "abcdefg"):
        monkeypatch.setattr(authz, "ADMIN_TOKEN", short)
        monkeypatch.setattr(mcp_router, "_EXTERNAL_TOKEN", "")
        with TestClient(world["app"]) as client:
            response = send_driver(client, "ordinary prose with x and abcdefg in it", f"c2-floor-{len(short)}")
            assert response.status_code == 200, (short, response.text)


def test_c2_guard_is_defined_once_and_called_from_both_paths():
    """One definition of the check; one call site per write path."""
    root = Path(guard.__file__).parent
    defs = [p.name for p in root.glob("*.py") if re.search(r"^def refuse_credential_values\b", p.read_text(), re.M)]
    assert defs == ["message_secret_guard.py"], defs
    for name in ("director_messaging.py", "driver_inbox.py"):
        calls = re.findall(r"\brefuse_credential_values\(", (root / name).read_text())
        assert len(calls) == 1, (name, len(calls))
    assert "def _configured_values" in inspect.getsource(guard)


def test_docstring_states_the_measured_coverage_boundary():
    """The module docstring carries the three statements: which credentials are
    checked, which are not and why (per-driver tokens: HMAC only, token directory
    not visible to the backend), and the rule for what a writer does instead
    (name the location, never the value). Asserted on the docstring as written."""
    doc = guard.__doc__ or ""
    # (i) the coverage set: the two credentials the server holds in plaintext.
    assert "Checked: the two credentials this server holds in plaintext" in doc
    assert "AITELIER_ADMIN_TOKEN" in doc
    assert "AITELIER_MCP_EXTERNAL_TOKEN" in doc
    # (ii) the per-driver-token gap, with its reason.
    assert "NOT checked: each driver's own ``AITELIER_DRIVER_TOKEN``" in doc
    assert "HMAC" in doc and "drivers.token_hash" in doc
    assert "~/.aitelier-drivers/" in doc
    assert "not visible to" in doc and "backend process" in doc
    # (iii) the rule for writers: name the location, never the value.
    assert "name where the token lives" in doc
    assert "never its value" in doc
