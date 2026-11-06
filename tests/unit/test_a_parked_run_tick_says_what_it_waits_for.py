"""A tick over a run parked at a human checkpoint says what it waits for.

Measured on the shared backend, 2026-11-06T22:29:53Z, every ~5 s, two lines per
poll — one per project — both the same shape:

    project=sg-0bfccd9cdaf8444dae54541198edf4fc outcome=tick_error
      error=run c5d7fc06-6d32-4651-8640-5adb7b58e2f2 is recorded as worktree in
      '/home/linxuhao/.AItelier/worktrees/c5d7fc06-…' (branch 'codex/run/…', …

Both runs were `paused` at a human checkpoint since 2026-10-31 and both recorded
worktree directories had since been reaped, so the line was the isolation
resolver's refusal — correct in itself, since substituting the shared checkout
for a run that owns no tree is exactly what `core/run_isolation.py` exists to
refuse — being raised on an every-tick basis, burying every other tick outcome.

The reachable path: Phase A's `_advance_off_the_loop` runs BEFORE the tick looks
at `run["status"]`, and advancing resolves the run's code path
(`api/dependencies._existing_repo_code_path` → `run_isolation.resolve_for_resolver`),
so the refusal escaped the whole tick into `_on_detached_tick_done`.

A run parked at a checkpoint has nothing for the tick to advance. These tests
pin both halves of the intended change:

* parked → the tick returns normally and reports the pause it is waiting on;
* NOT parked → the isolation failure is still reported on the tick surface, and
  no path is substituted for the run.

They drive the REAL tick entry through the real poller dispatch
(`_dispatch_detached_ticks`, which attaches `_on_detached_tick_done` — the one
place `tick_error` is written), and the fixture's `advance_run` asks the REAL
`run_isolation.resolve_for_resolver` for its code path, exactly as
`_existing_repo_code_path` does. So the failure the unparked case must keep
producing is the production one — not a raised mock of it.

Doubles cover storage, the clock, the run lookup and the graph: nothing here
rewrites the resolver, `core/run_isolation.py` is not touched by this change.
"""
import asyncio
import logging

import pytest

from core import run_isolation
from core import scheduler

PROJECT = "proj-parked-tick"
RUN_ID = "parked-run-0001"
PAUSED_SINCE = "2026-10-31 09:12:44"
CHECKPOINT_LABEL = "Approve the task plan"


# ── the world behind the real tick ──────────────────────────────────────────

class _Node:
    def __init__(self, checkpoint=False, checkpoint_label=None):
        self.checkpoint = checkpoint
        self.checkpoint_label = checkpoint_label


class _Resolver:
    """Enough of the graph resolver for the tick's peek and the label scan."""

    def __init__(self, nodes):
        self._nodes = nodes

    def is_tool(self, step_id):
        return False

    def get_node(self, step_id):
        return self._nodes.get(step_id)


class _NoClaimRows:
    """`_has_active_claim` asks the engine's connection; nothing is claimed."""

    def execute(self, *args, **kwargs):
        return self

    def fetchone(self):
        return None


class _Skillflow:
    """A skillflow whose advance_run resolves its code path before advancing.

    `resolve_for_resolver` is the real function, with the real record written by
    the fixture, so an unparked tick fails the way production fails. A paused
    tick must never get here at all — `advance_calls` is the measurement.
    """

    def __init__(self, db, run, steps, nodes):
        self._db = db
        self._run = dict(run)
        self._steps = [dict(s) for s in steps]
        self._nodes = nodes
        self._conn = _NoClaimRows()
        self.advance_calls = 0
        self.resolved = None          # a path here would be a substitution
        self.isolation_error = None
        self.traces = []

    # reads the tick makes on its way to the decision
    def get_run(self, run_id):
        return dict(self._run)

    def get_steps(self, run_id):
        return [dict(s) for s in self._steps]

    def _get_resolver_for_run(self, run_id):
        return _Resolver(self._nodes)

    def _should_delegate_tool(self, tool_name):
        return False

    def trace(self, *args, **kwargs):
        self.traces.append((args, kwargs))

    def trace_query(self, *args, **kwargs):
        return []

    # the call the defect was about
    def advance_run(self, run_id):
        self.advance_calls += 1
        try:
            self.resolved = run_isolation.resolve_for_resolver(
                self._db, run_id, sf=self)
        except run_isolation.IsolationUnavailable as exc:
            self.isolation_error = exc
            raise
        raise AssertionError(
            "fixture expects no successful advance: this run's tree is gone")


class _UnreadableLabel(_Skillflow):
    """A paused run whose checkpoint label cannot be read (engine hiccup)."""

    def _get_resolver_for_run(self, run_id):
        raise RuntimeError("graph history unavailable")


