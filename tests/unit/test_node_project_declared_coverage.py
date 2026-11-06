# tests/unit/test_node_project_declared_coverage.py
# The witness for `design.gate-absence-has-a-disposition-20261106`: a repository
# past NODE_PROJECT_CAP must not sit in a permanent human park, and the report
# must say which projects were not visited and why.
#
# Driven through `run_tests`, not through `_find_node_projects`: the claim is
# about what a RELEASE reads (`report_state`, `release_disposition`, the shipped
# `test_evidence` schema), and a probe that calls the helper directly would
# confirm the helper while leaving the report unmeasured. The only seam is
# `repository_executor.execute` (the process boundary), as in the adopted
# non-author probe `test_node_gate_measured_semantics.py`.
import json
from pathlib import Path

import pytest

import aitelier.tools.run_tests.impl as rt
from aitelier.gate_evidence import release_disposition, report_state


def _report(out_dir):
    return json.loads((Path(out_dir) / "test_report.json").read_text())


def _project(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps(
        {"name": "p-" + directory.name, "version": "0.0.0",
         "scripts": {"build": "build-it", "test": "test-it"}}))
    (directory / "package-lock.json").write_text("{}")


def _nine(repo):
    """Nine node projects in this repository's own layouts, in gate order:
    root, `web/`, then seven under `packages/` (sorted)."""
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
    import subprocess

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
    _serve(monkeypatch, red=red)
    out = tmp_path / "out"
    result = rt.run_tests(project_root=str(repo), out_dir=str(out),
                          repo_gate=False, state_dir=str(tmp_path / "state"))
    return result, _report(out)


def _evidence_schema():
    """The `test_evidence` schema `configs/coding_impl.yaml` validates."""
    import yaml

    config = yaml.safe_load(
        (Path(rt.__file__).resolve().parents[3] / "configs" / "coding_impl.yaml")
        .read_text(encoding="utf-8"))
    for step in config["steps"]:
        if step.get("id") == "test_evidence":
            return step["tool_params"]["inline_schema"]
    raise AssertionError("test_evidence step not found")


# ------------------------------------------------------------------ the defect
def test_capped_tree_names_the_project_it_did_not_visit_and_parks(tmp_path, monkeypatch):
    """Nine green projects, no declaration: the run is partial, not passed, and
    the report says which project and which file would fix it."""
    repo = tmp_path / "repo"
    order = _nine(repo)

    result, report = _run(tmp_path, monkeypatch, repo)

    node = report["node"]
    assert node["measured_dirs"] == order[:8], node["measured_dirs"]
    absent = report["node_unmeasured"]
    assert [e["dir"] for e in absent] == [order[8]], absent
    assert absent[0]["kind"] == "capped"
    # The words a person needs: the directory, why, and the one edit that ends it.
    assert order[8] in node["summary"] and "not measured" in node["summary"], node["summary"]
    assert "node_projects.json" in absent[0]["reason"], absent[0]["reason"]
    # Every measured project was green, so this is not a red...
    assert node["passed"] is True and report["passed"] is True, report["failures"]
    # ...and it is not a pass either. Nothing downstream is told otherwise.
    assert report_state(report) == "partial", report_state(report)
    assert release_disposition(report) == "unresolved", release_disposition(report)
    # The consumer that exists refuses it: an implementer may not claim green
    # tests while a project never ran.
    import jsonschema

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(report, _evidence_schema())


