# tests/test_ai_router.py
import pytest
from unittest.mock import patch, MagicMock
from core.ai_router import AIGateway
import litellm

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


def test_extract_usage_no_cache_fields():
    """No cache info → hit=0, miss=all prompt tokens."""
    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=500, completion_tokens=20))
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
    """Anthropic-family models get a system-message cache breakpoint."""
    gw = AIGateway("anthropic/claude-sonnet-4-6")
    pts = gw._cache_control_points()
    assert pts == [{"location": "message", "role": "system"}]


def test_cache_control_points_deepseek_none():
    """Auto-cachers (DeepSeek/Minimax) must NOT get a cache_control field."""
    assert AIGateway("deepseek/deepseek-v4-flash")._cache_control_points() is None
    assert AIGateway("minimax/abab6.5")._cache_control_points() is None


def test_build_kwargs_omits_cache_control_for_deepseek():
    gw = AIGateway("deepseek/deepseek-v4-flash")
    kwargs = gw._build_kwargs([{"role": "user", "content": "hi"}])
    assert "cache_control_injection_points" not in kwargs
