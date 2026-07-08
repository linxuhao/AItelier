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

def test_ensure_project_defaults_repo_path_for_new(db_manager):
    """new/clone projects get repo_path defaulted to projects_dir()/id.

    Regression: butler-created 'new' projects passed repo_path=None, leaving the
    row out of the dashboard's repository grouping (repo_path IS NOT NULL filter).
    """
    from core.datadir import projects_dir

    p = db_manager.ensure_project("repo_new_proj", repo_type="new")
    assert p["repo_path"] == str(projects_dir() / "repo_new_proj")

    # An explicit repo_path (e.g. existing repo) is never overwritten.
    p2 = db_manager.ensure_project(
        "repo_existing_proj", repo_type="existing", repo_path="/some/real/repo"
    )
    assert p2["repo_path"] == "/some/real/repo"

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

def test_sync_tasks_preserves_completed_on_redecomposition(db_manager):
    """Goal-loop resync must keep completed tasks, not wipe them (delete-all bug)."""
    import json
    db_manager.ensure_project("sync_proj")
    # First decomposition: 3 tasks.
    manifest_v1 = {
        "tasks": [
            {"id": "a", "description": "Task A", "dependencies": [], "task_type": "normal"},
            {"id": "b", "description": "Task B", "dependencies": ["a"], "task_type": "normal"},
            {"id": "c", "description": "Task C", "dependencies": [], "task_type": "normal"},
        ],
        "execution_order": [["a"], ["b"], ["c"]],
    }
    ids = db_manager.create_tasks_from_manifest("sync_proj", manifest_v1)
    # Mark a + b completed (as if the loop finished them).
    for tid in ids[:2]:
        db_manager.update_task_status(tid, "completed")

    # Goal-loop re-runs PM and emits only a NEW remediation card (partial manifest).
    manifest_v2 = {
        "tasks": [
            {"id": "fix_a", "description": "Fix A", "dependencies": ["a"], "task_type": "normal"},
        ],
        "execution_order": [["a"], ["b"], ["c"], ["fix_a"]],
    }
    db_manager.sync_tasks_from_manifest("sync_proj", manifest_v2)

    tasks = db_manager.list_tasks_by_project("sync_proj")
    keys = {t["manifest_key"]: t["status"] for t in tasks}
    # Completed a + b survive; incomplete c is dropped/re-derived; new fix_a added.
    assert keys.get("a") == "completed"
    assert keys.get("b") == "completed"
    assert "fix_a" in keys
    assert keys.get("fix_a") == "pending"
    # New task's dep on completed "a" resolves to the preserved row.
    fix = [t for t in tasks if t["manifest_key"] == "fix_a"][0]
    a = [t for t in tasks if t["manifest_key"] == "a"][0]
    assert a["id"] in json.loads(fix["dependencies"])


def test_supersede_task_archives_and_clones(db_manager):
    """A goal-loop re-run preserves the completed attempt as SUPERSEDED and
    clones a fresh PENDING re-run row (same manifest_key/prompt/deps)."""
    import json
    db_manager.ensure_project("sup_proj")
    manifest = {
        "tasks": [
            {"id": "boot", "description": "Bootstrapper", "dependencies": [],
             "task_type": "normal"},
        ],
        "execution_order": [["boot"]],
    }
    ids = db_manager.create_tasks_from_manifest("sup_proj", manifest)
    db_manager.complete_task(ids[0])

    new_id = db_manager.supersede_task(ids[0])
    assert new_id is not None and new_id != ids[0]

    tasks = {t["id"]: t for t in db_manager.list_tasks_by_project("sup_proj")}
    assert tasks[ids[0]]["status"] == "superseded"   # prior attempt preserved
    assert tasks[new_id]["status"] == "pending"       # re-run row
    assert tasks[new_id]["manifest_key"] == "boot"    # same key → loop maps onto it
    # superseded row is not "incomplete work" (won't block scheduler/gates)
    assert db_manager.has_incomplete_tasks("sup_proj") is True  # the new pending row
    db_manager.complete_task(new_id)
    assert db_manager.has_incomplete_tasks("sup_proj") is False  # superseded ignored
    # Re-superseding a non-completed row is a no-op.
    assert db_manager.supersede_task(ids[0]) is None


