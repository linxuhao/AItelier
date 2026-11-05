"""Functional tests for git_sync_pre in the AItelier DPE pipeline.

Verifies:
  - Tool is registered and callable via AItelier's ToolLoader
  - Step exists as first step in dpe_default_v2 graph
  - Sync passes (up-to-date) and transitions → Researcher
  - A `policy: "pull"` sync is REFUSED by the installed engine's closed-Git
    policy, and the pipeline fails carrying that refusal
  - Error message flows through the run's error_reason
  - The refused sync leaves the checkout untouched (nothing fetched, nothing
    pulled)
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
    """A diverged checkout still stops the pipeline with the engine's own words.

    The name is kept because the node's acceptance refers to it, but the path it
    used to pin no longer exists and this docstring says so rather than implying
    otherwise. It used to assert that the failure message contained "diverged"
    and the hint "git pull --rebase": `policy: "pull"` really did fetch, and a
    diverged branch made the tool report the divergence to the pipeline.

    The installed engine's closed-Git policy now refuses that policy outright —
    `git fetch`/`git pull` would run repository-configured transport, credential,
    hook and checkout-filter programs inside the engine process — so the tool
    reports `action: "unavailable"` and NOTHING is fetched. There is no supported
    path left that can produce the diverged message: it is not a product
    regression in this repository either (the tool ships in skillflow, not here),
    it is a test that outlived the behaviour it pinned. Re-pinning the dead
    string, or weakening this to "the run failed somehow", would both be lies.

    What it now proves, through a REAL run of the installed engine and tool (no
    mocks), in exactly the situation that used to end in a diverged verdict:

      * the step is recorded `failed`, and its `last_error` — read from the
        run's own `skillflow_steps` row, not a mocked status — carries the
        engine's refusal;
      * the run says the same thing, so a caller reading only the run learns
        what to do about it (sync the checkout outside the engine) instead of
        learning that a step failed for no stated reason;
      * the local checkout is untouched: `f.txt` still holds the local content,
        no `FETCH_HEAD` was written, and the remote-tracking ref still points at
        the clone-time commit.

    That last witness is only worth something because the remote was moved
    AHEAD of it first: a real fetch would have moved `origin/<branch>` to the new
    remote commit, and a real pull would have rewritten `f.txt`. The divergence
    in the setup is what makes "nothing happened" measurable.
    """
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

    # The witnesses for "nothing was fetched, nothing was pulled", taken before
    # the run: the remote is now ahead of the clone's remote-tracking ref, so a
    # fetch would move it, and `f.txt` diverges, so a pull would rewrite it.
    branch = _git(local, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    tracking_ref = f"refs/remotes/origin/{branch}"
    tracking_before = _git(local, "rev-parse", tracking_ref).stdout.strip()
    remote_head = _git(remote, "rev-parse", "HEAD").stdout.strip()
    assert tracking_before, "could not read the clone's remote-tracking ref"
    assert remote_head and remote_head != tracking_before, (
        "the remote did not move past the clone — the no-fetch witness below "
        "would be blind, since a fetch could not leave a trace")

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
        # `policy: "pull"` is still this test's subject: it is the opt-in that
        # used to fetch, and it is the opt-in the engine now refuses. Without it
        # the tool skips silently and there would be no failure to read.
        tool_params={"project_root": str(local), "policy": "pull"},
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

    # The step FAILED, and the engine's refusal is its failure — not its output.
    # skillflow >=1.5.54 fails a tool step whose result carries a truthy `error`
    # (unless the node declares `tool_error: "route"`), so the run stops here
    # carrying the tool's own words rather than dying a step later on "No
    # matching transition" — a message about the graph, for a problem in the
    # repository.
    step = sf._conn.execute(
        "SELECT status, last_error FROM skillflow_steps "
        "WHERE run_id = ? AND step_id = 'git_sync_pre' "
        "ORDER BY id DESC LIMIT 1",
        (rid,),
    ).fetchone()
    assert step is not None, "git_sync_pre step was not executed"
    assert step["status"] == "failed", dict(step)

    message = step["last_error"] or ""
    assert 'policy "pull" is unavailable in the engine' in message, \
        f"Expected the engine's refusal of the pull policy in: {message}"
    assert "nothing was fetched or changed" in message.lower(), \
        f"Expected the refusal to say it made no change: {message}"

    # …and the run says the same thing, so a caller reading only the run learns
    # what to do: sync this checkout outside the engine.
    run = sf._conn.execute(
        "SELECT status, error_reason FROM skillflow_runs WHERE id = ?", (rid,),
    ).fetchone()
    assert run["status"] == "failed", dict(run)
    assert run["error_reason"] and step["last_error"] in run["error_reason"], \
        dict(run)
    assert "sync this checkout outside the engine" in run["error_reason"].lower(), \
        f"Expected the actionable instruction in the run: {run['error_reason']}"

    # The refusal is not just words: the checkout was left exactly as it was.
    assert (local / "f.txt").read_text() == "local", \
        "the local working tree was rewritten — a pull ran"
    assert _git(local, "rev-parse", tracking_ref).stdout.strip() == tracking_before, (
        "the remote-tracking ref moved — a fetch ran. The remote is at "
        f"{remote_head}, the clone's ref was {tracking_before}")
    assert not (local / ".git" / "FETCH_HEAD").exists(), \
        "git wrote FETCH_HEAD — a fetch ran"
    assert _git(local, "status", "--porcelain").stdout.strip() == "", \
        "the local checkout has uncommitted changes after the run"
