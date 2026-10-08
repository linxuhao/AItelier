"""Stopping ONE per-owner scheduler must stop only that owner's work.

The 1e0 independent review found that every per-owner scheduler registers the
global `_cancel_detached_ticks` on EVENT_SCHEDULER_SHUTDOWN, and that listener
holds ALL owners' detached ticks: with Alice and Bob both running, stopping
only Alice cancelled Bob's in-flight ticks too (claims -> pending, retry 0,
release 1, tasks CANCELLED) while Bob's scheduler was still live.

These tests drive the real supported route — `start_user_scheduler` /
`stop_scheduler` with real per-owner jobs (real AsyncIOScheduler, interval,
`max_instances=1`, coroutine-registered) and real SkillFlow claim rows — with
two owners and a real claim for each. Source-only: delivered UNRUN, executed
later by the director's ROOT-owned CPU evaluator.
"""

import asyncio
import uuid

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.util import iscoroutinefunction_partial

from core import scheduler as sc

from tests.unit.test_demo_owner_pollers_admit_late_projects import (  # noqa: F401
    INTERVAL_S,
    MARGIN_S,
    OWNER,
    _real_sf,
    _wire_real_tick,
    _work_graph,
    env,
)


def _owner_settings():
    return {"scheduler_type": "interval", "scheduler_interval": INTERVAL_S}


async def test_owner_interval_job_is_registered_as_a_coroutine_job(env):
    """The poll job `_add_scheduler_job` builds for an owner must be one
    apscheduler itself recognizes as a coroutine and awaits.

    The lambda it replaced (`lambda: poll_and_execute_owner(owner)`) is a
    SYNCHRONOUS callable that merely returns the coroutine: apscheduler ran it
    as a sync job, the coroutine was never awaited, and the owner's projects
    were never polled. A manual direct-await in the test would say nothing
    about that — the property is the registration apscheduler sees.
    """
    sched = AsyncIOScheduler()
    sc._add_scheduler_job(sched, _owner_settings(), owner_email=OWNER)
    job = [j for j in sched.get_jobs()
           if isinstance(j.trigger, IntervalTrigger)
           and j.func is not sc._check_hung_claims]
    assert len(job) == 1, job
    assert job[0].max_instances == 1
    assert iscoroutinefunction_partial(job[0].func), (
        "the owner interval job is not registered as a coroutine function — "
        "apscheduler would run it as sync and drop the coroutine")


async def test_wake_scheduler_owner_date_job_is_registered_as_a_coroutine_job(
        env):
    """The wake-on-confirm immediate tick has the same registration surface:
    it must also be a coroutine job, not a lambda returning a coroutine."""
    sched = AsyncIOScheduler()
    sc._add_scheduler_job(sched, _owner_settings(), owner_email=OWNER)
    sched.start(paused=True)          # running, but jobs stay pending: no race
    sc._user_scheduler_map[OWNER] = sched
    try:
        sc.wake_scheduler(OWNER)
        date_jobs = [j for j in sched.get_jobs()
                     if j.func is not sc._check_hung_claims
                     and not isinstance(j.trigger, IntervalTrigger)]
        assert date_jobs, "wake_scheduler added no immediate owner job"
        assert all(iscoroutinefunction_partial(j.func) for j in date_jobs)
    finally:
        if sched.running:
            sched.shutdown(wait=False)


async def test_stopping_one_owner_scheduler_leaves_the_other_untouched(
        env, monkeypatch, tmp_path):
    """The real two-owner probe: Alice and Bob, each with their own started
    per-owner scheduler and one real claimed step in flight. Stopping ONLY
    Alice (the supported `stop_scheduler` route) cancels Alice's tick and
    releases her claim; Bob's scheduler stays live and his task, claim, retry
    and release counters are untouched."""
    sf = _real_sf(tmp_path)
    sf.register_graph(_work_graph())
    owners = {"alice": OWNER, "bob": "bob@example.com"}
    runs, pids = {}, {}
    for who, email in owners.items():
        pid = f"{who}-{uuid.uuid4().hex[:6]}"
        pids[who] = pid
        env.owners[pid] = email
        runs[pid] = sf.get_or_create_run("demo_owner_work_t", pid, {})
        sf.start_run(runs[pid])

    def rows():
        return {r["run_id"]: dict(r) for r in sf._conn.execute(
            "SELECT run_id, status, retry_count, release_count "
            "FROM skillflow_steps WHERE step_id = 'a'").fetchall()}

    all_claimed = asyncio.Event()

    async def execute(claimed):
        # Runner entry follows the real claim; signal only after both actual
        # rows exist and are claimed, so the stop cannot race dispatch.
        current = rows()
        if all(current[runs[pid]]["status"] == "claimed" for pid in pids.values()):
            all_claimed.set()
        await asyncio.Event().wait()          # an agent step that never ends
    _wire_real_tick(monkeypatch, sf, runs, execute)
    env.active.extend(pids.values())

    scheds = {}
    try:
        for who, email in owners.items():
            scheds[who] = sc.start_user_scheduler(email, _owner_settings())
        await asyncio.wait_for(all_claimed.wait(), timeout=INTERVAL_S + MARGIN_S)
        assert all(rows()[runs[pid]]["status"] == "claimed"
                   for pid in pids.values()), rows()
        alice_task = sc._detached_ticks.get(pids["alice"])
        bob_task = sc._detached_ticks.get(pids["bob"])
        assert alice_task is not None and bob_task is not None, sc._detached_ticks

        sc.stop_scheduler(OWNER)              # stop ONLY Alice
        done, pending = await asyncio.wait([alice_task], timeout=5)
        assert done and all(t.cancelled() for t in done), (
            "Alice's tick was not cancelled by her scheduler's stop")
        assert not bob_task.done(), (
            "Bob's live task was cancelled with Alice's scheduler")

        bob_row = rows()[runs[pids["bob"]]]
        assert bob_row["status"] == "claimed", (
            f"Bob's claim was released by Alice's stop: {bob_row}")
        assert bob_row["retry_count"] == 0 and bob_row["release_count"] == 0, (
            f"Bob's retry/release budget was charged by Alice's stop: {bob_row}")
        alice_row = rows()[runs[pids["alice"]]]
        assert alice_row["status"] == "pending", (
            f"Alice's claim was not handed back: {alice_row}")
        assert alice_row["retry_count"] == 0, (
            f"retry budget charged on stop: {alice_row}")
        assert alice_row["release_count"] == 1, (
            f"not released via release_claim: {alice_row}")
        assert scheds["bob"].running, "Bob's scheduler died with Alice's"
        assert OWNER not in sc._user_scheduler_map, "Alice's map entry left"
        assert pids["alice"] not in sc._detached_ticks, "Alice's slot left"
        assert pids["bob"] in sc._detached_ticks, "Bob's slot was dropped"
    finally:
        for task in (sc._detached_ticks.get(pids["alice"]),
                     sc._detached_ticks.get(pids["bob"]),
                     locals().get("alice_task"), locals().get("bob_task")):
            if task is not None and not task.done():
                task.cancel()
        for s in scheds.values():
            if s.running:
                s.shutdown(wait=False)
        await asyncio.sleep(0.1)
