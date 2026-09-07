"""What the tick's log says about a step, and what it is entitled to say.

Live, 2026-09-05 activation smoke: a run stopped mid-step logged both

    outcome=cancelled_mid_step run=cb7ee2b7 step=implement … delivery … refused
    outcome=executed          run=cb7ee2b7 step=implement confirmed=True

`confirmed` was the `_executed` flag — "the runner returned" — set before
`confirm_step` is even attempted, so it reported the delivery as confirmed about
precisely the case where it had just been fenced.

An independent review then showed the first repair still overstated: setting the
flag on any NORMAL return of `confirm_step` is not acceptance either.
`confirm_step` returns normally after an output-schema failure, a `validation:`
retry, a lifecycle-hook retry and a lifecycle failure — none of which reach the
completion UPDATE. The field is therefore named for the fact that is actually
observed, `confirm_returned`, and the last test here drives a REAL validation
retry to show why the distinction is not academic.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.exceptions import TerminalRunFenced
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.workspace import WorkspaceManager

from core import scheduler


def _async(fn):
    async def inner(*a, **kw):
        return fn(*a, **kw)
    return inner


def _wire(monkeypatch, sf, runner, node="implement"):
    """Patch only the tick's collaborators — never skillflow's internals."""
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run", lambda p: RUN["id"])
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *a: False)
    monkeypatch.setattr(scheduler, "_advance_off_the_loop", _async(lambda *a: node))
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda p: None)
    monkeypatch.setattr("aitelier.runner.AgentStepRunner", lambda **kw: runner)
    logged = []
    monkeypatch.setattr(scheduler, "tick_log",
                        lambda pid, outcome, **kw: logged.append((outcome, kw)))
    return logged


RUN = {"id": "run1"}


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
    runner = MagicMock()
    runner.execute = _async(lambda step: MagicMock())
    logged = _wire(monkeypatch, sf, runner)
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


async def test_a_normal_tick_reports_that_the_confirm_returned(tick):
    sf, logged = tick

    await scheduler._run_skillflow_tick("p1", None)

    ex = [kw for o, kw in logged if o == "executed"]
    assert len(ex) == 1, logged
    assert ex[0]["confirm_returned"] is True
    assert "confirmed" not in ex[0], (
        "the log still claims acceptance it cannot observe")
    assert ex[0]["step"] == "implement"


async def test_confirm_returned_is_false_when_the_confirm_raised(tick):
    sf, logged = tick
    sf.confirm_step.side_effect = RuntimeError("database is locked")

    await scheduler._run_skillflow_tick("p1", None)

    ex = [kw for o, kw in logged if o == "executed"]
    assert len(ex) == 1, logged
    assert ex[0]["confirm_returned"] is False
    sf.fail_step.assert_called_once()


# ── the case the review asked for: a REAL normal return that is a rejection ──

BROKEN = "def broken(:\n"


def _lint(files, workspace_root="", **kw):
    """Hermetic stand-in for the `lint` tool: compile each match, report failures."""
    root = Path(workspace_root)
    bad = []
    for pattern in files:
        for fp in sorted(root.rglob(pattern)):
            try:
                compile(fp.read_text(), str(fp), "exec")
            except SyntaxError as e:
                bad.append({"file": fp.name, "passed": False,
                            "error": f"{fp.name}: {e.msg}"})
    return {"all_passed": not bad, "results": bad}


async def test_a_normal_confirm_return_is_not_acceptance(tmp_path, monkeypatch):
    """A REAL `validation:` failure. `confirm_step` returns normally, the step is
    NOT completed — it goes back to `pending` to be re-run — and the tick must
    not describe that as confirmed.

    This is the gap the independent review found: the previous repair set its
    flag on any normal return, so a rejected output still read as accepted.
    """
    class _Loader:
        """Duck-typed tool loader — skillflow calls load_fn/load_schema.

        Local rather than skillflow's test helper: that helper lives in
        skillflow's own test package, which is not importable from here.
        """
        def __init__(self, tools):
            self._tools = tools

        def load_fn(self, name):
            return self._tools[name]

        def load_schema(self, name):
            return {}

        def is_native(self, name):
            return True

    MockToolLoader = _Loader

    sf = SkillFlow(":memory:")
    sf.register_agent_config("maker")
    sf.register_graph(PipelineGraph(
        name="g", begin="implement",
        steps=[StepNode(id="implement", step_type="agent", agent_config="maker",
                        output_mode="write", max_retries=3,
                        validation=[{"files": ["*.py"], "tool": "lint"}],
                        transitions=[Transition(to=None)])]))
    sf._tool_loader = MockToolLoader({"lint": _lint})
    sf._workspace = WorkspaceManager(str(tmp_path / "ws"))
    run_id = sf.create_run("g", {"project_id": "pid"}, project_id="pid")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    RUN["id"] = run_id

    class Runner:
        async def execute(self, claimed):
            # stage output that the validator will reject
            tmp = sf._workspace.get_step_tmp_dir("pid", "g", "implement")
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / "bad.py").write_text(BROKEN)
            return StepResult(outputs={}, flags={})

    logged = _wire(monkeypatch, sf, Runner())

    await scheduler._run_skillflow_tick("pid", None)

    row = sf._conn.execute(
        "SELECT status, validation_retry_count FROM skillflow_steps "
        "WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    # The premise: confirm_step did NOT raise, and the step was NOT accepted.
    assert row["status"] == "pending", dict(row)
    assert row["validation_retry_count"] >= 1, dict(row)

    ex = [kw for o, kw in logged if o == "executed"]
    assert len(ex) == 1, logged
    assert ex[0]["confirm_returned"] is True
    assert "confirmed" not in ex[0], (
        "a validation-rejected step was logged as confirmed — the exact "
        "overstatement this field was renamed to stop")


async def test_output_ceiling_tick_never_confirms_or_retries(tick, monkeypatch):
    from core.dpe_pipeline import NativeOutputCapExhausted
    sf, logged = tick
    runner = MagicMock()

    async def exhausted(_claimed):
        raise NativeOutputCapExhausted("output cap ceiling; draft retained")

    runner.execute = exhausted
    monkeypatch.setattr("aitelier.runner.AgentStepRunner", lambda **kw: runner)
    await scheduler._run_skillflow_tick("p1", None)
    sf.confirm_step.assert_not_called()
    sf.fail_step.assert_called_once()
    assert sf.fail_step.call_args.kwargs["retryable"] is False
    assert not any(kw.get("confirm_returned") for _, kw in logged)
