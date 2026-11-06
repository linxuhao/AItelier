# tests/unit/test_partial_gate_cannot_reach_done.py
# A node gate that measured only SOME of a repository's projects must not reach
# a success terminal in either live pipeline that runs `run_tests`.
#
# The defect: `passed` is honestly true there (the projects that ran passed),
# and `configs/fix_tests.yaml` / `configs/subagent.yaml` routed on the raw
# boolean, so a repository past the node visit cap concluded "success" while a
# project never ran. The fix is a flat `node_gate_complete` written by
# `run_tests`, and one transition in each config, ordered before the success
# edge.
#
# The second half of the defect — measured later, and the reason this file now
# reads the RETURN — was that the routing took that verdict out of the report
# FILE (`from_file`). A report that did not CONTAIN the key matched neither the
# partial edge nor the success edge as then worded, fell through to the raw
# `passed: true` under them, and concluded success. Both configs now take the
# verdict from `run_tests`' own return and their success edge REQUIRES
# `node_gate_complete: true`, so a return (or report) without the key matches
# nothing at all. `tests/skillflow/test_gated_pipelines.py` drives that absence
# through the real engine; this file pins the resolver-level answer.
#
# Driven, not inferred:
#   * the reports AND the returns come from the REAL `rt.run_tests` (only the
#     process boundary, `repository_executor.execute`, is served — the same seam
#     `test_node_project_declared_coverage.py` uses). The flags handed to the
#     resolver are the tool's RETURN, which is what the engine merges into the
#     step's flags (`skillflow/core.py:_confirm_tool_in_tx` passes `tool_result`
#     as `result_flags`);
#   * the transition is selected by the ENGINE's own resolver,
#     `skillflow.graph.GraphResolver.resolve_transition`, with a `file_reader`
#     that reads the report file exactly as the scheduler does (the red edge
#     still reads it). Nothing here re-implements the match rule.
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from skillflow.graph import GraphResolver, PipelineGraph

import aitelier.tools.run_tests.impl as rt

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = {name: ROOT / "configs" / f"{name}.yaml" for name in ("fix_tests", "subagent")}
# The step each config's `test` gate routes FROM, and the success step it
# routes to on a green report. The red destination is the fix/work loop.
GREEN_TARGET = {"fix_tests": "done", "subagent": "review"}
RED_TARGET = {"fix_tests": "fix", "subagent": "work"}
PARTIAL_TARGET = "node_gate_incomplete"


def _project(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps(
        {"name": "p-" + directory.name, "version": "0.0.0",
         "scripts": {"build": "build-it", "test": "test-it"}}))
    (directory / "package-lock.json").write_text("{}")


def _nine(repo):
    """Nine node projects in gate order: root, `web/`, then seven under
    `packages/`. With no declaration the gate visits the first eight, so the
    ninth is a real `capped` absence."""
    _project(repo)
    _project(repo / "web")
    for n in range(1, 8):
        _project(repo / "packages" / f"p{n}")
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_probe_ok.py").write_text(
        "def test_probe_ok():\n    assert True\n")
    return [".", "web"] + [f"packages/p{n}" for n in range(1, 8)]


def _project_of(workdir):
    path = str(workdir).replace("\\", "/")
    if "/src/" in path:
        return path.split("/src/", 1)[1]
    return "."


def _serve(monkeypatch, red=None):
    """Serve npm in-process; the pytest leg runs for real through the fixture."""
    from tests.gate_fixture import install_entry_executor

    install_entry_executor(monkeypatch)
    inner = rt.repository_executor.execute

    def execute(repo_dir, args, timeout, **kw):
        if not kw.get("profile"):
            return inner(repo_dir, args, timeout, **kw)
        project = _project_of(kw.get("workdir"))
        command = " ".join(args[1:])
        code = 0
        if red is not None and project == red[0] and command.startswith(red[1]):
            code = 1
        return subprocess.CompletedProcess(args, code, "", "")

    monkeypatch.setattr(rt.repository_executor, "execute", execute)


