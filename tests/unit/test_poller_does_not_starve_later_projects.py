"""A long step in one project must not starve projects dispatched after it.

`poll_and_execute` used to pick its batch once and `gather` it, and apscheduler
runs that job with `max_instances=1`: the job lasted as long as the slowest tick
in its batch, every later tick was `tick_skipped`, and a project that became
active after the batch was picked got no tick at all until the slow one
returned. Live 2026-09-22: 7 rounds dispatched at 19:10Z, 5 of them with zero
tick lines at 19:33Z while one project sat in a ~26-minute repo gate
(iss-ce5d36fce9534128).

The tests below drive the REAL `poll_and_execute` through the REAL apscheduler
job `_add_scheduler_job` builds (interval, `max_instances=1`) wherever the
property is about the scheduler, and a real SkillFlow wherever the property is
about a row. Names added by the fix are read with `getattr`, so on the old tree
these fail on the behaviour they pin, not on an AttributeError.
"""

import asyncio
import logging
import threading
import time
import uuid
from pathlib import Path

import pytest
import skillflow
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from skillflow import PipelineGraph, SkillFlow, StepResult
from skillflow.tool_loader import ToolLoader

from core import scheduler as sc

INTERVAL_S = 1          # the smallest interval the settings accept (an int)
MARGIN_S = 2.0          # explicit slack on top of one interval
LONG_STEP_S = 8.0       # the "gate": long next to INTERVAL_S + MARGIN_S


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated poller state, a mutable active-project list, a real tick log."""
    monkeypatch.setattr(sc, "_tick_locks", {})
    monkeypatch.setattr(sc, "_tick_last_stuck", {})
    if hasattr(sc, "_detached_ticks"):
        monkeypatch.setattr(sc, "_detached_ticks", {})
    monkeypatch.setattr(sc, "_sweep_ended_leases", lambda: None)
    monkeypatch.setattr(sc, "_scheduler_instance", None)   # no wake date jobs

    active: list[str] = []
    monkeypatch.setattr(
        sc.db, "get_active_projects",
        lambda owner_email=None, fifo=False, limit=1:
            [{"project_id": p} for p in active][:limit])

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

    class Env:
        pass
    e = Env()
    e.active, e.log_path, e.timeline, e.t0 = active, log_path, timeline, t0
    e.lines_for = lambda pid: [ln for ln in log_path.read_text().splitlines()
                               if f"project={pid} " in ln]
    yield e
    h.close()


def _scheduler():
    """The production job: `_add_scheduler_job` with interval settings."""
    sched = AsyncIOScheduler()
    sc._add_scheduler_job(sched, {"scheduler_type": "interval",
                                  "scheduler_interval": INTERVAL_S})
    poll = [j for j in sched.get_jobs() if j.func is sc.poll_and_execute]
    assert len(poll) == 1 and poll[0].max_instances == 1, \
        "the job under test must be the real single-instance poller"
    return sched


async def _settle(timeout=LONG_STEP_S + 5):
    """Wait for every tick this module started — however the tree dispatches."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()
                   and ("tick:" in t.get_name()
                        or "run_coroutine_job" in repr(t.get_coro()))]
        if not pending:
            return
        await asyncio.sleep(0.05)


# ── criterion 1: a project dispatched during a long step gets a tick ────────

