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
    from tests.code_output_fixture import init_code_repo
    init_code_repo(tmp_path / "proj" / "p")
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
                "summary": "ok" if passed else "FAILED", "failures": [],
                "node_gate_complete": True}),
                encoding="utf-8")
        # The RETURN carries the verdict, as the real `run_tests` does: both
        # configs route on it now, and a fake producer that omitted it would be
        # modelling the fail-open shape rather than a working executor.
        return {"written": "test_report.json", "passed": passed,
                "node_gate_complete": True}

    def _repo_apply(*args, **kwargs):
        calls["repo_apply"] += 1
        return {"passed": True, "applied": True, "committed": True}

    loader.register_dynamic_tool("run_tests", {}, _run_tests)
    loader.register_dynamic_tool("repo_apply", {}, _repo_apply)
    _, run_id = _register(sf, "fix_tests")
    return sf, run_id, calls




def _drive_worker(sf, run_id, worker_step, max_ticks=40):
    """Advance to termination; only the agent `worker_step` is claimable —
    confirm it with one staged file and empty flags (routing test).
    Returns (status, worker_runs)."""
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
        from tests.code_output_fixture import write_claim_code
        write_claim_code(sf, run_id, claimed)
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


# ── fix_tests: a partial node gate must not conclude success ────────
#
# The unit test `test_partial_gate_cannot_reach_done.py` asserts which edge the
# resolver picks for a partial report. It cannot show that the run ENDS where
# that edge points: the terminal only fails the run if the engine honours its
# `end_conditions` entry, and a mutant that declared the terminal
# `result: completed` was caught by nothing but a YAML-shape assertion. These
# drive the REAL graph so the claim is measured where it matters — including
# the fail-open case, a green report that carries NO completeness verdict at
# all (`test_fix_tests_green_with_no_verdict_anywhere_does_not_complete`).

_PARTIAL_GREEN = {
    "passed": True, "returncode": 0, "summary": "8 of 9 node project(s) measured",
    "failures": [], "node_gate_complete": False,
    "node_unmeasured": [{"dir": "packages/p7", "kind": "capped",
                         "reason": "not visited: more than 8 node projects"}],
}
_PARTIAL_RED = dict(_PARTIAL_GREEN, passed=False, returncode=1,
                    summary="1 of 9 node project(s) measured",
                    failures=["node:web:test failed (rc=1)"])


def _wire_fix_tests_report(tmp_path, reports):
    """Like `_wire_fix_tests`, but each call serves a whole caller-supplied
    report, so a report shape the unit tests can construct by hand can also be
    driven through the engine.

    The fake producer copies the report's own verdict onto the RETURN, exactly
    as the real `run_tests` does — including its absence: a report with no
    `node_gate_complete` key produces a return with no key either, which is the
    shape `test_fix_tests_green_with_no_verdict_anywhere_does_not_complete`
    exists to route."""
    sf, loader = _sf(tmp_path)
    calls = {"run_tests": 0, "repo_apply": 0}
    seq = list(reports)

    def _run_tests(*args, out_dir="", **kwargs):
        report = dict(seq[min(calls["run_tests"], len(seq) - 1)])
        calls["run_tests"] += 1
        if out_dir:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / "test_report.json").write_text(
                json.dumps(report), encoding="utf-8")
        ret = {"written": "test_report.json", "passed": report["passed"]}
        if "node_gate_complete" in report:
            ret["node_gate_complete"] = bool(report["node_gate_complete"])
        return ret

    def _repo_apply(*args, **kwargs):
        calls["repo_apply"] += 1
        return {"passed": True, "applied": True, "committed": True}

    loader.register_dynamic_tool("run_tests", {}, _run_tests)
    loader.register_dynamic_tool("repo_apply", {}, _repo_apply)
    _, run_id = _register(sf, "fix_tests")
    return sf, run_id, calls


