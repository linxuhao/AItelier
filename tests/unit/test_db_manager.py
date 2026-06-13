# tests/unit/test_db_manager.py

import json
import pytest
import sqlite3
from core.db_manager import DBManager
from models.schemas import TaskStatus


def test_wal_mode_enabled(db_manager):
    """测试系统是否成功强制挂载了 WAL 并发日志模式"""
    with sqlite3.connect(db_manager.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        mode = cursor.fetchone()[0]
        assert mode.lower() == "wal"

def test_push_and_get_next_task_atomicity(db_manager):
    """测试任务排队与原子弹出的 FIFO 逻辑"""
    # 1. 压入双重任务
    t1_id = db_manager.push_task("proj_A", "Task A prompt")
    t2_id = db_manager.push_task("proj_B", "Task B prompt")

    # 2. 原子获取第一条，需验证状态自动跃迁为 RUNNING
    next_task = db_manager.get_next_pending_task()
    assert next_task["id"] == t1_id
    assert next_task["project_id"] == "proj_A"
    assert next_task["status"] == TaskStatus.RUNNING.value

    # 3. 验证再次拉取，应为第二条
    next_task_2 = db_manager.get_next_pending_task()
    assert next_task_2["id"] == t2_id
    assert next_task_2["status"] == TaskStatus.RUNNING.value

    # 4. 队列空载响应测试
    empty_task = db_manager.get_next_pending_task()
    assert empty_task is None

def test_update_task_status(db_manager):
    """测试状态机流转控制"""
    task_id = db_manager.push_task("proj_C", "Status test")
    success = db_manager.update_task_status(task_id, TaskStatus.COMPLETED)
    assert success is True

    # 验证底层状态真正落盘
    with db_manager.get_connection() as conn:
        cursor = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert cursor.fetchone()["status"] == TaskStatus.COMPLETED.value

def test_record_io_log(db_manager):
    """测试日志写盘功能"""
    log_id = db_manager.record_io_log({
        "task_id": 99,
        "step_name": "Step 2: Architecture",
        "direction": "OUTBOX",
        "git_commit_hash": "a1b2c3d4",
        "content_summary": "Generated DAG files"
    })
    assert log_id > 0

def test_ensure_project(db_manager):
    """测试项目幂等创建"""
    p1 = db_manager.ensure_project("my_proj", name="My Project")
    assert p1["project_id"] == "my_proj"
    assert p1["name"] == "My Project"

    # Idempotent — returns same row
    p2 = db_manager.ensure_project("my_proj", name="Ignored")
    assert p2["project_id"] == "my_proj"
    # Name not updated on existing project
    assert p2["name"] == "My Project"

def test_get_project(db_manager):
    """测试项目查询"""
    db_manager.ensure_project("lookup_proj")
    result = db_manager.get_project("lookup_proj")
    assert result is not None
    assert result["project_id"] == "lookup_proj"

    assert db_manager.get_project("nonexistent") is None

def test_delete_project(db_manager):
    """测试项目删除"""
    db_manager.ensure_project("del_proj")
    assert db_manager.delete_project("del_proj") is True
    assert db_manager.get_project("del_proj") is None
    assert db_manager.delete_project("del_proj") is False

def test_list_projects_with_stats(db_manager):
    """测试带统计的项目列表"""
    db_manager.ensure_project("proj_1")
    db_manager.push_task("proj_1", "Task 1")

    projects = db_manager.list_projects_with_stats()
    assert len(projects) >= 1
    ids = {p["project_id"] for p in projects}
    assert "proj_1" in ids

def test_create_tasks_from_manifest(db_manager):
    """测试从 manifest 创建任务"""
    db_manager.ensure_project("manifest_proj")
    manifest = {
        "tasks": [
            {"id": "t1", "description": "First task", "dependencies": [], "task_type": "normal"},
            {"id": "t2", "description": "Second task", "dependencies": ["t1"], "task_type": "normal"},
        ],
        "execution_order": [["t1"], ["t2"]],
    }
    task_ids = db_manager.create_tasks_from_manifest("manifest_proj", manifest)
    assert len(task_ids) == 2

    # t2 should have t1 as dependency
    tasks = db_manager.list_tasks()
    t2 = [t for t in tasks if t["id"] == task_ids[1]][0]
    import json
    deps = json.loads(t2["dependencies"])
    assert task_ids[0] in deps

def test_get_ready_tasks(db_manager):
    """测试就绪任务查询（依赖关系）"""
    db_manager.ensure_project("ready_proj")

    t1 = db_manager.push_task("ready_proj", "Independent task")
    t2 = db_manager.push_task("ready_proj", "Dependent task")

    # t2 depends on t1
    import json
    with db_manager.get_connection() as conn:
        conn.execute("UPDATE tasks SET dependencies = ? WHERE id = ?", (json.dumps([t1]), t2))
        conn.commit()

    # Only t1 should be ready (t2 depends on t1 which is not completed)
    ready = db_manager.get_ready_tasks("ready_proj")
    assert len(ready) == 1
    assert ready[0]["id"] == t1

def test_has_incomplete_tasks(db_manager):
    """测试未完成任务检查"""
    db_manager.ensure_project("incomp_proj")
    assert db_manager.has_incomplete_tasks("incomp_proj") is False

    task_id = db_manager.push_task("incomp_proj", "Do work")
    assert db_manager.has_incomplete_tasks("incomp_proj") is True

    db_manager.update_task_status(task_id, TaskStatus.COMPLETED)
    assert db_manager.has_incomplete_tasks("incomp_proj") is False

def test_advance_project_step_delegates_to_skillflow(db_manager):
    """advance_project_step returns None — skillflow handles via advance_run()."""
    db_manager.ensure_project("adv_proj")
    next_step = db_manager.advance_project_step("adv_proj")
    assert next_step is None


# ── Users table tests ──


def test_users_table_exists(db_manager):
    """users table should exist after DB init."""
    with db_manager.get_connection() as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
    assert "users" in tables


def test_sentinel_cli_user_created(db_manager):
    """Sentinel cli@local user should be auto-created on init."""
    with db_manager.get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", ("cli@local",)).fetchone()
    assert row is not None
    assert row["email"] == "cli@local"
    assert row["source"] == "cli"


# ── owner_email column tests ──


def test_push_task_default_owner(db_manager):
    """Tasks without explicit owner get cli@local."""
    db_manager.ensure_project("owner_proj")
    task_id = db_manager.push_task("owner_proj", "test prompt")
    with db_manager.get_connection() as conn:
        row = conn.execute("SELECT owner_email FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["owner_email"] == "cli@local"


def test_push_task_custom_owner(db_manager):
    """Tasks with explicit owner_email."""
    db_manager.ensure_project("custom_proj", owner_email="user@test.com")
    task_id = db_manager.push_task("custom_proj", "test prompt", owner_email="user@test.com")
    with db_manager.get_connection() as conn:
        row = conn.execute("SELECT owner_email FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["owner_email"] == "user@test.com"


def test_ensure_project_default_owner(db_manager):
    """Projects without explicit owner get cli@local."""
    p = db_manager.ensure_project("def_owner_proj")
    assert p["owner_email"] == "cli@local"


def test_ensure_project_custom_owner(db_manager):
    """Projects with explicit owner_email."""
    p = db_manager.ensure_project("custom_owner_proj", owner_email="alice@test.com")
    assert p["owner_email"] == "alice@test.com"


def test_list_tasks_owner_filter(db_manager):
    """list_tasks filters by owner_email when provided."""
    db_manager.ensure_project("filter_proj", owner_email="a@t.com")
    db_manager.push_task("filter_proj", "task A", owner_email="a@t.com")
    db_manager.push_task("filter_proj", "task B", owner_email="b@t.com")

    # Filter for a@t.com
    a_tasks = db_manager.list_tasks(owner_email="a@t.com")
    assert len(a_tasks) == 1
    assert a_tasks[0]["owner_email"] == "a@t.com"

    # No filter returns all
    all_tasks = db_manager.list_tasks()
    assert len(all_tasks) == 2


def test_list_projects_with_stats_owner_filter(db_manager):
    """list_projects_with_stats filters by owner_email."""
    db_manager.ensure_project("pa", owner_email="x@t.com")
    db_manager.ensure_project("pb", owner_email="y@t.com")

    x_projects = db_manager.list_projects_with_stats(owner_email="x@t.com")
    assert len(x_projects) == 1
    assert x_projects[0]["project_id"] == "pa"

    all_projects = db_manager.list_projects_with_stats()
    assert len(all_projects) == 2


def test_list_tasks_by_project_owner_filter(db_manager):
    """list_tasks_by_project filters by owner_email."""
    db_manager.ensure_project("proj_mixed", owner_email="m@t.com")
    db_manager.push_task("proj_mixed", "owned by m", owner_email="m@t.com")
    db_manager.push_task("proj_mixed", "owned by n", owner_email="n@t.com")

    m_tasks = db_manager.list_tasks_by_project("proj_mixed", owner_email="m@t.com")
    assert len(m_tasks) == 1
    assert m_tasks[0]["owner_email"] == "m@t.com"

    all_tasks = db_manager.list_tasks_by_project("proj_mixed")
    assert len(all_tasks) == 2


def test_create_tasks_from_manifest_owner(db_manager):
    """create_tasks_from_manifest sets owner_email on all tasks."""
    db_manager.ensure_project("manifest_owner_proj", owner_email="dev@test.com")
    manifest = {
        "tasks": [
            {"id": "m1", "description": "Task one", "dependencies": [], "task_type": "normal"},
        ],
        "execution_order": [["m1"]],
    }
    task_ids = db_manager.create_tasks_from_manifest("manifest_owner_proj", manifest, owner_email="dev@test.com")
    assert len(task_ids) == 1

    with db_manager.get_connection() as conn:
        row = conn.execute("SELECT owner_email FROM tasks WHERE id = ?", (task_ids[0],)).fetchone()
    assert row["owner_email"] == "dev@test.com"


# ── Repo type columns tests ──


def test_ensure_project_default_repo_type(db_manager):
    """Projects default to repo_type='new'."""
    p = db_manager.ensure_project("repo_proj")
    assert p["repo_type"] == "new"
    assert p["repo_path"] is None
    assert p["repo_url"] is None


def test_ensure_project_with_repo_fields(db_manager):
    """Projects can be created with repo_type, repo_path, repo_url."""
    p = db_manager.ensure_project(
        "clone_proj",
        repo_type="clone",
        repo_path="/some/path",
        repo_url="https://github.com/user/repo.git",
    )
    assert p["repo_type"] == "clone"
    assert p["repo_path"] == "/some/path"
    assert p["repo_url"] == "https://github.com/user/repo.git"


def test_get_repo_info(db_manager):
    """get_repo_info returns repo fields."""
    db_manager.ensure_project(
        "info_proj",
        repo_type="existing",
        repo_path="/local/repo",
    )
    info = db_manager.get_repo_info("info_proj")
    assert info["repo_type"] == "existing"
    assert info["repo_path"] == "/local/repo"
    assert info["repo_url"] is None


def test_get_repo_info_not_found(db_manager):
    """get_repo_info raises ValueError for unknown project."""
    with pytest.raises(ValueError, match="not found"):
        db_manager.get_repo_info("nonexistent_proj")


def test_get_repo_info_defaults(db_manager):
    """get_repo_info defaults repo_type to 'new' if null."""
    db_manager.ensure_project("null_repo_proj")
    # Explicitly set repo_type to None to test default
    with db_manager.get_connection() as conn:
        conn.execute("UPDATE projects SET repo_type = NULL WHERE project_id = 'null_repo_proj'")
        conn.commit()
    info = db_manager.get_repo_info("null_repo_proj")
    assert info["repo_type"] == "new"


# ── retry_project tests ──


def test_retry_project_resets_failed_project(db_manager):
    """retry_project should reset a failed project and its failed tasks."""
    db_manager.ensure_project("failed_proj")
    # Set up with direct SQL — set_project_status is no-op (skillflow owns status)
    with db_manager.get_connection() as conn:
        conn.execute("UPDATE projects SET status = ? WHERE project_id = ?", ("failed", "failed_proj"))
        conn.commit()
    task_id = db_manager.push_task("failed_proj", "Will retry")
    db_manager.update_task_status(task_id, TaskStatus.FAILED)

    success = db_manager.retry_project("failed_proj")
    assert success is True

    # Pipeline status is owned by skillflow — projects.status is no longer
    # the source of truth. Verify task reset instead.

    with db_manager.get_connection() as conn:
        row = conn.execute("SELECT status, current_step FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["status"] == TaskStatus.PENDING.value
        assert row["current_step"] == "t_plan"


def test_retry_project_non_failed_noop(db_manager):
    """retry_project should return False for non-failed projects."""
    db_manager.ensure_project("active_proj")
    with db_manager.get_connection() as conn:
        conn.execute("UPDATE projects SET status = ? WHERE project_id = ?", ("executing", "active_proj"))
        conn.commit()

    success = db_manager.retry_project("active_proj")
    assert success is False


def test_retry_project_multiple_failed_tasks(db_manager):
    """retry_project should reset all failed tasks in the project."""
    db_manager.ensure_project("multi_fail_proj")
    # Pipeline status is owned by skillflow — no longer stored in projects table
    t1 = db_manager.push_task("multi_fail_proj", "Task 1")
    t2 = db_manager.push_task("multi_fail_proj", "Task 2")
    db_manager.update_task_status(t1, TaskStatus.FAILED)
    db_manager.update_task_status(t2, TaskStatus.FAILED)

    db_manager.retry_project("multi_fail_proj")

    with db_manager.get_connection() as conn:
        r1 = conn.execute("SELECT status, current_step FROM tasks WHERE id = ?", (t1,)).fetchone()
        r2 = conn.execute("SELECT status, current_step FROM tasks WHERE id = ?", (t2,)).fetchone()
        assert r1["status"] == TaskStatus.PENDING.value
        assert r2["status"] == TaskStatus.PENDING.value


# ── task_meta_state tests ──


def test_set_and_get_task_meta_state(db_manager):
    """set_task_meta_state and get_task_meta_state should store and retrieve JSON error data."""
    db_manager.ensure_project("meta_proj")
    task_id = db_manager.push_task("meta_proj", "Error tracking test")

    error_data = json.dumps({"error": "Max retries exceeded", "step": "t_impl", "type": "MaxRetriesExceeded"})
    db_manager.set_task_meta_state(task_id, error_data)

    retrieved = db_manager.get_task_meta_state(task_id)
    assert retrieved is not None
    parsed = json.loads(retrieved)
    assert parsed["error"] == "Max retries exceeded"
    assert parsed["step"] == "t_impl"
    assert parsed["type"] == "MaxRetriesExceeded"


def test_get_task_meta_state_none_when_unset(db_manager):
    """get_task_meta_state should return None when no meta_state is set."""
    db_manager.ensure_project("no_meta_proj")
    task_id = db_manager.push_task("no_meta_proj", "No meta state")
    assert db_manager.get_task_meta_state(task_id) is None


def test_task_meta_state_preserves_existing(db_manager):
    """set_task_meta_state should overwrite existing meta_state."""
    db_manager.ensure_project("overwrite_proj")
    task_id = db_manager.push_task("overwrite_proj", "Overwrite test")

    db_manager.set_task_meta_state(task_id, json.dumps({"error": "first"}))
    db_manager.set_task_meta_state(task_id, json.dumps({"error": "second", "traceback": "stack"}))

    retrieved = json.loads(db_manager.get_task_meta_state(task_id))
    assert retrieved["error"] == "second"
    assert "traceback" in retrieved
