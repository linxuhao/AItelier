# tests/unit/test_ai_router_outgoing_body_bound_20261118.py
"""The outgoing request body is measured and bounded before anything is sent.

Measured occurrence, 2026-11-18T11:49:47Z: this backend forwarded a completion
whose provider payload the upstream gateway refused with

    [invalid_request_error] decoded body exceeds 50331648 bytes

The exception reached the driver shaped like a provider 400 — no byte count, no
limit, no culprit, no identity — and the turn died with no result. These tests
pin the two halves that were missing: a local refusal BEFORE dispatch, and a
refusal that says how big the body was, what the bound is, and which part is
worth dropping.

The bound is injectable (`core.ai_router.OUTGOING_BODY_BOUND_BYTES`) precisely
so these tests exercise the boundary with kilobyte payloads instead of building
48 MiB.
"""
import json

import litellm
import pytest
from unittest.mock import MagicMock, patch

from core import ai_router
from core.ai_router import (
    AIGateway,
    FAILOVER_EXCEPTIONS,
    GATEWAY_BODY_LIMIT_BYTES,
    OUTGOING_BODY_BOUND_BYTES,
    OutgoingBodyTooLarge,
    _retry_llm_error,
    measure_outgoing_body,
)


@pytest.fixture(autouse=True)
def _plain_transport(monkeypatch):
    """Pin the non-streaming transport: these tests count provider calls, and
    the streaming path has its own suite (test_llm_stream_transport.py)."""
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")


def _ok_response(text: str = "done"):
    response = MagicMock()
    response.choices[0].message.content = text
    response.choices[0].finish_reason = "stop"
    response.usage = None
    return response


def _pre_change_kwargs(gateway: AIGateway, messages: list[dict],
                       tools: list[dict] | None = None) -> dict:
    """The kwargs the PRE-change code handed to litellm for this same call.

    Built by a second gateway on the same model: `_build_kwargs` + the
    per-entry additions in `generate` / `generate_native` are exactly the
    assembly that existed before the guard, so equality against a captured call
    is a check that the guard measured the body without touching it.
    """
    kwargs = gateway._build_kwargs(list(messages))
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    kwargs.pop("response_format", None)
    return kwargs


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


TOOLS = [{
    "type": "function",
    "function": {
        "name": "bash",
        "description": "run a shell command",
        "parameters": {"type": "object",
                       "properties": {"cmd": {"type": "string"}}},
    },
}]


# ── the declared numbers ─────────────────────────────────────────────────

def test_the_bound_leaves_real_margin_under_the_measured_gateway_limit():
    """The upstream ceiling is the number from the 11:49:47Z refusal.

    Pinned literally: the gateway said 50331648, and a bound derived from a
    different constant would silently stop describing that occurrence.
    """
    assert GATEWAY_BODY_LIMIT_BYTES == 50331648
    assert OUTGOING_BODY_BOUND_BYTES < GATEWAY_BODY_LIMIT_BYTES
    margin = GATEWAY_BODY_LIMIT_BYTES - OUTGOING_BODY_BOUND_BYTES
    # A margin that is a rounding error is not a margin: the measurement cannot
    # see the transport's own punctuation or the keys a failover rewrites.
    assert margin >= 1024 * 1024


# ── the refusal ──────────────────────────────────────────────────────────

