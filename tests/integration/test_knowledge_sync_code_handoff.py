"""A real knowledge tool step must hand a clean linked worktree to code output."""

import json
import subprocess
from pathlib import Path

import skillflow
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.output_targets import git
from skillflow.tool_loader import ToolLoader


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def test_knowledge_sync_then_real_code_claim_commit_in_linked_worktree(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    git(primary, "init", "-q")
    git(primary, "config", "user.name", "Knowledge test")
    git(primary, "config", "user.email", "knowledge@test.invalid")
    (primary / "baseline.py").write_text("baseline = True\n")
    (primary / ".gitignore").write_text("user-owned/\n")
    git(primary, "add", "--", "baseline.py", ".gitignore")
    git(primary, "commit", "-qm", "baseline")
    linked = tmp_path / "linked"
    git(primary, "worktree", "add", "-q", "-b", "knowledge-code", str(linked))
    ignore_before = (linked / ".gitignore").read_bytes()

    loader = ToolLoader(Path(skillflow.__file__).parent / "tools")
    loader.add_tools_dir(Path(__file__).resolve().parents[2] / "aitelier" / "tools")
    sf = SkillFlow(
        str(tmp_path / "sf.db"),
        tool_loader=loader,
        workspace_base=str(tmp_path / "artifacts"),
        code_path_resolver=lambda project_id, run_id=None: linked,
    )
    knowledge = StepNode(
        id="knowledge_sync",
        step_type="tool",
        tool_name="knowledge_sync",
        tool_params={"out_dir": "$STEP_DIR"},
        transitions=[Transition(to="implement")],
    )
    implement = StepNode(
        id="implement",
        output_mode="write",
        output_target="code",
        output_allow_full_write=True,
        transitions=[Transition(to=None)],
    )
    sf.register_graph(PipelineGraph(name="knowledge_code", begin=knowledge.id,
                                    steps=[knowledge, implement]))
    graph_root = tmp_path / "artifacts" / "p" / "knowledge_code"
    (graph_root / "2").mkdir(parents=True)
    (graph_root / "2" / "step2_design.md").write_text(
        "# Design\n\n## Overview\n\nCarry only reviewed knowledge.\n")

    rid = sf.create_run("knowledge_code", project_id="p")
    sf.start_run(rid)
    sf.advance_run(rid)  # executes knowledge_sync and moves to implement
    claim = sf.claim_next_step(rid)
    assert claim is not None and claim.step_id == "implement"
    assert _run(linked, "check-ignore", ".aitelier/knowledge.md").returncode == 0
    assert _run(linked, "status", "--short").stdout == ""

    result = sf.execute_tool(
        "create", {"file": "feature.py", "content": "value = 42\n"},
        run_id=rid, step_id=claim.step_id,
        step_instance_id=claim.token.step_instance_id,
        claim_epoch=claim.token.claim_epoch,
    )
    assert not result.get("error"), result
    sf.confirm_step(claim.token, StepResult())

    receipt = json.loads((Path(claim.inputs["_artifact_dir"]) / "code_changes.json").read_text())
    assert receipt["files"] == ["feature.py"]
    assert ".aitelier" not in git(linked, "ls-tree", "-r", "--name-only", "HEAD")
    assert _run(linked, "status", "--short").stdout == ""
    assert (linked / ".gitignore").read_bytes() == ignore_before