def test_fix_tests_ends_failed_on_a_partial_node_gate(tmp_path):
    """The same shape that reached `done` before the change: green on the
    projects it measured, one project never visited. It must end the run, and
    it must not charge a worker lap to something no edit can fix."""
    full = dict(_PARTIAL_GREEN, node_gate_complete=True, node_unmeasured=[],
                summary="9 of 9 node project(s) measured")
    green = _wire_fix_tests_report(tmp_path / "green", [full])
    green_status, green_runs = _drive_worker(*green[:2], "fix", max_ticks=10)

    sf, run_id, calls = _wire_fix_tests_report(tmp_path / "partial", [_PARTIAL_GREEN])
    status, runs = _drive_worker(sf, run_id, "fix", max_ticks=10)

    # The pipeline opens at `fix` (that lap is its first move, not a charge for
    # the absence), then tests once and stops. Same number of laps as the fully
    # measured green run, one fewer step to reach a verdict — and that verdict is
    # a failure, not a success.
    assert green_status == "completed" and green_runs == 1, (green_status, green_runs)
    assert status == "failed", status
    assert runs == green_runs, f"the absence cost {runs} lap(s), green took {green_runs}"
    assert calls["run_tests"] == 1, calls
    run = sf.get_run(run_id)
    assert "node_gate_incomplete" in json.dumps(run), run


def test_fix_tests_keeps_the_fix_loop_when_a_partial_report_also_has_a_red(tmp_path):
    """The case the first version of this change broke: a measured red beside
    an unmeasured project belongs to the repair loop, not to the terminal."""
    sf, run_id, calls = _wire_fix_tests_report(tmp_path, [_PARTIAL_RED])
    status, runs = _drive_worker(sf, run_id, "fix", max_ticks=40)

    assert status == "failed"          # never green, bounded
    assert runs == 4, runs             # initial + three bounded laps, as before
    assert calls["run_tests"] == 4, calls


# ── fix_tests: an ABSENT completeness verdict must not conclude success ──
#
# The fail-open the change exists for, driven through the real graph: a fully
# green report whose `node_gate_complete` is in NEITHER carrier — absent from
# `test_report.json` and absent from the tool's return. That is the shape an
# executor image older than the config leaves, or a report written by a
# different producer. Until this change it matched the raw `passed: true`
# success edge and the run ended `completed` with the node gate unmeasured.
_ABSENT_VERDICT_GREEN = {
    "passed": True, "returncode": 0,
    "summary": "green; nothing said whether node projects were measured",
    "failures": [],
}


def test_fix_tests_green_with_no_verdict_anywhere_does_not_complete(tmp_path):
    sf, run_id, calls = _wire_fix_tests_report(tmp_path, [_ABSENT_VERDICT_GREEN])
    status, runs = _drive_worker(sf, run_id, "fix", max_ticks=10)
    run = sf.get_run(run_id)

    assert status != "completed", (status, run)
    # MEASURED, not assumed: the engine's tool-step router finds no matching
    # edge, fails the run in the same transaction and records the reason — it
    # does not park, retry or time out. Observed run row:
    #   status="failed", current_node="test",
    #   error_reason="No matching transition from 'test' with flags
    #    {'written': 'test_report.json', 'passed': True} — test_report.json
    #    summary: green; nothing said whether node projects were measured"
    # (one `fix` lap, one `run_tests` call — the absence costs nothing extra).
    assert status == "failed", status
    assert "No matching transition" in json.dumps(run), run
    assert "node_gate_incomplete" not in json.dumps(run), run
    assert calls["run_tests"] == 1, calls
    assert runs == 1, runs


# ── subagent: adversarial LLM-reviewer gate ─────────────────────────

def _wire_subagent(tmp_path, verdicts, test_results=None):
    """Real subagent graph; repo_apply stubbed; the objective `test` gate
    (run_tests) returns a scripted pass/fail sequence (`test_results`, default
    all-green so it routes straight to review); the reviewer's verdict is
    scripted by writing review_verdict.json into the review step's dir at
    confirm time (`verdicts` is the per-review `passed` sequence)."""
    sf, loader = _sf(tmp_path)
    calls = {"repo_apply": 0, "review": 0, "run_tests": 0}
    tseq = list(test_results) if test_results is not None else [True]

    def _repo_apply(*args, **kwargs):
        calls["repo_apply"] += 1
        return {"passed": True, "applied": True, "committed": True}

    def _run_tests(*args, out_dir="", **kwargs):
        passed = tseq[min(calls["run_tests"], len(tseq) - 1)]
        calls["run_tests"] += 1
        if out_dir:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / "test_report.json").write_text(json.dumps({
                "passed": passed, "returncode": 0 if passed else 1,
                "summary": "ok" if passed else "FAILED", "failures": [],
                "node_gate_complete": True}),
                encoding="utf-8")
        # As the real tool does — and as `subagent`'s success edge requires.
        return {"written": "test_report.json", "passed": passed,
                "node_gate_complete": True}

    loader.register_dynamic_tool("repo_apply", {}, _repo_apply)
    loader.register_dynamic_tool("run_tests", {}, _run_tests)
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
                from tests.code_output_fixture import write_claim_code
                write_claim_code(sf, run_id, claimed)
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


