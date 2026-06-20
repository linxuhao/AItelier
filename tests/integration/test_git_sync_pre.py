"""Functional tests for git_sync_pre in the AItelier DPE pipeline.

Verifies:
  - Tool is registered and callable via AItelier's ToolLoader
  - Step exists as first step in dpe_default_v2 graph
  - Sync passes (up-to-date) and transitions → Researcher
  - Sync fails (diverged) and pipeline fails with actionable message
  - Error message flows through the run's error_reason
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager


# ── Helpers ──────────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _init_git(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)


def _get_tool_loader() -> ToolLoader:
    import skillflow
    native = Path(skillflow.__file__).parent / "tools"
    custom = Path(__file__).parent.parent.parent / "aitelier" / "tools"
    loader = ToolLoader(native)
    if custom.exists():
        loader.add_tools_dir(custom)
    return loader


def _load_dpe_config() -> dict:
    config_path = Path(__file__).parent.parent.parent / "configs" / "dpe_default.yaml"
    return yaml.safe_load(config_path.read_text())


# ── Test: tool is registered ─────────────────────────────────────────────────


def test_git_sync_pre_tool_registered():
    """git_sync_pre is loadable via AItelier's ToolLoader."""
    loader = _get_tool_loader()
    # Load schema FIRST (before load_fn, which caches an empty schema)
    schema = loader.load_schema("git_sync_pre")
    assert schema["name"] == "git_sync_pre"
    assert "project_root" in schema["parameters"]

    fn = loader.load_fn("git_sync_pre")
    assert callable(fn)


# ── Test: step exists in DPE graph ───────────────────────────────────────────


def test_git_sync_pre_is_first_step_in_dpe_config():
    """git_sync_pre is the first step in dpe_default_v2."""
    config = _load_dpe_config()
    assert config["name"] == "dpe_default_v2"
    assert config["begin"] == "git_sync_pre"

    steps = config["steps"]
    first = steps[0]
    assert first["id"] == "git_sync_pre"
    assert first["step_type"] == "tool"
    assert first["tool_name"] == "git_sync_pre"

    # Must transition to Research (1) on success
    to_1 = [t for t in first["transitions"] if t.get("to") == "1"]
    assert len(to_1) == 1
    assert to_1[0]["match"] == {"synced": True}


# ── Test: sync up-to-date → pipeline continues ───────────────────────────────


def test_git_sync_pre_up_to_date_advances_to_researcher(tmp_path):
    """When sync is up-to-date, pipeline transitions past git_sync_pre."""
    # Create git repo in the expected project_root path
    projects_base = tmp_path / "projects"
    proj_root = projects_base / "syncproj"
    proj_root.mkdir(parents=True)
    _init_git(proj_root)
    (proj_root / "f.txt").write_text("hi")
    _git(proj_root, "add", "-A")
    _git(proj_root, "commit", "-qm", "init")
    _git(proj_root, "remote", "add", "origin", ".")

    ws_base = tmp_path / "ws"

    sf = SkillFlow(":memory:")
    sf._tool_loader = _get_tool_loader()
    sf._workspace = WorkspaceManager(
        str(ws_base), projects_base=str(projects_base),
    )

    # Minimal graph: git_sync_pre → next_step
    sync_node = StepNode(
        id="git_sync_pre",
        step_type="tool",
        tool_name="git_sync_pre",
        tool_params={"project_root": str(proj_root)},
        transitions=[
            Transition(to="next_step", match={"synced": True}),
        ],
    )
    next_step = StepNode(
        id="next_step",
        step_type="tool",
        tool_name="notify",  # built-in, no agent_config needed
        transitions=[Transition(to=None)],
    )
    g = PipelineGraph(name="sync_test", begin="git_sync_pre",
                      steps=[sync_node, next_step])
    sf.register_graph(g)

    rid = sf.create_run("sync_test", {"project_id": "syncproj"})
    sf.start_run(rid)
    sf.advance_run(rid)

    run = sf._conn.execute(
        "SELECT current_node, status, error_reason FROM skillflow_runs WHERE id = ?",
        (rid,),
    ).fetchone()

    assert run["status"] in ("running", "completed"), f"Run failed: {run['error_reason']}"
    assert run["error_reason"] is None, f"Unexpected error: {run['error_reason']}"


# ── Test: sync diverged → pipeline fails with actionable message ─────────────


def test_git_sync_pre_diverged_pipeline_fails_with_message(tmp_path):
    """When remote diverged, pipeline fails and error_reason is actionable."""
    remote = tmp_path / "remote"
    _init_git(remote)
    (remote / "f.txt").write_text("remote")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "remote init")

    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(remote), str(local))

    # Create diverging commits
    (local / "f.txt").write_text("local")
    _git(local, "add", "-A")
    _git(local, "commit", "-qm", "local diverged")
    (remote / "f.txt").write_text("remote v2")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-qm", "remote diverged")

    ws_base = tmp_path / "ws"
    projects_base = tmp_path / "projects"

    sf = SkillFlow(":memory:")
    sf._tool_loader = _get_tool_loader()
    sf._workspace = WorkspaceManager(
        str(ws_base), projects_base=str(projects_base),
    )

    # Minimal graph: git_sync_pre → (success) next_step, (failure) stuck
    sync_node = StepNode(
        id="git_sync_pre",
        step_type="tool",
        tool_name="git_sync_pre",
        tool_params={"project_root": str(local)},
        transitions=[
            Transition(to="next_step", match={"synced": True}),
        ],
    )
    next_step = StepNode(
        id="next_step",
        step_type="tool",
        tool_name="notify",
        transitions=[Transition(to=None)],
    )
    g = PipelineGraph(name="sync_test", begin="git_sync_pre",
                      steps=[sync_node, next_step])
    sf.register_graph(g)

    rid = sf.create_run("sync_test", {"project_id": "syncproj"})
    sf.start_run(rid)
    sf.advance_run(rid)

    # Check step outputs — the tool should have failed with a clear error
    step = sf._conn.execute(
        "SELECT outputs_json FROM skillflow_steps "
        "WHERE run_id = ? AND step_id = 'git_sync_pre' "
        "ORDER BY id DESC LIMIT 1",
        (rid,),
    ).fetchone()
    assert step is not None, "git_sync_pre step was not executed"

    import json
    outputs = json.loads(step["outputs_json"])
    assert outputs.get("synced") is False, f"Expected synced=False, got: {outputs}"
    assert "error" in outputs, f"Expected error in outputs: {outputs}"
    assert "diverged" in outputs["error"].lower(), \
        f"Expected 'diverged' in error: {outputs['error']}"
    assert "git pull --rebase" in outputs["error"], \
        f"Expected actionable fix hint: {outputs['error']}"
