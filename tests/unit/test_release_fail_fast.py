"""Release gates stop expensive work only after writing current-cycle evidence."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from aitelier.gate_evidence import audit_evidence, first_upstream_blocker
from aitelier.tools.godot_compile import impl as compile_impl
from aitelier.tools.godot_vision import impl as vision_impl
from aitelier.tools.run_tests import impl as tests_impl
from core.release_gate_migration import (
    migrate_generated_release_gates,
    migrate_release_document,
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
    assert [t["to"] for t in steps["5_compile"]["transitions"]] == ["5_vision"]
    assert any(t.get("to") == "5_final_test"
               and t.get("match", {}).get("field") == "passed"
               and t.get("match", {}).get("value") is False
               for t in steps["5_vision"]["transitions"])
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
