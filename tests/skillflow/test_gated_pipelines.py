# tests/skillflow/test_gated_pipelines.py
#
# Execution tests for the gated reusable pipelines: drive the REAL graphs
# (configs/fix_tests.yaml, configs/subagent.yaml) through SkillFlow and prove
# the gate + loop-back + loop-external `done` terminal actually behave — the
# behaviour that was only ever live-verified, and that the STRUCTURAL tests in
# test_coding_mode.py cannot catch (they assert the `done` gate exists, not
# that the loop terminates correctly).
#
# Regression guarded: `node_reached` on a LOOPED node (test/review) fires
# prematurely — a stale `completed` row from a prior failed iteration reports
# the run green before the retried step runs. The fix routes pass → a
# loop-external `done` gate. These tests fail if that regresses.
#
# The graph is REAL; only the leaves are scripted — the worker agent step is
# confirmed manually (routing is under test, no LLM), the objective gate
# (run_tests) returns a caller-supplied pass/fail sequence, and the LLM review
# verdict is written as review_verdict.json at confirm time.

import json
from pathlib import Path

import yaml

from skillflow import SkillFlow, PipelineGraph
import skillflow as _skillflow_pkg
from skillflow.tool_loader import ToolLoader
from skillflow.core import StepResult

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _sf(tmp_path):
    loader = ToolLoader(Path(_skillflow_pkg.__file__).parent / "tools")
    loader.add_tools_dir(_REPO_ROOT / "aitelier" / "tools")
    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "proj"),
                   stale_threshold_seconds=60)
    return sf, loader


def _register(sf, name):
    for role, cfg in (yaml.safe_load(
            (_REPO_ROOT / "agent_configs" / f"{name}.yaml").read_text(
                encoding="utf-8")) or {}).items():
        try:
            sf.register_agent_config_from_dict(role, cfg)
        except Exception:
            pass
    graph = PipelineGraph.from_yaml(_REPO_ROOT / "configs" / f"{name}.yaml")
    sf.register_graph(graph)
    run_id = sf.create_run(graph.name, {"project_id": "p"})
    sf.start_run(run_id)
    return graph, run_id


# ── fix_tests: objective run_tests gate ─────────────────────────────

def _wire_fix_tests(tmp_path, test_results):
    sf, loader = _sf(tmp_path)
    calls = {"run_tests": 0, "repo_apply": 0}
    seq = list(test_results)

    def _run_tests(*args, out_dir="", **kwargs):
        passed = seq[min(calls["run_tests"], len(seq) - 1)]
        calls["run_tests"] += 1
        if out_dir:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / "test_report.json").write_text(json.dumps({
                "passed": passed, "returncode": 0 if passed else 1,
                "summary": "ok" if passed else "FAILED", "failures": []}),
                encoding="utf-8")
        return {"written": "test_report.json", "passed": passed}

    def _repo_apply(*args, **kwargs):
        calls["repo_apply"] += 1
        return {"passed": True, "applied": True, "committed": True}

    loader.register_dynamic_tool("run_tests", {}, _run_tests)
    loader.register_dynamic_tool("repo_apply", {}, _repo_apply)
    _, run_id = _register(sf, "fix_tests")
    return sf, run_id, calls


def _drive_worker(sf, run_id, worker_step, max_ticks=40):
    """Advance to termination; only the agent `worker_step` is claimable —
    confirm it empty (routing test). Returns (status, worker_runs)."""
    worker_runs = 0
    for _ in range(max_ticks):
        node = sf.advance_run(run_id)
        run = sf.get_run(run_id)
        if node is None:
            if run["status"] == "running":
                continue
            return run["status"], worker_runs
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        assert claimed.step_id == worker_step, (
            f"only `{worker_step}` should be claimable, got {claimed.step_id}")
        worker_runs += 1
        sf.confirm_step(claimed.token, StepResult(flags={}))
    return "TIMEOUT", worker_runs


