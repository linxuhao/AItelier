"""DeepSeek direct reads images through the OpenAI-compatible path (owner 2026-11-23).

litellm's native `deepseek/` provider collapses a user message's content list to
a string - dropping its image_url blocks without an error - unless litellm's
model map says the model supports vision. That map is fetched at backend start,
and the host venv's older litellm always collapses: a probe on 2026-11-23 sent a
64x32 PNG through `deepseek/deepseek-flash` and the model saw 17 prompt tokens
("I can't see an image"). The same endpoint, key and model id through litellm's
`openai/` path read the digits (219 prompt tokens) and a Chinese A4 page.

So an image-bearing request bound to a `deepseek/` model goes out as
`openai/<model>` with the same api_base and key. A text-only request keeps the
native provider, exactly as before.
"""
import json

import pytest

from core.ai_router import AIGateway

IMAGE = {"role": "user", "content": [
    {"type": "text", "text": "What digits are in this image?"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]}
TEXT = {"role": "user", "content": "hi"}


@pytest.fixture
def providers(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_TEST_DS_KEY", "k-deepseek")
    monkeypatch.setenv("AITELIER_TEST_OG_KEY", "k-opencode")
    p = tmp_path / "llm_providers.json"
    p.write_text(json.dumps({
        "deepseek": {"base_url": "https://api.deepseek.com/", "api_key_env": "AITELIER_TEST_DS_KEY"},
        "opencodego2": {"base_url": "https://opencode.ai/zen/go/v1", "api_key_env": "AITELIER_TEST_OG_KEY"},
    }), encoding="utf-8")
    return str(p)


def _gw(model, providers, **kw):
    return AIGateway(model, config_path=providers, routes_path=providers + ".absent", **kw)


def test_an_image_request_to_deepseek_direct_goes_through_the_openai_path(providers):
    gw = _gw("deepseek/deepseek-flash", providers, enable_thinking=True, thinking_effort="high")
    kwargs = gw._build_kwargs([{"role": "system", "content": "s"}, IMAGE])
    assert kwargs["model"] == "openai/deepseek-flash"
    assert kwargs["api_base"] == "https://api.deepseek.com/"
    assert kwargs["api_key"] == "k-deepseek"
    # The image block is still in the request, and the thinking keys are unchanged.
    assert kwargs["messages"][1]["content"][1]["type"] == "image_url"
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert gw.litellm_model == "deepseek/deepseek-flash", "the binding itself is unchanged"


def test_a_text_request_to_deepseek_direct_keeps_the_native_provider(providers):
    gw = _gw("deepseek/deepseek-flash", providers)
    assert gw._build_kwargs([TEXT])["model"] == "deepseek/deepseek-flash"
    # A content list with no image is not an image request either.
    listed = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    assert gw._build_kwargs([listed])["model"] == "deepseek/deepseek-flash"


def test_an_image_in_the_history_counts_too(providers):
    """A DSH session sends earlier turns again: an image anywhere in the request
    would be collapsed by the native provider, so the whole request switches."""
    gw = _gw("deepseek/deepseek-flash", providers)
    kwargs = gw._build_kwargs([IMAGE, {"role": "assistant", "content": "4721"}, TEXT])
    assert kwargs["model"] == "openai/deepseek-flash"


def test_other_providers_are_untouched(providers):
    gw = _gw("opencodego2/deepseek-v4.1-flash", providers)
    assert gw._build_kwargs([IMAGE])["model"] == "openai/deepseek-v4.1-flash"
    assert gw._build_kwargs([TEXT])["model"] == "openai/deepseek-v4.1-flash"


def test_the_call_litellm_receives_uses_the_openai_path(providers, monkeypatch):
    """End to end through generate_native: the model litellm is called with."""
    import litellm
    from unittest.mock import MagicMock
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")
    seen = []
    response = MagicMock()
    response.choices[0].message.content = "4721"
    response.choices[0].message.tool_calls = None
    response.choices[0].message.reasoning_content = ""
    response.choices[0].finish_reason = "stop"

    def fake(**kwargs):
        seen.append((kwargs["model"], kwargs["api_base"]))
        return response
    monkeypatch.setattr(litellm, "completion", fake)
    gw = _gw("deepseek/deepseek-flash", providers)
    AIGateway.generate_native.__wrapped__(gw, [IMAGE])
    AIGateway.generate_native.__wrapped__(gw, [TEXT])
    assert seen == [("openai/deepseek-flash", "https://api.deepseek.com/"),
                    ("deepseek/deepseek-flash", "https://api.deepseek.com/")]
