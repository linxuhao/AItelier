"""A fenced tick must not also report the step as confirmed.

Live, 2026-09-05 activation smoke: a run stopped mid-step logged both

    outcome=cancelled_mid_step run=cb7ee2b7 step=implement … delivery … refused
    outcome=executed          run=cb7ee2b7 step=implement confirmed=True

`confirmed` was the `_executed` flag — "the runner returned" — set before
`confirm_step` is even attempted. So the second line said the delivery was
confirmed about the one case where it had just been refused. The DB was right
throughout; only the log contradicted itself.
"""
from unittest.mock import MagicMock

import pytest
from skillflow.exceptions import TerminalRunFenced

from core import scheduler


def _async(fn):
    async def inner(*a, **kw):
        return fn(*a, **kw)
    return inner


@pytest.fixture
def tick(monkeypatch):
    sf = MagicMock()
    sf.trace_query.return_value = [[0]]
    sf._get_resolver_for_run.return_value.is_tool.return_value = False
    claimed = MagicMock()
    claimed.step_id = "implement"
    claimed.token.step_instance_id = 7
    claimed.token.version = 1
    sf.claim_next_step.return_value = claimed
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run", lambda p: "run1")
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *a: False)
    monkeypatch.setattr(scheduler, "_advance_off_the_loop",
                        _async(lambda *a: "implement"))
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda p: None)
    logged = []
    monkeypatch.setattr(scheduler, "tick_log",
                        lambda pid, outcome, **kw: logged.append((outcome, kw)))
    runner = MagicMock()
    runner.execute = _async(lambda step: MagicMock())
    monkeypatch.setattr("aitelier.runner.AgentStepRunner", lambda **kw: runner)
    return sf, logged


async def test_a_fenced_tick_reports_only_the_cancellation(tick):
    sf, logged = tick
    sf.confirm_step.side_effect = TerminalRunFenced(
        "Run 'run1' is failed; delivery of step 'implement' refused")

    await scheduler._run_skillflow_tick("p1", None)

    outcomes = [o for o, _ in logged]
    assert "cancelled_mid_step" in outcomes, logged
    assert "executed" not in outcomes, (
        "a fenced tick also claimed the step executed: " + repr(logged))


async def test_a_normal_tick_still_reports_executed_and_confirmed(tick):
    sf, logged = tick

    await scheduler._run_skillflow_tick("p1", None)

    ex = [kw for o, kw in logged if o == "executed"]
    assert len(ex) == 1, logged
    assert ex[0]["confirmed"] is True
    assert ex[0]["step"] == "implement"


async def test_confirmed_is_false_when_the_confirm_itself_failed(tick):
    """`confirmed` must track the confirm, not the runner returning."""
    sf, logged = tick
    sf.confirm_step.side_effect = RuntimeError("database is locked")

    await scheduler._run_skillflow_tick("p1", None)

    ex = [kw for o, kw in logged if o == "executed"]
    assert len(ex) == 1, logged
    assert ex[0]["confirmed"] is False, (
        "the runner returned but the confirm failed, and the log said confirmed")
    sf.fail_step.assert_called_once()
