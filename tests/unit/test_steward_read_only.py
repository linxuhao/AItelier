"""Plugin step 12: the server enforces "the steward is read-only".

Two places, both a named RESTRICTION on a driver whose `capabilities.role` is
`steward` (never a grant):

* `core.state_commands.execute`'s write branch: only the steward whitelist
  passes; a driver bound to the reserved project without the steward role, or
  whose capabilities cannot be read, is refused (fail closed);
* `api.authz.write_denial_reason`: a non-GET/HEAD request with a steward token
  passes only for the State command/query families and the steward's OWN
  completion/heartbeat/activity; everything else is `steward_read_only`.

The HTTP tests run the real app with the write gate ON and test mode OFF, and
authenticate with the real driver tokens through the registry, never through a
test-mode bypass or a dependency override of the verdict.
"""
from __future__ import annotations

import copy
import json
import re
import sys

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from api import authz
from api.dependencies import get_db_manager
from api.main import app
from core import cf_access, dsh_driver
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers, load_roles
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

HOME = "aitelier-home"
PEPPER = "steward-fixture-pepper-" * 3
OWNER_TOKEN = "steward-fixture-owner-token-0123456789"
STEWARD = {"route": "driver", "reasoning_effort": "high", "compaction": {"thresholdRatio": 0.4},
           "prompt": "steward.md", "presented": ["ask_owner", "close_question"],
           "forbidden": ["subagent"], "presented_only": True}
NODE = {"key": "n1", "goal": "fixture goal",
        "acceptance": [{"id": "a", "kind": "test", "description": "fixture criterion"}]}


def _refusal(action):
    return f"steward_read_only: {action} is not available to the steward role"


def _own_inbox_refusal(action):
    return (f"steward_read_only: {action} may only settle a delivery addressed to "
            "this steward's own project")


def _private_inbox_refusal(action):
    return (f"steward_read_only: {action} may only settle a delivery addressed to this "
            "steward's own inbox, and never with break_glass_reason")


