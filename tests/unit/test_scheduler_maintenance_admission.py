"""Ended-run maintenance must not own the APScheduler admission job.

The observed inventory had no held leases/running runs but 411 released roots
still to check. Gate an actual reaper observation over that inventory while
the real interval job admits newly activated projects. No LLMs or sidecar.
"""
import asyncio
import threading
import time
from collections import Counter
from types import SimpleNamespace

import pytest
from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from skillflow import PipelineGraph, SkillFlow
from skillflow.graph import StepNode

import api.dependencies as deps
from core import run_isolation as ri, scheduler as sc
from core.db_manager import DBManager


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    db = DBManager(str(tmp_path / "host.db"))
    sf = SkillFlow(str(tmp_path / "engine.db"))
    sf.register_graph(PipelineGraph(name="maintenance", begin="a",
                                    steps=[StepNode(id="a")]))
    monkeypatch.setattr(sc, "db", db)
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    monkeypatch.setattr(deps, "get_config_registry", lambda: SimpleNamespace(
        list=lambda: [SimpleNamespace(config_name="maintenance", scheduler_owned=True)]))
    monkeypatch.setenv("AITELIER_ZVEC_LIFECYCLE", "0")
    monkeypatch.setattr(sc, "_lease_sweep_future", None)
    monkeypatch.setattr(sc, "_last_lease_sweep", 0.0)
    monkeypatch.setattr(sc, "_LEASE_SWEEP_INTERVAL_S", 60.0)
    monkeypatch.setattr(sc, "_lease_sweep_reported", {})
    monkeypatch.setattr(sc, "_detached_ticks", {})
    monkeypatch.setattr(sc, "_detached_tick_owners", {})
    monkeypatch.setattr(sc, "_tick_locks", {})
    monkeypatch.setattr(sc, "_scheduler_instance", None)
    monkeypatch.setattr(sc, "MAX_CONCURRENT_PROJECTS", 2)
    # All records are private fixture data; missing sources must be retained.
    ids = [sf.create_run("maintenance", project_id=f"ended-{i}") for i in range(411)]
    with sf._conn:
        sf._conn.executemany("UPDATE skillflow_runs SET status=? WHERE id=?",
                            [("completed" if i < 324 else "failed", rid)
                             for i, rid in enumerate(ids)])
    with db.get_connection() as conn:
        conn.executemany("""INSERT INTO run_isolation
            (run_id,project_id,config_name,mode,source_repo,worktree_path,
             released_at,disposition) VALUES (?,?,?,?,?,?,datetime('now'),?)""",
            [(rid, f"ended-{i}", "maintenance",
              "read_snapshot" if i < 109 else "worktree",
              str(tmp_path / "missing-source"), str(tmp_path / f"missing-root-{i}"),
              "auto_released_terminal_quiet") for i, rid in enumerate(ids)])
        conn.commit()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    real = ri._source_current_ref

    def slow_observation(rec):
        calls.append(rec["run_id"])
        if len(calls) == 1:
            entered.set()
            release.wait(12)
        return real(rec)

    monkeypatch.setattr(ri, "_source_current_ref", slow_observation)
    sweep = sc._sweep_ended_leases

    def observe_completion():
        try:
            sweep()
        finally:
            finished.set()

    monkeypatch.setattr(sc, "_sweep_ended_leases", observe_completion)

    def activate(pid, owner="one"):
        sf.create_run("maintenance", project_id=pid)
        with sf._conn:
            sf._conn.execute("UPDATE skillflow_runs SET status='running' WHERE project_id=?", (pid,))
        db.ensure_project(pid, owner_email=owner, config_name="maintenance")
        with db.get_connection() as conn:
            conn.execute("UPDATE runs SET status='running' WHERE project_id=?", (pid,))
            conn.commit()

    yield SimpleNamespace(db=db, sf=sf, entered=entered, release=release,
                          finished=finished, calls=calls, activate=activate)
    release.set()
    assert sc._lease_sweep_future is None, "maintenance ownership escaped teardown"
    sf._conn.close()


