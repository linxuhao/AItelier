"""The output budget: observed, acted on, and named when it is what ran out.

Reference sample: attempt-c5aae131eaa246d0bc6d7c72864aea0f (2026-09-20,
config coding_impl, step "implement", role green, max_tool_turns=100). It died
on turn 20 with `written_files == []` — 20% of the turn budget spent, 100% of
the output budget spent. The half-budget guard read only `turn >= max_turns//2`
so it could not fire on that curve by construction.

REFERENCE_CURVE below is copied out of that run's own trace.db
(`select payload_json from skillflow_trace where event='token_usage'`), one row
per turn, together with the cap each turn ran under (32768 up to and including
the turn-16 starve, 65536 after the escalation that starve bought).
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.ai_router import OUTPUT_CAP_CEILING
from core.dpe_pipeline import (
    MaxRetriesExceeded,
    PipelineEngine,
    _output_budget_state,
    _should_intervene_early,
)

# (turn, completion_tokens, reasoning_tokens, starved)
REFERENCE_CURVE = [
    (1, 133, 0, False),
    (2, 274, 114, False),
    (3, 86, 20, False),
    (4, 114, 31, False),
    (5, 129, 31, False),
    (6, 312, 207, False),
    (7, 112, 14, False),
    (8, 2871, 2629, False),
    (9, 98, 0, False),
    (10, 83, 0, False),
    (11, 101, 17, False),
    (12, 180, 46, False),
    (13, 163, 61, False),
    (14, 12832, 12348, False),
    (15, 19251, 18610, False),
    (16, 32768, 31573, True),    # cap 32768 filled -> escalate to 65536
    (17, 46829, 45065, False),
    (18, 36667, 35303, False),
    (19, 19573, 18579, False),
    (20, 65536, 62770, True),    # cap 65536 == ceiling -> attempt dies here
]
REFERENCE_MAX_TURNS = 100
REFERENCE_START_CAP = 32768
CURVE_COMPLETION = sum(row[1] for row in REFERENCE_CURVE)
CURVE_REASONING = sum(row[2] for row in REFERENCE_CURVE)

TS = {"read_file": {}, "list_tree": {}, "write": {}}


class _WS:
    def __init__(self, tmp: Path):
        self.base_path = tmp
        self.projects_base = tmp / "projects"
        self.written_drafts = {}

    def _get_secure_path(self, project_id):
        return self.base_path / project_id

    def get_code_path(self, project_id, run_id=None):
        p = self.projects_base / project_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write_draft(self, project_id, step_id, filename, content, graph_name=None):
        self.written_drafts[f"{step_id}/{filename}"] = content

    def clean_draft_dir(self, project_id, step_id, graph_name=None):
        self.written_drafts = {}


def _turn(text="", tool_calls=None, reasoning="", truncated=False):
    return SimpleNamespace(text=text, tool_calls=tool_calls or [],
                           reasoning_content=reasoning, truncated=truncated)


def _tc(name, args=None, cid="c1"):
    return {"id": cid, "function": {"name": name,
                                    "arguments": json.dumps(args or {})}}


@pytest.fixture
def engine():
    from unittest.mock import patch
    from core.ai_router import AIGateway
    with patch("core.agents.AgentFactory.__init__", return_value=None):
        eng = PipelineEngine()
        eng.factory = MagicMock()
        eng.factory.is_native.return_value = True
        eng.factory.get_fallback_to_json.return_value = False
        eng.factory.get_max_retries.return_value = 1
        eng.factory.get_max_tool_turns.return_value = REFERENCE_MAX_TURNS
        nat = eng.factory.get_native_agent.return_value
        nat.gateway.litellm_model = "mock"
        nat.gateway.last_usage = {}
        nat.gateway.last_outbound = {}
        nat.gateway.max_output_tokens = REFERENCE_START_CAP
        nat.gateway.escalate_output_cap = lambda: AIGateway.escalate_output_cap(
            nat.gateway)
        # The guard is a coding_impl policy; this is the graph the reference
        # attempt ran under.
        eng._draft_graph_name = lambda: "coding_impl"
        return eng


def _replay(engine, ws, *, write_on_first_turn=False, expect_death=True):
    """Drive the REAL native turn loop over the reference consumption curve."""
    nat = engine.factory.get_native_agent.return_value
    calls = {"n": 0}

    def _one_turn(**_kw):
        calls["n"] += 1
        turn, completion, reasoning, starved = REFERENCE_CURVE[calls["n"] - 1]
        nat.gateway.last_usage = {"prompt_tokens": 10000,
                                  "completion_tokens": completion,
                                  "reasoning_tokens": reasoning}
        if starved:
            return _turn(reasoning="r" * reasoning, truncated=True)
        if write_on_first_turn and turn == 1:
            return _turn(tool_calls=[_tc("write", {"file": "main.py",
                                                   "content": "x"}, cid=f"c{turn}")])
        return _turn(tool_calls=[_tc("read_file", {"file": f"src/mod_{turn}.py"},
                                     cid=f"c{turn}")])

    nat.turn.side_effect = _one_turn
    traces = []
    engine._trace_cb = lambda category, event, payload: traces.append(
        (category, event, payload))
    engine._emit = lambda ev, payload=None: None
    engine._exec_tool = MagicMock(side_effect=lambda action: (
        {"written": "main.py"} if action.get("tool") == "write"
        else {"content": "source"}))
    def _go():
        return engine.run_step(task_id=1, step_id="t_impl", workspace=ws,
                               project_id="default",
                               agent_config_name="task_implementer",
                               tool_schemas=TS)
    if expect_death:
        with pytest.raises(MaxRetriesExceeded, match="output cap"):
            _go()
    else:
        assert _go() is True
    return traces


def _setup(tmp, project_id="default", step_id="t_impl"):
    (tmp / project_id / f"{step_id}.tmp").mkdir(parents=True, exist_ok=True)
    (tmp / "projects" / project_id).mkdir(parents=True, exist_ok=True)
    (tmp / "projects" / project_id / "README.md").write_text("# existing\n")


# --------------------------------------------------------------------------
# the-output-budget-is-observed-at-all
# --------------------------------------------------------------------------

def test_todays_rule_is_blind_to_the_curve_that_killed_the_attempt():
    """NEGATIVE POLE for the whole card: turn-only never fires on this curve."""
    fired = [turn for turn, *_ in REFERENCE_CURVE
             if _should_intervene_early(turn=turn - 1, max_turns=REFERENCE_MAX_TURNS,
                                        writable=True, written_files=[],
                                        already_intervened=False)]
    assert fired == []


def test_output_budget_is_reported_next_to_the_turn_count(engine):
    tmp = Path(tempfile.mkdtemp()); _setup(tmp)
    traces = _replay(engine, _WS(tmp))
    exhausted = [p for _c, e, p in traces if e == "output_cap_exhausted"]
    assert len(exhausted) == 1
    budget = exhausted[0]["output_budget"]
    assert budget["completion_tokens"] == CURVE_COMPLETION == 238112
    assert budget["reasoning_tokens"] == CURVE_REASONING == 227418
    assert budget["max_output_tokens"] == OUTPUT_CAP_CEILING == 65536
    assert budget["cap_at_start"] == REFERENCE_START_CAP
    assert budget["escalations_used"] == 1
    assert budget["escalations_remaining"] == 0
    assert budget["spent_fraction"] == 1.0
    assert budget["half_spent"] is True
    # ...and the turn count is right there beside it, not merged into it.
    assert exhausted[0]["turn_budget"] == {"turns_used": 20, "max_turns": 100}


def test_a_step_that_already_wrote_is_not_flagged(engine):
    """NEGATIVE POLE: identical output curve, but the step delivered on turn 1.

    The curve is the same up to turn 16, where the starve ends a step that has
    already written (nothing is left to nudge it into). The budget is still
    OBSERVED — what is withheld is the intervention.
    """
    tmp = Path(tempfile.mkdtemp()); _setup(tmp)
    traces = _replay(engine, _WS(tmp), write_on_first_turn=True, expect_death=False)
    assert [p for _c, e, p in traces if e == "early_progress_intervention"] == []
    budget = [p for _c, e, p in traces if e == "step_budget"]
    assert len(budget) == 1
    assert budget[0]["output_budget"]["half_spent"] is True
    assert budget[0]["output_budget"]["spent_fraction"] == 1.0   # turn 16 filled 32768
    assert budget[0]["written_files"] == ["main.py"]
    assert budget[0]["first_write_turn"] == 1
    assert budget[0]["early_progress_intervened"] is False
    assert budget[0]["turn_budget"]["max_turns"] == REFERENCE_MAX_TURNS


def test_every_step_reports_both_budgets_even_when_it_succeeds(engine):
    """`the-output-budget-is-observed-at-all` says EVERY step, not every death."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp)
    traces = _replay(engine, _WS(tmp), write_on_first_turn=True, expect_death=False)
    report = [p for _c, e, p in traces if e == "step_budget"][0]
    assert report["budget_exhausted"] is None
    assert set(report["turn_budget"]) == {"turns_used", "max_turns"}
    for field in ("completion_tokens", "reasoning_tokens", "max_output_tokens",
                  "escalations_remaining"):
        assert field in report["output_budget"], field


