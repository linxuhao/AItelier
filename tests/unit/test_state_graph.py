"""State DAG invariants against a real, isolated SQLite database."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.db_manager import DBManager
from core.state_graph import StateConflict, StateGraphError, StateGraphStore, StateNotFound


def node(k, deps=None, priority=0):
    return {"key": k, "goal": f"Deliver {k}", "dependencies": deps or [], "priority": priority,
            "acceptance": [{"id": "behaviour", "kind": "test", "description": "Real behaviour test passes"}]}


@pytest.fixture
def store(tmp_path):
    result = StateGraphStore(DBManager(str(tmp_path / "state.sqlite")))
    result.create_project("shrimp", "Shrimp game")
    return result


def test_forward_references_and_frontier_are_deterministic(store):
    store.add_nodes("shrimp", [node("parent", ["a", "b"]), node("a"), node("b", priority=9)])
    assert [n["node_key"] for n in store.frontier("shrimp")["nodes"]] == ["b", "a"]
    assert store.get_node("shrimp", "parent")["blocked_by"] == ["a", "b"]
    assert store.frontier("shrimp", 1)["truncated"] is True
    assert store.get_node("shrimp", "a")["status"] == "OPEN"


def test_state_project_is_not_a_workflow_run(store):
    with store.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert store.list_projects()[0]["project_id"] == "shrimp"


@pytest.mark.parametrize("bad", [
    [node("a", ["a"])],
    [node("a", ["missing"])],
    [node("a", ["b"]), node("b", ["a"])],
    [node("a"), node("a")],
    [{**node("a"), "dependencies": ["b", "b"]}, node("b")],
    [{**node("a"), "acceptance": []}],
    [{**node("a"), "acceptance": [{"id": "x", "kind": "magic", "description": "x"}]}],
    [{**node("a"), "acceptance": [{"id": "x", "kind": [], "description": "x"}]}],
    [{**node("a"), "acceptance": node("a")["acceptance"] * 2}],
    [{**node("a"), "current_step": "implementation"}],
    [{**node("a"), "status": "VERIFIED"}],
    [{**node("a"), "key": "../escape"}],
    [{**node("a"), "priority": True}],
])
def test_bad_batch_rolls_back_entire_graph_and_audit(store, bad):
    before = store.events("shrimp")
    with pytest.raises(StateGraphError):
        store.add_nodes("shrimp", bad)
    assert store.get_graph("shrimp")["nodes"] == []
    assert store.events("shrimp") == before


def test_edges_cannot_silently_resolve_in_another_project(store):
    store.create_project("other", "Other project")
    store.add_nodes("other", [node("external")])
    with pytest.raises(StateGraphError, match="unknown dependency"):
        store.add_nodes("shrimp", [node("a", ["external"])])


def test_project_create_retry_is_idempotent_but_conflicting_payload_is_not(store):
    before = store.events("shrimp")
    store.create_project("shrimp", "Shrimp game")
    assert store.events("shrimp") == before
    with pytest.raises(StateConflict):
        store.create_project("shrimp", "Different")


def test_revision_is_immutable_and_transitively_invalidates_dependents(store):
    store.add_nodes("shrimp", [node("a"), node("b", ["a"]), node("c", ["b"]), node("unrelated")])
    # Fixture for P1 only: P2 exercises this through real acceptance receipts.
    with store.db.get_connection() as conn:
        conn.execute("UPDATE state_nodes SET status='VERIFIED', verified_receipt='fixture-receipt'")
        conn.commit()
    result = store.revise_node("shrimp", "a", 1, "New monthly action design", goal="Two actions per month")
    assert result["invalidated"] == ["a", "b", "c"]
    assert store.get_node("shrimp", "a")["revision"] == 2
    assert store.get_node("shrimp", "b")["status"] == "STALE"
    assert store.get_node("shrimp", "b")["verified_receipt"] is None
    assert store.get_node("shrimp", "unrelated")["status"] == "VERIFIED"
    assert store.get_node("shrimp", "a")["readiness"] == "ready"
    assert store.get_node("shrimp", "b")["readiness"] == "blocked"
    with store.db.get_connection() as conn:
        revisions = conn.execute("SELECT revision,goal FROM state_node_revisions WHERE node_key='a' ORDER BY revision").fetchall()
    assert [(r[0], r[1]) for r in revisions] == [(1, "Deliver a"), (2, "Two actions per month")]


def test_competing_revisions_have_exactly_one_winner(store):
    store.add_nodes("shrimp", [node("a")])
    def edit(goal):
        try:
            return store.revise_node("shrimp", "a", 1, "Concurrent edit", goal=goal)["revision"]
        except StateConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["First requirement", "Second requirement"]))
    assert sorted(map(str, results)) == ["2", "conflict"]
    assert len([e for e in store.events("shrimp") if e["event_type"] == "node_revised"]) == 1


def test_cycle_on_revision_is_atomic(store):
    store.add_nodes("shrimp", [node("a"), node("b", ["a"])])
    before = store.get_graph("shrimp")
    events = store.events("shrimp")
    with pytest.raises(StateGraphError, match="cycle"):
        store.revise_node("shrimp", "a", 1, "Bad dependency", dependencies=["b"])
    assert store.get_graph("shrimp") == before
    assert store.events("shrimp") == events


def test_split_adds_dependencies_without_replacing_parent_contract(store):
    store.add_nodes("shrimp", [node("coop")])
    before = store.get_node("shrimp", "coop")["contract_hash"]
    result = store.split_node("shrimp", "coop", 1, [node("host"), node("encounter", ["host"])], "Research found subgoals")
    assert result["children"] == ["host", "encounter"]
    parent = store.get_node("shrimp", "coop")
    assert parent["dependencies"] == ["encounter", "host"]
    assert parent["contract_hash"] == before
    assert parent["status"] != "VERIFIED"


def test_invalid_split_leaves_no_child_or_event(store):
    store.add_nodes("shrimp", [node("parent")])
    before = store.get_graph("shrimp")
    events = store.events("shrimp")
    with pytest.raises(StateGraphError, match="cycle"):
        store.split_node("shrimp", "parent", 1, [node("child", ["parent"])], "Cyclic split")
    assert store.get_graph("shrimp") == before
    assert store.events("shrimp") == events


def test_stale_split_rolls_back_new_children(store):
    store.add_nodes("shrimp", [node("parent")])
    with pytest.raises(StateConflict):
        store.split_node("shrimp", "parent", 99, [node("child")], "Old view")
    with pytest.raises(StateNotFound):
        store.get_node("shrimp", "child")


def test_history_and_current_survive_reopening(store):
    store.add_nodes("shrimp", [node("a")])
    fresh = StateGraphStore(DBManager(store.db.db_path))
    assert fresh.get_graph("shrimp") == store.get_graph("shrimp")
    assert fresh.events("shrimp") == store.events("shrimp")
    assert fresh.events("shrimp", after=fresh.events("shrimp")[0]["seq"])[0]["event_type"] == "node_created"


@pytest.mark.parametrize("table", ["state_events", "state_node_revisions"])
@pytest.mark.parametrize("op", ["DELETE", "UPDATE"])
def test_history_tables_reject_rewriting(store, table, op):
    store.add_nodes("shrimp", [node("a")])
    with store.db.get_connection() as conn:
        statement = f"DELETE FROM {table}" if op == "DELETE" else f"UPDATE {table} SET project_id=project_id"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(statement)


def test_superseding_does_not_delete_history_and_blocks_dependents(store):
    store.add_nodes("shrimp", [node("a"), node("b", ["a"])])
    result = store.supersede_node("shrimp", "a", 1, "Replaced by another design")
    assert result["status"] == "SUPERSEDED"
    assert store.get_node("shrimp", "b")["blocked_by"] == ["a"]
    assert store.frontier("shrimp")["total"] == 0
    with pytest.raises(StateConflict):
        store.revise_node("shrimp", "a", 1, "Cannot reopen")
    with pytest.raises(StateConflict):
        store.add_nodes("shrimp", [node("c", ["a"])])
    assert store.events("shrimp")[-1]["event_type"] == "node_superseded"


def test_contract_digest_is_stable_across_json_key_order(store):
    a = node("a")
    b = node("b")
    b["acceptance"][0] = dict(reversed(list(a["acceptance"][0].items())))
    store.add_nodes("shrimp", [a, b])
    assert store.get_node("shrimp", "a")["contract_hash"] == store.get_node("shrimp", "b")["contract_hash"]


def test_invalid_limits_and_unknown_ids_fail_explicitly(store):
    with pytest.raises(StateGraphError):
        store.frontier("shrimp", True)
    with pytest.raises(StateNotFound):
        store.get_graph("missing")
    with pytest.raises(StateGraphError):
        store.events("shrimp", limit=100000)


def test_frontier_is_compact_and_full_context_remains_pullable(store):
    n = node("large")
    n["goal"] = "A" * 1500
    store.add_nodes("shrimp", [n])
    entry = store.frontier("shrimp")["nodes"][0]
    assert len(entry["goal"]) == 1200 and entry["goal_truncated"] is True
    assert "acceptance" not in entry
    assert len(store.get_node("shrimp", "large")["goal"]) == 1500
