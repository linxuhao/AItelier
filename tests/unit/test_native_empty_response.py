"""Contentless native provider replies fail once per candidate, not as output repair."""
import json
import litellm
import pytest

from core import ai_router
from core.ai_router import AIGateway, endpoint_cooldowns
from tests.unit.test_provider_refusal import wiring, gw, MSGS
from tests.unit.test_exhausted_provider_errors import call_case
from tests.integration.test_native_parity import engine


@pytest.fixture(autouse=True)
def plain(monkeypatch):
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")


def response(content=None, *, reasoning="", finish="stop", tools=None,
             completion=0):
    return litellm.ModelResponse(
        model="owned-offline", choices=[{"finish_reason": finish, "index": 0,
            "message": {"role": "assistant", "content": content,
                        "tool_calls": tools, "reasoning_content": reasoning}}],
        usage={"prompt_tokens": 19, "completion_tokens": completion,
               "total_tokens": 19 + completion})


@pytest.mark.parametrize("tools", [None, [{"type": "function", "function": {
    "name": "read_file", "parameters": {"type": "object"}}}]])
def test_contentless_native_response_is_named_failure(wiring, monkeypatch, tools):
    g = gw(wiring, "deepseek/deepseek-v4-flash")
    calls = []
    blank = response()
    def complete(**kwargs):
        calls.append(kwargs["model"])
        return blank
    monkeypatch.setattr(litellm, "completion", complete)
    with pytest.raises(Exception, match="empty native response") as caught:
        turn = g.generate_native(MSGS, tools=tools)
        print(json.dumps({"accepted_response": blank.model_dump(),
                          "returned_turn": vars(turn), "scripted_calls": calls}, default=str))
    assert type(caught.value) is ai_router.EmptyProviderResponse
    assert calls == ["deepseek/deepseek-v4-flash"]
    assert ai_router.is_provider_failure(caught.value)
    assert endpoint_cooldowns() == {}


@pytest.mark.parametrize("content", [None, "", " \t\n"])
def test_blank_primary_fails_over_once_and_stays_bound(wiring, monkeypatch, content):
    g = gw(wiring, "pool")
    calls = []
    def complete(**kwargs):
        calls.append(kwargs["model"])
        return response(content) if len(calls) == 1 else response("visible answer", completion=2)
    monkeypatch.setattr(litellm, "completion", complete)
    assert g.generate_native(MSGS).text == "visible answer"
    assert g.generate_native(MSGS).text == "visible answer"
    assert calls == ["anthropic/claude-sonnet-5-5", "deepseek/deepseek-v4-flash",
                     "deepseek/deepseek-v4-flash"]
    assert len(g._failovers) == 1 and "EmptyProviderResponse" in g._failovers[0][1]
    assert endpoint_cooldowns() == {}
    assert g.last_usage["completion_tokens"] == 2


def test_exhausted_pool_does_not_restart_tenacity_or_park(wiring, monkeypatch):
    g = gw(wiring, "pool")
    calls = []
    def complete(**kwargs):
        calls.append(kwargs["model"])
        return response()
    monkeypatch.setattr(litellm, "completion", complete)
    with pytest.raises(ai_router.EmptyProviderResponse) as caught:
        g.generate_native(MSGS)
    assert calls == ["anthropic/claude-sonnet-5-5", "deepseek/deepseek-v4-flash"]
    assert caught.value._aitelier_candidates == g._candidates
    assert not ai_router._retry_llm_error(caught.value)
    assert endpoint_cooldowns() == {}


def test_absent_usage_does_not_make_blank_native_output_valid(wiring, monkeypatch):
    g = gw(wiring, "deepseek/deepseek-v4-flash")
    blank = response()
    blank.usage = None
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: blank)
    with pytest.raises(ai_router.EmptyProviderResponse):
        g.generate_native(MSGS)


