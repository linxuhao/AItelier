"""The streaming transport inside AIGateway._call_llm.

The gateway streams every completion — not to show tokens, but because chunk
arrival is the only externally visible signal that distinguishes a long
completion from a wedged one (the 2026-08-27 trickle hang). The chunks are
re-assembled with litellm.stream_chunk_builder into the exact non-streaming
response shape, so nothing downstream changes.

Rebuild FIDELITY (content / tool_calls / reasoning_content / usage surviving
the builder) was verified live against ark, qwen and opencodego on 2026-08-28;
these tests pin the transport mechanics around it.
"""

import types

import litellm
import pytest

from core.ai_router import AIGateway, FAILOVER_EXCEPTIONS


class _Gate(AIGateway):
    """Construction skipped — only _call_llm is under test."""

    def __init__(self):
        self.active_model = "test/model"
        self.provider = "test"
        self.on_progress = None


def _chunk(content=None, reasoning=None, args=None):
    """A minimal stream-chunk stand-in with the attributes _chunk_len reads."""
    tool_calls = None
    if args is not None:
        tool_calls = [types.SimpleNamespace(
            function=types.SimpleNamespace(arguments=args))]
    delta = types.SimpleNamespace(content=content, reasoning_content=reasoning,
                                  tool_calls=tool_calls)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])


@pytest.fixture
def transport(monkeypatch):
    """Mock litellm at the seam: capture the completion kwargs, feed a canned
    chunk list, and stub the builder to a sentinel that records its inputs."""
    state = {"kwargs": None, "built_from": None, "chunks": [
        _chunk(content="hel"), _chunk(content="lo"),
        _chunk(reasoning="think"), _chunk(args='{"a": 1}'),
    ], "rebuilt": object()}

    def _completion(**kwargs):
        state["kwargs"] = kwargs
        assert kwargs.get("stream"), "transport must request a stream"
        return iter(state["chunks"])

    def _builder(chunks, messages=None):
        state["built_from"] = (list(chunks), messages)
        return state["rebuilt"]

    monkeypatch.setattr(litellm, "completion", _completion)
    monkeypatch.setattr(litellm, "stream_chunk_builder", _builder)
    return state


def test_streams_and_rebuilds(transport):
    g = _Gate()
    kwargs = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    result = g._call_llm(kwargs)

    assert result is transport["rebuilt"]
    built_chunks, built_messages = transport["built_from"]
    assert built_chunks == transport["chunks"], "every chunk must reach the builder"
    assert built_messages == kwargs["messages"]
    assert transport["kwargs"]["stream_options"] == {"include_usage": True}


def test_original_kwargs_are_not_mutated(transport):
    g = _Gate()
    kwargs = {"model": "m", "messages": []}
    g._call_llm(kwargs)
    assert "stream" not in kwargs and "stream_options" not in kwargs, (
        "a failover retry re-dispatches the same kwargs dict — leaking the "
        "stream flag would make the retry return a raw stream to callers "
        "expecting a ModelResponse")


def test_kill_switch_reverts_to_plain_call(monkeypatch):
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")
    sentinel = object()
    seen = {}

    def _completion(**kwargs):
        seen.update(kwargs)
        return sentinel

    monkeypatch.setattr(litellm, "completion", _completion)
    g = _Gate()
    assert g._call_llm({"model": "m"}) is sentinel
    assert "stream" not in seen


def test_progress_ticks_with_growing_chars(transport, monkeypatch):
    monkeypatch.setattr(_Gate, "_PROGRESS_EVERY_S", 0.0)  # tick every chunk
    g = _Gate()
    ticks = []
    g.on_progress = ticks.append
    g._call_llm({"model": "m", "messages": []})

    assert len(ticks) == 4
    chars = [t["chars"] for t in ticks]
    # hel(3) → lo(2) → reasoning(5) → tool args(8): content, reasoning and
    # tool-call arguments all count as liveness
    assert chars == [3, 5, 10, 18]
    assert all("elapsed" in t and t["served_by"] == "test/model" for t in ticks)


def test_progress_throttles(transport, monkeypatch):
    monkeypatch.setattr(_Gate, "_PROGRESS_EVERY_S", 3600.0)
    g = _Gate()
    ticks = []
    g.on_progress = ticks.append
    g._call_llm({"model": "m", "messages": []})
    # the first chunk always ticks ("it's alive"); the rest are inside the
    # throttle window
    assert len(ticks) == 1


def test_a_broken_progress_hook_cannot_break_the_call(transport, monkeypatch):
    monkeypatch.setattr(_Gate, "_PROGRESS_EVERY_S", 0.0)
    g = _Gate()

    def _boom(_):
        raise RuntimeError("observer crashed")

    g.on_progress = _boom
    assert g._call_llm({"model": "m", "messages": []}) is transport["rebuilt"]


def test_empty_stream_raises_a_failover_error(transport):
    transport["chunks"] = []
    g = _Gate()
    with pytest.raises(litellm.exceptions.APIConnectionError) as ei:
        g._call_llm({"model": "m", "messages": []})
    assert isinstance(ei.value, FAILOVER_EXCEPTIONS), (
        "a chunkless stream is an endpoint failure and must route to the "
        "next candidate like one")


def test_chunk_len_survives_malformed_chunks():
    assert AIGateway._chunk_len(object()) == 0
    assert AIGateway._chunk_len(types.SimpleNamespace(choices=[])) == 0
    assert AIGateway._chunk_len(_chunk(content="ab", reasoning="cde",
                                       args="fg")) == 7
