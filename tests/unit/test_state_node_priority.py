"""Priority is scheduling metadata: a CAS-only change that retains acceptance.

Exercises the real command -> service -> store -> SQLite path on an owned
disposable database.  A priority change must never recreate node identity,
revision, contract hash, status, dependencies, acceptance, receipts, evidence
or a running attempt; it must refuse a stale compare-and-swap and malformed
inputs without writing; a same-value no-op obeys the CAS but emits no
misleading change event; and the change must reorder the selection surfaces.
This file is UNRUN in the implementation workflow.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.db_manager import DBManager
from core.state_commands import READ_REQUESTS, WRITE_REQUESTS, execute, is_public_read
from core.state_database import StateDatabase
from core.state_graph import StateConflict, StateGraphError, StateGraphStore
from core.state_service import StateService


def node(k, deps=None, priority=0):
    return {"key": k, "goal": f"Deliver {k}", "dependencies": deps or [], "priority": priority,
            "acceptance": [{"id": "behaviour", "kind": "test",
                            "description": "Real behaviour test passes"}]}


@pytest.fixture
def store(tmp_path):
    result = StateGraphStore(DBManager(str(tmp_path / "state.sqlite")), project_read_trusted=True)
    result.create_project("game", "Game")
    return result


@pytest.fixture
def service(tmp_path):
    return StateService(StateDatabase(str(tmp_path / "host.sqlite")), actor="director-a",
                        project_read_trusted=True)


def snapshot(store, project_id="game"):
    """Every history-bearing row a priority change must leave byte-identical."""
    with store.transaction() as conn:
        nodes = {r["node_key"]: dict(r) for r in conn.execute(
            "SELECT * FROM state_nodes WHERE project_id=?", (project_id,))}
        revisions = [dict(r) for r in conn.execute(
            "SELECT * FROM state_node_revisions WHERE project_id=? ORDER BY node_key,revision",
            (project_id,))]
        deps = [tuple(r) for r in conn.execute(
            "SELECT node_key,dependency_key FROM state_dependencies WHERE project_id=? "
            "ORDER BY node_key,dependency_key", (project_id,))]
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM state_events WHERE project_id=? ORDER BY seq", (project_id,))]
    return nodes, revisions, deps, events


def test_priority_change_writes_only_priority_and_audits_one_event(store):
    store.add_nodes("game", [node("a"), node("b")])
    before_nodes, before_revisions, before_deps, before_events = snapshot(store)
    result = store.set_node_priority("game", "a", 5, 0, "hotfix first")
    assert result == {"key": "a", "priority": 5, "previous": 0, "revision": 1, "changed": True}
    after_nodes, after_revisions, after_deps, after_events = snapshot(store)
    assert after_nodes["a"]["priority"] == 5 and after_nodes["a"]["revision"] == 1
    for field in ("node_key", "revision", "goal", "contract_json", "contract_hash",
                  "status", "verified_receipt", "created_at", "facet"):
        assert after_nodes["a"][field] == before_nodes["a"][field], field
    # History, dependencies and the other node are untouched; one event appended.
    assert after_revisions == before_revisions
    assert after_deps == before_deps
    assert after_nodes["b"] == before_nodes["b"]
    assert len(after_events) == len(before_events) + 1
    event = after_events[-1]
    assert event["event_type"] == "node_priority_set" and event["node_key"] == "a"
    assert json.loads(event["payload_json"]) == {
        "previous": 0, "current": 5, "revision": 1, "reason": "hotfix first"}


def test_same_priority_noop_obeys_cas_but_emits_no_event(store):
    store.add_nodes("game", [node("a")])
    before = snapshot(store)
    assert store.set_node_priority("game", "a", 0, 0, "reassert")["changed"] is False
    assert snapshot(store) == before
    with pytest.raises(StateConflict, match="priority changed"):
        store.set_node_priority("game", "a", 3, 7, "stale expectation")
    assert snapshot(store) == before
    assert store.get_node("game", "a")["priority"] == 0


@pytest.mark.parametrize("priority,expected,reason", [
    (True, 0, "a boolean is not an integer"),
    (0.5, 0, "a float is not an integer"),
    ("5", 0, "text is not an integer"),
    (100001, 0, "above the ceiling"),
    (-100001, 0, "below the floor"),
    (5, 100001, "stale expectation out of range"),
    (5, 0, "   "),
])
def test_malformed_priority_inputs_refuse_without_writing(store, priority, expected, reason):
    store.add_nodes("game", [node("a")])
    before = snapshot(store)
    with pytest.raises(StateGraphError):
        store.set_node_priority("game", "a", priority, expected, reason)
    assert snapshot(store) == before


def test_unknown_project_or_node_refuses_without_writing(store):
    store.add_nodes("game", [node("a")])
    before = snapshot(store)
    with pytest.raises(StateGraphError):
        store.set_node_priority("ghost", "a", 1, 0, "unknown project")
    with pytest.raises(StateGraphError):
        store.set_node_priority("game", "ghost", 1, 0, "unknown node")
    assert snapshot(store) == before


def test_priority_change_preserves_a_verified_node_and_its_receipt(store):
    store.add_nodes("game", [node("a")])
    with store.db.get_connection() as conn:
        conn.execute("UPDATE state_nodes SET status='VERIFIED',verified_receipt='receipt-1' "
                     "WHERE project_id='game' AND node_key='a'")
        conn.commit()
    assert store.set_node_priority("game", "a", 9, 0, "schedule accepted work first")["changed"] is True
    after = store.get_node("game", "a")
    assert after["status"] == "VERIFIED" and after["verified_receipt"] == "receipt-1"
    assert after["revision"] == 1 and after["priority"] == 9


def test_priority_change_preserves_a_running_attempt_and_its_context(service):
    service.create_project("game", "Game")
    service.store.add_nodes("game", [node("a")])
    attempt = service.start_external_attempt("game", "a", 1, "harness", "job-1", "once")
    with service.store.transaction() as conn:
        before = dict(conn.execute("SELECT * FROM state_attempts WHERE attempt_id=?",
                                   (attempt["attempt_id"],)).fetchone())
    execute(service, "set_node_priority",
            {"project_id": "game", "node_key": "a", "priority": 4, "expected_priority": 0,
             "reason": "bump while the harness runs"}, allow_write=True)
    with service.store.transaction() as conn:
        after = dict(conn.execute("SELECT * FROM state_attempts WHERE attempt_id=?",
                                  (attempt["attempt_id"],)).fetchone())
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM state_events WHERE project_id='game' ORDER BY seq")]
    assert after == before, "a scheduling change must not touch an attempt row"
    assert service.get_attempt(attempt["attempt_id"])["context_hash"] == attempt["context_hash"]
    change = json.loads([e["payload_json"] for e in events
                         if e["event_type"] == "node_priority_set"][-1])
    assert change["actor"] == "director-a"
    assert (change["previous"], change["current"]) == (0, 4)
    assert change["revision"] == 1


def test_frontier_and_overview_reorder_by_priority(store):
    store.add_nodes("game", [node("a"), node("b")])
    assert [n["node_key"] for n in store.frontier("game")["nodes"]] == ["a", "b"]
    store.set_node_priority("game", "b", 9, 0, "run b first")
    assert [n["node_key"] for n in store.frontier("game")["nodes"]] == ["b", "a"]
    assert [n["node_key"] for n in store.get_graph("game")["nodes"]] == ["b", "a"]


def test_reprioritizing_one_project_leaves_another_byte_identical(store):
    store.create_project("other", "Other")
    store.add_nodes("game", [node("a")])
    store.add_nodes("other", [node("a")])
    before = snapshot(store, "other")
    assert store.set_node_priority("game", "a", 8, 0, "game only")["changed"] is True
    assert snapshot(store, "other") == before


def test_command_surface_is_writer_only(service):
    service.create_project("game", "Game")
    service.store.add_nodes("game", [node("a")])
    assert "set_node_priority" in WRITE_REQUESTS and "set_node_priority" not in READ_REQUESTS
    assert is_public_read("set_node_priority") is False
    request = {"project_id": "game", "node_key": "a", "priority": 1, "expected_priority": 0,
               "reason": "authorized"}
    with pytest.raises(StateGraphError, match="read surface"):
        execute(service, "set_node_priority", dict(request))
    result = execute(service, "set_node_priority", dict(request), allow_write=True)
    assert result["changed"] is True and result["priority"] == 1
    with pytest.raises(StateGraphError):
        execute(service, "set_node_priority", {**request, "priority": 2,
                                               "director_identity": "bad\x01control"}, allow_write=True)


def test_serialized_concurrent_same_value_cas_has_one_winner(store):
    store.add_nodes("game", [node("a")])

    def attempt(_):
        try:
            return store.set_node_priority("game", "a", 7, 0, "race")
        except StateConflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert len([r for r in results if r and r["changed"]]) == 1
    assert store.get_node("game", "a")["priority"] == 7
    events = [e for e in snapshot(store)[3] if e["event_type"] == "node_priority_set"]
    assert len(events) == 1
