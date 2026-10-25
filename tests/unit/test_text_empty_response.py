"""Blank JSON/text completions are endpoint failures, through the real gateway."""
import json

import litellm
import pytest

from core import ai_router
from core.ai_router import endpoint_cooldowns
from tests.unit.test_provider_refusal import wiring, gw


@pytest.fixture(autouse=True)
def plain(monkeypatch):
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")


def response(content=None, *, reasoning="", finish="stop", completion=0):
    return litellm.ModelResponse(model="owned-offline", choices=[{
        "index": 0, "finish_reason": finish, "message": {
            "role": "assistant", "content": content, "reasoning_content": reasoning}}],
        usage={"prompt_tokens": 19, "completion_tokens": completion,
               "total_tokens": 19 + completion})


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("payload", [
    {}, {"content": ""}, {"content": " \t\n"},
    {"reasoning": "still thinking", "completion": 7},
    {"finish": "length", "completion": 7}, {"completion": 7},
])
def test_blank_text_fails_over_before_success(wiring, monkeypatch, json_mode, payload):
    g = gw(wiring, "pool")
    calls, events = [], []
    g.on_completion = lambda event, fields: events.append((event, fields))
    def complete(**kwargs):
        calls.append(kwargs.copy())
        return response(**payload) if len(calls) == 1 else response('{"ok":true}', completion=2)
    monkeypatch.setattr(litellm, "completion", complete)
    result = g.generate("system", "question", is_json_mode=json_mode)
    # Captured stdout is first-failure evidence when the baseline returns blank.
    print(json.dumps({"result": result, "models": [c["model"] for c in calls],
                      "events": events}, default=str))
    assert result == '{"ok":true}'
    assert [c["model"] for c in calls] == [
        "anthropic/claude-sonnet-5-5", "deepseek/deepseek-v4-flash"]
    assert all(c.get("response_format") == ({"type": "json_object"} if json_mode else None)
               for c in calls)
    finished = [f for e, f in events if e == "completion_finished"]
    assert [f["outcome"] for f in finished] == ["error", "completed"]
    assert finished[0]["error_type"] == "EmptyProviderResponse"
    assert [f["served_by"] for f in finished] == [c["model"] for c in calls]
    assert g.last_usage["served_by"] == "deepseek/deepseek-v4-flash"
    assert g.last_usage["completion_tokens"] == 2
    assert endpoint_cooldowns() == {}
    assert g.generate("system", "question", is_json_mode=json_mode) == '{"ok":true}'
    assert len(calls) == 3 and calls[-1]["model"] == "deepseek/deepseek-v4-flash"


@pytest.mark.parametrize("stream", [False, True])
def test_exhausted_empty_text_is_named_bounded_failure(wiring, monkeypatch, stream):
    g = gw(wiring, "pool")
    monkeypatch.setenv("AITELIER_LLM_STREAM", "1" if stream else "0")
    calls, events = [], []
    g.on_completion = lambda event, fields: events.append((event, fields))
    def complete(**kwargs):
        calls.append(kwargs["model"])
        if not stream:
            return response(reasoning="still thinking", completion=7)
        return iter([litellm.ModelResponse(stream=True, model="owned-offline", choices=[{
            "index": 0, "delta": {"role": "assistant", "content": "", "reasoning_content": "still thinking"},
            "finish_reason": "stop"}], usage={"prompt_tokens": 19,
            "completion_tokens": 7, "total_tokens": 26})])
    monkeypatch.setattr(litellm, "completion", complete)
    with pytest.raises(ai_router.EmptyProviderResponse, match="empty text response") as caught:
        g.generate("system", "question", is_json_mode=True)
    assert calls == ["anthropic/claude-sonnet-5-5", "deepseek/deepseek-v4-flash"]
    assert caught.value._aitelier_candidates == g._candidates
    assert ai_router.is_provider_failure(caught.value)
    assert not ai_router._retry_llm_error(caught.value)
    assert [f["outcome"] for e, f in events if e == "completion_finished"] == ["error", "error"]
    assert endpoint_cooldowns() == {}


@pytest.mark.parametrize("content,json_mode", [(' {"ok": true} \n', True), ("plain answer", False)])
@pytest.mark.parametrize("stream", [False, True])
def test_valid_text_is_preserved(wiring, monkeypatch, content, json_mode, stream):
    g = gw(wiring, "pool")
    monkeypatch.setenv("AITELIER_LLM_STREAM", "1" if stream else "0")
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        if not stream:
            return response(content, completion=2)
        return iter([litellm.ModelResponse(stream=True, model="owned-offline", choices=[{
            "index": 0, "delta": {"role": "assistant", "content": content},
            "finish_reason": "stop"}], usage={"prompt_tokens": 19,
            "completion_tokens": 2, "total_tokens": 21})])
    monkeypatch.setattr(litellm, "completion", complete)
    assert g.generate("system", "question", is_json_mode=json_mode) == content.strip()
    assert len(calls) == 1
    assert g.last_usage["served_by"] == "anthropic/claude-sonnet-5-5"


def test_text_refusal_keeps_refusal_classification(wiring, monkeypatch):
    g = gw(wiring, "pool")
    events = []
    g.on_completion = lambda event, fields: events.append((event, fields))
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: response(finish="content_filter"))
    with pytest.raises(ai_router.ProviderRefusal):
        g.generate("system", "question", is_json_mode=True)
    assert [f["error_type"] for e, f in events if e == "completion_finished"] == [
        "ProviderRefusal", "ProviderRefusal"]
