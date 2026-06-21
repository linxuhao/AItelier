# core/ai_router.py
# 引入本地 Provider 注册表。通过拦截自定义前缀并强制转译为 openai/ 协议，彻底接管网关路由。
# v2: 新增 native tool calling 支持 (generate_native)。

import os
import re
import json
import uuid
from dataclasses import dataclass, field
from typing import Optional
import litellm
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)

RETRYABLE_EXCEPTIONS = (
    litellm.exceptions.RateLimitError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.Timeout,
    litellm.exceptions.APIConnectionError
)


# Some providers (notably DeepSeek) intermittently emit their tool calls as
# *content* using an Anthropic-style markup wrapped in "｜｜DSML｜｜" markers
# (｜ = U+FF5C fullwidth pipe) instead of returning structured `tool_calls`.
# When that happens LiteLLM hands us plain text and `msg.tool_calls` is empty,
# so the call is silently dropped (the agent's file writes / reads vanish and
# the step later fails validation). We salvage these by parsing the markup.
# We key on the `invoke name=` / `parameter name=` tokens and ignore the
# surrounding pipe/DSML noise so the parser is robust to encoding variants.
_DSML_INVOKE_RE = re.compile(
    r"invoke\s+name=\"([^\"]+)\"\s*>(.*?)</[^>]*invoke\s*>", re.DOTALL)
_DSML_PARAM_RE = re.compile(
    r"parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?[^>]*>"
    r"(.*?)</[^>]*parameter\s*>", re.DOTALL)


