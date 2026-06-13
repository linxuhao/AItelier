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
