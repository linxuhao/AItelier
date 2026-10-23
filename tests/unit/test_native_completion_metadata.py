"""Native completion metadata survives gateway conversion into the run trace.

Real `AIGateway.generate_native` conversion (only the provider call is
scripted) feeding the real native loop and its real trace callback. Scripted
responses are NOT evidence about any historical provider incident; they only
prove that what the adapter boundary delivered is what gets recorded.
Tool-call arguments are SDK-parsed strings at the adapter boundary, never
original wire bytes.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.ai_router import AIGateway
from core.dpe_pipeline import _native_completion_metadata
from tests.integration.test_native_parity import (  # noqa: F401  (fixtures)
    engine, budget_case, _run, _tc,
)
from tests.unit.test_native_starved_output import wire

_UNSET = object()
PROVENANCE = "sdk_parsed_adapter_boundary_not_wire_bytes"


def _resp(finish=_UNSET, calls=(), content="", reasoning=""):
    """Provider response; `finish=_UNSET` omits finish_reason entirely."""
    tool_calls = [
        SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args))
        for cid, name, args in calls
    ] or None
    msg = SimpleNamespace(content=content, tool_calls=tool_calls,
                          reasoning_content=reasoning)
    choice = SimpleNamespace(message=msg)
    if finish is not _UNSET:
        choice.finish_reason = finish
    return SimpleNamespace(choices=[choice], usage=None)


def _drive(case, responses, budget=4):
    """Route nat.turn through the real gateway conversion."""
    eng, ws, draft, traces = case
    nat, calls, emitted = wire(case, [], budget)
    gw = AIGateway("deepseek/deepseek-v4-flash")
    stream = iter(responses)

    def turn(**kwargs):
        calls.append(kwargs)
        with patch.object(gw, "_complete_prebuilt", return_value=next(stream)):
            return gw.generate_native(
                kwargs["messages"], tools=kwargs.get("tools"),
                tool_choice=kwargs.get("tool_choice", "auto"))

    nat.turn.side_effect = turn
    return nat, calls, emitted


def _responses(traces):
    return [p for _c, ev, p in traces if ev == "agent_response"]


FINISH = ("f", "finish_step", "{}")


def test_length_with_partial_malformed_arguments_is_recorded_unchanged(budget_case):
    eng, ws, draft, traces = budget_case
    partial = '{"file": "a.gd", "content": "extends Node\\nfunc _re'
    _drive(budget_case, [
        _resp("length", [("c1", "edit", partial)]),
        _resp("tool_calls", [FINISH]),
    ], budget=4)
    _run(eng, ws)
    first = _responses(traces)[0]
    assert first["finish_reason"] == "length"
    assert first["finish_reason_known"] is True
    assert first["truncated"] is True
    assert first["completion_metadata_source"] == "adapter_choice_boundary"
    assert first["arguments_provenance"] == PROVENANCE
    assert first["tool_calls"] == [{"name": "edit", "arguments": partial}]
    with pytest.raises(json.JSONDecodeError):
        json.loads(first["tool_calls"][0]["arguments"])


@pytest.mark.parametrize("finish", ["stop", "tool_calls"])
def test_empty_object_arguments_are_recorded_unchanged_and_not_truncated(budget_case, finish):
    eng, ws, draft, traces = budget_case
    _drive(budget_case, [
        _resp(finish, [("c1", "edit", "{}")]),
        _resp("tool_calls", [FINISH]),
    ], budget=4)
    _run(eng, ws)
    first = _responses(traces)[0]
    assert first["finish_reason"] == finish
    assert first["truncated"] is False
    assert first["tool_calls"] == [{"name": "edit", "arguments": "{}"}]


@pytest.mark.parametrize("missing", ["absent", "none", "empty"])
def test_missing_finish_metadata_is_explicitly_unknown(budget_case, missing):
    eng, ws, draft, traces = budget_case
    first_resp = _resp(_UNSET if missing == "absent" else (
        None if missing == "none" else ""), [("c1", "edit", "{}")])
    _drive(budget_case, [first_resp, _resp("tool_calls", [FINISH])], budget=4)
    _run(eng, ws)
    first = _responses(traces)[0]
    assert first["finish_reason"] == "unknown"
    assert first["finish_reason_known"] is False
    # Unknown is not guessed from the empty-object arguments or token counts.
    assert first["truncated"] is False
    assert first["tool_calls"] == [{"name": "edit", "arguments": "{}"}]


def test_validation_refusal_after_starved_turn_retains_metadata(budget_case):
    """A later malformed correction batch is refused (never executed) yet its
    trace row carries the adapter's finish reason and exact argument string."""
    eng, ws, draft, traces = budget_case
    bad = '{"file": "a.gd", "content": "unterminated'
    nat, calls, _emitted = _drive(budget_case, [
        _resp("length", content="", reasoning="thinking"),
        _resp("length", [("c1", "edit", bad)]),
        _resp("tool_calls", [("c2", "edit", json.dumps({"revision": 2})), FINISH]),
    ], budget=4)
    assert _run(eng, ws) is True
    refused = [p for _c, ev, p in traces if ev == "output_starvation_invalid_correction"]
    assert len(refused) == 1
    assert refused[0]["finish_reason"] == "length"
    assert refused[0]["truncated"] is True
    assert refused[0]["tool_calls"][0]["function"]["arguments"] == bad
    assert refused[0]["arguments_provenance"] == PROVENANCE
    # The refused batch was not executed: only the valid third turn's edit ran.
    edits = [c for c in eng._exec_tool.call_args_list
             if c.args and c.args[0].get("tool") == "edit"]
    assert len(edits) == 1
    # Fences unchanged: starvation is still recorded; turns bounded by budget.
    assert any(ev == "output_cap_starved" for _c, ev, _p in traces)
    assert nat.turn.call_count == 3
    starved_row = _responses(traces)[0]
    assert starved_row["finish_reason"] == "length"
    assert starved_row["truncated"] is True
    assert _responses(traces)[-1]["finish_reason"] == "tool_calls"