def _run(tmp_path, monkeypatch, repo, red=None):
    """Run the real `run_tests` and return (report, tool_return, report_dir).

    `tool_return` is the dict the engine merges into the step's flags — the value
    the configs route on. `report_dir` is the `out_dir` the report was written
    to; the config's red edge still reads `test_report.json` from it.
    """
    _serve(monkeypatch, red=red)
    out = tmp_path / "out"
    ret = rt.run_tests(project_root=str(repo), out_dir=str(out),
                       repo_gate=False, state_dir=str(tmp_path / "state"))
    report = json.loads((out / "test_report.json").read_text())
    return report, ret, out


def _select(config_name: str, flags: dict, report_dir: Path):
    """(transition, target) the ENGINE's resolver picks for `test` with `flags`.

    `flags` is `run_tests`' RETURN, exactly as the engine passes it. The
    `file_reader` is still supplied because the red edge still reads the report;
    the node-gate verdict no longer goes through it.
    """
    graph = PipelineGraph.from_yaml(CONFIGS[config_name])
    resolver = GraphResolver(graph)

    def read(path: str) -> str:
        return (report_dir / path).read_text(encoding="utf-8")

    return resolver.resolve_transition("test", flags, {}, file_reader=read)


# --------------------------------------------------------------- the flag itself
def test_partial_report_writes_node_gate_complete_false(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    order = _nine(repo)

    report, _, _ = _run(tmp_path, monkeypatch, repo)

    # The defect's precondition, still true: the report says nothing failed.
    assert report["passed"] is True, report["failures"]
    assert [e["dir"] for e in report["node_unmeasured"]] == [order[8]]
    assert report["node_gate_complete"] is False


def test_fully_measured_green_report_writes_node_gate_complete_true(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    report, _, _ = _run(tmp_path, monkeypatch, repo)

    assert report["passed"] is True, report["failures"]
    assert "node_unmeasured" not in report
    assert report["node_gate_complete"] is True


def test_python_only_repository_has_no_node_gate_complete(tmp_path, monkeypatch):
    """No package.json anywhere: there is no node section, so the key is absent
    from the FILE (gate_evidence and the older tests read it there) and the
    repository routes exactly as it did before this change."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_probe_ok.py").write_text(
        "def test_probe_ok():\n    assert True\n")

    report, _, _ = _run(tmp_path, monkeypatch, repo)

    assert "node" not in report
    assert "node_gate_complete" not in report


def test_red_measured_report_writes_node_gate_complete_true(tmp_path, monkeypatch):
    """A measured red is a complete measurement: the flag is True, and the
    report routes to fix/work through the existing `passed: false` edge."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    report, _, _ = _run(tmp_path, monkeypatch, repo, red=("web", "test"))

    assert report["passed"] is False, report["failures"]
    assert report["node_gate_complete"] is True


# ------------------------------------------- the verdict on the tool's RETURN
#
# Criterion `return-carries-the-verdict`: the routing verdict is read from the
# payload the engine merges into the step's flags, so it has to be THERE, as a
# real boolean, on every path that returns a report. Measured on the three
# shapes the configs have to route: a partial gate, a fully measured green, and
# a repository with no node section at all.
def test_return_carries_a_false_verdict_for_a_partial_gate(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _nine(repo)

    _, ret, _ = _run(tmp_path, monkeypatch, repo)

    assert ret["node_gate_complete"] is False, ret.get("node_gate_complete")
    assert isinstance(ret["node_gate_complete"], bool)


def test_return_carries_a_true_verdict_for_a_fully_measured_green(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    _, ret, _ = _run(tmp_path, monkeypatch, repo)

    assert ret["node_gate_complete"] is True, ret.get("node_gate_complete")
    assert isinstance(ret["node_gate_complete"], bool)


def test_return_answers_complete_for_a_repository_without_a_node_section(tmp_path, monkeypatch):
    """The report deliberately has no key here (there was no node section), so
    the RETURN has to answer for itself: `True`, "there was nothing that could be
    incomplete", which is what keeps a Python-only repository's routing
    unchanged. A return that simply OMITTED the key would route it nowhere."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_probe_ok.py").write_text(
        "def test_probe_ok():\n    assert True\n")

    report, ret, _ = _run(tmp_path, monkeypatch, repo)

    assert "node_gate_complete" not in report
    assert ret["node_gate_complete"] is True, ret.get("node_gate_complete")
    assert isinstance(ret["node_gate_complete"], bool)


def test_return_carries_a_true_verdict_for_a_measured_red(tmp_path, monkeypatch):
    """The fourth shape that returns a report: a fully measured RUN WITH A RED.
    The verdict is about MEASUREMENT, not about the suite's colour, so it is
    `True` — and `passed: false` is what routes the run to its repair loop. A
    producer that conflated the two would send every red to the incomplete
    terminal instead."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    _, ret, _ = _run(tmp_path, monkeypatch, repo, red=("web", "test"))

    assert ret["node_gate_complete"] is True, ret.get("node_gate_complete")
    assert ret["passed"] is False, ret.get("passed")
    assert isinstance(ret["node_gate_complete"], bool)


# ---------------------------------------------------------- the routing, per config
@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_partial_report_selects_the_terminal_not_success(config_name, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _nine(repo)
    _, ret, out = _run(tmp_path, monkeypatch, repo)

    assert _select(config_name, ret, out)[1] == PARTIAL_TARGET


@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_green_fully_measured_report_still_selects_success(config_name, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))
    _, ret, out = _run(tmp_path, monkeypatch, repo)

    assert _select(config_name, ret, out)[1] == GREEN_TARGET[config_name]


@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_red_measured_report_still_selects_fix_loop(config_name, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))
    _, ret, out = _run(tmp_path, monkeypatch, repo, red=("web", "test"))

    assert _select(config_name, ret, out)[1] == RED_TARGET[config_name]


@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_partial_report_with_a_measured_red_still_selects_fix_loop(config_name, tmp_path, monkeypatch):
    """A red in a measured project keeps its fix loop even when another project
    was not measured. This is the case the partial edge must NOT capture: its
    `node_gate_complete` is also false, and only the failing edge (ordered first)
    keeps it on the fix loop instead of the terminal."""
    repo = tmp_path / "repo"
    _nine(repo)  # ninth project is capped out → node_gate_complete False
    _, ret, out = _run(tmp_path, monkeypatch, repo, red=("web", "test"))

    assert _select(config_name, ret, out)[1] == RED_TARGET[config_name]


@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_python_only_green_report_still_selects_success(config_name, tmp_path, monkeypatch):
    """No node section: the return answers `True`, so green still reaches success."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_probe_ok.py").write_text(
        "def test_probe_ok():\n    assert True\n")
    _, ret, out = _run(tmp_path, monkeypatch, repo)

    assert _select(config_name, ret, out)[1] == GREEN_TARGET[config_name]


# ------------------------------------------- absence is not a success terminal
@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_a_green_with_no_verdict_anywhere_matches_no_transition(config_name, tmp_path, monkeypatch):
    """The fail-open shape: fully green, and NOTHING says whether the node gate
    was complete — the key is absent from the report file AND from the tool
    return (what an older executor image, or a different producer, leaves).
    Before the change this fell through to the raw `passed: true` success edge.
    Now it matches no edge at all; the engine turns that into a failed run
    (`No matching transition from 'test'`), never into a success — driven through
    the real engine in tests/skillflow/test_gated_pipelines.py.
    """
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))
    report, ret, out = _run(tmp_path, monkeypatch, repo)

    # Strip BOTH carriers, which is the producer shape under test.
    del ret["node_gate_complete"]
    report.pop("node_gate_complete", None)
    (out / "test_report.json").write_text(json.dumps(report), encoding="utf-8")

    transition, target = _select(config_name, ret, out)

    assert transition is None, (transition, target)
    assert target is None, target


@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_the_verdict_is_not_read_from_the_file(config_name):
    """The artifact half of the contract: the incomplete edge matches the step's
    FLAGS (`{field: node_gate_complete, value: false}`), the success edge
    REQUIRES `node_gate_complete: true` positively (never a mere absence of
    `false`), and no edge reads this field out of `test_report.json`.

    The success edge carries `passed: true` as well, and that term is
    load-bearing rather than decorative: `max_loop` exhaustion is resolved by
    skipping the red edge, so a success edge keyed on completeness alone would
    catch a suite that never passes and report it as `completed` — measured
    before the term was added (`test_fix_tests_never_passing_fails_bounded` /
    `test_subagent_test_never_green_fails_bounded` went red).
    """
    graph = PipelineGraph.from_yaml(CONFIGS[config_name])
    test_step = next(s for s in graph.steps if s.id == "test")
    edges = {t.to: (t.match or {}) for t in test_step.transitions}

    assert edges[PARTIAL_TARGET] == {"field": "node_gate_complete", "value": False}
    success = edges[GREEN_TARGET[config_name]]
    assert success.get("node_gate_complete") is True, success
    assert success.get("passed") is True, success
    for target, match in edges.items():
        assert not (match.get("from_file") and match.get("field") == "node_gate_complete"), (
            f"{config_name}: {target} still reads node_gate_complete from a file")


# ----------------------------------------------------------- the config shape
@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_edge_order_red_then_partial_then_green(config_name):
    """Order is load-bearing (the engine takes the first match). The red edge comes
    first, so a partial report with a measured red keeps its fix loop. The partial
    edge comes before the green edge, so a green partial report cannot reach a
    success terminal. Reversing either silently misroutes a real case."""
    graph = PipelineGraph.from_yaml(CONFIGS[config_name])
    test_step = next(s for s in graph.steps if s.id == "test")
    targets = [t.to for t in test_step.transitions]
    assert targets.index(RED_TARGET[config_name]) < targets.index(PARTIAL_TARGET)
    assert targets.index(PARTIAL_TARGET) < targets.index(GREEN_TARGET[config_name])


@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_terminal_is_a_gate_with_no_outgoing_edge_and_ends_failed(config_name):
    """The terminal is a gate whose only edge is to null, and an end condition
    names it as `failed`, so the run ends there rather than reading as complete."""
    graph = PipelineGraph.from_yaml(CONFIGS[config_name])
    terminal = next(s for s in graph.steps if s.id == PARTIAL_TARGET)
    assert terminal.step_type == "gate"
    assert [t.to for t in terminal.transitions] == [None]
    failed = [c for c in graph.end_conditions.conditions
              if c.type == "node_reached" and c.node == PARTIAL_TARGET]
    assert failed and all(c.result == "failed" for c in failed)


def test_the_flag_is_false_when_the_only_project_was_not_measured(tmp_path, monkeypatch):
    """The single-project path returns a node dict with `skipped` and NO
    `unmeasured` key, so a flag computed from `unmeasured` alone says "complete"
    about a gate that measured nothing. Round 2's mutant that dropped the
    `skipped` term survived 147 tests; this is the path that distinguishes it —
    in the report AND (criterion `return-carries-the-verdict`) on the return."""
    repo = tmp_path / "repo"
    _project(repo / "web")
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_probe_ok.py").write_text(
        "def test_probe_ok():\n    assert True\n")

    from tests.gate_fixture import install_entry_executor

    install_entry_executor(monkeypatch)
    inner = rt.repository_executor.execute

    def execute(repo_dir, args, timeout, **kw):
        if not kw.get("profile"):
            return inner(repo_dir, args, timeout, **kw)
        raise rt.repository_executor.IsolationUnavailable(
            "no bounded execution profile for this command (fixture)")

    monkeypatch.setattr(rt.repository_executor, "execute", execute)
    out = tmp_path / "out"
    ret = rt.run_tests(project_root=str(repo), out_dir=str(out), repo_gate=False,
                       state_dir=str(tmp_path / "state"))
    report = json.loads((out / "test_report.json").read_text())

    node = report["node"]
    assert node["skipped"] is True
    assert "unmeasured" not in node, node.keys()
    assert report["node_gate_complete"] is False, report["node_gate_complete"]
    assert ret["node_gate_complete"] is False, ret.get("node_gate_complete")
