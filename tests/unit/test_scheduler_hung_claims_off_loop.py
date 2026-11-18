"""The hung-claim supervisor may WAIT for the engine — never do it ON the loop.

2026-11-18T11:25-11:52Z, measured: this backend proxies every DSH driver's LLM
completion, and its event loop served almost nothing for minutes at a time
(event-loop lag 165 s / 132 s / 83 s / 79 s / 72 s; `/health` timing out at 15 s;
two of the director's native children died). The loop-stack watchdog's dump
`loop-stack-20261118T114939.873511Z-p7-111.log` ends the loop thread's stack at::

    core/scheduler.py:960 _check_hung_claims
      -> core/skillflow_host.py:582 reconcile_active_operations
      -> skillflow/core.py:2146 audit_operation_owners -> _ro
      -> skillflow/core.py:662 `with self._lock:`

The engine's lock is GLOBAL, and a WORKER thread holds it for the whole of its
own engine call (one `_sweep_ended_leases` -> `reconcile_all_leases` -> `get_run`
is enough). A 5 s supervisor that waits for that lock on the loop thread turns a
slow engine call into a whole-server outage.

Each case below stages exactly that shape, and nothing weaker:

1. an engine stand-in whose `_lock` is a `threading.Lock` ACQUIRED AND HELD BY A
   SEPARATE THREAD (no asyncio anywhere in the stall, no sleeping coroutine);
2. the REAL `scheduler._check_hung_claims()` running as an asyncio task, beside a
   counter task doing `await asyncio.sleep(0.01)` and incrementing;
3. the lock is held until a CONDITION — the counter advanced by a real margin —
   under a bounded `asyncio.wait_for`, then released, and the supervisor must
   have completed normally.

No assertion here is about a duration. The margin is counted in loop turns, and
the only clocks are (a) the generous ceiling on the wait and (b) the ordering of
two `time.monotonic()` readings, which says the engine call really did wait for
the holder rather than returning early.

The margin is what gives these tests teeth: the base tree (before the
`asyncio.to_thread` hops) scores 0 loop turns on every case — the loop is frozen
inside the engine call and cannot even run the `wait_for` timer — while the
delivered tree scores at least `MARGIN` on every case. The base arm was measured
on a scratch copy of the tree, never by editing this one.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import threading
import time
from dataclasses import dataclass
from unittest import mock

import pytest

from core import scheduler

#: Loop turns the counter must complete WHILE the lock is held. A turn is
#: `await asyncio.sleep(0.01)` plus an increment, so this is a small, honest
#: margin: five turns cannot happen if the loop is blocked in the engine call.
MARGIN = 5

#: Safety valve on the holder thread. In the DELIVERED tree the holder is
#: released by the counter condition (~50 ms), so this never elapses; it exists
#: so that a BROKEN arm (the loop frozen inside the engine call, unable to answer
#: the condition or even its own `wait_for` timer) still ends instead of hanging
#: the suite, and so that the base arm's measurement has a fixed window.
SAFETY_S = 20.0

#: Generous ceiling for the condition and for the supervisor completing. This
#: host runs at load 20 with heavy swap and other suites run here; the condition
#: needs ~50 ms of loop time.
WAIT_FOR_S = 90.0

#: The calls `_check_hung_claims` makes that can WAIT for the engine lock: its
#: own engine calls, plus the host's claim scan, which takes `sf._lock` through
#: `_sf_statement_lock` before it issues its statement. One case per site, so a
#: regression that re-freezes the loop at ANY of them is caught, not just at the
#: one the 2026-11-18 dump happened to catch.
SITES = (
    "reconcile_active_operations",
    "audit_operation_owners",
    "recover_stale_claims",
    "get_run",
    "list_runs",
    "_get_resolver_for_run",
    "claim_scan",
)

RUN_ID = "run-1118-loopblock"
PROJECT_ID = "loopblock-1118"


class _Holder:
    """A `threading.Lock` owned and held by a SEPARATE THREAD.

    `threading.Lock` can only be released by the thread that took it, so the
    release has to be decided here: the coroutine sets an Event when its
    condition is met, and this thread — the only one allowed to — drops the lock
    and, at that instant, samples how many loop turns happened while it held it.
    """

    def __init__(self, safety_s: float = SAFETY_S):
        self.lock = threading.Lock()
        # Published by the counter task, read by the holder thread.
        self.counter = [0]
        # None until the holder has sampled it, i.e. until the lock is dropped.
        self.ticks_while_held: int | None = None
        self.released_at: float | None = None
        self._release = threading.Event()
        self._safety_s = safety_s
        self._thread: threading.Thread | None = None

    def _hold(self) -> None:
        self.lock.acquire()
        try:
            self._release.wait(timeout=self._safety_s)
            self.ticks_while_held = int(self.counter[0])
            self.released_at = time.monotonic()
        finally:
            self.lock.release()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._hold,
                                        name="held-engine-lock", daemon=True)
        self._thread.start()

    def release(self) -> None:
        self._release.set()

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


class _Resolver:
    def get_node(self, step_id):
        return None


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Conn:
    """Just enough of skillflow's connection for the claimed-row scan."""

    def execute(self, sql, params=()):
        claimed_at = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        return _Cursor({"step_instance_id": "si-1", "step_id": "a",
                        "claimed_at": claimed_at})


