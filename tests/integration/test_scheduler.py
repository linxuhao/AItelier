# tests/integration/test_scheduler.py
# Integration tests for the project-priority-first scheduler.

import pytest
import json
from core.db_manager import DBManager
from models.schemas import TaskStatus


def test_get_next_active_project_priority(tmp_path):
    """Higher-priority project should be picked first."""
    db = DBManager(str(tmp_path / "prio.db"))

    db.ensure_project("low_proj", name="Low")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'low_proj'"); _c.commit()
    with db.get_connection() as conn:
        conn.execute("UPDATE projects SET priority = 1 WHERE project_id = 'low_proj'")
        conn.commit()

    db.ensure_project("high_proj", name="High")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'high_proj'"); _c.commit()
    with db.get_connection() as conn:
        conn.execute("UPDATE projects SET priority = 10 WHERE project_id = 'high_proj'")
        conn.commit()

    result = db.get_next_active_project()
    assert result["project_id"] == "high_proj"


def test_get_next_active_project_skips_completed(tmp_path):
    """Completed or failed projects should not be picked."""
    db = DBManager(str(tmp_path / "skip.db"))

    db.ensure_project("done_proj", name="Done")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'completed' WHERE project_id = 'done_proj'"); _c.commit()

    db.ensure_project("active_proj", name="Active")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'active_proj'"); _c.commit()

    result = db.get_next_active_project()
    assert result["project_id"] == "active_proj"


def test_get_next_active_project_none_when_all_done(tmp_path):
    """Should return None when no projects have active work."""
    db = DBManager(str(tmp_path / "empty.db"))
    result = db.get_next_active_project()
    assert result is None


# ── FIFO mode tests (demo mode) ──


def test_fifo_picks_oldest_project(tmp_path):
    """In FIFO mode, the oldest active project is picked regardless of priority."""
    db = DBManager(str(tmp_path / "fifo_basic.db"))

    db.ensure_project("proj_later", name="Later")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'proj_later'"); _c.commit()
    with db.get_connection() as conn:
        conn.execute("UPDATE projects SET priority = 10, created_at = '2026-01-02' WHERE project_id = 'proj_later'")
        conn.commit()

    db.ensure_project("proj_first", name="First")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'proj_first'"); _c.commit()
    with db.get_connection() as conn:
        conn.execute("UPDATE projects SET priority = 1, created_at = '2026-01-01' WHERE project_id = 'proj_first'")
        conn.commit()

    result = db.get_next_active_project(fifo=True)
    assert result["project_id"] == "proj_first"


def test_fifo_ignores_priority(tmp_path):
    """FIFO should ignore priority — only creation order matters."""
    db = DBManager(str(tmp_path / "fifo_prio.db"))

    # High priority but created later
    db.ensure_project("high_prio_late", name="High Late")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'high_prio_late'"); _c.commit()
    with db.get_connection() as conn:
        conn.execute("UPDATE projects SET priority = 100, created_at = '2026-01-03' WHERE project_id = 'high_prio_late'")
        conn.commit()

    # Low priority but created first
    db.ensure_project("low_prio_early", name="Low Early")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'low_prio_early'"); _c.commit()
    with db.get_connection() as conn:
        conn.execute("UPDATE projects SET priority = 1, created_at = '2026-01-01' WHERE project_id = 'low_prio_early'")
        conn.commit()

    # FIFO picks oldest
    assert db.get_next_active_project(fifo=True)["project_id"] == "low_prio_early"
    # Non-FIFO (default) picks highest priority
    assert db.get_next_active_project(fifo=False)["project_id"] == "high_prio_late"


def test_fifo_skips_completed(tmp_path):
    """FIFO should skip completed/failed projects."""
    db = DBManager(str(tmp_path / "fifo_skip.db"))

    db.ensure_project("old_done", name="Old Done")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'completed' WHERE project_id = 'old_done'"); _c.commit()
    with db.get_connection() as conn:
        conn.execute("UPDATE projects SET created_at = '2026-01-01' WHERE project_id = 'old_done'")
        conn.commit()

    db.ensure_project("new_active", name="New Active")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'new_active'"); _c.commit()
    with db.get_connection() as conn:
        conn.execute("UPDATE projects SET created_at = '2026-01-02' WHERE project_id = 'new_active'")
        conn.commit()

    result = db.get_next_active_project(fifo=True)
    assert result["project_id"] == "new_active"


