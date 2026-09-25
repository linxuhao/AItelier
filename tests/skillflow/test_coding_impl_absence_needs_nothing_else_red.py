# tests/skillflow/test_coding_impl_absence_needs_nothing_else_red.py
#
# Two ways a run ended "gate did not run" on the r2 candidate although that
# was not the whole story (review gnr2, 2026-09-25), driven on the REAL
# `configs/coding_impl.yaml`, host, `run_tests` and scheduler tick (the wiring
# and the driver of `test_coding_impl_gate_absence.py`):
#
#   * pole 4 — the round's pytest is red and the shared gate is busy. The red
#     was measured, so the run goes back to `implement`; it does not park at
#     the absence gate.
#   * the absence gate's own lap limit — `test_gate_absent -> test` carries
#     `max_loop: 100`, counted over the whole run. With a 60 s wait the laps
#     run out before the default 3 h ceiling, and the run ended on the
#     engine's "Gate 'test_gate_absent': cycle limit exceeded". It must end on
#     the absence sentence instead.
#
# And one shape the director ruled on (rev 4, 2026-09-25): the round's pytest
# is killed at its wall and the shared gate is busy. pytest's own summary says
# "NOTHING was measured", so nothing was measured anywhere: the run waits at
# the absence gate and spends no implement cycle.
import gc
import json

from core import gate_deferral
from skillflow.output_targets import git
from skillflow.tool_loader import ToolLoader
from tests.gate_fixture import GATE_PY, HarnessRig
from tests.skillflow.test_coding_impl_gate_absence import (
    _SILENT_TAIL, _drive, _gate_calls, _wire)

_GATE_TAIL = "exec python3 - <<'PYGATE'\n" + GATE_PY + "\nPYGATE\n"


def test_pole_4_a_pytest_red_behind_a_busy_gate_goes_back_to_implement(
        tmp_path, monkeypatch):
    rig = HarnessRig(tmp_path / "harness", monkeypatch)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    monkeypatch.setenv("FIXTURE_GATE_CLIENT_TIMEOUT", "30")
    monkeypatch.setenv("FIXTURE_GATE_OP", "coding-impl-gate")
    monkeypatch.setenv("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS", "0.3")
    counter = tmp_path / "calls.txt"
    try:
        sf, run_id = _wire(tmp_path, monkeypatch, episode_max=30, wait=5,
                           counter=counter, tail=_GATE_TAIL)
        rig.hold()
        project = sf._workspace.get_project_code_path("p", run_id=run_id)
        (project / "tests" / "test_bad.py").write_text(
            "def test_bad():\n    assert 1 == 2\n")
        git(project, "add", "tests/test_bad.py")
        git(project, "commit", "-qm", "fixture: a measured pytest red")
        implement_runs, statuses, outcomes, nodes = _drive(
            sf, run_id, monkeypatch, ticks=80, clock_step=1)
        report = json.loads(gate_deferral.find_test_report(sf, run_id).read_text())
    finally:
        rig.close()
    run = sf.get_run(run_id)
    print("POLE4 " + json.dumps({
        "implement_runs": implement_runs, "final_status": run["status"],
        "error_reason": run.get("error_reason"),
        "gate_calls": _gate_calls(counter), "outcomes": outcomes,
        "report": {k: report.get(k) for k in (
            "passed", "repo_gate_absent", "repo_gate_unmeasured",
            "evidence_state", "failures")}}, indent=1))

    assert implement_runs > 1, implement_runs
    assert "silent" not in outcomes and "expired" not in outcomes, outcomes
    assert "test_gate_absent" not in nodes, nodes
    assert gate_deferral.ABSENCE_TERMINAL not in (run.get("error_reason") or "")
    assert report["repo_gate_absent"] is False
    assert report["repo_gate_unmeasured"] is True
    assert any("test_bad" in f for f in report["failures"])


def _loaded_run_tests(sf):
    """The `run_tests` function the host's ToolLoader loaded, as the graph runs it."""
    loaders = [v for v in vars(sf).values() if isinstance(v, ToolLoader)]
    if not loaders:
        loaders = [o for o in gc.get_referents(*vars(sf).values())
                   if isinstance(o, ToolLoader)]
    assert len(loaders) == 1, (len(loaders), sorted(vars(sf)))
    fn = loaders[0].load_fn("run_tests")
    assert fn.__globals__["__file__"].endswith("aitelier/tools/run_tests/impl.py")
    return fn