class _Clock:
    """Deterministic tick clock, so coalescing windows mean what they say."""

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
    """Capture what `tick_log` writes, with every heartbeat table reset."""
    out: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            out.append(record.getMessage())

    lg = logging.getLogger("aitelier.scheduler.tick.parked")
    lg.handlers = [_Sink()]
    lg.propagate = False
    lg.setLevel(logging.INFO)
    monkeypatch.setattr(scheduler, "_get_tick_logger", lambda: lg)
    monkeypatch.setattr(scheduler, "_tick_last_idle", 0.0)
    monkeypatch.setattr(scheduler, "_tick_last_hold", 0.0)
    monkeypatch.setattr(scheduler, "_tick_last_skip", 0.0)
    monkeypatch.setattr(scheduler, "_tick_last_paused", {})
    monkeypatch.setattr(scheduler, "_tick_last_wait", {})
    monkeypatch.setattr(scheduler, "_tick_last_stuck", {})
    return out


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(scheduler, "_time", c)
    return c


class _Vanished:
    """The fixture's DB plus the worktree path its row names (which is gone)."""

    def __init__(self, db, path):
        self.db = db
        self.path = path


@pytest.fixture
def vanish_worktree(db_manager, tmp_path):
    """Record this run the way the live broken rows are recorded.

    `run_isolation._write_record` is the module's own writer; the row is
    `mode=worktree` and names a path that does not exist, which is what the two
    parked runs' rows held once their trees were reaped.
    """
    gone = tmp_path / "worktrees" / RUN_ID
    run_isolation._write_record(
        db_manager,
        run_id=RUN_ID,
        project_id=PROJECT,
        config_name="dpe_default",
        mode=run_isolation.MODE_WORKTREE,
        source_repo=str(tmp_path / "repo"),
        worktree_path=str(gone),
        branch=f"codex/run/{RUN_ID}",
        base_sha="0" * 40,
    )
    assert not gone.exists(), "the recorded tree must be gone, not merely absent"
    return _Vanished(db_manager, gone)


def _ticks(db, monkeypatch, *, status, skillflow_cls=_Skillflow):
    """Wire the doubles and return (drive_n_ticks, sf)."""
    sf = skillflow_cls(
        db,
        run={"id": RUN_ID, "status": status, "current_node": "t_impl",
             "project_id": PROJECT, "graph_name": "dpe_default",
             "updated_at": PAUSED_SINCE, "context_json": "{}"},
        steps=[{"step_id": "t_plan", "status": "completed"},
               {"step_id": "t_impl", "status": "pending"}],
        nodes={"t_plan": _Node(checkpoint=True,
                               checkpoint_label=CHECKPOINT_LABEL),
               "t_impl": _Node()},
    )
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run",
                        lambda project_id: RUN_ID)
    monkeypatch.setattr(scheduler, "_quota_hold_remaining", lambda: 0.0)

    async def drive(times: int = 1):
        loop = asyncio.get_running_loop()
        results = []
        for _ in range(times):
            tasks = scheduler._dispatch_detached_ticks(
                [{"project_id": PROJECT}], loop)
            assert tasks, "the poller must have dispatched this project's tick"
            results.extend(await asyncio.gather(*tasks,
                                                return_exceptions=True))
            # `_on_detached_tick_done` is a done callback: let it run so the
            # `tick_error` line (if any) is written before we read `lines`.
            for _ in range(2):
                await asyncio.sleep(0)
        return results

    return drive, sf


def _matching(lines, *, outcome=None, status=None):
    return [line for line in lines
            if (outcome is None or f"outcome={outcome}" in line)
            and (status is None or f"status={status}" in line)]


# ── criterion 1: a parked run is told, not raised ───────────────────────────

