"""Real SQLite/SkillFlow waits with a deterministic overlap in observation."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import threading

import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode

from core import state_changes
from core.state_database import StateDatabase
from core.db_manager import DBManager
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager


def node(name):
    return {"key": name, "goal": name, "acceptance": [
        {"id": "check", "kind": "test", "description": "Synthetic check"}]}


@pytest.fixture
def system(tmp_path):
    sf = SkillFlow(str(tmp_path / "engine.sqlite"))
    sf.register_graph(PipelineGraph(name="work", begin="implement",
                                   steps=[StepNode(id="implement")]))
    service = StateService(DBManager(str(tmp_path / "state.sqlite")),
        WorkspaceManager(str(tmp_path / "workspaces")), sf, {}, project_read_trusted=True)
    for project in ("owner", "follow"):
        service.create_project(project, project)
        service.store.add_nodes(project, [node("a"), node("b")])
    owner, rid = workflow(service, "owner", "a")
    sf.pause_run(rid)  # Cached running, actual paused: recovery must consult SkillFlow.
    yield service, owner, rid
    assert str(service.db.db_path) not in state_changes._recovering
    assert str(service.db.db_path) not in state_changes._waiters
    sf._conn.close()


def workflow(service, project, key, state="running"):
    attempt = service.attempts.reserve(project, key, 1, "work", key)
    if state == "reserved":
        return attempt, None
    sf = service.sf
    rid = sf.create_run("work", project_id=attempt["execution_project_id"])
    sf.start_run(rid)
    service.attempts.bind_run(attempt["attempt_id"], rid, sf)
    if state in ("paused", "resumed", "candidate"):
        sf.pause_run(rid)
        service.attempts.reconcile(attempt["attempt_id"], sf)
        if state != "paused":
            sf.resume_run(rid)
    if state == "candidate":
        sf.advance_run(rid)
        step = sf.claim_next_step(rid)
        assert step is not None
        sf.confirm_step(step.token, StepResult(outputs={"implement": "synthetic"}))
        sf.advance_run(rid)
        service.attempts.reconcile(attempt["attempt_id"], sf)
    return service.attempts.get(attempt["attempt_id"]), rid


def cursor(service, project):
    return service.store.events(project)[-1]["seq"]


def other_service(service):
    return StateService(StateDatabase(service.db.db_path), service.ws, service.sf, {},
                        project_read_trusted=True)


def observe(service, attempt, status):
    body = json.dumps({"status": status, "settled": True, "usable": True}).encode()
    report = Path(service.db.db_path).parent / (attempt["attempt_id"] + ".json")
    report.write_bytes(body)
    service.external.observe(attempt["attempt_id"], "observed", 0, attempt["context_hash"],
        status, str(report), hashlib.sha256(body).hexdigest(), quiescent=status == "failed")


@asynccontextmanager
async def held_recovery(system, monkeypatch):
    service, owner, rid = system
    entered, release = threading.Event(), threading.Event()
    get_run = service.sf.get_run

    def held_get_run(run_id):
        if run_id == rid:
            entered.set()
            assert release.wait(4), "fixture must release the actual observer thread"
        return get_run(run_id)

    monkeypatch.setattr(service.sf, "get_run", held_get_run)
    owner_wait = asyncio.create_task(service.wait_for_state_change("owner",
        after=cursor(service, "owner"), attempt_ids=[owner["attempt_id"]],
        return_when_idle=True, timeout_seconds=3))
    try:
        assert await asyncio.to_thread(entered.wait, 1), "owner must hold real recovery"
        yield release
    finally:
        release.set()
        result = await asyncio.wait_for(owner_wait, 2)
        assert result["events"][-1]["payload"]["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("project", ["owner", "follow"])
async def test_external_only_wait_survives_other_recovery_and_replays_own_event(system, monkeypatch, project):
    service = system[0]
    external = service.external.register(project, "b", 1, "synthetic", "worker", "external")
    after = cursor(service, project)
    async with held_recovery(system, monkeypatch):
        waiter = asyncio.create_task(other_service(service).wait_for_state_change(project,
            after=after, attempt_ids=[external["attempt_id"]], return_when_idle=True,
            timeout_seconds=2))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), .12)
            observe(service, external, "failed")
            result = await asyncio.wait_for(waiter, 1)
            assert result["events"][-1]["payload"]["attempt_id"] == external["attempt_id"]
            assert result["events"][-1]["payload"]["status"] == "failed"
            assert result["next_after"] > after and "reason" not in result
            assert all(event["project_id"] == project for event in result["events"])
        finally:
            if not waiter.done():
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["reserved", "resumed", "paused", "candidate", "empty"])
async def test_busy_timeout_cannot_authorize_cached_workflow_or_idle(system, monkeypatch, state):
    service = system[0]
    target = None if state == "empty" else workflow(service, "follow", "a", state)[0]
    scope = [target["attempt_id"]] if target else ["no-such-attempt"]
    before = service.attempts.get(target["attempt_id"]) if target else None
    after = cursor(service, "follow")
    async with held_recovery(system, monkeypatch):
        result = await other_service(service).wait_for_state_change("follow", after=after,
            attempt_ids=scope, return_when_idle=True, timeout_seconds=.12)
        assert result == {"events": [], "next_after": after, "timed_out": True}
        if target:
            assert service.attempts.get(target["attempt_id"]) == before


@pytest.mark.asyncio
async def test_busy_wait_cancellation_leaves_owner_recovery_intact(system, monkeypatch):
    service = system[0]
    called = threading.Event()
    recover = state_changes.recover_page

    def observed(*args):
        result = recover(*args)
        if args[1] == "follow":
            called.set()
        return result

    monkeypatch.setattr(state_changes, "recover_page", observed)
    async with held_recovery(system, monkeypatch):
        waiter = asyncio.create_task(other_service(service).wait_for_state_change("follow",
            after=cursor(service, "follow"), return_when_idle=True, timeout_seconds=2))
        assert await asyncio.to_thread(called.wait, 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert str(service.db.db_path) in state_changes._recovering
        assert len(state_changes._waiters[str(service.db.db_path)]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "candidate", "empty"])
async def test_quiet_recovery_release_allows_fresh_disposition(system, monkeypatch, state):
    service = system[0]
    target = None if state == "empty" else workflow(service, "follow", "a", state)[0]
    scope = [target["attempt_id"]] if target else ["no-such-attempt"]
    async with held_recovery(system, monkeypatch) as release:
        waiter = asyncio.create_task(other_service(service).wait_for_state_change("follow",
            after=cursor(service, "follow"), attempt_ids=scope, return_when_idle=True,
            timeout_seconds=3))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), .12)
            release.set()
            result = await asyncio.wait_for(waiter, 2)
            assert not result["timed_out"] and not result["events"]
            assert result["reason"] == ("nothing_to_wait" if state == "empty" else "action_required")
            if target:
                assert result["attempts"][0]["attempt_id"] == target["attempt_id"]
        finally:
            if not waiter.done():
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter


@pytest.mark.asyncio
async def test_deadline_before_recovery_does_not_authorize_cached_idle(system, monkeypatch):
    service = system[0]
    scan = state_changes.scan

    def slow_scan(*args):
        result = scan(*args)
        threading.Event().wait(.08)
        return result

    monkeypatch.setattr(state_changes, "scan", slow_scan)
    after = cursor(service, "follow")
    result = await service.wait_for_state_change("follow", after=after,
        return_when_idle=True, timeout_seconds=.04)
    assert result == {"events": [], "next_after": after, "timed_out": True}


@pytest.mark.asyncio
async def test_busy_nonzero_recovery_cursor_waits_without_hotspin_and_reaches_last_page(system, monkeypatch):
    service = system[0]
    service.store.add_nodes("follow", [node("n" + str(i)) for i in range(11)])
    runs = [workflow(service, "follow", "n" + str(i)) for i in range(11)]
    service.sf.pause_run(runs[-1][1])  # Actual last-page change, not a cached projection.
    after = cursor(service, "follow")
    page_done, return_page = threading.Event(), threading.Event()
    calls = []
    recover = state_changes.recover_page

    def observed(*args):
        result = recover(*args)
        if args[1] == "follow":
            calls.append(args[2])
            if len(calls) == 1:
                page_done.set()
                assert return_page.wait(4)
        return result

    monkeypatch.setattr(state_changes, "recover_page", observed)
    waiter = asyncio.create_task(other_service(service).wait_for_state_change("follow",
        after=after, return_when_idle=True, timeout_seconds=3))
    try:
        assert await asyncio.to_thread(page_done.wait, 1)
        async with held_recovery(system, monkeypatch) as release:
            return_page.set()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), .15)
            assert 2 <= len(calls) <= 4, "busy page must sleep rather than spin"
            assert calls[1] > 0, "fixture must exercise an existing page cursor"
            release.set()
        result = await asyncio.wait_for(waiter, 2)
        assert result["events"][-1]["payload"]["attempt_id"] == runs[-1][0]["attempt_id"]
        assert result["events"][-1]["payload"]["status"] == "paused"
        assert "reason" not in result
    finally:
        return_page.set()
        if not waiter.done():
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter


@pytest.mark.asyncio
async def test_reentrant_observation_does_not_deadlock_or_lose_outer_result(system, monkeypatch):
    service, owner, rid = system
    get_run = service.sf.get_run
    calls = []

    def reentrant(run_id):
        if run_id == rid:
            calls.append(state_changes.recover_page(service, "follow", 0, None, None))
        return get_run(run_id)

    monkeypatch.setattr(service.sf, "get_run", reentrant)
    result = await asyncio.wait_for(service.wait_for_state_change("owner",
        after=cursor(service, "owner"), attempt_ids=[owner["attempt_id"]],
        return_when_idle=True, timeout_seconds=1), 2)
    assert calls
    assert result["events"][-1]["payload"]["status"] == "paused"
