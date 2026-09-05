# tests/skillflow/test_coding_impl_gate.py
#
# coding_impl test-gate: drive the REAL configs/coding_impl.yaml graph through
# SkillFlow and prove the run_tests outcome now GATES completion.
#
# Regression: the `test` step used to transition `to: null` unconditionally, so
# a FAILING suite did not block completion — broken code got committed and only
# the butler's post-offload check caught it (observed 2026-07-03, git 622ee65:
# `sub` undefined name committed, run_tests never gated it). Now a failing test
# loops back to `implement` (with the report as feedback) until it passes, and a
# suite that never goes green fails the run cleanly instead of reporting success.
#
# The graph is REAL (loaded from the YAML); only the leaf tools are scripted —
# run_tests returns a caller-supplied pass/fail sequence (the "seeded bug" is the
# first failing result; the "fix" is a later passing one), repo_apply is a no-op.
# The single agent step (`implement`) is confirmed manually since only routing is
# under test.

import json
from pathlib import Path

import pytest
import yaml

from skillflow import SkillFlow, PipelineGraph
import skillflow as _skillflow_pkg
from skillflow.tool_loader import ToolLoader
from skillflow.core import StepResult

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _wire(tmp_path, test_results, *, tool_result=None, schema_failure=None):
    """Real coding_impl graph with a scripted run_tests (`test_results` is the
    per-invocation `passed` sequence; the last value repeats) and a stubbed
    repo_apply. Returns (sf, run_id, calls)."""
    loader = ToolLoader(Path(_skillflow_pkg.__file__).parent / "tools")
    loader.add_tools_dir(_REPO_ROOT / "aitelier" / "tools")

    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "proj"),
                   stale_threshold_seconds=60)

    calls = {"run_tests": 0, "repo_apply": 0}
    seq = list(test_results)

    def _run_tests(*args, out_dir="", **kwargs):
        outcome = seq[min(calls["run_tests"], len(seq) - 1)]
        calls["run_tests"] += 1
        if isinstance(outcome, bool):
            report = {
                "passed": outcome, "returncode": 0 if outcome else 1,
                "summary": "ok" if outcome else "FAILED tests/test_ops.py::test_sub",
                "failures": [] if outcome else
                            ["FAILED tests/test_ops.py::test_sub - NameError: name 'sub'"],
            }
        else:
            report = outcome
        if out_dir and report is not None:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / "test_report.json").write_text(
                report if isinstance(report, str) else json.dumps(report),
                encoding="utf-8")
        # Deliberately optimistic return: artifact validation must be decisive.
        passed = outcome if isinstance(outcome, bool) else True
        return tool_result if tool_result is not None else {
            "written": "test_report.json", "passed": passed}

    def _repo_apply(*args, **kwargs):
        calls["repo_apply"] += 1
        return {"passed": True, "applied": True, "committed": True}

    # register_dynamic_tool replaces the fn load_fn returns; framework mode
    # (delegate_tools_to_agent=False, the default) runs tool nodes inline.
    loader.register_dynamic_tool("run_tests", {}, _run_tests)
    loader.register_dynamic_tool("repo_apply", {}, _repo_apply)
    if schema_failure is not None:
        real_schema = loader.load_fn("json_schema")
        schema_calls = 0

        def _schema(**kwargs):
            nonlocal schema_calls
            schema_calls += 1
            # Shape check succeeds, then verdict tool errors with stale success
            # flags/artifact still available from the preceding step.
            if schema_calls >= 2:
                if isinstance(schema_failure, Exception):
                    raise schema_failure
                return schema_failure
            # Use the real native validator for the earlier successful check.
            import inspect
            return real_schema(**{k: v for k, v in kwargs.items()
                                  if k in inspect.signature(real_schema).parameters})
        loader.register_dynamic_tool("json_schema", {}, _schema)


    # register_graph validates agent_config references — provide the real ones.
    for name, cfg in (yaml.safe_load(
            (_REPO_ROOT / "agent_configs" / "coding_impl.yaml").read_text(
                encoding="utf-8")) or {}).items():
        try:
            sf.register_agent_config_from_dict(name, cfg)
        except Exception:
            pass

    graph = PipelineGraph.from_yaml(_REPO_ROOT / "configs" / "coding_impl.yaml")
    sf.register_graph(graph)
    run_id = sf.create_run(graph.name, {"project_id": "p"})
    sf.start_run(run_id)
    return sf, run_id, calls


def _drive(sf, run_id, max_ticks=40):
    """Advance the real graph to termination. `test` (tool) and `done` (gate)
    resolve inline; only `implement` (agent) is claimable — confirm it with an
    empty result (routing test, no LLM), after staging one file so the step has
    something for its `on_deliver` to deliver (skillflow >= 1.5.32 re-asks a
    maker that promoted nothing instead of delivering an empty step dir).
    Returns (status, implement_runs)."""
    implement_runs = 0
    for _ in range(max_ticks):
        node = sf.advance_run(run_id)
        run = sf.get_run(run_id)
        if node is None:
            if run["status"] == "running":
                continue  # advanced into an inline node (tool/gate) — advance again
            return run["status"], implement_runs
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        assert claimed.step_id == "implement", (
            f"only `implement` should be claimable, got {claimed.step_id}")
        implement_runs += 1
        tmp = sf._workspace.get_step_tmp_dir("p", sf.get_run(run_id)["graph_name"],
                                             claimed.step_id)
        Path(tmp).mkdir(parents=True, exist_ok=True)
        (Path(tmp) / "impl.py").write_text("x = 1\n", encoding="utf-8")
        sf.confirm_step(claimed.token, StepResult(flags={}))
    return "TIMEOUT", implement_runs


