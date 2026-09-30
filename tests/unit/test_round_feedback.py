"""Focused synthetic transport tests. No engine or context-independence proof."""
import copy
import json
import subprocess
from pathlib import Path
import pytest
from aitelier import round_feedback as rf
from aitelier.gate_coverage import publication_scope_refusal, full_coverage
from aitelier import gate_evidence as ge
from aitelier.tools.godot_playtest import impl as pt
from aitelier.tools.run_tests import impl as rt


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    git(repo, "init", "-q"); git(repo, "config", "user.name", "Feedback Fixture")
    git(repo, "config", "user.email", "feedback@example.invalid")
    (repo / "project.godot").write_text("[application]\n")
    scenarios = []
    for n in "abcde":
        (repo / (n + ".gd")).write_text("extends Node\nvar value = 0\n")
        (repo / (n + ".tscn")).write_text('[gd_scene]\n[ext_resource path="res://' + n + '.gd"]\n')
        scenarios.append({"name": n, "scene": "res://" + n + ".tscn", "timeline": [
            {"at": 4, "assert": [{"name": "N.value", "node": "N", "expr": "value == 1"}]}]})
    git(repo, "add", "project.godot", *[n + s for n in "abcde" for s in (".gd", ".tscn")])
    git(repo, "commit", "-qm", "base"); base = git(repo, "rev-parse", "HEAD")
    (repo / "a.gd").write_text("extends Node\nvar value = 1\n")
    git(repo, "add", "a.gd"); git(repo, "commit", "-qm", "literal probe delta")
    head = git(repo, "rev-parse", "HEAD"); spec = {"scenarios": scenarios}
    scope = rf.feedback_plan(repo, base, head, spec, "playtest/", ["b"], ["c"])
    return repo, base, head, spec, scope, tmp_path / "out"


def raw(spec):
    identities = rf.assertion_identities(spec)
    return {"passed": True, "spec_used": True, "behavior": {"all_passed": True, "scenarios": [
        {"name": name, "ran": True, "passed": True, "asserts": [
            {"name": k[0], "node": k[1], "expr": k[2], "frame": k[3], "passed": True, "actual": 1}
            for k, count in rows.items() for _ in range(count)]} for name, rows in identities.items()]}}


def test_priority_is_a_partial_ordered_partition(world):
    repo, base, head, spec, scope, _ = world
    assert scope["selected_scenarios"] == ["a", "b", "c"]
    assert scope["unselected_scenarios"] == ["d", "e"]
    assert scope["selection_basis"]["direct_consumers"] == {"a": ["a.gd"]}
    assert all("UNKNOWN" in v for v in scope["unselected_observations"].values())
    from aitelier.scoped_gate import select_scope
    assert select_scope(repo, base, head, spec)["coverage"] == "full"


def test_shared_autoload_recommends_full_and_does_not_run(world):
    repo, base, _, spec, _, out = world
    (repo / "project.godot").write_text('[autoload]\nGlobal="*res://a.gd"\n')
    git(repo, "add", "project.godot"); git(repo, "commit", "-qm", "shared")
    scope = rf.feedback_plan(repo, base, "HEAD", spec, "playtest/", ["b"], ["c"])
    assert scope["full_recommended"] and not scope["feedback_available"]
    report = rf.run_feedback(repo, out, scope, spec, {}, lambda _: pytest.fail("must not run"))
    assert report["selected_state"] == "unavailable" and not report["passed"]