def test_sync_tasks_keeps_superseded_history(db_manager):
    """A subsequent manifest resync must NOT delete SUPERSEDED audit rows."""
    db_manager.ensure_project("sup2_proj")
    manifest = {
        "tasks": [{"id": "boot", "description": "B", "dependencies": [],
                   "task_type": "normal"}],
        "execution_order": [["boot"]],
    }
    ids = db_manager.create_tasks_from_manifest("sup2_proj", manifest)
    db_manager.complete_task(ids[0])
    new_id = db_manager.supersede_task(ids[0])
    db_manager.complete_task(new_id)  # re-run finishes before the next re-decompose
    # A later goal-loop re-decomposition resyncs the same manifest.
    db_manager.sync_tasks_from_manifest("sup2_proj", manifest)
    statuses = {t["id"]: t["status"]
                for t in db_manager.list_tasks_by_project("sup2_proj")}
    assert statuses.get(ids[0]) == "superseded"  # history survives the resync
    assert statuses.get(new_id) == "completed"   # live completed re-run survives too


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


def test_upsert_user_stores_epoch_integer(db_manager):
    """upsert_user should store last_seen_at as an integer Unix epoch."""
    db_manager.upsert_user("epoch@test.com", "Epoch User")
    with db_manager.get_connection() as conn:
        row = conn.execute(
            "SELECT last_seen_at FROM users WHERE email = ?",
            ("epoch@test.com",),
        ).fetchone()
    assert isinstance(row["last_seen_at"], int)
    assert row["last_seen_at"] > 1700000000  # after 2023


def test_delete_user_existing(db_manager):
    """delete_user should remove an existing user and return True."""
    db_manager.upsert_user("del@test.com", "Delete Me")
    result = db_manager.delete_user("del@test.com")
    assert result is True
    with db_manager.get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE email = ?", ("del@test.com",)
        ).fetchone()
    assert row is None


def test_delete_user_nonexistent(db_manager):
    """delete_user should return False for a non-existent email."""
    result = db_manager.delete_user("noone@test.com")
    assert result is False


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
    """Projects default to repo_type='new' with repo_path under projects_dir()."""
    from core.datadir import projects_dir

    p = db_manager.ensure_project("repo_proj")
    assert p["repo_type"] == "new"
    assert p["repo_path"] == str(projects_dir() / "repo_proj")
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
        conn.execute("UPDATE runs SET repo_type = NULL WHERE project_id = 'null_repo_proj'")
        conn.commit()
    info = db_manager.get_repo_info("null_repo_proj")
    assert info["repo_type"] == "new"


# ── retry_project tests ──


def test_retry_project_resets_failed_project(db_manager):
    """retry_project should reset a failed project and its failed tasks."""
    db_manager.ensure_project("failed_proj")
    # Set up with direct SQL — set_project_status is no-op (skillflow owns status)
    with db_manager.get_connection() as conn:
        conn.execute("UPDATE runs SET status = ? WHERE project_id = ?", ("failed", "failed_proj"))
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
        conn.execute("UPDATE runs SET status = ? WHERE project_id = ?", ("executing", "active_proj"))
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


# ── Versioned schema-migration runner (Phase 0 scaffolding) ──────────

def test_schema_migrations_records_baseline(db_manager):
    """A fresh DB records the legacy schema (v0) plus all registered structural
    migrations (which no-op on a fresh DB)."""
    with db_manager.get_connection() as conn:
        versions = sorted(r["version"] for r in conn.execute(
            "SELECT version FROM schema_migrations").fetchall())
    assert versions[0] == 0
    assert 1 in versions   # projects_to_runs recorded (no-op on fresh DB)


def test_schema_migrations_idempotent(tmp_path):
    """Re-opening the DB does not duplicate or re-run recorded migrations."""
    db_file = str(tmp_path / "idem.db")
    DBManager(db_file)
    DBManager(db_file)  # reopen
    with sqlite3.connect(db_file) as conn:
        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations").fetchall()]
    # No duplicates across reopens.
    assert len(versions) == len(set(versions))
    assert versions[0] == 0


