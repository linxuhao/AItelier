"""Static graph and coverage protocol tests; detector rows here are synthetic.

No engine is run and no runtime saving/context independence is claimed.
"""
import copy
import json
import subprocess
from pathlib import Path

import pytest

from aitelier import gate_evidence as ge
from aitelier import scoped_gate as sg
from aitelier.gate_coverage import full_coverage, source_scope_refusal
from aitelier.scoped_gate import (select_scope, spec_digest, validate_coverage,
                                 publication_scope_refusal)
from aitelier.tools.run_tests import impl as rt


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(repo):
    git(repo, "add", "project.godot", "a.tscn", "b.tscn", "c.tscn", "a.gd", "b.gd", "c.gd")
    git(repo, "commit", "-m", "synthetic graph")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def graph(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Scope Fixture")
    git(tmp_path, "config", "user.email", "scope@example.invalid")
    (tmp_path / "project.godot").write_text("[application]\n")
    for name in "abc":
        (tmp_path / f"{name}.tscn").write_text(
            f'[gd_scene]\n[ext_resource path="res://{name}.gd"]\n')
        (tmp_path / f"{name}.gd").write_text("extends Node\nvar count = 0\n")
    base = commit(tmp_path)
    (tmp_path / "a.gd").write_text("extends Node\nvar count = 1\n")
    head = commit(tmp_path)
    spec = {"scenarios": [{"name": n, "scene": f"res://{n}.tscn", "timeline": [
        {"at": 20, "assert": [{"name": "N.count", "node": "N", "expr": "count == 1"}]}]}
        for n in "abc"]}
    return tmp_path, base, head, spec


def measured(head, spec, *, flagged=(), historical=()):
    # Shape copied from the VERIFIED native detector, values deliberately
    # synthetic: this tests consumption, never establishes a real measurement.
    names = [s["name"] for s in spec["scenarios"]]
    cells = ("fixed_delta_load0", "fixed_delta_loaded",
             "real_time_cap_load0", "real_time_cap_loaded")
    rows = {cell: [[n, "N.count", "N", "count == 1", 20,
                   not (n in flagged and cell == cells[3]), 1] for n in names]
            for cell in cells}
    return {"head_sha": head, "spec_sha256": spec_digest(spec),
            "batch_context_dependent_scenarios": list(historical),
            "detector_result": {"assertion_rows": rows, "load_bite": {
                n: {"bit": True, "delta_sec": 1.0, "frames_load0": 50,
                    "frames_loaded": 50} for n in names}}}


def plan(graph, measurement=None):
    repo, base, head, spec = graph
    return select_scope(repo, base, head, spec,
                        measurement if measurement is not None else measured(head, spec))


def test_literal_resource_edges_select_the_changed_scene_and_name_the_omission(graph):
    scope = plan(graph)
    assert scope["coverage"] == "subset"
    assert scope["selected_scenarios"] == ["a"]
    assert scope["unselected_scenarios"] == ["b", "c"]
    assert scope["selection_basis"]["changed_symbols"] == {"a.gd": ["count"]}
    assert scope["selection_basis"]["resource_intersections"]["a"] == ["a.gd"]
    assert validate_coverage(scope) is None


def test_detector_positive_and_known_context_case_are_always_included(graph):
    _, _, head, spec = graph
    scope = plan(graph, measured(head, spec, flagged=["b"], historical=["c"]))
    assert scope["selected_scenarios"] == ["a", "b", "c"]
    assert scope["always_included_scenarios"] == ["b", "c"]
    assert scope["fallback_full"] is False


def test_selection_preserves_author_order_and_required_replays(graph):
    repo, base, head, spec = graph
    spec["scenarios"][1]["repeatability"] = True
    replay = copy.deepcopy(spec["scenarios"][1])
    replay["name"] = "replay-of-b"
    spec["scenarios"] = [spec["scenarios"][2], spec["scenarios"][1],
                         spec["scenarios"][0], replay]
    scope = select_scope(repo, base, head, spec, measured(head, spec, flagged=["b"]))
    assert scope["selected_scenarios"] == ["b", "a", "replay-of-b"]
    assert scope["unselected_scenarios"] == ["c"]
    assert validate_coverage(scope) is None


@pytest.mark.parametrize("mutation", ["missing", "old_tree", "partial_cell", "no_bite", "duplicates", "wrong_assertion"])
def test_incomplete_or_unreached_detector_evidence_falls_back_full(graph, mutation):
    repo, base, head, spec = graph
    data = measured(head, spec)
    if mutation == "missing":
        data = None
    elif mutation == "old_tree":
        data["head_sha"] = base
    elif mutation == "partial_cell":
        data["detector_result"]["assertion_rows"]["fixed_delta_loaded"].pop()
    elif mutation == "duplicates":
        rows = data["detector_result"]["assertion_rows"]["fixed_delta_loaded"]
        rows.append(rows[0])
    elif mutation == "wrong_assertion":
        data["detector_result"]["assertion_rows"]["fixed_delta_loaded"][0][3] = "true"
    else:
        data["detector_result"]["load_bite"]["c"]["bit"] = False
    scope = select_scope(repo, base, head, spec, data)
    assert scope["fallback_full"] is True
    assert scope["coverage"] == "full"
    assert scope["selected_scenarios"] == ["a", "b", "c"]
    assert scope["selection_basis"]["fallback_reason"]


@pytest.mark.parametrize("code", ['var x = load(path)\n', 'var x = preload("res://absent.tscn")\n',
                                 'var x = FileAccess.open(path, FileAccess.READ)\n'])
def test_dynamic_and_missing_resource_references_never_produce_empty(graph, code):
    repo, base, _, spec = graph
    (repo / "c.gd").write_text("extends Node\n" + code)
    head = commit(repo)
    scope = select_scope(repo, base, head, spec, measured(head, spec))
    assert scope["fallback_full"] and scope["selected_scenarios"] == ["a", "b", "c"]


def test_class_reference_is_an_actual_cross_scene_edge(graph):
    repo, base, _, spec = graph
    (repo / "a.gd").write_text("class_name SharedRule\nextends Node\n")
    (repo / "b.gd").write_text("extends Node\nvar rule: SharedRule\n")
    link = commit(repo)
    (repo / "a.gd").write_text("class_name SharedRule\nextends Node\nvar count = 2\n")
    head = commit(repo)
    scope = select_scope(repo, link, head, spec, measured(head, spec))
    assert scope["selected_scenarios"] == ["a", "b"]
    assert scope["unselected_scenarios"] == ["c"]


def test_removed_reference_at_base_still_selects_the_previous_user(graph):
    repo, _, _, spec = graph
    (repo / "b.gd").write_text('extends Node\nvar rule = preload("res://a.gd")\n')
    base = commit(repo)
    (repo / "b.gd").write_text("extends Node\nvar count = 2\n")
    (repo / "a.gd").write_text("extends Node\nvar count = 3\n")
    head = commit(repo)
    scope = select_scope(repo, base, head, spec, measured(head, spec))
    assert scope["selected_scenarios"] == ["a", "b"]


def test_shared_autoload_change_selects_all_scenarios(graph):
    repo, _, _, spec = graph
    (repo / "project.godot").write_text('[autoload]\nGlobal="*res://a.gd"\n')
    base = commit(repo)
    (repo / "a.gd").write_text("extends Node\nvar count = 4\n")
    head = commit(repo)
    scope = select_scope(repo, base, head, spec, measured(head, spec))
    assert scope["coverage"] == "full" and not scope["fallback_full"]


def test_unmapped_change_and_failed_git_select_explicit_full(graph):
    repo, base, head, spec = graph
    (repo / "project.godot").write_text("[application]\nconfig/name=\"new\"\n")
    head = commit(repo)
    for revision in (head, "nonexistent"):
        scope = select_scope(repo, base, revision, spec)
        assert scope["fallback_full"]
        assert scope["selected_scenarios"] == ["a", "b", "c"]


def test_an_unexpected_selector_exception_is_an_explicit_full_fallback(graph, monkeypatch):
    monkeypatch.setattr(sg, "_graph", lambda *_: (_ for _ in ()).throw(RuntimeError("reader broke")))
    scope = plan(graph)
    assert scope["fallback_full"] and scope["selected_scenarios"] == ["a", "b", "c"]
    assert scope["selection_basis"]["fallback_reason"] == "RuntimeError: reader broke"


def test_unreachable_missing_resource_does_not_invent_a_runtime_edge(graph):
    repo, _, _, spec = graph
    (repo / "unused.gd").write_text('extends Node\nvar unused = preload("res://missing.tscn")\n')
    git(repo, "add", "unused.gd")
    git(repo, "commit", "-m", "unused fixture")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "a.gd").write_text("extends Node\nvar count = 5\n")
    head = commit(repo)
    scope = select_scope(repo, base, head, spec, measured(head, spec))
    assert scope["selected_scenarios"] == ["a"] and not scope["fallback_full"]


