"""Spent windows need exhaustion and provider provenance, not window mentions.

The five-hour text is observed Ollama wording. Weekly is a synthetic control,
not a claim about an observed provider response. Wire calls are always patched.
"""
import time

import httpx
import litellm
import pytest

from core.ai_router import endpoint_cooldowns
from core.llm_quota import is_quota_exhausted, quota_reset_at
import core.scheduler as sched
from tests.unit.test_model_routes import _Resp, _plain_transport, gw, wiring  # noqa: F401
from tests.unit.test_poller_does_not_starve_later_projects import (
    _agent_graph, _real_sf, _wire_real_tick,
)

OLLAMA = ("You reached your Pro 5-hour limit. Max is $100/month for $300 of "
          "usage, with no 5-hour or weekly caps: https://ollama.com/settings/billing "
          "(ref: cd6d7215-241b-4c9a-a0c7-3483e253599d)")
WEEKLY_SYNTHETIC = "You reached your Pro weekly limit."
OPENAI_TPM = ("Rate limit reached for gpt-4o in organization org-abc on tokens per "
              "min (TPM): Limit 30000, Used 29000, Requested 2000.")
ANTHROPIC_ITPM = ("This request would exceed the rate limit for your organization "
                  "(a1b2) of 50,000 input tokens per minute.")
BURST_DAILY = ("Rate limit exceeded: 60 requests per minute. Your plan also has a "
               "daily limit of 10000; you have used 120")
BURST_FIVE_HOUR = ("Too many requests per minute. Your 5-hour limit has not been "
                   "reached")
OLD_QUOTA = ("You have exceeded the 5-hour usage quota. It will reset at "
             "2099-01-01 00:00:00 +0000.")


def rate_error(message):
    return litellm.exceptions.RateLimitError(
        message, llm_provider="alpha", model="m-1",
        response=httpx.Response(429, request=httpx.Request("POST", "https://alpha.example/v1")))


@pytest.fixture(autouse=True)
def clear_hold(monkeypatch):
    monkeypatch.setattr(sched, "_QUOTA_HOLD_UNTIL", 0.0)
    monkeypatch.setattr(sched, "_QUOTA_HOLD_REASON", "")


@pytest.mark.parametrize("message", [OLLAMA, WEEKLY_SYNTHETIC], ids=["observed-ollama", "synthetic-weekly"])
def test_first_window_error_fails_over_and_next_gateway_skips(wiring, monkeypatch, message):
    calls = []

    def wire(**kwargs):
        calls.append(kwargs.get("api_key"))
        if kwargs.get("api_key") == "alpha-secret":
            raise rate_error(message)
        return _Resp()

    monkeypatch.setattr(litellm, "completion", wire)
    g = gw(wiring, "model_a")
    g.generate_native([{"role": "user", "content": "hi"}])
    assert calls == ["alpha-secret", "beta-secret"]
    assert g.active_model == "beta/m-1"
    assert quota_reset_at(message) is None
    assert endpoint_cooldowns() == {"alpha/m-1": pytest.approx(time.time() + 300, abs=5)}
    assert gw(wiring, "model_a").active_model == "beta/m-1"


@pytest.mark.parametrize("message", [OPENAI_TPM, ANTHROPIC_ITPM, BURST_DAILY, BURST_FIVE_HOUR])
def test_burst_retries_same_endpoint_and_does_not_park(wiring, monkeypatch, message):
    calls = []

    def wire(**kwargs):
        calls.append(kwargs.get("api_key"))
        if len(calls) == 1:
            raise rate_error(message)
        return _Resp()

    monkeypatch.setattr(litellm, "completion", wire)
    g = gw(wiring, "model_a")
    g.generate_native([{"role": "user", "content": "hi"}])
    assert calls == ["alpha-secret", "alpha-secret"]
    assert g.active_model == "alpha/m-1"
    assert endpoint_cooldowns() == {}
    assert gw(wiring, "model_a").active_model == "alpha/m-1"


@pytest.mark.parametrize("message, expected", [
    (OLLAMA, True), (WEEKLY_SYNTHETIC, True), (OLD_QUOTA, True),
    (OPENAI_TPM, False), (ANTHROPIC_ITPM, False),
    (BURST_DAILY, False), (BURST_FIVE_HOUR, False),
    ("monthly limit must be positive", False),
    ("Command failed: grep 'weekly limit' src/billing.py returned 1", False),
    ("429 slow down (per-minute throttle). hourly limit remaining: 87%", False),
])
def test_message_meaning(message, expected):
    assert is_quota_exhausted(message) is expected


@pytest.mark.parametrize("error, spent", [
    (ValueError("monthly limit must be positive"), False),
    (RuntimeError("Test failed: assert user hit weekly limit"), False),
    (RuntimeError("Test failed: daily limit not enforced for user"), False),
    (RuntimeError("You reached your Pro weekly limit."), False),
    (RuntimeError("RateLimitError: You reached your Pro weekly limit."), False),
    (rate_error(BURST_DAILY), False),
    (rate_error(BURST_FIVE_HOUR), False),
    (rate_error(OLLAMA), True),
    (rate_error(WEEKLY_SYNTHETIC), True),
    (Exception(OLD_QUOTA), True),  # preserve the existing quota/reset branch
], ids=["monthly-validation", "weekly-test-failure", "daily-test-failure",
        "weekly-app-message", "spoofed-class-in-app-message", "burst-daily",
        "burst-five-hour", "observed-ollama", "synthetic-weekly", "existing-quota"])
async def test_actual_scheduler_error_path_holds_only_spent_windows(tmp_path, monkeypatch, error, spent):
    sf = _real_sf(tmp_path)
    sf.register_graph(_agent_graph())
    run = sf.get_or_create_run("starve_work_t", "quota-control", {})
    sf.start_run(run)
    executed = []

    async def execute(claimed):
        executed.append(claimed.step_id)
        raise error

    _wire_real_tick(monkeypatch, sf, {"quota-control": run}, execute)
    monkeypatch.setattr(sched, "_odbg", lambda *a: None)
    outcomes = []
    monkeypatch.setattr(sched, "tick_log", lambda pid, outcome, **kw: outcomes.append(outcome))
    await sched._run_skillflow_tick("quota-control", None)
    assert executed == ["a"], "the production tick must reach the failing executor"
    assert ("quota_exhausted" in outcomes) is spent
    remaining = sched._quota_hold_remaining()
    if spent:
        expected = 6 * 3600 if error.args and "2099" in str(error) else 300
        assert remaining == pytest.approx(expected, abs=5)
    else:
        assert remaining == 0.0
    assert sf.get_run(run)["status"] == "running"
    rows = sf.trace_query(run, "SELECT status, retry_count FROM skillflow_steps WHERE run_id = ?", (run,))
    assert [tuple(row) for row in rows] == [("pending", 1)]
