"""The known-red baseline only exists if a step DECLARES the capability.

The mechanism shipped complete and dead. `api/dependencies.py` registered the
`stateful` capability, `_apply_baseline` read the `state_dir` it injects, and
`tests/unit/test_run_tests_known_red_baseline.py` was green — over `tmp_path`.
In the deployment, measured 2026-09-20:

  * `grep -rn "capability: stateful" configs/` → no hits, anywhere;
  * `~/.AItelier/pipeline_state/` did not exist;
  * `find ~/.AItelier -name run_tests_baseline.json` → nothing;
  * the live trace of run 024cfec6 records the tool call's params as
    `{out_dir, step_id, config_name, step_name, step_type, project_id}` —
    no `state_dir`.

A unit test over `tmp_path` cannot see any of that: it passes the argument the
graph never passes. These tests read the graph.
"""
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIRS = (ROOT / "configs", ROOT / "configs" / "addons")


def _steps(document):
    """Every step dict in a graph document, including an addon's spliced ones."""
    out = list(document.get("steps") or [])
    for addition in (document.get("add_steps") or []):
        out.append(addition)
    for splice in (document.get("splice") or []):
        out.extend(splice.get("steps") or [])
    for value in document.values():
        if isinstance(value, dict):
            out.extend(_steps(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and "steps" in item:
                    out.extend(_steps(item))
    return out


def _run_tests_steps():
    found = []
    for directory in CONFIG_DIRS:
        for path in sorted(directory.glob("*.yaml")):
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                continue
            if not isinstance(document, dict):
                continue
            for step in _steps(document):
                if isinstance(step, dict) and step.get("tool_name") == "run_tests":
                    found.append((path.name, step))
    return found


def test_every_run_tests_step_declares_the_stateful_capability():
    """Without the declaration the tool gets no state_dir and no baseline.

    This is the check that was missing: the capability, the tool and the unit
    tests were all in place and not one graph asked for it.
    """
    steps = _run_tests_steps()
    assert steps, "no run_tests steps found — the scan is broken, not the configs"
    missing = [f"{name}:{step.get('id')}" for name, step in steps
               if step.get("capability") != "stateful"]
    assert missing == [], (
        "these run_tests steps get no durable state_dir, so every pre-existing "
        f"red in the repo reads as the round's: {missing}")


def test_the_declaration_is_what_makes_state_dir_arrive(tmp_path):
    """The real step dict, the real capability, skillflow's real injection.

    Verbatim `test` step from configs/coding_impl.yaml — only the tool BODY is
    swapped, for a recorder that reports the kwargs it was handed. Asserting on
    the declaration alone would not prove it is honoured; asserting on
    `_apply_baseline(report, tmp_path)` proves nothing about the graph.
    """
    from skillflow.core import SkillFlow
    from skillflow.graph import PipelineGraph

    document = yaml.safe_load(
        (ROOT / "configs" / "coding_impl.yaml").read_text(encoding="utf-8"))
    step = next(s for s in document["steps"] if s.get("tool_name") == "run_tests")
    assert step.get("capability") == "stateful"

    probe = dict(step)
    probe["transitions"] = [{"to": "done"}]
    graph = PipelineGraph._from_dict({
        "name": "coding_impl", "begin": probe["id"],
        "steps": [probe, {"id": "done", "step_type": "gate", "transitions": []}],
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"}]},
    })

    captured = {}

    class _Loader:
        def is_native(self, name):
            return True

        def load_schema(self, name):
            return {}

        def load_fn(self, name):
            def _recorder(**kwargs):
                captured.update(kwargs)
                return {"written": "test_report.json", "passed": True}
            return _recorder

    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=_Loader(),
                   workspace_base=str(tmp_path / "ws"))
    sf.register_capability(
        "stateful",
        context_provider=lambda cfg: {"state_dir": str(sf._workspace.state_dir(cfg))})
    sf.register_graph(graph)
    run_id = sf.create_run("coding_impl", {"project_id": "p"})
    sf.start_run(run_id)
    for _ in range(12):
        sf.advance_run(run_id)
        if captured:
            break

    assert "state_dir" in captured, captured
    assert captured["state_dir"] == str(sf._workspace.state_dir("coding_impl"))


def test_the_same_graph_without_the_declaration_gets_nothing(tmp_path):
    """The control. Otherwise the test above could be passing on a default."""
    from skillflow.core import SkillFlow
    from skillflow.graph import PipelineGraph

    document = yaml.safe_load(
        (ROOT / "configs" / "coding_impl.yaml").read_text(encoding="utf-8"))
    step = dict(next(s for s in document["steps"]
                     if s.get("tool_name") == "run_tests"))
    step.pop("capability")
    step["transitions"] = [{"to": "done"}]
    graph = PipelineGraph._from_dict({
        "name": "coding_impl_nocap", "begin": step["id"],
        "steps": [step, {"id": "done", "step_type": "gate", "transitions": []}],
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"}]},
    })

    captured = {}

    class _Loader:
        def is_native(self, name):
            return True

        def load_schema(self, name):
            return {}

        def load_fn(self, name):
            def _recorder(**kwargs):
                captured.update(kwargs)
                return {"written": "test_report.json", "passed": True}
            return _recorder

    sf = SkillFlow(str(tmp_path / "t.db"), tool_loader=_Loader(),
                   workspace_base=str(tmp_path / "ws"))
    sf.register_capability(
        "stateful",
        context_provider=lambda cfg: {"state_dir": "SHOULD-NOT-ARRIVE"})
    sf.register_graph(graph)
    run_id = sf.create_run("coding_impl_nocap", {"project_id": "p"})
    sf.start_run(run_id)
    for _ in range(12):
        sf.advance_run(run_id)
        if captured:
            break

    assert captured, "the recorder never ran — the probe proves nothing"
    assert "state_dir" not in captured


# ── the terminal reason (criterion 4) ───────────────────────────────────────

def test_a_cycle_limit_reason_names_the_reds_and_the_baseline():
    """Run 024cfec6 died as `Cycle limit exceeded` and nothing else.

    Four implement laps over three failures that were red on the round's own
    base commit, and the reason carried none of it.
    """
    from core.scheduler import _attribution_line

    line = _attribution_line({
        "passed": False,
        "failures": [
            "FAILED tests/unit/test_mcp_transport_security.py::test_compose_default_uses_public_host_and_wildcard_port - FileNotFoundError",
            "FAILED tests/unit/test_deployment_journal_anchor.py::test_x - assert",
        ],
        "new_failures": [],
        "baseline_state": "compared",
        "baseline_failures": ["a", "b"],
    })
    assert "0 of 2 failures are NEW" in line
    assert "baseline compared, 2 known-red" in line
    assert "test_compose_default_uses_public_host_and_wildcard_port" in line
    # The DB status column keeps 160 characters; the counts must be inside it.
    assert "0 of 2 failures are NEW" in line[:160]


def test_the_reason_says_when_attribution_was_never_possible():
    from core.scheduler import _attribution_line
    line = _attribution_line({
        "passed": False,
        "failures": ["FAILED tests/unit/test_a.py::test_one - boom"],
        "new_failures": ["FAILED tests/unit/test_a.py::test_one - boom"],
        "baseline_state": "unavailable",
        "baseline_failures": [],
    })
    assert "attribution UNKNOWN" in line
    assert "capability: stateful" in line
    assert "tests/unit/test_a.py::test_one" in line


def test_a_passing_report_contributes_no_failure_reason():
    from core.scheduler import _attribution_line
    assert _attribution_line({"passed": True, "failures": []}) == ""
    assert _attribution_line({}) == ""


def test_the_reason_reads_the_report_the_run_itself_recorded(tmp_path,
                                                             monkeypatch):
    """Not a path this function reconstructs — the `out_dir` in the trace.

    skillflow stores a tool step's result as
    `{"source": "tool_step", "written": ..., "passed": false}`: the baseline
    fields are dropped, so the reason has to reach the report on disk.
    """
    from core import scheduler

    out_dir = tmp_path / "coding_impl" / "test"
    out_dir.mkdir(parents=True)
    (out_dir / "test_report.json").write_text(json.dumps({
        "passed": False,
        "failures": ["FAILED tests/unit/test_a.py::test_one - boom"],
        "new_failures": [],
        "baseline_state": "compared",
        "baseline_failures": ["tests/unit/test_a.py::test_one"],
    }), encoding="utf-8")

    rows = [
        {"step_id": "test", "payload_json": json.dumps(
            {"source": "tool_step", "written": "test_report.json",
             "passed": False})},
        {"step_id": "test", "payload_json": json.dumps(
            {"source": "tool_step", "params": {
                "out_dir": str(out_dir), "step_name": "run_tests",
                "step_id": "test", "config_name": "coding_impl"}})},
    ]

    class _SF:
        def trace_query(self, run_id, sql, args):
            return rows

    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: _SF())
    detail = scheduler._test_gate_attribution("run-x")
    assert detail.startswith("test: ")
    assert "0 of 1 failures are NEW" in detail


def test_no_run_tests_call_in_the_trace_yields_no_attribution(monkeypatch):
    from core import scheduler

    class _SF:
        def trace_query(self, run_id, sql, args):
            return [{"step_id": "implement", "payload_json": json.dumps(
                {"params": {"step_name": "write", "out_dir": "/nope"}})}]

    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: _SF())
    assert scheduler._test_gate_attribution("run-x") == ""