@pytest.mark.parametrize("mutation", ["good", "assert_fail", "zero", "missing", "truncated", "duplicate", "predicate", "unrun", "hard", "spec_error", "dirty", "float_frame", "assert_error"])
def test_exact_rows_and_hard_polarities_remain_partial(world, mutation):
    repo, _, _, spec, scope, out = world
    chosen, _ = pt.select_scenarios(spec, scope["selected_scenarios"])
    reply = raw(chosen)
    def transport(payload):
        assert payload["captures"] == 0 and payload["operation_id"] == "fixture-owner"
        rows = reply["behavior"]["scenarios"]
        if mutation == "assert_fail": rows[0]["asserts"][0]["passed"] = False
        if mutation == "zero": rows[0]["asserts"] = []
        if mutation == "missing": return {}
        if mutation == "truncated": rows.pop()
        if mutation == "duplicate": rows[0]["asserts"].append(copy.deepcopy(rows[0]["asserts"][0]))
        if mutation == "predicate": rows[0]["asserts"][0]["expr"] = "true"
        if mutation == "unrun": rows[0]["ran"] = False
        if mutation == "hard": reply["passed"] = False
        if mutation == "spec_error": reply["spec_errors"] = ["unknown timeline key"]
        if mutation == "dirty": (repo / "a.gd").write_text("changed during feedback")
        if mutation == "float_frame": rows[0]["asserts"][0]["frame"] = 4.0
        if mutation == "assert_error": rows[0]["asserts"][0]["error"] = "evaluation failed"
        return reply
    report = rf.run_feedback(repo, out, scope, spec, {"operation_id": "fixture-owner"}, transport)
    assert report["selected_state"] == ("selected_pass" if mutation == "good" else "selected_fail")
    assert report["passed"] is False and report["full_test_passed"] is False
    assert ge.report_state(report) == "partial" and ge.release_disposition(report) == "unresolved"
    assert json.loads((out / "round_feedback_raw.json").read_text()) == reply or mutation == "missing"
    assert report["raw_artifact"]["sha256"] and report["gate_coverage"] == scope
    with pytest.raises(ValueError, match="first attempt"):
        rf.run_feedback(repo, out, scope, spec, {}, transport)


def test_even_all_selected_feedback_cannot_publish_or_override_release(world):
    _, _, head, spec, _, _ = world
    scope = full_coverage(head, spec); scope["purpose"] = rf.PURPOSE
    assert publication_scope_refusal(scope)
    report = {"passed": True, "purpose": rf.PURPOSE, "upstream_state": "passed", "gate_coverage": scope}
    assert ge.report_state(report) == "partial" and ge.release_disposition(report) == "unresolved"


def test_invalid_scope_and_zero_authored_never_contact_transport(world):
    repo, _, _, spec, scope, out = world
    report = rf.run_feedback(repo, out, None, spec, {}, lambda _: pytest.fail("must not run"))
    assert report["selected_state"] == "unavailable"
    bad = copy.deepcopy(spec); bad["scenarios"][0]["timeline"] = []
    with pytest.raises(ValueError, match="zero authored"):
        rf.assertion_identities(bad)


def test_tool_entry_retains_machine_partition(world, monkeypatch):
    repo, base, _, spec, _, out = world
    monkeypatch.setattr(pt, "read_spec", lambda _: (spec, {"source": "playtest/", "errors": []}))
    monkeypatch.setattr(pt, "post_playtest", lambda payload, **kwargs: raw(payload["spec"]))
    result = pt.godot_playtest(project_root=str(repo), out_dir=str(out), purpose=rf.PURPOSE,
                              base_sha=base, sentinel_scenarios=["b"], mandatory_scenarios=["c"])
    assert result["selected_state"] == "selected_pass" and result["passed"] is False
    assert json.loads(Path(result["written"]).read_text())["gate_coverage"] == result["gate_coverage"]


def test_stamped_goal_audit_keeps_partial_machine_scope(world):
    repo, _, _, spec, scope, out = world
    chosen, _ = pt.select_scenarios(spec, scope['selected_scenarios'])
    result = rf.run_feedback(repo, out, scope, spec, {}, lambda _: raw(chosen))
    ge.stamp_report(result, run_id='feedback-fixture', out_dir=str(out), start_cycle=True)
    (out / 'playtest_report.json').write_text(json.dumps(result))
    entry = ge._audit_one(out.parent, 'out', 'playtest_report.json', 'feedback-fixture', result[ge.CYCLE_FIELD])
    assert entry['state'] == 'partial' and entry['passed'] is False
    assert entry['gate_coverage'] == scope and entry['purpose'] == rf.PURPOSE
    assert result['release_evidence'] == 'unresolved'


def test_unknown_tool_purpose_never_contacts_engine(world, monkeypatch):
    repo, _, _, _, _, out = world
    monkeypatch.setattr(pt, 'post_playtest', lambda _: pytest.fail('must not run'))
    with pytest.raises(ValueError, match='unknown playtest purpose'):
        pt.godot_playtest(project_root=str(repo), out_dir=str(out), purpose='typo')


