"""Claim-time preconditions settle without touching retained code.

A code-output claim validates the run-owned worktree before an agent starts.  A
fresh code step over retained dirty files raises from claim_next_step and rolls
its claim transaction back, so the same pending row is presented on every tick.
These tests keep that boundary distinct from an agent failure.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import skillflow
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.output_targets import git
from skillflow.tool_loader import ToolLoader

from core import scheduler


def _dirty_code_run(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    git(root, "init", "-q")
    (root / "base.py").write_text("baseline = True\n")
    git(root, "add", "--", "base.py")
    git(root, "commit", "-qm", "base")
    retained = root / "unknown-draft.py"
    retained.write_bytes(b"owner work\x00must survive")

    sf = SkillFlow(
        str(tmp_path / "sf.db"),
        tool_loader=ToolLoader(Path(skillflow.__file__).parent / "tools"),
        workspace_base=str(tmp_path / "artifacts"),
        code_path_resolver=lambda project_id, run_id=None: root,
    )
    node = StepNode(
        id="implement",
        output_mode="write",
        output_target="code",
        output_allow_full_write=True,
        transitions=[Transition(to=None)],
    )
    sf.register_graph(PipelineGraph(name="dirty_claim", begin=node.id, steps=[node]))
    run_id = sf.create_run("dirty_claim", project_id="dirty-project")
    sf.start_run(run_id)
    return sf, run_id, root, retained


def _wire_tick(monkeypatch, sf, run_id):
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run", lambda pid: run_id)
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *args: False)
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda pid: None)
    monkeypatch.setattr(scheduler, "_claim_retry_after", {})


async def test_dirty_claim_failure_is_bounded_actionable_and_lossless(tmp_path, monkeypatch):
    sf, run_id, root, retained = _dirty_code_run(tmp_path)
    _wire_tick(monkeypatch, sf, run_id)

    for _ in range(scheduler._MAX_IDENTICAL_CLAIM_PRECONDITIONS - 1):
        await scheduler._run_skillflow_tick("dirty-project", None)
        assert sf.get_run(run_id)["status"] == "running"

    await scheduler._run_skillflow_tick("dirty-project", None)

    run = sf.get_run(run_id)
    assert run["status"] == "failed"
    reason = run["error_reason"]
    assert "owning step implement" in reason
    assert f"run_id={run_id}" in reason
    assert "project_id=dirty-project" in reason
    assert "step_instance_id=1" in reason
    assert "Resume/recover that owning step" in reason
    assert "pending changes were retained" in reason
    assert retained.read_bytes() == b"owner work\x00must survive"
    assert git(root, "status", "--porcelain", "--untracked-files=all").strip() == "?? unknown-draft.py"
    assert git(root, "rev-list", "--count", "HEAD").strip() == "1"

    rows = sf.trace_query(run_id,
        "SELECT category,event,step_id,step_instance_id,payload_json FROM skillflow_trace "
        "WHERE run_id=? ORDER BY seq", (run_id,))
    preconditions = [r for r in rows if r["event"] == "claim_precondition_failed"]
    assert len(preconditions) == scheduler._MAX_IDENTICAL_CLAIM_PRECONDITIONS
    assert all(r["category"] == "scheduler" and r["step_id"] == "implement"
               for r in preconditions)
    assert all(r["step_instance_id"] == 1 for r in preconditions)
    assert not any(r["event"] in {"agent_response", "agent_error", "step_failed"}
                   for r in rows)


async def test_identical_count_survives_process_memory_reset(tmp_path, monkeypatch):
    sf, run_id, _, _ = _dirty_code_run(tmp_path)
    _wire_tick(monkeypatch, sf, run_id)

    await scheduler._run_skillflow_tick("dirty-project", None)
    scheduler._claim_retry_after.clear()  # simulate losing all process-local policy state
    await scheduler._run_skillflow_tick("dirty-project", None)
    scheduler._claim_retry_after.clear()
    await scheduler._run_skillflow_tick("dirty-project", None)

    assert sf.get_run(run_id)["status"] == "failed"


async def test_transient_claim_contention_backs_off_without_consuming_terminal_budget(monkeypatch):
    sf = MagicMock()
    sf.trace_query.return_value = [[0]]
    sf._get_resolver_for_run.return_value.is_tool.return_value = False
    sf.get_run.return_value = {"status": "running", "current_node": "implement",
                               "project_id": "p1"}
    sf.claim_next_step.side_effect = RuntimeError("database is locked")
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run", lambda pid: "run1")
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *args: False)
    monkeypatch.setattr(scheduler, "_advance_recording_crashes", lambda *args: "implement")
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda pid: None)
    monkeypatch.setattr(scheduler, "_claim_retry_after", {})

    await scheduler._run_skillflow_tick("p1", None)
    await scheduler._run_skillflow_tick("p1", None)

    sf.claim_next_step.assert_called_once_with("run1")
    sf.fail_run.assert_not_called()
    assert scheduler._claim_retry_after[(id(sf), "run1")] > scheduler._time.monotonic()


async def test_terminal_settlement_releases_single_project_scheduler_slot(tmp_path, monkeypatch):
    sf, run_id, _, _ = _dirty_code_run(tmp_path)
    _wire_tick(monkeypatch, sf, run_id)
    projects = ["dirty-project", "next-project"]
    selected = []

    async def select_one_tick():
        active = "dirty-project" if sf.get_run(run_id)["status"] == "running" else "next-project"
        selected.append(active)
        if active == "dirty-project":
            await scheduler._run_skillflow_tick(active, None)

    for _ in range(scheduler._MAX_IDENTICAL_CLAIM_PRECONDITIONS + 1):
        await select_one_tick()

    assert selected[-1] == "next-project"
    assert selected.count("dirty-project") == scheduler._MAX_IDENTICAL_CLAIM_PRECONDITIONS
