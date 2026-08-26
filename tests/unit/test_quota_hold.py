"""Provider quota exhaustion must park the scheduler, not kill the run.

Live failure this pins: the 2026-08-26 jinyong-jianghu run hit DeepSeek's 5-hour
quota at 00:44, burned max_retries across four claims, and was marked `failed` at
00:59 — 18 minutes before the quota reopened on its own at 03:18.
"""
import time
from datetime import datetime, timezone

import pytest

from core.llm_quota import is_quota_exhausted, quota_reset_at
import core.scheduler as sched


REAL = ("litellm.RateLimitError: RateLimitError: OpenAIException - You have "
        "exceeded the 5-hour usage quota. It will reset at 2026-08-26 09:18:28 "
        "+0800 CST. We recommend upgrading your plan for more quota, or waiting "
        "for the reset. Request id: 021787705978570463b2e7ab2e4caf441b2615bb")

BURST = ("litellm.RateLimitError: RateLimitError: Rate limit reached for "
         "requests per min. Please retry shortly.")


class _Rate(Exception):
    """Stands in for litellm's RateLimitError in predicate tests."""


@pytest.fixture(autouse=True)
def _clear_hold():
    sched._QUOTA_HOLD_UNTIL = 0.0
    sched._QUOTA_HOLD_REASON = ""
    yield
    sched._QUOTA_HOLD_UNTIL = 0.0
    sched._QUOTA_HOLD_REASON = ""


# ── classification ───────────────────────────────────────────────────────────

def test_spent_window_is_recognised():
    assert is_quota_exhausted(REAL)


def test_burst_throttling_is_not_a_spent_window():
    """The distinction is the whole point: a burst 429 SHOULD keep retrying."""
    assert not is_quota_exhausted(BURST)


def test_reset_instant_is_parsed_in_utc():
    got = quota_reset_at(REAL)
    assert got == datetime(2026, 8, 26, 1, 18, 28, tzinfo=timezone.utc)


def test_no_reset_time_reports_none_rather_than_guessing():
    assert quota_reset_at("usage quota exceeded; try later") is None


def test_retry_predicate_refuses_to_retry_a_spent_quota(monkeypatch):
    """The gateway's tenacity predicate, which is what stops the 20 seconds of
    pointless backoff. Imported here rather than at module scope so this file
    still collects while the gateway is mid-refactor in another branch."""
    from core.ai_router import _retry_llm_error
    monkeypatch.setattr("core.ai_router.RETRYABLE_EXCEPTIONS", (_Rate,))
    assert not _retry_llm_error(_Rate(REAL))
    assert _retry_llm_error(_Rate(BURST))
    assert not _retry_llm_error(ValueError("unrelated"))


# ── the hold ─────────────────────────────────────────────────────────────────

