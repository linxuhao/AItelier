# tests/test_ai_router.py
import pytest
from unittest.mock import patch, MagicMock
from core.ai_router import AIGateway
import litellm


@pytest.fixture(autouse=True)
def _plain_transport(monkeypatch):
    """These tests pin retry/parsing over mocked responses, not the transport:
    disable the streaming transport so mocked litellm.completion can return
    plain MagicMocks. Streaming has its own suite
    (test_llm_stream_transport.py)."""
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")


def test_aigateway_success():
    """验证正常情况下的内容提取"""
    gateway = AIGateway("deepseek/deepseek-v4-flash")
    
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "Success Payload"
    
    with patch('litellm.completion', return_value=mock_response) as mock_litellm:
        result = gateway.generate("sys", "user")
        assert result == "Success Payload"
        mock_litellm.assert_called_once()

def test_aigateway_retry_on_ratelimit():
    """验证遇到速率限制时的重试机制（模拟两次失败，第三次成功）"""
    gateway = AIGateway("deepseek/deepseek-v4-flash")
    
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "Recovered Content"
    
    # 模拟异常序列: RateLimit -> RateLimit -> Success
    with patch('litellm.completion') as mock_litellm:
        mock_litellm.side_effect = [
            litellm.exceptions.RateLimitError("Limit reached", model="zai", llm_provider="zai"),
            litellm.exceptions.RateLimitError("Limit reached", model="zai", llm_provider="zai"),
            mock_response
        ]
        
        # 为了缩短测试耗时，可以临时调小网关内 retry 的 wait 间隔，或直接运行
        result = gateway.generate("sys", "user")
        assert result == "Recovered Content"
        assert mock_litellm.call_count == 3

# ── DSML tool-call salvage (SF-A) ──────────────────────────────────────

def test_parse_dsml_tool_calls_real_sample():
    from core.ai_router import parse_dsml_tool_calls
    import json
    content = (
        '<｜｜DSML｜｜tool_calls>\n'
        '<｜｜DSML｜｜invoke name="web_fetch">\n'
        '<｜｜DSML｜｜parameter name="url" string="true">https://x.io/</｜｜DSML｜｜parameter>\n'
        '</｜｜DSML｜｜invoke>\n'
        '</｜｜DSML｜｜tool_calls>'
    )
    tcs = parse_dsml_tool_calls(content)
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "web_fetch"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"url": "https://x.io/"}


def test_parse_dsml_typed_param_and_clean_text():
    from core.ai_router import parse_dsml_tool_calls, strip_dsml_markup
    import json
    content = (
        'Writing now.\n'
        '<｜｜DSML｜｜invoke name="write_sota">\n'
        '<｜｜DSML｜｜parameter name="content" string="true"># H</｜｜DSML｜｜parameter>\n'
        '<｜｜DSML｜｜parameter name="overwrite" string="false">true</｜｜DSML｜｜parameter>\n'
        '</｜｜DSML｜｜invoke>'
    )
    tcs = parse_dsml_tool_calls(content)
    assert len(tcs) == 1
    args = json.loads(tcs[0]["function"]["arguments"])
    assert args["content"] == "# H"
    assert args["overwrite"] is True  # string="false" -> JSON-typed
    assert strip_dsml_markup(content) == "Writing now."


def test_parse_dsml_no_false_positive():
    from core.ai_router import parse_dsml_tool_calls, strip_dsml_markup
    assert parse_dsml_tool_calls("A normal answer with no markup.") == []
    assert strip_dsml_markup("A normal answer.") == "A normal answer."


def test_generate_native_salvages_dsml_from_content():
    from core.ai_router import AIGateway
    gateway = AIGateway("deepseek/deepseek-v4-flash")
    msg = MagicMock()
    msg.tool_calls = None  # provider did NOT return structured tool calls
    msg.content = (
        'Let me read it.\n'
        '<｜｜DSML｜｜invoke name="read_file">\n'
        '<｜｜DSML｜｜parameter name="path" string="true">a.md</｜｜DSML｜｜parameter>\n'
        '</｜｜DSML｜｜invoke>'
    )
    msg.reasoning_content = ""
    resp = MagicMock(); resp.choices = [MagicMock(message=msg)]
    with patch('litellm.completion', return_value=resp):
        turn = gateway.generate_native([{"role": "user", "content": "x"}], tools=[{"x": 1}])
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0]["function"]["name"] == "read_file"
    assert turn.text == "Let me read it."  # markup stripped from text


# ── Phase 0: prompt-cache usage telemetry ──────────────────────────────
from types import SimpleNamespace


def test_extract_usage_deepseek_hit_miss():
    """DeepSeek exposes explicit prompt_cache_hit/miss_tokens."""
    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=1000, completion_tokens=200,
        prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200,
    ))
    u = AIGateway._extract_usage(resp)
    assert u["prompt_tokens"] == 1000
    assert u["cache_hit_tokens"] == 800
    assert u["cache_miss_tokens"] == 200
    assert u["hit_ratio"] == 0.8


def test_extract_usage_openai_cached_tokens():
    """OpenAI-style nests cached count under prompt_tokens_details."""
    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=1000, completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=600),
    ))
    u = AIGateway._extract_usage(resp)
    assert u["cache_hit_tokens"] == 600
    assert u["cache_miss_tokens"] == 400
    assert u["hit_ratio"] == 0.6


def test_extract_usage_no_cache_fields_is_unknown_not_zero():
    """A silent provider is UNKNOWN, never "the whole prompt missed".

    Ollama Cloud returns usage with only prompt/completion/total tokens yet
    demonstrably prefix-caches (2026-09-01 warm/cold A/B). Recording that as a
    full miss is a false positive claim, so the counters must be None.
    """
    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=500, completion_tokens=20))
    u = AIGateway._extract_usage(resp)
    assert u["prompt_tokens"] == 500          # token counts are still real
    assert u["completion_tokens"] == 20
    assert u["cache_hit_tokens"] is None
    assert u["cache_miss_tokens"] is None
    assert u["hit_ratio"] is None


def test_extract_usage_reported_zero_hit_is_not_unknown():
    """A provider that DOES report cached_tokens=0 measured a real 0% miss.

    This is the whole point of the fix: "measured 0" and "never said" must not
    collapse onto the same record, or a genuine cold prompt becomes
    indistinguishable from an unmeasured one.
    """
    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=500, completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0)))
    u = AIGateway._extract_usage(resp)
    assert u["cache_hit_tokens"] == 0
    assert u["cache_miss_tokens"] == 500
    assert u["hit_ratio"] == 0.0


def test_extract_usage_missing_usage():
    """Response without usage → empty dict (no crash)."""
    assert AIGateway._extract_usage(SimpleNamespace()) == {}


