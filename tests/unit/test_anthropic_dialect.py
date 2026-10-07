"""Claude in a rotate pool: its own thinking dialect, and spent credit as an
endpoint condition.

Measured 2026-10-07 on anthropic/claude-haiku-5-5 (litellm 1.104): the
extra_body keys every other endpoint takes come back a 400 from Anthropic, and
spent credit is a 400 too — neither is failed over, so both would kill a step
that the rotation happened to put on Claude.
"""

import json

import litellm
import pytest

from core import model_routes
from core.ai_router import (AIGateway, endpoint_cooldowns,
                            reset_endpoint_cooldowns)

PROVIDERS = {
    "anthropic": {"base_url": "https://api.anthropic.com", "api_key_env": "ANT_KEY"},
    "deepseek": {"base_url": "https://api.deepseek.com/", "api_key_env": "DS_KEY"},
}
CREDIT_MSG = ('AnthropicException - {"type":"error","error":{"type":'
              '"invalid_request_error","message":"Your credit balance is too '
              'low to access the Anthropic API. Please go to Plans & Billing '
              'to upgrade or purchase credits."}}')


@pytest.fixture(autouse=True)
def _plain_transport(monkeypatch):
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")


@pytest.fixture
def wiring(tmp_path, monkeypatch):
    providers = tmp_path / "llm_providers.json"
    providers.write_text(json.dumps(PROVIDERS), encoding="utf-8")
    routes = tmp_path / "model_routes.json"
    routes.write_text(json.dumps({
        "pool": {"rotate": ["anthropic/claude-haiku-5-5"],
                 "fallback": ["deepseek/deepseek-v4-flash"]},
        "pool_effort": {"rotate": ["anthropic/claude-haiku-5-5"],
                        "fallback": ["deepseek/deepseek-v4-flash"],
                        "effort": {"anthropic/claude-haiku-5-5": "medium",
                                   "deepseek/deepseek-v4-flash": "low"}},
    }), encoding="utf-8")
    monkeypatch.setenv("ANT_KEY", "ant-secret")
    monkeypatch.setenv("DS_KEY", "ds-secret")
    model_routes.reset_cache()
    reset_endpoint_cooldowns()
    yield str(providers), str(routes)
    model_routes.reset_cache()
    reset_endpoint_cooldowns()


def gw(wiring, model, **kw):
    providers, routes = wiring
    return AIGateway(model, config_path=providers, routes_path=routes, **kw)


class _Resp:
    def __init__(self):
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": "ok", "tool_calls": None,
                                      "reasoning_content": ""})(),
            "finish_reason": "stop"})()]
        self.usage = None


MSGS = [{"role": "user", "content": "hi"}]


# ── the dialect ──────────────────────────────────────────────────────

def test_thinking_on_with_effort_is_a_top_level_reasoning_effort(wiring):
    k = gw(wiring, "pool", enable_thinking=True, thinking_effort="high")._build_kwargs(MSGS)
    assert k["reasoning_effort"] == "high"
    assert "extra_body" not in k and "thinking" not in k
    assert "temperature" not in k


def test_the_route_tables_effort_for_claude_wins(wiring):
    k = gw(wiring, "pool_effort", enable_thinking=True, thinking_effort="max")._build_kwargs(MSGS)
    assert k["reasoning_effort"] == "medium"


def test_thinking_on_without_effort_is_adaptive(wiring):
    k = gw(wiring, "pool", enable_thinking=True)._build_kwargs(MSGS)
    assert k["thinking"] == {"type": "adaptive"}
    assert "extra_body" not in k and "reasoning_effort" not in k


def test_thinking_off_is_a_top_level_disabled(wiring):
    k = gw(wiring, "pool")._build_kwargs(MSGS)
    assert k["thinking"] == {"type": "disabled"}
    assert "extra_body" not in k and "reasoning_effort" not in k


def test_failover_onto_deepseek_restores_its_own_dialect(wiring):
    g = gw(wiring, "pool_effort", enable_thinking=True)
    k = g._build_kwargs(MSGS)
    g._bind("deepseek/deepseek-v4-flash")
    g._apply_binding(k)
    assert "thinking" not in k and "reasoning_effort" not in k
    assert k["extra_body"] == {"thinking": {"type": "enabled"},
                               "reasoning_effort": "low"}


def test_deepseek_alone_is_unchanged(wiring):
    on = gw(wiring, "deepseek/deepseek-v4-flash", enable_thinking=True,
            thinking_effort="high")._build_kwargs(MSGS)
    off = gw(wiring, "deepseek/deepseek-v4-flash")._build_kwargs(MSGS)
    assert on["extra_body"] == {"thinking": {"type": "enabled"},
                                "reasoning_effort": "high"}
    assert off["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False},
                                 "thinking": {"type": "disabled"}}
    assert "thinking" not in on and "thinking" not in off


# ── spent credit ─────────────────────────────────────────────────────

def _credit():
    return litellm.exceptions.BadRequestError(
        CREDIT_MSG, model="claude-haiku-5-5", llm_provider="anthropic")


def test_spent_credit_fails_over_and_parks(wiring, monkeypatch):
    g = gw(wiring, "pool")
    seen = []

    def fake(**kwargs):
        seen.append(kwargs["model"])
        if kwargs["model"].startswith("anthropic/"):
            raise _credit()
        return _Resp()

    monkeypatch.setattr(litellm, "completion", fake)
    g.generate_native(MSGS)
    assert g.active_model == "deepseek/deepseek-v4-flash"
    assert "anthropic/claude-haiku-5-5" in endpoint_cooldowns()

    g2 = gw(wiring, "pool")
    assert g2.active_model == "deepseek/deepseek-v4-flash"
    assert g2._failovers == []


def test_spent_credit_is_parked_for_the_longest_hold(wiring, monkeypatch):
    import time
    from core import ai_router
    monkeypatch.setattr(litellm, "completion",
                        lambda **k: (_ for _ in ()).throw(_credit())
                        if k["model"].startswith("anthropic/") else _Resp())
    gw(wiring, "pool").generate_native(MSGS)
    left = endpoint_cooldowns()["anthropic/claude-haiku-5-5"] - time.time()
    assert left > ai_router._COOLDOWN_FALLBACK_S


def test_other_anthropic_bad_requests_still_raise_without_failover(wiring, monkeypatch):
    g = gw(wiring, "pool", enable_thinking=True)
    seen = []

    def fake(**kwargs):
        seen.append(kwargs["model"])
        raise litellm.exceptions.BadRequestError(
            "messages.0.content: Field required", model="claude-haiku-5-5",
            llm_provider="anthropic")

    monkeypatch.setattr(litellm, "completion", fake)
    with pytest.raises(Exception):
        g.generate_native(MSGS)
    assert seen == ["anthropic/claude-haiku-5-5"]
    assert endpoint_cooldowns() == {}


def test_spent_credit_with_nothing_left_still_raises(wiring, monkeypatch):
    g = gw(wiring, "anthropic/claude-haiku-5-5")
    monkeypatch.setattr(litellm, "completion",
                        lambda **k: (_ for _ in ()).throw(_credit()))
    with pytest.raises(Exception, match="credit balance"):
        g.generate_native(MSGS)