def test_state_never_raises_a_cap():
    """FORBIDDEN NON-FIX: the observer must not be able to buy more budget."""
    before = OUTPUT_CAP_CEILING
    state = _output_budget_state(
        completion_tokens=238112, reasoning_tokens=227418,
        peak_turn_completion=65536, peak_turn_cap=65536,
        max_output_tokens=65536, cap_at_start=32768, escalations_used=1)
    from core import ai_router
    assert ai_router.OUTPUT_CAP_CEILING == before == 65536
    assert state["max_output_tokens"] == 65536


# --------------------------------------------------------------------------
# an-unwatched-budget-triggers-the-same-intervention
# --------------------------------------------------------------------------

def test_intervention_fires_before_turn_twenty_on_the_reference_curve(engine):
    tmp = Path(tempfile.mkdtemp()); _setup(tmp)
    traces = _replay(engine, _WS(tmp))
    fired = [p for _c, e, p in traces if e == "early_progress_intervention"]
    assert len(fired) == 1, "the intervention must still be single-shot"
    assert fired[0]["trigger"] == "output_budget"
    # 1-based turn 16 — the first loop head that can see turn 15's 19251/32768.
    assert fired[0]["turn"] + 1 == 16
    assert fired[0]["turn"] + 1 < 20
    assert fired[0]["max_turns"] == REFERENCE_MAX_TURNS
    assert fired[0]["action"] == "demand_write_or_split_external"
    assert fired[0]["output_budget"]["spent_fraction"] == pytest.approx(
        19251 / 32768, abs=1e-4)


