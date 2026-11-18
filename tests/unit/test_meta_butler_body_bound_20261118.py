# tests/unit/test_meta_butler_body_bound_20261118.py
"""The two direct `litellm.acompletion` writers are behind the ONE body guard.

Measured occurrence, 2026-11-18T11:49:47Z (issue iss-7106321940514e5a): a body
over the upstream 50331648-byte ceiling was forwarded unexamined and the turn
died with an upstream 400 that carried no byte count, no bound and no culprit.

`a03c6f6a70` measured and bounded the outgoing body at `AIGateway`'s funnel, but
`core/meta_agent.py` bypasses that gateway and calls `litellm.acompletion`
itself at two sites, so the same body there still died upstream:

  (a) `_summarize_chunk` — the compacter. Its `except` returns None, so the
      failure was SILENT: compaction was skipped and the session drifted.
  (b) `_stream_llm`'s `_open_stream` — the butler's streaming turn.

These tests pin the properties that make the fix real rather than nominal:

  * there is ONE guard (`core.ai_router.refuse_if_body_too_large`), imported by
    both writers — not a second bound, error type or message shape;
  * EACH writer refuses an over-bound body with ZERO provider calls and an
    error that says how big the body was, what the bound is and which part is
    the culprit;
  * the compacter still fails SOFT (None) but leaves an attributable log line;
  * a body just UNDER the bound still reaches the provider byte-identically —
    so the guard is a bound, not a blanket refusal.

The bound is injectable (`core.ai_router.OUTGOING_BODY_BOUND_BYTES`) precisely
so these tests exercise the boundary with kilobyte payloads instead of building
48 MiB.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core import ai_router
from core import meta_agent
from core.ai_router import (
    GATEWAY_BODY_LIMIT_BYTES,
    OUTGOING_BODY_BOUND_BYTES,
    OutgoingBodyTooLarge,
    measure_outgoing_body,
)
from core.meta_agent import MetaAgent


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def mock_ws():
    return MagicMock()


def _agent(mock_db, mock_ws, session_id="sess-body-bound"):
    return MetaAgent(mock_db, mock_ws, owner_email="t@local",
                     session_id=session_id)


# ── one guard, imported by both writers ──────────────────────────────────

def test_the_guard_is_one_imported_policy_not_a_second_copy():
    """The writers reuse ai_router's guard; they do not re-declare it.

    A copy of the number or of the exception type in `core/meta_agent.py` would
    be a second policy that can drift from the gateway's (a length someone
    "fixes" in one place), which is exactly what the issue forbids.
    """
    guard = getattr(ai_router, "refuse_if_body_too_large", None)
    identity = getattr(ai_router, "body_identity", None)
    assert guard is not None, (
        "the refusal is still a private AIGateway method; a direct writer "
        "cannot import it")
    assert identity is not None
    # The same objects, not look-alikes.
    assert meta_agent.refuse_if_body_too_large is guard
    assert meta_agent.body_identity is identity
    assert meta_agent.OutgoingBodyTooLarge is ai_router.OutgoingBodyTooLarge

    source = Path(meta_agent.__file__).read_text(encoding="utf-8")
    # Neither the upstream ceiling nor our bound is restated here...
    for literal in ("46137344", "46_137_344", "50331648", "50_331_648",
                    "48 * 1024"):
        assert literal not in source, f"a second copy of the bound: {literal}"
    # ...nor is the exception type, and BOTH direct writers call the guard.
    assert "class OutgoingBodyTooLarge" not in source
    assert source.count("refuse_if_body_too_large(") == 2


def test_the_one_refusal_message_shape_is_the_shared_guards(capsys, monkeypatch):
    """One `[ai_router] refused outgoing body` line, naming the caller.

    The occurrence's second half was silence: the failure had no line anyone
    could grep. The line is printed by the shared guard, so all three writers
    report in one shape.
    """
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


# ── (a) the compacter: refused before dispatch, fail-soft, attributable ──

async def test_compacter_refuses_oversized_chunk_before_any_provider_call(
        mock_db, mock_ws, monkeypatch, capsys):
    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", 2_000)
    agent = _agent(mock_db, mock_ws, session_id="sess-compacter")
    errors: list[str] = []
    agent._log_error = errors.append

    sent: list[dict] = []

    async def provider_reached(**kwargs):
        sent.append(kwargs)
        raise AssertionError("an over-bound compaction reached the provider")

    with patch("core.meta_agent.litellm.acompletion", new=provider_reached):
        result = await agent._summarize_chunk("chunk " + "x" * 20_000)

    assert sent == [], "the compacter dispatched an over-bound body"
    # The fail-soft contract is untouched: the transcript is left alone.
    assert result is None
    # But the refusal is attributable in the log, with the measurement.
    refusal_logs = [e for e in errors if "compaction refused before dispatch" in e]
    assert len(refusal_logs) == 1, errors
    logged = refusal_logs[0]
    assert "AItelier refused this call before sending it" in logged
    assert "messages[1]" in logged and "role=user" in logged
    assert "2,000 bytes" in logged
    out = capsys.readouterr().out
    assert "[ai_router] refused outgoing body (meta_agent.compacter" in out


async def test_compacter_under_bound_chunk_still_reaches_the_provider(
        mock_db, mock_ws, monkeypatch):
    """The control: a small chunk is compacted normally, no guard in the way."""
    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", 10 * 1024 * 1024)
    agent = _agent(mock_db, mock_ws, session_id="sess-compacter-ok")
    response = MagicMock()
    response.choices[0].message.content = "  a summary  "
    sent: list[dict] = []

    async def provider(**kwargs):
        sent.append(kwargs)
        return response

    with patch("core.meta_agent.litellm.acompletion", new=provider):
        result = await agent._summarize_chunk("chunk " + "x" * 500)

    assert result == "a summary"
    assert len(sent) == 1


# ── (b) the butler's streaming turn ──────────────────────────────────────

def _text_stream(text: str = "hi"):
    """An async iterator shaped like litellm's streaming response."""
    async def gen():
        yield SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None))],
            usage=None)
    return gen()