def test_generate_sets_last_usage():
    """generate() records last_usage from the response."""
    gateway = AIGateway("deepseek/deepseek-v4-flash")
    resp = MagicMock()
    resp.choices[0].message.content = "ok"
    resp.usage = SimpleNamespace(
        prompt_tokens=100, completion_tokens=10,
        prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60)
    with patch('litellm.completion', return_value=resp):
        gateway.generate("sys", "user")
    assert gateway.last_usage["cache_hit_tokens"] == 40
    assert gateway.last_usage["hit_ratio"] == 0.4


# ── Phase 5: explicit-provider cache breakpoint ────────────────────────
def test_cache_control_points_anthropic():
    """Anthropic-family models get a system breakpoint and a last-message
    breakpoint, so a growing conversation is read back from the cache."""
    gw = AIGateway("anthropic/claude-sonnet-4-6")
    pts = gw._cache_control_points()
    assert pts == [{"location": "message", "role": "system"},
                   {"location": "message", "index": -1}]


def test_build_kwargs_carries_last_message_breakpoint_for_anthropic():
    gw = AIGateway("anthropic/claude-haiku-5-5")
    kwargs = gw._build_kwargs([{"role": "system", "content": "s"},
                               {"role": "user", "content": "hi"}])
    assert {"location": "message", "index": -1} in kwargs["cache_control_injection_points"]


def test_failover_off_anthropic_drops_both_breakpoints():
    gw = AIGateway("anthropic/claude-haiku-5-5")
    kwargs = gw._build_kwargs([{"role": "user", "content": "hi"}])
    assert "cache_control_injection_points" in kwargs
    gw._bind("deepseek/deepseek-v4-flash")
    gw._apply_binding(kwargs)
    assert "cache_control_injection_points" not in kwargs


def test_cache_control_points_deepseek_none():
    """Auto-cachers (DeepSeek/Minimax) must NOT get a cache_control field."""
    assert AIGateway("deepseek/deepseek-v4-flash")._cache_control_points() is None
    assert AIGateway("minimax/abab6.5")._cache_control_points() is None


def test_build_kwargs_omits_cache_control_for_deepseek():
    gw = AIGateway("deepseek/deepseek-v4-flash")
    kwargs = gw._build_kwargs([{"role": "user", "content": "hi"}])
    assert "cache_control_injection_points" not in kwargs


# ── Reasoning-starved turns: DeepSeek bills reasoning inside max_tokens ──
def _native_response(*, finish_reason, content="", tool_calls=None,
                     reasoning_content="", usage=None):
    msg = MagicMock()
    msg.tool_calls = tool_calls
    msg.content = content
    msg.reasoning_content = reasoning_content
    choice = MagicMock(message=msg)
    choice.finish_reason = finish_reason
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


def test_extract_usage_reports_reasoning_tokens():
    """reasoning_tokens is the split that shows reasoning ate the cap."""
    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=500, completion_tokens=4096,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=4096),
    ))
    u = AIGateway._extract_usage(resp)
    assert u["completion_tokens"] == 4096
    assert u["reasoning_tokens"] == 4096


def test_extract_usage_omits_reasoning_tokens_when_absent():
    """Providers without a reasoning split must not gain a bogus zero."""
    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=500, completion_tokens=40))
    assert "reasoning_tokens" not in AIGateway._extract_usage(resp)


def test_generate_native_flags_output_cap_truncation():
    """finish_reason=length with no text and no tool call is the silent-review
    failure: the whole budget went to reasoning_content."""
    gw = AIGateway("deepseek/deepseek-v4-flash")
    resp = _native_response(finish_reason="length", content="",
                            reasoning_content="thinking " * 500,
                            usage=SimpleNamespace(
                                prompt_tokens=900, completion_tokens=4096,
                                completion_tokens_details=SimpleNamespace(
                                    reasoning_tokens=4096)))
    with patch('litellm.completion', return_value=resp):
        turn = gw.generate_native([{"role": "user", "content": "review"}],
                                  tools=[{"x": 1}])
    assert turn.truncated is True
    assert turn.text == "" and turn.tool_calls == []
    assert gw.last_usage["reasoning_tokens"] == 4096


def test_generate_native_not_truncated_on_normal_stop():
    gw = AIGateway("deepseek/deepseek-v4-flash")
    resp = _native_response(finish_reason="tool_calls", content="",
                            tool_calls=[MagicMock(
                                id="c1",
                                function=SimpleNamespace(
                                    name="create_verdict", arguments="{}"))])
    with patch('litellm.completion', return_value=resp):
        turn = gw.generate_native([{"role": "user", "content": "review"}],
                                  tools=[{"x": 1}])
    assert turn.truncated is False
    assert turn.tool_calls[0]["function"]["name"] == "create_verdict"


def test_deepseek_thinking_effort_rides_extra_body():
    """The reviewer fix is inert unless the level actually reaches DeepSeek."""
    gw = AIGateway("deepseek/deepseek-v4-flash", enable_thinking=True,
                   thinking_effort="low")
    kwargs = gw._build_kwargs([{"role": "user", "content": "hi"}])
    assert kwargs["extra_body"]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in kwargs


# ── Output-cap escalation (starved-turn recovery) ──────────────────────
#
# A turn truncated at max_tokens having emitted only reasoning did not choose
# to stay silent — DeepSeek bills reasoning inside the same cap as visible
# output, so it ran out of room before it could speak. Reissuing that call
# unchanged reproduces it exactly, so the cap is the setting that has to move.

def test_escalate_output_cap_doubles():
    gateway = AIGateway("deepseek/deepseek-v4-flash", max_output_tokens=8192)
    assert gateway.escalate_output_cap() == 16384
    assert gateway.max_output_tokens == 16384


def test_escalate_output_cap_reaches_ceiling_by_clamping_not_overshooting():
    """A cap that would overshoot lands exactly on the ceiling. Doubling into a
    max_tokens the provider rejects trades a starved turn for an API error."""
    from core.ai_router import OUTPUT_CAP_CEILING
    gateway = AIGateway("deepseek/deepseek-v4-flash",
                        max_output_tokens=OUTPUT_CAP_CEILING - 1000)
    assert gateway.escalate_output_cap() == OUTPUT_CAP_CEILING
    assert gateway.max_output_tokens == OUTPUT_CAP_CEILING


def test_escalate_output_cap_declines_at_ceiling_and_leaves_cap_alone():
    from core.ai_router import OUTPUT_CAP_CEILING
    gateway = AIGateway("deepseek/deepseek-v4-flash",
                        max_output_tokens=OUTPUT_CAP_CEILING)
    assert gateway.escalate_output_cap() is None
    assert gateway.max_output_tokens == OUTPUT_CAP_CEILING


