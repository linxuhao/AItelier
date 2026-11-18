"""A poll whose database read is slow must not silence the server.

`poll_and_execute` is an ASYNC apscheduler job, so everything it does
synchronously happens ON the event loop: while it waits, HTTP requests, the SSE
stream and /health answer nothing. The reads it made there were the long ones,
because `db.get_active_projects` is not one local query — it first asks
skillflow for every active run in four `list_runs` sweeps over a table that grows
with every run this host has executed.

Measured, not hypothetical: the container's watchdog captured 20 stall dumps on
2026-11-17T23:05:18Z-23:40:56Z in `~/.AItelier/logs/loop-stack-dumps/`, longest
`watched loop silent for 6.939s` (loop-stack-20261117T230650.863248Z-p8-23.log),
whose watched-loop section is::

    apscheduler/executors/base.py:181 run_coroutine_job
    /app/core/scheduler.py:2990 poll_and_execute
    /app/core/db_manager.py:1170 get_active_projects
    skillflow/core.py:1836 list_runs -> .fetchall()

6 of the 20 dumps name `get_active_projects` there; the tick path is named too
(`_parked_checkpoint` -> `sf.get_steps` in 4, `_get_or_create_skillflow_run` in
4 — 3 of those with `db.get_project` beneath it).

So every blocking read on that path moved to a worker thread
(`asyncio.to_thread`), and these tests are the witness. Each one makes ONE real
CALL on the path take a measured 0.5 s and then asks whether the loop kept
serving *throughout* it — a concurrent heartbeat coroutine whose maximum
inter-beat gap must stay far under the stub's duration — and whether the tick
still finished and did its work.

The witness has teeth, and that was checked rather than assumed: in a scratch
copy of this tree with ONLY `core/scheduler.py` restored to the previous commit,
all three `test_the_loop_keeps_beating_while_the_db_path_is_slow[...]` nodes fail
on the gap assertion — measured 503 ms, 1003 ms and 1003 ms against a 250 ms
budget, with ZERO heartbeats landing inside the 500 ms call. Nothing else was
needed to make them fail, so it is the placement of these calls that they pin.
`test_the_selected_project_set_is_the_frozen_expectation` stays green on that
tree, which is the point of freezing it: the selection was never supposed to
change, and it did not. (`test_the_same_selection_comes_back_from_a_worker_thread`
fails there naming the helper it exercises — that helper is the change.)

Measured on this tree, first attempt, no retry: maximum inter-beat gap 5.5 ms
(`get_active_projects`), 5.2 ms (`get_project`) and 5.9 ms (`get_steps`) while
the call took 500 ms — 98 of 99 heartbeats landed INSIDE the slow call.

The frozen selection below is a LITERAL, copied from what the unchanged
`get_active_projects` returned for this exact fixture state on 5b159291 before
the change was made. It is deliberately not recomputed from the code under test:
a test that recomputes its own expectation cannot notice a changed selection.
"""

import asyncio
import logging
import time
import uuid

import pytest

import api.dependencies as deps
from core import scheduler as sc
from core.db_manager import DBManager
from skillflow import PipelineGraph, SkillFlow, StepResult
from skillflow.graph import StepNode, Transition

# The deliberately slow DB path. Long next to any plausible scheduling jitter,
# short enough that the suite stays cheap; the budget below is relative to it.
STUB_S = 0.5
# One heartbeat per 5 ms: ~100 beats fit inside one stub, so "the loop kept
# serving" is a COUNT of observations, not an impression.
BEAT_S = 0.005
# How long the loop may stay silent. Half the stub: a regression spends the
# WHOLE stub on the loop and is therefore at least 2x over budget even if every
# other beat is perfect, while ordinary noise on a 2-CPU box stays far under it.
GAP_BUDGET_S = 0.25
# A stalled loop is deterministic, a busy box is not: re-measure a false red
# instead of reporting one. A regression fails all of them.
ATTEMPTS = 3
# Beats that must land INSIDE the slow call: 0.5 s / 5 ms = 100 expected, so
# this is an order of magnitude below the real number and still impossible for
# a blocked loop (which records zero — it cannot run at all).
MIN_BEATS_INSIDE = 10

REGISTERED_CONFIG = "owned"
DRAFTING_PROJECT = "loop-witness-drafting"

