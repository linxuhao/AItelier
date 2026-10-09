"""A provider refusal is not a finished step.

finish_reason content_filter with no content and no tool calls used to come
back as an empty NativeTurn, which the native loop reads as the agent being
done. Claims, each with its opposite pole: a bare refusal fails over without
parking; a content_filter answer that carries text or tool calls is returned
as before; with nothing left, the call raises instead of returning empty.
"""

import json

import litellm
import pytest

from core import model_routes
from core.ai_router import (AIGateway, ProviderRefusal, endpoint_cooldowns,
                            reset_endpoint_cooldowns)

PROVIDERS = {
    "anthropic": {"base_url": "https://api.anthropic.com", "api_key_env": "ANT_KEY"},
    "deepseek": {"base_url": "https://api.deepseek.com/", "api_key_env": "DS_KEY"},
}
MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _plain_transport(monkeypatch):
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")


@pytest.fixture
def wiring(tmp_path, monkeypatch):
    providers = tmp_path / "llm_providers.json"
    providers.write_text(json.dumps(PROVIDERS), encoding="utf-8")
    routes = tmp_path / "model_routes.json"
    routes.write_text(json.dumps({
        "pool": {"rotate": ["anthropic/claude-sonnet-5-5"],
                 "fallback": ["deepseek/deepseek-v4-flash"]},
    }), encoding="utf-8")
    monkeypatch.setenv("ANT_KEY", "ant-secret")
    monkeypatch.setenv("DS_KEY", "ds-secret")
    model_routes.reset_cache()
    reset_endpoint_cooldowns()
    yield str(providers), str(routes)
    model_routes.reset_cache()
    reset_endpoint_cooldowns()


def gw(wiring, model):
    providers, routes = wiring
    return AIGateway(model, config_path=providers, routes_path=routes)


def _resp(finish_reason, content=None, tool_calls=None):
    msg = type("M", (), {"content": content, "tool_calls": tool_calls,
                         "reasoning_content": ""})()
    choice = type("C", (), {"message": msg, "finish_reason": finish_reason})()
    r = type("R", (), {})()
    r.choices = [choice]
    r.usage = None
    return r


def test_a_bare_refusal_fails_over_without_parking(wiring, monkeypatch):
    g = gw(wiring, "pool")
    seen = []

    def fake(**kwargs):
        seen.append(kwargs["model"])
        if kwargs["model"].startswith("anthropic/"):
            return _resp("content_filter")
        return _resp("stop", content="done")

    monkeypatch.setattr(litellm, "completion", fake)
    turn = g.generate_native(MSGS)
    assert turn.text == "done"
    assert seen == ["anthropic/claude-sonnet-5-5", "deepseek/deepseek-v4-flash"]
    assert g.active_model == "deepseek/deepseek-v4-flash"
    assert endpoint_cooldowns() == {}
    assert [f for f, _ in g._failovers] == ["anthropic/claude-sonnet-5-5"]


def test_a_content_filter_answer_with_text_or_tool_calls_is_kept(wiring, monkeypatch):
    for kwargs in ({"content": "partial answer"},
                   {"tool_calls": [type("T", (), {"id": "c1", "function": type(
                       "F", (), {"name": "read", "arguments": "{}"})()})()]}):
        g = gw(wiring, "pool")
        monkeypatch.setattr(litellm, "completion",
                            lambda **k: _resp("content_filter", **kwargs))
        turn = g.generate_native(MSGS)
        assert g.active_model == "anthropic/claude-sonnet-5-5"
        assert turn.text == "partial answer" or turn.tool_calls


def test_a_refusal_with_nothing_left_raises_instead_of_returning_empty(wiring, monkeypatch):
    g = gw(wiring, "anthropic/claude-sonnet-5-5")
    monkeypatch.setattr(litellm, "completion", lambda **k: _resp("content_filter"))
    with pytest.raises(ProviderRefusal, match="declined to answer"):
        g.generate_native(MSGS)


def test_an_ordinary_empty_stop_is_not_a_refusal(wiring, monkeypatch):
    g = gw(wiring, "pool")
    monkeypatch.setattr(litellm, "completion", lambda **k: _resp("stop", content=""))
    turn = g.generate_native(MSGS)
    assert turn.text == "" and g.active_model == "anthropic/claude-sonnet-5-5"
