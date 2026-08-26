"""A spent quota must not be re-labelled as a native-tool-calling failure.

`_run_native_step` re-raises a spent-quota error on purpose — there is a long
comment there naming the 2026-08-26 outage it was written to stop. But its
caller in `run_step` wraps the call in a bare `except Exception` and, whenever
the role sets `fallback_to_json_mode`, swallows it and retries in JSON mode.
Every role in `agent_configs/dpe_default.yaml` sets it, so that re-raise never
once reached the scheduler through this path.

The cost is not only the mislabel. The JSON path re-walks the SAME candidate
list, and `_next_usable` deliberately degrades to "try it anyway" when every
endpoint is parked — so it re-pays one real 429 per candidate against endpoints
already known to be spent, on top of the walk the native path just finished.
"""
import pytest

from core.dpe_pipeline import PipelineEngine


class _Factory:
    def __init__(self, fallback: bool):
        self._fallback = fallback

    def is_native(self, _name):                 # every DPE role is native
        return True

    def get_fallback_to_json(self, _name):
        return self._fallback


def _engine(raises, fallback=True):
    """A PipelineEngine reduced to the dispatch path under test."""
    e = object.__new__(PipelineEngine)
    e.factory = _Factory(fallback)
    e.events = []
    e._emit = lambda kind, payload=None: e.events.append(kind)

    def _boom(*a, **k):
        raise raises
    e._run_native_step = _boom
    return e


def _run(e):
    return e.run_step(1, "t_impl", None, agent_config_name="task_implementer")


QUOTA = Exception(
    "litellm.RateLimitError: RateLimitError: OpenAIException - You have "
    "exceeded the 5-hour usage quota. It will reset at 2026-08-26 09:18:28 "
    "+0800 CST.")


def test_a_spent_quota_propagates_even_though_the_role_allows_json_fallback():
    """The scheduler is the only layer that can park on this, and it can only
    see it if the exception object reaches it."""
    e = _engine(QUOTA, fallback=True)
    with pytest.raises(Exception) as ei:
        _run(e)
    assert ei.value is QUOTA, "the quota error was replaced or swallowed"
    assert "native_fallback" not in e.events, (
        "a spent quota was reported as a native-tool-calling failure, and the "
        "JSON path re-walked the same spent endpoints")


def test_an_ordinary_native_failure_still_falls_back():
    """The guard must be narrow: a malformed tool call is exactly what the JSON
    fallback exists for, and stopping the run on it would be a worse bug than
    the one being fixed."""
    e = _engine(ValueError("tool call schema mismatch"), fallback=True)
    with pytest.raises(Exception):
        _run(e)      # the JSON path is not wired up in this reduced engine
    assert "native_fallback" in e.events


def test_the_quota_error_is_recognised_by_the_same_predicate_the_scheduler_uses():
    """Two `is_quota_exhausted` call sites now guard this error; if they ever
    disagree with the scheduler's, the hold silently stops firing."""
    from core.llm_quota import is_quota_exhausted

    assert is_quota_exhausted(QUOTA)
    assert not is_quota_exhausted(ValueError("tool call schema mismatch"))
