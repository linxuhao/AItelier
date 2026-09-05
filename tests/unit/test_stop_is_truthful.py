"""What a stop promises, and what the host does when it lands mid-step.

On 2026-09-05 `stop_pipeline` answered "Pipeline stopped." and the step it had
not stopped committed `ac5237b` forty seconds later. The message was
unconditional — it could not have said anything else, because `fail_run`
returned None and the butler had nothing to report from.

A stop now does three separable things and the caller is told which it got:
no further steps are claimed (always); an in-flight step that had not reached
its lifecycle hooks is closed before delivering (reported in `steps_closed`);
and one that HAD reached them is not preempted at all (reported in
`deliveries_in_flight`, with a warning to go and look at the repository).
"""

from unittest.mock import MagicMock

import pytest
from skillflow.exceptions import TerminalRunFenced

from core import scheduler
from core.meta_agent import MetaAgent


# ── the butler's message ─────────────────────────────────────────────

class _Butler:
    """The two methods `_tool_stop_pipeline` actually uses."""
    def __init__(self, run):
        self._run = run
        self._resolve_run_row = lambda rid: run
    _tool_stop_pipeline = MetaAgent._tool_stop_pipeline


def _stop(monkeypatch, report, status="running"):
    sf = MagicMock()
    sf.fail_run.return_value = report
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf, raising=False)
    b = _Butler({"id": "r1", "status": status})
    return b._tool_stop_pipeline({"run_id": "r1", "reason": "operator stop"})


def test_a_stop_that_prevented_the_delivery_says_so(monkeypatch):
    out = _stop(monkeypatch, {"run_id": "r1", "status": "failed",
                              "steps_closed": ["implement"],
                              "deliveries_in_flight": []})
    assert out["status"] == "stopped"
    assert out["steps_closed"] == ["implement"]
    assert "no further steps" in out["message"]
    assert "nothing was promoted or committed" in out["message"]
    assert "WARNING" not in out["message"]


def test_a_stop_that_arrived_too_late_warns_about_the_repository(monkeypatch):
    """The incident's shape. The one message that must never read as clean."""
    out = _stop(monkeypatch, {"run_id": "r1", "status": "failed",
                              "steps_closed": [],
                              "deliveries_in_flight": ["implement"]})
    assert out["deliveries_in_flight"] == ["implement"]
    assert "WARNING" in out["message"]
    assert "not preempted" in out["message"].lower()
    assert "check the repository" in out["message"]


def test_a_stop_between_steps_claims_nothing_more_and_nothing_less(monkeypatch):
    out = _stop(monkeypatch, {"run_id": "r1", "status": "failed",
                              "steps_closed": [], "deliveries_in_flight": []})
    assert out["message"] == ("Pipeline stopped; no further steps will be "
                              "claimed.")


def test_an_older_skillflow_returning_none_does_not_break_the_butler(monkeypatch):
    """`fail_run` returned None before this change; the host must not assume."""
    out = _stop(monkeypatch, None)
    assert out["status"] == "stopped"
    assert out["steps_closed"] == [] and out["deliveries_in_flight"] == []


def test_an_already_terminal_run_is_still_refused_early(monkeypatch):
    out = _stop(monkeypatch, {"steps_closed": []}, status="failed")
    assert out["status"] == "failed" and "nothing to stop" in out["message"]


# ── the tick ─────────────────────────────────────────────────────────

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
    return sf, logged


def _async(fn):
    async def inner(*a, **kw):
        return fn(*a, **kw)
    return inner


async def test_a_fenced_confirm_is_not_recorded_as_a_step_failure(tick,
                                                                  monkeypatch):
    """`fail_step` would put the step back to `pending` and undo the close the
    cancellation just made. There is no failure here: the work was refused."""
    sf, logged = tick
    sf.confirm_step.side_effect = TerminalRunFenced(
        "Run 'run1' is failed; delivery of step 'implement' refused")
    runner = MagicMock()
    runner.execute = _async(lambda step: MagicMock())
    monkeypatch.setattr("aitelier.runner.AgentStepRunner",
                        lambda **kw: runner)

    await scheduler._run_skillflow_tick("p1", None)

    sf.fail_step.assert_not_called()
    assert any(o == "cancelled_mid_step" for o, _ in logged), logged
    kw = next(kw for o, kw in logged if o == "cancelled_mid_step")
    assert kw["step"] == "implement" and "refused" in kw["reason"]


# ── the engine does not start a new attempt on a stopped run ─────────

def _engine(status):
    from core.dpe_pipeline import PipelineEngine
    eng = PipelineEngine.__new__(PipelineEngine)
    eng._run_id = "run1"
    sf = MagicMock()
    sf.get_run.return_value = {"status": status}
    sf.TERMINAL_RUN_STATUSES = ("failed", "completed")
    return eng, sf


def test_a_new_attempt_on_a_stopped_run_is_refused(monkeypatch):
    """09:09:35: the budget ran out and the runner began a COMPLETE fresh model
    call, 35 s after the operator was told the pipeline had stopped."""
    eng, sf = _engine("failed")
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf, raising=False)
    with pytest.raises(TerminalRunFenced):
        eng._refuse_if_run_cancelled("implement", 2)


def test_a_healthy_run_starts_its_attempts_normally(monkeypatch):
    eng, sf = _engine("running")
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf, raising=False)
    eng._refuse_if_run_cancelled("implement", 2)     # must not raise


def test_the_check_never_breaks_a_step_on_its_own(monkeypatch):
    """A guard whose own failure stops real work is worse than no guard."""
    eng, sf = _engine("running")
    sf.get_run.side_effect = RuntimeError("database is locked")
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf, raising=False)
    eng._refuse_if_run_cancelled("implement", 2)     # must not raise


def test_a_step_with_no_run_id_is_left_alone(monkeypatch):
    from core.dpe_pipeline import PipelineEngine
    eng = PipelineEngine.__new__(PipelineEngine)
    eng._refuse_if_run_cancelled("implement", 1)     # must not raise
