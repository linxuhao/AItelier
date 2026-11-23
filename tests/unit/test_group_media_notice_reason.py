"""group_media_notice carries a failed send's own short code to State.

Three hops carry it: the director plugin passes the code its own send returned
(`noticePass` in integrations/dsh-director/lib/channels.mjs), the driver's groups
adapter forwards it as the request field `reason` (integrations/dsh-driver/
steward-host.mjs), and this command model must accept it. The adapter's hop is
asserted in the driver's node suite; this file pins the HTTP hop, because the
model forbids unknown fields and would otherwise answer 422 before the service
ever saw the code - a fix that looks delivered while the note stays bare.

A caller that sends no code keeps exactly today's body.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.state_commands import REQUESTS

BASE = {"project_id": "aitelier", "channel": "wechat", "group_ref": "27960105726@chatroom", "epoch": "E1",
        "notice": "on", "phase": "failed"}


def body(**extra):
    return REQUESTS["group_media_notice"].model_validate(
        {"request_key": "k", **BASE, **extra}).model_dump()


def test_a_failed_attempt_carries_the_sends_own_code():
    assert body(reason="room_not_bound")["reason"] == "room_not_bound"


def test_a_caller_that_sends_no_code_leaves_the_field_unset():
    assert body()["reason"] is None


def test_the_code_is_a_bounded_string_never_free_text():
    assert body(reason="room_backstop")["reason"] == "room_backstop"
    with pytest.raises(ValidationError):
        body(reason="x" * 65)
    with pytest.raises(ValidationError):
        body(reason=7)