def test_escalate_output_cap_declines_when_configured_above_ceiling():
    """A role configured above the ceiling must not be silently lowered — the
    escalation declines and leaves the operator's value intact."""
    from core.ai_router import OUTPUT_CAP_CEILING
    over = OUTPUT_CAP_CEILING * 2
    gateway = AIGateway("deepseek/deepseek-v4-flash", max_output_tokens=over)
    assert gateway.escalate_output_cap() is None
    assert gateway.max_output_tokens == over


def test_escalation_is_self_bounding():
    """The magnitude needs no counter of its own: escalating until it declines
    terminates, from the smallest cap in use, in a handful of steps."""
    from core.ai_router import OUTPUT_CAP_CEILING
    gateway = AIGateway("deepseek/deepseek-v4-flash", max_output_tokens=4096)
    steps = 0
    while gateway.escalate_output_cap() is not None:
        steps += 1
        assert steps < 100, "escalation failed to terminate"
    assert gateway.max_output_tokens == OUTPUT_CAP_CEILING
    assert steps == 4  # 4096 → 8192 → 16384 → 32768 → 65536


class TestEffortDelivery:
    """How the reasoning level reaches the wire — the one place two provider
    families disagree and litellm has an opinion of its own."""

    def _gw(self, model, effort="low"):
        from core.ai_router import AIGateway
        return AIGateway(model, enable_thinking=True, thinking_effort=effort)

    def test_effort_never_rides_as_a_top_level_param(self):
        """Top-level `reasoning_effort` is broken in BOTH directions.

        DeepSeek: litellm's DeepSeekChatConfig pops it and drops the LEVEL
        (BerriAI/litellm#27439), so every effort collapses to the default.
        Everyone else behind the openai/ shim: litellm VALIDATES the param
        against a model it has never heard of and refuses to send the request
        at all — measured 2026-08-26, every effort value on qwen/qwen3.8-max
        raised UnsupportedParamsError before anything left the process. That is
        a client-side error, so it is not in FAILOVER_EXCEPTIONS: it would have
        killed the step outright the first time a role with an effort set was
        routed onto a non-DeepSeek model.
        """
        for model in ("deepseek/deepseek-v4-flash", "qwen/qwen3.8-max",
                      "ark/glm-5.3", "localqwen/qwen3"):
            kwargs = self._gw(model)._build_kwargs([{"role": "user", "content": "x"}])
            assert "reasoning_effort" not in kwargs, (
                f"{model}: effort must go through extra_body, never top-level")
            assert kwargs["extra_body"]["reasoning_effort"] == "low", model

    def test_no_effort_means_no_key_at_all(self):
        """Unset must not become a value — the provider's own default applies."""
        kwargs = self._gw("qwen/qwen3.8-max", effort=None)._build_kwargs(
            [{"role": "user", "content": "x"}])
        assert "reasoning_effort" not in kwargs
        assert "reasoning_effort" not in kwargs["extra_body"]

    def test_no_role_sends_an_effort_qwen3_8_max_does_not_define(self):
        """`qwen3.8-max` has a different, NARROWER effort vocabulary.

        Its chat template documents low / medium / xhigh (xhigh default) and has
        no `max` at all; DeepSeek's is low / high / max, with medium and xhigh
        silently folded into high. The two overlap on `low` alone.

        Out-of-vocabulary values are not rejected — measured 2026-08-26, every
        value returns 200 — so a wrong one is SILENTLY IGNORED and the model
        falls back to its own default, which for qwen is its HIGHEST setting. An
        effort set to `low` to save money would land on maximum reasoning with
        no error to show for it: the same shape as the truncation that blanked 5
        of 8 reviewer turns.

        Worse, on a real reasoning task (a rigorous proof, 8192-token cap) the
        others move with the knob and qwen3.8-max does not:

            ark/glm-5.3      out 6995 (unset) / 3670 (low) / 8192 (max, capped)
            qwen/glm-5.2     reasoning 874 / 664 / 1389
            qwen/qwen3.8-max reasoning 144 / 327 / 132   <- no order at all

        n=1 per level, so treat the last row as a warning rather than a result —
        but do not assume the knob works there. This guard is deliberately
        narrow: it fires only for the one model whose vocabulary is documented
        to differ, which is exactly the trap that springs the first time a role
        moves onto the `smart` route.
        """
        from pathlib import Path

        import pytest
        import yaml

        root = Path(__file__).resolve().parents[2]
        # Derive route membership through ModelRoutes — the same loader the
        # router uses — rather than hand-parsing the file. A raw `endpoint in
        # candidates` check silently went empty the day the route switched to
        # the {"rotate": [...], "fallback": [...]} dict form (it tested dict
        # KEYS), so the shape is not ours to assume.
        #
        # The SHIPPED example, never config_or_example: that prefers the
        # operator's live table, which made this a statement about one
        # machine's routing. A unit test must say the same thing on every
        # checkout. (Whether a given DEPLOYMENT routes a max-effort role onto
        # qwen3.8-max is a real question, but it is a deployment lint, not
        # this.)
        from core.model_routes import ModelRoutes
        routes = ModelRoutes(root / "model_routes.example.json")
        narrow = {n for n in routes.names()
                  if "qwen/qwen3.8-max" in routes.resolve(n)}
        if not narrow:
            pytest.skip("no route in the resolved table reaches "
                        "qwen/qwen3.8-max — the trap cannot spring")
        ok = {None, "low", "medium"}

        offenders = []
        for f in sorted((root / "agent_configs").glob("*.yaml")):
            for role, rc in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).items():
                if not isinstance(rc, dict) or rc.get("model") not in narrow:
                    continue
                eff = (rc.get("thinking") or {}).get("effort")
                if eff in ok:
                    continue
                # The route may state the string FOR THIS ENDPOINT, in which
                # case the role's never reaches the wire and the trap cannot
                # spring. Narrowed, not disarmed: a route that declares nothing
                # still sends the role's value, which is the case this guard
                # was written for.
                if routes.effort_for(rc["model"], "qwen/qwen3.8-max"):
                    continue
                offenders.append(f"{f.name}:{role} model={rc['model']} effort={eff}")
        assert not offenders, (
            "these roles can be served by qwen3.8-max with an effort it does "
            "not define; it will be silently ignored and you will get that "
            "model's default (its HIGHEST): " + "; ".join(offenders))


