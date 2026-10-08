"""The demo (FIFO) and per-owner pollers must not starve late projects either.

`poll_and_execute` was made to dispatch DETACHED ticks, so a long step in one
project no longer owns the interval job (`max_instances=1`). The demo poller and
the per-owner poller were left awaiting `_execute_skillflow_tick(...)` inline, so
a project activated after they had picked their ONE candidate got no tick until
that step returned — the same starvation, on the other two entry points
(iss-29db2c9e028a4c71).

These tests drive the REAL `_add_scheduler_job` jobs (real AsyncIOScheduler,
interval, `max_instances=1`) for demo and owner, and a real SkillFlow wherever
the property is about a claim row. Everything is source-only here: the tests are
delivered UNRUN; they are executed later by the director's ROOT-owned CPU
evaluator.
"""

import asyncio
import logging
import threading
import time
import uuid

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from skillflow import PipelineGraph, SkillFlow


from core import scheduler as sc

INTERVAL_S = 1          # smallest interval the settings accept
MARGIN_S = 2.0          # explicit slack on top of one interval
LONG_STEP_S = 8.0       # the "gate": long next to INTERVAL_S + MARGIN_S
OWNER = "alice@example.com"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated poller state, a mutable active-project list, a real tick log."""
    monkeypatch.setattr(sc, "_tick_locks", {})
    monkeypatch.setattr(sc, "_tick_last_stuck", {})
    monkeypatch.setattr(sc, "_detached_ticks", {})
    monkeypatch.setattr(sc, "_sweep_ended_leases", lambda: None)
    monkeypatch.setattr(sc, "_sweep_ended_leases_async",
                        lambda: asyncio.sleep(0))
    monkeypatch.setattr(sc, "_lease_sweep_future", None)
    monkeypatch.setattr(sc, "_scheduler_instance", None)   # no wake date jobs

    monkeypatch.setattr(sc, "_user_scheduler_map", {})

    class Env:
        pass
    e = Env()
    e.active = []           # project ids, in FIFO (created_at) order
    e.paused = set()        # "held/paused" rows the query must exclude
    e.owners = {}           # pid -> owner_email
    e.priority = {}         # pid -> priority (for the non-FIFO ordering)
    e.query_log = []        # (owner_email, fifo) of every get_active_projects call

    def fake_active(owner_email=None, fifo=False, limit: int = 4):
        e.query_log.append((owner_email, fifo))
        rows = [{"project_id": p} for p in e.active if p not in e.paused]
        if owner_email is not None:
            rows = [r for r in rows if e.owners.get(r["project_id"]) == owner_email]
        if not fifo:
            rows.sort(key=lambda r: e.priority.get(r["project_id"], 0), reverse=True)
        return rows[:limit]

    monkeypatch.setattr(sc.db, "get_active_projects", fake_active)
    monkeypatch.setattr(sc.db, "get_next_active_project",
                        lambda owner_email=None, fifo=False: (
                            fake_active(owner_email=owner_email, fifo=fifo) or [None])[0])

    # The real tick_log, writing to a file of this test's own.
    log_path = tmp_path / "scheduler_ticks.log"
    lg = logging.getLogger(f"test.tick.{uuid.uuid4().hex}")
    lg.propagate = False
    lg.setLevel(logging.INFO)
    h = logging.FileHandler(log_path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                     datefmt="%Y-%m-%dT%H:%M:%SZ"))
    lg.addHandler(h)
    monkeypatch.setattr(sc, "_tick_logger", lg)

    t0 = time.monotonic()
    timeline: list[tuple[float, str, str]] = []
    real_tick_log = sc.tick_log

    def spy(project_id, outcome, **detail):
        timeline.append((time.monotonic() - t0, project_id, outcome))
        real_tick_log(project_id, outcome, **detail)
    monkeypatch.setattr(sc, "tick_log", spy)

    e.log_path, e.timeline, e.t0 = log_path, timeline, t0
    e.lines_for = lambda pid: [ln for ln in log_path.read_text().splitlines()
                               if f"project={pid} " in ln]
    yield e
    h.close()


def _poll_job(sched):
    """The single interval poller job `_add_scheduler_job` built (not the
    hung-claims supervisor, which is also an interval job)."""
    jobs = [j for j in sched.get_jobs()
            if isinstance(j.trigger, IntervalTrigger)
            and j.func is not sc._check_hung_claims]
    assert len(jobs) == 1, jobs
    return jobs[0]


def _scheduler(entry: str, owner_email: str = OWNER):
    """The production job for the entry under test, with interval settings."""
    settings = {"scheduler_type": "interval", "scheduler_interval": INTERVAL_S}
    sched = AsyncIOScheduler()
    if entry == "demo":
        sc._add_scheduler_job(sched, settings, demo=True)
    elif entry == "owner":
        sc._add_scheduler_job(sched, settings, owner_email=owner_email)
    else:
        sc._add_scheduler_job(sched, settings)
    job = _poll_job(sched)
    assert job.max_instances == 1, "the job under test must be single-instance"
    return sched


async def _settle(timeout=LONG_STEP_S + 5):
    """Wait for every tick whatever entry dispatched."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()
                   and ("tick:" in t.get_name()
                        or "run_coroutine_job" in repr(t.get_coro()))]
        if not pending:
            return
        await asyncio.sleep(0.05)