def test_all_selected_provisional_feedback_keeps_old_full_gate_red(world, tmp_path, monkeypatch):
    repo, _, head, spec, _, _ = world
    scope = full_coverage(head, spec); scope['purpose'] = rf.PURPOSE
    baseline_dir = Path(rt._baseline_dir(str(tmp_path / 'state'), repo)); baseline_dir.mkdir(parents=True)
    known = 'repo_gate:run_tests.sh#playtest/old/assertion'
    path = baseline_dir / rt.BASELINE_FILE; path.write_text(json.dumps({'failures': [known]}))
    monkeypatch.setattr(rt, '_acquire_repo_gate', lambda _: {
        'script': 'run_tests.sh', 'returncode': 0, 'passed': True, 'output': 'provisional green',
        'measured': rt.REPO_GATE_MEASURED_PASS, 'gate_coverage': scope})
    result = rt.run_tests(project_root=str(repo), out_dir=str(tmp_path / 'graph/test'),
                          state_dir=str(tmp_path / 'state'), run_id='provisional-fixture', evidence_cycle_start=True)
    assert known in json.loads(path.read_text())['failures']
    assert result['purpose'] == rf.PURPOSE and result['full_test_passed'] is False
    assert result['release_evidence'] == 'unresolved'
    audit = ge.audit_evidence(tmp_path / 'graph', 'provisional-fixture', [('test', 'test_report.json')])
    assert not audit['passed'] and audit['gates_audited'][0]['purpose'] == rf.PURPOSE


@pytest.mark.parametrize("retry", ["bad_base", "missing_spec", "truncated_spec", "success", "malformed_prior"])
def test_tool_retry_preserves_first_report_scope_and_raw_artifacts(world, monkeypatch, retry):
    repo, base, _, spec, scope, out = world
    chosen, _ = pt.select_scenarios(spec, scope["selected_scenarios"])
    reply = raw(chosen)
    reply["behavior"]["scenarios"][0]["asserts"][0]["passed"] = False
    first = rf.run_feedback(repo, out, scope, spec, {"operation_id": "first-negative"}, lambda _: reply)
    assert first["selected_state"] == "selected_fail" and first["gate_coverage"] == scope
    (out / "round_feedback.log").write_text("first raw command/RC log\n")
    if retry == "malformed_prior":
        (out / "playtest_report.json").write_bytes(b'{"gate_coverage": truncated')
    before = {path.name: path.read_bytes() for path in out.iterdir() if path.is_file()}
    reads = []
    def read(_):
        reads.append(True)
        if retry == "missing_spec":
            return None, {"source": "", "errors": ["missing"]}
        if retry == "truncated_spec":
            return {"scenarios": []}, {"source": "playtest/", "errors": []}
        return spec, {"source": "playtest/", "errors": []}
    monkeypatch.setattr(pt, "read_spec", read)
    monkeypatch.setattr(pt, "post_playtest", lambda *a, **k: pytest.fail("retry must not contact transport"))
    with pytest.raises(ValueError, match="first attempt"):
        pt.godot_playtest(project_root=str(repo), out_dir=str(out), purpose=rf.PURPOSE,
                          base_sha="invalid-ref" if retry == "bad_base" else base,
                          sentinel_scenarios=["b"], mandatory_scenarios=["c"])
    assert reads == []
    assert {path.name: path.read_bytes() for path in out.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("artifact", ["round_feedback_request.json", "round_feedback_raw.json", "playtest_report.json"])
def test_incomplete_first_attempt_artifact_blocks_both_entry_points(world, monkeypatch, artifact):
    repo, base, _, spec, scope, out = world
    out.mkdir()
    path = out / artifact
    path.write_bytes(b"first interrupted attempt; do not parse or overwrite")
    before = path.read_bytes()
    monkeypatch.setattr(pt, "read_spec", lambda _: pytest.fail("guard must precede planning"))
    monkeypatch.setattr(pt, "post_playtest", lambda *a, **k: pytest.fail("must not contact transport"))
    with pytest.raises(ValueError, match="first attempt"):
        pt.godot_playtest(project_root=str(repo), out_dir=str(out), purpose=rf.PURPOSE,
                          base_sha=base, sentinel_scenarios=["b"], mandatory_scenarios=["c"])
    with pytest.raises(ValueError, match="first attempt"):
        rf.run_feedback(repo, out, scope, spec, {}, lambda _: pytest.fail("must not run"))
    assert path.read_bytes() == before and sorted(p.name for p in out.iterdir()) == [artifact]
