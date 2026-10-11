"""A real knowledge tool step must hand a clean linked worktree to code output."""

import json
import subprocess
from pathlib import Path

import pytest
import skillflow
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.output_targets import git
from skillflow.tool_loader import ToolLoader

from aitelier.tools.knowledge_sync.impl import knowledge_sync


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


@pytest.mark.parametrize("legacy", ["none", "generated", "unowned"])
def test_knowledge_sync_then_real_code_claim_commit_in_linked_worktree(
        tmp_path, legacy):
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
    legacy_head = None
    legacy_bytes = None
    if legacy == "generated":
        seeded = knowledge_sync(
            project_root=str(linked), project_id="legacy",
            out_dir=str(graph_root / "knowledge_sync"))
        assert seeded["written"] is True
        git(linked, "add", "-f", "--", ".aitelier/knowledge.md")
        git(linked, "commit", "-qm", "legacy tracked knowledge")
        legacy_head = git(linked, "rev-parse", "HEAD").strip()
    elif legacy == "unowned":
        path = linked / ".aitelier" / "knowledge.md"
        path.parent.mkdir()
        path.write_text("user-owned notes\n")
        git(linked, "add", "--", ".aitelier/knowledge.md")
        git(linked, "commit", "-qm", "user tracked knowledge")
        legacy_head = git(linked, "rev-parse", "HEAD").strip()
        legacy_bytes = path.read_bytes()

    rid = sf.create_run("knowledge_code", project_id="p")
    sf.start_run(rid)
    sf.advance_run(rid)  # executes knowledge_sync or fails it before implement
    if legacy == "unowned":
        assert sf.get_run(rid)["status"] == "failed"
        assert sf.claim_next_step(rid) is None
        assert git(linked, "rev-parse", "HEAD").strip() == legacy_head
        assert (linked / ".aitelier" / "knowledge.md").read_bytes() == legacy_bytes
        assert _run(linked, "status", "--short").stdout == ""
        trace = sf.get_trace(rid)
        detail = "\n".join(str(row) for row in trace)
        assert ".aitelier/knowledge.md" in detail and "unproven" in detail
        return
    claim = sf.claim_next_step(rid)
    assert claim is not None and claim.step_id == "implement"
    assert _run(linked, "check-ignore", ".aitelier/knowledge.md").returncode == 0
    assert _run(linked, "status", "--short").stdout == ""
    if legacy == "generated":
        assert git(linked, "rev-parse", "HEAD^").strip() == legacy_head
        assert git(linked, "diff-tree", "--no-commit-id", "--name-status", "-r", "HEAD") == \
            "D\t.aitelier/knowledge.md\n"

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
