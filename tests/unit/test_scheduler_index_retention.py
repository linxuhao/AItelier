"""Index-retention diagnostics through the real `_sweep_ended_leases` producer."""
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api import dependencies
from core import scheduler as sc


@pytest.fixture
def sweep(monkeypatch, caplog):
    db, sf = object(), object()
    reconciliation = Mock()
    reaper = Mock(return_value={"retained": [], "removed": []})
    monkeypatch.setattr(sc, "db", db)
    monkeypatch.setattr(dependencies, "get_skillflow", lambda: sf)
    monkeypatch.setattr(sc.run_isolation, "reconcile_all_leases", reconciliation)
    monkeypatch.setattr(sc.run_isolation, "reap_released_worktrees", reaper)
    monkeypatch.setattr(sc, "_last_lease_sweep", 0.0)
    monkeypatch.setattr(sc, "_LEASE_SWEEP_INTERVAL_S", 0.0)
    monkeypatch.setattr(sc, "_lease_sweep_reported", {})
    monkeypatch.setattr(sc, "_worktree_sweep_reported", {})
    monkeypatch.setattr(sc, "_index_sweep_reported", {})
    monkeypatch.setattr(sc, "_get_tick_logger", lambda: logging.getLogger("owned.index.ticks"))
    caplog.set_level(logging.INFO, logger="owned.index.ticks")

    def observe(retained=(), released=()):
        reconciliation.side_effect = None
        reconciliation.return_value = {
            "released": list(released), "retained": [],
            "index_resources": {"requested": [], "retained": list(retained)}}
        before_reap, before_rec = reaper.call_count, reconciliation.call_count
        caplog.clear()
        sc._sweep_ended_leases()
        # One existing reconcile and one existing reap per sweep; nothing extra.
        assert reconciliation.call_count == before_rec + 1
        assert reaper.call_count == before_reap + 1
        reconciliation.assert_called_with(db, sf)
        return [r.getMessage() for r in caplog.records
                if "outcome=index_retained" in r.getMessage()]

    return SimpleNamespace(observe=observe, reconciliation=reconciliation)


def item(run_id, reason="index preparation is pending or unavailable"):
    return {"run_id": run_id, "reason": reason}


A = "samehead-1111-4111-8111-111111111111"
B = "samehead-2222-4222-8222-222222222222"


def test_first_emits_full_id_and_unchanged_repeats_are_quiet(sweep):
    first = sweep.observe([item(A)])
    assert len(first) == 1
    assert f"run={A} " in first[0] and "reason=index preparation is pending" in first[0]
    for _ in range(5):
        assert sweep.observe([item(A)]) == []


def test_same_eight_character_prefix_ids_stay_distinct(sweep):
    assert len(sweep.observe([item(A)])) == 1
    second = sweep.observe([item(A), item(B)])
    assert len(second) == 1 and f"run={B} " in second[0] and A not in second[0]
    assert sweep.observe([item(A), item(B)]) == []
    changed = sweep.observe([item(A), item(B, "index build failed")])
    assert len(changed) == 1 and f"run={B} " in changed[0]


def test_changed_reason_emits_once(sweep):
    sweep.observe([item(A)])
    out = sweep.observe([item(A, "index build failed")])
    assert len(out) == 1 and "reason=index build failed" in out[0]
    assert sweep.observe([item(A, "index build failed")]) == []


def test_numeric_and_long_tail_differences_are_never_normalized(sweep):
    assert len(sweep.observe([item(A, "idle 10s; minimum 60s")])) == 1
    assert len(sweep.observe([item(A, "idle 11s; minimum 60s")])) == 1
    assert len(sweep.observe([item(A, "error code 500 from provider")])) == 1
    assert len(sweep.observe([item(A, "error code 503 from provider")])) == 1
    assert sweep.observe([item(A, "error code 503 from provider")]) == []
    long_reason = "unknown failure\n" + "x" * 300
    first = sweep.observe([item(A, long_reason)])
    assert len(first) == 1 and "\n" not in first[0]
    assert len(first[0].split("reason=", 1)[1]) == 200  # display stays bounded
    assert sweep.observe([item(A, long_reason)]) == []
    # A difference beyond the display limit is still a genuine change.
    assert len(sweep.observe([item(A, long_reason + " tail")])) == 1


def test_absent_ids_retire_and_reappearance_emits(sweep):
    sweep.observe([item(A), item(B)])
    assert sweep.observe([item(B)]) == []
    assert set(sc._index_sweep_reported) == {B}  # bounded by current observation
    again = sweep.observe([item(A), item(B)])
    assert len(again) == 1 and f"run={A} " in again[0]
    assert sweep.observe([]) == []
    assert sc._index_sweep_reported == {}
    assert len(sweep.observe([item(B)])) == 1


def test_released_id_is_retired_and_reappearance_emits(sweep):
    sweep.observe([item(A)])
    assert sweep.observe([], released=[A]) == []
    assert A not in sc._index_sweep_reported
    assert len(sweep.observe([item(A)])) == 1


def test_failed_observation_neither_retires_nor_emits(sweep, caplog):
    assert len(sweep.observe([item(A)])) == 1
    sweep.reconciliation.side_effect = RuntimeError("owned reconcile unavailable")
    caplog.clear()
    sc._sweep_ended_leases()
    assert "lease sweep failed" in caplog.text
    assert not any("index_retained" in r.getMessage() for r in caplog.records)
    assert set(sc._index_sweep_reported) == {A}  # not treated as quiet/disappeared
    assert sweep.observe([item(A)]) == []  # still unchanged afterwards


def test_missing_reason_is_logged_once(sweep):
    assert len(sweep.observe([{"run_id": A, "reason": None}])) == 1
    assert sweep.observe([{"run_id": A, "reason": None}]) == []
