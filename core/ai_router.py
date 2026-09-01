# core/ai_router.py
# 引入本地 Provider 注册表。通过拦截自定义前缀并强制转译为 openai/ 协议，彻底接管网关路由。
# v2: 新增 native tool calling 支持 (generate_native)。

import os
import re
import json
import uuid
from dataclasses import dataclass, field
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
import litellm


# Keyless-skip lines already printed, per (candidate, key_env).
_warned_keyless: set = set()


def _read_secret(name: str) -> str | None:
    """Resolve a secret value WITHOUT relying on the environment.

    Order: a mounted secret file (/run/secrets/<name> or
    $AITELIER_SECRETS_DIR/<name>) → then os.getenv as a local-dev fallback.
    Keeping LLM keys in a file (not env) stops every test/build subprocess that
    inherits os.environ from accidentally receiving them. (Same-uid code in this
    container can still read /run/secrets — full isolation needs a separate
    execution sandbox.)
    """
    candidates = [os.path.join("/run/secrets", name)]
    secrets_dir = os.getenv("AITELIER_SECRETS_DIR")
    if secrets_dir:
        candidates.append(os.path.join(secrets_dir, name))
    for path in candidates:
        try:
            if os.path.isfile(path):
                val = open(path, encoding="utf-8").read().strip()
                if val:
                    return val
        except OSError:
            pass
    return os.getenv(name)


from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception
)

RETRYABLE_EXCEPTIONS = (
    litellm.exceptions.RateLimitError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.Timeout,
    litellm.exceptions.APIConnectionError
)

# Errors that another endpoint serving the SAME model might not have. A token
# plan's failure modes live here: 401/403 (key dead or plan lapsed), 429 (quota
# or rate limit), 5xx, connection loss.
#
# Deliberately EXCLUDED: ContextWindowExceededError and BadRequestError. Those
# are properties of the request, not the endpoint — failing over just replays
# the same doomed call against every provider in the list, turning one clear
# error into N confusing ones and burning the quota we are trying to conserve.
FAILOVER_EXCEPTIONS = (
    litellm.exceptions.AuthenticationError,
    litellm.exceptions.PermissionDeniedError,
    litellm.exceptions.RateLimitError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.InternalServerError,
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.Timeout,
    litellm.exceptions.NotFoundError,
)
# ContextWindowExceededError is deliberately NOT here. It is not an endpoint
# outage, so walking the list blindly would replay a doomed request at every
# provider. But it is not purely request-shaped either: a prompt that overflows
# a 131k local endpoint fits the cloud candidates queued behind it. It gets its
# own narrow walk (`_failover_context`) that only advances to a BIGGER window.


def _endpoint_window(concrete: str, provs: dict | None) -> int | None:
    """Declared input-token ceiling for `provider/model`, or None if unbounded.

    Declared, never probed: the only honest source is the operator's own
    registry. A missing key means "no declared ceiling", which is treated as
    unbounded — the failure mode of guessing too small (refusing to walk to a
    candidate that would have served the request) is worse than of guessing too
    large (one wasted call that raises the same error again).
    """
    if not provs:
        return None
    prov = concrete.split("/", 1)[0]
    v = (provs.get(prov) or {}).get("max_input_tokens")
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None



# A 429 is two different failures wearing one exception class, and treating them
# alike is what killed the 2026-08-26 game run:
#
#   BURST      per-minute/per-second throttling. Clears in seconds; backing off
#              three times with a 10s cap is exactly right.
#   QUOTA      a usage window is spent ("You have exceeded the 5-hour usage
#              quota. It will reset at 2026-08-26 09:18:28 +0800 CST"). NOTHING
#              clears it but the clock. Retrying inside 20 seconds cannot
#              succeed, and every wasted attempt still spends a step retry, so
#              the run burned max_retries in 15 minutes and was marked `failed`
#              — 18 minutes before the quota came back on its own.
#
# So quota exhaustion is NOT retried here. It is raised immediately and the
# scheduler holds every tick until the reset instant (see _quota_hold).
# The burst-vs-spent-window test lives in core.llm_quota: the scheduler asks
# the same questions on a path that must not import litellm.
from core.llm_quota import is_quota_exhausted, quota_reset_at


def _retry_llm_error(exc: BaseException) -> bool:
    """tenacity predicate: retry transient provider errors, never a spent quota."""
    if not isinstance(exc, RETRYABLE_EXCEPTIONS):
        return False
    return not is_quota_exhausted(exc)


# ── Spent-window cooldown, per ENDPOINT ──────────────────────────────────────
# A spent quota is the one failure that is both certain to clear and certain not
# to clear soon, so re-electing that endpoint on the next step means paying one
# doomed call per step for hours. Skipping it until the provider's own reset
# instant costs one call, once.
#
# Keyed on the CONCRETE `provider/model`, not the provider. The blast radius of
# being wrong is asymmetric: too narrow costs at most one wasted call per model
# per window; too broad silently retires every model behind one key because a
# single one ran out. Ark bills a per-model window, so the narrow key is also
# the accurate one.
#
# In-process and unpersisted, on purpose — the same reasoning as the scheduler's
# hold: a restart during an outage costs one call to re-establish, which is
# cheaper than a durable record that can outlive the condition it describes.
_ENDPOINT_COOLDOWN: dict[str, float] = {}
_COOLDOWN_MAX_S = 6 * 3600      # never trust one report further than this
_COOLDOWN_FALLBACK_S = 300      # provider named no reset instant
# In-place retries of a burst 429 before it is allowed to cost us the preferred
# endpoint. tenacity gives 3 attempts, so 2 leaves one for the failover itself.
_BURST_TOLERANCE = 2


def endpoint_cooldowns() -> dict[str, float]:
    """Live view of `provider/model` → epoch seconds it becomes usable again."""
    import time as _t
    now = _t.time()
    return {k: v for k, v in _ENDPOINT_COOLDOWN.items() if v > now}