# FROZEN: `(project_id, status, config_name, priority)` of
# `db.get_active_projects(limit=10)`, verbatim from the behaviour on 5b159291:
# priority DESC, then updated_at ASC, and the drafting project excluded because
# it has no active run and the fallback path skips `meta_state='drafting'`.
FROZEN_PRIORITY_SELECTION = [
    ("loop-witness-b", "executing", REGISTERED_CONFIG, 9),
    ("loop-witness-a", "executing", REGISTERED_CONFIG, 0),
]
# FROZEN: the same state selected FIFO (created_at ASC), which is a DIFFERENT
# order — so this pins the ordering rule, not just membership.
FROZEN_FIFO_SELECTION = ["loop-witness-a", "loop-witness-b"]
FROZEN_IDS = [row[0] for row in FROZEN_PRIORITY_SELECTION]

# Where a "slow query" can be installed, and which stall dump names it.
SLOW_SITES = {
    # db_manager.py:1170 get_active_projects -> skillflow list_runs (6 of 20)
    "get_active_projects": ("db", "the poll's own project query"),
    # scheduler.py:467 _get_or_create_skillflow_run -> db_manager.py:877 (3 of 20)
    "get_project": ("db", "the run resolution inside the tick"),
    # scheduler.py:1204 _parked_checkpoint -> skillflow get_steps (4 of 20)
    "get_steps": ("sf", "the parked-checkpoint read inside the tick"),
}


class _Manifest:
    """The one config the poller owns — what `get_config_registry().list()`
    answers, so the REAL `get_active_projects` runs its real filter."""

    config_name = REGISTERED_CONFIG
    scheduler_owned = True


class _Registry:
    def list(self):
        return [_Manifest()]


class _Slow:
    """A real call, made to take `STUB_S` on purpose.

    It DELEGATES: the call still does its work and returns its own result, so
    the tick's behaviour is untouched. Only the duration is a stub — that is
    the measurement this test needs, because a database that is merely slow
    must not be able to freeze the server, and no test can wait for a real 7 s
    `list_runs` to happen.
    """

    def __init__(self, real, seconds=STUB_S):
        self.real = real
        self.seconds = seconds
        self.spans = []          # (entered, left) monotonic, one per call

    def __call__(self, *args, **kwargs):
        entered = time.monotonic()
        try:
            time.sleep(self.seconds)
            return self.real(*args, **kwargs)
        finally:
            self.spans.append((entered, time.monotonic()))


def _install_slow(site, env, monkeypatch):
    holder, _why = SLOW_SITES[site]
    target = env.db if holder == "db" else env.sf
    slow = _Slow(getattr(target, site))
    monkeypatch.setattr(target, site, slow)
    return slow


def _paused_run(sf, project_id):
    """A run parked at a checkpoint, built by driving the real engine.

    Parked on purpose: `_parked_checkpoint` is where the dumps caught
    `sf.get_steps`, and a parked run lets the tick reach that read and finish —
    it logs `outcome=terminal status=paused` and stops, so the test measures the
    reads without executing any step.
    """
    run_id = sf.create_run(REGISTERED_CONFIG, project_id=project_id)
    sf.start_run(run_id)
    for step_id in ("a", "b"):
        sf.advance_run(run_id)
        claim = sf.claim_next_step(run_id)
        assert claim.step_id == step_id, (project_id, claim.step_id)
        sf.confirm_step(claim.token, StepResult(outputs={}))
    sf.advance_run(run_id)           # "b" is the checkpoint: the run parks
    assert sf.get_run(run_id)["status"] == "paused", project_id
    return run_id


