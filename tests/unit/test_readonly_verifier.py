"""Final verification is a read-only interval over one resolved candidate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from core.output_migration import migrate_generated_outputs


ROOT = Path(__file__).resolve().parents[2]


def _graph(path: Path = ROOT / "configs" / "dpe_default.yaml") -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(graph: dict) -> dict[str, dict]:
    return {step["id"]: step for step in graph["steps"]}


def _targets(step: dict) -> list[str | None]:
    return [edge.get("to") for edge in step.get("transitions", [])]


def test_builtin_verifier_has_one_report_output_and_named_readme_owner():
    graph = _graph()
    steps = _steps(graph)

    assert graph["anchors"]["readme_owner"] == "5_readme"
    assert steps["5_readme"]["agent_config"] == "delivery_documenter"
    assert steps["5_readme"]["output"] == {
        "mode": "content",
        "fixed": {
            "readme": {
                "file": "README.md",
                "format": (
                    "Markdown project delivery guide: overview, installation, "
                    "operation, and public interfaces, reflecting the resolved candidate"
                ),
                "on_exists": "replace",
                "target": "code",
            }
        },
        "target": "artifact",
    }

    assert steps["5"]["output"] == {
        "mode": "content",
        "fixed": {
            "report": {
                "file": "final/verify_report.json",
                "format": steps["5"]["output"]["fixed"]["report"]["format"],
                "on_exists": "replace",
                "target": "artifact",
            }
        },
        "target": "artifact",
    }
    assert _targets(steps["5_evidence"]) == ["5_readme"]
    assert _targets(steps["5_readme"]) == ["5_candidate_before"]
    assert _targets(steps["5_candidate_before"]) == ["5"]
    assert _targets(steps["5"]) == ["5_candidate_after"]
    assert _targets(steps["5_candidate_after"]) == ["5_knowledge"]


def test_composed_game_finalizes_design_then_readme_before_verifier():
    from skillflow.compose import compose_graph

    graph = compose_graph(
        _graph(), [_graph(ROOT / "configs" / "addons" / "game_harness.yaml")]
    )
    steps = _steps(graph)
    assert _targets(steps["5_evidence"]) == ["5_design"]
    assert _targets(steps["5_game_evidence"]) == ["5_readme"]
    assert _targets(steps["5_readme"]) == ["5_candidate_before"]
    assert _targets(steps["5_candidate_before"]) == ["5"]
    assert _targets(steps["5"]) == ["5_candidate_after"]

    extra = steps["5_readme"].get("config", {}).get("extra_templates", [])
    assert "game_harness/readme_owner.md" in extra


def test_verifier_write_schema_is_report_only_in_native_and_json_modes():
    from skillflow import PipelineGraph
    from skillflow.write_tools import generate_write_tool_schemas

    graph = PipelineGraph.from_yaml(ROOT / "configs" / "dpe_default.yaml")
    verifier = next(step for step in graph.steps if step.id == "5")
    for native in (True, False):
        schemas = {
            schema["name"]: schema
            for schema in generate_write_tool_schemas(
                verifier.output_mode,
                verifier.output_fixed,
                output_target=verifier.output_target,
            )
        }
        assert {name for name in schemas if name != "finish_step"} == {
            "write_report", "create_report", "edit_report"
        }
        assert not ({
            "create_readme", "edit_readme", "write_readme",
            "create", "edit", "write", "apply_patch", "repo_remove_file",
        } & set(schemas)), native


@pytest.mark.parametrize("mode", ["native", "json"])
@pytest.mark.parametrize("tool,params", [
    ("write_readme", {"content": "mutated"}),
    ("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"}),
])
def test_verifier_mutation_attempt_is_refused_before_dispatch(
        mode, tool, params, monkeypatch):
    from unittest.mock import MagicMock, patch
    from core.dpe_pipeline import PipelineEngine

    schemas = {"write_report": {"name": "write_report", "x-write": True}}
    action = {"tool": tool, "params": params}
    with patch("core.agents.AgentFactory.__init__", return_value=None):
        engine = PipelineEngine()
    engine._tool_schemas = schemas
    if mode == "native":
        skillflow = MagicMock()
        monkeypatch.setattr("api.dependencies.get_skillflow", lambda: skillflow)
        result = engine._exec_tool(action)
        assert "not granted" in result["error"]
        skillflow.execute_tool.assert_not_called()
    else:
        writes = [candidate for candidate in [action]
                  if engine._is_mutation_tool(candidate["tool"], schemas)]
        _, _, _, unclaimed = engine._classify_actions([action], schemas, writes)
        assert unclaimed == [action]
        assert "no such tool" in engine._unclaimed_feedback(unclaimed, schemas)


def test_candidate_integrity_detects_code_and_readme_changes(tmp_path):
    from aitelier.tools.candidate_integrity.impl import candidate_integrity

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_bytes(b"# candidate\n")
    (repo / "src.py").write_bytes(b"VALUE = 1\n")
    graph_dir = tmp_path / "artifacts" / "dpe"
    graph_dir.mkdir(parents=True)
    before = graph_dir / "5_candidate_before"
    after = graph_dir / "5_candidate_after"

    snap = candidate_integrity(
        project_root=str(repo), out_dir=str(before), phase="snapshot"
    )
    assert snap["passed"] is True
    baseline = json.loads((before / "candidate_snapshot.json").read_text())
    assert baseline["readme"]["sha256"]

    (repo / "src.py").write_bytes(b"VALUE = 2\n")
    (repo / "README.md").write_bytes(b"# mutated\n")
    checked = candidate_integrity(
        project_root=str(repo), out_dir=str(after), phase="verify",
        baseline_step="5_candidate_before",
    )
    assert checked["passed"] is False
    assert "error" in checked
    report = json.loads((after / "candidate_integrity_report.json").read_text())
    assert report["before"]["candidate_sha256"] != report["after"]["candidate_sha256"]
    assert report["before"]["readme"]["sha256"] != report["after"]["readme"]["sha256"]
    assert report["changed_paths"] == ["README.md", "src.py"]


def test_candidate_integrity_rejects_unbound_roots_without_touching_cwd(tmp_path):
    from aitelier.tools.candidate_integrity.impl import candidate_integrity

    assert candidate_integrity(project_root=".", out_dir=str(tmp_path), phase="snapshot")["error"]
    assert candidate_integrity(project_root=str(tmp_path), out_dir=".", phase="snapshot")["error"]


def test_saved_generated_dpe_moves_readme_and_verifier_without_losing_custom_prompt(tmp_path):
    root = tmp_path / "configs"
    root.mkdir()
    graph = {
        "name": "gen_game",
        "begin": "task_loop",
        "steps": [
            {"id": "task_loop", "step_type": "loop", "loop": {
                "source": {"step": "3", "file": "tasks.json", "field": "items"},
                "item_as": "item", "max_iterations": 3},
             "transitions": [{"to": "work", "max_loop": 3}, {"to": "5"}]},
            {"id": "work", "step_type": "agent", "agent_config": "worker",
             "output": {"mode": "write", "target": "code"},
             "transitions": [{"to": "task_loop", "max_loop": 3}]},
            {"id": "5", "step_type": "agent", "agent_config": "gen_game__final_verifier",
             "context": [{"source": {"from": "repository", "mode": "tool"}}],
             "output": {"mode": "content", "target": "artifact", "fixed": {
                 "readme": {"file": "README.md", "target": "code"},
                 "report": {"file": "final/verify_report.json", "target": "artifact",
                            "format": '{"all_goals_met": bool}'}}},
             "transitions": [{"to": "5_test"}]},
            {"id": "5_test", "step_type": "tool", "tool_name": "run_tests",
             "transitions": [{"to": "5_knowledge"}]},
            {"id": "5_knowledge", "step_type": "tool", "tool_name": "knowledge_sync",
             "transitions": [{"to": "5_design"}]},
            {"id": "5_design", "step_type": "agent", "agent_config": "designer",
             "context": [{"source": {"step": "5", "optional": True}}],
             "output": {"mode": "write", "target": "code"},
             "transitions": [{"to": "5_final_test"}]},
            {"id": "5_final_test", "step_type": "tool", "tool_name": "run_tests",
             "transitions": [{"to": "5_review"}]},
            {"id": "5_review", "step_type": "agent", "agent_config": "reviewer",
             "output": {"mode": "content", "fixed": {"verdict": {
                 "file": "review.json", "format": '{"passed": bool}'}}},
             "transitions": [{"to": "done"}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }
    graph_path = root / "gen_game.yaml"
    graph_path.write_text(yaml.safe_dump(graph, sort_keys=False))
    custom_tail = "\n\n## Project-specific evidence\nKeep this exact sentence."
    role_path = root / "gen_game.roles.json"
    role_path.write_text(json.dumps({
        "gen_game__final_verifier": {
            "system_prompt": (
                "你负责验证裁定，并产出/更新项目交付文档 `README.md`。\n"
                "4. **产出文档**: 用 `create_readme` 写入项目交付文档 `README.md`\n"
                "details\n## 关键约束\nkeep constraints" + custom_tail
            ),
            "tools": ["list_tree", "apply_patch", "write_readme",
                      "custom_optional_writer"],
        },
        "worker": {"system_prompt": "worker"},
        "designer": {"system_prompt": "designer"},
        "reviewer": {"system_prompt": "reviewer"},
    }, ensure_ascii=False))

    reports = migrate_generated_outputs(root)
    assert reports
    changed = yaml.safe_load(graph_path.read_text())
    steps = _steps(changed)
    assert _targets(steps["task_loop"]) == ["work", "5_test"]
    assert _targets(steps["5_test"]) == ["5_design"]
    assert _targets(steps["5_final_test"]) == ["5_readme"]
    assert _targets(steps["5_readme"]) == ["5_candidate_before"]
    assert _targets(steps["5_candidate_before"]) == ["5"]
    assert _targets(steps["5"]) == ["5_candidate_after"]
    assert _targets(steps["5_candidate_after"]) == ["5_knowledge"]
    assert _targets(steps["5_knowledge"]) == ["5_review"]
    assert set(steps["5"]["output"]["fixed"]) == {"report"}
    assert steps["5_design"]["context"] == []

    roles = json.loads(role_path.read_text())
    prompt = roles["gen_game__final_verifier"]["system_prompt"]
    assert "report-only" in prompt
    assert "create_readme" not in prompt
    assert "产出/更新项目交付文档" not in prompt
    assert prompt.endswith(custom_tail)
    assert roles["gen_game__final_verifier"]["tools"] == ["list_tree"]

    graph_bytes, role_bytes = graph_path.read_bytes(), role_path.read_bytes()
    assert migrate_generated_outputs(root) == []
    assert graph_path.read_bytes() == graph_bytes
    assert role_path.read_bytes() == role_bytes


def test_forge_teaches_generated_verifiers_to_be_report_only():
    for relative in ("templates/forge_architect.md", "templates/forge_emit.md"):
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "final verifier" in text
        assert "report-only" in text
        assert "readme" in text and "earlier" in text