class TestThinkingDialect:
    """One switch shape for every endpoint — and why that is the RIGHT answer,
    not laziness.

    The temptation is to adapt the switch per provider, because QwenCloud's doc
    says `enable_thinking: true` where DeepSeek's and z.ai's say
    `thinking: {"type": "enabled"}`. Measured 2026-08-26 by trying to DISABLE
    thinking in each dialect (binary, unlike effort levels — either the model
    stops reasoning or it does not):

        endpoint / model              thinking:{type:disabled}   enable_thinking:false
        qwen/qwen3.8-max              honoured (no reasoning)    honoured
        qwen/deepseek-v4-flash-0731   honoured                   honoured
        qwen/glm-5.2                  honoured                   honoured
        ark/deepseek-v4-flash         honoured (reasoning 0)     IGNORED (reasoning 55)
        ark/glm-5.3                   400 BadRequest             n/a (never reports)

    Two conclusions, both against adapting:

    1. The dialect is NOT a provider property — qwen and ark both serve the same
       DeepSeek models, and only ark tells them apart. It would have to be a
       per provider/model table, which is a matrix that goes stale every time a
       vendor ships.
    2. It does not need to be one. `thinking: {"type": "enabled"}` is understood
       EVERYWHERE, including by QwenCloud, which accepts both. `enable_thinking`
       is the narrower dialect — ark/deepseek ignores it outright. Switching
       qwen to "its own" dialect would gain nothing and lose uniformity.

    Recorded because the pull toward "follow each vendor's doc" is strong and
    the cost of giving in is a silently-ignored switch on ark.
    """

    def _eb(self, model, effort="low"):
        from core.ai_router import AIGateway
        gw = AIGateway(model, enable_thinking=True, thinking_effort=effort)
        return gw._build_kwargs([{"role": "user", "content": "x"}])["extra_body"]

    def test_every_non_minimax_endpoint_gets_the_portable_switch(self):
        for model in ("deepseek/deepseek-v4-flash", "qwen/qwen3.8-max",
                      "qwen/deepseek-v4-flash-0731", "ark/glm-5.3",
                      "ark/deepseek-v4-flash", "localqwen/qwen3"):
            eb = self._eb(model)
            assert eb.get("thinking") == {"type": "enabled"}, model
            assert "enable_thinking" not in eb, (
                f"{model}: enable_thinking is the NARROWER dialect — "
                f"ark/deepseek ignores it. See this class's docstring.")

    def test_we_never_send_a_disable_that_one_endpoint_rejects(self):
        """Written when nothing turned thinking off; the mode it anticipated
        now exists, so this guards a LIVE path.

        The withholding is broader than the measurement ON PURPOSE, and
        `qwen/glm-5.2` below is the case where the two differ: the table above
        records it HONOURING thinking:{disabled}, and it is withheld anyway.
        Sending the key where it is refused is a 400 that is neither failed
        over nor retried; withholding it only leaves a model thinking. The safe
        error is the broad one, and it costs nothing while no role pairs `glm`
        with thinking off. See `_rejects_thinking_disabled`."""
        from core.ai_router import AIGateway
        for model in ("ark/glm-5.3", "ark/glm-5.3-flash",
                      "opencodego/glm-5.3-flash", "qwen/glm-5.2"):
            gw = AIGateway(model, enable_thinking=False)
            eb = gw._build_kwargs([{"role": "user", "content": "x"}])["extra_body"]
            assert "thinking" not in eb, (
                f"{model}: never thinking:{{type:disabled}} — it 400s there")


class TestThinkingOffMeansOff:
    """`enable_thinking=False` must instruct, not merely stay silent.

    Sending nothing leaves the endpoint's own default in charge, and Qwen3.8's
    chat template defaults to xhigh. `compacter` — a role whose entire job is
    to shrink a transcript — therefore reasoned at the highest setting on the
    2-in-5 `flash` steps that bind localqwen, exactly contradicting its config.

    One assertion per claim: an `or` across them stays green with half the
    branch deleted.
    """

    def _eb(self, model):
        from core.ai_router import AIGateway
        return AIGateway(model, enable_thinking=False)._build_kwargs(
            [{"role": "user", "content": "x"}]).get("extra_body", {})

    def test_the_qwen_template_is_told_not_to_think(self):
        """The only key measured to work on localqwen: 22 reasoning tokens -> 0."""
        assert self._eb("localqwen/qwen3")["chat_template_kwargs"] == {
            "enable_thinking": False}

    def test_deepseek_is_told_too(self):
        """`chat_template_kwargs` is ignored on DeepSeek; `thinking` is what
        takes it to 0 there. Both keys ship, or one family stays unsuppressed."""
        eb = self._eb("deepseek/deepseek-v4-flash")
        assert eb["thinking"] == {"type": "disabled"}
        assert eb["chat_template_kwargs"] == {"enable_thinking": False}

    def test_glm_still_gets_the_portable_key(self):
        """Dropping `thinking` for GLM must not drop the other one with it —
        it measured 80 reasoning tokens -> 29 on opencodego/glm-5.3-flash."""
        assert self._eb("ark/glm-5.3-flash")["chat_template_kwargs"] == {
            "enable_thinking": False}

    def test_thinking_on_is_untouched(self):
        """The enable path must not acquire a disable key."""
        from core.ai_router import AIGateway
        eb = AIGateway("deepseek/deepseek-v4-flash",
                       enable_thinking=True)._build_kwargs(
            [{"role": "user", "content": "x"}])["extra_body"]
        assert eb["thinking"] == {"type": "enabled"}
        assert "chat_template_kwargs" not in eb


def test_litellm_globals_are_actually_applied():
    """Building a gateway must set the process-wide litellm settings.

    They were orphaned past a `return` inside `_next_usable` by the routing
    refactor and silently stopped running — dead code Python does not warn
    about, and no test noticed because `core/meta_agent.py` sets the same two
    globals, so anything that had instantiated a MetaAgent first looked fine.
    Test paths and pipeline-only processes did not.
    """
    import litellm

    from core.ai_router import AIGateway

    litellm.telemetry = True
    litellm.drop_params = False
    AIGateway("deepseek/deepseek-v4-flash")
    assert litellm.telemetry is False
    assert litellm.drop_params is True