def test_oversized_payload_is_refused_before_any_provider_call(monkeypatch):
    """A payload over the bound never reaches litellm, and the error says why."""
    bound = 64_000
    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", bound)

    gateway = AIGateway("deepseek/deepseek-v4-flash")
    events: list[tuple[str, dict]] = []
    gateway.on_completion = lambda event, payload: events.append((event, payload))

    spilled = "x" * 200_000          # a spilled-file read, verbatim
    messages = [
        {"role": "system", "content": "you are a driver"},
        {"role": "user", "content": "read the log"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "bash",
                                      "arguments": '{"cmd": "cat log"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": spilled},
    ]

    def explode(**kwargs):
        raise AssertionError("a refused payload must not reach the provider")

    with patch("litellm.completion", side_effect=explode) as spy:
        with pytest.raises(OutgoingBodyTooLarge) as caught:
            gateway.generate_native(messages, tools=TOOLS)
        assert spy.call_count == 0

    error = caught.value
    # (a) the measured byte count, (b) the bound, (c) the largest part.
    assert error.measured_bytes > bound
    assert error.bound_bytes == bound
    assert error.gateway_limit_bytes == GATEWAY_BODY_LIMIT_BYTES
    assert error.largest["part"] == "messages[3]"
    assert error.largest["detail"] == "role=tool"
    assert error.largest["bytes"] == len(json.dumps(
        messages[3], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    # (d) the identity this layer knows.
    assert error.identity["model_route"] == "deepseek/deepseek-v4-flash"
    assert error.identity["endpoint"] == gateway.active_model
    assert error.identity["session"] == gateway._opencode_session_id
    assert error.identity["messages"] == len(messages)

    text = str(error)
    assert f"{error.measured_bytes:,} bytes" in text
    assert f"{bound:,} bytes" in text
    assert f"{GATEWAY_BODY_LIMIT_BYTES:,}" in text
    assert "messages[3]" in text and "role=tool" in text
    # It must read as THIS backend's limit, with no upstream exception prose.
    assert "AItelier refused this call before sending it" in text
    assert "Nothing was sent upstream" in text
    assert "invalid_request_error" not in text
    assert "Upstream request failed" not in text
    # Nothing below the funnel was entered: no invocation was observed.
    assert events == []


def test_the_refusal_is_not_routeable_as_a_provider_error(monkeypatch):
    """No failover, no retry, no parked endpoint: the body is the caller's."""
    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", 4_000)
    gateway = AIGateway("deepseek/deepseek-v4-flash")
    with patch("litellm.completion", side_effect=AssertionError("sent")):
        with pytest.raises(OutgoingBodyTooLarge) as caught:
            gateway.generate("sys", "y" * 10_000)
    error = caught.value
    assert not isinstance(error, FAILOVER_EXCEPTIONS)
    assert not isinstance(error, litellm.exceptions.APIError)
    assert _retry_llm_error(error) is False
    assert gateway._failovers == []


def test_json_mode_entry_is_guarded_too(monkeypatch):
    """`generate` is the other public door into the funnel; both are covered."""
    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", 8_000)
    gateway = AIGateway("deepseek/deepseek-v4-flash")
    with patch("litellm.completion", side_effect=AssertionError("sent")) as spy:
        with pytest.raises(OutgoingBodyTooLarge) as caught:
            gateway.generate("sys", "z" * 20_000, is_json_mode=True)
        assert spy.call_count == 0
    assert caught.value.largest["part"] == "messages[1]"
    assert caught.value.largest["detail"] == "role=user"
    assert caught.value.identity["native"] is False


# ── attributability ──────────────────────────────────────────────────────

def test_refusal_names_the_offending_part_and_the_runner_up(monkeypatch):
    """With the bound injected low, an over-bound payload names its culprit."""
    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", 20_000)
    gateway = AIGateway("deepseek/deepseek-v4-flash")
    big = "a" * 30_000          # the tool result that pushed it over
    medium = "b" * 5_000
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "c1", "content": big},
        {"role": "user", "content": medium},
    ]
    with patch("litellm.completion", side_effect=AssertionError("sent")):
        with pytest.raises(OutgoingBodyTooLarge) as caught:
            gateway.generate_native(messages, tools=TOOLS)

    error = caught.value
    assert error.largest["part"] == "messages[2]"
    assert error.largest["detail"] == "role=tool"
    # The runner-up is named too, so a caller that trims only the largest and
    # retries is told what is next rather than discovering it by failing again.
    runner_up = sorted(error.parts, key=lambda p: p["bytes"], reverse=True)[1]
    assert runner_up["part"] in str(error)
    assert f"{error.largest['bytes']:,} bytes" in str(error)
    # Every part is accounted for, and the total is their sum.
    assert sum(p["bytes"] for p in error.parts) == error.measured_bytes


# ── the normal turn is untouched ─────────────────────────────────────────

def test_a_payload_at_the_bound_still_reaches_the_provider_byte_identical(
        monkeypatch):
    """A body exactly AT the bound is not refused, and its bytes are unchanged.

    The same payload one byte over IS refused, so this pins the comparison
    (over, not at) rather than merely "large payloads are fine".
    """
    gateway = AIGateway("deepseek/deepseek-v4-flash")
    messages = [
        {"role": "system", "content": "you are a driver"},
        {"role": "user", "content": "q" * 40_000},
    ]
    expected = _pre_change_kwargs(gateway, messages, TOOLS)
    size, _ = measure_outgoing_body(expected)
    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", size)

    with patch("litellm.completion", return_value=_ok_response()) as spy:
        gateway.generate_native(messages, tools=TOOLS)
        assert spy.call_count == 1

    captured = spy.call_args.kwargs
    # The payload is what the pre-change assembly produced: same keys, same
    # bytes, nothing added, nothing truncated.
    assert _canonical(captured) == _canonical(expected)
    assert captured["messages"][1]["content"] == "q" * 40_000

    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", size - 1)
    with patch("litellm.completion", side_effect=AssertionError("sent")) as spy2:
        with pytest.raises(OutgoingBodyTooLarge):
            gateway.generate_native(messages, tools=TOOLS)
        assert spy2.call_count == 0


def test_a_normal_small_turn_is_unchanged_with_the_real_bound():
    """The default bound does not alter an ordinary turn's request at all."""
    assert OUTGOING_BODY_BOUND_BYTES == 46_137_344
    gateway = AIGateway("deepseek/deepseek-v4-flash")
    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"}]
    expected = _pre_change_kwargs(gateway, messages, TOOLS)
    with patch("litellm.completion", return_value=_ok_response("hi")) as spy:
        assert gateway.generate_native(messages, tools=TOOLS).text == "hi"
        assert spy.call_count == 1
    assert _canonical(spy.call_args.kwargs) == _canonical(expected)


# ── cost: measured once per call, not once per candidate ─────────────────

def test_the_measurement_is_paid_once_per_call_not_once_per_candidate(monkeypatch):
    """A failover must not re-measure the identical body.

    "flash" routes to several candidates; the first rejects the call with an
    endpoint error (non-retryable, so tenacity does not restart the whole call)
    and the walk continues. Two provider attempts happen, and the body is
    measured exactly once.
    """
    measured_calls: list[int] = []
    real_measure = ai_router.measure_outgoing_body

    def counting_measure(kwargs):
        measured_calls.append(1)
        return real_measure(kwargs)

    monkeypatch.setattr(ai_router, "measure_outgoing_body", counting_measure)
    gateway = AIGateway("flash")
    assert len(gateway._candidates) >= 2, gateway._candidates
    messages = [{"role": "user", "content": "go"}]
    with patch("litellm.completion") as spy:
        spy.side_effect = [
            litellm.exceptions.AuthenticationError(
                "dead key", llm_provider="ark", model=gateway.active_model),
            _ok_response("answered by the fallback"),
        ]
        turn = gateway.generate_native(messages, tools=TOOLS)
    assert spy.call_count == 2, "expected one failover within the same call"
    assert turn.text == "answered by the fallback"
    assert len(measured_calls) == 1, (
        "the outgoing body was measured once per candidate attempt instead of "
        "once per call")
