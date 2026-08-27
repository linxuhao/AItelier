"""The wall-clock cap on a single LLM call.

`_build_kwargs` sets `timeout: 300.0` and litellm plumbs it to
`httpx.Client.build_request(timeout=...)` — and it still did not bound the
call. Measured 2026-08-27 on run 8305b1e3 (jinyong-aim, step 2): py-spy caught
the worker parked 31 minutes inside `_receive_response_body`, i.e. the response
had started and its body never finished. The step heartbeated the whole time,
so the reaper read it as "slow, not dead" — correctly, because from the outside
an unbounded read and a slow model look identical.

These tests pin the cap itself, not litellm.
"""

import threading
import time

import litellm
import pytest

from core.ai_router import AIGateway


class _Gate(AIGateway):
    """AIGateway with construction skipped — only the bounded-call path is under
    test, and building a real gateway would need a provider registry."""

    def __init__(self):
        self.active_model = "test/model"


def test_a_hung_call_raises_timeout_not_forever(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def _hang(**kwargs):
        started.set()
        release.wait(30)          # bounded so a failing test cannot wedge CI
        return "late"

    monkeypatch.setattr(litellm, "completion", _hang)
    g = _Gate()
    monkeypatch.setattr(_Gate, "_WALL_CAP_S", 0.3)

    t0 = time.monotonic()
    with pytest.raises(litellm.exceptions.Timeout) as ei:
        g._completion_bounded({"timeout": 0.3, "model": "test/model"})
    elapsed = time.monotonic() - t0

    assert started.is_set(), "the call must actually have been dispatched"
    assert elapsed < 5, f"the cap did not fire promptly (took {elapsed:.1f}s)"
    assert "cannot catch a response that trickles" in str(ei.value)
    release.set()


def test_the_cap_does_not_block_on_the_abandoned_thread(monkeypatch):
    """`with ThreadPoolExecutor(...)` would join the hung worker on exit and
    reproduce the very hang this exists to break. The pool must be shut down
    with wait=False."""
    release = threading.Event()

    def _hang(**kwargs):
        release.wait(30)
        return "late"

    monkeypatch.setattr(litellm, "completion", _hang)
    monkeypatch.setattr(_Gate, "_WALL_CAP_S", 0.3)
    g = _Gate()

    t0 = time.monotonic()
    with pytest.raises(litellm.exceptions.Timeout):
        g._completion_bounded({"timeout": 0.3})
    # returning at all before the worker finishes is the assertion
    assert time.monotonic() - t0 < 5
    assert not release.is_set()
    release.set()


def test_a_prompt_answer_is_returned_untouched(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(litellm, "completion", lambda **kw: sentinel)
    g = _Gate()
    assert g._completion_bounded({"timeout": 5.0}) is sentinel


def test_the_call_receives_the_kwargs_verbatim(monkeypatch):
    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(litellm, "completion", _capture)
    g = _Gate()
    g._completion_bounded({"timeout": 5.0, "model": "m", "messages": [1]})
    assert seen == {"timeout": 5.0, "model": "m", "messages": [1]}


def test_an_error_from_the_call_propagates(monkeypatch):
    def _boom(**kwargs):
        raise ValueError("provider said no")

    monkeypatch.setattr(litellm, "completion", _boom)
    g = _Gate()
    with pytest.raises(ValueError, match="provider said no"):
        g._completion_bounded({"timeout": 5.0})


def test_timeout_is_a_failover_exception():
    """The cap raises litellm.Timeout precisely because the existing failover
    already routes it — a wedged endpoint must fail over like a refused one."""
    from core.ai_router import FAILOVER_EXCEPTIONS
    assert litellm.exceptions.Timeout in FAILOVER_EXCEPTIONS


def test_cap_defaults_when_kwargs_carry_no_timeout(monkeypatch):
    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(litellm, "completion", _capture)
    g = _Gate()
    assert g._completion_bounded({}) == "ok"