async def until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "demo", "owner"])
async def test_real_single_instance_poller_admits_during_slow_reaper(inventory, monkeypatch, kind):
    e = inventory
    counts, outcomes, aps_events = Counter(), [], []
    hold = asyncio.Event()

    async def tick(pid, loop):
        counts[pid] += 1
        await hold.wait()

    monkeypatch.setattr(sc, "_run_skillflow_tick", tick)
    real_log = sc.tick_log

    def log(pid, outcome, **detail):
        outcomes.append((pid, outcome))
        real_log(pid, outcome, **detail)

    monkeypatch.setattr(sc, "tick_log", log)
    scheduler = AsyncIOScheduler()
    sc._add_scheduler_job(scheduler, {"scheduler_type": "interval", "scheduler_interval": 1},
                          owner_email="one" if kind == "owner" else None,
                          demo=kind == "demo")
    poll = [j for j in scheduler.get_jobs() if j.id != "hung" and
            "poll_and_execute" in repr(j.func)]
    assert len(poll) == 1 and poll[0].max_instances == 1
    scheduler.add_listener(lambda ev: aps_events.append(ev.code),
                           EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED)
    scheduler.start()
    try:
        await until(e.entered.is_set)
        assert e.entered.is_set(), "did not reach the actual ended-root reaper"
        e.activate("new-eligible")
        activated = time.monotonic()
        await until(lambda: counts["new-eligible"] == 1)
        elapsed = time.monotonic() - activated
        print(f"kind={kind} admission_seconds={elapsed:.3f} "
              f"counts={dict(counts)} aps_events={aps_events} "
              f"maintenance_finished={e.finished.is_set()}")
        assert counts["new-eligible"] == 1, (
            "new eligible project was not admitted during slow maintenance; "
            f"APS max-instance skips={aps_events.count(EVENT_JOB_MAX_INSTANCES)}")
        assert not e.release.is_set() and not e.finished.is_set()
        e.activate("late-eligible")
        e.activate("over-cap")
        if kind == "owner":
            e.activate("foreign", owner="two")
        await until(lambda: counts["late-eligible"] == 1)
        assert counts["late-eligible"] == 1 and len(sc._detached_ticks) == 2
        await asyncio.sleep(1.1)  # another genuine interval, with both slots held
        assert counts == {"new-eligible": 1, "late-eligible": 1}
        assert ("new-eligible", "locked") in outcomes
        assert ("over-cap", "at_capacity") in outcomes
        assert EVENT_JOB_MAX_INSTANCES not in aps_events and EVENT_JOB_MISSED not in aps_events
        assert len(e.calls) == 1, "overlapping polls started duplicate maintenance"
        if kind == "owner":
            # Another owner's shutdown cannot cancel this owner's admitted work.
            assert sc._cancel_detached_ticks(owner_email="two") == 0
            assert all(not task.done() for task in sc._detached_ticks.values())
            assert "foreign" not in counts
    finally:
        scheduler.shutdown(wait=False)
        hold.set()
        e.release.set()
        await sc.settle_scheduler_maintenance()
        await until(lambda: not sc._detached_ticks)
    assert e.finished.is_set() and len(e.calls) == 411
    assert len(ri.retained(e.db)) == 411
    assert all(r["disposition"] == "auto_released_terminal_quiet" for r in ri.retained(e.db))
    with e.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM checkout_leases").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_returned_poll_keeps_worker_owned_and_reports_unawaited_error(inventory, monkeypatch, caplog):
    e = inventory
    workers = []

    def fail():
        workers.append(threading.get_ident())
        e.entered.set()
        e.release.wait(3)
        e.finished.set()
        raise RuntimeError("unawaited-maintenance-failure")

    monkeypatch.setattr(sc, "_sweep_ended_leases", fail)
    poll = asyncio.create_task(sc.poll_and_execute())
    try:
        await until(e.entered.is_set)
        assert await asyncio.wait_for(poll, .5) == []
        future = sc._lease_sweep_future
        assert future is not None and not future.done()
        assert not poll.cancel(), "an already returned poll should own no worker cancellation"
        await sc.poll_and_execute_demo()
        await sc.poll_and_execute_owner("another-owner")
        assert len(workers) == 1 and sc._lease_sweep_future is future
        e.release.set()
        await until(lambda: sc._lease_sweep_future is None)
        assert e.finished.is_set() and future.done()
        messages = [r for r in caplog.records if r.message == "lease sweep failed"]
        assert len(messages) == 1 and "unawaited-maintenance-failure" in caplog.text
    finally:
        e.release.set()
        await sc.settle_scheduler_maintenance()
