import asyncio
import concurrent.futures
import threading

import pytest

from core.state_database import StateDatabase
from core.state_graph import StateConflict
from core.state_service import StateService


@pytest.fixture
def services(tmp_path):
    db_path = str(tmp_path / "state.sqlite")
    first = StateService(StateDatabase(db_path), actor="alice@example.test")
    first.create_project("aitelier", "AItelier")
    first.create_project("wuxia-myth", "Wuxia")
    return first, StateService(StateDatabase(db_path), actor="bob@example.test")


def test_project_scoped_sections_history_and_provenance(services):
    alice, bob = services
    a = alice.driver_notes.update(
        "aitelier", "permanent", "release rules", 0, "aitelier-director")
    w = bob.driver_notes.update(
        "wuxia-myth", "temporary", "combat batch", 0, "wuxia-director")
    assert a["revision"] == w["revision"] == 1
    assert a["permanent"] == "release rules" and a["temporary"] == ""
    assert w["temporary"] == "combat batch" and w["permanent"] == ""
    assert a["updated_by"] == {
        "actor": "alice@example.test", "director_identity": "aitelier-director"}
    assert w["updated_by"] == {
        "actor": "bob@example.test", "director_identity": "wuxia-director"}

    appended = alice.driver_notes.update(
        "aitelier", "temporary", "one", 1, "aitelier-director", "append")
    appended = bob.driver_notes.update(
        "aitelier", "temporary", " + two", 2, "relief-director", "append")
    assert appended["temporary"] == "one + two"
    history = alice.driver_notes.history("aitelier")
    assert [entry["revision"] for entry in history["entries"]] == [1, 2, 3]
    assert history["entries"][-1]["actor"] == "bob@example.test"
    assert alice.driver_notes.history("wuxia-myth")["entries"][0]["temporary"] == "combat batch"
    reopened = StateService(StateDatabase(alice.db.db_path), actor="handoff@example.test")
    assert reopened.driver_notes.get("aitelier")["temporary"] == "one + two"


def test_compare_and_swap_rejects_one_concurrent_writer_without_lost_update(services):
    alice, bob = services
    barrier = threading.Barrier(2)

    def update(service, value, identity):
        barrier.wait()
        try:
            return ("ok", service.driver_notes.update(
                "aitelier", "temporary", value, 0, identity))
        except StateConflict as exc:
            return ("conflict", str(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result() for future in [
                pool.submit(update, alice, "alice", "director-a"),
                pool.submit(update, bob, "bob", "director-b"),
            ]
        ]
    assert sorted(result[0] for result in results) == ["conflict", "ok"]
    conflict = next(result[1] for result in results if result[0] == "conflict")
    assert "expected 0, current 1" in conflict
    current = alice.driver_notes.get("aitelier")
    assert current["revision"] == 1
    assert current["temporary"] in {"alice", "bob"}
    assert len(alice.driver_notes.history("aitelier")["entries"]) == 1


@pytest.mark.asyncio
async def test_note_wait_is_project_scoped_and_wakes_from_any_selected_source(services):
    a, b = services
    a.store.add_nodes("aitelier", [
        {"key": "selected", "goal": "Selected", "acceptance": [
            {"id": "check", "kind": "test", "description": "check"}]},
        {"key": "other", "goal": "Other", "acceptance": [
            {"id": "check", "kind": "test", "description": "check"}]},
    ])
    attempt = a.external.register(
        "aitelier", "other", 1, "test", "worker", "request")
    cursor = a.store.events("aitelier")[-1]["seq"]

    note_wait = asyncio.create_task(a.wait_for_state_change(
        "aitelier", after=cursor, note_after_revision=0,
        return_when_idle=True, timeout_seconds=1))
    await asyncio.sleep(.02)
    project_cursor = a.store.events("aitelier")[-1]["seq"]
    b.driver_notes.update("wuxia-myth", "temporary", "foreign", 0, "wuxia-director")
    assert a.store.events("aitelier")[-1]["seq"] == project_cursor
    await asyncio.sleep(.05)
    assert not note_wait.done()
    a.driver_notes.update("aitelier", "temporary", "local", 0, "aitelier-director")
    changed = await asyncio.wait_for(note_wait, 1)
    assert changed["events"][0]["event_type"] == "driver_note_updated"
    assert changed["events"][0]["project_id"] == "aitelier"

    cursor = changed["next_after"]
    a.external.observe(attempt["attempt_id"], "paused", 0, attempt["context_hash"],
        "paused", "private/report", "a" * 64)
    any_result = await a.wait_for_state_change(
        "aitelier", after=cursor, node_keys=["selected"],
        attempt_ids=[attempt["attempt_id"]], filter_mode="any", timeout_seconds=0)
    assert any_result["events"][0]["payload"]["attempt_id"] == attempt["attempt_id"]
    consumed = a.store.events("aitelier")[-1]["seq"]
    snapshot = await a.wait_for_state_change(
        "aitelier", after=consumed, note_after_revision=1,
        attempt_ids=[attempt["attempt_id"]], filter_mode="any",
        return_when_idle=True, timeout_seconds=0)
    assert snapshot["reason"] == "action_required"
    assert snapshot["attempts"][0]["attempt_id"] == attempt["attempt_id"]


@pytest.mark.asyncio
async def test_note_revision_is_a_live_condition_but_plain_idle_stays_fast(services):
    service, _ = services
    cursor = service.store.events("aitelier")[-1]["seq"]
    idle = await service.wait_for_state_change(
        "aitelier", after=cursor, return_when_idle=True, timeout_seconds=900)
    assert idle["reason"] == "nothing_to_wait" and not idle["timed_out"]

    pending = await service.wait_for_state_change(
        "aitelier", after=cursor, note_after_revision=0,
        return_when_idle=True, timeout_seconds=.02)
    assert pending["timed_out"] and "reason" not in pending
    service.driver_notes.update(
        "aitelier", "permanent", "ready", 0, "aitelier-director")
    already = await service.wait_for_state_change(
        "aitelier", after=service.store.events("aitelier")[-1]["seq"],
        note_after_revision=0, return_when_idle=True, timeout_seconds=0)
    assert already["reason"] == "driver_note_changed"
    assert already["note_revision"] == 1