def test_hold_runs_until_the_published_reset(monkeypatch):
    """Before: 20s of backoff against a 5-hour window. After: park until it opens."""
    now = datetime(2026, 8, 26, 0, 59, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(sched._time, "time", lambda: now)
    sched._note_quota_exhausted(REAL)
    remaining = sched._quota_hold_remaining()
    # 01:18:28 UTC + 30s grace − 00:59:00 = 1198s
    assert remaining == pytest.approx(1198, abs=2)


def test_hold_falls_back_when_no_reset_time_is_published(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(sched._time, "time", lambda: now)
    sched._note_quota_exhausted("usage quota exceeded, no time given")
    assert sched._quota_hold_remaining() == pytest.approx(
        sched._QUOTA_HOLD_FALLBACK, abs=1)


def test_hold_is_capped_against_an_absurd_timestamp(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(sched._time, "time", lambda: now)
    sched._note_quota_exhausted("quota exceeded. It will reset at 2099-01-01 00:00:00 +0000")
    assert sched._quota_hold_remaining() <= sched._QUOTA_HOLD_MAX


def test_already_elapsed_reset_does_not_hold(monkeypatch):
    """The window reopened while we were failing — don't park on stale news."""
    now = datetime(2026, 8, 26, 5, 0, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(sched._time, "time", lambda: now)
    sched._note_quota_exhausted(REAL)
    assert sched._quota_hold_remaining() == 0.0


def test_hold_never_shortens_on_a_second_report(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(sched._time, "time", lambda: now)
    sched._note_quota_exhausted("quota exceeded. It will reset at 2026-08-26 09:18:28 +0800")
    long_hold = sched._quota_hold_remaining()
    sched._note_quota_exhausted("usage quota exceeded, no time given")   # 300s
    assert sched._quota_hold_remaining() == long_hold


# ── the tick ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_held_tick_claims_nothing(monkeypatch):
    """The retry budget is spent BY CLAIMING, so a held tick must not get that far."""
    now = 1_000_000.0
    monkeypatch.setattr(sched._time, "time", lambda: now)
    sched._note_quota_exhausted(REAL.replace("2026-08-26 09:18:28 +0800",
                                             "2099-01-01 00:00:00 +0000"))

    def _boom(*a, **k):
        raise AssertionError("tick resolved a run while the quota was held")

    monkeypatch.setattr(sched, "_get_or_create_skillflow_run", _boom)
    monkeypatch.setattr(sched, "get_skillflow", _boom)
    logged = []
    monkeypatch.setattr(sched, "tick_log",
                        lambda pid, outcome, **kw: logged.append(outcome))

    await sched._run_skillflow_tick("jinyong-jianghu", None)
    assert logged == ["quota_hold"]


@pytest.mark.asyncio
async def test_tick_proceeds_once_the_hold_expires(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(sched._time, "time", lambda: now)
    sched._note_quota_exhausted("usage quota exceeded, no time given")
    assert sched._quota_hold_remaining() > 0

    monkeypatch.setattr(sched._time, "time",
                        lambda: now + sched._QUOTA_HOLD_FALLBACK + 1)
    reached = []
    monkeypatch.setattr(sched, "get_skillflow",
                        lambda: reached.append("resolved") or _stop())

    def _stop():
        raise RuntimeError("reached the body")

    with pytest.raises(RuntimeError, match="reached the body"):
        await sched._run_skillflow_tick("jinyong-jianghu", None)
    assert reached == ["resolved"]


# ── log volume ───────────────────────────────────────────────────────────────

def test_quota_hold_lines_are_coalesced_to_a_heartbeat(monkeypatch):
    """A 5-hour hold at tick cadence is ~3600 lines that all say the same thing,
    which is exactly how the informative lines get evicted from the rotation."""
    now = [1_000_000.0]
    monkeypatch.setattr(sched._time, "time", lambda: now[0])
    sched._tick_last_hold = 0.0
    written = []
    class _Lg:
        def info(self, fmt, *a):
            written.append(fmt % a)
    monkeypatch.setattr(sched, "_get_tick_logger", lambda: _Lg())

    for _ in range(12):                      # one minute of 5s ticks
        sched.tick_log("p", "quota_hold", remaining="300s")
        now[0] += 5
    assert len(written) == 1

    now[0] += 60                             # next heartbeat window
    sched.tick_log("p", "quota_hold", remaining="240s")
    assert len(written) == 2


def test_coalescing_never_swallows_a_real_outcome(monkeypatch):
    """Only the two repeating tokens are throttled; everything else logs."""
    now = [2_000_000.0]
    monkeypatch.setattr(sched._time, "time", lambda: now[0])
    sched._tick_last_hold = 0.0
    written = []
    class _Lg:
        def info(self, fmt, *a):
            written.append(fmt % a)
    monkeypatch.setattr(sched, "_get_tick_logger", lambda: _Lg())

    for outcome in ("quota_exhausted", "executed", "claim_failed", "terminal"):
        sched.tick_log("p", outcome)
        sched.tick_log("p", outcome)
    assert len(written) == 8


class TestQuotaEscapesTheAgentLoop:
    """The hold is only reachable if the error actually leaves the pipeline.

    Every DPE role is `native_tool_calling: true`, so every LLM call lands in
    `_run_native_step`'s exception handler. That handler turned any exception
    into agent feedback and broke the turn loop, so the RateLimitError never
    reached the scheduler; `feedback` was then overwritten by the "No output
    produced" message and the loop re-called the spent endpoint once per
    attempt until MaxRetriesExceeded — which the scheduler catches BEFORE its
    quota check and which carries no reset-time prose. The hold existed and
    could not fire on the one path that mattered.
    """

    def test_a_spent_quota_is_not_swallowed_as_agent_feedback(self):
        import inspect

        from core import dpe_pipeline

        src = inspect.getsource(dpe_pipeline.PipelineEngine._run_native_step)
        handler = src[src.index("except Exception as e:"):]
        raise_pos = handler.index("raise")
        feedback_pos = handler.index("Native tool calling error")
        assert raise_pos < feedback_pos, (
            "the quota re-raise must come BEFORE the generic feedback path, or "
            "the error is converted to prose and the hold can never fire")
        assert "is_quota_exhausted" in handler

    def test_the_predicate_agrees_on_a_real_provider_message(self):
        from core.llm_quota import is_quota_exhausted

        spent = Exception(
            "litellm.RateLimitError: RateLimitError: OpenAIException - You have "
            "exceeded the 5-hour usage quota. It will reset at "
            "2026-08-26 09:18:28 +0800 CST.")
        burst = Exception("RateLimitError: Too many requests, slow down")
        assert is_quota_exhausted(spent)
        assert not is_quota_exhausted(burst), (
            "a burst 429 clears in seconds and must stay ordinary feedback")


def test_an_unrelated_models_short_window_cannot_cut_a_long_hold(monkeypatch):
    """The cooldown map is process-wide; the hold must not be.

    A 5-minute window on the vision judge sat in the same map as flash's three
    5-hour windows, so `min()` over all of it ended the hold in 5 minutes. The
    scheduler then woke, claimed a flash step, failed on a still-spent plan and
    spent a retry — once per window until max_retries killed the run, which is
    the outage this feature exists to prevent.
    """
    from core import ai_router

    from datetime import datetime, timezone
    reset = datetime(2026, 8, 26, 9, 18, 28, tzinfo=timezone.utc).timestamp()
    now = reset - 5 * 3600                       # five hours before it reopens
    monkeypatch.setattr(sched._time, "time", lambda: now)
    monkeypatch.setattr(sched, "_QUOTA_HOLD_UNTIL", 0.0)
    ai_router.reset_endpoint_cooldowns()
    # A real 5-hour window, so the base hold is long and the only thing that
    # could shorten it is the cooldown map.
    err = Exception("usage quota exceeded. It will reset at "
                    "2026-08-26 09:18:28 +0000.")
    err._aitelier_candidates = ["ark/flash", "qwen/flash"]
    try:
        monkeypatch.setattr(ai_router, "_ENDPOINT_COOLDOWN", {
            "localqwen/vision": now + 300,      # a different model, reopens soon
            "ark/flash": now + 5 * 3600,
            "qwen/flash": now + 5 * 3600,
        })
        sched._note_quota_exhausted(err)
        held = sched._quota_hold_remaining()
    finally:
        ai_router.reset_endpoint_cooldowns()
    assert held > 300, (
        f"held {held:.0f}s — a model this project never calls shortened the hold")


def test_the_hold_ends_when_the_FIRST_plan_reopens(monkeypatch):
    """The error that escapes is the LAST candidate's, so its reset is the
    latest one. Parking on it idles past the moment the first plan came back.

    flash = ark -> qwen -> deepseek, spent at 03:00 / 05:00 / 09:00. Only
    deepseek's message propagates; holding on it wastes six hours during which
    ark was already serving.
    """
    from core import ai_router

    now = 1_000_000.0
    monkeypatch.setattr(sched._time, "time", lambda: now)
    monkeypatch.setattr(sched, "_QUOTA_HOLD_UNTIL", 0.0)
    ai_router.reset_endpoint_cooldowns()
    err = Exception("usage quota exceeded, no time given")
    err._aitelier_candidates = ["ark/deepseek-v4-flash",
                               "qwen/deepseek-v4-flash-0731",
                               "deepseek/deepseek-v4-flash"]
    try:
        monkeypatch.setattr(ai_router, "_ENDPOINT_COOLDOWN", {
            "ark/deepseek-v4-flash": now + 3600,        # first to reopen
            "qwen/deepseek-v4-flash-0731": now + 7200,
            "deepseek/deepseek-v4-flash": now + 21600,  # the one that escaped
        })
        sched._note_quota_exhausted(err)
        held = sched._quota_hold_remaining()
    finally:
        ai_router.reset_endpoint_cooldowns()
    assert 0 < held <= 3600 + 1, (
        f"held {held:.0f}s — must end when the earliest endpoint reopens, not "
        f"when the last one does")
