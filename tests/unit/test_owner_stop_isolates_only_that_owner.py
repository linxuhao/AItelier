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
import threading
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


# How long a row waits for the claim handback to become OBSERVABLE. The release
# write runs inside the cancelled tick, but it is a database write, and what the
# row asserts is a fact about the ROW once that write landed. Sampling the
# instant `stop_scheduler` returned let the assertion race the write: under load
# the read saw `status='claimed', release_count=0` while the handback was still
# on its way, and the row called a healthy product broken. Bounded, so a
# scheduler that really does stop without releasing still makes the row RED
# (after this long) instead of hanging the suite.
_RELEASE_TIMEOUT_S = 10.0
_POLL_S = 0.02


async def _awaited(read, predicate):
    """Poll `read()` until `predicate(value)`, bounded; return the last value.

    A witness waits for the observation it measures. The deadline is a ceiling,
    never the verdict: what the caller asserts afterwards is still a fact about
    the product, and when it never becomes true the row fails on the value the
    poll last saw, naming the wait it gave up on.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RELEASE_TIMEOUT_S
    value = read()
    while not predicate(value):
        if loop.time() >= deadline:
            break
        await asyncio.sleep(_POLL_S)
        value = read()
    return value


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
    and release counters are untouched.

    The handback is waited for, bounded, because it is a WRITE: the verdict this
    row reaches is about the claim row, so it waits for the row to say so rather
    than for the cancellation to return."""
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
    parked: dict = {}

    async def execute(claimed):
        # TOKEN POSSESSION, not a row read. Entering the runner is what proves a
        # tick came back from its claim await WITH the token. A row can read
        # `claimed` while its tick is still parked at that await — the claim is
        # committed on the worker thread of the `to_thread` hop — and a stop
        # delivered there cancels the tick before it ever holds the token. The
        # gate this replaced read the ROWS, so it proved both claims had been
        # COMMITTED, not that both ticks had returned from their claim, and its
        # "so the stop cannot race dispatch" was therefore false. The window
        # itself is covered deterministically by
        # test_a_cancel_delivered_at_the_claim_await_hands_the_claim_back.
        parked[asyncio.current_task()] = claimed
        if len(parked) == len(pids):
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
        assert {alice_task, bob_task} <= set(parked), (
            "the task holding a claim token is not the task this row stops: "
            f"parked={list(parked)}")

        sc.stop_scheduler(OWNER)              # stop ONLY Alice
        done, pending = await asyncio.wait([alice_task], timeout=5)
        assert done and all(t.cancelled() for t in done), (
            "Alice's tick was not cancelled by her scheduler's stop")
        assert not bob_task.done(), (
            "Bob's live task was cancelled with Alice's scheduler")

        # THE CANCELLATION IS THE TRIGGER, NOT THE OBSERVATION. Alice's claim is
        # handed back by the code that runs while her task unwinds, and that
        # handback is a database write: reading the row the instant the task is
        # cancelled samples a moment the write may not have reached yet. Wait,
        # bounded, for the condition this row asserts - exactly once - and then
        # assert it.
        alice_row = await _awaited(
            lambda: rows()[runs[pids["alice"]]],
            lambda row: row["status"] == "pending" and row["release_count"] == 1)
        bob_row = rows()[runs[pids["bob"]]]
        assert bob_row["status"] == "claimed", (
            f"Bob's claim was released by Alice's stop: {bob_row}")
        assert bob_row["retry_count"] == 0 and bob_row["release_count"] == 0, (
            f"Bob's retry/release budget was charged by Alice's stop: {bob_row}")
        assert alice_row["status"] == "pending", (
            f"Alice's claim was not handed back within {_RELEASE_TIMEOUT_S:g}s: "
            f"{alice_row}")
        assert alice_row["retry_count"] == 0, (
            f"retry budget charged on stop: {alice_row}")
        assert alice_row["release_count"] == 1, (
            f"not released via release_claim within {_RELEASE_TIMEOUT_S:g}s: "
            f"{alice_row}")
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


def _row(sf, run_id):
    """The one step row of `run_id` as a dict (None when there is no row)."""
    with sf._lock:
        found = sf._conn.execute(
            "SELECT id, status, retry_count, release_count, version "
            "FROM skillflow_steps WHERE run_id = ?", (run_id,)).fetchone()
    return None if found is None else dict(found)


