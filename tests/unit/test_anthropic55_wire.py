"""Offline production LiteLLM conversion, including a stale release catalog."""
import copy
import pytest
import litellm
from litellm.llms.anthropic.chat.transformation import AnthropicConfig
from core.ai_router import AIGateway
from tests.unit.test_anthropic_dialect import wiring, gw, MSGS

@pytest.mark.parametrize("model", ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"])
@pytest.mark.parametrize("enabled,effort", [(False,None),(True,None),(True,"high")])
def test_55_actual_wire_omits_sampling_and_preserves_effort(wiring, monkeypatch, model, enabled, effort):
    monkeypatch.setattr(litellm, "model_cost", copy.deepcopy(litellm.model_cost))
    # Reproduce the live exact-entry mismatch: adaptive mapped, effort dropped.
    litellm.register_model({"anthropic/"+model: {
        "litellm_provider":"anthropic", "supports_adaptive_thinking":True,
        "supports_output_config":False,
        **{"supports_"+e+"_reasoning_effort":False for e in ["low","minimal","medium","high","xhigh","max"]},
    }}, persist_across_reloads=False)
    g=gw(wiring,"anthropic/"+model,enable_thinking=enabled,thinking_effort=effort)
    k=g._build_kwargs(MSGS)
    assert "temperature" not in k
    assert "extra_body" not in k
    params={key:k[key] for key in ("thinking","reasoning_effort","temperature","max_tokens") if key in k}
    c=AnthropicConfig()
    p=c.map_openai_params(params,{},model,False)
    body=c.transform_request(model,MSGS,p,{"drop_params":True},{})
    assert "temperature" not in body and "extra_body" not in body
    if model == "claude-opus-5-5" and not enabled:
        # Official Opus 5.5 always thinks; LiteLLM omits disabled accordingly.
        assert "thinking" not in body
    else:
        assert body["thinking"]["type"] == ("adaptive" if enabled else "disabled")
    if effort:
        assert k["reasoning_effort"]==effort
        assert body["output_config"]["effort"]==effort


def test_legacy_anthropic_off_retains_sampling(wiring):
    k=gw(wiring,"anthropic/claude-haiku-4-5",temperature=0.7)._build_kwargs(MSGS)
    assert k["temperature"]==0.7


def test_off_failover_restores_non_anthropic_sampling(wiring):
    g=gw(wiring,"pool",temperature=0.7)
    k=g._build_kwargs(MSGS)
    assert "temperature" not in k
    g._bind("deepseek/deepseek-v4-flash")
    g._apply_binding(k)
    expected=gw(wiring,"deepseek/deepseek-v4-flash",temperature=0.7)._build_kwargs(MSGS)
    assert k==expected


@pytest.mark.parametrize("enabled", [False, True])
def test_native_malformed_400_is_not_retried_or_parked(wiring, monkeypatch, enabled):
    from core.ai_router import endpoint_cooldowns
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")
    seen=[]
    def reject(**kwargs):
        seen.append(kwargs)
        raise litellm.exceptions.BadRequestError(
            "messages.0.content: Field required", model="claude-haiku-5-5",llm_provider="anthropic")
    monkeypatch.setattr(litellm,"completion",reject)
    g=gw(wiring,"pool",enable_thinking=enabled)
    with pytest.raises(litellm.exceptions.BadRequestError):
        g.generate_native(MSGS)
    assert len(seen)==1
    assert endpoint_cooldowns()=={}