class TestTheThinkingOffKeysCannotKillAStep:
    """The suppressors are vendor extensions, and a 400 is fatal here.

    `litellm.BadRequestError` is in neither FAILOVER_EXCEPTIONS nor
    RETRYABLE_EXCEPTIONS, so an endpoint that refuses `chat_template_kwargs` or
    `thinking:{"type":"disabled"}` would end the step. Measured 2026-08-30 none
    of the routed endpoints refuses the pair — but qwen/*, the rotation head of
    four routes, answers 429 until 09-02 and could not be measured, and
    suppressing thinking is an optimisation. It must degrade, not kill.
    """

    def _gw(self, **kw):
        from core.ai_router import AIGateway
        return AIGateway("deepseek/deepseek-v4-flash", **kw)

    def _fail_once(self, gw, exc):
        """Make the first completion raise `exc`, the second succeed; return
        the extra_body each attempt actually carried."""
        seen = []

        def _bounded(kwargs):
            seen.append(dict(kwargs.get("extra_body") or {}))
            if len(seen) == 1:
                raise exc
            return _Resp()

        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {
                "content": "ok", "tool_calls": None})()})()]
            usage = None

        gw._completion_bounded = _bounded
        gw._extract_usage = lambda r: {}
        return seen

    def test_a_rejected_suppressor_degrades_instead_of_killing_the_step(self):
        import litellm
        gw = self._gw(enable_thinking=False)
        seen = self._fail_once(gw, litellm.exceptions.BadRequestError(
            "unknown field chat_template_kwargs", "m", "p"))
        gw._complete_prebuilt(gw._build_kwargs([{"role": "user", "content": "x"}]))
        assert len(seen) == 2, "it must retry, not raise"
        assert "chat_template_kwargs" in seen[0]
        assert seen[1] == {}, "the retry must carry no suppressor at all"

    def test_the_downgrade_sticks_for_the_rest_of_the_gateway(self):
        """One rejection, one extra call — not one per turn forever."""
        import litellm
        gw = self._gw(enable_thinking=False)
        self._fail_once(gw, litellm.exceptions.BadRequestError("nope", "m", "p"))
        gw._complete_prebuilt(gw._build_kwargs([{"role": "user", "content": "x"}]))
        assert gw._suppress_thinking is False
        later = gw._build_kwargs([{"role": "user", "content": "y"}])
        assert "extra_body" not in later

    def test_a_role_that_WANTS_thinking_is_never_downgraded(self):
        """The retry must not strip a reasoning role's own switch — that would
        silently turn a reviewer into a non-reasoning one on any bad request."""
        import litellm
        import pytest as _pytest
        gw = self._gw(enable_thinking=True, thinking_effort="low")
        seen = self._fail_once(gw, litellm.exceptions.BadRequestError(
            "something else entirely", "m", "p"))
        with _pytest.raises(Exception):
            gw._complete_prebuilt(
                gw._build_kwargs([{"role": "user", "content": "x"}]))
        assert len(seen) == 1, "a thinking-ON role must not get the retry"


class TestARefusedInjectedParameterIsDroppedByName:
    """The mirror image of the thinking-off guard, and the arm that killed a step.

    Measured on the worker route (iss-357ac3514c684371): the driver builds the
    gateway with `enable_thinking=True` (api/dsh_driver_routers.py), so the
    thinking-off guard above — gated on `not self.enable_thinking` — can never
    fire. The pool endpoint refused the `thinking` this gateway itself injected
    with a 400; `litellm.BadRequestError` is in neither FAILOVER_EXCEPTIONS nor
    RETRYABLE_EXCEPTIONS, so the step died, and because the SSE response had
    already been opened the client saw only "Unexpected end of JSON input".

    The fix is not "retry a thinking-ON role on any 400" — that would silently
    turn a reviewer into a non-reasoning one, which
    `test_a_role_that_WANTS_thinking_is_never_downgraded` pins. It is: read
    what the refusal NAMES, and drop exactly that key.

    Three arms, one assertion each: a named key is dropped and the turn
    succeeds, the drop is EXACTLY that key, and it is bounded to one retry.
    """

    #: The recorded shape of the upstream answer: a 400 that names the argument
    #: it will not accept. `thinking` is the key the enable branch injects.
    REFUSAL = ("litellm.BadRequestError: OpenAIException - Unrecognized "
               "request argument supplied: thinking")

    def _gw(self, **kw):
        from core.ai_router import AIGateway
        return AIGateway("deepseek/deepseek-v4-flash", **kw)

    def _stub(self, gw, *errors):
        """Stub transport: raise each of `errors` in order, then answer.

        Returns the kwargs of every attempt, COPIED — the gateway mutates one
        dict in place across attempts, so a reference would let the retry
        rewrite what the first attempt actually sent.
        """
        seen = []

        def _bounded(kwargs):
            seen.append({k: (dict(v) if isinstance(v, dict) else v)
                         for k, v in kwargs.items()})
            if len(seen) <= len(errors):
                raise errors[len(seen) - 1]
            return _Resp()

        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {
                "content": "ok", "tool_calls": None})()})()]
            usage = None

        gw._completion_bounded = _bounded
        gw._extract_usage = lambda r: {}
        return seen

    def test_a_named_refusal_drops_that_key_and_the_turn_succeeds(self, capsys):
        """The recorded failure end to end: the step must live."""
        gw = self._gw(enable_thinking=True, thinking_effort="low")
        seen = self._stub(gw, litellm.exceptions.BadRequestError(
            self.REFUSAL, "m", "p"))
        response = gw._complete_prebuilt(
            gw._build_kwargs([{"role": "user", "content": "x"}]))
        assert response is not None, "the turn must not die on OUR OWN parameter"
        assert len(seen) == 2, "it must retry once, in place, not raise"
        assert "thinking" in seen[0]["extra_body"], (
            "the first attempt must really have carried the refused key, or "
            "the retry proves nothing")
        assert "thinking" not in seen[1].get("extra_body", {}), (
            "the retry must not carry the key the endpoint refused")
        assert "thinking" not in seen[1], "nor as a top-level key"
        out = capsys.readouterr().out
        assert "extra_body.thinking" in out, "the dropped key must be printed"
        assert gw.active_model in out, "and WHICH endpoint refused it"

    def test_exactly_the_named_key_goes(self):
        """A reasoning role keeps the effort it was not refused for — dropping
        the whole envelope would degrade it just as silently as refusing it."""
        gw = self._gw(enable_thinking=True, thinking_effort="low")
        seen = self._stub(gw, litellm.exceptions.BadRequestError(
            self.REFUSAL, "m", "p"))
        gw._complete_prebuilt(
            gw._build_kwargs([{"role": "user", "content": "x"}]))
        assert seen[1]["extra_body"] == {
            k: v for k, v in seen[0]["extra_body"].items() if k != "thinking"}, (
        "exactly the named key goes; every other injected key stays")
        assert {k: v for k, v in seen[0].items() if k != "extra_body"} == {
            k: v for k, v in seen[1].items() if k != "extra_body"}, (
        "nothing outside extra_body may move either")

    def test_the_named_drop_is_bounded_to_one_retry(self):
        """A second refusal on the same key means the key was not the story."""
        gw = self._gw(enable_thinking=True, thinking_effort="low")
        refusal = litellm.exceptions.BadRequestError(self.REFUSAL, "m", "p")
        seen = self._stub(gw, refusal, refusal)
        with pytest.raises(Exception):
            gw._complete_prebuilt(
                gw._build_kwargs([{"role": "user", "content": "x"}]))
        assert len(seen) == 2, "one drop, one retry — never a loop"
        assert gw._dropped_injected_param == "thinking"

    def test_a_400_naming_nothing_we_injected_still_raises(self):
        """`top_k` is the caller's/provider's business, not ours: no retry."""
        gw = self._gw(enable_thinking=True, thinking_effort="low")
        seen = self._stub(gw, litellm.exceptions.BadRequestError(
            "Unrecognized request argument supplied: top_k", "m", "p"))
        with pytest.raises(Exception):
            gw._complete_prebuilt(
                gw._build_kwargs([{"role": "user", "content": "x"}]))
        assert len(seen) == 1, "not our parameter, not our retry"
        assert gw._dropped_injected_param is None

    def test_the_next_turn_does_not_resend_the_refused_key(self):
        """The refusal is remembered per endpoint, not just for one call.

        Measured 2026-11-22 on the worker route: `_apply_binding` writes
        `extra_body.thinking` afresh on EVERY turn of a session, while the drop
        was budgeted once per gateway. So a child that drew an
        `opencodego*/beam-free` endpoint survived the turn that dropped the key
        and died on the next one with the same 400 — the client saw a truncated
        SSE body (`Unexpected end of JSON input`), not a refusal. Four children
        of this project's director ended that way in twelve minutes.
        """
        gw = self._gw(enable_thinking=True, thinking_effort="low")
        seen = self._stub(gw, litellm.exceptions.BadRequestError(
            self.REFUSAL, "m", "p"))
        gw._complete_prebuilt(gw._build_kwargs([{"role": "user", "content": "x"}]))
        assert len(seen) == 2, "turn 1: one drop, one retry"
        later = self._stub(gw)                       # no refusal queued
        gw._complete_prebuilt(gw._build_kwargs([{"role": "user", "content": "y"}]))
        assert len(later) == 1, (
            "turn 2 must not need a retry: the key was already refused here")
        assert "thinking" not in (later[0].get("extra_body") or {}), (
            "and must not carry the refused key at all")
        assert "thinking" not in later[0], "nor as a top-level key"

    def test_another_endpoint_still_gets_the_key(self):
        """A refusal belongs to the endpoint that made it, not to the gateway.

        The remembered key is what a FAILOVER must not inherit: the next
        endpoint down the list never refused anything, and withholding a
        reasoning switch from it would silently turn a reasoning role into a
        non-reasoning one.
        """
        gw = self._gw(enable_thinking=True, thinking_effort="low")
        self._stub(gw, litellm.exceptions.BadRequestError(self.REFUSAL, "m", "p"))
        gw._complete_prebuilt(gw._build_kwargs([{"role": "user", "content": "x"}]))
        refused_endpoint = gw.active_model
        assert gw._refused_params == {refused_endpoint: "thinking"}
        gw._bind("someother/deepseek-v4.1-flash")    # a failover moved the binding
        moved = gw._build_kwargs([{"role": "user", "content": "y"}])
        assert "thinking" in (moved.get("extra_body") or {}), (
            "an endpoint that never refused the key still gets it")


