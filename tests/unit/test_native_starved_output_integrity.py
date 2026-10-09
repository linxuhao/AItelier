"""Integrity fence for the native reasoning-starvation recovery cycle."""
import copy
import json
from unittest.mock import MagicMock

import pytest

from core import agents
from core.ai_router import OUTPUT_CAP_CEILING
from core.dpe_pipeline import NativeOutputCapExhausted
from tests.integration.test_native_parity import engine, budget_case, _run, _turn, _tc


def script_case(case, responses, *, cap=OUTPUT_CAP_CEILING, budget=6):
    eng, ws, draft, traces = case
    eng.factory.get_max_tool_turns.return_value = budget
    nat = eng.factory.get_native_agent.return_value
    nat.gateway.max_output_tokens = cap
    nat.gateway.last_outbound = {}
    nat.gateway.escalate_output_cap = MagicMock()
    calls, emitted = [], []
    stream = iter(responses)
    eng._emit = lambda event, payload=None: emitted.append((event, copy.deepcopy(payload or {})))

    def turn(**kwargs):
        calls.append({"cap": nat.gateway.max_output_tokens, **copy.deepcopy(kwargs)})
        item = next(stream)
        if isinstance(item, BaseException):
            raise item
        nat.gateway.last_usage = {
            "completion_tokens": cap if item.truncated else 7,
            "reasoning_tokens": cap if item.truncated else 0,
        }
        return item

    nat.turn.side_effect = turn
    return nat, calls, emitted


def assert_retained_incomplete(case, nat, emitted, *, partial, calls):
    eng, ws, draft, traces = case
    assert draft.exists() is partial
    if partial:
        assert draft.read_text() == "partial draft; not completed\n"
    assert not (ws.get_code_path("default") / "partial.gd").exists()
    assert not [ev for ev, _ in emitted if ev in ("files_written", "step_done", "native_fallback")]
    eng.factory.get_agent.assert_not_called()
    nat.gateway.escalate_output_cap.assert_not_called()
    assert not [ev for ev, _ in emitted if ev in ("output_cap_escalated", "turn_granted_for_escalation")]
    failed = [p for _cat, ev, p in traces if ev == "output_cap_exhausted"]
    assert len(failed) == 1
    assert failed[0]["turn_budget"]["turns_used"] == calls
    assert failed[0]["written_files"] == (["partial.gd"] if partial else [])
    assert failed[0]["output_budget"]["escalations_used"] == 0
    return failed[0]


@pytest.mark.parametrize("partial,cap", [(False, 8192), (True, 8192), (True, OUTPUT_CAP_CEILING)])
def test_recovery_transport_error_retains_partial_work_and_accounting(budget_case, monkeypatch, partial, cap):
    remember = MagicMock()
    monkeypatch.setattr(agents, "remember_output_cap", remember)
    responses = ([_turn(tool_calls=[_tc("edit")])] if partial else []) + [
        _turn(reasoning="starved", truncated=True), RuntimeError("correction transport failed")]
    nat, calls, emitted = script_case(budget_case, responses, cap=cap, budget=len(responses))
    with pytest.raises(NativeOutputCapExhausted):
        _run(budget_case[0], budget_case[1])
    failed = assert_retained_incomplete(budget_case, nat, emitted, partial=partial, calls=len(responses))
    assert failed["correction_error"] == "RuntimeError: correction transport failed"
    assert failed["output_budget"]["completion_tokens"] == cap + 7 * int(partial)
    assert failed["output_budget"]["reasoning_tokens"] == cap
    assert calls[-1]["tool_choice"] == "auto"
    assert nat.gateway.max_output_tokens == cap
    remember.assert_not_called()


@pytest.mark.parametrize("correction", [
    _turn(), _turn(text="I am done"), _turn(text="partial text", truncated=True),
    _turn(tool_calls=[_tc("invented_tool")]),
    _turn(tool_calls=[{"id": "bad", "function": {"name": "finish_step", "arguments": "{"}}]),
    _turn(tool_calls=[{"id": "bad", "function": {"name": "edit", "arguments": "[]"}}]),
    _turn(tool_calls=[{"function": {"name": "edit", "arguments": "{}"}}]),
])
def test_absent_or_invalid_correction_cannot_complete_or_execute_effects(budget_case, correction):
    eng, ws, draft, traces = budget_case
    nat, calls, emitted = script_case(budget_case, [
        _turn(tool_calls=[_tc("edit")]), _turn(truncated=True), correction], budget=3)
    with pytest.raises(NativeOutputCapExhausted):
        _run(eng, ws)
    failed = assert_retained_incomplete(budget_case, nat, emitted, partial=True, calls=3)
    assert "correction_error" in failed
    assert eng._exec_tool.call_count == 1
    assert not [ev for ev, _ in emitted if ev == "agent_message"]
    invalid = [p for _cat, ev, p in traces if ev == "output_starvation_invalid_correction"]
    assert len(invalid) == 1 and invalid[0]["tool_calls"] == correction.tool_calls


