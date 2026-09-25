"""The absence gate's lap limit at a 60 s wait, on whichever tree is first on
PYTHONPATH (the gnr2 reviewer's D2 drive, `probes/test_attack2_drive.py`,
at the r3 setting).

The REAL `configs/coding_impl.yaml`, host, `run_tests`, scheduler tick and
harness admission code (`tests/gate_fixture.py`). The harness is held for the
whole drive: the holder's release is an Event whose wait has no 120 s cap, so
the round's gate is refused on every lap. Wait 60 s, the default 3 h ceiling,
the clock moved 30 s per tick: 100 laps of about 90 s fit inside the ceiling,
so the edge's `max_loop: 100` is reached first.

Run from the tree root: `python -m pytest -q -s <this file>`. It prints one
`DRIVE` line per variant and asserts nothing; the log is the measurement.
"""
import json
import os
import tempfile
import threading

os.environ.setdefault("AITELIER_HOME", tempfile.mkdtemp(prefix="gn3-lap-home-"))

from core import gate_deferral  # noqa: E402
from tests.gate_fixture import GATE_PY, HarnessRig  # noqa: E402
from tests.skillflow.test_coding_impl_gate_absence import _drive, _wire  # noqa: E402

_TAIL = "exec python3 - <<'PYGATE'\n" + GATE_PY + "\nPYGATE\n"


class _HeldUntilSet(threading.Event):
    def wait(self, timeout=None):
        return super().wait(None)


def _lap_drive(tmp_path, monkeypatch, name, *, second_driver):
    rig = HarnessRig(tmp_path / "harness", monkeypatch)
    rig.release = _HeldUntilSet()
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    monkeypatch.setenv("FIXTURE_GATE_CLIENT_TIMEOUT", "60")
    monkeypatch.setenv("FIXTURE_GATE_OP", "coding-impl-gate")
    monkeypatch.setenv("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS", "0.3")
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=10800, wait=60,
                       counter=counter, tail=_TAIL)
    rig.hold()
    try:
        runs, statuses, outcomes, nodes = _drive(
            sf, run_id, monkeypatch, ticks=400, clock_step=30,
            second_driver=second_driver)
    finally:
        rig.close()
    run = sf.get_run(run_id)
    with sf._ro() as conn:
        edge = conn.execute(
            "SELECT count, max_loop FROM skillflow_edge_counts WHERE run_id=? "
            "AND from_step='test_gate_absent' AND to_step='test'", (run_id,)).fetchone()
    print("DRIVE " + json.dumps({
        "probe": name, "tree": gate_deferral.__file__,
        "wait_seconds": gate_deferral.wait_seconds(),
        "episode_max_seconds": gate_deferral.episode_max_seconds(),
        "implement_runs": runs, "final_status": run["status"],
        "final_node": run.get("current_node"), "error_reason": run.get("error_reason"),
        "edge_test_gate_absent_to_test": list(edge) if edge else None,
        "gate_calls": int(counter.read_text()) if counter.exists() else 0,
        "outcome_counts": {k: outcomes.count(k) for k in sorted(set(outcomes))},
        "last_outcomes": outcomes[-4:], "ticks": len(outcomes),
        "rendered_for": sorted(set(rig.rendered))}))


def test_lap_limit_tick_only(tmp_path, monkeypatch):
    _lap_drive(tmp_path, monkeypatch, "laps_wait60_tick_only", second_driver=False)


def test_lap_limit_with_a_second_driver(tmp_path, monkeypatch):
    _lap_drive(tmp_path, monkeypatch, "laps_wait60_second_driver", second_driver=True)