async def test_a_project_activated_during_a_long_step_gets_a_tick(env, monkeypatch):
    gate, late = f"gate-{uuid.uuid4().hex[:6]}", f"late-{uuid.uuid4().hex[:6]}"
    gate_started, late_ran = asyncio.Event(), asyncio.Event()
    release = threading.Event()

    async def tick(pid, loop):
        if pid == gate:
            gate_started.set()
            # A synchronous tool step, off the loop — exactly how
            # `_advance_off_the_loop` runs `_run_repo_gate`.
            await asyncio.to_thread(release.wait, LONG_STEP_S)
            sc.tick_log(pid, "executed", step="gate")
        else:
            sc.tick_log(pid, "executed", step="work")
            late_ran.set()
    monkeypatch.setattr(sc, "_run_skillflow_tick", tick)
    # The old tree dispatched through the lock-taking wrapper; route it to the
    # same stand-in so both trees execute the identical step body.
    monkeypatch.setattr(sc, "_execute_skillflow_tick",
                        lambda pid, loop: _locked(pid, loop, tick))

    env.active.append(gate)
    sched = _scheduler()
    sched.start()
    try:
        await asyncio.wait_for(gate_started.wait(), timeout=INTERVAL_S + MARGIN_S)
        env.active.append(late)                 # dispatched AFTER the tick began
        activated = time.monotonic()
        try:
            await asyncio.wait_for(late_ran.wait(), timeout=INTERVAL_S + MARGIN_S)
        except asyncio.TimeoutError:
            pass
        waited = time.monotonic() - activated
        assert late_ran.is_set(), (
            f"project activated during a {LONG_STEP_S:.0f}s step got no tick in "
            f"{INTERVAL_S}s interval + {MARGIN_S}s margin: "
            f"{len(env.lines_for(late))} tick-log lines for {late}")
        assert env.lines_for(late), "it ran but the tick log does not show it"
        assert not release.is_set() and gate in sc._tick_locks \
            and sc._tick_locks[gate].locked(), \
            "the long step must still be running when the late project ran"
        print(f"\nlate project ran {waited:.2f}s after activation, "
              f"while {gate} was still inside its step")
    finally:
        release.set()
        await _settle()
        sched.shutdown(wait=False)
        await _settle()


async def _locked(pid, loop, tick):
    lock = sc._get_tick_lock(pid)
    if not lock.acquire(blocking=False):
        sc.tick_log(pid, "locked")
        return
    try:
        await tick(pid, loop)
    finally:
        lock.release()


# ── criterion 1, end to end: a real tool step, a real agent step ────────────

def _tool_graph():
    return PipelineGraph._from_dict({
        "name": "starve_gate_t", "begin": "gate",
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "step_complete", "step": "gate"}]},
        "steps": [{"id": "gate", "step_type": "tool", "tool_name": "slow_gate"}],
    })


def _agent_graph():
    return PipelineGraph._from_dict({
        "name": "starve_work_t", "begin": "a",
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "step_complete", "step": "a"}]},
        "steps": [{"id": "a", "step_type": "agent", "agent_config": "x",
                   "timeout_seconds": 60}],
    })


def _real_sf(tmp_path, loader=None):
    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
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