async def test_a_cancel_delivered_at_the_claim_await_hands_the_claim_back(
        env, monkeypatch, tmp_path):
    """A cancellation that lands on the claim's own await must hand the claim back.

    The deterministic counterpart of the pair row's 1-in-10 race. The claim is
    committed by the WORKER THREAD of `asyncio.to_thread(sf.claim_next_step,
    run_id)`, and the tick only binds the token once that hop resolves back onto
    the loop. A scheduler shutdown can cancel the tick in between
    (`AsyncIOScheduler.shutdown` defers its own event through
    `call_soon_threadsafe`, so it can beat the tick's resumption), and the
    CancelledError is then delivered at that await — OUTSIDE the `try:` around
    `runner.execute` whose `except asyncio.CancelledError` calls
    `release_claim_on_cancel`. Before the fix (iss-46fde53a9f2d4211) the tick
    died there and the row stayed `claimed` / `release_count 0` for good: the
    reaper refuses a claim whose owner process is alive, and the owner is this
    server.

    This row does not sample that race, it enters the window by construction:
    the claim worker is parked AFTER the commit, the cancellation is delivered
    while it is still parked, and the unpark is queued on the loop BEHIND the
    tick's own cancellation callback — so the order (cancel delivered first,
    worker returns second) is fixed by the loop's FIFO ready queue, not by
    timing. What it then asserts is the product fact: `pending`,
    `release_count 1`, `retry_count 0`, and `release_claim` called on exactly
    the row the worker committed.
    """
    sf = _real_sf(tmp_path)
    sf.register_graph(_work_graph())
    pid = f"alice-{uuid.uuid4().hex[:6]}"
    env.owners[pid] = OWNER
    run_id = sf.get_or_create_run("demo_owner_work_t", pid, {})
    sf.start_run(run_id)
    env.active.append(pid)

    released = []
    engine_release = sf.release_claim

    def release_spy(token, reason):
        # Delegate to the real engine: what this row asserts is the ROW it
        # produces, plus which token it was produced from.
        result = engine_release(token, reason)
        released.append((getattr(token, "step_instance_id", None), reason,
                         result))
        return result
    monkeypatch.setattr(sf, "release_claim", release_spy, raising=False)

    reached_runner = []

    async def execute(claimed):
        # Reaching the runner means the tick resumed with its token, so the
        # cancellation did NOT land in the claim's commit window — this row
        # would then be witnessing the ordinary execute-block handback instead.
        # Recorded rather than raised: a raise inside a detached tick is only
        # logged, so the row asserts on this afterwards.
        reached_runner.append(claimed)
        await asyncio.Event().wait()          # an agent step that never ends
    _wire_real_tick(monkeypatch, sf, {pid: run_id}, execute)

    loop = asyncio.get_running_loop()
    committed = asyncio.Event()       # set from the worker thread, loop-side wait
    let_go = threading.Event()        # the only thing that unparks the worker
    worker: dict = {}
    engine_claim = sf.claim_next_step

    def claim_then_park(rid):
        worker["thread"] = threading.get_ident()
        result = engine_claim(rid)
        worker["committed"] = True
        loop.call_soon_threadsafe(committed.set)
        # The window: the row is already `claimed`, and this tick has not
        # resumed with the token. Parked on a threading.Event, not a sleep, so
        # the test decides when — and how — the window closes.
        if not let_go.wait(timeout=_RELEASE_TIMEOUT_S * 2):
            raise AssertionError("the test never released the claim worker")
        worker["returned"] = True
        return result
    monkeypatch.setattr(sf, "claim_next_step", claim_then_park, raising=False)

    task = None
    try:
        started = sc._dispatch_detached_ticks([{"project_id": pid}], loop,
                                             owner_email=OWNER)
        assert len(started) == 1, started
        task = sc._detached_ticks[pid]

        await asyncio.wait_for(committed.wait(), timeout=5.0)
        at_commit = _row(sf, run_id)
        assert at_commit is not None, "the claim committed no step row"
        assert at_commit["status"] == "claimed", at_commit
        assert worker.get("thread") != threading.get_ident(), (
            "the claim did not cross a thread hop, so this row tests nothing")
        assert not worker.get("returned"), (
            "the worker already returned the token to the tick; the "
            "cancellation would land after the window")

        task.cancel()
        # Queued on the same loop, i.e. FIFO AFTER the wakeup `task.cancel()`
        # just enqueued: the tick is cancelled first (that is the window), the
        # worker is let go second. Deterministic, not a race.
        loop.call_soon(let_go.set)

        done, pending = await asyncio.wait([task], timeout=_RELEASE_TIMEOUT_S)
        assert done and task.cancelled(), (
            f"the cancelled tick did not finish: done={done} pending={pending}")
        assert not reached_runner, (
            "the cancelled tick reached the runner, so the cancellation did "
            "not land in the claim's commit window and this row is not "
            "witnessing it")

        after = await _awaited(
            lambda: _row(sf, run_id),
            lambda row: row["status"] == "pending" and row["release_count"] == 1)
        assert after["status"] == "pending", (
            "a cancellation delivered at the claim await stranded the claim "
            f"(waited {_RELEASE_TIMEOUT_S:g}s): {after}")
        assert after["retry_count"] == 0, (
            f"retry budget charged for a cancellation: {after}")
        assert after["release_count"] == 1, (
            f"not released via release_claim: {after}")
        assert released, (
            "release_claim was never called for the claim committed in the "
            f"cancelled tick's window: {after}")
        for instance, reason, _result in released:
            assert instance == at_commit["id"], (
                f"release_claim was called on another row ({at_commit['id']} "
                f"committed): {released}")
            assert "cancel" in reason.lower(), (
                f"released for a reason that is not a cancellation: {reason!r}")
    finally:
        let_go.set()                  # never leave the claim worker parked
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.wait([task], timeout=_RELEASE_TIMEOUT_S)