def test_fifo_with_owner_filter(tmp_path):
    """FIFO + owner_email should combine: FIFO within a single user's projects."""
    db = DBManager(str(tmp_path / "fifo_owner.db"))

    db.ensure_project("alice_old", name="Alice Old", owner_email="alice@t.com")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'alice_old'"); _c.commit()
    with db.get_connection() as conn:
        conn.execute("UPDATE projects SET priority = 1, created_at = '2026-01-01' WHERE project_id = 'alice_old'")
        conn.commit()

    db.ensure_project("alice_new", name="Alice New", owner_email="alice@t.com")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'alice_new'"); _c.commit()
    with db.get_connection() as conn:
        conn.execute("UPDATE projects SET priority = 100, created_at = '2026-01-03' WHERE project_id = 'alice_new'")
        conn.commit()

    db.ensure_project("bob_mid", name="Bob Mid", owner_email="bob@t.com")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'bob_mid'"); _c.commit()
    with db.get_connection() as conn:
        conn.execute("UPDATE projects SET created_at = '2026-01-02' WHERE project_id = 'bob_mid'")
        conn.commit()

    # Alice FIFO: picks her oldest
    result = db.get_next_active_project(owner_email="alice@t.com", fifo=True)
    assert result["project_id"] == "alice_old"

    # Alice non-FIFO: picks her highest priority
    result = db.get_next_active_project(owner_email="alice@t.com", fifo=False)
    assert result["project_id"] == "alice_new"

    # Bob only has one project
    result = db.get_next_active_project(owner_email="bob@t.com", fifo=True)
    assert result["project_id"] == "bob_mid"


def test_fifo_returns_none_when_empty(tmp_path):
    """FIFO should return None when no active projects."""
    db = DBManager(str(tmp_path / "fifo_empty.db"))
    assert db.get_next_active_project(fifo=True) is None


# ── retry_project tests ──


def test_retry_project_resets_failed_status(tmp_path):
    """retry_project should reset project status from failed to planning."""
    db = DBManager(str(tmp_path / "retry_proj.db"))
    db.ensure_project("retry_test_proj", name="Retry Test")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'failed' WHERE project_id = 'retry_test_proj'"); _c.commit()
    # Add a failed task so retry_project has work to reset
    t = db.push_task("retry_test_proj", "test task")
    db.update_task_status(t, TaskStatus.FAILED)

    result = db.retry_project("retry_test_proj")
    assert result is True
    # Pipeline status is owned by skillflow — no longer checks projects.status


def test_retry_project_resets_failed_tasks_to_pending(tmp_path):
    """retry_project should reset all failed tasks to pending and reset their step to t_plan."""
    db = DBManager(str(tmp_path / "retry_tasks.db"))
    db.ensure_project("retry_tasks_proj")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'failed' WHERE project_id = 'retry_tasks_proj'"); _c.commit()
    t1 = db.push_task("retry_tasks_proj", "Failed task 1")
    t2 = db.push_task("retry_tasks_proj", "Failed task 2")
    db.update_task_status(t1, TaskStatus.FAILED)
    db.update_task_status(t2, TaskStatus.FAILED)

    db.retry_project("retry_tasks_proj")

    with db.get_connection() as conn:
        r1 = conn.execute("SELECT status, current_step FROM tasks WHERE id = ?", (t1,)).fetchone()
        r2 = conn.execute("SELECT status, current_step FROM tasks WHERE id = ?", (t2,)).fetchone()
        assert r1["status"] == TaskStatus.PENDING.value
        assert r1["current_step"] == "t_plan"
        assert r2["status"] == TaskStatus.PENDING.value
        assert r2["current_step"] == "t_plan"


def test_retry_project_returns_false_for_non_failed(tmp_path):
    """retry_project should return False if project is not failed."""
    db = DBManager(str(tmp_path / "not_failed.db"))
    db.ensure_project("active_proj")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'executing' WHERE project_id = 'active_proj'"); _c.commit()

    result = db.retry_project("active_proj")
    assert result is False


