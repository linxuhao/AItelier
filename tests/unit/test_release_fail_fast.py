"""Release gates stop expensive work only after writing current-cycle evidence."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from aitelier.gate_evidence import (
    audit_evidence,
    first_upstream_blocker,
    release_disposition,
)
from aitelier.tools.godot_compile import impl as compile_impl
from aitelier.tools.godot_vision import impl as vision_impl
from aitelier.tools.run_tests import impl as tests_impl
from core.release_gate_migration import (
    migrate_generated_release_gates,
    migrate_release_document,
    release_gate_ownership_error,
)

ROOT = Path(__file__).resolve().parents[2]
RUN = "run-current"
CYCLE = "cycle-current"
GATES = json.dumps([["5_test", "test_report.json"]])


def _cycle(root: Path, cycle: str = CYCLE) -> None:
    (root / ".evidence_cycle.json").write_text(json.dumps({
        "version": 1, "run_id": RUN, "evidence_cycle_id": cycle,
        "evidence_generated_at": "test",
    }))


def _report(root: Path, step: str, filename: str = "test_report.json",
            **values) -> Path:
    path = root / step / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"passed": True, "run_id": RUN, "evidence_cycle_id": CYCLE}
    body.update(values)
    path.write_text(json.dumps(body))
    return path


@pytest.mark.parametrize(("values", "state"), [
    ({"passed": False, "passed_relative": True}, "known_failure"),
    ({"passed": False, "pending": True}, "pending"),
    ({"passed": True, "skipped": True}, "skipped"),
    ({"passed": False, "blind": True}, "blind"),
    ({"passed": False, "infrastructure_unavailable": True},
     "infrastructure_unavailable"),
])
def test_upstream_states_stay_distinct_and_nonpassing(tmp_path, values, state):
    _cycle(tmp_path)
    _report(tmp_path, "5_test", **values)
    blocker = first_upstream_blocker(tmp_path, RUN,
                                     [["5_test", "test_report.json"]])
    assert blocker["state"] == state
    assert blocker["passed"] is False
    assert release_disposition(values) == (
        "known_failure" if state == "known_failure" else "unresolved")
    verdict = audit_evidence(tmp_path, RUN,
                             [["5_test", "test_report.json"]])
    assert verdict["passed"] is False
    assert verdict["gates_audited"][0]["state"] == state


@pytest.mark.parametrize(("setup", "state"), [
    ("missing", "missing"),
    ("stale", "stale"),
])
def test_missing_and_stale_upstream_never_release(tmp_path, setup, state):
    _cycle(tmp_path)
    if setup == "stale":
        _report(tmp_path, "5_test", evidence_cycle_id="cycle-old")
    blocker = first_upstream_blocker(tmp_path, RUN,
                                     [["5_test", "test_report.json"]])
    assert blocker["state"] == state
    assert blocker["passed"] is False


def test_5_test_failure_writes_fresh_compile_and_playtest_markers(
        tmp_path, monkeypatch):
    _cycle(tmp_path)
    _report(tmp_path, "5_test", passed=False)
    out = tmp_path / "5_compile"
    monkeypatch.setattr(compile_impl, "_godot_compile_unstamped",
                        lambda **_: pytest.fail("compile must not run"))
    result = compile_impl.godot_compile(
        project_root=str(tmp_path), out_dir=str(out), run_id=RUN,
        evidence_cycle_from="5_test", fail_fast_gates=GATES)
    assert result["passed"] is False
    for name in ("compile_report.json", "playtest_report.json"):
        report = json.loads((out / name).read_text())
        assert report["passed"] is False
        assert report["skipped_because"] == "upstream_failed"
        assert report["upstream_state"] == "failed"
        assert report["run_id"] == RUN
        assert report["evidence_cycle_id"] == CYCLE


def test_5_compile_failure_writes_fresh_nonblind_vision_marker(
        tmp_path, monkeypatch):
    _cycle(tmp_path)
    _report(tmp_path, "5_compile", "compile_report.json", passed=False)
    _report(tmp_path, "5_compile", "playtest_report.json", passed=False,
            gate_skipped=True, skipped_because="upstream_failed")
    out = tmp_path / "5_vision"
    monkeypatch.setattr(vision_impl, "_godot_vision_unstamped",
                        lambda **_: pytest.fail("vision must not run"))
    result = vision_impl.godot_vision(
        project_root=str(tmp_path), workspace_root=str(tmp_path.parent),
        config_name=tmp_path.name, out_dir=str(out), run_id=RUN,
        evidence_cycle_from="5_test",
        fail_fast_gates=json.dumps([
            ["5_compile", "compile_report.json"],
            ["5_compile", "playtest_report.json"],
        ]))
    report = json.loads((out / "vision_report.json").read_text())
    assert result["passed"] is False
    assert report["skipped_because"] == "upstream_failed"
    assert report["blind"] is False
    assert report["evidence_cycle_id"] == CYCLE


def test_post_design_final_test_short_circuits_on_prior_failure(
        tmp_path, monkeypatch):
    _cycle(tmp_path)
    _report(tmp_path, "5_vision", "vision_report.json", passed=False,
            gate_skipped=True, skipped_because="upstream_failed", blind=False)
    out = tmp_path / "5_final_test"
    monkeypatch.setattr(tests_impl, "_resolve_pytest_python",
                        lambda *_: pytest.fail("pytest must not run"))
    result = tests_impl.run_tests(
        project_root=str(tmp_path), out_dir=str(out), run_id=RUN,
        evidence_cycle_from="5_test",
        fail_fast_gates=json.dumps([["5_vision", "vision_report.json"]]),
        repo_gate=False)
    report = json.loads((out / "test_report.json").read_text())
    assert result["passed"] is False
    assert report["skipped_because"] == "upstream_failed"
    assert report["evidence_cycle_id"] == CYCLE


def _canonical_game():
    from skillflow.compose import compose_graph
    return compose_graph(
        yaml.safe_load((ROOT / "configs/dpe_default.yaml").read_text()),
        [yaml.safe_load((ROOT / "configs/addons/game_harness.yaml").read_text())],
    )


def _assert_game_release_contract(document):
    steps = {step["id"]: step for step in document["steps"]}
    assert [t["to"] for t in steps["5_test"]["transitions"]] == ["5_compile"]
    assert [t["to"] for t in steps["5_compile"]["transitions"]] == [
        "5_vision", "5_vision", "5_release_wait"]
    assert [t["to"] for t in steps["5_vision"]["transitions"]] == [
        "5_final_test", "5_vision_human", "5_final_test", "5_evidence"
        if "5_evidence" in steps else "5_knowledge", "5_release_wait"]
    assert [t["to"] for t in steps["5_final_test"]["transitions"]] == [
        "5_game_evidence" if "5_game_evidence" in steps else "5_review",
        "5_final_test_replan", "5_release_wait"]
    assert all(step.get("tool_params", {}).get("repo_gate") is False
               for step in document["steps"]
               if step.get("tool_name") == "run_tests")
    assert sum(step.get("tool_name") == "godot_compile"
               for step in document["steps"]) == 1
    assert steps["5_test"]["tool_params"]["evidence_cycle_start"] is True
    assert "5_test" in steps["5_compile"]["tool_params"]["fail_fast_gates"]
    assert "5_compile" in steps["5_vision"]["tool_params"]["fail_fast_gates"]
    assert "5_vision" in steps["5_final_test"]["tool_params"]["fail_fast_gates"]
    assert any(t.get("to") == "5_final_test"
               for t in steps["5_design"]["transitions"])
    wait = steps["5_release_wait"]
    assert wait["checkpoint"] is True
    assert wait["checkpoint_reject_to"] == "3"
    assert wait["transitions"] == [{
        "to": "5_test", "match": {"from": "checkpoint", "value": "approved"},
        "max_loop": 4}]


def test_canonical_game_has_one_full_gate_and_fail_fast_chain():
    _assert_game_release_contract(_canonical_game())


def test_saved_generated_game_is_migrated_to_the_same_release_contract():
    path = (ROOT / "evidence/output-target-migration-20260911/generated-configs/"
            "gen_dpe_state_game.yaml")
    document = yaml.safe_load(path.read_text())
    changes = migrate_release_document(document)
    assert changes
    _assert_game_release_contract(document)
    assert migrate_release_document(copy.deepcopy(document)) == []


def test_prior_r2_generated_shape_upgrades_transactionally():
    path = (ROOT / "evidence/output-target-migration-20260911/generated-configs/"
            "gen_dpe_state_game.yaml")
    document = yaml.safe_load(path.read_text())
    steps = {step["id"]: step for step in document["steps"]}
    steps["5_vision"]["transitions"].insert(2, {
        "to": "5_final_test", "match": {
            "from_file": "vision_report.json", "field": "passed",
            "value": False}})
    before = copy.deepcopy(document)
    assert migrate_release_document(document)
    assert document != before
    _assert_game_release_contract(document)


def test_generated_game_migration_is_atomic_backed_up_and_idempotent(tmp_path):
    source = (ROOT / "evidence/output-target-migration-20260911/generated-configs/"
              "gen_dpe_state_game.yaml")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    target = config_dir / "gen_dpe_state_game.yaml"
    original = source.read_bytes()
    target.write_bytes(original)

    reports = migrate_generated_release_gates(config_dir)
    assert len(reports) == 1
    assert Path(reports[0]["backup"]).read_bytes() == original
    _assert_game_release_contract(yaml.safe_load(target.read_text()))
    assert migrate_generated_release_gates(config_dir) == []


def _resolver(document):
    from skillflow.graph import GraphResolver, PipelineGraph
    return GraphResolver(PipelineGraph._from_dict(document))


@pytest.mark.parametrize(("values", "state"), [
    ({"passed": False, "pending": True}, "pending"),
    ({"passed": True, "skipped": True}, "skipped"),
    ({"passed": False, "infrastructure_unavailable": True},
     "infrastructure_unavailable"),
])
def test_unresolved_test_evidence_writes_distinct_marker_and_holds(
        tmp_path, monkeypatch, values, state):
    _cycle(tmp_path)
    _report(tmp_path, "5_test", **values)
    out = tmp_path / "5_compile"
    monkeypatch.setattr(compile_impl, "_godot_compile_unstamped",
                        lambda **_: pytest.fail("compile must not run"))
    result = compile_impl.godot_compile(
        project_root=str(tmp_path), out_dir=str(out), run_id=RUN,
        evidence_cycle_from="5_test", fail_fast_gates=GATES)
    marker = json.loads((out / "compile_report.json").read_text())
    assert result["release_evidence"] == "unresolved"
    assert marker["skipped_because"] == "upstream_unresolved"
    assert marker["upstream_state"] == state
    assert marker["evidence_cycle_id"] == CYCLE
    assert _resolver(_canonical_game()).next_node(
        "5_compile", result, {}) == "5_release_wait"


@pytest.mark.parametrize("setup", ["missing", "stale", "unreadable"])
def test_absent_or_invalid_test_evidence_holds_without_replan(
        tmp_path, monkeypatch, setup):
    _cycle(tmp_path)
    if setup == "stale":
        _report(tmp_path, "5_test", evidence_cycle_id="old")
    elif setup == "unreadable":
        path = tmp_path / "5_test" / "test_report.json"
        path.parent.mkdir()
        path.write_text("{bad json")
    out = tmp_path / "5_compile"
    monkeypatch.setattr(compile_impl, "_godot_compile_unstamped",
                        lambda **_: pytest.fail("compile must not run"))
    result = compile_impl.godot_compile(
        project_root=str(tmp_path), out_dir=str(out), run_id=RUN,
        evidence_cycle_from="5_test", fail_fast_gates=GATES)
    assert result["release_evidence"] == "unresolved"
    assert result["upstream_state"] == setup
    resolver = _resolver(_canonical_game())
    assert resolver.next_node("5_compile", result, {}) == "5_release_wait"
    assert resolver.resolve_transition(
        "5_release_wait", {}, {}, checkpoint_approved=True)[1] == "5_test"


def test_known_failure_marker_chain_reaches_replan_and_stays_fresh(
        tmp_path, monkeypatch):
    _cycle(tmp_path)
    _report(tmp_path, "5_test", passed=False, passed_relative=True)
    monkeypatch.setattr(compile_impl, "_godot_compile_unstamped",
                        lambda **_: pytest.fail("compile must not run"))
    compile_result = compile_impl.godot_compile(
        project_root=str(tmp_path), out_dir=str(tmp_path / "5_compile"),
        run_id=RUN, evidence_cycle_from="5_test", fail_fast_gates=GATES)
    resolver = _resolver(_canonical_game())
    assert compile_result["release_evidence"] == "known_failure"
    assert resolver.next_node("5_compile", compile_result, {}) == "5_vision"

    monkeypatch.setattr(vision_impl, "_godot_vision_unstamped",
                        lambda **_: pytest.fail("vision must not run"))
    vision_result = vision_impl.godot_vision(
        project_root=str(tmp_path), workspace_root=str(tmp_path.parent),
        config_name=tmp_path.name, out_dir=str(tmp_path / "5_vision"),
        run_id=RUN, evidence_cycle_from="5_test",
        fail_fast_gates=json.dumps([
            ["5_compile", "compile_report.json"],
            ["5_compile", "playtest_report.json"],
        ]))
    assert vision_result["release_evidence"] == "known_failure"
    assert resolver.next_node("5_vision", vision_result, {}) == "5_final_test"

    monkeypatch.setattr(tests_impl, "_resolve_pytest_python",
                        lambda *_: pytest.fail("pytest must not run"))
    final_result = tests_impl.run_tests(
        project_root=str(tmp_path), out_dir=str(tmp_path / "5_final_test"),
        run_id=RUN, evidence_cycle_from="5_test", repo_gate=False,
        fail_fast_gates=json.dumps([
            ["5_test", "test_report.json"],
            ["5_compile", "compile_report.json"],
            ["5_compile", "playtest_report.json"],
            ["5_vision", "vision_report.json"],
        ]))
    assert final_result["release_evidence"] == "known_failure"
    assert resolver.next_node("5_final_test", final_result, {}) == \
        "5_final_test_replan"
    for step, filename in (
        ("5_compile", "compile_report.json"),
        ("5_compile", "playtest_report.json"),
        ("5_vision", "vision_report.json"),
        ("5_final_test", "test_report.json"),
    ):
        report = json.loads((tmp_path / step / filename).read_text())
        assert report["skipped_because"] == "upstream_failed"
        assert report["evidence_cycle_id"] == CYCLE


@pytest.mark.parametrize("state", [
    "pending", "missing", "stale", "unreadable",
    "infrastructure_unavailable", "skipped",
])
def test_both_canonical_and_generated_graphs_hold_every_unresolved_class(state):
    generated = yaml.safe_load((
        ROOT / "evidence/output-target-migration-20260911/generated-configs/"
        "gen_dpe_state_game.yaml").read_text())
    assert migrate_release_document(generated)
    for document in (_canonical_game(), generated):
        resolver = _resolver(document)
        flags = {"passed": False, "release_evidence": "unresolved",
                 "evidence_state": state}
        assert resolver.next_node("5_compile", flags, {}) == "5_release_wait"
        assert resolver.next_node("5_vision", flags, {}) == "5_release_wait"
        assert resolver.next_node("5_final_test", flags, {}) == "5_release_wait"
        assert "5_final_test_replan" not in {
            resolver.next_node("5_compile", flags, {}),
            resolver.next_node("5_vision", flags, {}),
            resolver.next_node("5_final_test", flags, {}),
        }


def _bad_graph(mutator):
    document = yaml.safe_load((
        ROOT / "evidence/output-target-migration-20260911/generated-configs/"
        "gen_dpe_state_game.yaml").read_text())
    mutator({step["id"]: step for step in document["steps"]}, document)
    before = copy.deepcopy(document)
    assert migrate_release_document(document) == []
    assert document == before
    return document


@pytest.mark.parametrize("mutator", [
    lambda steps, _doc: steps["5_test"].update(transitions=[{"to": "5_review"}]),
    lambda steps, _doc: steps["5_compile"].update(transitions=[{"to": "5_review"}]),
    lambda steps, _doc: steps["5_vision"].update(transitions=[{"to": "5_review"}]),
    lambda steps, _doc: steps["5_vision"].update(
        transitions=list(reversed(steps["5_vision"]["transitions"]))),
    lambda steps, _doc: steps["5_vision_human"].update(
        transitions=[{"to": "5_knowledge"}]),
    lambda steps, _doc: steps["5_vision_judged"].update(
        transitions=[{"to": "5_review"}]),
    lambda steps, _doc: steps["5_knowledge"].update(
        transitions=[{"to": "5_review"}]),
    lambda steps, _doc: steps["5_design"].update(transitions=[{"to": "5_review"}]),
    lambda steps, _doc: steps["5_final_test"].update(transitions=[{"to": "5_review"}]),
    lambda steps, _doc: steps["5_final_test"].update(
        transitions=list(reversed(steps["5_final_test"]["transitions"]))),
    lambda steps, _doc: steps["5_final_test_replan"].update(
        transitions=[{"to": "5_review"}]),
    lambda steps, _doc: steps["5_compile"].update(tool_params=None),
    lambda steps, _doc: steps["5_compile"].update(transitions=None),
    lambda steps, _doc: steps["5_compile"].update(step_type="gate"),
    lambda steps, _doc: steps["5_design"].update(
        validation=[{"tool": "run_tests"}]),
    lambda steps, _doc: steps["5_design"].setdefault("config", {}).update(
        extra_tools=["run_tests"]),
    lambda steps, _doc: steps["5_design"].setdefault("config", {}).update(
        extra_tools=["godot_playtest"]),
    lambda _steps, doc: doc["steps"].append({
        "id": "extra_compile", "step_type": "tool",
        "tool_name": "godot_compile", "transitions": [{"to": "5_review"}]}),
])
def test_migration_rejects_adversarial_shape_without_partial_mutation(mutator):
    _bad_graph(mutator)


@pytest.mark.parametrize(("surface", "grants"), [
    pytest.param("config", ["run_tests"], id="config-yaml-list"),
    pytest.param("config", {"run_tests": {"nested": True}},
                 id="config-mapping-keys"),
    pytest.param("role", ["run_tests"], id="paired-role-json-array"),
    pytest.param("role", {"run_tests": {"nested": True}},
                 id="paired-role-mapping-keys"),
])
def test_skillflow_executes_every_persisted_tool_grant_shape(
        tmp_path, surface, grants):
    import skillflow
    from skillflow import PipelineGraph, SkillFlow
    from skillflow.graph import StepNode
    from skillflow.tool_loader import ToolLoader

    loader = ToolLoader(Path(skillflow.__file__).parent / "tools",
                        ROOT / "aitelier" / "tools")
    sf = SkillFlow(str(tmp_path / "state.db"), tool_loader=loader,
                   workspace_base=str(tmp_path / "workspace"))
    node = StepNode(id="design", config={"extra_tools": grants}) \
        if surface == "config" else StepNode(id="design", agent_config="paired")
    if surface == "role":
        sf.register_agent_config_from_dict("paired", {"tools": grants})
    sf.register_graph(PipelineGraph(
        name="tool_grant_probe", begin="design", steps=[node]))
    run_id = sf.create_run("tool_grant_probe", project_id="p")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    claim = sf.claim_next_step(run_id)
    assert "run_tests" in claim.inputs["_tool_schemas"]
    calls = []
    loader.register_dynamic_tool(
        "run_tests", claim.inputs["_tool_schemas"]["run_tests"],
        lambda **kwargs: calls.append(kwargs) or {"passed": True})

    result = sf.execute_tool(
        "run_tests", {}, run_id=run_id, step_id=claim.step_id,
        step_instance_id=claim.token.step_instance_id,
        claim_epoch=claim.token.claim_epoch)

    assert result["passed"] is True
    assert len(calls) == 1


@pytest.mark.parametrize(("surface", "grants"), [
    ("config", {"run_tests": {"nested": True}}),
    ("role", {"run_tests": {"nested": True}}),
    ("config", "run_tests"),
    ("role", "run_tests"),
    ("config", [{"run_tests": {}}]),
    ("role", [{"run_tests": {}}]),
    ("config", None),
    ("role", None),
])
def test_migration_fails_closed_for_unsupported_tool_grant_containers(
        surface, grants):
    document = yaml.safe_load((
        ROOT / "evidence/output-target-migration-20260911/generated-configs/"
        "gen_dpe_state_game.yaml").read_text())
    role = next(step["agent_config"] for step in document["steps"]
                if step["id"] == "5_design")
    roles = None
    if surface == "config":
        next(step for step in document["steps"]
             if step["id"] == "5_design").setdefault("config", {})[
                 "extra_tools"] = grants
    else:
        roles = {role: {"tools": grants}}
    before = copy.deepcopy(document)

    assert "must be a list of non-empty strings" in release_gate_ownership_error(
        document, roles)
    assert migrate_release_document(document, roles=roles) == []
    assert document == before


def test_file_migration_contains_malformed_graph_and_continues(tmp_path):
    source = (ROOT / "evidence/output-target-migration-20260911/generated-configs/"
              "gen_dpe_state_game.yaml")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    malformed = config_dir / "gen_a_bad.yaml"
    malformed.write_text("steps:\n  - id: 5_test\n    tool_params:\n")
    bad_bytes = malformed.read_bytes()
    null_params = config_dir / "gen_b_null.yaml"
    null_document = yaml.safe_load(source.read_text())
    next(step for step in null_document["steps"]
         if step["id"] == "5_compile")["tool_params"] = None
    null_params.write_text(yaml.safe_dump(null_document, sort_keys=False))
    null_bytes = null_params.read_bytes()
    wrong_type = config_dir / "gen_c_wrong_type.yaml"
    wrong_type_document = yaml.safe_load(source.read_text())
    next(step for step in wrong_type_document["steps"]
         if step["id"] == "5_compile")["step_type"] = "gate"
    wrong_type.write_text(yaml.safe_dump(wrong_type_document, sort_keys=False))
    wrong_type_bytes = wrong_type.read_bytes()
    duplicate_gate = config_dir / "gen_d_duplicate_gate.yaml"
    duplicate_gate_document = yaml.safe_load(source.read_text())
    next(step for step in duplicate_gate_document["steps"]
         if step["id"] == "5_design")["validation"] = [{"tool": "run_tests"}]
    duplicate_gate.write_text(
        yaml.safe_dump(duplicate_gate_document, sort_keys=False))
    duplicate_gate_bytes = duplicate_gate.read_bytes()
    extra_gate = config_dir / "gen_e_extra_gate.yaml"
    extra_gate_document = yaml.safe_load(source.read_text())
    next(step for step in extra_gate_document["steps"]
         if step["id"] == "5_design").setdefault("config", {})[
             "extra_tools"] = ["run_tests"]
    extra_gate.write_text(yaml.safe_dump(extra_gate_document, sort_keys=False))
    extra_gate_bytes = extra_gate.read_bytes()
    role_gate = config_dir / "gen_f_role_gate.yaml"
    role_gate.write_bytes(source.read_bytes())
    role_gate_bytes = role_gate.read_bytes()
    role_name = next(step["agent_config"] for step in extra_gate_document["steps"]
                     if step["id"] == "5_design")
    role_file = role_gate.with_suffix(".roles.json")
    role_file.write_text(json.dumps({role_name: {"tools": ["run_tests"]}}))
    role_bytes = role_file.read_bytes()
    mapping_gate = config_dir / "gen_g_mapping_gate.yaml"
    mapping_document = yaml.safe_load(source.read_text())
    next(step for step in mapping_document["steps"]
         if step["id"] == "5_design").setdefault("config", {})[
             "extra_tools"] = {"run_tests": {"nested": True}}
    mapping_gate.write_text(yaml.safe_dump(mapping_document, sort_keys=False))
    mapping_bytes = mapping_gate.read_bytes()
    mapping_role_gate = config_dir / "gen_h_mapping_role_gate.yaml"
    mapping_role_gate.write_bytes(source.read_bytes())
    mapping_role_bytes = mapping_role_gate.read_bytes()
    mapping_role_file = mapping_role_gate.with_suffix(".roles.json")
    mapping_role_file.write_text(json.dumps({
        role_name: {"tools": {"run_tests": {"nested": True}}}}))
    mapping_role_file_bytes = mapping_role_file.read_bytes()
    malformed_grant = config_dir / "gen_i_malformed_grant.yaml"
    malformed_document = yaml.safe_load(source.read_text())
    next(step for step in malformed_document["steps"]
         if step["id"] == "5_design").setdefault("config", {})[
             "extra_tools"] = "run_tests"
    malformed_grant.write_text(yaml.safe_dump(malformed_document, sort_keys=False))
    malformed_bytes = malformed_grant.read_bytes()
    good = config_dir / "gen_z_good.yaml"
    good.write_bytes(source.read_bytes())
    reports = migrate_generated_release_gates(config_dir)
    assert malformed.read_bytes() == bad_bytes
    assert null_params.read_bytes() == null_bytes
    assert wrong_type.read_bytes() == wrong_type_bytes
    assert duplicate_gate.read_bytes() == duplicate_gate_bytes
    assert extra_gate.read_bytes() == extra_gate_bytes
    assert role_gate.read_bytes() == role_gate_bytes
    assert role_file.read_bytes() == role_bytes
    assert mapping_gate.read_bytes() == mapping_bytes
    assert mapping_role_gate.read_bytes() == mapping_role_bytes
    assert mapping_role_file.read_bytes() == mapping_role_file_bytes
    assert malformed_grant.read_bytes() == malformed_bytes
    assert [Path(report["path"]).name for report in reports] == ["gen_z_good.yaml"]
    _assert_game_release_contract(yaml.safe_load(good.read_text()))