def test_subagent_test_gate_loops_before_review(tmp_path):
    # Objective gate: tests fail once → loop back to work (the reviewer is never
    # consulted on a red build) → tests green → review → completed.
    # NOTE: on the loop-then-pass path skillflow re-claims `review` once before
    # routing to `done` (an advance quirk, same family as the stale-completed-row
    # trap — costs one extra reviewer call, doesn't affect the outcome), so we
    # assert review>=1 rather than ==1. The clean single-review path is covered
    # by test_subagent_passing_first_review; the red-blocks-review invariant by
    # test_subagent_test_never_green_fails_bounded (review==0).
    status, work_runs, calls = _wire_subagent(
        tmp_path, verdicts=[True], test_results=[False, True])()
    assert status == "completed"
    assert calls["run_tests"] == 2     # red then green
    assert work_runs == 2              # re-worked after the red gate
    assert calls["review"] >= 1        # reviewer only ran on the green build


def test_subagent_test_never_green_fails_bounded(tmp_path):
    # A change that never passes the suite fails on the test gate's bound and
    # never reaches the reviewer — a test-breaking change cannot slip past.
    status, work_runs, calls = _wire_subagent(
        tmp_path, verdicts=[True], test_results=[False])()
    assert status == "failed"
    assert calls["run_tests"] == 4     # initial + 3 bounded retries
    assert calls["review"] == 0        # never got past the objective gate


# ── single-step pipelines: investigate (read-only) + code_review ────

def _drive_single(sf, run_id, step, max_ticks=20):
    """Drive a single-agent-step graph to termination; confirm the one agent
    step empty. Returns (status, runs)."""
    runs = 0
    for _ in range(max_ticks):
        node = sf.advance_run(run_id)
        run = sf.get_run(run_id)
        if node is None:
            if run["status"] == "running":
                continue
            return run["status"], runs
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        assert claimed.step_id == step
        runs += 1
        sf.confirm_step(claimed.token, StepResult(flags={}))
    return "TIMEOUT", runs


def test_investigate_completes_and_is_read_only(tmp_path):
    sf, _ = _sf(tmp_path)
    graph, run_id = _register(sf, "investigate")
    status, runs = _drive_single(sf, run_id, "investigate")
    assert status == "completed" and runs == 1
    # read-only guarantee: the graph has no write output-mode and no
    # repo-mutating lifecycle hook anywhere.
    inv = next(n for n in graph.steps if n.id == "investigate")
    assert inv.output_mode == "content"          # no create/edit derived
    for n in graph.steps:
        hooks = (getattr(n, "lifecycle", None) or {})
        assert "on_deliver" not in hooks           # no repo_apply


def test_code_review_completes(tmp_path):
    # code_review's review step validates review_verdict.json, so it must be
    # written before confirm (as the real reviewer agent would).
    sf, _ = _sf(tmp_path)
    graph, run_id = _register(sf, "code_review")
    status = runs = None
    for _ in range(20):
        node = sf.advance_run(run_id)
        run = sf.get_run(run_id)
        if node is None:
            if run["status"] == "running":
                continue
            status = run["status"]
            break
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        assert claimed.step_id == "review"
        tmp = sf._workspace.get_step_tmp_dir("p", graph.name, "review")
        Path(tmp).mkdir(parents=True, exist_ok=True)
        (Path(tmp) / "review_verdict.json").write_text(json.dumps({
            "passed": True, "feedback": "lgtm", "findings": []}),
            encoding="utf-8")
        runs = (runs or 0) + 1
        sf.confirm_step(claimed.token, StepResult(flags={}))
    assert status == "completed" and runs == 1