def test_passing_first_try_completes_without_looping(tmp_path):
    """Tests pass on the first run → straight to done, implement runs once."""
    sf, run_id, calls = _wire(tmp_path, [True])
    status, implement_runs = _drive(sf, run_id)
    assert status == "completed"
    assert implement_runs == 1
    assert calls["run_tests"] == 1


def test_failing_test_loops_back_then_completes_on_fix(tmp_path):
    """A failing suite must NOT complete the run: it loops back to implement,
    which re-runs; once the retried suite passes, the run completes. This is the
    core gate the old `to: null` lacked."""
    sf, run_id, calls = _wire(tmp_path, [False, True])
    status, implement_runs = _drive(sf, run_id)
    assert status == "completed"
    assert implement_runs == 2            # initial + one fix pass
    assert calls["run_tests"] == 2        # re-verified after the fix
    assert calls["repo_apply"] == 2       # each implement delivery committed


def test_never_passing_fails_run_bounded(tmp_path):
    """A suite that never goes green must FAIL the run, not loop forever and
    never complete. Bounded by `max_loop: 3` on the test→implement edge —
    enforced on tool-step edges since skillflow-py 1.5.2 (before that, the
    tool's outgoing edge was never counted and the loop was unbounded)."""
    sf, run_id, calls = _wire(tmp_path, [False])
    status, implement_runs = _drive(sf, run_id)
    assert status == "failed"
    # 4 test runs (initial + 3 retries) then the edge exhausts → run fails.
    assert calls["run_tests"] == 4
    assert implement_runs == 4
    assert "test_evidence_missing" not in str(sf.get_run(run_id).get("error_reason"))

    # The decisive regression assertion: a failing suite never yields success.
    assert status != "completed"


@pytest.mark.parametrize("report", [
    None,
    "",
    "not JSON",
    '{"passed":true',
    '{"passed":false}{"passed":true,"summary":"ok","failures":[]}',
    [],
    {},
    {"passed": True, "summary": "npm unavailable", "failures": [],
     "node": {"passed": True, "skipped": True}},
    {"passed": True},
    {"passed": "true", "summary": "ok", "failures": []},
    {"passed": True, "summary": "runner absent", "failures": [], "skipped": True},
    {"passed": False, "summary": "runner absent", "failures": [], "skipped": True},
    {"passed": True, "summary": "nothing collected", "failures": [],
     "no_tests_collected": True},
    {"passed": False, "summary": "nothing collected", "failures": [],
     "no_tests_collected": True},
])
def test_invalid_or_absent_evidence_fails_without_implementation_retry(tmp_path, report):
    sf, run_id, calls = _wire(tmp_path, [report])
    status, implement_runs = _drive(sf, run_id)
    assert status == "failed"
    assert implement_runs == calls["run_tests"] == calls["repo_apply"] == 1
    assert "test_evidence_missing" in str(sf.get_run(run_id))


@pytest.mark.parametrize("extras", [
    {},
    {"skipped": False, "no_tests_collected": False},
    {"node": {"passed": True, "checks": {"build": {"passed": True}}}},
])
def test_valid_report_does_not_require_optional_pytest_fields(tmp_path, extras):
    report = {"passed": True, "summary": "checks passed", "failures": [], **extras}
    sf, run_id, calls = _wire(tmp_path, [report])
    assert _drive(sf, run_id) == ("completed", 1)
    assert calls["run_tests"] == 1


def test_report_failure_overrides_optimistic_return_flag(tmp_path):
    report = {"passed": False, "summary": "real failing test", "failures": ["assertion"]}
    sf, run_id, calls = _wire(tmp_path, [report, True])
    assert _drive(sf, run_id) == ("completed", 2)
    assert calls["run_tests"] == 2


def test_failed_tool_return_cannot_consume_existing_passing_artifact(tmp_path):
    sf, run_id, calls = _wire(tmp_path, [True], tool_result={
        "written": None, "passed": False, "error": "report could not be written",
    })
    assert _drive(sf, run_id) == ("failed", 1)
    assert calls["run_tests"] == 1
    assert "report could not be written" in str(sf.get_run(run_id))


@pytest.mark.parametrize("failure", [
    {"_error": True, "all_passed": True},
    {"error": "validator unavailable", "all_passed": True},
    RuntimeError("validator unavailable"),
])
def test_validator_error_cannot_reuse_prior_success(tmp_path, failure):
    sf, run_id, calls = _wire(tmp_path, [True], schema_failure=failure)
    # An execution exception can be raised to the host before the framework's
    # bounded tool retries reach terminal failure. Continue the SAME run only.
    for _ in range(5):
        try:
            status, _ = _drive(sf, run_id)
            break
        except RuntimeError as exc:
            assert str(exc) == "validator unavailable"
    else:
        pytest.fail("validator error never reached bounded terminal failure")
    assert status == "failed"
    assert calls["run_tests"] == calls["repo_apply"] == 1


def test_error_flag_wins_over_written_report(tmp_path):
    sf, run_id, calls = _wire(tmp_path, [True], tool_result={
        "_error": True, "written": "test_report.json", "passed": True,
    })
    assert _drive(sf, run_id) == ("failed", 1)
    assert calls["run_tests"] == 1
