"""What `hold_blocks_advance(sf=...)` and `observe_run` read from the run itself.

* The host's hold notes the run's LATEST report before it answers. A
  re-acquisition that came back silent again writes a new report; the host
  must start a new wait from it, or a second driver advances the run straight
  back into the gate (review gnr2 R5: deleting that branch was noticed by no
  test over 237 executions).
* The absence gate's edge back to `test` carries a `max_loop` the engine
  counts over the whole run. Once it is spent, the run ends naming the
  absence; the engine's "cycle limit exceeded" is never reached.

The `sf` here is a stand-in with the three readers `gate_deferral` uses: the
run row (its current node), the trace query that finds the `test` step's
report, and the read connection that holds `skillflow_edge_counts`.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3

from core import gate_deferral


class _Run:
    def __init__(self, out_dir, *, laps=None, max_loop=100,
                 node=gate_deferral.ABSENCE_GATE):
        self.out_dir = out_dir
        self.node = node
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE skillflow_edge_counts (run_id TEXT, from_step TEXT, "
            "to_step TEXT, count INTEGER, max_loop INTEGER)")
        if laps is not None:
            self.conn.execute(
                "INSERT INTO skillflow_edge_counts VALUES (?, ?, ?, ?, ?)",
                ("r1", gate_deferral.ABSENCE_GATE, gate_deferral.ABSENCE_GATE_TARGET,
                 laps, max_loop))

    def get_run(self, run_id):
        return {"run_id": run_id, "status": "running", "current_node": self.node}

    def trace_query(self, run_id, sql, params):
        payload = {"params": {"step_name": "test", "out_dir": str(self.out_dir)}}
        return [("test", json.dumps(payload))]

    @contextlib.contextmanager
    def _ro(self):
        yield self.conn


def _silent_report(out_dir, mtime):
    path = out_dir / "test_report.json"
    path.write_text(json.dumps({"passed": False, "repo_gate_absent": True,
                                "repo_gate_unmeasured": True,
                                "repo_gate": {"script": "run_tests.sh"}}))
    os.utime(path, (mtime, mtime))
    return path


def test_the_host_starts_a_new_wait_from_a_new_silent_report(tmp_path):
    ledger = gate_deferral.DeferralLedger()
    sf = _Run(tmp_path)
    wait = gate_deferral.wait_seconds()
    t0 = 1_000_000.0
    _silent_report(tmp_path, 1_000)
    assert gate_deferral.observe_run(sf, "r1", now=t0, ledger=ledger)["state"] == "silent"
    after = t0 + wait + 1
    assert gate_deferral.observe_run(sf, "r1", now=after, ledger=ledger)["state"] == "due"

    # The re-acquisition ran and came back silent: a new report.
    _silent_report(tmp_path, 2_000)
    assert gate_deferral.hold_blocks_advance("r1", now=after + 1, ledger=ledger,
                                             sf=sf) is True
    # Nothing of it is visible to a host that does not read the run.
    fresh = gate_deferral.DeferralLedger()
    _silent_report(tmp_path, 1_000)
    gate_deferral.observe_run(sf, "r1", now=t0, ledger=fresh)
    _silent_report(tmp_path, 2_000)
    assert gate_deferral.hold_blocks_advance("r1", now=after + 1,
                                             ledger=fresh) is False


def test_the_host_does_not_restart_the_wait_on_the_same_report(tmp_path):
    ledger = gate_deferral.DeferralLedger()
    sf = _Run(tmp_path)
    t0 = 1_000_000.0
    _silent_report(tmp_path, 1_000)
    gate_deferral.observe_run(sf, "r1", now=t0, ledger=ledger)
    after = t0 + gate_deferral.wait_seconds() + 1
    assert gate_deferral.hold_blocks_advance("r1", now=after, ledger=ledger,
                                             sf=sf) is False


def test_spent_laps_end_the_run_naming_the_absence(tmp_path):
    ledger = gate_deferral.DeferralLedger()
    sf = _Run(tmp_path, laps=100, max_loop=100)
    t0 = 1_000_000.0
    _silent_report(tmp_path, 1_000)
    assert gate_deferral.observe_run(sf, "r1", now=t0, ledger=ledger)["state"] == "silent"
    seen = gate_deferral.observe_run(
        sf, "r1", now=t0 + gate_deferral.wait_seconds() + 1, ledger=ledger)
    assert seen["state"] == "expired"
    assert gate_deferral.absent_terminal_names_no_failure(seen["reason"])
    assert "cycle limit" not in seen["reason"].lower()
    # A second driver may not advance it into the engine's limit either.
    assert gate_deferral.hold_blocks_advance(
        "r1", now=t0 + gate_deferral.wait_seconds() + 2, ledger=ledger, sf=sf)


def test_a_lap_left_is_still_due(tmp_path):
    ledger = gate_deferral.DeferralLedger()
    sf = _Run(tmp_path, laps=99, max_loop=100)
    t0 = 1_000_000.0
    _silent_report(tmp_path, 1_000)
    gate_deferral.observe_run(sf, "r1", now=t0, ledger=ledger)
    seen = gate_deferral.observe_run(
        sf, "r1", now=t0 + gate_deferral.wait_seconds() + 1, ledger=ledger)
    assert seen["state"] == "due"
    assert gate_deferral.absence_laps_spent(sf, "r1") is False


def test_the_last_lap_granted_is_run(tmp_path):
    """The traversal that spends the last lap has already happened when the
    run stands at `test`: that gate run is due, not ended."""
    ledger = gate_deferral.DeferralLedger()
    sf = _Run(tmp_path, laps=100, max_loop=100, node="test")
    t0 = 1_000_000.0
    _silent_report(tmp_path, 1_000)
    gate_deferral.observe_run(sf, "r1", now=t0, ledger=ledger)
    seen = gate_deferral.observe_run(
        sf, "r1", now=t0 + gate_deferral.wait_seconds() + 1, ledger=ledger)
    assert seen["state"] == "due"
    assert gate_deferral.absence_laps_spent(sf, "r1") is False


def test_laps_are_read_from_the_engine_counter(tmp_path):
    assert gate_deferral.absence_laps_spent(_Run(tmp_path), "r1") is False
    assert gate_deferral.absence_laps_spent(_Run(tmp_path, laps=3, max_loop=3), "r1")
    assert not gate_deferral.absence_laps_spent(_Run(tmp_path, laps=2, max_loop=3), "r1")
    assert not gate_deferral.absence_laps_spent(_Run(tmp_path, laps=5, max_loop=None), "r1")
    assert gate_deferral.absence_laps_spent(None, "r1") is False
