"""A cancelled step driver must hand its claim back.

Every driven step loop wraps execute+confirm in `except Exception` so a failing
step is failed rather than left claimed. `asyncio.CancelledError` is a
BaseException (3.8+), so that clause never saw it: a client disconnect, a
request timeout, or an ended session unwound straight past it and the step
stayed `claimed` with nothing recorded.

Nothing else recovers that. skillflow's reaper refuses to reclaim a claim whose
owner PROCESS is alive, and the owner of a butler-driven step is the server
itself. Live 2026-08-29, jinyong-touch step `2`: the agent had already finished
— `finish_step` returned completed and both outputs were staged — when its
driver went away. 1105 consecutive `active_claim` ticks, 80+ minutes, work done.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from core.run_driver import release_claim_on_cancel


@pytest.fixture
def claimed():
    c = MagicMock()
    c.step_id = "2"
    c.token = object()
    return c


def test_the_claim_goes_back_without_spending_a_retry(claimed):
    """`release_claim`, not `fail_step`. The step did not fail — its executor
    went away — and `fail_step(retryable=True)` increments retry_count, so three
    cancellations would kill a healthy step with an error blaming it for what
    the client did."""
    sf = MagicMock()
    release_claim_on_cancel(sf, claimed)

    sf.release_claim.assert_called_once()
    assert sf.release_claim.call_args.args[0] is claimed.token
    sf.fail_step.assert_not_called()


def test_an_engine_without_release_claim_still_gets_the_claim_back(claimed):
    """The container tracks PyPI while the host runs an editable checkout, so
    the engine can be older than this call. Handing the claim back matters more
    than handing it back cheaply."""
    sf = MagicMock(spec=["fail_step"])          # no release_claim attribute
    release_claim_on_cancel(sf, claimed)

    sf.fail_step.assert_called_once()
    assert sf.fail_step.call_args.kwargs["retryable"] is True


def test_a_failing_release_is_reported_not_raised(claimed, caplog):
    """It runs while a cancellation is propagating; raising here would replace
    the cancellation with a less useful error. But silence would hide a claim
    that is now stuck until the process restarts."""
    sf = MagicMock()
    sf.release_claim.side_effect = RuntimeError("db gone")

    with caplog.at_level("WARNING", logger="aitelier"):
        release_claim_on_cancel(sf, claimed)     # must not raise

    assert "stay claimed" in caplog.text


async def _drive(loop_body):
    """Run a step loop shaped like the three real ones and cancel it mid-step."""
    task = asyncio.create_task(loop_body())
    await asyncio.sleep(0)          # let it reach the await
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_real_loop_shape_releases_and_still_cancels(claimed):
    """The two halves that both matter: the claim comes back, AND the
    cancellation still propagates. Swallowing it would be its own bug — the
    caller would believe the driver finished."""
    sf = MagicMock()
    released = []
    sf.release_claim.side_effect = lambda *a, **k: released.append(a[0])

    async def body():
        try:
            await asyncio.Event().wait()          # the step, never finishing
            sf.confirm_step(claimed.token, None)
        except asyncio.CancelledError:
            release_claim_on_cancel(sf, claimed)
            raise
        except Exception:                          # the pre-existing clause
            sf.fail_step(claimed.token, "boom", retryable=True)

    await _drive(body)

    assert released == [claimed.token], "the claim was not handed back"
    sf.confirm_step.assert_not_called()


async def test_the_bare_except_exception_loop_is_what_leaked(claimed):
    """The control: the shape before the fix leaks the claim. Without this the
    test above would pass on a build where CancelledError never reached the
    handler at all."""
    sf = MagicMock()

    async def body():
        try:
            await asyncio.Event().wait()
        except Exception:
            sf.fail_step(claimed.token, "boom", retryable=True)

    await _drive(body)

    sf.fail_step.assert_not_called()