def test_a_declaration_makes_the_same_tree_fully_measured(tmp_path, monkeypatch):
    """The same nine projects, declared: nothing is absent, nothing parks."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps({"projects": order}))

    result, report = _run(tmp_path, monkeypatch, repo)

    node = report["node"]
    assert node["measured_dirs"] == order, node["measured_dirs"]
    assert node["unmeasured"] == []
    assert node["skipped"] is False
    assert "node_unmeasured" not in report
    assert report_state(report) == "passed", report_state(report)
    assert release_disposition(report) == "passed", release_disposition(report)
    assert result["passed"] is True


def test_a_declaration_that_omits_a_project_cannot_hide_it(tmp_path, monkeypatch):
    """The asymmetry that makes reading a repository-authored file safe: the
    declaration decides what RUNS, discovery decides what must be REPORTED."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    omitted = order[3]
    (repo / "node_projects.json").write_text(json.dumps({"projects": order[:3]}))

    result, report = _run(tmp_path, monkeypatch, repo)

    absent = {e["dir"]: e["kind"] for e in report["node_unmeasured"]}
    assert omitted in absent, absent
    assert absent[omitted] == "undeclared"
    assert set(report["node"]["measured_dirs"]) == set(order[:3])
    assert report_state(report) == "partial", report_state(report)
    assert release_disposition(report) == "unresolved", release_disposition(report)


def test_a_declaration_may_add_a_project_discovery_cannot_see(tmp_path, monkeypatch):
    """A declaration ADDS coverage too: a package.json three levels down is
    measured because the repository named it."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    deep = "tools/deep/thing"
    _project(repo / deep)
    (repo / "node_projects.json").write_text(
        json.dumps({"projects": order + [deep]}))

    result, report = _run(tmp_path, monkeypatch, repo)

    node = report["node"]
    assert deep in node["measured_dirs"], node["measured_dirs"]
    assert f"{deep}:test" in node["checks"], sorted(node["checks"])
    assert "node_unmeasured" not in report
    assert report_state(report) == "passed", report_state(report)


@pytest.mark.parametrize("declaration,words", [
    ({"projects": ["../outside"]}, "inside the repository"),
    ({"projects": ["web/missing"]}, "no package.json"),
    ({"projects": []}, "non-empty"),
    ({"projects": [f"packages/p{n}" for n in range(40)]}, "at most"),
    (["web"], "non-empty"),
])
def test_an_unusable_declaration_is_reported_not_obeyed(tmp_path, monkeypatch,
                                                         declaration, words):
    """A broken declaration falls back to inference AND reports itself: silence
    would be the one way a declaration could hide a project."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text(json.dumps(declaration))

    result, report = _run(tmp_path, monkeypatch, repo)

    node = report["node"]
    # Inference still ran and still measured a full cap of projects.
    assert node["measured_dirs"] == order[:8], node["measured_dirs"]
    absent = report["node_unmeasured"]
    kinds = [e["kind"] for e in absent]
    assert "declaration" in kinds, absent
    entry = next(e for e in absent if e["kind"] == "declaration")
    assert entry["dir"] == "node_projects.json"
    assert words in entry["reason"], entry["reason"]
    assert report_state(report) == "partial", report_state(report)
    assert release_disposition(report) == "unresolved", release_disposition(report)


def test_a_measured_red_still_fails_beside_an_absence(tmp_path, monkeypatch):
    """The other load-bearing property: one project's red is not softened by
    another project's absence."""
    repo = tmp_path / "repo"
    order = _nine(repo)

    result, report = _run(tmp_path, monkeypatch, repo, red=("web", "test"))

    assert report["node"]["passed"] is False
    assert report["passed"] is False, report["failures"]
    assert report_state(report) != "passed", report_state(report)
    assert release_disposition(report) != "partial", release_disposition(report)
    assert [e["dir"] for e in report["node_unmeasured"]] == [order[8]]


def test_a_declaration_that_is_not_json_is_reported_not_obeyed(tmp_path, monkeypatch):
    """A file that cannot be parsed is refused like any other unusable
    declaration, and the run still measures what inference finds. The
    parametrized cases above cover shape; this one covers bytes."""
    repo = tmp_path / "repo"
    order = _nine(repo)
    (repo / "node_projects.json").write_text('{"projects": ["web",')

    result, report = _run(tmp_path, monkeypatch, repo)

    assert report["node"]["measured_dirs"] == order[:8]
    entry = next(e for e in report["node_unmeasured"] if e["kind"] == "declaration")
    assert "not readable JSON" in entry["reason"], entry["reason"]
    assert report_state(report) == "partial", report_state(report)