def test_a_pytest_wall_behind_a_busy_gate_waits_at_the_absence_gate(
        tmp_path, monkeypatch):
    rig = HarnessRig(tmp_path / "harness", monkeypatch)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    monkeypatch.setenv("FIXTURE_GATE_CLIENT_TIMEOUT", "30")
    monkeypatch.setenv("FIXTURE_GATE_OP", "coding-impl-gate")
    monkeypatch.setenv("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS", "0.3")
    counter = tmp_path / "calls.txt"
    try:
        sf, run_id = _wire(tmp_path, monkeypatch, episode_max=30, wait=5,
                           counter=counter, tail=_GATE_TAIL)
        rig.hold()
        project = sf._workspace.get_project_code_path("p", run_id=run_id)
        (project / "tests" / "test_slow.py").write_text(
            "import time\n\ndef test_slow():\n    time.sleep(30)\n")
        git(project, "add", "tests/test_slow.py")
        git(project, "commit", "-qm", "fixture: a suite that outlives the pytest wall")
        fn = _loaded_run_tests(sf)
        monkeypatch.setitem(fn.__globals__, "PYTEST_WALL_SECONDS", 2)
        implement_runs, statuses, outcomes, nodes = _drive(
            sf, run_id, monkeypatch, ticks=80, clock_step=1)
        report = json.loads(gate_deferral.find_test_report(sf, run_id).read_text())
    finally:
        rig.close()
    run = sf.get_run(run_id)
    reason = run.get("error_reason") or ""
    print("PYTEST_WALL " + json.dumps({
        "implement_runs": implement_runs, "final_status": run["status"],
        "error_reason": reason, "gate_calls": _gate_calls(counter),
        "outcomes": outcomes,
        "report": {k: report.get(k) for k in (
            "passed", "timed_out", "skipped_because", "repo_gate_absent",
            "repo_gate_unmeasured", "evidence_state", "failures")}}, indent=1))

    assert implement_runs == 1, implement_runs
    assert "test_gate_absent" in nodes, nodes
    assert run["status"] == "failed", statuses[-3:]
    assert "cycle limit" not in reason.lower(), reason
    assert reason.startswith(gate_deferral.ABSENCE_TERMINAL), reason
    assert report["timed_out"] is True
    assert report["repo_gate_absent"] is True
    assert any(f.startswith("pytest:timed out") for f in report["failures"])


def test_the_absence_gate_laps_run_out_naming_the_absence(tmp_path, monkeypatch):
    """Wait 60 s, the default 3 h ceiling, a gate that never answers: 100 laps
    of 90 s fit inside the ceiling, so the edge's limit is what ends the run."""
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=10800, wait=60,
                       counter=counter, tail=_SILENT_TAIL)
    assert gate_deferral.wait_seconds() == 60
    assert gate_deferral.episode_max_seconds() == 10800
    implement_runs, statuses, outcomes, nodes = _drive(
        sf, run_id, monkeypatch, ticks=400, clock_step=30, second_driver=False)
    run = sf.get_run(run_id)
    reason = run.get("error_reason") or ""
    print("LAPS " + json.dumps({
        "implement_runs": implement_runs, "final_status": run["status"],
        "error_reason": reason, "gate_calls": _gate_calls(counter),
        "reacquires": outcomes.count("reacquire"), "ticks": len(outcomes),
        "last_outcomes": outcomes[-4:]}, indent=1))

    assert run["status"] == "failed", statuses[-3:]
    assert implement_runs == 1
    assert "cycle limit" not in reason.lower()
    assert reason.startswith(gate_deferral.ABSENCE_TERMINAL), reason
    assert gate_deferral.absent_terminal_names_no_failure(reason)
    # Every lap the edge allows was used: the first run plus 100 re-runs.
    assert outcomes.count("reacquire") == 100
    assert _gate_calls(counter) == 101
    # And the sentence counts those gate runs, not the ticks that looked.
    assert reason.endswith("(run_tests.sh, 101 gate run(s))"), reason