async def _drain(agent, messages):
    return [event async for event in agent._stream_llm(messages)]


async def test_butler_turn_refuses_oversized_transcript_before_any_provider_call(
        mock_db, mock_ws, monkeypatch, capsys):
    bound = 20_000
    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", bound)
    agent = _agent(mock_db, mock_ws, session_id="sess-butler")
    messages = [
        {"role": "system", "content": "you are the butler"},
        {"role": "user", "content": "read the log"},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 200_000},
    ]

    sent: list[dict] = []

    async def provider_reached(**kwargs):
        sent.append(kwargs)
        raise AssertionError("an over-bound butler turn reached the provider")

    caught: BaseException | None = None
    with patch("core.meta_agent.litellm.acompletion", new=provider_reached):
        try:
            await _drain(agent, messages)
        except BaseException as exc:      # noqa: BLE001 -- asserted below
            caught = exc

    # ZERO provider calls is the property the issue is about; it is checked
    # BEFORE the error's shape so a base tree that dispatches fails here.
    assert sent == [], "the butler turn dispatched an over-bound body"
    assert isinstance(caught, OutgoingBodyTooLarge), caught
    error = caught

    assert error.measured_bytes > bound
    assert error.bound_bytes == bound
    assert error.gateway_limit_bytes == GATEWAY_BODY_LIMIT_BYTES
    assert error.largest["part"] == "messages[2]"
    assert error.largest["detail"] == "role=tool"
    assert error.largest["bytes"] == len(json.dumps(
        messages[2], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    # Attributability: which writer, which route, which session, how big.
    assert error.identity["caller"] == "meta_agent.butler_turn"
    assert error.identity["model_route"] == agent._raw_model
    assert error.identity["session"] == "sess-butler"
    assert error.identity["messages"] == len(messages)
    text = str(error)
    assert f"{error.measured_bytes:,} bytes" in text
    assert f"{bound:,} bytes" in text
    assert "AItelier refused this call before sending it" in text
    out = capsys.readouterr().out
    assert "[ai_router] refused outgoing body (meta_agent.butler_turn" in out


async def test_butler_turn_just_under_the_injected_bound_reaches_the_provider(
        mock_db, mock_ws, monkeypatch):
    """The control, and the comparison: one byte less and it IS refused.

    Three arms over one body:
      1. learn the number the guard measures at this site;
      2. inject a bound just ABOVE it — the provider is reached and the
         payload is byte-identical (the guard measures, it does not mutate);
      3. inject a bound one byte BELOW it — zero provider calls.
    """
    agent = _agent(mock_db, mock_ws, session_id="sess-control")
    messages = [{"role": "system", "content": "you are the butler"},
                {"role": "user", "content": "q" * 10_000}]
    measured: list[int] = []
    real_measure = ai_router.measure_outgoing_body

    def recording_measure(kwargs):
        total, parts = real_measure(kwargs)
        measured.append(total)
        return total, parts

    monkeypatch.setattr(ai_router, "measure_outgoing_body", recording_measure)

    sent: list[dict] = []

    async def provider(**kwargs):
        sent.append(dict(kwargs))
        return _text_stream("hi")

    # Arm 1: the body is measured by the guard at its real (default) bound.
    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES",
                        OUTGOING_BODY_BOUND_BYTES)
    with patch("core.meta_agent.litellm.acompletion", new=provider):
        events = await _drain(agent, messages)
    assert [e["_type"] for e in events] == ["text_delta", "collected"]
    assert len(sent) == 1
    first = measured[-1]

    # Arm 2: a bound just ABOVE the measured body — it still goes out.
    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", first + 64)
    with patch("core.meta_agent.litellm.acompletion", new=provider):
        await _drain(agent, messages)
    assert len(sent) == 2, "a body just under the bound was refused"
    just_under = measured[-1]
    assert 0 <= (first + 64) - just_under < 512, (first, just_under)
    assert sent[-1]["messages"][1]["content"] == "q" * 10_000
    assert sent[-1]["tools"] is meta_agent.TOOL_DEFINITIONS

    # Arm 3: one byte less of allowance and the same body is refused.
    monkeypatch.setattr(ai_router, "OUTGOING_BODY_BOUND_BYTES", just_under - 1)
    caught: BaseException | None = None
    with patch("core.meta_agent.litellm.acompletion", new=provider):
        try:
            await _drain(agent, messages)
        except BaseException as exc:      # noqa: BLE001 -- asserted below
            caught = exc
    assert len(sent) == 2, "an over-bound body was dispatched"
    assert isinstance(caught, OutgoingBodyTooLarge), caught
    assert caught.bound_bytes == just_under - 1
    assert caught.measured_bytes == just_under