def parse_dsml_tool_calls(content: str) -> list[dict]:
    """Extract tool calls from DeepSeek 'DSML' markup leaked into content.

    Returns a list of OpenAI-format tool_call dicts. Empty if no markup found.
    """
    if not content or "invoke name=" not in content:
        return []
    tool_calls: list[dict] = []
    for name, body in _DSML_INVOKE_RE.findall(content):
        args: dict = {}
        for pname, is_string, pval in _DSML_PARAM_RE.findall(body):
            val = pval.strip()
            if is_string == "false":
                # non-string params: bool / number / json
                try:
                    val = json.loads(val)
                except (ValueError, TypeError):
                    pass  # keep raw string if it doesn't parse
            args[pname] = val
        tool_calls.append({
            "id": f"dsml_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        })
    return tool_calls


def strip_dsml_markup(content: str) -> str:
    """Remove leaked DSML tool-call markup from content, leaving clean prose."""
    if not content or "DSML" not in content:
        return content
    # Drop everything from the first DSML tool_calls/invoke marker onward.
    cut = re.search(r"<[^>]*DSML[^>]*>|<[^>]*invoke\s+name=", content)
    return content[:cut.start()].rstrip() if cut else content


@dataclass
class NativeTurn:
    """Result of a native tool-calling completion turn.

    Attributes:
        text: Model's free-text content (thoughts / reasoning).
        tool_calls: List of structured tool calls, each with
                    ``{id, function: {name, arguments}}``.
                    Empty list means the model has finished.
        reasoning_content: DeepSeek-style chain-of-thought content.
            Must be passed back on subsequent turns when thinking + tools
            are used together, otherwise the API returns a 400 error.
    """
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    reasoning_content: str = ""


class AIGateway:
    """
    AItelier 统一模型路由网关
    拦截本地 JSON 配置中的自定义 Provider，并自动降级为 OpenAI 兼容协议发起请求。
    """

    def __init__(self, model_name: str, config_path: str = "llm_providers.json",
                 enable_thinking: bool = False, thinking_effort: str | None = None,
                 temperature: float = 0.2, max_output_tokens: int = 8192):
        self.api_base = None
        self.api_key = None
        self.litellm_model = model_name
        self.enable_thinking = enable_thinking
        self.thinking_effort = thinking_effort
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.provider = None
        # Phase 0 cache telemetry: usage of the most recent completion.
        self.last_usage: dict = {}

        # 读取本地 Provider 注册表
        if os.path.exists(config_path) and '/' in model_name:
            provider, actual_model = model_name.split('/', 1)
            self.provider = provider

            with open(config_path, "r", encoding="utf-8") as f:
                providers = json.load(f)

            if provider in providers:
                cfg = providers[provider]
                self.api_base = cfg.get("base_url")

                # 动态提取环境变量中的 API Key
                key_env = cfg.get("api_key_env")
                if key_env:
                    self.api_key = os.getenv(key_env)

                # Use LiteLLM's native provider when available (minimax, etc.).
                try:
                    _, native_provider, _, _ = litellm.get_llm_provider(model_name)
                    if native_provider and native_provider != "openai":
                        self.litellm_model = model_name
                    else:
                        self.litellm_model = f"openai/{actual_model}"
                except Exception:
                    self.litellm_model = f"openai/{actual_model}"

        litellm.telemetry = False
        litellm.drop_params = True

    # ── cache telemetry ──────────────────────────────────────────────

    @staticmethod
    def _extract_usage(response) -> dict:
        """Pull token + prompt-cache stats from a completion response.

        Normalizes across providers:
          - DeepSeek: usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens
          - OpenAI-style: usage.prompt_tokens_details.cached_tokens
        Cache-hit tokens on DeepSeek bill at ~1/10th, so hit_ratio is the
        key cost lever this telemetry measures. Returns {} if no usage.
        """
        usage = getattr(response, "usage", None)
        if not usage:
            return {}

        def _num(v):
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        try:
            prompt_tokens = _num(getattr(usage, "prompt_tokens", None)) or 0
            completion_tokens = _num(getattr(usage, "completion_tokens", None)) or 0

            # DeepSeek exposes explicit hit/miss split.
            hit = _num(getattr(usage, "prompt_cache_hit_tokens", None))
            miss = _num(getattr(usage, "prompt_cache_miss_tokens", None))
            # OpenAI-style nests cached count under prompt_tokens_details.
            if hit is None:
                details = getattr(usage, "prompt_tokens_details", None)
                cached = _num(getattr(details, "cached_tokens", None)) if details else None
                if cached is not None:
                    hit = cached
                    miss = prompt_tokens - cached

            if not prompt_tokens and hit is None:
                return {}

            hit = hit or 0
            miss = miss if miss is not None else (prompt_tokens - hit)
            hit_ratio = (hit / prompt_tokens) if prompt_tokens else 0.0
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_hit_tokens": hit,
                "cache_miss_tokens": max(miss, 0),
                "hit_ratio": round(hit_ratio, 4),
            }
        except (TypeError, ValueError):
            return {}

    # ── shared kwargs builder ────────────────────────────────────────

    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        """P1-1: guard against empty/whitespace message content.

        Some providers (notably Deepseek) reject a request with
        `BadRequestError: Prompt must contain ...` when any turn has empty
        content. Rather than drop turns (which can break role alternation),
        replace empty content with a single-space sentinel. Messages that carry
        tool_calls / tool results legitimately may have empty content and are
        left untouched.
        """
        cleaned: list[dict] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            has_tools = bool(m.get("tool_calls")) or m.get("role") == "tool"
            if not has_tools and (content is None or
                                  (isinstance(content, str) and not content.strip())):
                m = {**m, "content": " "}
            cleaned.append(m)
        return cleaned

    def _cache_control_points(self):
        """Return LiteLLM cache_control_injection_points for explicit-cache
        providers (Anthropic family), else None.

        Marks the system message as the breakpoint so everything up to and
        including it (tools + system) is cached. DeepSeek/Minimax/OpenAI use
        automatic prefix caching and are intentionally excluded — sending them
        a cache_control field is at best ignored and at worst rejected.
        """
        model = (self.litellm_model or "").lower()
        is_anthropic = self.provider == "anthropic" or "claude" in model or "anthropic" in model
        if not is_anthropic:
            return None
        return [{"location": "message", "role": "system"}]

    def _build_kwargs(self, messages: list[dict], **extra) -> dict:
        """Build litellm completion kwargs from state + extra."""
        messages = self._sanitize_messages(messages)
        kwargs = {
            "model": self.litellm_model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            # A2 fix: bound LiteLLM call. Without this, a stalled provider
            # (e.g. deepseek v4-flash hung on 2nd native turn) blocks the
            # asyncio loop for the litellm default 6000s before failing.
            # 300s (5 min) + tenacity retry (3x exp backoff) caps a single
            # failure burst at ~34s; even worst-case 3 failures = ~15 min.
            "timeout": 300.0,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key

        # Phase 5: explicit-cache providers (Anthropic family) need a
        # cache_control breakpoint to cache the prefix; auto-cachers
        # (DeepSeek/Minimax/OpenAI) rely on prefix stability and must NOT
        # receive a cache_control field, so this is gated to Anthropic models.
        points = self._cache_control_points()
        if points:
            kwargs["cache_control_injection_points"] = points

        # Thinking mode: inject reasoning params, remove incompatible temperature
        if self.enable_thinking:
            kwargs.pop("temperature", None)
            if self.thinking_effort:
                kwargs["reasoning_effort"] = self.thinking_effort
            extra_body = {}
            if self.provider == "minimax":
                extra_body["reasoning_split"] = True
            else:
                extra_body["thinking"] = {"type": "enabled"}
            kwargs["extra_body"] = extra_body

        kwargs.update(extra)
        return kwargs

    # ── JSON mode (existing) ─────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
        reraise=True
    )
    def generate(self, system_prompt: str, user_prompt: str,
                 is_json_mode: bool = False) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        kwargs = self._build_kwargs(messages)
        if is_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
            # Deepseek (and some others) reject json_object response_format unless
            # the prompt literally contains the word "json". Inject a hint if the
            # caller's prompts don't already mention it.
            msgs = kwargs["messages"]
            if not any("json" in str(m.get("content", "")).lower() for m in msgs):
                for m in msgs:
                    if m.get("role") == "system":
                        m["content"] = (str(m.get("content", "")).rstrip()
                                        + "\n\nRespond with valid JSON.")
                        break
                else:
                    msgs.append({"role": "system",
                                 "content": "Respond with valid JSON."})

        try:
            response = litellm.completion(**kwargs)
            self.last_usage = self._extract_usage(response)
            return response.choices[0].message.content.strip()
        except Exception as e:
            raise e

    # ── Native tool calling ──────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
        reraise=True
    )
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
        reraise=True
    )
    def generate_native(self, messages: list[dict], *,
                        tools: list[dict] | None = None,
                        tool_choice: str = "auto") -> NativeTurn:
        """Single turn with native tool calling.

        Args:
            messages: Accumulated conversation messages
                      (system + user + assistant + tool roles).
            tools: OpenAI-format tool definitions, or None for no tools.
            tool_choice: "auto", "required", "none", or a specific tool dict.

        Returns:
            NativeTurn with model text and parsed tool_calls.
        """
        kwargs = self._build_kwargs(messages)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        # Native tool calling is incompatible with JSON mode response_format
        kwargs.pop("response_format", None)

        try:
            response = litellm.completion(**kwargs)
            self.last_usage = self._extract_usage(response)
            msg = response.choices[0].message
        except Exception as e:
            raise e

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                fn = tc.function
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": fn.name,
                        "arguments": fn.arguments,
                    },
                })

        text = (msg.content or "").strip()

        # Salvage tool calls the model leaked into content as DSML markup
        # instead of returning them as structured tool_calls (DeepSeek). Without
        # this the call is silently dropped and the step later fails validation.
        if not tool_calls and text and "invoke name=" in text:
            salvaged = parse_dsml_tool_calls(text)
            if salvaged:
                tool_calls = salvaged
                text = strip_dsml_markup(text)

        return NativeTurn(
            text=text,
            tool_calls=tool_calls,
            reasoning_content=getattr(msg, "reasoning_content", "") or "",
        )