# ── criterion 1: a project activated during a long step gets a tick ──────────

@pytest.mark.parametrize("entry", ["demo", "owner"])
async def test_late_project_gets_a_tick_while_an_earlier_step_is_in_flight(
        env, monkeypatch, entry):
    """The issue's shape on the REAL demo/owner interval job: one project inside
    a long step (off the loop), a second project made active after that tick
    began, which must be executed while the first is still running."""
    gate, late = f"gate-{uuid.uuid4().hex[:6]}", f"late-{uuid.uuid4().hex[:6]}"
    env.owners.update({gate: OWNER, late: OWNER})
    gate_started, late_ran = asyncio.Event(), asyncio.Event()
    release = threading.Event()

    async def tick(pid, loop):
        if pid == gate:
            gate_started.set()
            # A synchronous tool step, off the loop — how `_advance_off_the_loop`
            # runs `_run_repo_gate`.
            await asyncio.to_thread(release.wait, LONG_STEP_S)
            sc.tick_log(pid, "executed", step="gate")
        else:
            sc.tick_log(pid, "executed", step="work")
            late_ran.set()
    monkeypatch.setattr(sc, "_run_skillflow_tick", tick)

    env.active.append(gate)
    sched = _scheduler(entry)
    sched.start()
    try:
        await asyncio.wait_for(gate_started.wait(), timeout=INTERVAL_S + MARGIN_S)
        env.active.append(late)                 # activated AFTER the tick began
        try:
            await asyncio.wait_for(late_ran.wait(), timeout=INTERVAL_S + MARGIN_S)
        except asyncio.TimeoutError:
            pass
        assert late_ran.is_set(), (
            f"{entry} poller: project activated during a {LONG_STEP_S:.0f}s step "
            f"got no tick in {INTERVAL_S}s + {MARGIN_S}s; tick-log lines for "
            f"{late}: {env.lines_for(late)}")
        assert env.lines_for(late), "it ran but the tick log does not show it"
        assert not release.is_set() and sc._tick_locks[gate].locked(), \
            "the long step must still be running when the late project ran"
    finally:
        release.set()
        await _settle()
        sched.shutdown(wait=False)
        await _settle()


# ── criterion 2: owner isolation and demo FIFO are preserved ────────────────

async def test_owner_poller_never_runs_another_owners_project(env, monkeypatch):
    """An owner's poller must only advance that owner's projects, and it must
    ask the query for exactly that owner."""
    ticked = []

    async def tick(pid, loop):
        ticked.append(pid)
    monkeypatch.setattr(sc, "_run_skillflow_tick", tick)
    env.active.extend(["alice-1", "bob-1"])
    env.owners.update({"alice-1": OWNER, "bob-1": "bob@example.com"})

    started = await sc.poll_and_execute_owner(OWNER)
    await asyncio.gather(*started, return_exceptions=True)

    assert ticked == ["alice-1"], ticked
    assert env.query_log and env.query_log[-1][0] == OWNER


async def test_demo_poller_keeps_fifo_order_and_passes_fifo_true(env, monkeypatch):
    """Demo mode is oldest-first regardless of priority, and it must still ask
    the query with `fifo=True`."""
    ticked = []

    async def tick(pid, loop):
        ticked.append(pid)
    monkeypatch.setattr(sc, "_run_skillflow_tick", tick)
    env.active.extend(["old", "new"])              # insertion order == oldest first
    env.priority.update({"old": 1, "new": 100})    # higher priority on the newer one

    started = await sc.poll_and_execute_demo()
    await asyncio.gather(*started, return_exceptions=True)

    assert ticked == ["old", "new"], f"demo mode is not FIFO: {ticked}"
    assert env.query_log[-1][1] is True, "demo poller did not ask for FIFO order"


async def test_demo_poller_never_dispatches_a_paused_project(env, monkeypatch):
    """Held/paused projects are excluded by the active-project query; a poller
    that dispatched its own candidate set would break that."""
    ticked = []

    async def tick(pid, loop):
        ticked.append(pid)
    monkeypatch.setattr(sc, "_run_skillflow_tick", tick)
    env.active.extend(["active", "paused"])
    env.paused.add("paused")

    started = await sc.poll_and_execute_demo()
    await asyncio.gather(*started, return_exceptions=True)

    assert ticked == ["active"], ticked