def test_fix_tests_passing_first_try(tmp_path):
    sf, run_id, calls = _wire_fix_tests(tmp_path, [True])
    status, runs = _drive_worker(sf, run_id, "fix")
    assert status == "completed"
    assert runs == 1 and calls["run_tests"] == 1


def test_fix_tests_loops_then_completes(tmp_path):
    # fail once → loop back to fix → pass. Proves the done gate fires AFTER the
    # fix, not on the first (failing) test (the premature-node_reached bug).
    sf, run_id, calls = _wire_fix_tests(tmp_path, [False, True])
    status, runs = _drive_worker(sf, run_id, "fix")
    assert status == "completed"
    assert runs == 2 and calls["run_tests"] == 2


def test_fix_tests_never_passing_fails_bounded(tmp_path):
    sf, run_id, calls = _wire_fix_tests(tmp_path, [False])
    status, runs = _drive_worker(sf, run_id, "fix")
    assert status == "failed"          # never green → run fails, not hangs
    assert status != "completed"        # decisive: a failing suite never = success
    assert calls["run_tests"] == 4      # initial + 3 bounded retries


# ── subagent: adversarial LLM-reviewer gate ─────────────────────────

def _wire_subagent(tmp_path, verdicts):
    """Real subagent graph; repo_apply stubbed; the reviewer's verdict is
    scripted by writing review_verdict.json into the review step's dir at
    confirm time (`verdicts` is the per-review `passed` sequence)."""
    sf, loader = _sf(tmp_path)
    calls = {"repo_apply": 0, "review": 0}

    def _repo_apply(*args, **kwargs):
        calls["repo_apply"] += 1
        return {"passed": True, "applied": True, "committed": True}

    loader.register_dynamic_tool("repo_apply", {}, _repo_apply)
    graph, run_id = _register(sf, "subagent")
    seq = list(verdicts)

    def drive(max_ticks=60):
        work_runs = 0
        for _ in range(max_ticks):
            node = sf.advance_run(run_id)
            run = sf.get_run(run_id)
            if node is None:
                if run["status"] == "running":
                    continue
                return run["status"], work_runs, calls
            claimed = sf.claim_next_step(run_id)
            if claimed is None:
                continue
            if claimed.step_id == "work":
                work_runs += 1
                sf.confirm_step(claimed.token, StepResult(flags={}))
            elif claimed.step_id == "review":
                passed = seq[min(calls["review"], len(seq) - 1)]
                calls["review"] += 1
                tmp = sf._workspace.get_step_tmp_dir("p", graph.name, "review")
                Path(tmp).mkdir(parents=True, exist_ok=True)
                (Path(tmp) / "review_verdict.json").write_text(json.dumps({
                    "passed": passed,
                    "feedback": "ok" if passed else "fix the thing",
                    "findings": [] if passed else ["x.py:1 — wrong"]}),
                    encoding="utf-8")
                sf.confirm_step(claimed.token, StepResult(flags={}))
            else:
                sf.confirm_step(claimed.token, StepResult(flags={}))
        return "TIMEOUT", work_runs, calls

    return drive


def test_subagent_passing_first_review(tmp_path):
    status, work_runs, calls = _wire_subagent(tmp_path, [True])()
    assert status == "completed"
    assert work_runs == 1 and calls["review"] == 1


def test_subagent_rejects_then_passes(tmp_path):
    # reviewer fails once → loop back to work → passes. The done gate must fire
    # only after the SECOND review, never on the first (failed) one.
    status, work_runs, calls = _wire_subagent(tmp_path, [False, True])()
    assert status == "completed"
    assert work_runs == 2 and calls["review"] == 2


def test_subagent_never_passing_fails_bounded(tmp_path):
    status, work_runs, calls = _wire_subagent(tmp_path, [False])()
    assert status == "failed"
    assert status != "completed"
    assert calls["review"] == 4        # initial + 3 bounded retries