@pytest.fixture
def world(tmp_path, monkeypatch):
    table = copy.deepcopy(load_roles())
    table["steward"] = copy.deepcopy(STEWARD)
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps(table))
    monkeypatch.setattr(dsh_driver, "ROLES_FILE", roles)
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    registry = DriverRegistry(db, PEPPER)
    registry.seed(OWNER_TOKEN)
    owner = StateService(db, actor="fixture", project_read_trusted=True)
    for project in ("alpha", "beta", HOME):  # none of them is opened: all private
        owner.store.create_project(project, project.title())
    runtime = DshDrivers(db, registry, tmp_path / "drivers",
                         command=[sys.executable, "-c", "import time; time.sleep(90)"])
    steward = runtime._provision(HOME, "aitelier", "driver", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    tokens = {name: (runtime.root / row["driver_id"] / "token").read_text().strip()
              for name, row in (("steward", steward), ("director", director))}

    def service(row):
        return StateService(db, actor="driver:" + row["driver_id"], driver_id=row["driver_id"],
                            project_read_trusted=True)

    def set_capabilities(row, text):
        with db.get_connection() as conn:
            conn.execute("UPDATE drivers SET capabilities_json=? WHERE driver_id=?", (text, row["driver_id"]))
            conn.commit()

    return type("World", (), dict(db=db, registry=registry, runtime=runtime, steward=steward,
                                  director=director, tokens=tokens, service=service,
                                  set_capabilities=set_capabilities, owner=owner))


def _project_ids(world):
    with world.db.get_connection() as conn:
        return {row[0] for row in conn.execute("SELECT project_id FROM state_projects")}


# --- execute: the write whitelist ---------------------------------------------------------

REFUSED = {
    "add_nodes": {"project_id": HOME, "nodes": [NODE]},
    "claim_node": {"project_id": "alpha", "node_key": "n1"},
    "start_attempt": {"project_id": "alpha", "node_key": "n1"},
    "create_project": {"project_id": "gamma", "title": "Gamma"},
    # Owner 2026-11-18: the steward may only PROPOSE archive / unarchive; the write is the owner's.
    "archive_project": {"project_id": "alpha"},
    "unarchive_project": {"project_id": "alpha"},
    # The steward's inbox writes are ACKNOWLEDGEMENTS only (a letter can be answered and
    # marked read); resolve stays closed in both inboxes, even for its OWN deliveries.
    "resolve_director_message": {"project_id": HOME, "delivery_id": "0" * 36,
                                 "expected_version": 1, "request_key": "resolve"},
    "resolve_driver_message": {"delivery_id": "0" * 36, "expected_version": 1,
                               "request_key": "resolve-driver"},
}


@pytest.mark.parametrize("action", sorted(REFUSED))
def test_the_steward_is_refused_every_write_outside_the_whitelist(world, action):
    with pytest.raises(StateGraphError) as refused:
        execute(world.service(world.steward), action, REFUSED[action], allow_write=True)
    assert str(refused.value) == _refusal(action)
    assert "gamma" not in _project_ids(world)
    assert not world.owner.store.is_project_archived("alpha")


def test_the_steward_write_whitelist_is_exactly_these_actions():
    """Enumerated, not derived: both inbox acknowledgements are named here and nothing
    else moved (resolve stays out of both)."""
    from core.state_commands import STEWARD_WRITE_ACTIONS
    assert STEWARD_WRITE_ACTIONS == {
        "send_user_message", "withdraw_user_question", "send_director_message",
        "write_driver_note_entry", "supersede_driver_note_entry", "delist_driver_note_entry",
        "create_steward_proposal", "withdraw_steward_proposal", "complete_steward_memory_request",
        "acknowledge_director_message",
        # Owner 2026-11-17: replies in the private inbox, reply-only (see the both-inboxes test).
        "send_driver_message",
        # Owner 2026-11-17: and marking a private letter read, so a wake stops re-injecting it.
        "acknowledge_driver_message",
        # Step 16 (design-steward 7.4): the steward's own scheduled reminders; the
        # service writes only state_steward_schedules, for the steward of its own project.
        "create_steward_schedule", "update_steward_schedule", "delete_steward_schedule", "fire_steward_schedule",
        # Step 19 (design-steward 7.2): the steward's HOST relays what an owner chat channel
        # received; each command checks the channel's bound account or the owner's code itself,
        # and none approves on the steward's say (tests/unit/test_steward_channels.py).
        "relay_owner_message", "bind_owner_channel", "record_channel_delivery",
        # ...and reports each channel's status (a detail line, the WeChat login QR as a
        # checked SVG) into its private status row, which only the owner's dashboard reads.
        "report_channel_status",
        # ...and records what the WeChat bridge answered to the owner's request for a new
        # login QR ({requested} or {refused}; never the QR), run by the host in code.
        "complete_owner_channel_login_request",
        # Step 25 C12 (design-steward 7.9.2): the channel-down alert, called ONLY by the host's
        # code after its status poll (no steward tool maps onto them): the backend decides if an
        # alert is due and through which channel; the host records how its fixed text went.
        "claim_channel_alert", "record_channel_alert",
        # Step 23 (design-steward 7.7): the host's group commands (a group code from the bound
        # account, a group @-message, the exit-checked reply, the turn audience) and the family
        # memory tool, accepted only from a group turn (tests/unit/test_steward_groups.py).
        "bind_owner_group", "relay_group_message", "post_group_reply", "set_steward_turn_audience",
        "note_family_memory",
        # Step 25 (design-steward 7.9.4): post_group_notice is ONLY the host's, called in code with
        # a schedule row that fired (the model has no tool for it: text and group come from the
        # row) - tests/unit/test_steward_family.py.
        "post_group_notice",
        # Step 25 (7.9.5): the family list tools; add and check are accepted from a group turn too
        # (public-read header + the recorded group audience of the named turn), remove never.
        "add_list_items", "check_list_items", "remove_list_items",
        # Step 24.1a (design-steward 7.8.5, 7.8.18): the host writes a fallback-restart marker;
        # the steward re-reads one registered connection's tools (discovery fields only). The
        # gateway judges private connections by step 23's turn audience, above.
        "steward_tools_resync_due", "steward_connection_refresh",
        # Step 22 (design-steward 7.6): the HOST pushes the directors' open questions to the
        # owner's default channel and relays a channel message as the answer to the codes it
        # names (the model passes one message id; tests/unit/test_owner_questions.py).
        "sync_owner_questions", "relay_owner_answer",
        # Step 24.1b (design-steward 7.8.9, 7.8.18): the host asks for a confirmation of an
        # outward action (the text written by code) and reads the stored call back for
        # run_confirmed; only the requester's own reply confirms (tests/unit/test_steward_oauth_confirm.py).
        "create_steward_confirmation", "get_steward_confirmation", "cancel_steward_confirmation"}
    assert "acknowledge_director_message" in STEWARD_WRITE_ACTIONS
    assert "acknowledge_driver_message" in STEWARD_WRITE_ACTIONS
    assert not {"resolve_driver_message", "resolve_director_message"} & STEWARD_WRITE_ACTIONS


def test_every_other_state_write_is_still_refused(world):
    """Not just the four sampled above: EVERY write action outside the whitelist is
    refused by the steward gate, with the named reason, whatever its arguments."""
    from core.state_commands import RETIRED_ACTIONS, STEWARD_WRITE_ACTIONS, WRITE_REQUESTS
    others = sorted(set(WRITE_REQUESTS) - STEWARD_WRITE_ACTIONS - set(RETIRED_ACTIONS))
    assert {"record_evidence", "verify_node", "add_nodes", "resolve_issue"} <= set(others)
    steward = world.service(world.steward)
    for action in others:
        with pytest.raises(StateGraphError) as refused:
            execute(steward, action, {}, allow_write=True)
        assert str(refused.value) == _refusal(action), action


def _delivery(world, sender, target, key):
    """One director-message delivery, sent by the owner fixture into `target`."""
    sent = execute(world.owner, "send_director_message", {
        "sender_project_id": sender, "target_project_id": target, "director_identity": "fixture",
        "request_key": key, "subject": "notice", "body": "work", "protocol_version": "v3"},
        allow_write=True)
    return sent["result"]["deliveries"][0]


def test_the_steward_acknowledges_a_delivery_in_its_own_project_inbox(world):
    own = _delivery(world, "beta", HOME, "own-notice")
    steward = world.service(world.steward)
    arguments = {"project_id": HOME, "delivery_id": own["delivery_id"],
                 "expected_version": own["version"], "request_key": "steward-ack:own",
                 "protocol_version": "v3"}
    acked = execute(steward, "acknowledge_director_message", arguments, allow_write=True)
    assert acked["result"]["delivery"]["status"] == "resolved", \
        "quorum 1 of a transient delivery: the acknowledgement settles it"
    assert acked["result"]["delivery"]["version"] == 2
    assert acked["result"]["acked_by_me"] is True
    # The same request is State's replay, not a second acknowledgement.
    replay = execute(steward, "acknowledge_director_message", arguments, allow_write=True)
    assert replay["result"]["replayed"] is True
    assert replay["result"]["delivery"]["version"] == 2
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_director_delivery_acks "
                            "WHERE delivery_id=?", (own["delivery_id"],)).fetchone()[0] == 1