def reset_endpoint_cooldowns() -> None:
    """Test hook: forget every cooldown."""
    _ENDPOINT_COOLDOWN.clear()


def _endpoint_available(name: str) -> bool:
    import time as _t
    return _ENDPOINT_COOLDOWN.get(name, 0.0) <= _t.time()


def _note_endpoint_spent(name: str, err) -> float:
    """Park ONE endpoint until the provider says its window reopens."""
    import time as _t
    reset = quota_reset_at(err)
    until = (reset.timestamp() if reset is not None
             else _t.time() + _COOLDOWN_FALLBACK_S)
    # A past instant means the window already reopened — nothing to park.
    until = min(until, _t.time() + _COOLDOWN_MAX_S)
    if until <= _t.time():
        return 0.0
    _ENDPOINT_COOLDOWN[name] = max(_ENDPOINT_COOLDOWN.get(name, 0.0), until)
    return _ENDPOINT_COOLDOWN[name]


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
        truncated: finish_reason was "length" — the turn was cut off by
            max_output_tokens. On DeepSeek that cap covers reasoning AND
            visible output together, so a long chain of thought can consume
            the whole budget and leave text/tool_calls empty; without this
            flag such a turn is indistinguishable from a deliberate no-op.
    """
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    reasoning_content: str = ""
    truncated: bool = False


# Hard ceiling for output-cap escalation (see AIGateway.escalate_output_cap).
# DeepSeek documents 64K as the maximum `max_tokens`, inside a 128K context
# window that the request's prompt also has to fit in. Doubling past this turns
# a starved turn into an outright API error, which is strictly worse than the
# starve it was trying to fix — so escalation clamps here and then stops.
OUTPUT_CAP_CEILING = 65536


def _rejects_thinking_disabled(model: str) -> bool:
    """Withhold `thinking: {"type": "disabled"}` from the GLM family.

    It is not a clean family property, and this is deliberately BROADER than
    the measurement:

        ark/glm-5.3            400   (2026-08-26, TestThinkingDialect)
        ark/glm-5.3-flash      400   (2026-08-30)
        opencodego/glm-5.3…    400   (2026-08-30)
        qwen/glm-5.2           honoured (2026-08-26)

    So the true rule is per provider AND model — the "matrix that goes stale
    every time a vendor ships" that TestThinkingDialect argues against. The two
    directions of error are not symmetric: withholding it costs nothing but a
    model that keeps thinking, while sending it where it is refused is a 400,
    and a 400 is neither failed over nor retried. Withholding from the whole
    family is therefore the safe error, and it is free today — `qwen/glm-5.2`
    is the only endpoint it gives up on, and no role pairs `glm` with thinking
    off. `_complete_prebuilt`'s one-shot downgrade catches whatever this misses.

    Matched on the model string, not the provider: a provider entry is a
    (base_url, key) pair and one vendor may be registered under several names
    (an API-key pool), so the model is what identifies the family.
    """
    return "glm" in model.lower()


def resolve_agent_model(model_name: str) -> str:
    """Apply the single-model pin, if one is set, to an already-resolved model.

    Benchmark / ablation escape hatch. The production DPE mix deliberately puts
    a few roles (architect, pm, final_verifier) on a stronger model than the
    rest, which makes a benchmark number unattributable: a reviewer can always
    say the stronger model produced the win, not the harness. Setting
    AITELIER_FORCE_ALL_AGENT_MODELS forces *every* agent onto one model, so a
    harness-vs-harness comparison holds the model fixed — and running the same
    benchmark with and without it measures the Pro-vs-Flash delta instead. The
    production config stays the default; nothing is edited to run a benchmark.

    Distinct from AITELIER_HOST_AGENT_MODEL (core/agents.py), which only says
    what the skillflow "host"/"default" sentinel means: this one overrides
    explicit per-role models too. Unset (the normal case) is a no-op.
    """
    return os.getenv("AITELIER_FORCE_ALL_AGENT_MODELS") or model_name


class AIGateway:
    """
    AItelier 统一模型路由网关
    拦截本地 JSON 配置中的自定义 Provider，并自动降级为 OpenAI 兼容协议发起请求。
    """

    def __init__(self, model_name: str, config_path: str | None = None,
                 enable_thinking: bool = False, thinking_effort: str | None = None,
                 temperature: float = 0.2, max_output_tokens: int = 8192,
                 routes_path: str | None = None):
        model_name = resolve_agent_model(model_name)
        self.enable_thinking = enable_thinking
        self.thinking_effort = thinking_effort
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        # Phase 0 cache telemetry: usage of the most recent completion.
        self.last_usage: dict = {}
        # Optional liveness hook, set by the host after construction: called
        # with {"chars", "elapsed", "served_by"} every ~_PROGRESS_EVERY_S
        # while a completion streams (see _call_llm). Runs on the LLM worker
        # thread; exceptions are swallowed there.
        self.on_progress = None
        from core.model_routes import config_or_example
        self._config_path = config_path or config_or_example("llm_providers.json")

        # An agent_config may name an INTERNAL model ("flash"); resolve it to
        # the ordered list of concrete provider/model endpoints that can serve
        # it. A concrete name resolves to itself, so this is a no-op for every
        # config that has not opted in. See core/model_routes.py.
        from core.model_routes import get_routes
        self.internal_model = model_name
        self._routes_path = routes_path
        self._candidates = get_routes(routes_path).resolve(model_name, rotate=True)
        self._failovers: list[tuple[str, str]] = []   # (from_model, why)
        # Cleared for good the first time an endpoint rejects the thinking-off
        # keys — see `_apply_binding`'s else branch and `_complete_prebuilt`.
        self._suppress_thinking = True
        self._burst_hits = 0
        # Start at the first candidate whose usage window is not already known
        # to be spent, so a fresh gateway does not re-discover the same
        # exhausted plan once per step.
        self._candidate_ix = self._next_usable(0)
        self._bind(self._candidates[self._candidate_ix])

        # Process-wide litellm settings. They live at the END of __init__ so
        # every gateway re-asserts them; a refactor once orphaned them past a
        # `return` and they silently stopped running (caught in review
        # 2026-08-26). `drop_params` matters more than it looks: with it False,
        # an unrecognised TOP-LEVEL param raises UnsupportedParamsError instead
        # of being dropped — which is how the qwen `reasoning_effort` failure
        # surfaced as a loud crash. Restoring it does NOT make the extra_body
        # routing in _build_kwargs unnecessary: dropping the effort silently is
        # worse than raising, and extra_body is the only path that actually
        # delivers it.
        litellm.telemetry = False
        litellm.drop_params = True

    def _next_usable(self, start: int) -> int:
        """First candidate index >= start that is not cooling down.

        Falls back to `start` when every remaining candidate is parked. Degrading
        to "try it anyway" rather than "refuse" is deliberate: a mis-parsed reset
        timestamp must never be able to make the system unrunnable, and the
        provider is the authority on its own quota — let it answer.
        """
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                provs = json.load(f)
        except (OSError, ValueError):
            provs = None    # no registry to check against — let everything try
        first_keyed = None
        for i in range(start, len(self._candidates)):
            c = self._candidates[i]
            if provs is not None and c.split("/", 1)[0] not in provs:
                # Unregistered: handing it to litellm raises a client-side
                # BadRequestError that deliberately does NOT fail over, so the
                # gateway would die with healthy candidates still queued.
                # (provs=None — unreadable registry — falls open: let it try.)
                continue
            # Skip a candidate whose provider declares a key that has no
            # value. Before rotation this could not happen on a correctly
            # provisioned install (the FIRST candidate's key is the required
            # one); with rotation, a pool member becomes the first bound
            # endpoint on ~1/n of steps, and binding a keyless one buys a
            # guaranteed AuthenticationError + failover on every such step —
            # paid latency and trace noise for a call that cannot succeed.
            # Missing key file = "this provider is unused"; honor it here too.
            # LOUDLY: the old path failed over with a printed line and a
            # failed_over_from trace mark, and a silent skip reads as "the
            # cheap provider is fine" while the fallback quietly pays.
            if provs is not None:
                key_env = (provs.get(c.split("/", 1)[0]) or {}).get("api_key_env")
                if key_env and not _read_secret(key_env):
                    # Once per (candidate, key) per process: _next_usable runs
                    # per step + per failover, and a permanently keyless pool
                    # member would otherwise print the identical line on every
                    # step of every run — spam that drowns the failover print
                    # this is meant to match. (Keyless is never a degrade
                    # fallback either: it cannot serve.)
                    if (c, key_env) not in _warned_keyless:
                        _warned_keyless.add((c, key_env))
                        print(f"[ai_router] skip {c}: no key — create "
                              f"~/.aitelier-secrets/{key_env} to enable it")
                    continue
            if first_keyed is None:
                first_keyed = i      # registered + keyed, maybe parked
            if _endpoint_available(c):
                return i
        # Everything usable is parked (or nothing is usable). Degrade to "try
        # it anyway" — but prefer the first candidate that at least HAS a key:
        # a parked endpoint might answer (parking is prose-parsed guesswork),
        # a keyless one cannot.
        return first_keyed if first_keyed is not None else start


    def _bind(self, concrete_model: str) -> None:
        """Point this gateway at ONE concrete provider/model.

        Every provider-specific behaviour downstream (`_cache_control_points`,
        the minimax/deepseek branches in `_build_kwargs`, `_explain_auth`) reads
        the attributes set here, so re-binding mid-life switches all of them
        together and `_build_kwargs` needs no failover awareness.
        """
        self.active_model = concrete_model
        self._burst_hits = 0        # a new endpoint starts with a clean count
        self.api_base = None
        self.api_key = None
        # Set to the key's NAME when a provider wants one and it is
        # absent, so the failure can say what to configure.
        self.missing_key_env = None
        self.litellm_model = concrete_model
        self.provider = None

        # 读取本地 Provider 注册表
        if os.path.exists(self._config_path) and '/' in concrete_model:
            provider, actual_model = concrete_model.split('/', 1)
            self.provider = provider

            with open(self._config_path, "r", encoding="utf-8") as f:
                providers = json.load(f)

            if provider in providers:
                cfg = providers[provider]
                self.api_base = cfg.get("base_url")

                # 动态提取环境变量中的 API Key
                key_env = cfg.get("api_key_env")
                if key_env:
                    self.api_key = _read_secret(key_env)
                    # Remember WHICH key was wanted. Without this, a missing key
                    # surfaces as the provider's own 401 several layers away —
                    # a true sentence about authentication that never names the
                    # secret file the deployment forgot to create.
                    self.missing_key_env = None if self.api_key else key_env

                # Use LiteLLM's native provider when available (minimax, etc.).
                try:
                    _, native_provider, _, _ = litellm.get_llm_provider(concrete_model)
                    if native_provider and native_provider != "openai":
                        self.litellm_model = concrete_model
                    else:
                        self.litellm_model = f"openai/{actual_model}"
                except Exception:
                    self.litellm_model = f"openai/{actual_model}"

    # ── cache telemetry ──────────────────────────────────────────────

    @staticmethod
    def _extract_usage(response) -> dict:
        """Pull token + prompt-cache stats from a completion response.

        Normalizes across providers:
          - DeepSeek: usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens
          - OpenAI-style: usage.prompt_tokens_details.cached_tokens
          - neither (Ollama Cloud): cache fields are None = UNKNOWN, not zero
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
            # completion_tokens INCLUDES reasoning_tokens on DeepSeek (verified
            # against the live API: a capped turn reports completion_tokens ==
            # max_tokens == reasoning_tokens with empty content). Recording the
            # split is what makes "reasoning ate the whole budget" visible in a
            # trace instead of looking like an ordinary short answer.
            completion_details = getattr(usage, "completion_tokens_details", None)
            reasoning_tokens = (_num(getattr(completion_details, "reasoning_tokens", None))
                                if completion_details else None)

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

            if hit is None and miss is None:
                # The provider said NOTHING about caching. `hit = 0, miss =
                # prompt_tokens` would record that as "the entire prompt missed
                # cache" — a positive claim, and a measurably false one: on
                # 2026-09-01 Ollama Cloud (ollamacloud/* in model_routes) was
                # observed returning a usage object holding only prompt/
                # completion/total tokens — no details object under any name —
                # while a controlled warm/cold latency A/B showed it genuinely
                # does prefix-cache (~2x prefill speedup). Silence is UNKNOWN,
                # so it is recorded as None: an unknown turn must stay out of
                # BOTH sides of every aggregate ratio rather than being summed
                # in as a miss and dragging the ratio down. None is the
                # vocabulary the aggregation sites already use for an undefined
                # ratio, so it carries all the way to the UI.
                hit_ratio = None
            else:
                hit = hit or 0
                miss = miss if miss is not None else (prompt_tokens - hit)
                miss = max(miss, 0)
                hit_ratio = round((hit / prompt_tokens) if prompt_tokens else 0.0, 4)
            out = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_hit_tokens": hit,
                "cache_miss_tokens": miss,
                "hit_ratio": hit_ratio,
            }
            if reasoning_tokens is not None:
                out["reasoning_tokens"] = reasoning_tokens
            return out
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

    def escalate_output_cap(self) -> int | None:
        """Double this gateway's output cap, clamped to OUTPUT_CAP_CEILING.

        Called when a turn was truncated having emitted only reasoning. On
        DeepSeek `max_tokens` bounds reasoning and visible output together, so
        such a turn did not *choose* to stay silent — it ran out of room before
        it could speak. Reissuing it unchanged necessarily reproduces the same
        outcome, and the cap is the single setting that caused it, so raising
        the cap is what makes the retry a genuinely different call.

        Returns the new cap, or None when already at the ceiling — the caller
        then knows it has no escalation left and should stop expecting the
        retry to behave differently.
        """
        if self.max_output_tokens >= OUTPUT_CAP_CEILING:
            return None
        self.max_output_tokens = min(self.max_output_tokens * 2,
                                     OUTPUT_CAP_CEILING)
        return self.max_output_tokens

    # ── Sticky failover across the internal model's candidates ───────

    # A TOTAL-CALL budget, deliberately NOT derived from the per-request
    # `timeout` in kwargs: the two measure different things. That timeout is
    # httpx's READ timeout — the maximum GAP between received bytes — and it
    # works; measured against a socket that sends a partial body and then goes
    # quiet, `timeout=3.0` raises litellm.Timeout at exactly 3.0s on the same
    # deepseek/custom-httpx path that hung. What it cannot catch is a response
    # that keeps trickling: every byte resets the gap, so a call can run
    # forever without ever being silent for 300s.
    #
    # Sized from measured healthy calls on this deployment, not guessed:
    # consecutive completions inside a step land 4-6s apart, and the slowest
    # observed were 90-102s. 900s is ~9x the worst healthy call, so this cannot
    # fire on "slow"; it fires on "never finishes".
    _WALL_CAP_S = float(os.getenv("AITELIER_LLM_WALL_CAP_S") or 900.0)

    # Seconds between on_progress ticks while a completion streams.
    _PROGRESS_EVERY_S = 3.0

    @staticmethod
    def _chunk_len(chunk) -> int:
        """Characters this stream chunk contributed (content + reasoning +
        tool-call arguments) — the liveness signal, not a token count."""
        try:
            delta = chunk.choices[0].delta
        except (AttributeError, IndexError):
            return 0
        n = (len(getattr(delta, "content", None) or "")
             + len(getattr(delta, "reasoning_content", None) or ""))
        for tc in (getattr(delta, "tool_calls", None) or []):
            fn = getattr(tc, "function", None)
            if fn is not None:
                n += len(getattr(fn, "arguments", None) or "")
        return n

    def _call_llm(self, kwargs: dict):
        """The provider call itself, on the bounded worker thread.

        Streams by default — not to show tokens, but because chunk arrival is
        the only signal that can distinguish a long completion from a wedged
        one from OUTSIDE the call (the 2026-08-27 trickle hang was invisible
        precisely because nothing measured arrival). Chunks are re-assembled
        into the exact non-streaming response shape — verified live against
        ark / qwen / opencodego: content, tool_calls (parseable args),
        reasoning_content, usage incl. cache fields all survive
        stream_chunk_builder — so nothing downstream can tell the difference.

        `on_progress` (optional, set by the host after construction) gets a
        small dict every ~_PROGRESS_EVERY_S while chunks arrive. It runs on
        this worker thread and must never break the call — exceptions are
        swallowed. AITELIER_LLM_STREAM=0 reverts to the plain call.
        """
        if os.getenv("AITELIER_LLM_STREAM", "1") == "0" or kwargs.get("stream"):
            return litellm.completion(**kwargs)
        import time as _t
        skwargs = dict(kwargs)
        skwargs["stream"] = True
        skwargs["stream_options"] = {"include_usage": True}
        chunks: list = []
        chars = 0
        t0 = _t.monotonic()
        next_note = 0.0     # first chunk ticks immediately → "it's alive"
        if self.on_progress is not None:
            # Dispatch announcement: between here and the first chunk the
            # server is queueing + prefilling (26:1 prefill:decode on this
            # workload — tens of seconds on a cache miss), and without this
            # tick that whole window shows as unexplained blank.
            try:
                self.on_progress({"phase": "llm_start",
                                  "served_by": self.active_model})
            except Exception:
                pass
        for chunk in litellm.completion(**skwargs):
            chunks.append(chunk)
            chars += self._chunk_len(chunk)
            now = _t.monotonic()
            if self.on_progress is not None and now >= next_note:
                next_note = now + self._PROGRESS_EVERY_S
                try:
                    self.on_progress({"phase": "llm", "chars": chars,
                                      "elapsed": round(now - t0, 1),
                                      "served_by": self.active_model})
                except Exception:
                    pass
        if self.on_progress is not None:
            # The stream ended — tell watchers to clear the line NOW instead
            # of letting a stale "generating" linger through tool execution
            # until the client-side expiry.
            try:
                self.on_progress({"phase": "llm_done", "chars": chars,
                                  "elapsed": round(_t.monotonic() - t0, 1),
                                  "served_by": self.active_model})
            except Exception:
                pass
        if not chunks:
            # A stream that opened and closed with zero chunks is an endpoint
            # failure — route it like one (APIConnectionError is in
            # FAILOVER_EXCEPTIONS) instead of handing the builder nothing.
            raise litellm.exceptions.APIConnectionError(
                message="stream closed before the first chunk",
                llm_provider=str(self.provider or ""),
                model=str(self.active_model or ""),
            )
        return litellm.stream_chunk_builder(chunks,
                                            messages=kwargs.get("messages"))

    def _completion_bounded(self, kwargs: dict):
        """`litellm.completion` under a TOTAL-time cap, which its timeout is not.

        Measured 2026-08-27 on run 8305b1e3 (jinyong-aim, step 2): the step
        produced no trace for 35 minutes and py-spy caught the worker parked at

            read (ssl.py) <- _receive_response_body (httpcore/_sync/http11.py)
            <- post (litellm .../http_handler.py) <- _complete_deepseek
            <- _complete_prebuilt (core/ai_router.py) <- turn (core/agents.py)

        The obvious reading — "the 300s timeout is broken" — is WRONG, and the
        experiment says so: against a socket that sends a partial body and then
        goes quiet, this same deepseek/custom-httpx path raises litellm.Timeout
        at exactly `timeout` (3.0s for timeout=3.0). The provider log agrees
        from the other side: exactly ONE litellm.completion was dispatched in
        those 35 minutes. A fired timeout would have failed over and dispatched
        a second one.

        Both facts hold together only one way: bytes kept arriving. `timeout` is
        httpx's READ timeout — the maximum GAP between bytes — so a response
        that trickles resets it forever. The call was not silent; it was
        useless. That distinction matters, and it is why this cap is on TOTAL
        time and is not derived from `timeout`.

        Raises `litellm.Timeout`, which is already in FAILOVER_EXCEPTIONS, so a
        trickling endpoint fails over to the next candidate exactly like a
        refused connection does — no new error path to route.

        The abandoned thread stays blocked on the socket until the peer or the
        OS closes it. That is the price and it is deliberate: an orphaned thread
        costs one file descriptor and is bounded by the retry count; an
        unbounded wait costs the whole run.
        """
        cap = self._WALL_CAP_S
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-call")
        try:
            future = pool.submit(self._call_llm, kwargs)
            try:
                return future.result(timeout=cap)
            except _FutureTimeout as e:
                raise litellm.exceptions.Timeout(
                    message=(
                        f"No completion after {cap:.0f}s of wall clock. The "
                        f"request carried timeout={kwargs.get('timeout')}, which "
                        f"is the READ timeout (max gap between bytes) and cannot "
                        f"catch a response that trickles without ever going "
                        f"quiet. Abandoned; failing over."),
                    model=str(self.active_model or kwargs.get("model") or ""),
                    llm_provider="aitelier-deadline",
                ) from e
        finally:
            # NEVER wait: the point of the cap is not to block on the thread we
            # just gave up on. `with ThreadPoolExecutor(...)` would join it and
            # reproduce the exact hang this method exists to break.
            pool.shutdown(wait=False)

    def _complete_prebuilt(self, kwargs: dict):
        """One completion, walking this model's candidates on endpoint errors.

        STICKY, never round-robin. Once a candidate answers, the gateway stays
        bound to it — and a gateway lives for one step (AgentFactory builds it
        per `create_agent`), so every turn of a multi-turn step hits the same
        endpoint. Two reasons, both load-bearing:

        1. Prefix caches are per-provider. Measured over this repo's whole trace
           history: 464M input tokens at an 89.4% cache-hit rate, against 18M
           output — a 26:1 prefill:decode workload. Alternating providers turns
           cached input into full-price input; at a 50% hit rate the full-price
           volume goes from 49M to ~232M. No per-token plan discount survives
           that, so spreading quota must happen at a granularity COARSER than a
           step (per run / per project), never per call.
        2. DeepSeek requires the assistant's `reasoning_content` to be replayed
           in every subsequent turn once `tools` is present, or the API returns
           400. Mid-step provider changes cannot guarantee that field survives
           in a form the new endpoint accepts.

        Raises the LAST error once the candidates are exhausted, so the caller
        still sees a real provider error (and `_explain_auth` can still name the
        missing secret).
        """
        while True:
            try:
                response = self._completion_bounded(kwargs)
            except FAILOVER_EXCEPTIONS as e:
                if not self._failover(e):
                    # Name the endpoints that would have to reopen. The escaping
                    # error is the LAST candidate's, and the scheduler parks on
                    # it — but "when can this model work again" is the EARLIEST
                    # of the whole list, and the scheduler cannot know the list
                    # from an exception alone. Without this it took the minimum
                    # over every parked endpoint in the process, including ones
                    # belonging to models this project never calls: a 5-minute
                    # window on the vision judge would cut a 5-hour hold on
                    # flash, and the run would burn its retries waking into a
                    # still-spent plan.
                    # Stamped on the object actually RAISED: _explain_auth
                    # may wrap into a new exception, and an attribute set on the
                    # original would not survive that.
                    exc = self._explain_auth(e)
                    exc._aitelier_candidates = list(self._candidates)
                    raise exc from e
                self._apply_binding(kwargs)
                continue
            except litellm.exceptions.ContextWindowExceededError as e:
                # Not an outage and not purely request-shaped — see
                # _failover_context. Walk only to a bigger declared window;
                # when there is none, this is a real request-shaped failure and
                # raises like any other.
                if not self._failover_context(
                        f"{type(e).__name__}: {str(e)[:200]}"):
                    raise self._explain_auth(e) from e
                self._apply_binding(kwargs)
                continue
            except Exception as e:
                # Request-shaped failure (bad params, unsupported args): every
                # candidate would reject it identically.
                #
                # Unless WE put the bad param there. The thinking-off keys are
                # vendor extensions, and an endpoint that refuses one answers
                # 400 — which is in neither FAILOVER_EXCEPTIONS nor
                # RETRYABLE_EXCEPTIONS, so it would kill the step outright.
                # Measured 2026-08-30 no endpoint on any route rejects the pair
                # this sends, but the rotation head of four routes (qwen/*)
                # could not be measured — its plan answers 429 until 09-02 — and
                # suppressing thinking is an OPTIMISATION. It is never worth a
                # dead step, so drop it and let that endpoint think: slow beats
                # dead, the same trade the vision gate made for itself before
                # the gateway took this over. Once per gateway, and only for a
                # role that asked for thinking off.
                if self._suppress_thinking and not self.enable_thinking:
                    self._suppress_thinking = False
                    print(f"[ai_router] {self.active_model} rejected the "
                          f"thinking-off keys ({type(e).__name__}); retrying "
                          f"once without them: {str(e)[:160]}", flush=True)
                    kwargs.pop("extra_body", None)
                    self._apply_binding(kwargs)
                    continue
                raise self._explain_auth(e) from e
            # Stamp WHICH endpoint answered onto the usage record. That record
            # is traced per turn (dpe_pipeline: category="usage"), and without
            # this a run's cost cannot be attributed: after a failover every
            # token in the trace looks identical to one served by the preferred
            # endpoint, so "which plan did this run actually spend" has no
            # answer. Only when there IS usage, so the existing `if usage:`
            # guard keeps behaving the same.
            usage = self._extract_usage(response)
            if usage:
                usage["served_by"] = self.active_model
                if self.internal_model != self.active_model:
                    usage["model_route"] = self.internal_model
                if self._failovers:
                    usage["failed_over_from"] = [f for f, _ in self._failovers]
            self.last_usage = usage
            # Pre-emptive, on the SUCCESSFUL path: the next turn's prompt is
            # this one plus the answer plus a tool result, so "it fit this
            # time" says nothing about the next.
            self._rebind_if_out_of_headroom((usage or {}).get("prompt_tokens"))
            # A burst tolerance that never resets is a lifetime counter, not a
            # "did it persist" one: two unrelated blips seventeen turns apart in
            # one step would have spent it, and the second failed over with no
            # in-place retry at all. Success is what makes the previous throttle
            # historical.
            self._burst_hits = 0
            return response

    def _failover(self, exc: Exception) -> bool:
        """Advance to the next usable candidate. False when none is left.

        A SPENT WINDOW is remembered, a transient error is not. `is_quota_exhausted`
        distinguishes them — both arrive as RateLimitError and only the prose
        says which — because the two want opposite handling: burst throttling
        clears in seconds and the endpoint should stay in rotation, while a
        5-hour window will reject every call until the clock says otherwise.
        """
        failed = self.active_model

        # A BURST 429 clears in seconds; a spent window does not. Both arrive as
        # RateLimitError and only the prose separates them. Failing over on a
        # burst is expensive twice: the binding is sticky for the whole step, so
        # every remaining turn abandons the per-provider prefix cache (26:1
        # prefill:decode at an 89.4% hit rate), and on the glm / smart / vision
        # routes the next candidate is a DIFFERENT MODEL — a transient blip
        # would silently downgrade the step's judge. So retry in place first and
        # only fail over if the throttle persists; tenacity owns the backoff
        # (`_retry_llm_error` deliberately keeps burst 429s retryable).
        if (isinstance(exc, litellm.exceptions.RateLimitError)
                and not is_quota_exhausted(exc)):
            self._burst_hits += 1
            if self._burst_hits < _BURST_TOLERANCE:
                raise exc
        held = ""
        if is_quota_exhausted(exc):
            import time as _t
            until = _note_endpoint_spent(failed, exc)
            if until:
                held = f", parked {until - _t.time():.0f}s"

        # Never walk back onto an endpoint this gateway has already failed on.
        # A route may name the same `provider/model` twice on purpose — the
        # duplicate `localqwen/qwen3` in `flash.rotate` is what gives the local
        # box a 2-in-5 share of the rotation head — and the walk is a single
        # forward pass over that list, so a duplicate got tried twice. Measured
        # 2026-08-30 while the box was down for a benchmark:
        #
        #   failover flash: localqwen/qwen3 -> qwen/qwen3.8-flash  (MidStream…)
        #   failover flash: qwen/qwen3.8-flash -> localqwen/qwen3  (RateLimit, parked)
        #   failover flash: localqwen/qwen3 -> opencodego/…-flash  (InternalServer…)
        #
        # Three hops to reach a live endpoint, the middle one certain to fail.
        # Parking does not cover this: it needs `is_quota_exhausted`, and a box
        # that is simply DOWN is not quota-exhausted. Nor is this the transient
        # case — tenacity has already retried, and a burst 429 is retried in
        # place above — so by the time we are here, that endpoint is spent for
        # this call. Skipping keeps the rotation share and drops the wasted call.
        tried = {f for f, _ in self._failovers}
        tried.add(failed)
        nxt_ix = self._next_usable(self._candidate_ix + 1)
        while (self._candidate_ix < nxt_ix < len(self._candidates)
               and self._candidates[nxt_ix] in tried):
            nxt_ix = self._next_usable(nxt_ix + 1)
        if nxt_ix <= self._candidate_ix or nxt_ix >= len(self._candidates):
            return False
        self._candidate_ix = nxt_ix
        nxt = self._candidates[nxt_ix]
        self._failovers.append((failed, f"{type(exc).__name__}: {str(exc)[:200]}"))
        self._bind(nxt)
        # Loud on purpose. A silent failover reads as "the cheap provider is
        # fine" while every run is quietly served by the expensive fallback.
        print(f"[ai_router] failover {self.internal_model}: {failed} -> {nxt} "
              f"({type(exc).__name__}{held})", flush=True)
        return True

    def _failover_context(self, why: str) -> bool:
        """Advance to a candidate whose declared window is BIGGER. Else False.

        An overflow on a small endpoint is a property of the ENDPOINT, not of
        the request: the local llama.cpp candidate serves 131k while the cloud
        candidates behind it take far more, and 1.35% of measured flash calls
        are longer than 131k. Hard-failing those steps on the one endpoint that
        cannot serve them — with usable candidates still queued — is the bug
        this exists to prevent.

        Only bigger windows, never merely "the next one": an equal-or-smaller
        candidate would reject the same prompt, so walking onto it converts one
        clear error into N and spends quota proving what the registry already
        said. A candidate with NO declared ceiling counts as bigger.
        """
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                provs = json.load(f)
        except (OSError, ValueError):
            return False        # no registry to compare against — do not guess
        here = _endpoint_window(self.active_model, provs)
        failed = self.active_model
        for i in range(self._candidate_ix + 1, len(self._candidates)):
            w = _endpoint_window(self._candidates[i], provs)
            if here is not None and w is not None and w <= here:
                continue
            if here is None:
                # Already on an endpoint with no declared ceiling: nothing in
                # the registry can be shown to be bigger, so stop rather than
                # replay the prompt at a candidate that may be smaller.
                return False
            self._candidate_ix = i
            nxt = self._candidates[i]
            self._failovers.append((failed, why))
            self._bind(nxt)
            print(f"[ai_router] context-failover {self.internal_model}: "
                  f"{failed} ({here}) -> {nxt} ({w or 'unbounded'})  {why}",
                  flush=True)
            return True
        return False

    def _rebind_if_out_of_headroom(self, prompt_tokens: int | None) -> None:
        """Move to a bigger endpoint BEFORE the next turn is squeezed.

        The wall is not the overflow, it is the asymptote. llama.cpp counts only
        the PROMPT against its context — measured: the same request reports the
        same total whether max_tokens is 512 or 8192 — so a prompt that fits is
        ACCEPTED and the generation is silently clamped to whatever room is
        left. Measured on a t_impl step: turn 17 returned at prompt 130,940 of a
        131,072 window and produced 131 tokens; the next attempt got 51. No
        error ever fired. The agent could neither call a tool nor finish, and
        the step was re-queued after 846s of discarded work.

        So the trigger is HEADROOM, not overflow. Of measured t_impl steps,
        22.1% reach a peak prompt that leaves less than their 32,768-token
        output budget, and only 9.1% actually exceed the window: waiting for
        ContextWindowExceededError would miss the larger half of the problem.

        Never fails the call that just succeeded — this only re-points the
        binding, and `_build_kwargs` applies it on the next turn.
        """
        if not prompt_tokens or not self.max_output_tokens:
            return
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                provs = json.load(f)
        except (OSError, ValueError):
            return          # no registry to compare against — do not guess
        window = _endpoint_window(self.active_model, provs)
        if window is None:
            return          # undeclared ceiling: nothing to be out of
        left = window - prompt_tokens
        if left >= self.max_output_tokens:
            return
        self._failover_context(
            f"headroom {left} < max_output_tokens {self.max_output_tokens}")

    def _apply_binding(self, kwargs: dict) -> dict:
        """(Re)write the keys that depend on WHICH endpoint is bound.

        Split out of `_build_kwargs` so a failover can re-target an
        already-assembled kwargs dict — the caller's own mutations (JSON-mode
        system nudge, `tools`, the `response_format` pop) are preserved, while
        model / credentials / provider quirks follow the new binding. Every key
        it can set, it also CLEARS when the new provider does not want it:
        leaving the previous provider's `api_key` or `cache_control_*` behind is
        how a failover turns one endpoint's outage into a second endpoint's 400.
        """
        kwargs["model"] = self.litellm_model
        for k in ("api_base", "api_key", "cache_control_injection_points",
                  "reasoning_effort", "extra_body"):
            kwargs.pop(k, None)
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
            extra_body = {}
            if self.provider == "minimax":
                extra_body["reasoning_split"] = True
            else:
                extra_body["thinking"] = {"type": "enabled"}
            # The route table may state the effort FOR THIS ENDPOINT, and it
            # wins over the role's: the role names one string for an internal
            # model that now spans endpoints whose vocabularies do not overlap.
            # DeepSeek takes low/high/max; Qwen3.8's chat template takes
            # low/medium/xhigh and RAISES on anything else — `max` comes back a
            # 500 ("Unexpected reasoning effort max"), measured on
            # localqwen/qwen3. Resolved HERE rather than at construction because
            # a failover rebinds mid-step, and the new endpoint may want a
            # different string for the same intent.
            from core.model_routes import get_routes
            effort = self.thinking_effort
            try:
                per_endpoint = get_routes(self._routes_path).effort_for(
                    self.internal_model or "", self.active_model or "")
            except (RuntimeError, OSError, ValueError):
                # An unreadable or malformed TABLE must not break the call —
                # the role's value stands and the request still goes out.
                # Deliberately NOT `except Exception`: the first draft of this
                # caught everything, and the local import of get_routes lives
                # in __init__, so line 948 raised NameError on every call and
                # the swallow turned it into "the route never declares an
                # effort". The tests failed with no error to read.
                per_endpoint = None
            if per_endpoint:
                effort = per_endpoint
            if effort:
                # ALWAYS through extra_body, never as a top-level param.
                #
                # Two independent reasons, one per provider family:
                #   * DeepSeek — litellm's DeepSeekChatConfig pops
                #     `reasoning_effort` and maps it to a bare
                #     `thinking: {"type": "enabled"}`, dropping the LEVEL
                #     (BerriAI/litellm#27439), so every effort silently
                #     collapses to DeepSeek's default `high`.
                #   * everyone else reached through the openai/ shim — litellm
                #     VALIDATES top-level params against what it believes the
                #     model supports, and it believes nothing about a model it
                #     has never heard of. Measured 2026-08-26: every single
                #     effort value on `qwen/qwen3.8-max` raised
                #     UnsupportedParamsError("openai does not support
                #     parameters: ['reasoning_effort']") before a request was
                #     even sent. That is a CLIENT-side error, so it is not in
                #     FAILOVER_EXCEPTIONS and not a provider fault — it would
                #     simply have killed the step, and it would have appeared
                #     the moment a role with an effort set was routed onto a
                #     non-DeepSeek model.
                #
                # extra_body is forwarded verbatim, which sidesteps both.
                extra_body["reasoning_effort"] = effort
            kwargs["extra_body"] = extra_body
        else:
            # `enable_thinking=False` used to send NOTHING, and nothing is not
            # the same as "do not think". A chat template that reasons by
            # DEFAULT then does the opposite of what the role asked for:
            # Qwen3.8's defaults to xhigh, so `compacter` — whose whole job is
            # to shrink a transcript — reasoned at the highest setting on the
            # 2-in-5 `flash` steps that bind localqwen. The effort table cannot
            # fix that either: it is read inside the branch above, so a role
            # with thinking off never reaches it.
            #
            # It takes BOTH keys because neither works everywhere. Measured
            # 2026-08-30, reasoning tokens on a fixed one-line prompt:
            #
            #   endpoint                    bare   ctk   thinking:disabled
            #   localqwen/qwen3               22     0     22  (ignored)
            #   ark|deepseek/…-v4-flash    12/21  13/27      0
            #   ark|deepseek/…-v4-pro      23/23  21/10      0
            #   opencodego/…-v4-flash         24    26     12  (reseller; partial)
            #   opencodego|ark/glm-5.3…    80/29  29/29    400  <-- rejects it
            #
            # `chat_template_kwargs` is silently ignored wherever it is not
            # understood — no 400 on any of the nine endpoints — so it is
            # unconditional. `thinking` is the same key the enable branch
            # above sends, and GLM accepts `enabled` while rejecting
            # `disabled`; that asymmetry is a vendor bug with no portable way
            # around it, so it gets the one guard below rather than a table of
            # who-gets-what.
            if self._suppress_thinking:
                extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
                if not _rejects_thinking_disabled(self.active_model or ""):
                    extra_body["thinking"] = {"type": "disabled"}
                kwargs["extra_body"] = extra_body
        return kwargs

    def _build_kwargs(self, messages: list[dict], **extra) -> dict:
        """Build litellm completion kwargs from state + extra."""
        kwargs = {
            "messages": self._sanitize_messages(messages),
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            # A2 fix: bound LiteLLM call. Without this, a stalled provider
            # (e.g. deepseek v4-flash hung on 2nd native turn) blocks the
            # asyncio loop for the litellm default 6000s before failing.
            # 300s (5 min) + tenacity retry (3x exp backoff) caps a single
            # failure burst at ~34s; even worst-case 3 failures = ~15 min.
            "timeout": 300.0,
        }
        self._apply_binding(kwargs)
        kwargs.update(extra)
        return kwargs

    # ── JSON mode (existing) ─────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception(_retry_llm_error),
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

        response = self._complete_prebuilt(kwargs)   # sets self.last_usage
        return response.choices[0].message.content.strip()

    def _explain_auth(self, e: Exception) -> Exception:
        """Prefix a provider AUTH failure with the secret it wanted.

        Only auth failures, and only when this provider's key is genuinely
        absent. The key being None is NOT itself an error — the self-hosted vLLM
        serves without one — so the naming rides on the failure that actually
        happened rather than pre-empting a call that may well succeed.
        """
        if not self.missing_key_env:
            return e
        auth = (litellm.exceptions.AuthenticationError,
                getattr(litellm.exceptions, "PermissionDeniedError", ()))
        if not isinstance(e, auth) and "401" not in str(e):
            return e
        # Name the CONFIG LINE the operator would edit, not just the endpoint
        # that happened to answer. After routing, "provider 'localqwen'" leaves
        # them hunting: nothing in agent_configs says `localqwen` — it says
        # `vision`, and the candidate list in model_routes.json is what put
        # them there. An error that names the resolved endpoint but not the
        # entry that chose it is the exact shape this repo keeps fixing.
        from core.external_deps import missing
        where = (f"Model '{self.litellm_model}' resolves to provider "
                 f"'{self.provider}', which reads it.")
        if self.internal_model != self.active_model:
            others = [c for c in self._candidates if c != self.active_model]
            where += (f"\n\nIt got here from model_routes.json: the internal "
                      f"model '{self.internal_model}' lists "
                      f"{self._candidates}, and candidate "
                      f"{self._candidate_ix + 1} of {len(self._candidates)} "
                      f"('{self.active_model}') is the one being tried"
                      + (f". Remaining: {others}." if others else "."))
        note = missing(self.missing_key_env, where)
        return type(e)(f"{note}\n\nProvider said: {e}") if isinstance(
            e, RuntimeError) else RuntimeError(f"{note}\n\nProvider said: {e}")

    # ── Native tool calling ──────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception(_retry_llm_error),
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

        response = self._complete_prebuilt(kwargs)   # sets self.last_usage
        choice = response.choices[0]
        msg = choice.message
        finish_reason = getattr(choice, "finish_reason", "") or ""

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
            truncated=(finish_reason == "length"),
        )
