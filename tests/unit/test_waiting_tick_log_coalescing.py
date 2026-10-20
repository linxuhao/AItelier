"""Normal waiting ticks stay bounded without hiding which project or why.

`_get_or_create_skillflow_run` logs `awaiting_brief` / `awaiting_seed` and
returns None; the ordinary tick then logs `no_run` for the same project. The
old coalescing used ONE global slot for `awaiting_brief` and none at all for
`awaiting_seed` / `no_run`, so a second waiting project was hidden behind the
first and the unchanged repeats of the others were never bounded.

These tests drive the ordinary `_run_skillflow_tick` and the real `tick_log`
producer, with doubles only for storage, the clock and the missing-input
questions. They were authored without being run.
"""
import logging
from unittest.mock import MagicMock, patch

import pytest

from core import scheduler

_MISSING = [{"config": "meta_conversation", "step": "finalize",
             "output": "step1_goals.json", "reader": "1"}]


class _Clock:
    def __init__(self, now: float = 50_000.0):
        self.now = now

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __getattr__(self, name):
        import time
        return getattr(time, name)


@pytest.fixture
def lines(monkeypatch):
    out: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            out.append(record.getMessage())

    lg = logging.getLogger("aitelier.scheduler.tick.test_waiting")
    lg.handlers = [_Sink()]
    lg.propagate = False
    lg.setLevel(logging.INFO)
    monkeypatch.setattr(scheduler, "_get_tick_logger", lambda: lg)
    monkeypatch.setattr(scheduler, "_tick_last_idle", 0.0)
    monkeypatch.setattr(scheduler, "_tick_last_paused", {})
    monkeypatch.setattr(scheduler, "_tick_last_wait", {})
    monkeypatch.setattr(scheduler, "_tick_last_stuck", {})
    return out


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(scheduler, "_time", c)
    return c


class _World:
    """Per-project input state behind the ordinary tick."""

    def __init__(self):
        self.projects = {}      # pid -> project row
        self.missing = {}       # pid -> missing_cross_config_inputs result
        self.seed = {}          # pid -> (published, reason) or None (published)

    def add(self, pid, *, missing=(), seed=None, config="dpe_game"):
        self.projects[pid] = {"project_id": pid, "config_name": config,
                              "meta_state": None, "brief": "", "status": "planning"}
        self.missing[pid] = list(missing)
        self.seed[pid] = seed


def _patches(world, monkeypatch):
    sf = MagicMock()
    sf._conn.execute.return_value.fetchone.return_value = None
    sf.get_or_create_run.return_value = "run_new"
    sf.get_run.return_value = {"status": "completed", "error_reason": None}
    sf.trace_query.return_value = [[0]]
    db = MagicMock()
    db.get_project.side_effect = lambda pid: world.projects.get(pid)

    def _missing(_sf, _config, pid):
        return world.missing[pid]

    manifest = MagicMock(seed_file="plan.md")
    registry = MagicMock()
    registry.get.return_value = manifest
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "db", db)
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *a: False)
    monkeypatch.setattr(scheduler, "_advance_recording_crashes", lambda *a: None)
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda pid: None)
    monkeypatch.setattr(scheduler, "_quota_hold_remaining", lambda: 0)
    monkeypatch.setattr(scheduler.run_isolation, "ensure_for_run",
                        lambda *a, **k: {"mode": "direct"})
    return [
        patch("core.run_launcher.missing_cross_config_inputs", side_effect=_missing),
        patch("api.dependencies.get_config_registry", return_value=registry),
        patch("core.seed_publication.reads_own_seed",
              side_effect=lambda graph, config, seed_file: True),
        # The "directory" is the project id, so the double can answer per project.
        patch("core.seed_publication.seed_dir",
              side_effect=lambda _sf, pid, config: pid),
        patch("core.seed_publication.seed_is_published",
              side_effect=lambda directory, seed_file: world.seed[directory] or (True, "")),
        patch("core.seed_publication.review_seed_context", return_value={}),
    ]


@pytest.fixture
def tick(monkeypatch):
    world = _World()
    patchers = _patches(world, monkeypatch)
    for p in patchers:
        p.start()
    try:
        async def _tick(pid):
            await scheduler._run_skillflow_tick(pid, None)
        _tick.world = world
        yield _tick
    finally:
        for p in patchers:
            p.stop()


def _for(lines, pid, outcome):
    return [l for l in lines
            if l.startswith(f"project={pid} outcome={outcome}")]


