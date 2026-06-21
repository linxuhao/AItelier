# tests/test_step_scheduler.py
# Tests for step-granular scheduling, DB progress tracking, and resume.

import json
import pytest
import tempfile
from pathlib import Path
from core.db_manager import DBManager
from models.schemas import TaskStatus
from core.workspace_manager import TASK_STEP_SEQUENCE, FINAL_STEP


@pytest.fixture
def db(tmp_path):
    return DBManager(str(tmp_path / "test.db"))


class TestDBStepTracking:
    def test_new_task_defaults(self, db):
        task_id = db.push_task("proj_a", "test prompt")
        progress = db.get_task_progress(task_id)
        assert progress["current_step"] == "t_plan"
        assert progress["completed_steps"] == []
        assert progress["current_subtask"] is None
        assert progress["priority"] == 0
        assert progress["step_locked"] is False

    def test_advance_step(self, db):
        task_id = db.push_task("proj_a", "test")
        db.update_task_status(task_id, TaskStatus.RUNNING)

        db.advance_step(task_id, next_step="1_5", completed_steps=["1"])
        progress = db.get_task_progress(task_id)
        assert progress["current_step"] == "1_5"
        assert progress["completed_steps"] == ["1"]

    def test_advance_to_null_means_done(self, db):
        task_id = db.push_task("proj_a", "test")
        db.update_task_status(task_id, TaskStatus.RUNNING)

        all_steps = ["1_5", "2", "3"]
        db.advance_step(task_id, next_step=None, completed_steps=all_steps)
        progress = db.get_task_progress(task_id)
        assert progress["current_step"] is None

    def test_advance_with_subtask(self, db):
        task_id = db.push_task("proj_a", "test")
        db.update_task_status(task_id, TaskStatus.RUNNING)

        db.advance_step(task_id, next_step="4", completed_steps=["1", "1_5", "2", "3"],
                        current_subtask="4_1")
        progress = db.get_task_progress(task_id)
        assert progress["current_step"] == "4"
        assert progress["current_subtask"] == "4_1"

    def test_acquire_step_lock(self, db):
        task_id = db.push_task("proj_a", "test")
        db.update_task_status(task_id, TaskStatus.RUNNING)

        # Should acquire lock
        task = db.acquire_step_lock()
        assert task is not None
        assert task["id"] == task_id

        # Second attempt should return None (locked)
        task2 = db.acquire_step_lock()
        assert task2 is None

    def test_release_step_lock(self, db):
        task_id = db.push_task("proj_a", "test")
        db.update_task_status(task_id, TaskStatus.RUNNING)

        db.acquire_step_lock()
        db.release_step_lock(task_id)

        # Should be able to acquire again
        task = db.acquire_step_lock()
        assert task is not None

    def test_priority_ordering(self, db):
        # Create tasks with different priorities
        low_id = db.push_task("proj_low", "low prio")
        with db.get_connection() as conn:
            conn.execute("UPDATE tasks SET priority = 10 WHERE id = ?", (low_id,))
            conn.commit()

        high_id = db.push_task("proj_high", "high prio")
        with db.get_connection() as conn:
            conn.execute("UPDATE tasks SET priority = 50 WHERE id = ?", (high_id,))
            conn.commit()

        db.update_task_status(low_id, TaskStatus.RUNNING)
        db.update_task_status(high_id, TaskStatus.RUNNING)

        # Should pick highest priority first
        task = db.acquire_step_lock()
        assert task["id"] == high_id

    def test_has_running_tasks(self, db):
        assert db.has_running_tasks() is False

        task_id = db.push_task("proj_a", "test")
        assert db.has_running_tasks() is False  # pending

        db.update_task_status(task_id, TaskStatus.RUNNING)
        assert db.has_running_tasks() is True

    def test_get_next_pending_task_priority(self, db):
        id1 = db.push_task("proj_a", "low")
        id2 = db.push_task("proj_b", "high")

        # Set priority
        with db.get_connection() as conn:
            conn.execute("UPDATE tasks SET priority = 100 WHERE id = ?", (id2,))
            conn.commit()

        task = db.get_next_pending_task_priority()
        assert task["id"] == id2  # higher priority first

    def test_idempotent_migration(self, tmp_path):
        """Creating DB twice should not fail on ALTER TABLE."""
        db1 = DBManager(str(tmp_path / "test1.db"))
        db1.push_task("p", "t")
        # Re-init should be safe
        db2 = DBManager(str(tmp_path / "test1.db"))
        tasks = db2.list_tasks()
        assert len(tasks) == 1


class TestStepSequences:
    """Test that step sequences defined in workspace_manager are consistent."""

    def test_task_step_sequence_order(self):
        """Task steps should follow the correct order."""
        assert TASK_STEP_SEQUENCE == ["t_plan", "t_impl"]
        assert FINAL_STEP == "5"