def test_the_steward_cannot_settle_another_projects_delivery(world):
    foreign = _delivery(world, "beta", "alpha", "foreign-notice")
    steward = world.service(world.steward)
    attempts = (
        # The foreign project's mailbox named directly.
        {"project_id": "alpha", "delivery_id": foreign["delivery_id"],
         "expected_version": 1, "request_key": "foreign-ack"},
        # No project_id at all: a write is never defaulted into the steward's inbox.
        {"delivery_id": foreign["delivery_id"], "expected_version": 1,
         "request_key": "foreign-ack-no-project"},
    )
    for arguments in attempts:
        with pytest.raises(StateGraphError) as refused:
            execute(steward, "acknowledge_director_message", arguments, allow_write=True)
        assert str(refused.value) == _own_inbox_refusal("acknowledge_director_message")
    # Naming its own project does not reach a foreign delivery either: State's transition
    # refuses a delivery whose target is not that project, as an unknown delivery.
    smuggled = execute(steward, "acknowledge_director_message", {
        "project_id": HOME, "delivery_id": foreign["delivery_id"], "expected_version": 1,
        "request_key": "smuggle"}, allow_write=True)
    assert smuggled["code"] == "unknown_delivery"
    # And an id that names nothing is a named refusal, never a silent success.
    unknown = execute(steward, "acknowledge_director_message", {
        "project_id": HOME, "delivery_id": "0" * 36, "expected_version": 1,
        "request_key": "unknown"}, allow_write=True)
    assert unknown["code"] == "unknown_delivery"
    with world.db.get_connection() as conn:
        row = conn.execute("SELECT status, version FROM state_director_deliveries "
                           "WHERE delivery_id=?", (foreign["delivery_id"],)).fetchone()
        assert tuple(row) == ("unread", 1), "a refused acknowledgement wrote nothing"
        assert conn.execute("SELECT COUNT(*) FROM state_director_delivery_acks "
                            "WHERE delivery_id=?", (foreign["delivery_id"],)).fetchone()[0] == 0



def test_the_whitelisted_writes_pass_the_steward_gate(world):
    steward = world.service(world.steward)
    sid = world.steward["driver_id"]
    sent = execute(steward, "send_user_message", {"request_key": "k1", "project_id": HOME,
                                                  "subject": "hello", "body": "fixture"}, allow_write=True)
    assert sent["sender_driver_id"] == sid
    # Its own notebooks: the private one and its bound project's.
    for scope in ({"driver_id": sid}, {"project_id": HOME}):
        written = execute(steward, "write_driver_note_entry", {**scope, "assertion": "rule one",
                          "body": "fixture body", "director_identity": sid}, allow_write=True)
        assert written
    # These reach their own validation: the steward gate let them through.
    for action, arguments in (
            ("withdraw_user_question", {"request_key": "k2", "project_id": HOME, "message_id": "m",
                                        "segment_id": "s", "reason": "fixture"}),
            ("supersede_driver_note_entry", {"driver_id": sid, "entry_id": "0" * 12, "reason": "r",
                                             "assertion": "a", "body": "b", "director_identity": sid}),
            ("delist_driver_note_entry", {"project_id": HOME, "entry_id": "0" * 12, "reason": "r",
                                          "director_identity": sid}),
            ("send_director_message", {"project_id": "alpha", "subject": "handoff", "body": "x"})):
        try:
            execute(steward, action, arguments, allow_write=True)
        except StateGraphError as exc:
            assert not str(exc).startswith(("steward_", "driver_capabilities")), (action, exc)


