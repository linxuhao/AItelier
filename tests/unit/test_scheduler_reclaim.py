"""Regression test: the 30s supervisor loop must RECLAIM, not just complain.

skillflow's reaper was reachable from exactly one place — the top of
advance_run — which `_run_skillflow_tick` returns before reaching on five paths.
With one project per tick, a single project that merely looked in-flight meant
nothing was swept anywhere in the system that tick, and the only cure left was
restarting the container (three times on 2026-08-22). `_check_hung_claims`
already ran on its own 30s job for exactly the right reason; it just had no
authority. These tests pin the authority, and pin that it is not indiscriminate:
a step that is still heartbeating is warned about, never reaped.
"""

import datetime as _dt
import logging
from pathlib import Path

import pytest
from skillflow import PipelineGraph, SkillFlow

from core import scheduler


def _graph(timeout_seconds: int = 30):
    return PipelineGraph._from_dict({
        "name": "reclaim_t",
        "begin": "a",
        "end_conditions": {"combinator": "or",
                           "conditions": [{"type": "step_complete", "step": "a"}]},
        "steps": [{"id": "a", "step_type": "agent", "agent_config": "x",
                   "timeout_seconds": timeout_seconds}],
    })


@pytest.fixture
def claimed(tmp_path, monkeypatch):
    """A real SkillFlow with one really-claimed agent step.

    Real, not a mock: "was it reset to pending" is a claim about a row, and a
    MagicMock would happily assert that skillflow was *called* while proving
    nothing about what it did.
    """
    sf = SkillFlow(str(tmp_path / "sf.db"),
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "proj"),
                   stale_threshold_seconds=180)
    sf.register_agent_config("x", model="m")
    sf.register_graph(_graph())
    run_id = sf.get_or_create_run("reclaim_t", "p1", {})
    sf.start_run(run_id)
    sf.advance_run(run_id)
    assert sf.claim_next_step(run_id) is not None
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    return sf, run_id


def _backdate(sf, *, silent_s: int, claimed_s: int):
    """Push the claim's activity clock and start clock into the past.

    They are separate on purpose: the reaper reads `updated_at` (silence) and
    the hung warning reads `claimed_at` (elapsed), and the whole point of the
    two-signal split is that they can disagree.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    silent = (now - _dt.timedelta(seconds=silent_s)).strftime("%Y-%m-%d %H:%M:%S")
    started = (now - _dt.timedelta(seconds=claimed_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sf._lock:
        sf._conn.execute(
            "UPDATE skillflow_steps SET updated_at = ?, claimed_at = ? "
            "WHERE status = 'claimed'", (silent, started))
        sf._conn.commit()


def _status(sf):
    return sf._conn.execute(
        "SELECT status FROM skillflow_steps LIMIT 1").fetchone()["status"]


async def test_silent_claim_is_reset_to_pending(claimed, caplog):
    """Past the reclaim threshold: actually reaped, by the 30s loop alone.

    Nothing here touches the scheduler tick — that is the point. advance_run is
    never called, and the claim still goes back to pending.
    """
    sf, _ = claimed
    _backdate(sf, silent_s=400, claimed_s=400)

    with caplog.at_level(logging.WARNING, logger="aitelier.scheduler"):
        await scheduler._check_hung_claims()

    assert _status(sf) == "pending"          # reap ≠ fail
    assert "RECLAIMED stale claim" in caplog.text


async def test_reclaim_is_logged_to_the_tick_log(claimed, monkeypatch):
    """The reclaim lands in scheduler_ticks.log with the other outcomes."""
    sf, _ = claimed
    _backdate(sf, silent_s=400, claimed_s=400)
    seen = []
    monkeypatch.setattr(scheduler, "tick_log",
                        lambda pid, outcome, **d: seen.append((pid, outcome, d)))

    await scheduler._check_hung_claims()

    assert [o for _, o, _ in seen] == ["reclaimed"]
    assert seen[0][0] == "p1"                # project, not run id


async def test_heartbeating_claim_is_warned_about_not_reaped(claimed, caplog):
    """Hung but under the reclaim threshold: the early signal must survive.

    Claimed for 400s (> 30s timeout × 3) but silent for only 5s, so it is slow,
    not dead. Losing this warning would be the regression.
    """
    sf, _ = claimed
    _backdate(sf, silent_s=5, claimed_s=400)

    with caplog.at_level(logging.WARNING, logger="aitelier.scheduler"):
        await scheduler._check_hung_claims()

    assert _status(sf) == "claimed"
    assert "Step may be hung" in caplog.text
    assert "RECLAIMED" not in caplog.text


async def test_healthy_claim_is_left_entirely_alone(claimed, caplog):
    sf, _ = claimed
    _backdate(sf, silent_s=5, claimed_s=5)

    with caplog.at_level(logging.WARNING, logger="aitelier.scheduler"):
        await scheduler._check_hung_claims()

    assert _status(sf) == "claimed"
    assert caplog.text == ""


async def test_the_scan_query_matches_the_real_schema(claimed, caplog):
    """`step_instance_id` is a skillflow_trace column, not a skillflow_steps one.

    Selecting it by that name from skillflow_steps raised OperationalError on
    every run, and the surrounding `except: continue` swallowed it — so the
    warning path and the orphan forensic snapshot were both dead code. If that
    ever comes back, the warning above disappears silently again.
    """
    sf, _ = claimed
    _backdate(sf, silent_s=5, claimed_s=400)

    with caplog.at_level(logging.WARNING, logger="aitelier.scheduler"):
        await scheduler._check_hung_claims()

    assert "hung-claim scan failed" not in caplog.text
    assert "Step may be hung" in caplog.text