def test_zero_or_ambiguous_contract_is_an_error_not_an_empty_success(graph):
    repo, base, head, _ = graph
    for spec in ({}, {"scenarios": []}, {"scenarios": [{"name": "a"}, {"name": "a"}]}):
        with pytest.raises(ValueError):
            select_scope(repo, base, head, spec)


def test_publication_conjunct_rejects_subset_and_missing_and_accepts_full(graph):
    subset = plan(graph)
    assert publication_scope_refusal(subset) == "subset gate cannot authorize publication"
    assert publication_scope_refusal(None) == "coverage marker is missing or invalid"
    full = copy.deepcopy(subset)
    full.update(coverage="full", selected_scenarios=full["all_scenarios"], unselected_scenarios=[])
    assert publication_scope_refusal(full) is None
    full["selected_scenarios"] = ["a"]
    assert publication_scope_refusal(full)
    assert ge.release_disposition({"passed": True, "repo_gate": {}, "gate_coverage": None}) == "unresolved"
    assert ge.release_disposition({"passed": False, "passed_relative": True,
        "baseline_state": "compared", "repo_gate": {}, "gate_coverage": subset}) == "known_failure"


def test_full_producer_payload_requires_exact_independent_source_contract(graph):
    _, _, head, spec = graph
    scope = full_coverage(head, spec)
    assert source_scope_refusal(scope, head, spec) is None
    assert "head" in source_scope_refusal(scope, "f" * 40, spec)
    changed = copy.deepcopy(spec)
    changed["scenarios"][0]["timeline"][0]["assert"][0]["expr"] = "count == 2"
    assert "digest" in source_scope_refusal(scope, head, changed)
    wrong = copy.deepcopy(scope)
    wrong["all_scenarios"].reverse()
    wrong["selected_scenarios"].reverse()
    assert "inventory/order" in source_scope_refusal(wrong, head, spec)


