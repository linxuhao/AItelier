"""Every driver-inbox row says who wrote it: `sender_kind` 'user' or 'driver'.

The steward's host reads its wake (wait_for_state_change -> `driver_inbox`, the rows of
list_driver_messages) for the owner's own messages by `sender_kind === 'user'`
(integrations/dsh-driver/steward-host.mjs: withAttachmentLines, humanInput, requesterOf).
The rows carried no such key (2026-11-22): an owner's file reached the steward as a bare
placeholder, and no wake ever counted as a person's message. The key means what the
conversation read (list_user_conversation) means by it: only the owner's half - the rows
write_user_half writes, from the dashboard or relayed from a chat channel - is 'user';
a director's letter, a steward's reply, a host's notice and a driver's note are 'driver'.

The last test pins a real wake's rows as the fixture the node test of steward-host reads
(integrations/dsh-driver/test-fixtures/steward-wake-driver-inbox.json), so a projection
that drops the key fails here, and a host that stops reading it fails there.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.driver_inbox import deliver_notice
from core.state_commands import execute
from tests.unit.test_steward_channels import HOME, bind, relay, world  # noqa: F401 (the fixture)

FIXTURE = (Path(__file__).resolve().parents[2] / "integrations" / "dsh-driver" / "test-fixtures"
           / "steward-wake-driver-inbox.json")


def _inbox(service):
    return {row["message_id"]: row for row in execute(service, "list_driver_messages", {})["messages"]}


def _wake(world):
    return asyncio.run(execute(world.steward, "wait_for_state_change", {
        "project_id": HOME, "include_driver_inbox": True, "timeout_seconds": 0}))["driver_inbox"]


def _director_letter(world, key="letter-1"):
    return execute(world.director, "send_driver_message", {
        "request_key": key, "target_driver_id": world.steward_row["driver_id"], "kind": "request",
        "subject": "Letter from the director", "body": "please look at alpha"}, allow_write=True)


def _host_notice(world):
    """A notice the backend writes for the steward (core/state_steward.py's proposal result):
    no sender driver, like the owner's own messages - and still not the owner's."""
    with world.db.get_connection() as conn:
        delivered = deliver_notice(conn, {
            "target_driver_id": world.steward_row["driver_id"], "sender_driver_id": None, "project_id": HOME,
            "kind": "note", "subject": "Proposal p-1 (create_project): approved",
            "body": "Your proposal p-1 (create_project) is approved.", "refs": {}})
        conn.commit()
    return delivered


def _scenario(world):
    """The steward's inbox, in seq order: a letter, an owner message from Slack, a host
    notice, an owner message from the dashboard."""
    bind(world)
    letter = _director_letter(world)
    slack = relay(world, "what is running?", "slack-ts-1")
    notice = _host_notice(world)
    dashboard = execute(world.owner, "send_user_conversation_message",
                        {"request_key": "d1", "project_id": HOME, "body": "from the dashboard"}, allow_write=True)
    return {"director-letter": letter["message_id"], "owner-slack": slack["message_id"],
            "host-notice": notice["message_id"], "owner-dashboard": dashboard["message_id"]}


def test_the_owners_messages_are_user_from_a_channel_and_from_the_dashboard(world):
    ids = _scenario(world)
    for rows in (_inbox(world.steward), {row["message_id"]: row for row in _wake(world)}):
        assert rows[ids["owner-slack"]]["sender_kind"] == "user"
        assert rows[ids["owner-dashboard"]]["sender_kind"] == "user"


def test_a_letter_a_host_notice_a_steward_reply_and_a_drivers_note_are_driver(world):
    ids = _scenario(world)
    for rows in (_inbox(world.steward), {row["message_id"]: row for row in _wake(world)}):
        assert rows[ids["director-letter"]]["sender_kind"] == "driver"
        assert rows[ids["host-notice"]]["sender_kind"] == "driver"
    # The steward answers the letter (its one way to write a driver); the director's
    # inbox shows it as a driver's message, and a plain note between drivers too.
    letter_delivery = next(row["delivery_id"] for row in _inbox(world.steward).values()
                           if row["message_id"] == ids["director-letter"])
    reply = execute(world.steward, "send_driver_message", {
        "request_key": "reply-1", "target_driver_id": world.director.driver_id,
        "reply_to_delivery_id": letter_delivery, "subject": "Re: letter", "body": "looked"}, allow_write=True)
    note = execute(world.director, "send_driver_message", {
        "request_key": "note-1", "target_driver_id": world.steward_row["driver_id"],
        "subject": "fyi", "body": "a note"}, allow_write=True)
    assert _inbox(world.director)[reply["message_id"]]["sender_kind"] == "driver"
    assert _inbox(world.steward)[note["message_id"]]["sender_kind"] == "driver"


def test_the_inbox_and_the_conversation_read_agree_on_who_wrote_each_owner_message(world):
    ids = _scenario(world)
    conversation = {m["message_id"]: m["sender_kind"] for m in execute(
        world.owner, "list_user_conversation", {"project_id": HOME})["messages"]}
    inbox = _inbox(world.steward)
    for label in ("owner-slack", "owner-dashboard"):
        assert inbox[ids[label]]["sender_kind"] == conversation[ids[label]] == "user"
    # A letter and a notice are not in the owner's conversation at all.
    assert ids["director-letter"] not in conversation and ids["host-notice"] not in conversation


def _normalized(rows, ids, world):
    """The wake's rows with what differs per run (ids, times) replaced by stable names."""
    names = {**{mid: label for label, mid in ids.items()},
             world.steward_row["driver_id"]: "steward", world.director.driver_id: "director"}
    out = []
    for row in rows:
        row = dict(row)
        row["delivery_id"] = "delivery-" + names[row["message_id"]]
        for key in ("message_id", "thread_id", "target_driver_id", "sender_driver_id"):
            if row[key] is not None:
                row[key] = names[row[key]]
        for key in ("created_at", "channel_at"):
            if key in row:
                row[key] = "2026-11-22T00:00:00+00:00"
        out.append(row)
    return out


def test_a_real_wakes_rows_are_the_fixture_the_steward_host_test_reads(world):
    """The contract between the two sides: the node test runs withAttachmentLines,
    humanInput and requesterOf on exactly these rows. On a deliberate row change, put the
    printed rows into the fixture's driver_inbox and keep the node test green."""
    ids = _scenario(world)
    live = _normalized(_wake(world), ids, world)
    pinned = json.loads(FIXTURE.read_text())["driver_inbox"]
    assert live == pinned, json.dumps(live, ensure_ascii=False, indent=1)
