"""A long-lived gateway that failed over moves back once the earlier endpoint serves.

A DSH session binds one gateway for its whole life. Observed 2026-11-07: a 429
burst at 09:16Z left the aitelier director on the paid fallback for hours while
the plans ahead of it were serving again. Wire calls are always patched.
"""
import litellm
import pytest

import core.ai_router as ai
from tests.unit.test_model_routes import _Resp, _plain_transport, gw, wiring  # noqa: F401
from tests.unit.test_window_quota_classification import OLLAMA_WEEKLY, rate_error


@pytest.fixture
def clock(monkeypatch):
    import time
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _wire(monkeypatch, failing):
    calls = []

    def wire(**kwargs):
        calls.append(kwargs.get("api_key"))
        if kwargs.get("api_key") in failing:
            raise litellm.exceptions.AuthenticationError("down", llm_provider="alpha", model="m-1")
        return _Resp()

    monkeypatch.setattr(litellm, "completion", wire)
    return calls


def test_returns_after_the_probe_interval_and_not_before(wiring, monkeypatch, clock):
    failing = {"alpha-secret"}
    calls = _wire(monkeypatch, failing)
    g = gw(wiring, "model_a")
    g.generate_native([{"role": "user", "content": "hi"}])
    assert g.active_model == "beta/m-1"
    failing.clear()
    clock[0] += ai._RETURN_PROBE_EVERY_S - 1
    assert g.return_to_earlier() is False and g.active_model == "beta/m-1"
    clock[0] += 2
    assert g.return_to_earlier() is True and g.active_model == "alpha/m-1"
    del calls[:]
    g.generate_native([{"role": "user", "content": "hi"}])
    assert calls == ["alpha-secret"]
    assert g.return_to_earlier() is False          # already at the head


def test_an_earlier_endpoint_that_still_fails_walks_forward_again(wiring, monkeypatch, clock):
    calls = _wire(monkeypatch, {"alpha-secret"})
    g = gw(wiring, "model_a")
    g.generate_native([{"role": "user", "content": "hi"}])
    clock[0] += ai._RETURN_PROBE_EVERY_S + 1
    assert g.return_to_earlier() is True
    del calls[:]
    g.generate_native([{"role": "user", "content": "hi"}])
    assert calls[-1] == "beta-secret" and g.active_model == "beta/m-1"


def test_a_parked_earlier_endpoint_is_not_probed(wiring, monkeypatch, clock):
    monkeypatch.setattr(ai, "_RETURN_PROBE_EVERY_S", 60)    # probe due while still parked
    def wire(**kwargs):
        if kwargs.get("api_key") == "alpha-secret":
            raise rate_error(OLLAMA_WEEKLY)
        return _Resp()

    monkeypatch.setattr(litellm, "completion", wire)
    g = gw(wiring, "model_a")
    g.generate_native([{"role": "user", "content": "hi"}])
    assert "alpha/m-1" in ai.endpoint_cooldowns()     # the weekly cap parks it
    clock[0] += 61
    assert g.return_to_earlier() is False
    assert g.active_model == "beta/m-1"


def test_never_returns_after_a_context_failover(wiring, monkeypatch, clock):
    _wire(monkeypatch, {"alpha-secret"})
    g = gw(wiring, "model_a")
    g.generate_native([{"role": "user", "content": "hi"}])
    g._context_failed_over = True
    clock[0] += ai._RETURN_PROBE_EVERY_S + 1
    assert g.return_to_earlier() is False and g.active_model == "beta/m-1"