class TestAParkedTickSaysWhatItWaitsFor:
    async def test_the_tick_returns_normally_and_names_the_pause(
            self, vanish_worktree, lines, clock, monkeypatch):
        drive, sf = _ticks(vanish_worktree.db, monkeypatch, status="paused")

        results = await drive(1)

        assert results == [None], (
            "the tick must return normally for a run parked at a checkpoint; it "
            f"raised instead: {results!r}")
        assert len(lines) == 1, lines
        line = lines[0]
        assert f"project={PROJECT} outcome=terminal" in line
        assert "status=paused" in line
        assert f"run={RUN_ID[:8]}" in line
        assert "step=t_plan" in line, "the checkpoint the run waits on is named"
        assert f"waits={CHECKPOINT_LABEL}" in line
        assert f"since={PAUSED_SINCE}" in line
        assert "recorded as worktree" not in line, (
            "the isolation refusal must not be the thing this tick says")
        assert "isolation" not in line.lower()
        assert sf.advance_calls == 0, (
            "a parked run has nothing to advance, so Phase A must not run for it")

    async def test_no_code_path_is_resolved_for_the_parked_run(
            self, vanish_worktree, lines, clock, monkeypatch):
        """The refusal is avoided, not outrun: asking would still raise.

        `advance_run` is the only caller that resolves a code path in this
        fixture (`_existing_repo_code_path`'s stand-in), so zero calls is zero
        resolutions. The direct call afterwards proves the fixture really is
        one that refuses to serve a path — nothing here made it agreeable.
        """
        drive, sf = _ticks(vanish_worktree.db, monkeypatch, status="paused")

        await drive(1)

        assert sf.advance_calls == 0
        assert sf.resolved is None
        with pytest.raises(run_isolation.IsolationUnavailable) as raised:
            run_isolation.resolve_for_resolver(vanish_worktree.db, RUN_ID, sf=sf)
        assert "Refusing to substitute the source checkout" in str(raised.value)

    async def test_an_unreadable_label_still_leaves_a_stated_pause(
            self, vanish_worktree, lines, clock, monkeypatch):
        """The pause timestamp alone is enough; silence is not.

        `_parked_checkpoint`'s label scan is best-effort on purpose: an engine
        hiccup while reading the graph must not restore the raising tick.
        """
        drive, sf = _ticks(vanish_worktree.db, monkeypatch, status="paused",
                           skillflow_cls=_UnreadableLabel)

        results = await drive(1)

        assert results == [None]
        assert len(lines) == 1, lines
        assert "status=paused" in lines[0]
        assert f"since={PAUSED_SINCE}" in lines[0]
        assert "step=" not in lines[0]
        assert "tick_error" not in lines[0]
        assert sf.advance_calls == 0


# ── criterion 2: an unparked run still fails closed ─────────────────────────

class TestAnUnparkedTickStillFailsClosed:
    async def test_the_isolation_failure_is_still_reported(
            self, vanish_worktree, lines, clock, monkeypatch):
        drive, sf = _ticks(vanish_worktree.db, monkeypatch, status="running")

        await drive(1)

        errors = _matching(lines, outcome="tick_error")
        assert len(errors) == 1, lines
        assert f"project={PROJECT}" in errors[0]
        assert "recorded as worktree" in errors[0], errors[0]
        assert "Refusing to substitute" in str(sf.isolation_error)
        assert not _matching(lines, status="paused"), (
            "the pause line belongs to parked runs only")

    async def test_no_path_is_substituted_for_the_run(
            self, vanish_worktree, lines, clock, monkeypatch):
        """The tick does not invent a tree for a run that owns none.

        The failure is reported, and nothing downstream saw a usable path: the
        resolver raised, `resolved` stayed None, and the only line written is
        the refusal itself (no `executed`, no path-bearing outcome).
        """
        drive, sf = _ticks(vanish_worktree.db, monkeypatch, status="running")

        await drive(1)

        assert sf.advance_calls == 1
        assert sf.resolved is None
        assert sf.isolation_error is not None
        assert [line for line in lines if "outcome=tick_error" in line] == lines
        with pytest.raises(run_isolation.IsolationUnavailable):
            run_isolation.resolve_for_resolver(vanish_worktree.db, RUN_ID, sf=sf)
        assert run_isolation.record(
            vanish_worktree.db, RUN_ID)["worktree_path"] == str(
                vanish_worktree.path), (
            "the recorded tree must still be the vanished one: neither the "
            "shared checkout nor any other path may be substituted for it")


# ── criterion 3: repeated parked ticks are bounded ──────────────────────────

class TestRepeatedParkedTicksAreBounded:
    async def test_five_parked_ticks_write_at_most_one_pause_line(
            self, vanish_worktree, lines, clock, monkeypatch):
        drive, sf = _ticks(vanish_worktree.db, monkeypatch, status="paused")

        results = await drive(5)

        assert results == [None] * 5, results
        pause = [line for line in lines if "status=paused" in line]
        assert len(pause) == 1, (
            f"five ticks over one unchanged pause wrote {len(pause)} lines: "
            f"{lines!r}")
        assert sf.advance_calls == 0

    async def test_a_changed_pause_is_reported_at_once(
            self, vanish_worktree, lines, clock, monkeypatch):
        """Coalescing must not hide a resume-and-re-pause behind its window."""
        drive, sf = _ticks(vanish_worktree.db, monkeypatch, status="paused")
        await drive(1)
        assert len(_matching(lines, status="paused")) == 1

        # The run moved on and parked again at another checkpoint / moment.
        sf._run["updated_at"] = "2026-11-06 22:30:01"
        clock.advance(5)
        await drive(1)

        assert len(_matching(lines, status="paused")) == 2, lines