def test_attributed_manifest_carries_coverage_whole_and_missing_marker_is_unmeasured(graph, tmp_path):
    scope = plan(graph)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"gate_coverage": scope}))
    retained = rt._Retained(tmp_path, [], None, True, rt.ATTRIBUTION_OWN, None, [], {})
    observed, error = rt._retained_coverage(retained)
    assert observed == scope and error is None
    assert rt._repo_gate_outcome({"returncode": 0, "gate_coverage": observed}) == rt.REPO_GATE_MEASURED_PASS
    manifest.write_text("{}")
    observed, error = rt._retained_coverage(retained)
    assert observed is None and error
    assert rt._repo_gate_outcome({"returncode": 0, "gate_coverage_error": error}) == rt.REPO_GATE_UNMEASURED
    assert rt._repo_gate_outcome({"returncode": 1, "gate_coverage_error": error,
                                  "retained_findings": 1}) == rt.REPO_GATE_MEASURED_FAIL


def test_scope_survives_test_report_current_cycle_and_audit(graph, tmp_path, monkeypatch):
    repo, _, _, _ = graph
    scope = plan(graph)
    gate = {"script": "run_tests.sh", "returncode": 0, "passed": True,
            "output": "x" * 5000, "output_truncated": True,
            "measured": rt.REPO_GATE_MEASURED_PASS, "gate_coverage": scope}
    monkeypatch.setattr(rt, "_acquire_repo_gate", lambda _: gate)
    # No Python product tests and no gate process; the real report writer and
    # routing fold consume a structured synthetic gate verdict.
    result = rt.run_tests(project_root=str(repo), out_dir=str(tmp_path / "graph" / "test"),
                          run_id="scope-fixture", evidence_cycle_start=True)
    report = json.loads((tmp_path / "graph" / "test" / "test_report.json").read_text())
    assert result["gate_coverage"] == report["gate_coverage"] == scope
    assert report["repo_gate"]["gate_coverage"] == scope
    assert result["release_evidence"] == "unresolved"
    audit = ge.audit_evidence(tmp_path / "graph", "scope-fixture", [("test", "test_report.json")])
    assert not audit["passed"]
    assert audit["gates_audited"][0]["gate_coverage"] == scope
    # This synthetic product has no Python tests, which the existing audit
    # correctly retains as skipped. Coverage must not erase that absence.
    assert audit["gates_audited"][0]["state"] == "skipped"
    assert audit["gates_audited"][0]["gate_coverage_error"] == "subset gate cannot authorize publication"
    clean = {"passed": True, "gate_coverage": scope}
    ge.stamp_report(clean, run_id="scope-fixture", out_dir=str(tmp_path / "graph" / "test"))
    (tmp_path / "graph" / "test" / "clean.json").write_text(json.dumps(clean))
    audited = ge.audit_evidence(tmp_path / "graph", "scope-fixture", [("test", "clean.json")])
    assert not audited["passed"] and audited["state"] == "coverage_incomplete"


def test_subset_does_not_erase_unmeasured_known_red_gate_cases(graph, tmp_path, monkeypatch):
    repo, _, _, _ = graph
    scope = plan(graph)
    baseline_dir = rt._baseline_dir(str(tmp_path / "state"), repo)
    Path(baseline_dir).mkdir(parents=True)
    path = Path(baseline_dir) / rt.BASELINE_FILE
    known = "repo_gate:run_tests.sh#playtest/omitted/assertion"
    path.write_text(json.dumps({"failures": [known]}))
    monkeypatch.setattr(rt, "_acquire_repo_gate", lambda _: {
        "script": "run_tests.sh", "returncode": 0, "passed": True,
        "output": "subset green", "measured": rt.REPO_GATE_MEASURED_PASS,
        "gate_coverage": scope})
    rt.run_tests(project_root=str(repo), out_dir=str(tmp_path / "out"),
                 state_dir=str(tmp_path / "state"))
    assert json.loads(path.read_text())["failures"] == [known]
