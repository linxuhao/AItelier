import asyncio

import pytest

from core.state_database import StateDatabase
from core.state_service import StateService
from core.state_commands import execute
from core.state_changes import _waiters


@pytest.fixture
def service(tmp_path):
    service = StateService(StateDatabase(str(tmp_path / "state.sqlite")))
    service.create_project("p", "Project")
    service.store.add_nodes("p", [{"key": "a", "goal": "A", "acceptance": [
        {"id": "check", "kind": "test", "description": "Test"}]}])
    return service


@pytest.mark.asyncio
async def test_replay_filter_paging_and_timeout(service):
    result = await execute(service, "wait_for_state_change", {"project_id": "p", "limit": 1})
    assert len(result["events"]) == 1 and not result["timed_out"]
    second = StateService(StateDatabase(service.db.db_path))
    page = await second.wait_for_state_change("p", after=result["next_after"], timeout_seconds=0)
    assert [e["event_type"] for e in page["events"]] == ["node_created"]
    timed = await second.wait_for_state_change("p", after=page["next_after"], timeout_seconds=.02)
    assert timed == {"events": [], "next_after": page["next_after"], "timed_out": True}
    assert not _waiters


@pytest.mark.asyncio
async def test_wait_commit_and_cancellation_cleanup(service):
    after = service.store.events("p")[-1]["seq"]
    wait = asyncio.create_task(service.wait_for_state_change("p", after=after, node_keys=["a"]))
    await asyncio.sleep(.02)
    service.store.revise_node("p", "a", 1, "new requirement", goal="Changed")
    result = await asyncio.wait_for(wait, 1)
    assert result["events"][0]["event_type"] == "node_revised"
    before_cancel = service.store.get_node("p", "a")
    wait = asyncio.create_task(service.wait_for_state_change("p", after=result["next_after"]))
    await asyncio.sleep(.02)
    wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait
    assert not _waiters
    assert service.store.get_node("p", "a") == before_cancel


@pytest.mark.asyncio
async def test_cross_process_fallback_and_quiet_filter(service):
    after = service.store.events("p")[-1]["seq"]
    wait = asyncio.create_task(service.wait_for_state_change("p", after=after, timeout_seconds=2))
    await asyncio.sleep(.02)
    # Raw independent connection deliberately bypasses process-local notifications.
    with service.db.get_connection() as conn:
        service.store._event(conn, "p", "a", "attempt_observed", {"attempt_id": "x", "status": "running"})
        service.store._event(conn, "p", "a", "attempt_observed", {"attempt_id": "x", "status": "paused"})
        conn.commit()
    result = await wait
    assert len(result["events"]) == 1
    assert result["events"][0]["payload"]["status"] == "paused"
    all_events = await service.wait_for_state_change("p", after=after, actionable_only=False, attempt_ids=["x"])
    assert len(all_events["events"]) == 2
    assert (await service.wait_for_state_change("p", after=after, attempt_ids=["other"], timeout_seconds=0))["timed_out"]


@pytest.mark.asyncio
async def test_commit_during_read_has_no_lost_wakeup(service, monkeypatch):
    from core import state_changes
    original = state_changes.scan
    after = service.store.events("p")[-1]["seq"]
    def racing_scan(*args):
        result = original(*args)
        if not result[0]:
            service.store.revise_node("p", "a", 1, "race", goal="New")
        return result
    monkeypatch.setattr(state_changes, "scan", racing_scan)
    result = await service.wait_for_state_change("p", after=after, timeout_seconds=.5)
    assert result["events"][0]["event_type"] == "node_revised"


@pytest.mark.asyncio
async def test_unchanged_write_does_not_notify(service):
    from core.state_changes import subscribe
    with subscribe(service.db.db_path) as signal:
        with service.store.transaction(write=True):
            pass
        await asyncio.sleep(0)
        assert not signal.is_set()


@pytest.mark.asyncio
async def test_replayed_event_precedes_slow_recovery(service, monkeypatch):
    from core import state_changes
    def forbidden(*args):
        raise AssertionError("existing events must return before runtime observation")
    service.runtime_factory = forbidden
    monkeypatch.setattr(state_changes, "recover_page", forbidden)
    result = await service.wait_for_state_change("p")
    assert result["events"]


