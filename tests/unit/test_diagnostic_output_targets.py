"""A real report tool called from a code agent must not dirty or deliver code."""
import json
from pathlib import Path

import skillflow
import yaml
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader
from skillflow.output_targets import git
from tests.code_output_fixture import init_code_repo


def test_run_tests_report_is_an_artifact_not_a_code_change(tmp_path):
    root = init_code_repo(tmp_path / "code")
    (root / "tests").mkdir()
    (root / "tests/test_smoke.py").write_text("def test_smoke():\n    assert 2 + 2 == 4\n")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.venv/\n")
    git(root, "add", "--", ".gitignore", "tests/test_smoke.py")
    git(root, "commit", "-qm", "baseline with actual test")
    loader = ToolLoader(Path(skillflow.__file__).parent / "tools")
    loader.add_tools_dir(Path(__file__).resolve().parents[2] / "aitelier/tools")
    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                   workspace_base=str(tmp_path / "artifacts"),
                   code_path_resolver=lambda pid, run_id=None: root)
    node = StepNode(id="implement", output_mode="write", output_target="code",
                    config={"extra_tools": ["run_tests"]},
                    context=[{"from": "repository", "mode": "tool"}],
                    transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name="g", begin=node.id, steps=[node]))
    rid = sf.create_run("g", project_id="p")
    sf.start_run(rid); sf.advance_run(rid); claim = sf.claim_next_step(rid)
    def invoke(name, **params):
        return sf.execute_tool(name, params, run_id=rid, step_id=node.id,
                               step_instance_id=claim.token.step_instance_id,
                               claim_epoch=claim.token.claim_epoch)
    result = invoke("run_tests", repo_gate=False)
    assert result.get("passed") is True, result
    assert result["artifact_written"] == "test_report.json"
    artifact = Path(claim.inputs["_artifact_dir"])
    report = json.loads((artifact / "test_report.json").read_text())
    assert report["passed"] and not report.get("skipped") and not report.get("no_tests_collected"), report
    assert not (root / "test_report.json").exists()
    assert "error" not in invoke("read", source="self", path="test_report.json")
    assert "error" in invoke("read", path="test_report.json")
    sf.confirm_step(claim.token, StepResult())
    assert sf.get_steps(rid)[0]["status"] == "completed", sf.get_steps(rid)
    receipt = json.loads((artifact / "code_changes.json").read_text())
    assert receipt["files"] == []
    assert git(root, "status", "--porcelain").strip() == ""


def test_report_tools_declare_their_output_destination():
    root = Path(__file__).resolve().parents[2] / "aitelier/tools"
    for name in ("run_tests", "godot_compile", "godot_playtest", "godot_vision"):
        schema = yaml.safe_load((root / name / "tool.yaml").read_text())
        assert schema["output"]["target"] == "artifact"
