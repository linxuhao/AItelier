"""First relay of a channel message wins (2026-11-22 replay after a host restart): the edges the
    reorder in _relayed touches most - a new ref is a new message, a stranger stays refused, an
    unbound owner's redelivery writes nothing, and a pending approval redelivered rendered
    differently is decided exactly once (or not at all once the sender is unbound)."""
from __future__ import annotations

import pytest

from core.state_graph import StateGraphError
from tests.unit.test_steward_channels import (  # noqa: F401
    SLACK_USER, Stopped, bind, conversation, ledger, plant_proposal, proposal, refused, relay, world)
from tests.unit.test_steward_connections import FakeUpstream  # noqa: F401
from tests.unit.test_steward_media import relay_with


def test_p1_same_body_new_ref_is_a_new_message(world):
    bind(world)
    a = relay_with(world, "[图片 未收：渠道不支持]", "r-1", [{"kind": "image", "refused": "unsupported"}])
    b = relay_with(world, "[图片 未收：渠道不支持]", "r-2", [{"kind": "image", "refused": "unsupported"}])
    assert a["duplicate"] is False and b["duplicate"] is False and a["message_id"] != b["message_id"]
    assert len(conversation(world)) == 2


def test_p2_stranger_with_owners_ref_and_other_body_is_refused(world):
    bind(world)
    relay_with(world, "[图片 未收：渠道不支持]", "r-1", [{"kind": "image", "refused": "unsupported"}])
    # derived key (another sender -> another key), and the very same key
    refused(lambda: relay_with(world, "[图片 未收：已取不到]", "r-1", [{"kind": "image", "refused": "gone"}],
                               sender="U0STRANGER", key="relay-x"), "channel_sender_not_bound")
    refused(lambda: relay_with(world, "[图片 未收：已取不到]", "r-1", [{"kind": "image", "refused": "gone"}],
                               sender="U0STRANGER"), "idempotency_conflict")
    assert len(conversation(world)) == 1


def test_p3_unbound_owner_redelivery_rendered_differently_writes_nothing(world):
    bind(world)
    first = relay_with(world, "[图片 未收：渠道不支持]", "r-1", [{"kind": "image", "refused": "unsupported"}])
    world.owner.channels.unbind("slack")
    again = relay_with(world, "[图片 未收：已取不到]", "r-1", [{"kind": "image", "refused": "gone"}])
    assert again == {**first, "duplicate": True}
    assert len(conversation(world)) == 1


@pytest.mark.parametrize("key", [None, "another-key"])
def test_p4_pending_approval_redelivered_rendered_differently_decides_once(world, monkeypatch, key):
    bind(world)
    plant_proposal(world)
    with monkeypatch.context() as patched:
        patched.setattr(world.steward.channels, "_decide", lambda *a, **k: (_ for _ in ()).throw(Stopped()))
        with pytest.raises(Stopped):
            relay_with(world, "同意 proposal p-1", "ts-approve", None)
    assert proposal(world)["status"] == "open"
    again = relay_with(world, "同意 proposal p-1\n[图片 未收：已取不到]", "ts-approve",
                       [{"kind": "image", "refused": "gone"}], key=key)
    assert again["approval"]["outcome"] == "executed" and again["decided_on_replay"] is True
    row = proposal(world)
    assert row["status"] == "done"
    third = relay_with(world, "something else", "ts-approve", [{"kind": "image", "refused": "too_large"}])
    assert third["duplicate"] is True and "decided_on_replay" not in third
    assert proposal(world)["version"] == row["version"]
    assert len(conversation(world)) == 1 and conversation(world)[0]["body"] == "同意 proposal p-1"


def test_p5_pending_approval_unbound_then_redelivered_differently_never_approves(world, monkeypatch):
    bind(world)
    plant_proposal(world)
    with monkeypatch.context() as patched:
        patched.setattr(world.steward.channels, "_decide", lambda *a, **k: (_ for _ in ()).throw(Stopped()))
        with pytest.raises(Stopped):
            relay_with(world, "同意 proposal p-1", "ts-approve", None)
    world.owner.channels.unbind("slack")
    again = relay_with(world, "同意 proposal p-1\n[图片]", "ts-approve", [{"kind": "image", "refused": "gone"}])
    assert again["approval"]["outcome"] == "refused" and again["approval"]["refusal"] == "channel_sender_not_bound"
    assert proposal(world)["status"] == "open"