def test_positive_reasoning_usage_without_visible_reasoning_is_preserved(wiring, monkeypatch):
    g = gw(wiring, "pool")
    reply = response()
    reply.usage = litellm.Usage(prompt_tokens=0, completion_tokens=0,
        completion_tokens_details={"reasoning_tokens": 5})
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: reply)
    turn = g.generate_native(MSGS)
    assert turn.text == "" and turn.tool_calls == []
    assert g.active_model == "anthropic/claude-sonnet-5-5"


@pytest.mark.parametrize("payload", [
    {"content": "answer"}, {"content": "No changes needed."},
    {"reasoning": "still thinking"},
    {"finish": "length"},
    {"completion": 7},
    {"tools": [{"id": "finish", "type": "function", "function": {
        "name": "finish_step", "arguments": "{}"}}]},
])
def test_meaningful_or_existing_bounded_shapes_are_preserved(wiring, monkeypatch, payload):
    g = gw(wiring, "pool")
    reply = response(**payload)
    calls = []
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: calls.append(kwargs["model"]) or reply)
    turn = g.generate_native(MSGS)
    assert turn.text == (payload.get("content") or "")
    assert turn.reasoning_content == payload.get("reasoning", "")
    assert turn.truncated is (payload.get("finish") == "length")
    assert bool(turn.tool_calls) is bool(payload.get("tools"))
    assert calls == ["anthropic/claude-sonnet-5-5"]


def test_blank_generic_generate_keeps_json_mode(wiring, monkeypatch):
    g = gw(wiring, "pool")
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        return response("")
    monkeypatch.setattr(litellm, "completion", complete)
    assert g.generate("system", "question", is_json_mode=True) == ""
    assert len(calls) == 1 and calls[0]["response_format"] == {"type": "json_object"}


def test_real_stream_builder_blank_native_response_fails(wiring, monkeypatch):
    g = gw(wiring, "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("AITELIER_LLM_STREAM", "1")
    chunks = [litellm.ModelResponse(stream=True, model="owned-offline",
        choices=[{"index": 0, "delta": {"role": "assistant", "content": ""},
                  "finish_reason": "stop"}],
        usage={"prompt_tokens": 19, "completion_tokens": 0, "total_tokens": 19})]
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        return iter(chunks)
    monkeypatch.setattr(litellm, "completion", complete)
    with pytest.raises(ai_router.EmptyProviderResponse):
        g.generate_native(MSGS)
    assert len(calls) == 1 and calls[0]["stream"] is True


@pytest.mark.parametrize("fallback", [False, True])
def test_actual_native_loop_preserves_named_failure_no_output_or_json_retry(call_case, wiring, monkeypatch, fallback):
    from core.agents import DPEAgentNative
    c = call_case
    from core.prompt_assembler import PromptAssembler

    # Keep the fixture prompt while retaining the real preamble API. A warmed
    # config registry enables DPE preambles even in this controlled native call.
    assembler = PromptAssembler()
    assembler.assemble = c.engine.assembler.assemble
    c.engine.assembler = assembler
    c.engine.factory.get_fallback_to_json.return_value = fallback
    gateway = gw(wiring, "pool")
    c.engine.factory.get_native_agent.return_value = DPEAgentNative(gateway, "owned fixture")
    gateway_calls = []
    def blank(**kwargs):
        gateway_calls.append(kwargs["model"])
        c.calls.append("native")
        return response()
    monkeypatch.setattr(litellm, "completion", blank)
    with pytest.raises(ai_router.EmptyProviderResponse) as caught:
        c.run()
    rows, summary = c.evidence("empty-native-" + str(fallback), caught.value)
    assert gateway_calls == ["anthropic/claude-sonnet-5-5", "deepseek/deepseek-v4-flash"]
    assert c.calls == ["native"] * 2  # No JSON call or extra provider replay.
    assert not any(event in ("native_fallback", "step_retry", "step_done", "files_written")
                   for event, _ in c.engine.events)
    errors = [r for r in rows if r["event"] == "native_error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["error_type"] == "EmptyProviderResponse"
    assert "empty native response" in errors[0]["payload"]["original_error"]
    assert summary["count"] == 1 and "EmptyProviderResponse" in summary["entries"][0]["summary"]
