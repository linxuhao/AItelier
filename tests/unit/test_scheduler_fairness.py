"""One busy project must not freeze every other project.

The poller picked exactly ONE candidate and, if that project's tick was already
in flight, logged `locked` and returned — the tick was spent on a project it
could not advance. Measured on this host: a `dpe_game` step ran 400s and produced
an unbroken run of `outcome=locked` lines while a freshly generated pipeline sat
at its begin node with zero trace rows for over an hour.

The rule now: the SAME project is still strictly serial (the per-project lock is
untouched); DIFFERENT projects proceed together, bounded.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from core import scheduler as sc


@pytest.fixture
def clean_locks():
    sc._tick_locks.clear() if hasattr(sc, "_tick_locks") else None
    yield
    if hasattr(sc, "_tick_locks"):
        sc._tick_locks.clear()


def _projects(*ids):
    return [{"project_id": i} for i in ids]


@pytest.mark.asyncio
async def test_a_busy_project_no_longer_consumes_the_tick(clean_locks):
    """The regression this whole change exists for.

    `busy` holds its lock (a long step is running). Before, it was the single
    pick and the tick ended there; `idle` never advanced. Now `idle` runs.
    """
    sc._get_tick_lock("busy").acquire()
    try:
        ticked = []
        with patch.object(sc.db, "get_active_projects",
                          return_value=_projects("busy", "idle")), \
             patch.object(sc, "_execute_skillflow_tick",
                          side_effect=_recorder(ticked)):
            await sc.poll_and_execute()
        assert ticked == ["idle"], f"expected only the free project to run: {ticked}"
    finally:
        sc._get_tick_lock("busy").release()


@pytest.mark.asyncio
async def test_different_projects_advance_in_the_same_tick(clean_locks):
    ticked = []
    with patch.object(sc.db, "get_active_projects",
                      return_value=_projects("a", "b", "c")), \
         patch.object(sc, "_execute_skillflow_tick",
                      side_effect=_recorder(ticked)):
        await sc.poll_and_execute()
    assert sorted(ticked) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_the_same_project_is_never_advanced_twice_at_once(clean_locks):
    """Serialism within one project is the invariant this must NOT trade away —
    two concurrent ticks on one run double-executed steps and produced version
    conflicts, which is why the lock exists."""
    entered, peak = [], []

    # Mock the INNER tick, not `_execute_skillflow_tick` — the lock lives in the
    # outer one, and mocking that away would test the mock instead of the lock.
    async def _slow_tick(pid, loop):
        entered.append(pid)
        peak.append(len(entered))
        await asyncio.sleep(0.02)
        entered.remove(pid)

    with patch.object(sc.db, "get_active_projects", return_value=_projects("solo")), \
         patch.object(sc, "_run_skillflow_tick", side_effect=_slow_tick):
        await asyncio.gather(sc.poll_and_execute(), sc.poll_and_execute())
    assert peak and max(peak) == 1, f"one project was advanced concurrently: {peak}"


@pytest.mark.asyncio
async def test_all_busy_still_reports_locked_rather_than_going_quiet(clean_locks):
    """Silence and health looked identical before the tick log existed; a tick
    that finds every candidate busy must still say so."""
    sc._get_tick_lock("busy").acquire()
    try:
        logged = []
        with patch.object(sc.db, "get_active_projects", return_value=_projects("busy")), \
             patch.object(sc, "tick_log", side_effect=lambda pid, outcome, **kw:
                          logged.append((pid, outcome))), \
             patch.object(sc, "_execute_skillflow_tick"):
            await sc.poll_and_execute()
        assert ("busy", "locked") in logged
    finally:
        sc._get_tick_lock("busy").release()


@pytest.mark.asyncio
async def test_an_idle_scheduler_still_says_idle(clean_locks):
    logged = []
    with patch.object(sc.db, "get_active_projects", return_value=[]), \
         patch.object(sc, "tick_log", side_effect=lambda pid, outcome, **kw:
                      logged.append((pid, outcome))):
        await sc.poll_and_execute()
    assert ("", "idle") in logged


def _recorder(sink):
    """An async side_effect. `patch.object` on an async function yields an
    AsyncMock, which AWAITS its side_effect's result — a plain lambda returning a
    coroutine leaves it un-awaited, so nothing is recorded and the test fails for
    a reason that has nothing to do with the code under test."""
    async def _tick(pid, loop):
        sink.append(pid)
    return _tick
