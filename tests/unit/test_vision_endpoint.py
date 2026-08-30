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


# ── thinking off: the fix for a 39.6-minute blind run ────────────────────────
#
# Measured 2026-08-30 on jinyong-touch's own frames: with thinking on, one
# four-frame call spends 769 completion tokens (2,444 chars of them
# `reasoning_content`) to write a 339-char answer; with it off, 101 tokens and
# the SAME six YES/NO verdicts. Server-side 9.93s -> 1.42s, and this gate makes
# one call per scenario — that run made 59 and took 2,373 seconds before ending
# blind at `unparseable_response`, its reply cut off mid-word at "Q3: NO -".
#
# These pin the two halves separately. An `or` across them would stay green with
# either half deleted.

def _sent_body(monkeypatch, *, status=200, text="Q0: YES - ok"):
    """Capture the JSON body `_post_once` actually puts on the wire."""
    import json as _json
    seen = {}

    class _Resp:
        status = 200

        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return _json.dumps(self._payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen["body"] = _json.loads(req.data)
        if status != 200:
            raise urllib.error.HTTPError("u", status, "nope", {}, None)
        return _Resp({"choices": [{"message": {"content": text}}]})

    monkeypatch.setattr(impl.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_thinking_is_off_on_the_wire(monkeypatch):
    """The runaway reasoning is suppressed at the request, not cleaned up after."""
    seen = _sent_body(monkeypatch)
    impl._post_once("http://judge.invalid/v1/chat/completions", "m", "", [], [], no_think=True)
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_thinking_can_be_left_on(monkeypatch):
    """`no_think=False` must send NO such field — it is what the 400 downgrade
    falls back to, and a gateway that rejected the field must not see it again."""
    seen = _sent_body(monkeypatch)
    impl._post_once("http://judge.invalid/v1/chat/completions", "m", "", [], [], no_think=False)
    assert "chat_template_kwargs" not in seen["body"]


def test_a_gateway_that_rejects_the_field_is_downgraded_not_blinded(monkeypatch):
    """`chat_template_kwargs` is a llama.cpp/vLLM extension. A strict judge 400s
    on it, and a 400 is not retryable — so before the downgrade this blinded the
    whole run. One extra call, thinking back on, verdict preserved."""
    monkeypatch.setattr(impl.time, "sleep", lambda s: None)
    monkeypatch.setattr(impl, "_NO_THINK", True)
    sent = []

    def picky(url, model, key, files, questions, max_tokens=0, no_think=True):
        sent.append(no_think)
        if no_think:
            raise urllib.error.HTTPError("u", 400, "unknown field", {}, None)
        return "Q0: YES - ok"

    monkeypatch.setattr(impl, "_post_once", picky)
    assert impl._post("u", "m", "", [], []) == "Q0: YES - ok"
    assert sent == [True, False], "expected exactly one downgrade, then success"


def test_the_downgrade_cannot_recurse(monkeypatch):
    """A judge that 400s for an unrelated reason must fail once, not forever."""
    monkeypatch.setattr(impl.time, "sleep", lambda s: None)
    monkeypatch.setattr(impl, "_NO_THINK", True)
    calls = {"n": 0}

    def always_400(*a, **k):
        calls["n"] += 1
        raise urllib.error.HTTPError("u", 400, "bad request", {}, None)

    monkeypatch.setattr(impl, "_post_once", always_400)
    with pytest.raises(urllib.error.HTTPError):
        impl._post("u", "m", "", [], [])
    assert calls["n"] == 2, "one attempt with the field, one without, then stop"