def test_versioned_migration_runs_once(tmp_path, monkeypatch):
    """A registered numbered migration runs exactly once and records its version;
    re-opening does not re-run it."""
    calls = []

    def _fake_mig(self, conn):
        calls.append(1)
        conn.execute("CREATE TABLE _mig_marker (x INTEGER)")

    monkeypatch.setattr(DBManager, "_VERSIONED_MIGRATIONS",
                        [(1, "fake_mig", _fake_mig)])
    db_file = str(tmp_path / "mig.db")
    DBManager(db_file)
    assert calls == [1]
    with sqlite3.connect(db_file) as conn:
        versions = sorted(r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations").fetchall())
        assert versions == [0, 1]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='_mig_marker'").fetchone()

    # Re-open: migration must NOT run again.
    DBManager(db_file)
    assert calls == [1]


# ── Phase 2: projects→runs + config_name + dpe_run_state migration ──

def _seed_legacy_projects_db(path: str):
    """Create an old project-based schema with data (pre-migration)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'planning',
            current_project_step TEXT DEFAULT '1',
            completed_project_steps TEXT DEFAULT '[]',
            brief TEXT DEFAULT NULL,
            priority INTEGER DEFAULT 0,
            owner_email TEXT DEFAULT 'cli@local',
            meta_state TEXT DEFAULT NULL,
            sota_version INTEGER DEFAULT 1,
            sota_updated_at DATETIME DEFAULT NULL,
            tasks_since_arch_update INTEGER DEFAULT 0,
            repo_type TEXT DEFAULT 'new',
            repo_path TEXT DEFAULT NULL,
            repo_url TEXT DEFAULT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(project_id)
        );
        INSERT INTO projects (project_id, name, status, completed_project_steps,
                              brief, priority, tasks_since_arch_update)
            VALUES ('proj_legacy', 'Legacy', 'executing', '["1","2"]',
                    'the brief text', 7, 3);
        INSERT INTO tasks (project_id, prompt, status)
            VALUES ('proj_legacy', 'do the thing', 'completed');
    """)
    conn.commit()
    conn.close()


