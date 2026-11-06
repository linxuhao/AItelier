# tests/unit/test_node_gate_complete_return.py
# The node gate's completeness verdict is routed from run_tests' RETURN, never
# from the report FILE. Deleting that field from the file must not change where
# `configs/fix_tests.yaml` or `configs/subagent.yaml` send a partial report.
#
# What this would catch: a regression that moves either config's
# `node_gate_complete` matcher back onto `from_file: test_report.json` (the
# shape measured by the 2026-11-06 non-author review, reports/
# gate-absence-review-r2-20261106.json at 69f53a084f). With the field deleted
# from the file, that matcher finds nothing, the measured-partial report selects
# no edge, and the refusal assertions below fail. It would also catch a producer
# that stops putting the verdict on the return, or puts a value there that
# disagrees with the file.
#
# Driven, not inferred:
#   * the reports and returns come from the REAL `rt.run_tests`. Only the process
#     boundary (`repository_executor.execute`) is served, the same seam
#     test_partial_gate_cannot_reach_done.py uses;
#   * the transition is chosen by the ENGINE's `GraphResolver.resolve_transition`
#     over each config's own parsed `test` step. No matcher is re-implemented here.
#     The flags handed to the resolver are the tool's RETURN, which the engine
#     merges into the step's flags; the file is only supplied to the red edge,
#     which still reads `passed` from it.
import json
import subprocess
from pathlib import Path

import pytest
from skillflow.graph import GraphResolver, PipelineGraph

import aitelier.tools.run_tests.impl as rt

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = {name: ROOT / "configs" / f"{name}.yaml" for name in ("fix_tests", "subagent")}
SUCCESS_TARGET = {"fix_tests": "done", "subagent": "review"}
REFUSAL_TARGET = "node_gate_incomplete"


def _project(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps(
        {"name": "p-" + directory.name, "version": "0.0.0",
         "scripts": {"build": "build-it", "test": "test-it"}}))
    (directory / "package-lock.json").write_text("{}")


def _nine(repo):
    """Nine node projects in gate order. With no declaration the gate visits the
    first eight, so the ninth is a real `capped` absence: the measured-partial
    shape. Returns the gate's visit order, for a fully declared control."""
    _project(repo)
    _project(repo / "web")
    for n in range(1, 8):
        _project(repo / "packages" / f"p{n}")
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_probe_ok.py").write_text(
        "def test_probe_ok():\n    assert True\n")
    return [".", "web"] + [f"packages/p{n}" for n in range(1, 8)]


def _serve(monkeypatch):
    """Serve npm in-process; the pytest leg runs for real through the fixture."""
    from tests.gate_fixture import install_entry_executor

    install_entry_executor(monkeypatch)
    inner = rt.repository_executor.execute

    def execute(repo_dir, args, timeout, **kw):
        if not kw.get("profile"):
            return inner(repo_dir, args, timeout, **kw)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rt.repository_executor, "execute", execute)


def _run(tmp_path, monkeypatch, repo):
    """Run the real `run_tests`. Returns (report, tool_return, report_dir): the
    report is what the FILE holds, the return is what the engine merges into the
    step's flags, and report_dir is the `out_dir` the file was written to."""
    _serve(monkeypatch)
    out = tmp_path / "out"
    ret = rt.run_tests(project_root=str(repo), out_dir=str(out),
                       repo_gate=False, state_dir=str(tmp_path / "state"))
    report = json.loads((out / "test_report.json").read_text(encoding="utf-8"))
    return report, ret, out


def _delete_field_from_file(out: Path, field: str = "node_gate_complete") -> None:
    """Remove exactly one field from the report file the producer wrote."""
    path = out / "test_report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert field in report, f"the producer did not write {field} into the file"
    del report[field]
    path.write_text(json.dumps(report), encoding="utf-8")


def _select(config_name: str, flags: dict, report_dir: Path):
    """(transition, target) the ENGINE's resolver picks for `test` with `flags`."""
    graph = PipelineGraph.from_yaml(CONFIGS[config_name])
    resolver = GraphResolver(graph)

    def read(path: str) -> str:
        return (report_dir / path).read_text(encoding="utf-8")

    return resolver.resolve_transition("test", flags, {}, file_reader=read)


# ------------------------------------------------ the producer's two statements
def test_return_and_file_both_say_incomplete_for_the_measured_partial_shape(tmp_path, monkeypatch):
    """The measured-partial shape: `passed` is true (the projects that ran are
    green) and one node project was never visited. Both carriers must say the
    gate was NOT complete, and they must agree."""
    repo = tmp_path / "repo"
    _nine(repo)

    report, ret, _ = _run(tmp_path, monkeypatch, repo)

    assert report["passed"] is True, report["failures"]
    assert ret["node_gate_complete"] is False, ret.get("node_gate_complete")
    assert isinstance(ret["node_gate_complete"], bool)
    assert report["node_gate_complete"] is ret["node_gate_complete"]


def test_return_and_file_both_say_complete_for_the_green_control(tmp_path, monkeypatch):
    """The green control: every node project declared and measured. The same
    producer, the same graph, only the completeness changes."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    report, ret, _ = _run(tmp_path, monkeypatch, repo)

    assert report["passed"] is True, report["failures"]
    assert "node_unmeasured" not in report
    assert ret["node_gate_complete"] is True, ret.get("node_gate_complete")
    assert isinstance(ret["node_gate_complete"], bool)
    assert report["node_gate_complete"] is ret["node_gate_complete"]


# ------------------------------------- the file's field is not what routes
@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_partial_report_with_its_field_deleted_from_the_file_still_refuses(
        config_name, tmp_path, monkeypatch):
    """A report the REAL producer wrote, whose `node_gate_complete` is then deleted
    from the file, must still land on the refusal edge, never on success.

    Would catch: a config that routes on `from_file: test_report.json` for this
    field. The deleted field would match nothing and the partial report would
    select no edge, so the refusal target assertion fails. A producer that drops
    the verdict from the return fails it the same way.
    """
    repo = tmp_path / "repo"
    _nine(repo)
    _, ret, out = _run(tmp_path, monkeypatch, repo)
    assert ret["node_gate_complete"] is False, ret.get("node_gate_complete")

    _delete_field_from_file(out)
    transition, target = _select(config_name, ret, out)

    assert target == REFUSAL_TARGET, (transition, target)
    assert target != SUCCESS_TARGET[config_name], (transition, target)


@pytest.mark.parametrize("config_name", ["fix_tests", "subagent"])
def test_green_control_still_reaches_success_when_the_file_field_is_deleted(
        config_name, tmp_path, monkeypatch):
    """Control for the test above: deleting the field from the file does not move a
    genuinely complete green report. The verdict is the return's, so the file is
    not consulted. This is why the refusal above is caused by the verdict, and
    not by the deletion breaking every route.

    Would catch: a config that routes a complete green run off the file, so that
    deleting the field stops it reaching success.
    """
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))
    _, ret, out = _run(tmp_path, monkeypatch, repo)
    assert ret["node_gate_complete"] is True, ret.get("node_gate_complete")

    _delete_field_from_file(out)
    transition, target = _select(config_name, ret, out)

    assert target == SUCCESS_TARGET[config_name], (transition, target)