async def test_demo_and_owner_share_the_process_wide_cap(env, monkeypatch):
    """The cap is over `_detached_ticks` — one process-wide bound across the main,
    demo and owner entry points, not one per poller."""
    monkeypatch.setattr(sc, "MAX_CONCURRENT_PROJECTS", 1)
    now = {"n": 0, "peak": 0}

    async def tick(pid, loop):
        now["n"] += 1
        now["peak"] = max(now["peak"], now["n"])
        await asyncio.sleep(0.2)
        now["n"] -= 1
    monkeypatch.setattr(sc, "_run_skillflow_tick", tick)
    env.active.extend(["a1", "a2"])
    env.owners.update({"a1": OWNER, "a2": OWNER})

    started = list(await sc.poll_and_execute_demo())
    started += list(await sc.poll_and_execute_owner(OWNER))
    held = sum(1 for _, _, o in env.timeline if o == "at_capacity")
    await asyncio.gather(*started, return_exceptions=True)

    assert now["peak"] == 1, f"cap 1 but peak in flight was {now['peak']}"
    assert held, "the second entry point did not report at_capacity"


# ── criterion 3: shutdown cancels detached demo/owner ticks and frees claims ─

def _work_graph():
    return PipelineGraph._from_dict({
        "name": "demo_owner_work_t", "begin": "a",
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "step_complete", "step": "a"}]},
        "steps": [{"id": "a", "step_type": "agent", "agent_config": "x",
                   "timeout_seconds": 60}],
    })


def _real_sf(tmp_path):
    sf = SkillFlow(str(tmp_path / "sf.db"),
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "proj"))
    sf.register_agent_config("x", model="m")
    return sf


def _wire_real_tick(monkeypatch, sf, runs, execute):
    """Everything below `_run_skillflow_tick` is real except the LLM call."""
    monkeypatch.setattr(sc, "get_skillflow", lambda: sf)
    monkeypatch.setattr(sc, "_get_or_create_skillflow_run", lambda pid: runs.get(pid))
    monkeypatch.setattr(sc, "_sync_project_status_to_db", lambda pid: None)
    monkeypatch.setattr(sc, "_get_event_bus", lambda: None)

    class _Runner:
        def __init__(self, **kw):
            pass

        async def execute(self, claimed):
            return await execute(claimed)
    import aitelier.runner
    monkeypatch.setattr(aitelier.runner, "AgentStepRunner", _Runner)


@pytest.mark.parametrize("entry", ["demo", "owner"])
async def test_shutdown_cancels_detached_ticks_and_releases_their_claims(
        env, monkeypatch, tmp_path, entry):
    """A scheduler shutdown must reach the detached ticks the demo/owner pollers
    started — the reason `_cancel_detached_ticks` is now registered on every
    poller's scheduler — and every cancelled tick must hand its claim back."""
    sf = _real_sf(tmp_path)
    sf.register_graph(_work_graph())
    pids = [f"s{i}-{uuid.uuid4().hex[:6]}" for i in range(3)]
    runs = {}
    for pid in pids:
        runs[pid] = sf.get_or_create_run("demo_owner_work_t", pid, {})
        sf.start_run(runs[pid])
        env.owners[pid] = OWNER

    entered: list[asyncio.Task] = []
    all_in = asyncio.Event()

    async def execute(claimed):
        entered.append(asyncio.current_task())
        if len(entered) == len(pids):
            all_in.set()
        await asyncio.Event().wait()            # an agent step that never ends
    _wire_real_tick(monkeypatch, sf, runs, execute)
    env.active.extend(pids)

    def rows():
        return {r["run_id"]: dict(r) for r in sf._conn.execute(
            "SELECT run_id, status, retry_count, release_count "
            "FROM skillflow_steps WHERE step_id = 'a'").fetchall()}

    sched = _scheduler(entry)
    sched.start()
    try:
        await asyncio.wait_for(all_in.wait(), timeout=INTERVAL_S + MARGIN_S)
        assert all(r["status"] == "claimed" for r in rows().values()), rows()
        sched.shutdown(wait=True)               # what the lifespan does
        done, pending = await asyncio.wait(entered, timeout=5)
        assert not pending, (
            f"{len(pending)} of {len(entered)} {entry} ticks were not cancelled "
            f"by the scheduler shutdown")
        assert all(t.cancelled() for t in done), \
            [t for t in done if not t.cancelled()]
        after = rows()
        for rid in runs.values():
            r = after[rid]
            assert r["status"] == "pending", f"claim not handed back: {r}"
            assert r["retry_count"] == 0, f"retry budget charged: {r}"
            assert r["release_count"] == 1, f"not released via release_claim: {r}"
        assert not sc._detached_ticks, "slots still held"
        assert not any(lk.locked() for lk in sc._tick_locks.values()), \
            "a project lock outlived its cancelled tick"
    finally:
        for t in entered:
            t.cancel()
        if sched.running:
            sched.shutdown(wait=False)
        await _settle()