# ── the four gaps a non-author review measured in that memory ──────────────
# `TestARefusedInjectedParameterIsDroppedByName` above pins the drop itself.
# What follows pins the MEMORY around it: where it runs, what counts as a
# refusal, what a whole-envelope withdrawal is recorded as, and how many
# refusals one endpoint's record can hold. Each class is one measured gap, and
# each is RED on the bytes that introduced the memory.


def _stub_transport(gw, *errors):
    """Stub transport: raise each of `errors` in order, then answer 200.

    Returns a COPY of every attempt's kwargs — the gateway mutates one dict in
    place across attempts, so a reference would let a later attempt rewrite what
    an earlier one actually sent.
    """
    seen = []

    def _bounded(kwargs):
        seen.append({k: (dict(v) if isinstance(v, dict) else v)
                     for k, v in kwargs.items()})
        if len(seen) <= len(errors):
            raise errors[len(seen) - 1]
        return _Resp()

    class _Resp:
        choices = [type("C", (), {"message": type("M", (), {
            "content": "ok", "tool_calls": None})()})()]
        usage = None

    gw._completion_bounded = _bounded
    gw._extract_usage = lambda r: {}
    return seen


def _recorded(gw, endpoint):
    """The generation parameters `gw`'s record holds for `endpoint`.

    Read the way `_apply_binding` reads it — as refused NAMES — rather than as
    whatever container the implementation happens to keep them in, so the
    witness fails on the missing name and never on the shape of the record.
    """
    record = gw._refused_params.get(endpoint, "")
    if isinstance(record, str):
        return {name.strip() for name in record.split(",") if name.strip()}
    return set(record)


class TestTheAnthropicBranchDoesNotResendARefusedKey:
    """Gap (i): the remembered strip never ran on the anthropic branch.

    `_apply_binding` returns early for `anthropic/*` endpoints — their own
    dialect has no `extra_body`, so `thinking` / `reasoning_effort` are injected
    as TOP-LEVEL parameters instead — and the strip sat after that return. The
    drop-and-retry therefore survived only the turn it happened on: the next
    turn rebuilt the refused key from the same branch and was refused again, for
    the rest of the session, exactly the failure the memory exists to prevent.

    The endpoint is a gateway-fronted Anthropic one: the recorded shape of a
    refusal there is the pool endpoint's own wording ("Unrecognized request
    argument supplied: …"), and `reasoning_effort` is what this branch injects
    for a role that asked for thinking.
    """

    ENDPOINT = "anthropic/claude-sonnet-4-5"
    REFUSAL = "Unrecognized request argument supplied: reasoning_effort"

    def _gw(self):
        return AIGateway(self.ENDPOINT, enable_thinking=True,
                         thinking_effort="low")

    def test_the_next_turn_does_not_resend_a_key_this_endpoint_refused(self):
        gw = self._gw()
        assert gw.active_model == self.ENDPOINT
        seen = _stub_transport(gw, litellm.exceptions.BadRequestError(
            self.REFUSAL, "m", "p"))
        response = gw._complete_prebuilt(
            gw._build_kwargs([{"role": "user", "content": "x"}]))
        assert response is not None, (
            "the turn must not die on a parameter WE injected")
        assert len(seen) == 2, "one drop, one retry"
        assert seen[0].get("reasoning_effort") == "low", (
            "the first attempt must really have carried the refused key, or "
            "the rest of this test proves nothing")
        assert "reasoning_effort" not in seen[1], (
            "the retry must not carry the key the endpoint refused")

        later = _stub_transport(gw)                  # turn 2, no refusal queued
        gw._complete_prebuilt(
            gw._build_kwargs([{"role": "user", "content": "y"}]))
        assert len(later) == 1, (
            "turn 2 must not need a retry: this endpoint already refused the key")
        assert not {"thinking", "reasoning_effort", "extra_body"} & set(later[0]), (
            "gap (i): the anthropic branch rebuilt the refused key and returned "
            f"before the remembered strip ran: {sorted(later[0])}")