def _seed(sf, db):
    """The crafted state: two active projects (both genuinely parked) plus one
    the query must keep out. Written with explicit timestamps so both
    orderings are deterministic — one row's priority and age disagree."""
    sf.register_graph(PipelineGraph(name=REGISTERED_CONFIG, begin="a", steps=[
        StepNode(id="a", transitions=[Transition(to="b")]),
        StepNode(id="b", checkpoint=True, checkpoint_label="Witness review",
                 transitions=[Transition(to="done")]),
        StepNode(id="done", transitions=[])]))
    runs = {pid: _paused_run(sf, pid) for pid in FROZEN_IDS}
    rows = [
        # project_id, status, priority, updated_at, created_at, meta_state
        ("loop-witness-b", "executing", 9,
         "2026-11-18 02:00:00", "2026-11-18 02:00:00", None),
        ("loop-witness-a", "executing", 0,
         "2026-11-18 01:00:00", "2026-11-18 01:00:00", None),
        (DRAFTING_PROJECT, "planning", 50,
         "2026-11-18 00:30:00", "2026-11-18 00:30:00", "drafting"),
    ]
    with db.get_connection() as conn:
        for pid, status, priority, updated, created, meta in rows:
            conn.execute(
                "INSERT INTO runs (project_id, name, config_name, status, "
                "priority, updated_at, created_at, meta_state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, pid.replace("-", " ").title(), REGISTERED_CONFIG, status,
                 priority, updated, created, meta))
        conn.commit()
    return runs


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated poller state, a real DB, a real SkillFlow, a real tick log."""
    db = DBManager(str(tmp_path / "host.db"))
    sf = SkillFlow(str(tmp_path / "engine.db"))
    monkeypatch.setattr(sc, "db", db)
    monkeypatch.setattr(sc, "get_skillflow", lambda: sf)
    # `get_active_projects` imports these INSIDE the function, so the module
    # attribute is what decides which engine and which registry it sees.
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    monkeypatch.setattr(deps, "get_config_registry", lambda: _Registry())

    monkeypatch.setattr(sc, "_tick_locks", {})
    monkeypatch.setattr(sc, "_tick_last_stuck", {})
    monkeypatch.setattr(sc, "_tick_last_paused", {})
    monkeypatch.setattr(sc, "_detached_ticks", {})
    monkeypatch.setattr(sc, "_detached_tick_owners", {})
    monkeypatch.setattr(sc, "_scheduler_instance", None)   # no wake date jobs
    # Lease maintenance is not what this measures, and a real sweep would add
    # its own worker thread to the loop-liveness picture.
    monkeypatch.setattr(sc, "_sweep_ended_leases", lambda: None)
    monkeypatch.setattr(sc, "_lease_sweep_future", None)

    # The real tick_log, writing to a file of this test's own.
    log_path = tmp_path / "scheduler_ticks.log"
    lg = logging.getLogger(f"test.tick.{uuid.uuid4().hex}")
    lg.propagate = False
    lg.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                           datefmt="%Y-%m-%dT%H:%M:%SZ"))
    lg.addHandler(handler)
    monkeypatch.setattr(sc, "_tick_logger", lg)

    class Env:
        pass

    e = Env()
    e.db, e.sf, e.log_path = db, sf, log_path
    e.runs = _seed(sf, db)
    e.lines_for = lambda pid: [ln for ln in log_path.read_text().splitlines()
                               if f"project={pid} " in ln]
    yield e
    handler.close()
    sf._conn.close()


async def _run_with_heartbeat(body):
    """Await `body()` while a heartbeat coroutine samples the loop every BEAT_S.

    Returns `(result, beats, (started, finished))`. The beats are the moments
    the heartbeat got to RUN, which is exactly what "the loop is serving" means:
    a synchronous call on the loop thread cannot let the clock advance between
    two beats, so the gaps between them measure the stalls the server's users
    would have felt.
    """
    beats = []
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            beats.append(time.monotonic())
            await asyncio.sleep(BEAT_S)

    task = asyncio.create_task(heartbeat(), name="heartbeat")
    await asyncio.sleep(0)                  # first beat, before the work starts
    started = time.monotonic()
    try:
        result = await body()
    finally:
        finished = time.monotonic()
        stop.set()
        await task
    return result, beats, (started, finished)


def _attempt_summary(slow, beats, window):
    started, finished = window
    marks = [started] + [b for b in beats if started <= b <= finished] + [finished]
    gaps = [b - a for a, b in zip(marks, marks[1:])]
    inside = [b for b in beats
              if any(enter <= b <= left for enter, left in slow.spans)]
    return {
        "max_gap": max(gaps) if gaps else float("inf"),
        "gaps": gaps,
        "beats": len(beats),
        "inside": len(inside),
        "spans": list(slow.spans),
        "window": window,
    }


@pytest.mark.parametrize("site", sorted(SLOW_SITES))
async def test_the_loop_keeps_beating_while_the_db_path_is_slow(env, monkeypatch, site):
    """Make one real DB call on the tick's path take STUB_S; the loop must keep
    serving throughout, and the tick must still complete and do its work."""
    slow = _install_slow(site, env, monkeypatch)
    _holder, why = SLOW_SITES[site]
    attempts = []

    async def body():
        dispatched = await sc.poll_and_execute()
        await asyncio.gather(*dispatched, return_exceptions=True)
        return dispatched

    try:
        for _ in range(ATTEMPTS):
            slow.spans.clear()
            dispatched, beats, window = await _run_with_heartbeat(body)
            attempt = _attempt_summary(slow, beats, window)
            attempt["dispatched"] = [t.get_name() for t in dispatched]
            attempts.append(attempt)
            if attempt["max_gap"] < GAP_BUDGET_S:
                break
    finally:
        await sc.settle_scheduler_maintenance()

    # The stub MUST have run: a green result from a call that never happened
    # would be a witness to nothing.
    assert all(a["spans"] for a in attempts), (
        f"{site} was never called through the poll/tick path — the deliberately "
        f"slow call this test measures is not on it")

    best = min(attempts, key=lambda a: a["max_gap"])
    report = "; ".join(
        f"attempt {i + 1}: max gap {a['max_gap'] * 1000:.0f} ms, "
        f"{a['beats']} beats ({a['inside']} inside the slow call)"
        for i, a in enumerate(attempts))
    assert best["max_gap"] < GAP_BUDGET_S, (
        f"the event loop went silent for {best['max_gap'] * 1000:.0f} ms while "
        f"{site} ({why}) was made to take {STUB_S * 1000:.0f} ms — every HTTP "
        f"request, the SSE stream and /health wait that long. Budget "
        f"{GAP_BUDGET_S * 1000:.0f} ms. {report}")
    assert best["inside"] >= MIN_BEATS_INSIDE, (
        f"only {best['inside']} heartbeats landed inside the {STUB_S * 1000:.0f} ms "
        f"{site} call (expected ~{int(STUB_S / BEAT_S)}); the loop was not "
        f"serving during it. {report}")

    # ...and the tick did its work, not just "did not block".
    assert best["dispatched"] == [f"tick:{pid}" for pid in FROZEN_IDS], best
    assert not sc._detached_ticks, "a dispatched tick never finished"
    for pid in FROZEN_IDS:
        assert any("outcome=terminal" in line and "status=paused" in line
                   for line in env.lines_for(pid)), (
            f"the tick for {pid} did not reach the parked checkpoint it was "
            f"supposed to report: {env.lines_for(pid)}")
        assert any(env.runs[pid][:8] in line for line in env.lines_for(pid)), (
            f"the tick log for {pid} does not name its run {env.runs[pid][:8]}: "
            f"{env.lines_for(pid)}")


def _selected(rows):
    return [(r["project_id"], r["status"], r["config_name"], r["priority"])
            for r in rows]


def test_the_selected_project_set_is_the_frozen_expectation(env):
    """The query's ANSWER must not change with the thread it runs on.

    Backend untouched: this is the same method the poller calls, asked
    directly, and its rows are compared against a literal copied from the
    behaviour BEFORE the change.
    """
    assert _selected(env.db.get_active_projects(limit=10)) == FROZEN_PRIORITY_SELECTION
    assert [r["project_id"] for r in env.db.get_active_projects(limit=2)] == FROZEN_IDS
    assert [r["project_id"] for r in
            env.db.get_active_projects(fifo=True, limit=10)] == FROZEN_FIFO_SELECTION
    assert DRAFTING_PROJECT not in [
        r["project_id"] for r in env.db.get_active_projects(limit=10)]


async def test_the_same_selection_comes_back_from_a_worker_thread(env):
    """Moving the call off the loop must not change WHICH projects come back.

    `DBManager.get_connection` decides `check_same_thread` from whether the
    caller is on an event loop, and it opens a fresh connection per operation —
    so a call made from a worker thread opens, uses and closes its connection on
    that one thread. This asks the moved query from off the loop and gets the
    identical frozen rows back, which is what makes the move safe rather than
    merely faster; and the poll helpers themselves are exercised through the
    real poller in the tests above.
    """
    rows = await sc._active_projects_off_the_loop(limit=10)
    assert _selected(rows) == FROZEN_PRIORITY_SELECTION

    dispatched = await sc.poll_and_execute()
    try:
        assert [t.get_name() for t in dispatched] == [f"tick:{pid}" for pid in FROZEN_IDS]
    finally:
        await asyncio.gather(*dispatched, return_exceptions=True)
        await sc.settle_scheduler_maintenance()
