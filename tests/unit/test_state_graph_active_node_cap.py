"""The per-project node cap bounds ACTIVE nodes, not the project's history.

Measured defect (2026-11-15): project `aitelier` held 1009 `state_nodes` rows —
962 VERIFIED, 37 SUPERSEDED, 3 STALE, 6 CANDIDATE, 1 OPEN. The size check ran
on the whole graph, so `add_nodes` refused ("a project supports at most 1000
nodes") and — because the same check sat in `_validate_dag`, which every revise
runs — `revise_node` refused too, on a project that only wanted to re-word one
OPEN goal.

The budget now counts nodes whose status is not closed. Closed nodes are
history: they stay readable (`get_node`), searchable (`search_nodes`) and drawn
by the dashboard, but they do not stop a project from recording new goals.

That also changes the issue-work path, which the old check never reached: base
`_add_issue_work` inserted unconditionally, so at the cap a newly measured
defect could get a work item while the project was already over its budget. It
now spends the same budget, and
`test_start_issue_work_refuses_past_the_active_cap` pins that new refusal. The
readability test below passes on the base tree too: it is a regression guard for
closed-node reads, not a witness of the cap change.
"""

import pytest

import core.state_graph as state_graph
from core.db_manager import DBManager
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError, StateGraphStore
from core.state_service import StateService

CRITERION = {"id": "behaviour", "kind": "test", "description": "Real behaviour test passes"}


def node(k, goal=None, deps=None):
    return {"key": k, "goal": goal or f"Deliver {k}", "dependencies": deps or [],
            "acceptance": [dict(CRITERION)]}


@pytest.fixture
def store(tmp_path):
    result = StateGraphStore(DBManager(str(tmp_path / "state.sqlite")), project_read_trusted=True)
    result.create_project("shrimp", "Shrimp game")
    return result


@pytest.fixture
def three_node_cap(monkeypatch):
    monkeypatch.setattr(state_graph, "MAX_NODES", 3)


def test_closed_nodes_do_not_consume_the_cap(store, three_node_cap):
    store.add_nodes("shrimp", [node("a"), node("b"), node("c")])

    with pytest.raises(StateGraphError) as refused:
        store.add_nodes("shrimp", [node("d")])
    message = str(refused.value)
    assert "at most 3 active nodes" in message
    assert "3 active now" in message

    # Closing a node spends nothing: it is history, not outstanding work.
    store.supersede_node("shrimp", "a", 1, "No longer required")
    assert store.add_nodes("shrimp", [node("d")])["created"] == ["d"]
    with pytest.raises(StateGraphError) as refused_again:
        store.add_nodes("shrimp", [node("e")])
    assert "3 active now" in str(refused_again.value)


def test_closed_nodes_remain_readable_and_searchable(store, three_node_cap):
    """Regression guard (green on the base tree too): closing a node must not
    hide it from get_node or search_nodes."""
    store.add_nodes("shrimp", [node("archived", goal="Deliver the zzunique archival goal"),
                               node("b"), node("c")])
    store.supersede_node("shrimp", "archived", 1, "Goal replaced")

    assert store.get_node("shrimp", "archived")["status"] == "SUPERSEDED"
    found = store.search_nodes("shrimp", "zzunique")
    assert [n["node_key"] for n in found["nodes"]] == ["archived"]
    assert found["nodes"][0]["status"] == "SUPERSEDED"


def test_revise_survives_closed_history_beyond_the_cap(store, three_node_cap):
    """Revising an OPEN goal must not be refused because the project history is
    longer than the budget: the old size check lived in `_validate_dag`, which
    every revise runs."""
    store.add_nodes("shrimp", [node("a"), node("b"), node("c")])
    store.supersede_node("shrimp", "a", 1, "No longer required")
    store.supersede_node("shrimp", "b", 1, "No longer required")
    store.add_nodes("shrimp", [node("d")])
    store.add_nodes("shrimp", [node("e")])
    # Five rows, three active: the graph is longer than MAX_NODES.
    revised = store.revise_node("shrimp", "c", 1, "Re-word the goal",
                                goal="Deliver c, re-worded")

    assert revised["revision"] == 2
    assert store.get_node("shrimp", "c")["goal"] == "Deliver c, re-worded"


def test_start_issue_work_refuses_past_the_active_cap(tmp_path, monkeypatch):
    """Issue work now spends the same budget. It did not before this change:
    base `_add_issue_work` inserted without any capacity check, so at the cap a
    newly reported defect could still add an `issue.<id>` row. This test pins
    the refusal that path gains, and that closing a node gives the budget back.
    """
    monkeypatch.setattr(state_graph, "MAX_NODES", 2)
    svc = StateService(StateDatabase(str(tmp_path / "state.sqlite")), actor="t@example.test",
                       project_read_trusted=True)
    svc.create_project("p", "P")
    svc.store.add_nodes("p", [node("a"), node("b")])
    issue = execute(svc, "report_issue", {"project_id": "p", "request_key": "r1", "kind": "defect",
                                          "title": "input dropped", "body": "measured: the key press is dropped",
                                          "director_identity": "d"}, allow_write=True)
    start = {"project_id": "p", "issue_id": issue["issue_id"], "expected_version": issue["version"],
             "acceptance": [dict(CRITERION)], "director_identity": "d"}

    with pytest.raises(StateGraphError) as refused:
        execute(svc, "start_issue_work", dict(start), allow_write=True)
    assert "at most 2 active nodes" in str(refused.value)

    svc.store.supersede_node("p", "a", 1, "No longer required")
    started = execute(svc, "start_issue_work", dict(start), allow_write=True)
    assert started["work_item"] == f"issue.{issue['issue_id']}"