class TestOnlyARefusedArgumentIsAttributed:
    """Gap (ii): attribution was a bare substring, so PROSE counted as refusal.

    `if name in text` answers for any message that contains the name. The
    recorded shape of the counter-example is a 400 about the MESSAGES that
    happens to use the word:

        messages.2: thinking blocks are not allowed when tools are present

    Nothing in it refuses `thinking` as a parameter, but the key was dropped
    anyway: the request stayed wrong in exactly the way the endpoint said, and
    the reasoning role was silently degraded for the rest of the session on top
    of it. A name is attributed only when the text marks it as an argument or
    parameter — the shapes the existing witnesses record
    ("Unrecognized request argument supplied: thinking" / "…: top_k" in
    `TestARefusedInjectedParameterIsDroppedByName`) still are.
    """

    PROSE = ("messages.2: thinking blocks are not allowed when tools are "
             "present")

    def test_a_message_that_merely_mentions_the_key_is_not_a_refusal(self):
        gw = AIGateway("deepseek/deepseek-v4-flash", enable_thinking=True,
                       thinking_effort="low")
        seen = _stub_transport(gw, litellm.exceptions.BadRequestError(
            self.PROSE, "m", "p"))
        with pytest.raises(litellm.exceptions.BadRequestError) as caught:
            gw._complete_prebuilt(
                gw._build_kwargs([{"role": "user", "content": "x"}]))
        assert self.PROSE in str(caught.value), (
            "the caller must SEE the error the endpoint actually raised")
        assert len(seen) == 1, (
            "not a refusal of our key: no drop, no retry, no silent degradation")
        assert "thinking" in (seen[0].get("extra_body") or {}), (
            "the reasoning switch must still be sent — the 400 was not about it")
        assert gw._dropped_injected_param is None
        assert gw._refused_params == {}, (
            "and nothing is remembered, so later turns keep the key too")


class TestAnEnvelopeWithdrawalNamesWhatWentWithIt:
    """Gap (iii): a whole-envelope refusal was silent about what it took.

    A 400 naming `extra_body` pops the envelope and every nested parameter with
    it — `thinking`, `reasoning_effort` — and nothing said which ones went. The
    invariants pinned here: the refusal is REMEMBERED as the envelope alone (no
    nested key is recorded as individually refused, so a later request carrying
    one of them is not stripped for a refusal that was never about it), and
    exactly ONE `[ai_router]` line names the members that went with it, on the
    same line as the refusal it belongs to.

    A 400 naming a NESTED key still drops just that key and keeps the rest of
    the envelope — that is pinned by
    `TestARefusedInjectedParameterIsDroppedByName::test_exactly_the_named_key_goes`.
    """

    #: The recorded shape of the native-Anthropic refusal litellm produces when
    #: `extra_body` is forwarded verbatim (see `_apply_binding`).
    REFUSAL = "extra_body: Extra inputs are not permitted"

    def test_the_envelope_alone_is_remembered_and_its_members_are_named(
            self, capsys):
        gw = AIGateway("deepseek/deepseek-v4-flash", enable_thinking=True,
                       thinking_effort="low")
        first = _stub_transport(gw, litellm.exceptions.BadRequestError(
            self.REFUSAL, "m", "p"))
        gw._complete_prebuilt(
            gw._build_kwargs([{"role": "user", "content": "x"}]))
        assert len(first) == 2, "one drop, one retry"
        assert set(first[0]["extra_body"]) == {"thinking", "reasoning_effort"}, (
            "both injected members must really have been on the wire, or "
            "'what went with it' is a claim about nothing")
        assert "extra_body" not in first[1], "the envelope is withdrawn"
        endpoint = gw.active_model
        assert _recorded(gw, endpoint) == {"extra_body"}, (
            "the record must be the ENVELOPE alone: a nested name recorded here "
            "would strip a later request carrying only that key, for a refusal "
            f"that was never about it ({gw._refused_params!r})")
        lines = [ln for ln in capsys.readouterr().out.splitlines()
                 if "rejected an injected generation parameter" in ln]
        assert len(lines) == 1, (
            f"exactly one line for the withdrawal, got {lines!r}")
        assert "extra_body.thinking" in lines[0], (
            "and it must name the member that went with the envelope")
        assert "extra_body.reasoning_effort" in lines[0], (
            "every member, not just the first one")

        later = _stub_transport(gw)                  # turn 2, no refusal queued
        gw._complete_prebuilt(
            gw._build_kwargs([{"role": "user", "content": "y"}]))
        assert "extra_body" not in later[0], (
            "the withdrawn envelope stays withdrawn on the next turn")


class TestEveryRefusedKeyIsRememberedPerEndpoint:
    """Gap (iv): one slot per endpoint, so a second refusal cost a second 400.

    `_refused_params: dict[str, str]` held a single name per endpoint and the
    writer overwrote it. An endpoint is sticky for a whole step, so it can
    refuse TWO of the parameters `_apply_binding` injects: the first was
    remembered, the second replaced it, and the first was re-injected on the
    next turn — a 400 per turn for a refusal already known and already printed.
    """

    THINKING_OFF = "Unrecognized request argument supplied: thinking"
    EFFORT_OFF = "Unrecognized request argument supplied: reasoning_effort"

    def _turn(self, gw, *errors):
        seen = _stub_transport(gw, *errors)
        gw._complete_prebuilt(
            gw._build_kwargs([{"role": "user", "content": "x"}]))
        return seen

    def test_two_refusals_on_one_endpoint_are_both_remembered(self):
        gw = AIGateway("deepseek/deepseek-v4-flash", enable_thinking=True,
                       thinking_effort="low")
        endpoint = gw.active_model
        turn1 = self._turn(gw, litellm.exceptions.BadRequestError(
            self.THINKING_OFF, "m", "p"))
        assert len(turn1) == 2, "turn 1: one drop, one retry"
        turn2 = self._turn(gw, litellm.exceptions.BadRequestError(
            self.EFFORT_OFF, "m", "p"))
        assert len(turn2) == 2, (
            "turn 2: the second key is refused here and dropped too")
        assert _recorded(gw, endpoint) == {"thinking", "reasoning_effort"}, (
            "gap (iv): the second refusal REPLACED the first instead of "
            f"joining it ({gw._refused_params!r})")

        turn3 = self._turn(gw)                       # neither key is refused
        assert len(turn3) == 1, (
            "turn 3 must not pay another 400: both keys were already refused")
        assert not {"extra_body", "thinking", "reasoning_effort"} & set(turn3[0]), (
            "and it must carry neither of them, in no shape: "
            f"{sorted(turn3[0])}")


