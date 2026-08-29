"""A checkpoint must stay answerable after the pointer to it is lost.

skillflow's `recover_stale_claims` reaps a dead owner's claim by setting the
run's `current_node` to NULL — unconditionally, PAUSED runs included, where the
reaped claim says nothing about where the run sits. Restart the container while
a checkpoint waits and that pointer is gone.

`_get_checkpoint_info` derived the checkpoint step from `current_node` alone, so
the loss was terminal: every answer path (SPA, CLI, butler, MCP) reported "no
checkpoint to answer" for a run visibly paused at one, and the run could never
be resumed. Measured on jinyong-hud, 2026-08-27, with the human vision verdict
already written.
"""
from unittest.mock import MagicMock

import pytest


def _node(checkpoint, label="", targets=()):
    n = MagicMock()
    n.checkpoint = checkpoint
    n.checkpoint_label = label
    n.transitions = [MagicMock(to=t, match={"from": "checkpoint"}) for t in targets]
    return n


NODES = {
    "5_vision": _node(False),
    "5_vision_human": _node(True, "看帧裁决", ("5_vision_judged",)),
    "3": _node(True, "计划评审", ("3_budget",)),
}


def _sf(current_node):
    sf = MagicMock()
    sf.get_run_by_project.return_value = {
        "id": "run1", "graph_name": "dpe_game", "status": "paused",
        "current_node": current_node, "error_reason": None,
    }
    resolver = MagicMock()
    resolver.get_node.side_effect = lambda sid: NODES.get(sid)
    sf._get_resolver.return_value = resolver
    # The pinned accessor is what the code prefers now — a run answers about the
    # graph version it started with, not whatever is registered. Same resolver
    # here so these tests keep exercising checkpoint RESOLUTION.
    sf._get_resolver_for_run.return_value = resolver
    sf.get_steps.return_value = [
        {"step_id": "3", "status": "completed", "id": 10},
        {"step_id": "5_vision", "status": "completed", "id": 20},
        {"step_id": "5_vision_human", "status": "completed", "id": 21},
    ]
    return sf


@pytest.fixture
def info(monkeypatch):
    from api import meta_routers

    def _call(current_node):
        monkeypatch.setattr(meta_routers, "get_skillflow", lambda: _sf(current_node))
        return meta_routers._get_checkpoint_info("p1")
    return _call


def test_pointer_present_identifies_the_exact_checkpoint(info):
    step_id, label, _run, _graph, instance = info("5_vision_judged")
    assert step_id == "5_vision_human"
    assert label == "看帧裁决"
    assert instance == 21


def test_pointer_lost_still_finds_the_checkpoint(info):
    # This is the restart case: current_node nulled, run still paused.
    step_id, label, _run, _graph, instance = info(None)
    assert step_id == "5_vision_human", (
        "a paused run whose current_node was reaped must still be answerable")
    assert label == "看帧裁决"
    assert instance == 21


def test_empty_pointer_is_treated_the_same_as_missing(info):
    assert info("")[0] == "5_vision_human"


def test_pointer_lost_picks_the_MOST_RECENT_checkpoint_not_the_first(info):
    # Step 3 is also a checkpoint and completed earlier. Falling back to it
    # would rewind the run past everything the task loop built.
    assert info(None)[0] != "3"