def test_the_steward_writes_no_other_notebook(world):
    steward = world.service(world.steward)
    sid = world.steward["driver_id"]
    for scope in ({"project_id": "alpha"}, {"driver_id": world.director["driver_id"]}):
        with pytest.raises(StateGraphError, match=re.escape(_refusal("write_driver_note_entry"))):
            execute(steward, "write_driver_note_entry", {**scope, "assertion": "a", "body": "b",
                                                         "director_identity": sid}, allow_write=True)


def _letter(world, target, key):
    """One director message from alpha's director to `target`; its delivery as the target reads it."""
    did = world.director["driver_id"]
    sent = execute(world.service(world.director), "send_director_message", {
        "protocol_version": "v3", "sender_project_id": "alpha", "director_identity": did, "request_key": key,
        "subject": "fixture letter", "body": "x" * 1863, "target_project_id": target}, allow_write=True)
    delivery = sent["result"]["deliveries"][0]
    return {k: delivery[k] for k in ("delivery_id", "version", "status")}


def _private_delivery(world, target, key):
    """One private message from alpha's director to `target`; that driver's delivery row."""
    did = world.director["driver_id"]
    execute(world.service(world.director), "send_driver_message", {"request_key": key, "subject": "private",
            "body": "y" * 900, "target_driver_id": target}, allow_write=True)
    with world.db.get_connection() as conn:
        row = conn.execute("SELECT delivery_id, version, status FROM driver_inbox_deliveries "
                           "WHERE target_driver_id=? ORDER BY rowid DESC LIMIT 1", (target,)).fetchone()
    return dict(row)


def _private_letter(world, key):
    """One private message from alpha's director to the steward; its delivery to the steward."""
    return _private_delivery(world, world.steward["driver_id"], key)


def test_the_steward_answers_letters_in_both_inboxes_but_never_starts_one(world):
    """Owner 2026-11-17: the steward replies to directors in the project inbox and in its
    private inbox. A private message from it is only ever a reply, to the sender of the
    delivery it cites; it settles a private delivery by ACKNOWLEDGEMENT only (the tests
    below), never by resolving it."""
    steward = world.service(world.steward)
    sid, did = world.steward["driver_id"], world.director["driver_id"]
    # Project inbox: a reply to the letter, back to the sender's project.
    letter = _letter(world, HOME, "letter-3")
    try:
        execute(steward, "send_director_message", {"protocol_version": "v3", "sender_project_id": HOME,
                "director_identity": sid, "request_key": "reply-3", "subject": "Re", "body": "got it",
                "target_project_id": "alpha", "reply_to_delivery_id": letter["delivery_id"]}, allow_write=True)
    except StateGraphError as exc:
        assert not str(exc).startswith("steward_"), exc
    # Private inbox: the reply passes, to the sender only.
    private = _private_letter(world, "private-1")
    sent = execute(steward, "send_driver_message", {"request_key": "p-reply", "subject": "Re", "body": "noted",
                   "target_driver_id": did, "reply_to_delivery_id": private["delivery_id"]}, allow_write=True)
    assert sent
    refused = {
        "no reply": {"request_key": "p1", "subject": "s", "body": "b", "target_driver_id": did},
        "another driver": {"request_key": "p2", "subject": "s", "body": "b", "target_driver_id": sid,
                           "reply_to_delivery_id": private["delivery_id"]},
        "a delivery not addressed to it": {"request_key": "p3", "subject": "s", "body": "b", "target_driver_id": did,
                                           "reply_to_delivery_id": "no-such-delivery"},
        "a whole project": {"request_key": "p4", "subject": "s", "body": "b", "project_members": "alpha",
                            "reply_to_delivery_id": private["delivery_id"]},
    }
    for why, arguments in refused.items():
        with pytest.raises(StateGraphError, match=re.escape(_refusal("send_driver_message"))):
            execute(steward, "send_driver_message", arguments, allow_write=True)
    # Settling is acknowledgement only: resolve stays closed over its OWN private letter.
    for action in ("resolve_driver_message", "resolve_director_message"):
        with pytest.raises(StateGraphError, match=re.escape(_refusal(action))):
            execute(steward, action, {"delivery_id": private["delivery_id"], "expected_version": private["version"],
                    "request_key": "settle-" + action, "project_id": HOME}, allow_write=True)


