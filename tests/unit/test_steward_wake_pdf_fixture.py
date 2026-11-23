"""Step 24.2c (design-steward 7.8.12b "十六" 契约): the steward's PDF wake fixture is a real wake.

integrations/dsh-driver/runtime.read-document.test.mjs boots the steward in the real pinned DSH
on one wake whose driver_inbox is the fixture's (test-fixtures/steward-wake-pdf.json): the owner's
dashboard message carrying one PDF upload. The node side takes the rows as State sends them;
this test makes State send them - the upload staged by the owner, the message written with it,
the steward's wait_for_state_change read - and compares them with the fixture, ids and times
renamed as in test_driver_inbox_sender_kind.py. The upload's bytes are the fixture's own PDF, so
its staged size is the byte length the host stores.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.state_commands import execute
from tests.unit.test_conversation_attachments import stage
from tests.unit.test_steward_channels import HOME, OWNER, world  # noqa: F401 (the fixture)

FIXTURE = (Path(__file__).resolve().parents[2] / "integrations" / "dsh-driver" / "test-fixtures"
           / "steward-wake-pdf.json")
UPLOAD = "u_00000000000000f1"


def _fixture():
    return json.loads(FIXTURE.read_text())


def _wake(world):
    return asyncio.run(execute(world.steward, "wait_for_state_change", {
        "project_id": HOME, "include_driver_inbox": True, "timeout_seconds": 0}))["driver_inbox"]


def _normalized(rows, message_id, world):
    names = {message_id: "owner-dashboard-pdf", world.steward_row["driver_id"]: "steward"}
    out = []
    for row in rows:
        row = dict(row)
        row["delivery_id"] = "delivery-" + names[row["message_id"]]
        for key in ("message_id", "thread_id", "target_driver_id", "sender_driver_id"):
            if row[key] is not None:
                row[key] = names[row[key]]
        row["created_at"] = "2026-11-23T00:00:00+00:00"
        out.append(row)
    return out


def test_the_fixture_pdf_is_the_size_its_attachment_says():
    fixture = _fixture()
    [row] = fixture["driver_inbox"]
    [attachment] = row["attachments"]
    data = fixture["uploads"][attachment["id"]].encode("latin-1")
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    assert attachment["size"] == len(data)


def test_a_real_wake_with_a_dashboard_pdf_is_the_fixture(world):
    """On a deliberate row change, put the printed rows into the fixture's driver_inbox and keep
    runtime.read-document.test.mjs green."""
    fixture = _fixture()
    [pinned_row] = fixture["driver_inbox"]
    [attachment] = pinned_row["attachments"]
    stage(world, UPLOAD, project_id=HOME, user="owner:" + OWNER, kind="file", mime="application/pdf",
          size=len(fixture["uploads"][UPLOAD].encode("latin-1")), name="invoice.pdf")
    sent = execute(world.owner, "send_user_conversation_message", {
        "request_key": "d-pdf", "project_id": HOME, "body": pinned_row["body"], "attachments": [UPLOAD]},
        allow_write=True)
    live = _normalized(_wake(world), sent["message_id"], world)
    assert attachment["id"] == UPLOAD
    assert live == fixture["driver_inbox"], json.dumps(live, ensure_ascii=False, indent=1)