async def test_end_to_end_a_real_long_tool_step_does_not_starve_a_later_project(
        env, monkeypatch, tmp_path):
    """The issue's shape on an isolated scheduler: one project inside a real
    inline tool step (advance_run → _execute_tool_inline, off the loop), a
    second project created after that tick began, whose agent step must be
    claimed, executed and confirmed while the tool step is still running."""
    release = threading.Event()
    tool_window = {}

    def slow_gate(*args, **kwargs):
        tool_window["start"] = time.monotonic() - env.t0
        release.wait(LONG_STEP_S)
        tool_window["end"] = time.monotonic() - env.t0
        return {"passed": True}

    # With a directory: an EMPTY ToolLoader is falsy and SkillFlow swaps in its
    # own, which does not know the tool.
    loader = ToolLoader(Path(skillflow.__file__).parent / "tools")
    sf = _real_sf(tmp_path, loader)
    loader.register_dynamic_tool("slow_gate", {}, slow_gate)
    sf.register_graph(_tool_graph())
    sf.register_graph(_agent_graph())

    gate, late = f"gate-{uuid.uuid4().hex[:6]}", f"late-{uuid.uuid4().hex[:6]}"
    runs = {gate: sf.get_or_create_run("starve_gate_t", gate, {})}
    sf.start_run(runs[gate])
    confirmed = asyncio.Event()

    async def execute(claimed):
        return StepResult()
    _wire_real_tick(monkeypatch, sf, runs, execute)

    real_confirm = sf.confirm_step

    def confirm(token, result):
        real_confirm(token, result)
        env.timeline.append((time.monotonic() - env.t0, late, "confirm_step"))
        confirmed.set()
    monkeypatch.setattr(sf, "confirm_step", confirm)

    env.active.append(gate)
    sched = _scheduler()
    sched.start()
    try:
        deadline = time.monotonic() + INTERVAL_S + MARGIN_S
        while "start" not in tool_window and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert "start" in tool_window, "the tool step never started"

        runs[late] = sf.get_or_create_run("starve_work_t", late, {})
        sf.start_run(runs[late])
        env.active.append(late)
        activated = time.monotonic() - env.t0
        env.timeline.append((activated, late, "ACTIVATED"))
        try:
            await asyncio.wait_for(confirmed.wait(), timeout=INTERVAL_S + MARGIN_S)
        except asyncio.TimeoutError:
            pass

        print("\ntimeline (s since start, project, event):")
        print(f"  {tool_window['start']:7.2f}  {gate}  slow_gate START")
        for t, pid, what in sorted(env.timeline):
            print(f"  {t:7.2f}  {pid}  {what}")
        print(f"  tool still running at check: {'end' not in tool_window}")
        print("tick log (the file tick_log wrote):")
        for ln in env.log_path.read_text().splitlines():
            print(f"  {ln}")

        assert confirmed.is_set(), (
            f"{late} was activated at {activated:.2f}s during a "
            f"{LONG_STEP_S:.0f}s tool step and was not executed within "
            f"{INTERVAL_S}s + {MARGIN_S}s margin; its tick-log lines: "
            f"{env.lines_for(late)}")
        assert "end" not in tool_window, \
            "the tool step had already returned; this proved nothing"
        assert any("outcome=executed" in ln and "confirm_returned=True" in ln
                   for ln in env.lines_for(late)), env.lines_for(late)
        row = sf._conn.execute(
            "SELECT status FROM skillflow_steps WHERE run_id = ? AND step_id = 'a'",
            (runs[late],)).fetchone()
        assert row["status"] == "completed", dict(row)
    finally:
        release.set()
        await _settle()
        sched.shutdown(wait=False)
        await _settle()


# ── criterion 2: same project serial, and the cap binds across polls ────────

async def _drive_polls(n, gap):
    """`n` polls `gap` apart, each also racing a twin at the same instant —
    the interval job and a wake-on-confirm job landing together."""
    started = []
    for _ in range(n):
        for r in await asyncio.gather(sc.poll_and_execute(), sc.poll_and_execute()):
            started += r or []
        await asyncio.sleep(gap)
    await asyncio.gather(*started, return_exceptions=True)
    await _settle()
    return started


async def test_the_same_project_is_never_in_flight_twice(env, monkeypatch):
    in_flight: dict[str, int] = {}
    peak: dict[str, int] = {}
    ran: dict[str, int] = {}

    async def tick(pid, loop):
        in_flight[pid] = in_flight.get(pid, 0) + 1
        peak[pid] = max(peak.get(pid, 0), in_flight[pid])
        ran[pid] = ran.get(pid, 0) + 1
        await asyncio.sleep(0.3)
        in_flight[pid] -= 1
    monkeypatch.setattr(sc, "_run_skillflow_tick", tick)
    env.active.extend(["p1", "p2"])

    await _drive_polls(15, 0.05)

    guarded = sum(1 for _, _, o in env.timeline if o == "locked")
    print(f"\nticks run={ran} peak per project={peak} "
          f"guard firings (locked)={guarded}, over 15 polls x 2 racing twins "
          f"x 2 projects")
    assert peak == {"p1": 1, "p2": 1}, f"a project was in flight twice: {peak}"
    assert all(n > 1 for n in ran.values()) and len(ran) == 2, ran
    assert guarded > 0, "the guard never had to fire; this proved nothing"