def test_the_steward_acknowledges_a_letter_in_its_own_private_inbox(world):
    """The other half of Owner 2026-11-17 (iss-cef568c3d62e4962): a private letter the
    steward has answered is MARKED READ, so the wake stops re-injecting it. The delivery
    is the steward's own: its driver id is the row's target."""
    sid = world.steward["driver_id"]
    private = _private_delivery(world, sid, "private-ack")
    assert (private["status"], private["version"]) == ("unread", 1)
    steward = world.service(world.steward)
    arguments = {"delivery_id": private["delivery_id"], "expected_version": private["version"],
                 "request_key": "steward-ack-private:" + private["delivery_id"]}
    acked = execute(steward, "acknowledge_driver_message", arguments, allow_write=True)
    assert (acked["delivery_id"], acked["status"], acked["version"]) == (
        private["delivery_id"], "acknowledged", private["version"] + 1)
    with world.db.get_connection() as conn:
        row = conn.execute("SELECT status, version FROM driver_inbox_deliveries WHERE delivery_id=?",
                           (private["delivery_id"],)).fetchone()
    assert tuple(row) == ("acknowledged", private["version"] + 1)
    # The same request is State's replay, not a second acknowledgement or a second version.
    replay = execute(steward, "acknowledge_driver_message", arguments, allow_write=True)
    assert replay == acked
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT version FROM driver_inbox_deliveries WHERE delivery_id=?",
                            (private["delivery_id"],)).fetchone()[0] == private["version"] + 1


def test_the_steward_cannot_acknowledge_another_drivers_private_letter(world):
    """The widening is one box, not the driver inbox: the recipient restriction is the
    command's own (core.driver_inbox.transition authorizes against the row's target), so a
    delivery addressed to another driver is refused and left exactly as it was."""
    world.registry.register("other", "Other", actor="fixture")
    foreign = _private_delivery(world, "other", "private-foreign")
    steward = world.service(world.steward)
    with pytest.raises(StateGraphError) as refused:
        execute(steward, "acknowledge_driver_message", {
            "delivery_id": foreign["delivery_id"], "expected_version": foreign["version"],
            "request_key": "ack-foreign"}, allow_write=True)
    assert str(refused.value) == "not_inbox_recipient"
    # An id that names no delivery is a named refusal too, never a silent success.
    with pytest.raises(StateGraphError) as unknown:
        execute(steward, "acknowledge_driver_message", {
            "delivery_id": "0" * 36, "expected_version": 1, "request_key": "ack-unknown"}, allow_write=True)
    assert str(unknown.value) == "unknown_delivery"
    with world.db.get_connection() as conn:
        row = conn.execute("SELECT status, version FROM driver_inbox_deliveries WHERE delivery_id=?",
                           (foreign["delivery_id"],)).fetchone()
    assert tuple(row) == ("unread", foreign["version"]), "a refused acknowledgement wrote nothing"


def test_the_steward_never_settles_with_break_glass_reason(world):
    """break_glass_reason is the one argument that widens the command's own recipient check
    (owner/admin), so the server refuses it before anything else; the same delivery without
    it is still acknowledged, so the refusal is about the escalation, not the action."""
    sid = world.steward["driver_id"]
    private = _private_delivery(world, sid, "private-glass")
    steward = world.service(world.steward)
    arguments = {"delivery_id": private["delivery_id"], "expected_version": private["version"],
                 "request_key": "ack-glass", "break_glass_reason": "the owner said so"}
    with pytest.raises(StateGraphError) as refused:
        execute(steward, "acknowledge_driver_message", arguments, allow_write=True)
    assert str(refused.value) == _private_inbox_refusal("acknowledge_driver_message")
    with world.db.get_connection() as conn:
        assert tuple(conn.execute("SELECT status, version FROM driver_inbox_deliveries WHERE delivery_id=?",
                                  (private["delivery_id"],)).fetchone()) == ("unread", private["version"])
    # The same delivery, acknowledged without the escalation, is accepted.
    acked = execute(steward, "acknowledge_driver_message", {k: v for k, v in arguments.items()
                    if k != "break_glass_reason"} | {"request_key": "ack-no-glass"}, allow_write=True)
    assert acked["status"] == "acknowledged"


def test_resolve_stays_closed_for_the_steward(world):
    """Regression: the permission did not widen. resolve_driver_message was and stays off
    the whitelist (no consumer; dismissing standing guidance is not the steward's call)."""
    from core.state_commands import STEWARD_WRITE_ACTIONS
    private = _private_letter(world, "private-resolve")
    assert "resolve_driver_message" not in STEWARD_WRITE_ACTIONS
    with pytest.raises(StateGraphError) as refused:
        execute(world.service(world.steward), "resolve_driver_message", {
            "delivery_id": private["delivery_id"], "expected_version": private["version"],
            "request_key": "resolve-private"}, allow_write=True)
    assert str(refused.value) == _refusal("resolve_driver_message")
    with world.db.get_connection() as conn:
        assert tuple(conn.execute("SELECT status, version FROM driver_inbox_deliveries WHERE delivery_id=?",
                                  (private["delivery_id"],)).fetchone()) == ("unread", private["version"])