class _EngineBase:
    """The engine surface `_check_hung_claims` uses, with ONE stalling method.

    `stalls` names the single site under test; every other call returns at once,
    so a witness says "the loop was free at THIS call" rather than "somewhere in
    the job". The stalling method does what the real engine does — `with
    self._lock:` around the read — so it blocks for exactly as long as a worker
    thread holds that lock, which is the production shape.
    """

    def __init__(self, holder: _Holder, stalls: str):
        self._lock = holder.lock
        self._holder = holder
        self._stalls = stalls
        self._stale_threshold = 180
        self.reached = threading.Event()
        self.acquired_at: float | None = None

    # ── the stall every case is built around ────────────────────────────
    def _enter(self, name: str) -> None:
        if name != self._stalls:
            return
        self.reached.set()
        with self._lock:          # what `skillflow/core.py:662 _ro` does
            pass
        self.acquired_at = time.monotonic()

    # ── the engine surface (order and signatures as `_check_hung_claims`) ─
    def recover_stale_claims(self, threshold):
        self._enter("recover_stale_claims")
        return [RUN_ID] if self._stalls == "get_run" else []

    def get_run(self, run_id):
        self._enter("get_run")
        return {"id": run_id, "project_id": PROJECT_ID, "status": "running"}

    def list_runs(self, **kwargs):
        self._enter("list_runs")
        # Both of these sites are reached only through a run row: the resolver is
        # asked about the claimed step of a run, and the claim scan needs a run to
        # scan. Every other case scans nothing, so its stall is the whole job.
        if self._stalls in ("_get_resolver_for_run", "claim_scan"):
            return [{"id": RUN_ID, "project_id": PROJECT_ID}]
        return []

    def _get_resolver_for_run(self, run_id):
        self._enter("_get_resolver_for_run")
        return _Resolver()

    @property
    def _conn(self):
        if self._stalls == "claim_scan":
            # The scan reached the connection, i.e. it ALREADY waited for the
            # holder: `_claimed_step_row` enters `_sf_statement_lock(sf)` before
            # it reads `sf._conn`. Reading this property is how that wait is
            # confirmed, and it must not take `self._lock` again — the test's
            # Lock is not re-entrant and the scan already holds it.
            self.reached.set()
            self.acquired_at = time.monotonic()
        return _Conn()


class _Engine(_EngineBase):
    def reconcile_active_operations(self, **kwargs):
        self._enter("reconcile_active_operations")
        return {}


class _LegacyEngine(_EngineBase):
    """The `else` arm: an engine that predates `reconcile_active_operations`.

    `hasattr(sf, "reconcile_active_operations")` has to be False for that arm to
    run at all, so this is a class without the method rather than a flag.
    """

    def audit_operation_owners(self):
        self._enter("audit_operation_owners")
        return {}


