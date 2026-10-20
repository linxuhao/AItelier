"""Observe real scheduler maintenance reporting without touching live worktrees."""
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api import dependencies
from core import scheduler as sc


@pytest.fixture
def sweep(monkeypatch, caplog):
    db, sf = object(), object()
    reconciliation = Mock(return_value={"released": [], "retained": [], "index_resources": {"retained": []}})
    reaper = Mock()
    monkeypatch.setattr(sc, "db", db)
    monkeypatch.setattr(dependencies, "get_skillflow", lambda: sf)
    monkeypatch.setattr(sc.run_isolation, "reconcile_all_leases", reconciliation)
    monkeypatch.setattr(sc.run_isolation, "reap_released_worktrees", reaper)
    monkeypatch.setattr(sc, "_last_lease_sweep", 0.0)
    monkeypatch.setattr(sc, "_LEASE_SWEEP_INTERVAL_S", 0.0)
    monkeypatch.setattr(sc, "_lease_sweep_reported", {})
    monkeypatch.setattr(sc, "_worktree_sweep_reported", {})
    monkeypatch.setattr(sc, "_get_tick_logger", lambda: logging.getLogger("owned.retention.ticks"))
    caplog.set_level(logging.INFO, logger="owned.retention.ticks")

    def observe(retained=(), removed=()):
        reaper.return_value = {"retained": list(retained), "removed": list(removed)}
        before_reap, before_reconcile = reaper.call_count, reconciliation.call_count
        caplog.clear()
        sc._sweep_ended_leases()
        assert reaper.call_count == before_reap + 1
        assert reconciliation.call_count == before_reconcile + 1
        reaper.assert_called_with(db, sf)
        reconciliation.assert_called_with(db, sf)
        return [record.getMessage() for record in caplog.records]

    return SimpleNamespace(observe=observe, reaper=reaper)


def test_normal_sweep_reports_first_changed_distinct_and_retired_causes(sweep):
    # Deliberately share eight characters: a display prefix is not a cache key.
    one = {"run_id": "samehead-1111-4111-8111-111111111111", "reason": "unclassified ignored files retained; they may contain evidence"}
    two = {"run_id": "samehead-2222-4222-8222-222222222222", "reason": one["reason"]}
    first = sweep.observe([one])
    assert len(first) == 1 and "outcome=worktree_retained" in first[0]
    assert "run=" + one["run_id"] in first[0] and "reason=" + one["reason"] in first[0]
    assert sweep.observe([one]) == []
    changed = {**one, "reason": "semantic release is not positively acknowledged"}
    update = sweep.observe([changed, two])
    assert len(update) == 2 and all("outcome=worktree_retained" in line for line in update)
    assert any("run=" + changed["run_id"] in line and changed["reason"] in line for line in update)
    assert any("run=" + two["run_id"] in line and two["reason"] in line for line in update)
    assert sweep.observe([changed, two]) == []
    removed = sweep.observe([two], [{"run_id": one["run_id"], "reason": "removed owned fixture; branch and commits preserved"}])
    assert removed == ["project=- outcome=worktree_reaped run=samehead reason=removed owned fixture; branch and commits preserved"]
    assert len(sweep.observe([changed, two])) == 1  # Reappearance after removal is new.
    assert sweep.observe() == []  # Disappearing from retained need not mean removed.
    assert len(sweep.observe([two])) == 1  # Reappearance after absence is also new.


def test_retention_messages_are_bounded_but_changes_after_display_limit_are_observed(sweep):
    reason = "unknown ownership\n" + "x" * 300
    item = {"run_id": "bounded-owned-run", "reason": reason}
    first = sweep.observe([item])
    assert len(first) == 1 and "\n" not in first[0] and "\r" not in first[0]
    assert len(first[0].split("reason=", 1)[1]) == 200
    assert sweep.observe([item]) == []
    assert len(sweep.observe([{**item, "reason": reason + " changed"}])) == 1


def test_idle_elapsed_only_repeats_are_coalesced_but_useful_changes_are_logged(sweep):
    def idle(run_id, elapsed, minimum=86400):
        return {"run_id": run_id, "reason": f"idle {elapsed}s; minimum {minimum}s"}

    a, b = "idle-run-aaaa-1111", "idle-run-bbbb-2222"
    first = sweep.observe([idle(a, 100)])
    assert len(first) == 1 and "reason=idle 100s; minimum 86400s" in first[0]
    assert sweep.observe([idle(a, 160)]) == []  # elapsed-only change
    assert sweep.observe([idle(a, 9000)]) == []
    changed_min = sweep.observe([idle(a, 9060, 172800)])  # minimum changed
    assert len(changed_min) == 1 and "minimum 172800s" in changed_min[0]
    assert sweep.observe([idle(a, 9120, 172800)]) == []
    # A distinct run with the same cause shape is its own first observation.
    assert len(sweep.observe([idle(a, 9180, 172800), idle(b, 5, 172800)])) == 1
    # Leaving idle and re-entering it are both visible.
    other = {"run_id": a, "reason": "unclassified ignored files retained; they may contain evidence"}
    assert len(sweep.observe([other, idle(b, 65, 172800)])) == 1
    assert len(sweep.observe([idle(a, 1, 172800), idle(b, 125, 172800)])) == 1
    # Retirement and reappearance remain new observations.
    assert sweep.observe([idle(b, 185, 172800)]) == []
    assert len(sweep.observe([idle(a, 2, 172800), idle(b, 245, 172800)])) == 1


def test_numbers_in_non_idle_or_inexact_reasons_are_never_stripped(sweep):
    run = "numeric-run-0001"
    assert len(sweep.observe([{"run_id": run, "reason": "cannot establish inactivity: clock 10 skew"}])) == 1
    assert len(sweep.observe([{"run_id": run, "reason": "cannot establish inactivity: clock 11 skew"}])) == 1
    assert sweep.observe([{"run_id": run, "reason": "cannot establish inactivity: clock 11 skew"}]) == []
    assert len(sweep.observe([{"run_id": run, "reason": "3 host write admissions have not retired"}])) == 1
    assert len(sweep.observe([{"run_id": run, "reason": "4 host write admissions have not retired"}])) == 1
    # Near-miss shapes of the idle reason keep their full text as the key.
    assert len(sweep.observe([{"run_id": run, "reason": "idle 10s; minimum 60s extra 1"}])) == 1
    assert len(sweep.observe([{"run_id": run, "reason": "idle 11s; minimum 60s extra 1"}])) == 1
    assert len(sweep.observe([{"run_id": run, "reason": "idle 11s; minimum 60s"}])) == 1
    assert len(sweep.observe([{"run_id": run, "reason": "idle 11.5s; minimum 60s"}])) == 1
    assert len(sweep.observe([{"run_id": run, "reason": "idle 12.5s; minimum 60s"}])) == 1



def test_failed_reap_is_not_a_successful_absence_observation(sweep, caplog):
    item = {"run_id": "owned-unknown-run", "reason": "source Git worktree inventory is unavailable"}
    assert len(sweep.observe([item])) == 1
    sweep.reaper.side_effect = RuntimeError("owned reaper observation unavailable")
    caplog.clear()
    sc._sweep_ended_leases()
    assert "worktree reap sweep failed" in caplog.text
    assert not any("outcome=worktree_reaped" in record.getMessage() for record in caplog.records)
    sweep.reaper.side_effect = None
    assert sweep.observe([item]) == []


def test_test_and_scheduler_import_the_owned_source():
    root = Path(__file__).resolve().parents[2]
    assert Path(sc.__file__).resolve() == root / "core/scheduler.py"
    assert root == Path("/src")