class TestFailoverNeverRevisitsADeadEndpoint:
    """A route may name one endpoint twice on purpose.

    The duplicate `localqwen/qwen3` in `flash.rotate` is what gives the local
    box a 2-in-5 share of the rotation head. The failover walk is a single
    forward pass over the resolved list, so that duplicate used to be tried
    twice. Measured 2026-08-30 while the box was down: localqwen -> qwen (429,
    parked) -> localqwen (still down) -> opencodego. Three hops, the middle one
    certain to fail.

    Parking does not cover it — that needs `is_quota_exhausted`, and a box that
    is DOWN is not quota-exhausted.
    """

    def _gw(self, candidates):
        from core.ai_router import AIGateway
        gw = AIGateway.__new__(AIGateway)          # no route table, no binding
        gw._candidates = list(candidates)
        gw._candidate_ix = 0
        gw._failovers = []
        gw._burst_hits = 0
        gw.internal_model = "flash"
        gw.active_model = candidates[0]
        gw._next_usable = lambda start: start      # nothing parked in this test
        gw._bind = lambda m: setattr(gw, "active_model", m)
        return gw

    def _walk(self, gw):
        """Fail over until exhausted; return the endpoints actually bound."""
        seen = [gw.active_model]
        while gw._failover(RuntimeError("endpoint down")):
            seen.append(gw.active_model)
        return seen

    def test_a_duplicate_is_not_tried_twice(self):
        gw = self._gw(["local/q", "plan/a", "local/q", "payg/b", "ark/c"])
        assert self._walk(gw) == ["local/q", "plan/a", "payg/b", "ark/c"], (
            "index 2 repeats local/q, which already failed at index 0")

    def test_the_walk_still_reaches_every_distinct_endpoint(self):
        """Skipping must not cut the list short — the whole point of a fallback
        list is that the LAST entry still gets its turn."""
        gw = self._gw(["a/1", "b/2", "a/1", "b/2", "c/3"])
        assert self._walk(gw) == ["a/1", "b/2", "c/3"]

    def test_exhaustion_is_reported_not_looped(self):
        """When only duplicates remain, `_failover` must return False so the
        caller raises the real provider error — never spin."""
        gw = self._gw(["a/1", "a/1", "a/1"])
        assert self._walk(gw) == ["a/1"]
        assert gw._failover(RuntimeError("down")) is False

    def test_a_route_with_no_duplicates_is_unchanged(self):
        gw = self._gw(["a/1", "b/2", "c/3"])
        assert self._walk(gw) == ["a/1", "b/2", "c/3"]


# ── malformed tool-call arguments in history (2026-11-05 incident) ──────────
# The real arguments of wuxia-myth root history seq 14564, shortened: one JSON
# object followed by an extra closing brace. ollamacloud /v1 answered the whole
# request with 400 "invalid tool call arguments".
_EXTRA_DATA = '{"arguments": {"action": "send_director_message", "arguments": {"body": "kill it"}}}}'


def _assistant_call(arguments):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "call_tool", "arguments": arguments}}]}


@pytest.mark.parametrize("arguments, expected", [
    ('{"a": 1}', '{"a": 1}'),
    ('{ "a" : [1, 2] }', '{ "a" : [1, 2] }'),
    ({"a": "é"}, '{"a": "é"}'),
    ("", "{}"),
    ("   ", "{}"),
    (None, "{}"),
    (_EXTRA_DATA, None),
    ('{"a": "trunc', None),
    ('[1, 2]', None),
    ('7', None),
    ('null', None),
])
def test_sanitize_makes_every_tool_call_argument_a_json_object(arguments, expected):
    import json
    [out] = AIGateway._sanitize_messages([_assistant_call(arguments)])
    got = out["tool_calls"][0]["function"]["arguments"]
    assert isinstance(json.loads(got), dict), got
    if expected is not None:
        assert got == expected
    else:
        # Not one object: the original text is kept whole, not "repaired" into a
        # call the model never made.
        assert json.loads(got) == {"unparsed_arguments": arguments}


def test_sanitize_passes_valid_arguments_through_unchanged_and_never_mutates():
    import copy
    valid = _assistant_call('{"cmd": "ls"}')
    bad = _assistant_call(_EXTRA_DATA)
    tool = {"role": "tool", "tool_call_id": "c1", "content": "not json {"}
    user = {"role": "user", "content": "hi"}
    before = copy.deepcopy([valid, bad, tool, user])
    out = AIGateway._sanitize_messages([valid, bad, tool, user])
    assert [valid, bad, tool, user] == before, "the caller's history must not be mutated"
    assert out[0] is valid, "an already-valid call is the same object: byte-identical prefix"
    assert out[2] is tool and out[3] is user
    assert out[1]["tool_calls"][0]["id"] == "c1"
    assert out[1]["tool_calls"][0]["function"]["name"] == "call_tool"


def test_generate_native_sends_normalized_tool_call_arguments():
    import json
    gw = AIGateway("deepseek/deepseek-v4-flash")
    resp = _native_response(finish_reason="stop", content="ok")
    history = [{"role": "user", "content": "go"}, _assistant_call(_EXTRA_DATA),
               {"role": "tool", "tool_call_id": "c1", "content": "invalid JSON"},
               {"role": "user", "content": "again"}]
    with patch('litellm.completion', return_value=resp) as completion:
        gw.generate_native(history, tools=[{"x": 1}])
    sent = completion.call_args.kwargs["messages"]
    args = sent[1]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args) == {"unparsed_arguments": _EXTRA_DATA}
    assert history[1]["tool_calls"][0]["function"]["arguments"] == _EXTRA_DATA


def test_the_one_refusal_message_shape_is_the_shared_guards(capsys, monkeypatch):
    """One `[ai_router] refused outgoing body` line, naming the caller.

    The occurrence's second half was silence: the failure had no line anyone
    could grep. The line is printed by the shared guard, so every writer
    reports in one shape.
    """
    from core import ai_router
    from core.ai_router import OutgoingBodyTooLarge

    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", 4_000)
    kwargs = {"model": "m", "messages": [{"role": "user", "content": "z" * 9_000}]}
    identity = ai_router.body_identity(kwargs, caller="test.caller",
                                       model_route="flash", session="s1")
    with pytest.raises(OutgoingBodyTooLarge) as caught:
        ai_router.refuse_if_body_too_large(kwargs, identity=identity)

    lines = [ln for ln in capsys.readouterr().out.splitlines()
             if "refused outgoing body" in ln]
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith("[ai_router] refused outgoing body (test.caller, "
                           "flash, session s1):")
    assert f"{caught.value.measured_bytes:,} bytes > 4,000 bound" in line
    assert "largest messages[0]" in line