def test_projects_to_runs_migration_parity(tmp_path):
    """Opening a legacy projects DB migrates it to the run schema with full
    field parity, config_name backfill, and DPE-state extraction."""
    db_file = str(tmp_path / "legacy.db")
    _seed_legacy_projects_db(db_file)

    db = DBManager(db_file)

    with db.get_connection() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "runs" in names and "projects" not in names
        assert "dpe_run_state" in names

        run = conn.execute("SELECT * FROM runs WHERE project_id='proj_legacy'").fetchone()
        assert run is not None
        assert run["config_name"] == "dpe_default_v2"   # backfilled
        assert run["name"] == "Legacy"
        assert run["status"] == "executing"
        assert run["priority"] == 7
        # DPE columns extracted off the run row
        run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        assert not ({"brief", "completed_project_steps", "sota_version",
                     "tasks_since_arch_update"} & run_cols)

        state = conn.execute(
            "SELECT * FROM dpe_run_state WHERE run_key='proj_legacy'").fetchone()
        assert state["brief"] == "the brief text"
        assert state["completed_project_steps"] == '["1","2"]'
        assert state["tasks_since_arch_update"] == 3

        # tasks FK auto-updated to runs, row preserved
        fk = conn.execute("PRAGMA foreign_key_list(tasks)").fetchall()
        assert any(r["table"] == "runs" for r in fk)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    # get_project surfaces the DPE state back onto the dict (shape preserved)
    proj = db.get_project("proj_legacy")
    assert proj["brief"] == "the brief text"
    assert proj["completed_project_steps"] == '["1","2"]'
    assert proj["config_name"] == "dpe_default_v2"

    # backup file written, migration recorded, idempotent re-open
    assert (tmp_path / "legacy.db.premigration-v1.bak").exists()
    with sqlite3.connect(db_file) as conn:
        versions = sorted(r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations").fetchall())
    assert versions == [0, 1, 2]
    DBManager(db_file)  # re-open must not error or double-apply


def test_dpe_run_state_roundtrip_on_fresh_db(db_manager):
    """brief / completed steps / tasks-since counters round-trip through
    dpe_run_state on a fresh run."""
    db_manager.ensure_project("p_new", config_name="dpe_default_v2")
    db_manager.set_project_brief("p_new", "hello brief")
    db_manager.update_project("p_new", completed_project_steps='["1"]', status="executing")

    proj = db_manager.get_project("p_new")
    assert proj["brief"] == "hello brief"
    assert proj["completed_project_steps"] == '["1"]'
    assert proj["status"] == "executing"
    assert proj["config_name"] == "dpe_default_v2"

    db_manager.increment_tasks_since_update("p_new")
    db_manager.increment_tasks_since_update("p_new")
    assert db_manager.should_refresh_planning("p_new", threshold=2) is True
    db_manager.reset_tasks_since_update("p_new")
    assert db_manager.should_refresh_planning("p_new", threshold=2) is False


def test_ensure_project_records_config_name(db_manager):
    """A run created for a non-DPE config records its config_name."""
    db_manager.ensure_project("conv_run", config_name="meta_conversation")
    assert db_manager.get_project("conv_run")["config_name"] == "meta_conversation"


# ── _sanitize_transcript: valid LLM message sequence under interleaving ──

def _a(*ids, content=None):
    """assistant message, optionally with tool_calls."""
    m = {"role": "assistant", "content": content}
    if ids:
        m["tool_calls"] = [{"id": i, "function": {"name": "x"}} for i in ids]
    return m

def _t(tcid):
    return {"role": "tool", "tool_call_id": tcid, "content": "{}"}

def _assert_valid(msgs):
    """Every assistant tool_calls must be immediately followed by EXACTLY its
    results; no orphan tool results; no empty rows."""
    i = 0
    while i < len(msgs):
        m = msgs[i]; calls = m.get("tool_calls") or []
        if m["role"] == "tool":
            raise AssertionError(f"orphan tool result at {i}: {m['tool_call_id']}")
        if m["role"] == "assistant" and calls:
            want = [c["id"] for c in calls]; got = []
            j = i + 1
            while j < len(msgs) and msgs[j]["role"] == "tool":
                got.append(msgs[j]["tool_call_id"]); j += 1
            assert got == want, f"want {want} got {got}"
            i = j; continue
        assert (m.get("content") or "").strip() or calls, f"empty msg at {i}"
        i += 1

def test_sanitize_interleaved_async_results():
    """The real bug: an async/checkpoint tool result (approve_project_brief)
    lands late, after a user message and another turn's call — so results are
    neither adjacent nor exclusive to their assistant. Sanitize must pair each
    assistant with its OWN result by id, not greedily grab trailing tools."""
    msgs = [
        {"role": "user", "content": "go"},
        _a("A"),                      # approve_project_brief
        {"role": "user", "content": "wait"},   # user interjects before A returns
        _a("B"),                      # wait_until_checkpoint
        _t("B"),                      # B's result
        _t("A"),                      # A's result arrives LATE, out of order
    ]
    out = DBManager._sanitize_transcript(msgs)
    _assert_valid(out)
    # both calls preserved, each adjacent to its own result
    assert any(m.get("tool_calls") and m["tool_calls"][0]["id"] == "A" for m in out)
    assert any(m.get("tool_calls") and m["tool_calls"][0]["id"] == "B" for m in out)

def test_sanitize_drops_unanswered_call():
    """A tool call whose result never arrived (crash / still in-flight) → drop
    the whole assistant group so no unanswered tool_call reaches the provider."""
    msgs = [_a("A"), _t("A"), _a("MISSING"), {"role": "user", "content": "hi"}]
    out = DBManager._sanitize_transcript(msgs)
    _assert_valid(out)
    assert not any(m.get("tool_calls") and m["tool_calls"][0]["id"] == "MISSING" for m in out)

def test_sanitize_drops_orphan_tool_and_empties():
    msgs = [
        _t("orphan"),                              # no assistant → drop
        _a(content="  "),                          # empty assistant → drop
        {"role": "user", "content": ""},           # empty user → drop
        {"role": "user", "content": "real"},
    ]
    out = DBManager._sanitize_transcript(msgs)
    _assert_valid(out)
    assert out == [{"role": "user", "content": "real"}]