def test_useful_valid_tool_completion_control(budget_case):
    eng, ws, draft, traces = budget_case
    args = json.dumps({"revision": 1})
    _drive(budget_case, [
        _resp("tool_calls", [("c1", "edit", args)]),
        _resp("stop", [FINISH]),
    ], budget=4)
    assert _run(eng, ws) is True
    rows = _responses(traces)
    assert [r["finish_reason"] for r in rows] == ["tool_calls", "stop"]
    assert all(r["truncated"] is False for r in rows)
    assert rows[0]["tool_calls"] == [{"name": "edit", "arguments": args}]
    assert draft.exists()


def test_gateway_native_turn_carries_finish_reason_and_salvage_flag():
    gw = AIGateway("deepseek/deepseek-v4-flash")
    with patch.object(gw, "_complete_prebuilt",
                      return_value=_resp("length", content="partial")):
        turn = gw.generate_native([{"role": "user", "content": "x"}])
    assert turn.finish_reason == "length" and turn.truncated is True
    assert turn.tool_calls_salvaged is False
    with patch.object(gw, "_complete_prebuilt", return_value=_resp(content="hi")):
        turn = gw.generate_native([{"role": "user", "content": "x"}])
    assert turn.finish_reason is None and turn.truncated is False


def test_recovered_turn_reports_unknown_not_assumed():
    recovered = SimpleNamespace(finish_reason=None, truncated=False,
                                tool_calls_salvaged=False)
    meta = _native_completion_metadata(recovered, recovered=True)
    assert meta["finish_reason"] == "unknown"
    assert meta["finish_reason_known"] is False
    assert meta["truncated"] is None
    assert meta["completion_metadata_source"].startswith("recovered_from_retained")
    # A recovered turn never inherits a finish reason it did not observe.
    stale = SimpleNamespace(finish_reason="length", truncated=True,
                            tool_calls_salvaged=False)
    assert _native_completion_metadata(stale, recovered=True)["finish_reason"] == "unknown"
