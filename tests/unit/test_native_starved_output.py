"""Native reasoning starvation consumes turns without buying capacity."""
import copy
import json
from unittest.mock import MagicMock

import pytest

from core import agents
from core.ai_router import AIGateway
from core.dpe_pipeline import NativeOutputCapExhausted
from tests.integration.test_native_parity import (
    engine, budget_case, _run, _turn, _tc,
)


def wire(case, responses, budget):
    eng, ws, draft, traces = case
    eng.factory.get_max_tool_turns.return_value = budget
    nat = eng.factory.get_native_agent.return_value
    nat.gateway.max_output_tokens = 8192
    nat.gateway.last_outbound = {}
    nat.gateway.escalate_output_cap = MagicMock(
        side_effect=lambda: AIGateway.escalate_output_cap(nat.gateway))
    calls, emitted = [], []
    eng._emit = lambda event, payload=None: emitted.append((event, payload or {}))
    stream = iter(responses)

    def turn(**kwargs):
        result = next(stream)
        calls.append({"cap": nat.gateway.max_output_tokens, **copy.deepcopy(kwargs)})
        nat.gateway.last_usage = {
            "completion_tokens": 8192 if result.truncated else 7,
            "reasoning_tokens": 8192 if result.truncated else 0,
        }
        return result

    nat.turn.side_effect = turn
    return nat, calls, emitted


def assert_no_reward(nat, emitted, remember):
    assert nat.gateway.max_output_tokens == 8192
    nat.gateway.escalate_output_cap.assert_not_called()
    remember.assert_not_called()
    assert not [ev for ev, _ in emitted if ev in (
        "output_cap_escalated", "turn_granted_for_escalation", "output_cap_ceiling")]


@pytest.mark.parametrize("partial", [False, True])
def test_starved_turn_gets_productive_correction_with_same_cap(budget_case, monkeypatch, partial):
    eng, ws, draft, traces = budget_case
    remember = MagicMock()
    monkeypatch.setattr(agents, "remember_output_cap", remember)
    dead = "DO-NOT-REPLAY-TRUNCATED-REASONING"
    responses = ([_turn(tool_calls=[_tc("edit")])] if partial else []) + [
        _turn(reasoning=dead, truncated=True),
        _turn(tool_calls=[_tc("edit", {"revision": 2}), _tc("finish_step", cid="finish")]),
    ]
    nat, calls, emitted = wire(budget_case, responses, len(responses))
    assert _run(eng, ws) is True
    assert nat.turn.call_count == len(responses)
    assert draft.read_text() == "partial draft; not completed\n"
    correction = calls[-1]
    assert correction["tool_choice"] == "auto"
    assert any("output cap and turn budget are unchanged" in (m.get("content") or "")
               for m in correction["messages"])
    assert any("VERY NEXT action must emit a concrete tool call" in (m.get("content") or "")
               for m in correction["messages"])
    assert dead not in json.dumps(correction["messages"])
    assert any(ev == "output_cap_starved" and p["reasoning_chars"] == len(dead)
               for _cat, ev, p in traces)
    assert_no_reward(nat, emitted, remember)


@pytest.mark.parametrize("partial,budget", [(False, 1), (False, 3), (True, 3)])
def test_repeated_starvation_is_incomplete_and_retains_accounting(budget_case, monkeypatch, partial, budget):
    eng, ws, draft, traces = budget_case
    remember = MagicMock()
    monkeypatch.setattr(agents, "remember_output_cap", remember)
    responses = ([_turn(tool_calls=[_tc("edit")])] if partial else []) + [
        _turn(reasoning="starved", truncated=True) for _ in range(budget - int(partial))]
    nat, calls, emitted = wire(budget_case, responses, budget)
    with pytest.raises(NativeOutputCapExhausted, match="explicit attention required"):
        _run(eng, ws)
    assert nat.turn.call_count == budget
    assert draft.exists() is partial
    if partial:
        assert draft.read_text() == "partial draft; not completed\n"
    assert not (ws.get_code_path("default") / "partial.gd").exists()
    eng.factory.get_agent.assert_not_called()
    assert not [ev for ev, _ in emitted if ev in ("step_done", "files_written")]
    failures = [p for _cat, ev, p in traces if ev == "output_cap_exhausted"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["turn_budget"]["max_turns"] == budget
    assert failure["turn_budget"]["turns_used"] == budget
    assert failure["budget_exhausted"] == "output"
    assert failure["written_files"] == (["partial.gd"] if partial else [])
    assert failure["output_budget"]["completion_tokens"] == (
        (budget - int(partial)) * 8192 + 7 * int(partial))
    assert failure["output_budget"]["reasoning_tokens"] == (budget - int(partial)) * 8192
    assert failure["output_budget"]["escalations_used"] == 0
    assert failure["tool_failures"] == 0
    assert_no_reward(nat, emitted, remember)


@pytest.mark.parametrize("truncated,text", [(False, ""), (True, "visible partial answer")])
def test_other_no_tool_responses_keep_existing_behavior(budget_case, truncated, text):
    eng, ws, draft, traces = budget_case
    nat, calls, emitted = wire(budget_case, [
        _turn(tool_calls=[_tc("edit")]), _turn(text=text, truncated=truncated)], 2)
    assert _run(eng, ws) is True
    assert draft.exists()
    assert nat.turn.call_count == 2
    assert not [ev for _cat, ev, _p in traces if ev == "output_cap_starved"]


def test_valid_truncated_tool_output_is_executed(budget_case):
    eng, ws, draft, traces = budget_case
    nat, calls, emitted = wire(budget_case, [
        _turn(tool_calls=[_tc("edit")], truncated=True),
        _turn(tool_calls=[_tc("finish_step")])], 2)
    assert _run(eng, ws) is True
    assert draft.exists()
    assert not [ev for _cat, ev, _p in traces if ev == "output_cap_starved"]


def test_quota_after_starvation_propagates_without_json_replay(budget_case):
    eng, ws, draft, traces = budget_case
    nat, calls, emitted = wire(budget_case, [_turn(truncated=True)], 3)
    original = nat.turn.side_effect
    quota = Exception("You have exceeded the 5-hour usage quota. It will reset at 2026-10-05.")
    def turn(**kwargs):
        if calls:
            raise quota
        return original(**kwargs)
    nat.turn.side_effect = turn
    with pytest.raises(Exception) as error:
        _run(eng, ws)
    assert error.value is quota
    eng.factory.get_agent.assert_not_called()
    assert nat.turn.call_count == 2
