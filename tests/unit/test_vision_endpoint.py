"""One truncated response must not blind a 47-scenario vision run.

On 2026-08-26 the gate died on `IncompleteRead(1 bytes read)` at scenario 5 of
47: the connection was established and broke mid-body. It got zero retries (only
ReasoningStarved had one), so the whole verdict came back
blind_reason=endpoint_unreachable — discarding 4 batches that had already been
judged and never looking at the other 43. Two rounds of health-bar work were
then planned against that non-verdict.

The primary judge being ABSENT is normal and handled (the box is shared); these
tests pin the two things that are not: that a mid-flight break is retried, and
that a deterministic failure is not.
"""
import http.client
import socket
import urllib.error
from pathlib import Path

import pytest

from aitelier.tools.godot_vision import impl

ROOT = Path(__file__).resolve().parents[2]


# ── what gets a second chance ────────────────────────────────────────────────

@pytest.mark.parametrize("exc", [
    http.client.IncompleteRead(b"x"),
    http.client.RemoteDisconnected("closed"),
    ConnectionResetError("reset by peer"),
    urllib.error.HTTPError("u", 503, "unavailable", {}, None),
    urllib.error.HTTPError("u", 500, "server error", {}, None),
    urllib.error.URLError(ConnectionResetError("reset")),
])
def test_mid_flight_breaks_are_retryable(exc):
    assert impl._retryable(exc)


@pytest.mark.parametrize("exc", [
    urllib.error.HTTPError("u", 404, "no such model", {}, None),
    urllib.error.HTTPError("u", 401, "bad key", {}, None),
    urllib.error.URLError(ConnectionRefusedError("refused")),
    # A timeout is temporary UNAVAILABILITY, the same class as refused — and
    # retrying it costs a full 300s per attempt against a judge that is not
    # answering, ~15 min per batch, re-paid for every scenario. Fall through to
    # the next judge instead; that is what the route is for.
    urllib.error.URLError(TimeoutError("timed out")),
    TimeoutError("read timed out"),
    socket.timeout("timed out"),
    ValueError("unparseable"),
])
def test_deterministic_failures_are_not_retryable(exc):
    """Retrying these wastes the run's clock and still fails — and for `refused`
    the caller has a better move: fall back to the other judge immediately."""
    assert not impl._retryable(exc)


# ── the loop itself ──────────────────────────────────────────────────────────

def test_a_single_truncated_response_still_produces_an_answer(monkeypatch):
    """The 2026-08-26 failure, replayed: first call truncates, second succeeds."""
    monkeypatch.setattr(impl.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise http.client.IncompleteRead(b"x")
        return "Q5: YES"

    monkeypatch.setattr(impl, "_post_once", flaky)
    assert impl._post("u", "m", "", [], []) == "Q5: YES"
    assert calls["n"] == 2


def test_it_gives_up_after_the_attempt_cap(monkeypatch):
    """A judge that is broken rather than flaky must still end the run, loudly."""
    monkeypatch.setattr(impl.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_broken(*a, **k):
        calls["n"] += 1
        raise http.client.IncompleteRead(b"x")

    monkeypatch.setattr(impl, "_post_once", always_broken)
    with pytest.raises(http.client.IncompleteRead):
        impl._post("u", "m", "", [], [])
    assert calls["n"] == impl._POST_ATTEMPTS


def test_a_deterministic_failure_is_not_retried(monkeypatch):
    """A 404 model name would be wrong the second time too — and every wasted
    attempt is a call the fallback judge could have been making instead."""
    monkeypatch.setattr(impl.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def wrong_model(*a, **k):
        calls["n"] += 1
        raise urllib.error.HTTPError("u", 404, "no such model", {}, None)

    monkeypatch.setattr(impl, "_post_once", wrong_model)
    with pytest.raises(urllib.error.HTTPError):
        impl._post("u", "m", "", [], [])
    assert calls["n"] == 1


def test_reasoning_starved_is_left_to_its_own_escalation(monkeypatch):
    """_ask retries this ONE step up in budget; a blind retry here at the same
    budget would burn the escalation's chance and starve identically."""
    monkeypatch.setattr(impl.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def starved(*a, **k):
        calls["n"] += 1
        raise impl.ReasoningStarved("wrote no answer text")

    monkeypatch.setattr(impl, "_post_once", starved)
    with pytest.raises(impl.ReasoningStarved):
        impl._post("u", "m", "", [], [])
    assert calls["n"] == 1


# The compose/impl default-agreement check that used to live here is GONE on
# purpose. It pinned GODOT_VISION_URL + GODOT_VISION_MODEL, and those env vars
# no longer choose the judge: the tool now resolves the internal model name
# `vision` through model_routes.json (ordered candidates) + llm_providers.json,
# the same two layers every agent step uses. A test that asserts a mechanism the
# code has replaced is not a regression guard, it is a tripwire on the wrong
# wire — and the routing table has its own tests.
