"""Step 24.2b P4 (design-steward 7.8.12a "消息带附件", "宿主怎么知道有附件", "显示", "隐私";
7.8.18 "消息", "取暂存与ack"): a user's conversation message carries the dashboard's uploads.

- `send_user_conversation_message.attachments` names at most 10 upload_ids; in the write
  transaction each must be an upload of this project by this user, still staged and carried by
  no message, or the whole message is refused invalid_attachment with nothing written;
- the row keeps attachments_json [{id, kind, mime, size, name}] and each upload is `attached` to
  the message, pending for every delivery target;
- a request without attachments replays byte-identically (its stored payload is unchanged);
- list_driver_messages (and so the wake's `driver_inbox`) carries `attachments` only on rows that
  have some; list_user_conversation shows the message row's, else the channel source row's;
- a public read of the conversation is refused, so it never carries attachments."""
from __future__ import annotations

import asyncio
import json

import pytest

from core import state_channels
from core.state_commands import ProjectPrivate, execute
from core.state_graph import StateGraphError
from core.state_service import StateService
from tests.unit.test_driver_inbox_answer_citation import BASE_ROW_KEYS
from tests.unit.test_steward_channels import HOME, OWNER, bind, world  # noqa: F401 (the fixture)

USER = "owner:" + OWNER
OTHER_USER = "owner:someone-else@example.com"
U1, U2, U3 = "u_00000000000000a1", "u_00000000000000a2", "u_00000000000000a3"
FOREIGN_USER, FOREIGN_PROJECT, USED = "u_00000000000000b1", "u_00000000000000b2", "u_00000000000000b3"
REF = "m_0123456789abcdef"


def stage(world, upload_id, project_id="alpha", user=USER, kind="image", mime="image/png", size=1234,
          name="shot.png"):
    with world.db.get_connection() as conn:
        conn.execute("INSERT INTO state_conversation_uploads(upload_id,project_id,user,kind,mime,size,name,state,"
                     "created_at) VALUES(?,?,?,?,?,?,?,'staged',?)",
                     (upload_id, project_id, user, kind, mime, size, name, "2026-11-23T00:00:00.000000+00:00"))
        conn.commit()


def upload(world, upload_id):
    with world.db.get_connection() as conn:
        row = conn.execute("SELECT * FROM state_conversation_uploads WHERE upload_id=?", (upload_id,)).fetchone()
    return dict(row)


def message_row(world, message_id):
    with world.db.get_connection() as conn:
        return dict(conn.execute("SELECT * FROM driver_inbox_messages WHERE message_id=?", (message_id,)).fetchone())


def message_count(world):
    with world.db.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM driver_inbox_messages").fetchone()[0]


def stored_payload(world, request_key):
    with world.db.get_connection() as conn:
        return conn.execute("SELECT payload_json FROM driver_inbox_requests WHERE operation='send_user_conversation' "
                            "AND request_key=?", (request_key,)).fetchone()[0]


def send(world, key, body="see attached", attachments=None, project_id="alpha", service=None):
    return execute(service or world.owner, "send_user_conversation_message", {
        "request_key": key, "project_id": project_id, "body": body,
        **({"attachments": attachments} if attachments is not None else {})}, allow_write=True)


def second_member(world):
    """alpha gets a second member driver, so a message there has two delivery targets."""
    world.registry.register("ada", "ada", actor="test")
    world.registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")


def inbox(service):
    return {row["message_id"]: row for row in execute(service, "list_driver_messages", {})["messages"]}


def conversation(world, project_id):
    return {m["message_id"]: m for m in execute(world.owner, "list_user_conversation",
                                                 {"project_id": project_id})["messages"]}


SHOT = {"id": U1, "kind": "image", "mime": "image/png", "size": 1234, "name": "shot.png"}


# --- the message --------------------------------------------------------------------------------

def test_the_row_keeps_the_attachments_and_each_upload_is_attached_pending_for_every_target(world):
    second_member(world)
    stage(world, U1)
    stage(world, U2, kind="voice", mime="audio/ogg", size=9, name="memo.ogg")
    sent = send(world, "m1", attachments=[U1, U2])
    targets = sorted(d["target_driver_id"] for d in sent["deliveries"])
    assert targets == sorted(["ada", world.director.driver_id])
    assert json.loads(message_row(world, sent["message_id"])["attachments_json"]) == [
        SHOT, {"id": U2, "kind": "voice", "mime": "audio/ogg", "size": 9, "name": "memo.ogg"}]
    for upload_id in (U1, U2):
        row = upload(world, upload_id)
        assert (row["state"], row["message_id"]) == ("attached", sent["message_id"])
        assert json.loads(row["pending_json"]) == targets


def test_an_empty_body_with_one_attachment_is_accepted(world):
    stage(world, U1)
    sent = send(world, "m1", body="", attachments=[U1])
    row = message_row(world, sent["message_id"])
    assert row["body"] == "" and json.loads(row["attachments_json"]) == [SHOT]


@pytest.mark.parametrize("bad", [FOREIGN_USER, FOREIGN_PROJECT, USED, "u_00000000000000ff", "not-an-id"])
def test_an_upload_that_is_not_usable_refuses_the_whole_message_and_writes_nothing(world, bad):
    stage(world, U1)
    stage(world, FOREIGN_USER, user=OTHER_USER)
    stage(world, FOREIGN_PROJECT, project_id=HOME)
    stage(world, USED)
    first = send(world, "first", attachments=[USED])
    before = message_count(world)
    with pytest.raises(StateGraphError, match="^invalid_attachment"):
        send(world, "m1", attachments=[U1, bad])
    assert message_count(world) == before
    # Nothing moved: the good upload is still staged and free, the used one still belongs to
    # the first message, and the request key was not spent.
    assert (upload(world, U1)["state"], upload(world, U1)["message_id"]) == ("staged", None)
    assert (upload(world, USED)["state"], upload(world, USED)["message_id"]) == ("attached", first["message_id"])
    for staged in (FOREIGN_USER, FOREIGN_PROJECT):
        assert (upload(world, staged)["state"], upload(world, staged)["message_id"]) == ("staged", None)
    assert send(world, "m1", attachments=[U1])["message_id"]