def _engine_for(site: str):
    cls = _LegacyEngine if site == "audit_operation_owners" else _Engine
    return cls


@dataclass
class Witness:
    site: str
    ticks_while_held: int | None = None
    reached_engine: bool = False
    acquired_at: float | None = None
    released_at: float | None = None
    completed: bool = False
    error: str | None = None
    elapsed_s: float = 0.0


async def _until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.01)


async def tick_with_engine_lock_held(site: str, *, margin: int = MARGIN,
                                     safety_s: float = SAFETY_S,
                                     wait_for_s: float = WAIT_FOR_S) -> Witness:
    """Drive the real `_check_hung_claims` beside a counter, lock held.

    The base-arm probe (a scratch copy of the tree with the base version of
    `core/scheduler.py`) calls THIS function and prints the witness; it does not
    assert, which is what makes the green/base comparison the same measurement.
    """
    witness = Witness(site=site)
    holder = _Holder(safety_s)
    engine = _engine_for(site)(holder, site)
    started = time.monotonic()
    holder.start()

    async def _count_loop_turns():
        while True:
            await asyncio.sleep(0.01)
            holder.counter[0] += 1

    with mock.patch.object(scheduler, "get_skillflow", lambda: engine):
        tick = asyncio.create_task(scheduler._check_hung_claims())
        counter = asyncio.create_task(_count_loop_turns())
        try:
            # Hold the lock until the CONDITION — the loop advanced the counter
            # by a real margin — with a bound, so a frozen loop reports rather
            # than hangs.
            await asyncio.wait_for(
                _until(lambda: holder.counter[0] >= margin), timeout=wait_for_s)
        finally:
            holder.release()
            try:
                await asyncio.wait_for(tick, timeout=wait_for_s)
                witness.completed = True
            except Exception as exc:                      # noqa: BLE001
                witness.error = f"{type(exc).__name__}: {exc}"
            # Sampled AFTER the supervisor finished, not at the instant the lock
            # was released. An offloaded site is WOKEN by that release: on the
            # delivered tree the claim scan waits for `sf._lock` on a worker
            # before the resolver is ever reached, so a sample taken at release
            # time measures the wake-up latency and calls a site unreachable that
            # ran milliseconds later (measured 2026-11-18 on the merged tree,
            # where it was the only red in the 58-test trial selection).
            witness.reached_engine = engine.reached.is_set()
            counter.cancel()
            await asyncio.gather(counter, return_exceptions=True)

    holder.join(wait_for_s)
    witness.ticks_while_held = holder.ticks_while_held
    witness.acquired_at = engine.acquired_at
    witness.released_at = holder.released_at
    witness.elapsed_s = time.monotonic() - started
    return witness


@pytest.mark.parametrize("site", SITES)
async def test_the_loop_keeps_ticking_while_the_engine_lock_is_held(site):
    witness = await tick_with_engine_lock_held(site)

    # The site under test is the one that actually ran: no early return, no
    # different branch. Without this the witness could be about nothing.
    assert witness.reached_engine, (
        f"_check_hung_claims never reached {site}; the witness would be empty")

    # THE PROPERTY. The counter advanced by a real margin while the engine call
    # was waiting for a lock another thread held. On the base tree this is 0:
    # the coroutine waited for the lock on the loop thread and the loop served
    # nothing at all until the lock was dropped.
    assert witness.ticks_while_held is not None
    assert witness.ticks_while_held >= MARGIN, (
        f"the loop served {witness.ticks_while_held} turn(s) while "
        f"{site} waited for the engine lock; expected at least {MARGIN} — the "
        f"call is being made ON the event loop, not via asyncio.to_thread")

    # The engine call really waited for the holder: it took the lock only after
    # the holder dropped it. Ordering of two monotonic readings, not a duration.
    assert witness.acquired_at is not None and witness.released_at is not None
    assert witness.acquired_at >= witness.released_at

    # ...and the supervisor completed NORMALLY once the lock came back: the fix
    # is a thread hop, not a timeout that skips the work and not a swallowed
    # failure.
    assert witness.error is None, f"{site}: {witness.error}"
    assert witness.completed
