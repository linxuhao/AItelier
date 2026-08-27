"""'submitted' must mean the poller can actually pick the project up.

`seed_and_trigger` caches the brief, marks planning done, wakes the scheduler
and returns {"status": "submitted", "next_step": "1"}. It did not touch the
project's status — and a meta conversation leaves `running:<step>` there, which
matches none of the statuses `get_active_projects` selects on
('planning','executing','verifying','running').

Live 2026-08-27, jinyong-neigong: "submitted", then `outcome=idle` on every
tick, with a finished brief sitting on disk and no run ever created. The reply
said the build had begun; nothing was going to begin.
"""
from unittest.mock import MagicMock, patch

import pytest

from core.project_submit import seed_and_trigger


def _db(status, steps="[]"):
    db = MagicMock()
    db.get_project.return_value = {
        "project_id": "pid_x", "status": status,
        "completed_project_steps": steps, "brief": "b"}
    return db


def _submit(db, tmp_path):
    # The guard reads step1_goals.json off the workspace — give it a real one
    # rather than patching the guard away: a test that skips because the guard
    # refused proves nothing about the line under test.
    goals = tmp_path / "meta_conversation" / "finalize" / "step1_goals.json"
    goals.parent.mkdir(parents=True)
    goals.write_text('{"mvp_goals": ["g"]}', encoding="utf-8")
    sf = MagicMock()
    sf._workspace.get_project_path.return_value = tmp_path
    ws = MagicMock()
    with patch("api.dependencies.get_skillflow", return_value=sf), \
         patch("core.scheduler.wake_scheduler"), \
         patch("core.meta_conversation.format_brief_as_markdown", return_value="# b"):
        result = seed_and_trigger(db, ws, "pid_x", {"mvp_goals": ["g"]})
    assert result.get("status") == "submitted", result
    return result


def _status_writes(db):
    return [c.kwargs.get("status") for c in db.update_project.call_args_list
            if "status" in c.kwargs]


@pytest.mark.parametrize("leftover", ["running:intent_detect", "running:gather", ""])
def test_meta_leftovers_are_normalised_to_planning(leftover, tmp_path):
    db = _db(leftover)
    _submit(db, tmp_path)
    assert "planning" in _status_writes(db), (
        "a project left in a meta status is invisible to get_active_projects")


def test_a_real_pipeline_status_is_left_alone(tmp_path):
    # This function means "the brief is ready, go" — not "reset whatever the
    # pipeline was doing".
    db = _db("executing")
    _submit(db, tmp_path)
    assert "planning" not in _status_writes(db)


def test_an_already_planning_project_is_left_alone(tmp_path):
    db = _db("planning")
    _submit(db, tmp_path)
    assert _status_writes(db) == []