def test_the_same_upload_twice_in_one_message_is_refused(world):
    stage(world, U1)
    with pytest.raises(StateGraphError, match="^invalid_attachment"):
        send(world, "m1", attachments=[U1, U1])
    assert upload(world, U1)["state"] == "staged"


def test_another_users_own_upload_is_theirs_even_in_the_same_project(world):
    """The owner's terminal is a project user too, with another actor: the owner's browser
    upload is not its to send."""
    stage(world, U1)
    from core.drivers import CLI_OWNER_ACTOR
    cli = StateService(world.db, actor=CLI_OWNER_ACTOR, project_read_trusted=True)
    with pytest.raises(StateGraphError, match="^invalid_attachment"):
        send(world, "cli-1", attachments=[U1], service=cli)
    assert upload(world, U1)["state"] == "staged"


def test_eleven_attachments_are_invalid_request(world):
    ids = [f"u_{i:016x}" for i in range(11)]
    for upload_id in ids:
        stage(world, upload_id)
    with pytest.raises(StateGraphError, match="attachments"):
        send(world, "m1", attachments=ids)
    with pytest.raises(StateGraphError, match="^invalid_request$"):
        world.owner.driver_inbox.send_user_conversation_message("m2", "alpha", "body", attachments=ids)
    with pytest.raises(StateGraphError, match="^invalid_request$"):
        world.owner.driver_inbox.send_user_conversation_message("m3", "alpha", "body", attachments=[7])
    assert all(upload(world, upload_id)["state"] == "staged" for upload_id in ids)
    assert send(world, "m4", attachments=ids[:10])["message_id"]


def test_a_request_without_attachments_stores_todays_payload_and_replays(world):
    first = send(world, "plain")
    today = json.dumps(["Message from the user", "see attached", "alpha", None, None], sort_keys=True)
    assert stored_payload(world, "plain") == today
    assert message_row(world, first["message_id"])["attachments_json"] is None
    # An explicit empty list is the same request.
    assert send(world, "plain", attachments=[]) == first
    assert stored_payload(world, "plain") == today
    stage(world, U1)
    with pytest.raises(StateGraphError, match="idempotency_conflict"):
        send(world, "plain", attachments=[U1])


def test_a_request_with_attachments_replays_to_the_same_message(world):
    stage(world, U1)
    first = send(world, "m1", attachments=[U1])
    before = message_count(world)
    assert send(world, "m1", attachments=[U1]) == first
    assert message_count(world) == before


# --- the driver's read and the wake ------------------------------------------------------------

def test_the_inbox_entry_carries_attachments_and_one_without_keeps_todays_exact_keys(world):
    stage(world, U1, project_id=HOME)
    plain = send(world, "plain", project_id=HOME)
    carried = send(world, "carried", body="", project_id=HOME, attachments=[U1])
    wake = asyncio.run(execute(world.steward, "wait_for_state_change", {
        "project_id": HOME, "include_driver_inbox": True, "timeout_seconds": 0}))["driver_inbox"]
    for rows in (inbox(world.steward), {row["message_id"]: row for row in wake}):
        assert set(rows[plain["message_id"]]) == BASE_ROW_KEYS
        assert set(rows[carried["message_id"]]) == BASE_ROW_KEYS | {"attachments"}
        assert rows[carried["message_id"]]["attachments"] == [SHOT]


# --- the conversation read ----------------------------------------------------------------------

def test_the_conversation_shows_the_message_rows_attachments_else_the_source_rows(world):
    bind(world)
    stage(world, U1, project_id=HOME)
    dashboard = send(world, "dash", body="", project_id=HOME, attachments=[U1])
    plain = send(world, "plain", project_id=HOME)
    files = [{"kind": "image", "ref": REF, "mime": "image/jpeg", "size": 1234},
             {"kind": "voice", "refused": "too_large"}]
    relayed = execute(world.steward, "relay_owner_message", {
        "request_key": "relay-c1", "channel": "slack", "sender_ref": "U0OWNER", "channel_message_ref": "c1",
        "body": "[图片]\n[语音 过大未收]", "channel_at": state_channels._now().isoformat(),
        "attachments": files}, allow_write=True)
    shown = conversation(world, HOME)
    assert shown[dashboard["message_id"]]["attachments"] == [SHOT]
    assert shown[relayed["message_id"]]["attachments"] == [
        {"id": REF, "kind": "image", "mime": "image/jpeg", "size": 1234, "name": "image-01234567.jpg"},
        {"kind": "voice", "refused": "too_large"}]
    assert "attachments" not in shown[plain["message_id"]]


def test_a_public_read_of_the_conversation_is_refused_so_it_carries_no_attachments(world):
    stage(world, U1)
    send(world, "m1", attachments=[U1])
    world.owner.open_project("alpha")  # even a public project's conversation is not public
    for reader in (StateService(world.db, actor=USER, project_read_trusted=False, public_view=True),
                   StateService(world.db, actor="anonymous", project_read_trusted=False)):
        with pytest.raises(ProjectPrivate):
            execute(reader, "list_user_conversation", {"project_id": "alpha"})
        # The method itself judges the read, whoever reaches it without `execute`.
        with pytest.raises(ProjectPrivate):
            reader.driver_inbox.list_user_conversation("alpha")
