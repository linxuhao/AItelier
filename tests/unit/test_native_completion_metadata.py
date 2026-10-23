"""Native completion metadata survives gateway conversion into the run trace.

Real `AIGateway.generate_native` conversion (only the provider call is
scripted) feeding the real native loop and its real trace callback. Scripted
responses are NOT evidence about any historical provider incident; they only
prove that what each boundary delivered is what gets recorded, and that the
trace names which boundary that was:

* structured SDK `function.arguments` -> SDK provenance;
* tool calls the host parsed out of leaked DSML content -> host-parser
  provenance (those strings are `json.dumps` output, not SDK arguments);
* a batch replayed from retained history -> original source UNKNOWN.

No case here claims original wire/stream bytes.
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
from tests.unit.test_native_direct_code import (
    host as _host, response as _native_response, execute as _execute,
)
from tests.unit.test_native_effect_recovery import (
    HostCrash, owned_tools, crash_after_fence, traces as _run_traces,
)
from tests.unit.test_native_starved_output import wire

_UNSET = object()
PROVENANCE = "sdk_parsed_adapter_boundary_not_wire_bytes"
DSML_PROVENANCE = "host_parsed_from_dsml_content_not_sdk_arguments"
RETAINED_PROVENANCE = "retained_history_original_source_unknown"



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
    # Original salvage state and argument source are unknown, not False.
    assert meta["tool_calls_salvaged_from_content"] is None
    assert meta["arguments_provenance"] == RETAINED_PROVENANCE
    # A recovered turn never inherits a finish reason it did not observe.
    stale = SimpleNamespace(finish_reason="length", truncated=True,
                            tool_calls_salvaged=True)
    stale_meta = _native_completion_metadata(stale, recovered=True)
    assert stale_meta["finish_reason"] == "unknown"
    assert stale_meta["tool_calls_salvaged_from_content"] is None
    assert stale_meta["arguments_provenance"] == RETAINED_PROVENANCE


_DSML_CONTENT = (
    'Writing now.\n'
    '<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n'
    '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="edit">\n'
    '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="file" string="true">a.gd'
    '</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
    '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="revision" string="false">2'
    '</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n'
    '</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n'
    '</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>'
)


def test_dsml_salvage_through_generate_native_is_host_parsed_not_sdk(budget_case):
    """Real generate_native DSML salvage -> actual native runner trace.

    The provider returned NO structured tool_calls; the host parsed the leaked
    markup and re-serialised it with json.dumps. The trace must say so, and
    must carry the host-parser argument string unchanged, while still carrying
    the adapter's finish_reason."""
    eng, ws, draft, traces = budget_case
    _drive(budget_case, [
        _resp("stop", content=_DSML_CONTENT),
        _resp("tool_calls", [FINISH]),
    ], budget=4)
    assert _run(eng, ws) is True
    first = _responses(traces)[0]
    # Independent expectation, written out rather than recomputed.
    assert first["tool_calls"] == [
        {"name": "edit", "arguments": '{"file": "a.gd", "revision": 2}'}]
    assert first["text"] == "Writing now."
    assert first["finish_reason"] == "stop"
    assert first["finish_reason_known"] is True
    assert first["truncated"] is False
    assert first["tool_calls_salvaged_from_content"] is True
    assert first["completion_metadata_source"] == "adapter_choice_boundary"
    assert first["arguments_provenance"] == DSML_PROVENANCE
    assert first["arguments_provenance"] != PROVENANCE
    # Salvaged call really ran as the edit tool (it wrote the draft).
    assert draft.exists()
    # The structured finish turn after it is still labelled SDK.
    last = _responses(traces)[-1]
    assert last["tool_calls_salvaged_from_content"] is False
    assert last["arguments_provenance"] == PROVENANCE


def test_retained_batch_recovery_trace_marks_original_source_unknown(tmp_path, monkeypatch):
    """Real retained-batch recovery -> actual recovered_agent_response trace."""
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    before, ws = _host(sf, rid, claim, root,
                       lambda messages, **kw: _native_response(
                           'remove_owned', file='baseline.py'))
    crash_after_fence(before, sf, rid, claim, 'remove_owned')
    with pytest.raises(HostCrash):
        _execute(before, ws, rid, claim)
    provider = []

    def after(messages, **kwargs):
        provider.append(1)
        return _native_response('finish_step', summary='retained invocation recovered')

    resumed, ws2 = _host(sf, rid, claim, root, after)
    try:
        assert _execute(resumed, ws2, rid, claim)
        events = _run_traces(sf, rid)
        recovered = [p for name, p in events if name == 'recovered_agent_response']
        assert len(recovered) == 1
        row = recovered[0]
        # The retained assistant batch's argument string, unchanged.
        assert row['tool_calls'] == [
            {'name': 'remove_owned', 'arguments': '{"file": "baseline.py"}'}]
        assert row['finish_reason'] == 'unknown'
        assert row['finish_reason_known'] is False
        assert row['truncated'] is None
        assert row['tool_calls_salvaged_from_content'] is None
        assert row['completion_metadata_source'] == (
            'recovered_from_retained_history_no_provider_call')
        assert row['arguments_provenance'] == RETAINED_PROVENANCE
        # No provider call produced it; the only provider call came after.
        assert any(name == 'native_batch_recovery' and p['provider_called'] is False
                   for name, p in events)
        assert provider == [1]
        assert executions == [('remove', 'baseline=True')]
    finally:
        sf._conn.close()