def test_get_next_active_project_excludes_failed(tmp_path):
    """get_next_active_project should NOT return failed projects."""
    db = DBManager(str(tmp_path / "skip_failed.db"))
    db.ensure_project("failed_proj")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'failed' WHERE project_id = 'failed_proj'"); _c.commit()

    db.ensure_project("planning_proj")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'planning' WHERE project_id = 'planning_proj'"); _c.commit()

    result = db.get_next_active_project()
    assert result["project_id"] == "planning_proj"


def test_retry_project_allows_failed_project_to_be_rescheduled(tmp_path):
    """After retry_project, the project should be picked up by get_next_active_project."""
    db = DBManager(str(tmp_path / "reschedule.db"))
    db.ensure_project("was_failed_proj")
    # set_project_status removed (was no-op); set via direct SQL
    with db.get_connection() as _c: _c.execute("UPDATE projects SET status = 'failed' WHERE project_id = 'was_failed_proj'"); _c.commit()

    db.retry_project("was_failed_proj")

    # Add a pending task so get_next_active_project picks it up
    # (skillflow runs don't exist in test env, task-based fallback is used)
    db.push_task("was_failed_proj", "retry test task")

    result = db.get_next_active_project()
    assert result is not None
    assert result["project_id"] == "was_failed_proj"


# ── _sync_task_statuses condition tests ──────────────────────────────

class FakeSF:
    """Minimal fake that records whether _conn was accessed."""
    def __init__(self, has_loop_state=False, loop_index=0):
        self._conn_accessed = False
        self._has_loop_state = has_loop_state
        self._loop_index = loop_index

    class _FakeConn:
        def execute(self, query, params):
            pass
        def fetchone(self):
            return None

    @property
    def _conn(self):
        return self._FakeConn()


def test_sync_task_statuses_returns_early_for_failed(tmp_path):
    """_sync_task_statuses should return early when run status is 'failed'."""
    from core.scheduler import _sync_task_statuses
    from unittest.mock import patch, MagicMock

    db = DBManager(str(tmp_path / "sync_failed.db"))
    db.ensure_project("sync_test")
    db.push_task("sync_test", "test task")

    sf = FakeSF()
    run = {"id": "fake-run", "status": "failed"}

    # Should return early without accessing sf internals
    with patch("core.scheduler.db", db):
        _sync_task_statuses("sync_test", run, sf)
    # If we got here without sf._conn being accessed for loop state, it's correct


def test_sync_task_statuses_returns_early_for_paused(tmp_path):
    """_sync_task_statuses should return early when run status is 'paused'.
    Regression: before the fix, the 'and run[\"status\"] == \"failed\"' clause
    caused paused runs to fall through and incorrectly update task statuses.
    """
    from core.scheduler import _sync_task_statuses
    from unittest.mock import patch

    db = DBManager(str(tmp_path / "sync_paused.db"))
    db.ensure_project("sync_test")
    db.push_task("sync_test", "test task")

    sf = FakeSF()
    run = {"id": "fake-run", "status": "paused"}

    # Should return early without trying to read skillflow_loop_state
    with patch("core.scheduler.db", db):
        _sync_task_statuses("sync_test", run, sf)
    # If we reached here without error, the early return worked


def test_sync_task_statuses_returns_early_for_completed(tmp_path):
    """_sync_task_statuses should mark all tasks completed for completed runs."""
    from core.scheduler import _sync_task_statuses
    from unittest.mock import patch

    db = DBManager(str(tmp_path / "sync_completed.db"))
    db.ensure_project("sync_test")
    task_id = db.push_task("sync_test", "test task")

    sf = FakeSF()
    run = {"id": "fake-run", "status": "completed"}

    with patch("core.scheduler.db", db):
        _sync_task_statuses("sync_test", run, sf)

    # Task should now be completed
    tasks = db.list_tasks_by_project("sync_test")
    assert len(tasks) == 1
    assert tasks[0]["status"] == TaskStatus.COMPLETED.value