@pytest.mark.asyncio
async def test_http_wait_contract(service):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from api.state_http import create_state_router
    app = FastAPI()
    app.include_router(create_state_router(lambda: service, lambda: None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/state/query/wait_for_state_change",
            json={"project_id": "p", "timeout_seconds": 0})
        assert response.status_code == 200
        assert response.json()["events"]
        invalid = await client.post("/api/state/query/wait_for_state_change",
            json={"project_id": "p", "timeout_seconds": 901})
        assert invalid.status_code == 422
        missing = await client.post("/api/state/query/wait_for_state_change",
            json={"project_id": "missing", "timeout_seconds": 0})
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_child_wait_sees_dependency_and_project_events(service):
    service.store.add_nodes("p", [{"key": "b", "goal": "Child", "dependencies": ["a"], "acceptance": [
        {"id": "check", "kind": "test", "description": "Test"}]}])
    cursor = service.store.events("p")[-1]["seq"]
    service.store.revise_node("p", "a", 1, "dependency change", goal="Parent revised")
    result = await service.wait_for_state_change("p", after=cursor, node_keys=["b"], timeout_seconds=0)
    assert result["events"][0]["node_key"] == "a"
    assert "b" in result["events"][0]["payload"]["invalidated"]
    with service.store.transaction(write=True) as conn:
        service.store._event(conn, "p", None, "dispatch_policy_changed", {"enabled": False})
    project = await service.wait_for_state_change("p", after=result["next_after"], node_keys=["b"], timeout_seconds=0)
    assert project["events"][0]["event_type"] == "dispatch_policy_changed"

@pytest.mark.asyncio
async def test_long_wait_wakes_on_change_and_cleans_up(service):
    cursor = service.store.events("p")[-1]["seq"]
    wait = asyncio.create_task(service.wait_for_state_change("p", after=cursor, timeout_seconds=900))
    await asyncio.sleep(.02)
    service.store.revise_node("p", "a", 1, "changed during long wait", goal="Updated")
    result = await asyncio.wait_for(wait, 1)
    assert not result["timed_out"]
    assert result["events"][0]["event_type"] == "node_revised"
    assert result["next_after"] > cursor
    assert not _waiters


def test_wait_bounds_and_compatibility_default():
    from pydantic import ValidationError
    from core.state_commands import WaitForStateChange
    assert WaitForStateChange(project_id="p").timeout_seconds == 30
    assert WaitForStateChange(project_id="p", timeout_seconds=900).timeout_seconds == 900
    for invalid in (-1, 901, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            WaitForStateChange(project_id="p", timeout_seconds=invalid)


@pytest.mark.asyncio
async def test_director_empty_idle_and_zero_timeout(service):
    cursor = service.store.events("p")[-1]["seq"]
    for timeout in (0, 900):
        result = await asyncio.wait_for(execute(service, "wait_for_state_change", {
            "project_id": "p", "after": cursor, "timeout_seconds": timeout,
            "return_when_idle": True}), 1)
        assert result == {"events": [], "next_after": cursor, "timed_out": False,
                          "reason": "nothing_to_wait"}
    assert not _waiters


def register_external(service):
    return service.external.register("p", "a", 1, "test", "worker", "request")


def observe_external(service, attempt, status):
    return service.external.observe(attempt["attempt_id"], "report", 0,
        attempt["context_hash"], status, "private-report", "a" * 64,
        quiescent=status == "failed")


@pytest.mark.asyncio
async def test_director_terminal_event_precedes_idle(service):
    attempt = register_external(service)
    cursor = service.store.events("p")[-1]["seq"]
    observe_external(service, attempt, "failed")
    result = await service.wait_for_state_change("p", after=cursor, return_when_idle=True)
    assert result["events"][0]["payload"]["status"] == "failed"
    assert "reason" not in result
    idle = await service.wait_for_state_change("p", after=result["next_after"], return_when_idle=True)
    assert idle["reason"] == "nothing_to_wait"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "unknown"])
async def test_director_external_waits_until_event(service, status):
    attempt = register_external(service)
    if status == "unknown":
        observe_external(service, attempt, status)
    cursor = service.store.events("p")[-1]["seq"]
    result = await service.wait_for_state_change("p", after=cursor,
        return_when_idle=True, timeout_seconds=.02)
    assert result == {"events": [], "next_after": cursor, "timed_out": True}


@pytest.mark.asyncio
async def test_director_paused_is_actionable_after_event_consumed(service):
    attempt = register_external(service)
    observe_external(service, attempt, "paused")
    cursor = service.store.events("p")[-1]["seq"]
    result = await service.wait_for_state_change("p", after=cursor, return_when_idle=True)
    assert result["reason"] == "action_required"
    assert result["attempts"] == [{"attempt_id": attempt["attempt_id"], "status": "paused"}]
    assert not result["timed_out"]


@pytest.mark.asyncio
async def test_director_filters_include_upstream_and_intersect_attempts(service):
    attempt = register_external(service)
    service.store.add_nodes("p", [
        {"key": "b", "goal": "Child", "dependencies": ["a"], "acceptance": [
            {"id": "check", "kind": "test", "description": "Test"}]},
        {"key": "c", "goal": "Other", "acceptance": [
            {"id": "check", "kind": "test", "description": "Test"}]}])
    cursor = service.store.events("p")[-1]["seq"]
    for nodes, attempts, idle in [(["b"], None, False), (["c"], None, True),
            (["b"], [attempt["attempt_id"]], False), (["c"], [attempt["attempt_id"]], True)]:
        result = await service.wait_for_state_change("p", after=cursor, node_keys=nodes,
            attempt_ids=attempts, return_when_idle=True, timeout_seconds=0)
        assert (result.get("reason") == "nothing_to_wait") is idle
        assert result["timed_out"] is not idle


@pytest.mark.asyncio
async def test_director_reservation_is_not_idle(service):
    service.attempts.reserve("p", "a", 1, "workflow", "request")
    cursor = service.store.events("p")[-1]["seq"]
    result = await service.wait_for_state_change("p", after=cursor,
        return_when_idle=True, timeout_seconds=0)
    assert result["timed_out"] and "reason" not in result


@pytest.mark.asyncio
async def test_director_commit_between_scan_and_disposition_replays_first(service, monkeypatch):
    from core import state_changes
    original = state_changes.wait_disposition
    cursor = service.store.events("p")[-1]["seq"]
    def racing_disposition(*args):
        service.store.revise_node("p", "a", 1, "race", goal="New")
        return original(*args)
    monkeypatch.setattr(state_changes, "wait_disposition", racing_disposition)
    result = await service.wait_for_state_change("p", after=cursor, return_when_idle=True)
    assert result["events"][0]["event_type"] == "node_revised"