def test_failed_corrective_edit_cannot_be_masked_by_finish_in_the_same_batch(budget_case):
    eng, ws, draft, traces = budget_case
    execute = eng._exec_tool.side_effect
    def fail_edit(action):
        if action["params"].get("revision") == "bad":
            return {"error": "edit refused; old text not found"}
        return execute(action)
    eng._exec_tool.side_effect = fail_edit
    nat, calls, emitted = script_case(budget_case, [
        _turn(tool_calls=[_tc("edit")]), _turn(truncated=True),
        _turn(tool_calls=[_tc("edit", {"revision": "bad"}), _tc("finish_step", cid="finish")])], budget=3)
    with pytest.raises(NativeOutputCapExhausted):
        _run(eng, ws)
    failed = assert_retained_incomplete(budget_case, nat, emitted, partial=True, calls=3)
    assert failed["tool_failures"] == 1
    assert failed["first_write_turn"] == 1
    assert "batch did not succeed" in failed["correction_error"]


@pytest.mark.parametrize("later", [RuntimeError("later recovery transport failed"), _turn(text="done without finish")])
def test_progress_read_does_not_drop_the_recovery_integrity_fence(budget_case, later):
    eng, ws, draft, traces = budget_case
    nat, calls, emitted = script_case(budget_case, [
        _turn(tool_calls=[_tc("edit")]), _turn(truncated=True),
        _turn(tool_calls=[_tc("read_file")]), later], budget=4)
    with pytest.raises(NativeOutputCapExhausted):
        _run(eng, ws)
    failed = assert_retained_incomplete(budget_case, nat, emitted, partial=True, calls=4)
    assert failed["reads_searches"] == 1
    assert calls[-1]["tool_choice"] == "auto"


def test_multi_turn_productive_recovery_finishes_within_original_budget(budget_case):
    eng, ws, draft, traces = budget_case
    nat, calls, emitted = script_case(budget_case, [
        _turn(tool_calls=[_tc("edit")]), _turn(truncated=True),
        _turn(tool_calls=[_tc("read_file")]),
        _turn(tool_calls=[_tc("edit", {"revision": 2})]),
        _turn(tool_calls=[_tc("finish_step")])], budget=5)
    assert _run(eng, ws) is True
    assert len(calls) == 5
    assert calls[-1]["tool_choice"] == "auto"
    assert draft.exists()
    assert any(ev == "files_written" for ev, _ in emitted)
    assert not [ev for _cat, ev, _p in traces if ev == "output_cap_exhausted"]


def test_quota_after_productive_read_in_recovery_remains_visible(budget_case):
    eng, ws, draft, traces = budget_case
    quota = Exception("You have exceeded the 5-hour usage quota. It will reset at 2026-10-05.")
    nat, calls, emitted = script_case(budget_case, [
        _turn(tool_calls=[_tc("edit")]), _turn(truncated=True),
        _turn(tool_calls=[_tc("read_file")]), quota], budget=4)
    with pytest.raises(Exception) as caught:
        _run(eng, ws)
    assert caught.value is quota
    assert draft.exists()
    eng.factory.get_agent.assert_not_called()
    assert not [ev for ev, _ in emitted if ev in ("files_written", "step_done", "native_fallback")]


def test_ordinary_no_tool_completion_without_starvation_is_unchanged(budget_case):
    eng, ws, draft, traces = budget_case
    nat, calls, emitted = script_case(budget_case, [
        _turn(tool_calls=[_tc("edit")]), _turn(text="ordinary completion")], budget=2)
    assert _run(eng, ws) is True
    assert draft.exists()

@pytest.mark.parametrize("partial,cap", [(False, 8192), (True, 8192), (True, OUTPUT_CAP_CEILING)])
@pytest.mark.parametrize("bad_id", ["duplicate", 7, True, ["call"], {"call": 1}])
def test_correction_batch_identity_refuses_before_any_tool_effect(budget_case, monkeypatch, partial, cap, bad_id):
    eng, ws, draft, traces = budget_case
    remember = MagicMock()
    monkeypatch.setattr(agents, "remember_output_cap", remember)
    responses = ([_turn(tool_calls=[_tc("edit")])] if partial else []) + [
        _turn(truncated=True),
        _turn(tool_calls=[_tc("read_file", cid=bad_id), _tc("finish_step", cid=bad_id)])]
    nat, calls, emitted = script_case(budget_case, responses, cap=cap, budget=len(responses))
    with pytest.raises(NativeOutputCapExhausted):
        _run(eng, ws)
    failed = assert_retained_incomplete(budget_case, nat, emitted, partial=partial, calls=len(responses))
    assert eng._exec_tool.call_count == int(partial)
    assert "tool call ids" in failed["correction_error"]
    assert nat.gateway.max_output_tokens == cap
    remember.assert_not_called()


@pytest.mark.parametrize("partial", [False, True])
def test_unique_batch_ids_and_cross_turn_reuse_still_finish(budget_case, partial):
    eng, ws, draft, traces = budget_case
    # c1 belongs to a later assistant batch, so a previous edit's c1 may be reused.
    responses = ([_turn(tool_calls=[_tc("edit", cid="c1")])] if partial else []) + [
        _turn(truncated=True),
        _turn(tool_calls=[_tc("read_file", cid="c1"), _tc("finish_step", cid="c2")])]
    nat, calls, emitted = script_case(budget_case, responses, cap=8192, budget=len(responses))
    assert _run(eng, ws) is True
    assert len(calls) == len(responses)
    assert eng._exec_tool.call_count == int(partial) + 1
    assert nat.gateway.max_output_tokens == 8192
    assert not [p for _, ev, p in traces if ev == "output_starvation_invalid_correction"]
