# tests/unit/test_partial_gate_cannot_reach_done.py
# A node gate that measured only SOME of a repository's projects must not reach
# a success terminal in either live pipeline that reads `test_report.json`.
#
# The defect: `passed` is honestly true there (the projects that ran passed),
# and `configs/fix_tests.yaml` / `configs/subagent.yaml` routed on the raw
# boolean, so a repository past the node visit cap concluded "success" while a
# project never ran. The fix is a flat `node_gate_complete` written by
# `run_tests`, and one transition in each config, ordered before `passed: true`.
#
# Driven, not inferred:
#   * the reports come from the REAL `rt.run_tests` (only the process boundary,
#     `repository_executor.execute`, is served — the same seam
#     `test_node_project_declared_coverage.py` uses);
#   * the transition is selected by the ENGINE's own resolver,
#     `skillflow.graph.GraphResolver.resolve_transition`, with a `file_reader`
#     that reads the report file exactly as the scheduler does. Nothing here
#     re-implements the match rule.
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
    """Run the real `run_tests` and return (report_dict, report_dir).

    `report_dir` is the `out_dir` the report was written to — the directory the
    config's `from_file: test_report.json` reads from."""
    _serve(monkeypatch, red=red)
    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out),
                 repo_gate=False, state_dir=str(tmp_path / "state"))
    report = json.loads((out / "test_report.json").read_text())
    return report, out


def _select(config_name: str, report_dir: Path) -> str | None:
    """The `test` step's transition the ENGINE takes for the report in `report_dir`.

    Uses `GraphResolver.resolve_transition` with a `file_reader` over the report
    directory — the same contract the scheduler gives the engine. `flags` are
    empty: a `from_file` match reads the file, and no return-dict flag is needed.
    """
    graph = PipelineGraph.from_yaml(CONFIGS[config_name])
    resolver = GraphResolver(graph)

    def read(path: str) -> str:
        return (report_dir / path).read_text(encoding="utf-8")

    transition, target = resolver.resolve_transition(
        "test", {}, {}, file_reader=read)
    assert transition is not None, f"{config_name}: no transition matched"
    return target


# --------------------------------------------------------------- the flag itself
def test_partial_report_writes_node_gate_complete_false(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    order = _nine(repo)

    report, _ = _run(tmp_path, monkeypatch, repo)

    # The defect's precondition, still true: the report says nothing failed.
    assert report["passed"] is True, report["failures"]
    assert [e["dir"] for e in report["node_unmeasured"]] == [order[8]]
    assert report["node_gate_complete"] is False


def test_fully_measured_green_report_writes_node_gate_complete_true(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    report, _ = _run(tmp_path, monkeypatch, repo)

    assert report["passed"] is True, report["failures"]
    assert "node_unmeasured" not in report
    assert report["node_gate_complete"] is True


def test_python_only_repository_has_no_node_gate_complete(tmp_path, monkeypatch):
    """No package.json anywhere: there is no node section, so the flag is absent
    and the report routes exactly as it did before this change."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_probe_ok.py").write_text(
        "def test_probe_ok():\n    assert True\n")

    report, _ = _run(tmp_path, monkeypatch, repo)

    assert "node" not in report
    assert "node_gate_complete" not in report


def test_red_measured_report_writes_node_gate_complete_true(tmp_path, monkeypatch):
    """A measured red is a complete measurement: the flag is True, and the
    report routes to fix/work through the existing `passed: false` edge."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    report, _ = _run(tmp_path, monkeypatch, repo, red=("web", "test"))

    assert report["passed"] is False, report["failures"]
    assert report["node_gate_complete"] is True


# ---------------------------------------------------------- the routing, per config
@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_partial_report_selects_the_terminal_not_success(config_name, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _nine(repo)
    _, out = _run(tmp_path, monkeypatch, repo)

    assert _select(config_name, out) == PARTIAL_TARGET


@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_green_fully_measured_report_still_selects_success(config_name, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))
    _, out = _run(tmp_path, monkeypatch, repo)

    assert _select(config_name, out) == GREEN_TARGET[config_name]


@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_red_measured_report_still_selects_fix_loop(config_name, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))
    _, out = _run(tmp_path, monkeypatch, repo, red=("web", "test"))

    assert _select(config_name, out) == RED_TARGET[config_name]


@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_partial_report_with_a_measured_red_still_selects_fix_loop(config_name, tmp_path, monkeypatch):
    """A red in a measured project keeps its fix loop even when another project
    was not measured. This is the case the partial edge must NOT capture: its
    `node_gate_complete` is also false, and only the failing edge (ordered first)
    keeps it on the fix loop instead of the terminal."""
    repo = tmp_path / "repo"
    _nine(repo)  # ninth project is capped out → node_gate_complete False
    _, out = _run(tmp_path, monkeypatch, repo, red=("web", "test"))

    assert _select(config_name, out) == RED_TARGET[config_name]


@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_python_only_green_report_still_selects_success(config_name, tmp_path, monkeypatch):
    """No node section: the new edge is inert and green still reaches success."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_probe_ok.py").write_text(
        "def test_probe_ok():\n    assert True\n")
    _, out = _run(tmp_path, monkeypatch, repo)

    assert _select(config_name, out) == GREEN_TARGET[config_name]


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
    `skipped` term survived 147 tests; this is the path that distinguishes it."""
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
    rt.run_tests(project_root=str(repo), out_dir=str(out), repo_gate=False,
                 state_dir=str(tmp_path / "state"))
    report = json.loads((out / "test_report.json").read_text())

    node = report["node"]
    assert node["skipped"] is True
    assert "unmeasured" not in node, node.keys()
    assert report["node_gate_complete"] is False, report["node_gate_complete"]