class TestMissingBriefRepeats:
    async def test_unchanged_awaiting_brief_and_no_run_are_bounded(
            self, lines, clock, tick):
        tick.world.add("p1", missing=_MISSING)
        for _ in range(12):
            await tick("p1")
            clock.advance(5)
        assert len(_for(lines, "p1", "awaiting_brief")) == 1
        assert len(_for(lines, "p1", "no_run")) == 1, \
            "no_run follows awaiting_brief on every tick and must not reset it"

    async def test_the_first_useful_line_names_project_and_cause(
            self, lines, clock, tick):
        tick.world.add("p1", missing=_MISSING)
        await tick("p1")
        brief = _for(lines, "p1", "awaiting_brief")
        assert len(brief) == 1
        assert "step1_goals.json" in brief[0] and "meta_conversation" in brief[0]
        assert len(_for(lines, "p1", "no_run")) == 1

    async def test_heartbeat_returns_after_the_existing_window(
            self, lines, clock, tick):
        tick.world.add("p1", missing=_MISSING)
        await tick("p1")
        clock.advance(scheduler._TICK_HOLD_HEARTBEAT_S - 1)
        await tick("p1")
        assert len(_for(lines, "p1", "awaiting_brief")) == 1
        assert len(_for(lines, "p1", "no_run")) == 1
        clock.advance(1)
        await tick("p1")
        assert len(_for(lines, "p1", "awaiting_brief")) == 2
        assert len(_for(lines, "p1", "no_run")) == 2

    async def test_a_second_project_is_not_hidden_by_the_first(
            self, lines, clock, tick):
        tick.world.add("p1", missing=_MISSING)
        tick.world.add("p2", missing=_MISSING)
        for _ in range(4):
            await tick("p1")
            await tick("p2")
            clock.advance(5)
        for pid in ("p1", "p2"):
            assert len(_for(lines, pid, "awaiting_brief")) == 1, pid
            assert len(_for(lines, pid, "no_run")) == 1, pid

    async def test_a_changed_cause_is_logged_immediately(
            self, lines, clock, tick):
        tick.world.add("p1", missing=_MISSING)
        await tick("p1")
        tick.world.missing["p1"] = [{"config": "meta_conversation",
                                     "step": "finalize",
                                     "output": "step2_plan.json", "reader": "1"}]
        await tick("p1")
        brief = _for(lines, "p1", "awaiting_brief")
        assert len(brief) == 2
        assert "step2_plan.json" in brief[1]

    async def test_the_drafting_flag_is_bounded_per_project_too(
            self, lines, clock, tick):
        tick.world.add("p1")
        tick.world.projects["p1"]["meta_state"] = "drafting"
        tick.world.add("p2")
        tick.world.projects["p2"]["meta_state"] = "drafting"
        for _ in range(3):
            await tick("p1")
            await tick("p2")
        assert len(_for(lines, "p1", "awaiting_brief")) == 1
        assert len(_for(lines, "p2", "awaiting_brief")) == 1


class TestMissingSeedRepeats:
    async def test_unchanged_awaiting_seed_is_bounded_per_project(
            self, lines, clock, tick):
        tick.world.add("p1", seed=(False, "plan.md is not published"),
                       config="coding_impl")
        tick.world.add("p2", seed=(False, "plan.md is not published"),
                       config="coding_impl")
        for _ in range(6):
            await tick("p1")
            await tick("p2")
            clock.advance(5)
        for pid in ("p1", "p2"):
            seed = _for(lines, pid, "awaiting_seed")
            assert len(seed) == 1, pid
            assert "seed=plan.md" in seed[0] and "not published" in seed[0]
            assert len(_for(lines, pid, "no_run")) == 1, pid

    async def test_a_changed_seed_reason_is_logged_immediately(
            self, lines, clock, tick):
        tick.world.add("p1", seed=(False, "plan.md is missing"),
                       config="coding_impl")
        await tick("p1")
        tick.world.seed["p1"] = (False, "plan.md is empty")
        await tick("p1")
        seed = _for(lines, "p1", "awaiting_seed")
        assert len(seed) == 2
        assert "plan.md is empty" in seed[1]

    async def test_seed_heartbeat_uses_the_existing_window(
            self, lines, clock, tick):
        tick.world.add("p1", seed=(False, "plan.md is missing"),
                       config="coding_impl")
        await tick("p1")
        clock.advance(scheduler._TICK_HOLD_HEARTBEAT_S)
        await tick("p1")
        assert len(_for(lines, "p1", "awaiting_seed")) == 2


class TestProgressAndReturnToWaiting:
    async def test_ready_input_runs_and_a_later_wait_is_immediate(
            self, lines, clock, tick):
        tick.world.add("p1", missing=_MISSING)
        await tick("p1")
        await tick("p1")
        assert len(_for(lines, "p1", "awaiting_brief")) == 1

        # The brief arrives: the same ordinary tick now creates a run and the
        # real producer reports real progress for the project.
        tick.world.missing["p1"] = []
        await tick("p1")
        assert len(_for(lines, "p1", "terminal")) == 1

        # Back to waiting with the SAME cause, well inside the old window:
        # real progress in between means it must be visible at once.
        tick.world.missing["p1"] = _MISSING
        clock.advance(5)
        await tick("p1")
        assert len(_for(lines, "p1", "awaiting_brief")) == 2
        assert len(_for(lines, "p1", "no_run")) == 2

    async def test_progress_on_one_project_does_not_reset_another(
            self, lines, clock, tick):
        tick.world.add("p1", missing=_MISSING)
        tick.world.add("p2", missing=_MISSING)
        await tick("p1")
        await tick("p2")
        tick.world.missing["p2"] = []
        await tick("p2")                      # p2 progresses
        clock.advance(5)
        await tick("p1")                      # p1 unchanged repeat
        assert len(_for(lines, "p1", "awaiting_brief")) == 1
        assert len(_for(lines, "p1", "no_run")) == 1


class TestOtherOutcomesAreUntouched:
    def test_a_real_outcome_is_still_never_coalesced(self, lines, clock):
        for i in range(4):
            scheduler.tick_log("p1", "executed", step=f"s{i}")
        assert len(lines) == 4

    def test_paused_terminal_heartbeat_is_unchanged(self, lines, clock):
        for _ in range(5):
            scheduler.tick_log("p1", "terminal", run="r1", status="paused")
        assert len(lines) == 1
        clock.advance(scheduler._TICK_PAUSED_HEARTBEAT_S)
        scheduler.tick_log("p1", "terminal", run="r1", status="paused")
        assert len(lines) == 2

    def test_worktree_retention_lines_are_not_coalesced(self, lines, clock):
        for _ in range(3):
            scheduler.tick_log("", "worktree_retained", run="r1", reason="dirty")
        assert len(lines) == 3

