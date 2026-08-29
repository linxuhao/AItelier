"""Which step a paused run is waiting on comes from the graph it is PINNED to.

`_get_checkpoint_info` is the single decider, and every surface takes its answer
— the SPA, the CLI, the butler, and MCP (which reuses it explicitly "so a
rejection over MCP and one from the dashboard cannot disagree"). It resolved by
NAME, which was safe only while one name meant one graph. Pinning ended that,
and this call site was on the human-answer path.

With a config edited while a run is paused, the node it asks about is the NEW
graph's, so it names a different step than the user is looking at. Then:

  Reject  → rewinds a step that is already `completed`, and injects feedback
            meant for the paused step into that other step's inputs.
  Approve → the AT-7 idempotency guard compares the client's checkpoint against
            the resolved one, mismatches, and returns `already_advanced` without
            touching the run. Success-shaped response, run paused forever —
            the unanswerable checkpoint that guard's own comment records from
            jinyong-hud, reached through a new door.
"""

from unittest.mock import MagicMock

import pytest

from api import meta_routers


def _node(checkpoint: bool, label: str = "", to: str = ""):
    n = MagicMock()
    n.checkpoint = checkpoint
    n.checkpoint_label = label
    n.checkpoint_reject_to = to
    n.transitions = []
    return n


@pytest.fixture
def sf(monkeypatch):
    """A paused run whose PINNED graph says `b` is a checkpoint and whose
    CURRENT graph says it is not — the shape a config edit produces."""
    sf = MagicMock()
    sf.get_run_by_project.return_value = {
        "id": "run1", "graph_name": "g", "status": "paused",
        "current_node": "done",
    }
    sf.get_steps.return_value = [
        {"step_id": "a", "status": "completed", "id": 1, "completion_seq": 1},
        {"step_id": "b", "status": "completed", "id": 2, "completion_seq": 2},
    ]
    pinned, current = {"a": _node(True, "A gate"), "b": _node(True, "B gate")}, \
                      {"a": _node(True, "A gate"), "b": _node(False)}
    sf._get_resolver_for_run.return_value.get_node.side_effect = pinned.get
    sf._get_resolver.return_value.get_node.side_effect = current.get
    monkeypatch.setattr(meta_routers, "get_skillflow", lambda: sf)
    return sf


def test_it_asks_the_pinned_graph_not_the_current_one(sf):
    step_id, label, run_id, graph, _inst = meta_routers._get_checkpoint_info("p1")

    assert (step_id, label) == ("b", "B gate"), (
        "resolved against the CURRENT graph — the user would be shown, and "
        "would answer, a different step than the run is paused on")
    assert run_id == "run1" and graph == "g"
    sf._get_resolver_for_run.assert_called_with("run1")


def test_it_answers_about_the_run_it_was_given(monkeypatch):
    """A project can have several runs; `get_run_by_project` returns the newest
    non-completed one of ANY config. MCP is handed a run_id precisely because of
    that, and resolving from the project instead made it answer about a
    different run — then pass that run's step id to `reject_checkpoint` on this
    one. A `graph_name` filter would not be enough: two runs of the SAME config
    collide just as well. The run id is the identity.
    """
    sf = MagicMock()
    wanted = {"id": "run-A", "graph_name": "g", "status": "paused",
              "current_node": "done"}
    newest = {"id": "run-B", "graph_name": "other", "status": "failed",
              "current_node": "x"}
    sf.get_run.side_effect = lambda rid: wanted if rid == "run-A" else newest
    sf.get_run_by_project.return_value = newest
    sf.get_steps.return_value = [
        {"step_id": "a", "status": "completed", "id": 1, "completion_seq": 1}]
    sf._get_resolver_for_run.return_value.get_node.side_effect = \
        {"a": _node(True, "A gate")}.get
    monkeypatch.setattr(meta_routers, "get_skillflow", lambda: sf)

    _step, _label, rid, _graph, _inst = meta_routers._get_checkpoint_info(
        "p1", "run-A")

    assert rid == "run-A", "answered about the project's newest run, not the one asked for"
    sf.get_run.assert_called_with("run-A")


def test_without_a_run_id_it_still_resolves_from_the_project(monkeypatch):
    """The control: the HTTP surface has only a project, and must keep working."""
    sf = MagicMock()
    sf.get_run_by_project.return_value = {
        "id": "run-B", "graph_name": "g", "status": "paused",
        "current_node": "done"}
    sf.get_steps.return_value = [
        {"step_id": "a", "status": "completed", "id": 1, "completion_seq": 1}]
    sf._get_resolver_for_run.return_value.get_node.side_effect = \
        {"a": _node(True, "A gate")}.get
    monkeypatch.setattr(meta_routers, "get_skillflow", lambda: sf)

    _step, _label, rid, _graph, _inst = meta_routers._get_checkpoint_info("p1")

    assert rid == "run-B"
    sf.get_run_by_project.assert_called_once()