def test_proposal_actions_are_named_for_step_13():
    from core.state_commands import STEWARD_WRITE_ACTIONS, WRITE_REQUESTS
    steward_actions = {"create_steward_proposal", "withdraw_steward_proposal", "complete_steward_memory_request"}
    assert steward_actions <= STEWARD_WRITE_ACTIONS and steward_actions <= set(WRITE_REQUESTS)


def test_a_director_is_unchanged(world):
    director = world.service(world.director)
    assert execute(director, "add_nodes", {"project_id": "alpha", "nodes": [NODE]}, allow_write=True)
    assert execute(director, "create_project", {"project_id": "gamma", "title": "Gamma"}, allow_write=True)
    # A registered driver with no capabilities at all (not DSH-managed) is ordinary too.
    world.registry.register("plain", "Plain", actor="fixture")
    plain = StateService(world.db, actor="driver:plain", driver_id="plain", project_read_trusted=True)
    assert execute(plain, "add_nodes", {"project_id": "beta", "nodes": [NODE]}, allow_write=True)


@pytest.mark.parametrize("capabilities, error", [
    (json.dumps({"runtime": "dsh", "project_id": HOME}), "steward_role_missing"),
    (json.dumps({"runtime": "dsh", "project_id": HOME, "role": "director"}), "steward_role_missing"),
    ("{}", "steward_role_missing"),
    ("{not json", "driver_capabilities_unreadable"),
    ("[]", "driver_capabilities_unreadable"),
])
def test_the_home_driver_fails_closed(world, capabilities, error):
    world.set_capabilities(world.steward, capabilities)
    steward = world.service(world.steward)
    for action, arguments in (("send_user_message", {"request_key": "k", "project_id": HOME,
                                                     "subject": "s", "body": "b"}),
                              ("add_nodes", REFUSED["add_nodes"])):
        with pytest.raises(StateGraphError) as refused:
            execute(steward, action, arguments, allow_write=True)
        assert str(refused.value) == error
    # Reads are not judged here.
    assert execute(steward, "list_projects", {})


# --- HTTP: the real app, gate on, test mode off, real tokens -------------------------------

@pytest.fixture
def client(world, monkeypatch):
    import api.main as main
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
    app.dependency_overrides[get_db_manager] = lambda: world.db
    try:
        with TestClient(app) as c:
            # After startup, so the lifespan still takes its test-mode path.
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            monkeypatch.setattr(app.state, "dsh_drivers", world.runtime, raising=False)
            assert authz.gate_enabled() and app.state._test_mode is False
            yield c
    finally:
        app.dependency_overrides.pop(get_db_manager, None)


def _headers(world, who):
    return {"X-AItelier-Driver-Token": world.tokens[who]}


RUNTIME_BODIES = {"completion": {"session_id": "s", "messages": []}, "heartbeat": None,
                  "activity": {"phase": "running"}}
_PARAM = re.compile(r"{([^}:]+)(?::[^}]+)?}")