def test_either_half_budget_triggers_and_only_once():
    common = dict(max_turns=100, writable=True, written_files=[],
                  already_intervened=False)
    assert _should_intervene_early(turn=10, output_budget_spent=0.49, **common) is False
    assert _should_intervene_early(turn=10, output_budget_spent=0.5, **common) is True
    assert _should_intervene_early(turn=50, output_budget_spent=0.0, **common) is True
    assert _should_intervene_early(
        turn=50, max_turns=100, writable=True, written_files=[],
        already_intervened=True, output_budget_spent=0.9) is False
    assert _should_intervene_early(
        turn=50, max_turns=100, writable=False, written_files=[],
        already_intervened=False, output_budget_spent=0.9) is False
    assert _should_intervene_early(
        turn=50, max_turns=100, writable=True, written_files=["a.py"],
        already_intervened=False, output_budget_spent=0.9) is False


def test_turn_side_behaviour_is_unchanged_without_an_output_signal():
    """The green light card `execution.coding-impl-progress-budget` bought must
    still hold: with no output signal the rule is byte-for-byte the old one."""
    for max_turns in (6, 32, 100):
        half = max(1, max_turns // 2)
        for turn in range(0, max_turns):
            assert _should_intervene_early(
                turn=turn, max_turns=max_turns, writable=True,
                written_files=[], already_intervened=False) is (turn >= half)


# --------------------------------------------------------------------------
# the-report-says-which-budget-ran-out
# --------------------------------------------------------------------------

def test_the_report_names_output_and_shows_the_turns_left_unspent(engine):
    tmp = Path(tempfile.mkdtemp()); _setup(tmp)
    traces = _replay(engine, _WS(tmp))
    report = [p for _c, e, p in traces if e == "output_cap_exhausted"][0]
    assert report["budget_exhausted"] == "output"
    assert report["turn_budget"]["turns_used"] == 20
    assert report["turn_budget"]["max_turns"] == 100
    assert report["output_budget"]["spent_fraction"] == 1.0
    assert report["first_write_turn"] is None
    assert report["written_files"] == []
    # Never merged: both budgets survive as separate, readable quantities.
    assert set(report["turn_budget"]) == {"turns_used", "max_turns"}
    assert "turns_used" not in report["output_budget"]
    assert [_c for _c, e, _p in traces if e == "turn_budget_exhausted"] == []
