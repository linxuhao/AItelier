"""`advance_run` going quiet is not the run ending.

Phase A used to log `outcome=terminal` and return the moment `advance_run`
returned no next node, without asking whether the run had actually finished. A
run that is still `running` then reads exactly like a completed one.

Live, jinyong-camera 2026-08-29: `5_review` sat for three hours emitting
`terminal status=running` every 5s — 2266 identical lines, 02:57:23Z to
06:06:16Z (counted in the tick log; the first report said 200). The step was claimable
the whole time; a hand-called `sf.claim_next_step(run_id)` returned a ClaimedStep
immediately and the run resumed. `advance_run` had nothing to say only because
the node already carried a completed step row from an earlier goal-loop pass.

These tests are the offline reproduction of that shape, which the fix shipped
without: it can be built from a stub, so the branch does not have to wait for
the next three-hour wedge to be exercised.
"""

from unittest.mock import MagicMock

import pytest

from core import scheduler


@pytest.fixture
def sf(monkeypatch):
    """A skillflow stub wired past everything the tick does before Phase A."""
    sf = MagicMock()
    sf.trace_query.return_value = [[0]]          # runaway-loop guard: 0 claims
    sf._get_resolver_for_run.return_value.is_tool.return_value = False
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run", lambda pid: "run1")
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *a: False)
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda pid: None)
    # advance_run has nothing to say — the wedge.
    monkeypatch.setattr(scheduler, "_advance_recording_crashes", lambda *a: None)
    return sf


@pytest.fixture
def ticks(monkeypatch):
    """Capture what tick_log was asked to record, bypassing its coalescing."""
    seen = []
    monkeypatch.setattr(scheduler, "tick_log",
                        lambda pid, outcome, **kw: seen.append((outcome, kw)))
    return seen


async def test_a_running_run_that_cannot_advance_is_not_reported_terminal(sf, ticks):
    """The whole defect: `terminal` is the word that stops the search."""
    sf.get_run.return_value = {"status": "running", "current_node": "5_review"}
    sf.claim_next_step.return_value = None

    await scheduler._run_skillflow_tick("p1", None)

    outcomes = [o for o, _ in ticks]
    assert "terminal" not in outcomes, \
        "a still-running run was reported as terminal"
    assert "wedged" in outcomes


async def test_the_wedge_asks_whether_the_step_is_claimable(sf, ticks):
    """What actually un-wedged the live run was a claim, not the diagnosis.

    Returning on advance's silence alone never asked. Falling through does, and
    when the answer is yes the run resumes on its own.
    """
    sf.get_run.return_value = {"status": "running", "current_node": "5_review"}
    claimed = MagicMock()
    claimed.step_id = "5_review"
    sf.claim_next_step.return_value = claimed

    await scheduler._run_skillflow_tick("p1", None)

    sf.claim_next_step.assert_called_once_with("run1")


@pytest.mark.parametrize("status", ["completed", "failed", "paused"])
async def test_a_genuinely_terminal_run_still_returns_without_claiming(
        sf, ticks, status):
    """The control. Without it these tests would pass on a build that had simply
    deleted the terminal branch and claimed on every silent advance — which for
    a finished run means opening a fresh instance of its last step, forever."""
    sf.get_run.return_value = {"status": status, "current_node": "done"}

    await scheduler._run_skillflow_tick("p1", None)

    assert [o for o, _ in ticks] == ["terminal"]
    sf.claim_next_step.assert_not_called()


def test_wedged_and_no_claim_coalesce_per_project(monkeypatch):
    """A wedge claiming cannot resolve emits both on every tick — ~34k lines a
    day, more than the flood the other heartbeats exist to prevent. Per project,
    because two projects can wedge at once and the operator needs to see which.
    """
    emitted = []
    monkeypatch.setattr(scheduler, "_get_tick_logger",
                        lambda: MagicMock(info=lambda *a: emitted.append(a)))
    monkeypatch.setattr(scheduler, "_tick_last_stuck", {})

    for _ in range(5):
        scheduler.tick_log("p1", "wedged", run="r")
        scheduler.tick_log("p1", "no_claim", run="r")
    scheduler.tick_log("p2", "wedged", run="r")

    assert len(emitted) == 3, \
        f"expected one line per (outcome, project), got {len(emitted)}"
