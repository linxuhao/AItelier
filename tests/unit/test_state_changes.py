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
            json={"project_id": "p", "timeout_seconds": 61})
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