def _walk(routes, prefix=""):
    """(path, route) for every route, descending into included routers (this
    FastAPI keeps them as wrappers around the original router)."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            yield from _walk(inner.routes, prefix + (getattr(context, "prefix", "") or ""))
            continue
        yield prefix + (getattr(route, "path", "") or ""), route


def _write_routes():
    """Every (method, path template) of the app other than GET/HEAD. A route that
    takes any method (the bare /mcp route) is taken as POST. Mounts (the SPA's
    static files) and websocket routes carry no methods and are skipped."""
    out = set()
    for path, route in _walk(app.routes):
        methods = getattr(route, "methods", set())
        if methods is None:
            methods = {"POST"}
        out |= {(method, path) for method in methods - {"GET", "HEAD"}}
    return sorted(out)


def _fill(path, project):
    return _PARAM.sub(lambda m: project if m.group(1) == "project_id" else "x1", path)


def _klass(path):
    if path.startswith(("/api/state/commands/", "/api/state/query/")):
        return "state"
    if re.fullmatch(r"/api/dsh-drivers/\{project_id\}/(completion|heartbeat|activity)", path):
        return "runtime"
    # Step 24.1a: the steward's MCP gateway passes the write gate and judges every method
    # itself (tests/unit/test_steward_connections.py).
    if path == "/api/steward/mcp":
        return "gateway"
    return "other"


def test_the_enumeration_sees_every_kind_of_write_route():
    routes = _write_routes()
    assert len(routes) > 50
    paths = {path for _, path in routes}
    for expected in ("/api/state/commands/{action}", "/api/state/query/{action}", "/mcp",
                     "/api/dsh-drivers/{project_id}/start", "/api/dsh-drivers/{project_id}/stop",
                     "/api/dsh-drivers/{project_id}/work", "/api/dsh-drivers/{project_id}/completion",
                     "/api/dsh-drivers/{project_id}/heartbeat", "/api/dsh-drivers/{project_id}/activity"):
        assert expected in paths, expected
    # Steps 13-14: the owner's decision and memory-request routes are write routes
    # the steward token may NOT use (owner only): the enumeration below refuses
    # them with steward_read_only by the path rule.
    for owner_only in ("/api/steward/proposals/{proposal_id}/approve",
                       "/api/steward/proposals/{proposal_id}/decline", "/api/steward/memory-requests"):
        assert owner_only in paths and _klass(owner_only) == "other", owner_only
    # Step 16: the owner's schedule routes, likewise owner only. The steward writes its
    # schedules through the State commands (class "state"), never through these.
    for method, owner_only in (("POST", "/api/steward/schedules"), ("POST", "/api/steward/schedules/preview"),
                               ("PUT", "/api/steward/schedules/{schedule_id}"),
                               ("DELETE", "/api/steward/schedules/{schedule_id}")):
        assert (method, owner_only) in routes and _klass(owner_only) == "other", (method, owner_only)


def test_every_write_route_with_a_steward_token(world, client):
    """Three classes: State command/query pass the gate (execute judges the
    command again), the steward's own runtime endpoints pass, everything else is
    steward_read_only - including every other project's runtime endpoints, every
    start/stop/work and MCP. A write route added later falls in the last class."""
    before = {row["project_id"]: (row["enabled"], row["phase"]) for row in world.runtime.rows()}
    headers = _headers(world, "steward")
    for method, template in _write_routes():
        klass = _klass(template)
        if klass == "state":
            action = "add_nodes" if "/commands/" in template else "list_projects"
            arguments = REFUSED["add_nodes"] if action == "add_nodes" else {}
            r = client.request(method, template.replace("{action}", action), json=arguments, headers=headers)
            if action == "add_nodes":
                assert r.status_code == 422 and r.json()["detail"] == _refusal("add_nodes"), (template, r.text)
            else:
                assert r.status_code == 200, (template, r.text)
            continue
        if klass == "gateway":
            # Past this gate; the gateway's own check refuses a caller it does not admit
            # (here the test client's Host, not loopback) with its own 403. It answers
            # on its own port alone (director 487).
            r = client.request(method, "http://testserver:4445" + template, json={}, headers=headers)
            assert r.status_code == 403 and r.json() == {"detail": "steward_gateway_forbidden"}, (method, r.text)
            continue
        if klass == "runtime":
            endpoint = template.rsplit("/", 1)[1]
            r = client.request(method, _fill(template, HOME), json=RUNTIME_BODIES[endpoint], headers=headers)
            # Past both gates (this one and owned()): the handler answers that the
            # steward's runtime is stopped.
            assert r.status_code == 409 and r.json()["detail"] == "driver is stopped", (template, r.text)
        r = client.request(method, _fill(template, "alpha"), json={}, headers=headers)
        assert r.status_code == 403, (method, template, r.status_code, r.text)
        assert "steward_read_only" in r.text, (method, template, r.text)
    # The director-messaging REST path answers a denial of its four actions with its
    # closed v2 envelope (api/main.py write_gate), whatever the denial code.
    r = client.post("/api/state/director-messages/send_director_message", json={}, headers=headers)
    assert r.status_code == 403 and "unauthorized" in r.text, r.text
    after = {row["project_id"]: (row["enabled"], row["phase"]) for row in world.runtime.rows()}
    assert after == before, "no driver was started or stopped"
    assert _project_ids(world) == {"alpha", "beta", HOME}


def _request(method, path, token):
    return Request({"type": "http", "method": method, "path": path, "raw_path": path.encode(),
                    "query_string": b"", "scheme": "http", "server": ("testserver", 80),
                    "headers": [(b"x-aitelier-driver-token", token.encode())]})


def test_the_same_enumeration_with_an_ordinary_driver_token_is_unchanged(world, monkeypatch):
    """An ordinary driver's off-tunnel token authorizes every write route, as it
    did before the steward existed. Judged at the verdict every guard shares
    (write_denial_reason), so no handler runs with a token that may write."""
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    owner_cli = OWNER_TOKEN
    for method, template in _write_routes():
        for project in ("alpha", HOME, "beta"):
            path = _fill(template, project)
            for token in (world.tokens["director"], owner_cli):
                assert authz.write_denial_reason(_request(method, path, token)) is None, (method, path)
            steward = authz.write_denial_reason(_request(method, path, world.tokens["steward"]))
            expected = None if _klass(template) in ("state", "gateway") or (
                _klass(template) == "runtime" and project == HOME) else authz.STEWARD_READ_ONLY
            assert steward == expected, (method, path, steward)
        # GET/HEAD are never narrowed.
        for safe in ("GET", "HEAD"):
            assert authz.write_denial_reason(_request(safe, _fill(template, "alpha"), world.tokens["steward"])) is None


def test_the_stewards_own_runtime_endpoints_succeed_when_it_runs(world, client, monkeypatch):
    world.runtime.update(HOME, enabled=1, phase="running")
    monkeypatch.setattr(world.runtime, "running", lambda project_id: project_id == HOME)
    headers = _headers(world, "steward")
    r = client.post(f"/api/dsh-drivers/{HOME}/heartbeat", headers=headers)
    assert r.status_code == 200 and r.json() == {"results": []}, r.text
    r = client.post(f"/api/dsh-drivers/{HOME}/activity", json={"phase": "waiting"}, headers=headers)
    assert r.status_code == 200 and world.runtime.get(HOME)["phase"] == "waiting", r.text
    # Another project's, even a running one, stays refused.
    world.runtime.update("alpha", enabled=1, phase="running")
    monkeypatch.setattr(world.runtime, "running", lambda project_id: True)
    r = client.post("/api/dsh-drivers/alpha/heartbeat", headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "steward_read_only"


def test_steward_reads_still_work_and_private_projects_stay_readable(world, client):
    headers = _headers(world, "steward")
    assert client.get("/api/dsh-drivers", headers=headers).status_code == 200
    r = client.get("/api/dsh-drivers/beta/conversation", headers=headers)
    assert r.status_code == 200, r.text
    r = client.post("/api/state/query/list_user_conversation", json={"project_id": "beta"}, headers=headers)
    assert r.status_code == 200, r.text
    r = client.post("/api/state/query/project_overview", json={"project_id": "beta"}, headers=headers)
    assert r.status_code == 200 and '"beta"' in r.text, r.text
    assert client.get("/api/drivers/me", headers=headers).json()["driver_id"] == world.steward["driver_id"]


def test_the_steward_reads_no_other_drivers_inbox(world, client):
    headers = _headers(world, "steward")
    r = client.post("/api/state/query/list_driver_messages",
                    json={"driver_id": world.director["driver_id"]}, headers=headers)
    assert r.status_code == 422 and "not_inbox_recipient" in r.json()["detail"], r.text


def test_whitelisted_commands_work_over_http_and_refused_ones_do_not(world, client):
    headers = _headers(world, "steward")
    r = client.post("/api/state/commands/send_user_message", headers=headers,
                    json={"request_key": "k", "project_id": HOME, "subject": "s", "body": "b"})
    assert r.status_code == 200 and r.json()["sender_driver_id"] == world.steward["driver_id"], r.text
    for action in sorted(REFUSED):
        r = client.post(f"/api/state/commands/{action}", json=REFUSED[action], headers=headers)
        assert r.status_code == 422 and r.json()["detail"] == _refusal(action), (action, r.text)


def test_the_steward_acknowledges_over_http_and_is_refused_another_inbox(world, client):
    headers = _headers(world, "steward")
    own = _delivery(world, "beta", HOME, "own-http")
    r = client.post("/api/state/commands/acknowledge_director_message", headers=headers, json={
        "project_id": HOME, "delivery_id": own["delivery_id"], "expected_version": own["version"],
        "request_key": "steward-ack:own-http", "protocol_version": "v3"})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["delivery"]["status"] == "resolved", r.text
    foreign = _delivery(world, "beta", "alpha", "foreign-http")
    r = client.post("/api/state/commands/acknowledge_director_message", headers=headers, json={
        "project_id": "alpha", "delivery_id": foreign["delivery_id"], "expected_version": 1,
        "request_key": "steward-ack:foreign-http"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == _own_inbox_refusal("acknowledge_director_message"), r.text


def test_the_home_driver_without_its_role_is_refused_over_http(world, client):
    world.set_capabilities(world.steward, json.dumps({"runtime": "dsh", "project_id": HOME}))
    r = client.post("/api/state/commands/send_user_message", headers=_headers(world, "steward"),
                    json={"request_key": "k", "project_id": HOME, "subject": "s", "body": "b"})
    assert r.status_code == 422 and r.json()["detail"] == "steward_role_missing", r.text


def test_an_ordinary_driver_over_http_is_unchanged(world, client):
    headers = _headers(world, "director")
    r = client.post("/api/state/commands/add_nodes", json={"project_id": "alpha", "nodes": [NODE]},
                    headers=headers)
    assert r.status_code == 200, r.text
    r = client.post("/api/dsh-drivers/alpha/heartbeat", headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "driver is stopped", r.text
    # The runtime endpoint of another project is refused by owned(), not by the gate.
    r = client.post(f"/api/dsh-drivers/{HOME}/heartbeat", headers=headers)
    assert r.status_code == 403 and r.json()["detail"] == "this runtime belongs to another driver", r.text
