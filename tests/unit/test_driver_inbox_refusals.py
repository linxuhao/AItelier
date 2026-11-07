"""Every refusal of send_driver_message names the constraint it violated.

Two contracts are pinned here:

1. A table: each refusal site reachable from `DriverInbox.send_driver_message`
   (and its `reply_to_message_id` precondition) raises a StateGraphError whose
   text begins with the machine code the caller branches on, and then states the
   violated constraint with the parameter names. A refusal that is only a bare
   code is a blind guess for the caller.

   One reachable refusal carries NO leading code: a `project_members` value that
   names no State project raises StateNotFound ("state project '<id>' not
   found") from the shared project lookup. It is pinned by its own test below,
   outside the table, and is reported as an open gap rather than papered over:
   changing the shared lookup is outside this witness-only change.

2. An HTTP witness: the real command route (`POST /api/state/commands/{action}`)
   answers the refused request with text that names BOTH parameters.

Base-implementation answer for the identical witness request (commit
9bdc8f389de68bde8d99395a3bd339e2b1f2409d, before this change): HTTP 422 with
`{"detail": "invalid_request"}` - a bare code, no field, no constraint. The same
request now answers `{"detail": "invalid_request: target_driver_id and
project_members are mutually exclusive; set exactly one"}`. The code stays first,
so existing `match="invalid_request"` assertions keep holding.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.state_http import create_state_router
from core.drivers import DriverRegistry
from core.state_database import StateDatabase
from core.state_graph import StateGraphError, StateNotFound, now
from core.state_service import StateService

BASE_WITNESS_DETAIL = "invalid_request"  # what 9bdc8f389de68bde8d99395a3bd339e2b1f2409d answered


@pytest.fixture
def peers(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "alpha")
    registry = DriverRegistry(db, "test-only-pepper-not-a-production-secret")
    for did in ["ada", "bob", "carol"]:
        registry.register(did, did, actor="test")
        registry.set_membership("alpha", did, "member", 0, "fixture", actor="test")
    services = {did: StateService(db, actor="driver:" + did, driver_id=did, project_read_trusted=True)
                for did in ["ada", "bob", "carol"]}
    return services


def _ok(**overrides):
    base = dict(request_key="k-ok", subject="subject", body="body",
                target_driver_id="bob")
    base.update(overrides)
    return base


# (case id, call kwargs overrides, expected leading code, required substring)
REFUSALS = [
    ("request_key_empty", dict(request_key=""), "invalid_request", "request_key"),
    ("request_key_too_long", dict(request_key="x" * 321), "invalid_request", "320"),
    ("request_key_not_str", dict(request_key=7), "invalid_request", "request_key"),
    ("subject_empty", dict(subject=""), "invalid_request", "subject"),
    ("subject_too_long", dict(subject="s" * 201), "invalid_request", "200"),
    ("body_too_long", dict(body="b" * 8001), "invalid_request", "8000"),
    ("both_targets", dict(target_driver_id="bob", project_members="alpha"),
     "invalid_request", "target_driver_id and project_members are mutually exclusive"),
    ("neither_target", dict(target_driver_id=None, project_members=None),
     "invalid_request", "target_driver_id or project_members is required"),
    ("bad_delivery_mode", dict(delivery_mode="sticky"), "invalid_request", "delivery_mode"),
    ("bad_kind", dict(kind="gossip"), "invalid_request", "kind must be one of"),
    ("refs_not_object", dict(refs=["attempt_id"]), "invalid_refs", "refs must be an object"),
    ("refs_unknown_key", dict(refs={"bogus": "x"}), "invalid_refs", "not allowed"),
    ("refs_node_key_without_project", dict(refs={"node_key": "n"}), "invalid_refs", "needs project_id"),
    ("refs_attempt_not_string", dict(refs={"attempt_id": 3}), "invalid_refs", "refs.attempt_id"),
    ("refs_attempt_missing_record", dict(refs={"attempt_id": "absent"}), "invalid_refs",
     "refs.attempt_id names no existing record"),
    ("unknown_target", dict(target_driver_id="nobody"), "unknown_driver", "no driver with this driver_id"),
    ("reply_not_delivered_here", dict(reply_to_message_id="no-such-message"), "unknown_message",
     "reply_to_message_id must name a message delivered to this driver"),
]


def _refusal_text(svc, overrides):
    """Call the real send path and return the StateGraphError text."""
    kwargs = _ok(**overrides)
    with pytest.raises(StateGraphError) as refused:
        svc.driver_inbox.send_driver_message(**kwargs)
    return str(refused.value)


@pytest.mark.parametrize("case,overrides,code,needle", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_every_refusal_leads_with_its_code_and_names_the_constraint(peers, case, overrides, code, needle):
    text = _refusal_text(peers["ada"], overrides)
    assert text.split(":", 1)[0].strip() == code, text
    assert needle in text, text
    # A refusal that is only the bare code is the defect this file exists to fix.
    assert text != code, text


def test_no_recipients_when_project_has_no_other_member(tmp_path):
    """A project whose only member is the sender resolves to zero recipients."""
    db = StateDatabase(str(tmp_path / "solo.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("solo", "solo")
    registry = DriverRegistry(db, "test-only-pepper-not-a-production-secret")
    registry.register("ada", "ada", actor="test")
    registry.set_membership("solo", "ada", "member", 0, "fixture", actor="test")
    ada = StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=True)
    text = _refusal_text(ada, dict(target_driver_id=None, project_members="solo", request_key="k-solo"))
    assert text.split(":", 1)[0].strip() == "no_recipients", text
    assert "no active member other than the sender" in text, text


def test_idempotency_conflict_names_the_reused_request_key(peers):
    ada = peers["ada"]
    ada.driver_inbox.send_driver_message("k-dup", "first", "b", target_driver_id="bob")
    text = _refusal_text(ada, dict(request_key="k-dup", subject="different", target_driver_id="bob"))
    assert text.split(":", 1)[0].strip() == "idempotency_conflict", text
    assert "request_key was already used" in text, text


def test_inactive_sender_is_named(peers):
    ada = peers["ada"]
    with ada.db.get_connection() as conn:
        conn.execute("UPDATE drivers SET status='retired' WHERE driver_id='ada'")
        conn.commit()
    text = _refusal_text(ada, dict(request_key="k-inactive"))
    assert text.split(":", 1)[0].strip() == "inactive_driver", text
    assert "not active" in text, text


def test_missing_identity_is_named(peers):
    """No driver identity and no owner actor: the send is refused, not guessed at."""
    anonymous = StateService(peers["ada"].db, actor="local-operator", project_read_trusted=True)
    text = _refusal_text(anonymous, dict(request_key="k-anon"))
    assert text.split(":", 1)[0].strip() == "invalid_request", text
    assert "sending requires a driver identity or an owner actor" in text, text
    assert text != "invalid_request", text


def test_unregistered_sender_is_named(peers):
    """A driver_id that was never registered is refused before any target is resolved."""
    with peers["ada"].db.get_connection() as conn:
        # Precondition: the id really is absent, so the refusal is the registry's, not a typo's.
        assert conn.execute("SELECT 1 FROM drivers WHERE driver_id='ghost'").fetchone() is None
    ghost = StateService(peers["ada"].db, actor="driver:ghost", driver_id="ghost", project_read_trusted=True)
    text = _refusal_text(ghost, dict(request_key="k-ghost"))
    assert text.split(":", 1)[0].strip() == "unknown_driver", text
    assert "no driver with this driver_id is registered" in text, text
    assert text != "unknown_driver", text


def test_more_than_200_recipients_names_the_limit(peers):
    """201 active members resolve to 201 recipients, and the cap refuses them by name.

    The members are inserted with SQL rather than registry.register: a
    registry call per member is too slow for a unit test. The sender is not a
    member, so none of the 201 is excluded and the count is exactly 201.
    """
    ada = peers["ada"]
    seed = StateService(ada.db, actor="fixture", project_read_trusted=True)
    seed.create_project("big", "big")
    stamp = now()
    with ada.db.get_connection() as conn:
        conn.executemany(
            "INSERT INTO drivers(driver_id,display_name,kind,is_admin,token_hash,token_rotated_at,"
            "host_label,capabilities_json,status,revision,created_at,last_seen_at) "
            "VALUES(?,?,'lan',0,?,NULL,'','{}','active',1,?,NULL)",
            [(f"member-{i:03d}", f"member {i:03d}", f"sha256:fixture-{i:03d}", stamp) for i in range(201)])
        conn.executemany(
            "INSERT INTO project_drivers(project_id,driver_id,status,revision,actor,updated_at) "
            "VALUES('big',?,'member',1,'fixture',?)",
            [(f"member-{i:03d}", stamp) for i in range(201)])
        conn.commit()
    text = _refusal_text(ada, dict(request_key="k-big", target_driver_id=None, project_members="big"))
    assert text.split(":", 1)[0].strip() == "no_recipients", text
    assert "project_members resolved to 201 recipients" in text, text
    assert "above the limit of 200 per message" in text, text
    assert text != "no_recipients", text


def test_unknown_project_members_project_is_named(peers):
    """A project_members name with no State project is refused by name.

    This is the one reachable refusal that carries NO leading machine code:
    StateNotFound is raised by the shared project lookup, and its text is
    "state project '<id>' not found". The module docstring records the exception.
    """
    ada = peers["ada"]
    with pytest.raises(StateNotFound) as refused:
        ada.driver_inbox.send_driver_message(**_ok(
            request_key="k-noproj", target_driver_id=None, project_members="no-such-project"))
    text = str(refused.value)
    assert "no-such-project" in text and "not found" in text, text


def test_http_witness_both_targets_names_both_parameters(peers):
    """The real command route, driven exactly as an MCP caller drives it."""
    app = FastAPI()
    app.include_router(create_state_router(lambda: peers["ada"], lambda: None))
    body = {"action": "send_driver_message", "arguments": {
        "request_key": "witness-both", "subject": "subject", "body": "body",
        "target_driver_id": "bob", "project_members": "alpha"}}
    with TestClient(app) as client:
        response = client.post("/api/state/commands/send_driver_message", json=body["arguments"])
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    print("HTTP witness status:", response.status_code)
    print("HTTP witness detail:", detail)
    assert detail.split(":", 1)[0].strip() == "invalid_request", detail
    assert "target_driver_id" in detail and "project_members" in detail, detail
    assert detail != BASE_WITNESS_DETAIL, "the base bare code must no longer be the whole answer"