async def test_in_flight_ticks_never_exceed_the_cap_across_polls(env, monkeypatch):
    cap = 3
    monkeypatch.setattr(sc, "MAX_CONCURRENT_PROJECTS", cap)
    now = {"n": 0, "peak": 0, "ran": 0}
    who: set[str] = set()
    last: dict[str, float] = {}

    async def tick(pid, loop):
        now["n"] += 1
        now["ran"] += 1
        who.add(pid)
        now["peak"] = max(now["peak"], now["n"])
        await asyncio.sleep(0.3)
        now["n"] -= 1
        last[pid] = time.monotonic()        # a finished tick bumps updated_at
    monkeypatch.setattr(sc, "_run_skillflow_tick", tick)
    env.active.extend(f"c{i}" for i in range(3 * cap))
    # `updated_at ASC`, as the real query orders: the least recently advanced
    # project comes first, so demand rotates past the first `cap` rows.
    monkeypatch.setattr(
        sc.db, "get_active_projects",
        lambda owner_email=None, fifo=False, limit=1:
            [{"project_id": p} for p in
             sorted(env.active, key=lambda p: last.get(p, 0.0))][:limit])

    await _drive_polls(15, 0.05)

    held = sum(1 for _, _, o in env.timeline if o == "at_capacity")
    print(f"\npeak in flight={now['peak']} cap={cap} ticks run={now['ran']} "
          f"distinct projects run={len(who)} at_capacity lines={held}, over "
          f"15 polls x 2 racing twins x {3 * cap} projects")
    # Demand exceeded the cap (more distinct projects ran than it allows at
    # once) — otherwise a peak of `cap` proves nothing about the bound.
    assert len(who) > cap, f"only {len(who)} projects ever ran"
    assert now["peak"] == cap, (
        f"peak in-flight {now['peak']} with cap {cap}")


# ── criterion 3: shutdown cancels detached ticks and releases their claims ──

async def test_shutdown_cancels_every_in_flight_tick_and_releases_its_claim(
        env, monkeypatch, tmp_path):
    sf = _real_sf(tmp_path)
    sf.register_graph(_agent_graph())
    pids = [f"s{i}-{uuid.uuid4().hex[:6]}" for i in range(3)]
    runs = {}
    for pid in pids:
        runs[pid] = sf.get_or_create_run("starve_work_t", pid, {})
        sf.start_run(runs[pid])

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

    sched = _scheduler()
    sched.start()
    try:
        await asyncio.wait_for(all_in.wait(), timeout=INTERVAL_S + MARGIN_S)
        assert all(r["status"] == "claimed" for r in rows().values()), rows()
        sched.shutdown(wait=True)                # what the lifespan does
        done, pending = await asyncio.wait(entered, timeout=5)
        assert not pending, (
            f"{len(pending)} of {len(entered)} in-flight ticks were not "
            f"cancelled by the scheduler shutdown")
        assert all(t.cancelled() for t in done), \
            [t for t in done if not t.cancelled()]
        after = rows()
        print(f"\nafter shutdown: {after}")
        for rid in runs.values():
            r = after[rid]
            assert r["status"] == "pending", f"claim not handed back: {r}"
            assert r["retry_count"] == 0, f"retry budget charged: {r}"
            assert r["release_count"] == 1, f"not released via release_claim: {r}"
        assert not getattr(sc, "_detached_ticks", {}), "slots still held"
        assert not any(lk.locked() for lk in sc._tick_locks.values()), \
            "a project lock outlived its cancelled tick"
    finally:
        for t in entered:
            t.cancel()
        if sched.running:
            sched.shutdown(wait=False)
        await _settle()
