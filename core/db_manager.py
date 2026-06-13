# File: core/db_manager.py

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from models.schemas import TaskStatus

class DBManager:
    """
    系统核心的 SQLite 持久化管理类。
    采用 WAL 模式提升高频读写的并发性能。
    """
    def __init__(self, db_path: str = None):
        if db_path is None:
            home = Path.home() / ".AItelier"
            home.mkdir(parents=True, exist_ok=True)
            db_path = str(home / "aitelier.db")
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def get_connection(self):
        """上下文管理器：提供连接并自动处理连接关闭，设置返回结果为 dict-like"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        """初始化表结构，并强制启用 WAL 日志模式"""
        with self.get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys = ON;")

            # ── Create tables in dependency order: projects → tasks → io_logs → subtasks ──

            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planning',
                    current_project_step TEXT DEFAULT '1',
                    completed_project_steps TEXT DEFAULT '[]',
                    brief TEXT DEFAULT NULL,
                    priority INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    current_step TEXT DEFAULT 't_plan',
                    completed_steps TEXT DEFAULT '[]',
                    current_subtask TEXT DEFAULT NULL,
                    priority INTEGER DEFAULT 0,
                    step_locked INTEGER DEFAULT 0,
                    dependencies TEXT DEFAULT '[]',
                    task_type TEXT DEFAULT 'normal',
                    task_meta_state TEXT DEFAULT NULL,
                    owner_email TEXT DEFAULT 'cli@local',
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    last_error TEXT DEFAULT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(project_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS io_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    step_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    git_commit_hash TEXT NOT NULL,
                    content_summary TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subtasks (
                    id TEXT PRIMARY KEY,
                    task_id INTEGER NOT NULL,
                    description TEXT,
                    dependencies TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retry_count INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    email TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'cloudflare',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_runs (
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    PRIMARY KEY (session_id, run_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # Idempotent migration — add repo columns to projects
            repo_migrations = [
                "ALTER TABLE projects ADD COLUMN repo_type TEXT DEFAULT 'new'",
                "ALTER TABLE projects ADD COLUMN repo_path TEXT DEFAULT NULL",
                "ALTER TABLE projects ADD COLUMN repo_url TEXT DEFAULT NULL",
            ]
            for sql in repo_migrations:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists

            # Sentinel CLI user
            conn.execute(
                "INSERT OR IGNORE INTO users (email, display_name, source) VALUES (?, ?, ?)",
                ("cli@local", "CLI User", "cli"),
            )

            # Idempotent migration — add columns to pre-existing tables
            migrations = [
                "ALTER TABLE tasks ADD COLUMN current_step TEXT DEFAULT '1'",
                "ALTER TABLE tasks ADD COLUMN completed_steps TEXT DEFAULT '[]'",
                "ALTER TABLE tasks ADD COLUMN current_subtask TEXT DEFAULT NULL",
                "ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 0",
                "ALTER TABLE tasks ADD COLUMN step_locked INTEGER DEFAULT 0",
                "ALTER TABLE tasks ADD COLUMN dependencies TEXT DEFAULT '[]'",
                "ALTER TABLE tasks ADD COLUMN task_type TEXT DEFAULT 'normal'",
                "ALTER TABLE tasks ADD COLUMN owner_email TEXT DEFAULT 'cli@local'",
                "ALTER TABLE tasks ADD COLUMN retry_count INTEGER DEFAULT 0",
                "ALTER TABLE tasks ADD COLUMN max_retries INTEGER DEFAULT 3",
                "ALTER TABLE projects ADD COLUMN status TEXT NOT NULL DEFAULT 'planning'",
                "ALTER TABLE projects ADD COLUMN current_project_step TEXT DEFAULT '1'",
                "ALTER TABLE projects ADD COLUMN completed_project_steps TEXT DEFAULT '[]'",
                "ALTER TABLE projects ADD COLUMN brief TEXT DEFAULT NULL",
                "ALTER TABLE projects ADD COLUMN priority INTEGER DEFAULT 0",
                "ALTER TABLE projects ADD COLUMN owner_email TEXT DEFAULT 'cli@local'",
                "ALTER TABLE projects ADD COLUMN meta_state TEXT DEFAULT NULL",
                "ALTER TABLE projects ADD COLUMN sota_version INTEGER DEFAULT 1",
                "ALTER TABLE projects ADD COLUMN sota_updated_at DATETIME DEFAULT NULL",
                "ALTER TABLE projects ADD COLUMN tasks_since_arch_update INTEGER DEFAULT 0",
            ]
            for sql in migrations:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists

            # Migrate mid-flight task steps from old sequence to new sequence
            self._migrate_task_steps(conn)

            # Migrate existing projects that predate the status column
            conn.execute(
                "UPDATE projects SET status = 'executing' "
                "WHERE status = 'planning' AND project_id IN "
                "(SELECT DISTINCT project_id FROM tasks)"
            )

            # Idempotent migration — add FK: tasks.project_id → projects.project_id
            # SQLite can't ALTER ADD CONSTRAINT, so recreate the table if FK is missing.
            self._migrate_tasks_fk(conn)

            # Chat history persistence across CLI restarts
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    project_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'system',
                    content TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Migration: add session_id column to existing chat_history
            try:
                conn.execute("ALTER TABLE chat_history ADD COLUMN session_id TEXT")
            except sqlite3.OperationalError:
                pass

            conn.commit()

    def _migrate_tasks_fk(self, conn):
        """
        Migrate tasks table to add FOREIGN KEY (project_id) REFERENCES projects(project_id).
        SQLite doesn't support ALTER TABLE ADD CONSTRAINT, so we must recreate the table.
        This is idempotent: it checks whether the FK already exists first.
        """
        # Check if FK already exists
        fk_rows = conn.execute("PRAGMA foreign_key_list(tasks)").fetchall()
        has_project_fk = any(
            row["table"] == "projects" and row["from"] == "project_id"
            for row in fk_rows
        )
        if has_project_fk:
            return  # already migrated

        # Ensure all existing tasks have a project row (FK requires it)
        orphan_project_ids = conn.execute(
            "SELECT DISTINCT project_id FROM tasks "
            "WHERE project_id NOT IN (SELECT project_id FROM projects)"
        ).fetchall()
        for row in orphan_project_ids:
            name = row["project_id"].replace("-", " ").replace("_", " ").title()
            conn.execute(
                "INSERT OR IGNORE INTO projects (project_id, name) VALUES (?, ?)",
                (row["project_id"], name)
            )

        # Recreate tasks table with FK + all current columns
        conn.execute("ALTER TABLE tasks RENAME TO _tasks_old")
        conn.execute("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                current_step TEXT DEFAULT 't_plan',
                completed_steps TEXT DEFAULT '[]',
                current_subtask TEXT DEFAULT NULL,
                priority INTEGER DEFAULT 0,
                step_locked INTEGER DEFAULT 0,
                dependencies TEXT DEFAULT '[]',
                task_type TEXT DEFAULT 'normal',
                task_meta_state TEXT DEFAULT NULL,
                owner_email TEXT DEFAULT 'cli@local',
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                FOREIGN KEY (project_id) REFERENCES projects(project_id)
            )
        """)
        # Ensure _tasks_old has all new columns before copy
        for col_sql in [
            "ALTER TABLE _tasks_old ADD COLUMN dependencies TEXT DEFAULT '[]'",
            "ALTER TABLE _tasks_old ADD COLUMN task_type TEXT DEFAULT 'normal'",
            "ALTER TABLE _tasks_old ADD COLUMN task_meta_state TEXT DEFAULT NULL",
            "ALTER TABLE _tasks_old ADD COLUMN owner_email TEXT DEFAULT 'cli@local'",
            "ALTER TABLE _tasks_old ADD COLUMN retry_count INTEGER DEFAULT 0",
            "ALTER TABLE _tasks_old ADD COLUMN max_retries INTEGER DEFAULT 3",
            "ALTER TABLE _tasks_old ADD COLUMN last_error TEXT DEFAULT NULL",
        ]:
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # column already exists

        conn.execute("""
            INSERT INTO tasks SELECT * FROM _tasks_old
        """)
        conn.execute("DROP TABLE _tasks_old")

    def push_task(self, project_id: str, prompt: str, owner_email: str = "cli@local") -> int:
        """推送新任务至队列末尾，默认状态为 PENDING"""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (project_id, prompt, status, current_step, owner_email) VALUES (?, ?, ?, ?, ?)",
                (project_id, prompt, TaskStatus.PENDING.value, "t_plan", owner_email)
            )
            conn.commit()
            task_id = cursor.lastrowid
        # Auto-ensure project row exists
        self.ensure_project(project_id)
        # Wake scheduler so the new task gets picked up
        try:
            from core.scheduler import wake_scheduler
            wake_scheduler(owner_email if owner_email != "cli@local" else None)
        except Exception:
            pass
        return task_id

    def get_next_pending_task(self) -> dict | None:
        """
        [核心逻辑] 获取时间最早的 pending 任务并原子性修改状态为 running。
        利用 SQLite >= 3.35.0 的 RETURNING 语法，避免高并发下的竞态条件 (Race Condition)。
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                UPDATE tasks 
                SET status = ? 
                WHERE id = (
                    SELECT id FROM tasks 
                    WHERE status = ? 
                    ORDER BY created_at ASC 
                    LIMIT 1
                )
                RETURNING *;
            """, (TaskStatus.RUNNING.value, TaskStatus.PENDING.value))
            row = cursor.fetchone()
            conn.commit()
            return dict(row) if row else None

    def update_task_status(self, task_id: int, status: TaskStatus | str) -> bool:
        """更新指定任务的流转状态"""
        # 兼容枚举或直接传字符串
        status_value = status.value if isinstance(status, TaskStatus) else status
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (status_value, task_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def complete_task(self, task_id: int, last_step: str = "t_verify"):
        """Mark a task as completed with accurate final step info."""
        from core.workspace_manager import TASK_STEP_SEQUENCE
        completed_steps = TASK_STEP_SEQUENCE[:TASK_STEP_SEQUENCE.index(last_step) + 1] if last_step in TASK_STEP_SEQUENCE else [last_step]
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, current_step = ?, completed_steps = ? WHERE id = ?",
                (TaskStatus.COMPLETED.value, last_step, json.dumps(completed_steps), task_id)
            )
            conn.commit()

    def set_task_last_error(self, task_id: int, error: str) -> bool:
        """记录任务的最后一次错误信息，同时在 status 变更时追踪。"""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET last_error = ? WHERE id = ?",
                (error[:2000], task_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_tasks(self, limit: int = 50, offset: int = 0, owner_email: str = None) -> list[dict]:
        """分页获取任务列表，按创建时间降序。owner_email=None 返回全部。"""
        with self.get_connection() as conn:
            if owner_email:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE owner_email = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (owner_email, limit, offset)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset)
                ).fetchall()
            return [dict(r) for r in rows]

    def record_io_log(self, log_data: dict) -> int:
        """持久化记录五步法流水线文件流转日志"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO io_logs (task_id, step_name, direction, git_commit_hash, content_summary)
                VALUES (?, ?, ?, ?, ?)
            """, (
                log_data['task_id'],
                log_data['step_name'],
                log_data['direction'],
                log_data['git_commit_hash'],
                log_data['content_summary']
            ))
            conn.commit()
            return cursor.lastrowid

    # ── Step-granular scheduling methods ──

    def get_task_progress(self, task_id: int) -> dict:
        """读取任务的步骤进度信息"""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT current_step, completed_steps, current_subtask, priority, step_locked FROM tasks WHERE id = ?",
                (task_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Task {task_id} not found")
            return {
                "current_step": row["current_step"],
                "completed_steps": json.loads(row["completed_steps"]),
                "current_subtask": row["current_subtask"],
                "priority": row["priority"],
                "step_locked": bool(row["step_locked"]),
            }

    def acquire_step_lock(self) -> dict | None:
        """
        原子获取步骤锁：选取最高优先级的 running 且未锁定任务。
        使用 RETURNING 避免竞态条件。
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                UPDATE tasks
                SET step_locked = 1
                WHERE id = (
                    SELECT id FROM tasks
                    WHERE status = ? AND step_locked = 0
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                )
                RETURNING *;
            """, (TaskStatus.RUNNING.value,))
            row = cursor.fetchone()
            conn.commit()
            return dict(row) if row else None

    def release_step_lock(self, task_id: int):
        """释放步骤锁"""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET step_locked = 0 WHERE id = ?",
                (task_id,)
            )
            conn.commit()

    def advance_step(self, task_id: int, next_step: str | None,
                     completed_steps: list[str], current_subtask: str | None = None):
        """
        推进任务到下一步。next_step=None 表示所有步骤已完成。
        """
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET current_step = ?, completed_steps = ?, current_subtask = ? WHERE id = ?",
                (next_step, json.dumps(completed_steps), current_subtask, task_id)
            )
            conn.commit()

    def has_running_tasks(self) -> bool:
        """检查是否有正在运行的任务（用于 pending → running 提升）"""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE status = ?",
                (TaskStatus.RUNNING.value,)
            ).fetchone()
            return row["cnt"] > 0

    def get_next_pending_task_priority(self) -> dict | None:
        """
        获取最高优先级的 pending 任务并原子提升为 running。
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                UPDATE tasks
                SET status = ?
                WHERE id = (
                    SELECT id FROM tasks
                    WHERE status = ?
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                )
                RETURNING *;
            """, (TaskStatus.RUNNING.value, TaskStatus.PENDING.value))
            row = cursor.fetchone()
            conn.commit()
            return dict(row) if row else None

    # ── Project management methods ──

    def ensure_project(self, project_id: str, name: str = None, owner_email: str = "cli@local",
                       repo_type: str = "new", repo_path: str = None, repo_url: str = None) -> dict:
        """
        Idempotently create a project row if it does not exist.
        Returns the project dict (existing or newly created).
        """
        if name is None:
            name = project_id.replace("-", " ").replace("_", " ").title()
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row:
                return dict(row)
            conn.execute(
                "INSERT INTO projects (project_id, name, owner_email, repo_type, repo_path, repo_url) VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, name, owner_email, repo_type, repo_path, repo_url)
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            return dict(row)

    def get_project(self, project_id: str) -> dict | None:
        """Return a single project row, or None."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_repo_info(self, project_id: str) -> dict:
        """Return repo_type, repo_path, repo_url for a project."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT repo_type, repo_path, repo_url FROM projects WHERE project_id = ?",
                (project_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Project {project_id} not found")
            return {
                "repo_type": row["repo_type"] or "new",
                "repo_path": row["repo_path"],
                "repo_url": row["repo_url"],
            }

    def update_project(self, project_id: str, name: str = None) -> bool:
        """Update project name and bump updated_at."""
        with self.get_connection() as conn:
            sets, params = [], []
            if name is not None:
                sets.append("name = ?")
                params.append(name)
            sets.append("updated_at = CURRENT_TIMESTAMP")
            params.append(project_id)
            cursor = conn.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE project_id = ?",
                params
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_project(self, project_id: str) -> bool:
        """Delete a project row. Does NOT delete tasks or workspace files."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM projects WHERE project_id = ?", (project_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_project_cascade(self, project_id: str) -> bool:
        """Delete project + all tasks + subtasks + workspace + project repo.
        Also releases step locks on running tasks so the scheduler skips them.
        Returns True if project existed."""
        import shutil
        from pathlib import Path
        with self.get_connection() as conn:
            # Get task IDs for subtask cleanup
            task_rows = conn.execute(
                "SELECT id FROM tasks WHERE project_id = ?", (project_id,)
            ).fetchall()
            task_ids = [r["id"] for r in task_rows]

            if task_ids:
                placeholders = ",".join("?" * len(task_ids))
                conn.execute(f"DELETE FROM subtasks WHERE task_id IN ({placeholders})", task_ids)
                conn.execute(f"DELETE FROM io_logs WHERE task_id IN ({placeholders})", task_ids)

            conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
            cursor = conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            conn.commit()
            existed = cursor.rowcount > 0

        # Clean up workspace directory + project repo
        if existed:
            ws_path = Path.home() / ".AItelier" / "workspaces" / project_id
            if ws_path.exists():
                shutil.rmtree(ws_path, ignore_errors=True)
            proj_path = Path.home() / ".AItelier" / "projects" / project_id
            if proj_path.exists():
                shutil.rmtree(proj_path, ignore_errors=True)

        # Clean up skillflow state (runs, steps, outbox, etc.)
        if existed:
            try:
                from api.dependencies import get_skillflow
                sf = get_skillflow()
                sf.delete_project(project_id)
            except Exception:
                pass  # skillflow may not be initialized in all contexts

        return existed

    def list_tasks_by_project(self, project_id: str, owner_email: str = None) -> list[dict]:
        """List all tasks for a specific project. owner_email=None returns all."""
        with self.get_connection() as conn:
            if owner_email:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE project_id = ? AND owner_email = ? ORDER BY created_at DESC",
                    (project_id, owner_email)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC",
                    (project_id,)
                ).fetchall()
            return [dict(r) for r in rows]

    def update_project(self, project_id: str, name: str = None,
                       brief: str = None, priority: int = None,
                       status: str = None,
                       current_project_step: str = None,
                       completed_project_steps: str = None) -> bool:
        """Update project fields. Only sets non-None values.

        A5 fix: accept current_project_step and completed_project_steps
        so the scheduler sync can push live pipeline progress into the
        project row (previously these kwargs were silently dropped).
        """
        updates = []
        params = []
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if brief is not None:
            updates.append("brief = ?")
            params.append(brief)
        if priority is not None:
            updates.append("priority = ?")
            params.append(priority)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if current_project_step is not None:
            updates.append("current_project_step = ?")
            params.append(current_project_step)
        if completed_project_steps is not None:
            updates.append("completed_project_steps = ?")
            params.append(completed_project_steps)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if not updates:
            return False
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(project_id)
        with self.get_connection() as conn:
            cursor = conn.execute(
                f"UPDATE projects SET {', '.join(updates)} WHERE project_id = ?",
                params
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_projects_with_stats(self, owner_email: str = None) -> list[dict]:
        """
        Return all projects with aggregated task stats.
        Includes per-status counts and last activity timestamp.
        owner_email=None returns all (CLI mode).
        """
        with self.get_connection() as conn:
            if owner_email:
                rows = conn.execute("""
                    SELECT
                        p.project_id,
                        p.name,
                        p.status,
                        p.current_project_step,
                        p.priority,
                        p.created_at,
                        p.updated_at,
                        p.owner_email,
                        COUNT(t.id) AS task_count,
                        SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                        SUM(CASE WHEN t.status = 'running' THEN 1 ELSE 0 END) AS running_count,
                        SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                        SUM(CASE WHEN t.status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                        (SELECT t2.status FROM tasks t2
                         WHERE t2.project_id = p.project_id
                         ORDER BY t2.created_at DESC LIMIT 1) AS latest_status,
                        (SELECT t2.current_step FROM tasks t2
                         WHERE t2.project_id = p.project_id
                         ORDER BY t2.created_at DESC LIMIT 1) AS latest_step,
                        COALESCE(MAX(t.created_at), p.created_at) AS last_update
                    FROM projects p
                    LEFT JOIN tasks t ON t.project_id = p.project_id
                    WHERE p.owner_email = ?
                    GROUP BY p.project_id
                    ORDER BY last_update DESC
                """, (owner_email,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT
                        p.project_id,
                        p.name,
                        p.status,
                        p.current_project_step,
                        p.priority,
                        p.created_at,
                        p.updated_at,
                        p.owner_email,
                        COUNT(t.id) AS task_count,
                        SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                        SUM(CASE WHEN t.status = 'running' THEN 1 ELSE 0 END) AS running_count,
                        SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                        SUM(CASE WHEN t.status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                        (SELECT t2.status FROM tasks t2
                         WHERE t2.project_id = p.project_id
                         ORDER BY t2.created_at DESC LIMIT 1) AS latest_status,
                        (SELECT t2.current_step FROM tasks t2
                         WHERE t2.project_id = p.project_id
                         ORDER BY t2.created_at DESC LIMIT 1) AS latest_step,
                        COALESCE(MAX(t.created_at), p.created_at) AS last_update
                    FROM projects p
                    LEFT JOIN tasks t ON t.project_id = p.project_id
                    GROUP BY p.project_id
                    ORDER BY last_update DESC
                """).fetchall()
            return [dict(r) for r in rows]

    # ── Project-level scheduling methods ──

    def get_next_active_project(self, owner_email: str = None, fifo: bool = False) -> dict | None:
        """
        Get the highest-priority project that has work to do.

        Pipeline state is owned by skillflow — queries skillflow_runs for
        active runs (running/paused).  Also includes projects whose runs
        are completed/failed but have pending tasks (will be reactivated
        by the scheduler).
        """
        from api.dependencies import get_skillflow
        sf = get_skillflow()

        # Collect project_ids from active skillflow runs (source of truth)
        active_ids: set[str] = set()
        for status in ('running', 'paused'):
            for r in sf.list_runs(status=status):
                pid = r.get("project_id")
                if pid:
                    active_ids.add(pid)

        STATUSES = ('planning', 'executing', 'verifying', 'running')
        ordering = "created_at ASC" if fifo else "priority DESC, updated_at ASC"
        with self.get_connection() as conn:
            row = None
            # Primary path: pick from active skillflow runs (source of truth)
            # A6 fix: also include 'planning' projects that have a non-empty brief
            # (brief-not-empty guard prevents /new-without-/submit projects from
            # spinning the scheduler in an empty-brief loop). This rescues
            # submissions that would otherwise be stuck behind another
            # project's active run.
            planning_guard = ("(status = 'planning' AND brief IS NOT NULL AND brief != ''"
                             " AND (meta_state IS NULL OR meta_state != 'drafting'))")
            if active_ids:
                placeholders = ",".join("?" * len(active_ids))
                if owner_email:
                    row = conn.execute(f"""
                        SELECT * FROM projects
                        WHERE (project_id IN ({placeholders})
                               OR project_id IN (
                                   SELECT DISTINCT t.project_id FROM tasks t
                                   WHERE t.status IN ('pending', 'running')
                               )
                               OR {planning_guard})
                          AND owner_email = ?
                        ORDER BY {ordering}
                        LIMIT 1
                    """, (*active_ids, owner_email)).fetchone()
                else:
                    row = conn.execute(f"""
                        SELECT * FROM projects
                        WHERE (project_id IN ({placeholders})
                               OR project_id IN (
                                   SELECT DISTINCT t.project_id FROM tasks t
                                   WHERE t.status IN ('pending', 'running')
                               )
                               OR {planning_guard})
                        ORDER BY {ordering}
                        LIMIT 1
                    """, (*active_ids,)).fetchone()
            # Fallback: no active skillflow runs OR none matched in the local DB
            # (e.g., tests use an isolated DB while skillflow uses production DB).
            if row is None:
                # Gate: skip projects whose meta conversation hasn't finished
                # (meta_state='drafting'). Same as in _get_or_create_skillflow_run.
                drafting_guard = "AND (meta_state IS NULL OR meta_state != 'drafting')"
                if owner_email:
                    row = conn.execute(f"""
                        SELECT * FROM projects
                        WHERE (status IN ({','.join('?'*len(STATUSES))})
                               OR project_id IN (
                                   SELECT DISTINCT t.project_id FROM tasks t
                                   WHERE t.status IN ('pending', 'running')
                               ))
                          AND owner_email = ?
                          {drafting_guard}
                        ORDER BY {ordering}
                        LIMIT 1
                    """, (*STATUSES, owner_email)).fetchone()
                else:
                    row = conn.execute(f"""
                        SELECT * FROM projects
                        WHERE (status IN ({','.join('?'*len(STATUSES))})
                               OR project_id IN (
                                   SELECT DISTINCT t.project_id FROM tasks t
                                   WHERE t.status IN ('pending', 'running')
                               ))
                          {drafting_guard}
                        ORDER BY {ordering}
                        LIMIT 1
                    """, (*STATUSES,)).fetchone()
            return dict(row) if row else None

    def advance_project_step(self, project_id: str) -> str | None:
        """Deprecated: skillflow owns pipeline progression via advance_run()."""
        return None

    def set_project_brief(self, project_id: str, brief: str):
        """Store the project brief markdown."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE projects SET brief = ?, updated_at = CURRENT_TIMESTAMP WHERE project_id = ?",
                (brief, project_id)
            )
            conn.commit()

    def update_task_prompt(self, task_id: int, prompt: str):
        """Update the prompt field of a task."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET prompt = ? WHERE id = ?",
                (prompt, task_id)
            )
            conn.commit()

    def delete_tasks_by_project(self, project_id: str) -> int:
        """Delete all tasks (and their subtasks/io_logs) for a project.

        Used by the scheduler's manifest resync (FW-2) when the PM re-runs and
        produces a new decomposition; the project row itself is left intact.
        Returns the number of tasks removed.
        """
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE project_id = ?", (project_id,)
            ).fetchall()
            task_ids = [r["id"] for r in rows]
            if task_ids:
                ph = ",".join("?" * len(task_ids))
                conn.execute(f"DELETE FROM subtasks WHERE task_id IN ({ph})", task_ids)
                conn.execute(f"DELETE FROM io_logs WHERE task_id IN ({ph})", task_ids)
            conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
            conn.commit()
            return len(task_ids)

    def create_tasks_from_manifest(self, project_id: str, manifest: dict, owner_email: str = "cli@local") -> list[int]:
        """
        Create task records from a P3 manifest.
        manifest = {"tasks": [...], "execution_order": [...]}
        Each task has: id, description, detailed_requirements, dependencies, task_type, artifact_requirement
        Returns list of created task IDs.

        Tasks start at t_plan (not step 1) since project-level planning is already done.
        """
        task_ids = []
        # Map manifest task IDs (string) to DB task IDs (integer)
        id_map = {}
        tasks = manifest.get("tasks", [])

        with self.get_connection() as conn:
            for t in tasks:
                manifest_id = t.get("id", "task")
                deps = t.get("dependencies", [])
                task_type = t.get("task_type", "normal")
                prompt = t.get("description", "")
                detailed = t.get("detailed_requirements", "")
                full_prompt = f"{prompt}\n\n{detailed}" if detailed else prompt

                # Resolve string deps to integer IDs (may not exist yet)
                dep_ints = [id_map[d] for d in deps if d in id_map]

                cursor = conn.execute(
                    "INSERT INTO tasks (project_id, prompt, status, dependencies, task_type, owner_email, "
                    "current_step, completed_steps) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (project_id, full_prompt, TaskStatus.PENDING.value,
                     json.dumps(dep_ints), task_type, owner_email,
                     "t_plan", json.dumps([]))
                )
                db_id = cursor.lastrowid
                id_map[manifest_id] = db_id
                task_ids.append(db_id)

            # Second pass: update dependencies now that all IDs are known
            for t in tasks:
                manifest_id = t.get("id", "task")
                deps = t.get("dependencies", [])
                if deps and manifest_id in id_map:
                    dep_ints = [id_map[d] for d in deps if d in id_map]
                    conn.execute(
                        "UPDATE tasks SET dependencies = ? WHERE id = ?",
                        (json.dumps(dep_ints), id_map[manifest_id])
                    )

            conn.commit()
        return task_ids

    def get_ready_tasks(self, project_id: str) -> list[dict]:
        """
        Return tasks for this project that are unblocked:
        - status is 'pending' or 'running'
        - all dependency tasks have status='completed'
        Ordered by: tool tasks first, then by dependency count ASC.
        """
        with self.get_connection() as conn:
            tasks = conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? AND status IN (?, ?)",
                (project_id, TaskStatus.PENDING.value, TaskStatus.RUNNING.value)
            ).fetchall()

            if not tasks:
                return []

            # Get completed task IDs for this project
            completed_ids = {
                row["id"] for row in conn.execute(
                    "SELECT id FROM tasks WHERE project_id = ? AND status = ?",
                    (project_id, TaskStatus.COMPLETED.value)
                ).fetchall()
            }

            ready = []
            for row in tasks:
                task = dict(row)
                deps = json.loads(task.get("dependencies", "[]"))
                # All dependencies must be completed
                if all(d in completed_ids for d in deps):
                    ready.append(task)

            # Sort: tool tasks first, then fewer deps first
            ready.sort(key=lambda t: (0 if t.get("task_type") == "tool" else 1, len(json.loads(t.get("dependencies", "[]")))))
            return ready

    def has_incomplete_tasks(self, project_id: str) -> bool:
        """Check if project has any tasks that aren't completed or failed."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE project_id = ? AND status NOT IN (?, ?)",
                (project_id, TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
            ).fetchone()
            return row["cnt"] > 0

    def has_only_failed_tasks(self, project_id: str) -> bool:
        """Return True if there is >=1 task and ALL are FAILED."""
        with self.get_connection() as conn:
            total = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE project_id = ?",
                (project_id,)
            ).fetchone()["cnt"]
            if total == 0:
                return False
            failed = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE project_id = ? AND status = ?",
                (project_id, TaskStatus.FAILED.value)
            ).fetchone()["cnt"]
            return failed == total

    def has_incomplete_tasks_for_owner(self, owner_email: str) -> bool:
        """Check if user has any incomplete tasks across all their projects."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE owner_email = ? AND status NOT IN (?, ?)",
                (owner_email, TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
            ).fetchone()
            return row["cnt"] > 0

    # ── Meta Orchestrator State ──

    def set_project_meta_state(self, project_id: str, state: str | None):
        """Set the meta_state column on a project.

        Used to gate the scheduler: projects with meta_state='drafting'
        are skipped until the meta conversation finishes and clears it.
        """
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE projects SET meta_state = ? WHERE project_id = ?",
                (state, project_id),
            )
            conn.commit()

    def get_project_meta_state(self, project_id: str) -> str | None:
        """Get the current meta_state for a project."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT meta_state FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return row["meta_state"] if row else None

    def reset_project_step(self, project_id: str, step_id: str):
        """Deprecated: skillflow handles via reject_checkpoint()."""

    def get_projects_awaiting_approval(self, owner_email: str = None) -> list[dict]:
        """Get all projects in waiting_user_approval status."""
        with self.get_connection() as conn:
            if owner_email:
                rows = conn.execute(
                    "SELECT * FROM projects WHERE status = 'waiting_user_approval' AND owner_email = ?",
                    (owner_email,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM projects WHERE status = 'waiting_user_approval'"
                ).fetchall()
            return [dict(r) for r in rows]

    # ── Settings ──

    def get_scheduler_settings(self) -> dict:
        """Get all scheduler settings as a dict."""
        with self.get_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            return {row["key"]: row["value"] for row in rows}

    def set_scheduler_setting(self, key: str, value: str):
        """Upsert a single scheduler setting."""
        with self.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (key, value),
            )
            conn.commit()

    # ── Task Retry & Lifecycle ──

    def retry_task(self, task_id: int) -> bool:
        """Reset a failed task to PENDING, restarting from the first task step."""
        from core.workspace_manager import TASK_STEP_SEQUENCE
        first_step = TASK_STEP_SEQUENCE[0] if TASK_STEP_SEQUENCE else "t_plan"
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = ?, current_step = ?, completed_steps = '[]', "
                "retry_count = COALESCE(retry_count, 0) + 1 "
                "WHERE id = ? AND status = ?",
                (TaskStatus.PENDING.value, first_step, task_id, TaskStatus.FAILED.value)
            )
            conn.commit()
            return cursor.rowcount > 0

    def retry_project(self, project_id: str) -> bool:
        """Reset a failed project and all its failed tasks to pending for retry.

        Uses skillflow's reactivate_run() instead of direct SQL on projects.status.
        Returns True if the project or its tasks were reset, False otherwise.
        """
        from api.dependencies import get_skillflow
        sf = get_skillflow()

        # Reactivate the skillflow run (skillflow owns pipeline status)
        run = sf.get_run_by_project(project_id)
        run_reactivated = run and run["status"] in ("failed", "completed")
        if run_reactivated:
            sf.reactivate_run(run["id"])

        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = ?, current_step = 't_plan', completed_steps = '[]', "
                "retry_count = COALESCE(retry_count, 0) + 1 "
                "WHERE project_id = ? AND status = ?",
                (TaskStatus.PENDING.value, project_id, TaskStatus.FAILED.value)
            )
            conn.commit()
            return run_reactivated or cursor.rowcount > 0

    def is_project_planning_complete(self, project_id: str) -> bool:
        """Check if project planning phase (steps 1_5, 2, 3) is fully complete.

        Reads from skillflow_steps via public API (source of truth).
        Falls back to legacy completed_project_steps for backward compatibility.
        """
        from core.workspace_manager import PROJECT_STEP_SEQUENCE
        import json as _json

        # Primary: check skillflow state via public API
        meta = self.get_project_meta_state(project_id)
        run_id = None
        if meta:
            try:
                ms = _json.loads(meta)
                run_id = ms.get("skillflow_run_id")
            except (_json.JSONDecodeError, ValueError):
                pass

        if run_id:
            try:
                from api.dependencies import get_skillflow
                sf = get_skillflow()
                steps = sf.get_steps(run_id)
                step_status = {s["step_id"]: s["status"] for s in steps}
                if step_status and all(
                    step_status.get(s) == "completed" for s in PROJECT_STEP_SEQUENCE
                ):
                    return True
            except Exception:
                pass

        # Fallback: legacy tracking
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT completed_project_steps FROM projects WHERE project_id = ?",
                (project_id,)
            ).fetchone()
            if not row or not row[0]:
                return False
            completed = _json.loads(row[0])
            return all(s in completed for s in PROJECT_STEP_SEQUENCE)

    def get_planning_pre_done_steps(self, project_id: str) -> list[str]:
        """Return the completed-step list a new task inherits from a fully-planned project."""
        from core.workspace_manager import PROJECT_STEP_SEQUENCE
        return ["1"] + list(PROJECT_STEP_SEQUENCE)

    def set_task_meta_state(self, task_id: int, meta_state: str):
        """Store error details in task meta_state for retrieval by /errors."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET task_meta_state = ? WHERE id = ?",
                (meta_state, task_id)
            )
            conn.commit()

    def get_task_meta_state(self, task_id: int) -> str | None:
        """Retrieve error details from task meta_state."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT task_meta_state FROM tasks WHERE id = ?",
                (task_id,)
            ).fetchone()
            return row[0] if row else None

    def set_completed_project_steps(self, project_id: str, steps: list[str]):
        """Deprecated: skillflow tracks this in skillflow_steps."""

    def increment_tasks_since_update(self, project_id: str):
        """Increment the counter of tasks completed since last arch update."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE projects SET tasks_since_arch_update = COALESCE(tasks_since_arch_update, 0) + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE project_id = ?",
                (project_id,)
            )
            conn.commit()

    def reset_tasks_since_update(self, project_id: str):
        """Reset the counter after a planning refresh."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE projects SET tasks_since_arch_update = 0, "
                "sota_version = COALESCE(sota_version, 1) + 1, "
                "sota_updated_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE project_id = ?",
                (project_id,)
            )
            conn.commit()

    def should_refresh_planning(self, project_id: str, threshold: int = 5) -> bool:
        """Check if project-level planning needs refresh based on task count."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT tasks_since_arch_update FROM projects WHERE project_id = ?",
                (project_id,)
            ).fetchone()
            if not row:
                return False
            return (row["tasks_since_arch_update"] or 0) >= threshold

    # ── Chat history persistence ─────────────────────────────────────

    def save_chat_message(self, project_id: str, role: str, content: str):
        """Persist a chat message to DB."""
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO chat_history (project_id, role, content) VALUES (?, ?, ?)",
                (project_id, role, content[:2000]),
            )

    def get_chat_history(self, project_id: str, limit: int = 50) -> list[dict]:
        """Load recent chat messages for a project."""
        with self.get_connection() as conn:
            rows = conn.execute(
                """SELECT role, content, created_at FROM chat_history
                   WHERE project_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (project_id, limit),
            ).fetchall()
            return [{"role": r["role"], "content": r["content"],
                     "created_at": r["created_at"]} for r in reversed(rows)]

    # ── Session management ──────────────────────────────────────────

    def create_session(self) -> str:
        """Create a new chat session and return its ID."""
        import uuid
        sid = uuid.uuid4().hex[:12]
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions (id) VALUES (?)", (sid,)
            )
            conn.commit()
        return sid

    def link_run_to_session(self, session_id: str, run_id: str):
        """Bind a skillflow run to a chat session."""
        with self.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO session_runs (session_id, run_id) VALUES (?, ?)",
                (session_id, run_id),
            )
            conn.commit()

    def get_session_id_for_run(self, run_id: str) -> str | None:
        """Find the session that owns a given skillflow run."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT session_id FROM session_runs WHERE run_id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
            return row["session_id"] if row else None

    def get_runs_for_session(self, session_id: str) -> list[str]:
        """List all run_ids bound to a session."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT run_id FROM session_runs WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            return [r["run_id"] for r in rows]

    def get_chat_history_by_session(self, session_id: str, limit: int = 100) -> list[dict]:
        """Load recent chat messages for a session (cross-project)."""
        with self.get_connection() as conn:
            rows = conn.execute(
                """SELECT role, content, project_id, created_at FROM chat_history
                   WHERE session_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
            return [{"role": r["role"], "content": r["content"],
                     "project_id": r["project_id"],
                     "created_at": r["created_at"]} for r in reversed(rows)]

    def save_chat_message_with_session(self, session_id: str, project_id: str,
                                       role: str, content: str):
        """Persist a chat message with session scope."""
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO chat_history (session_id, project_id, role, content) "
                "VALUES (?, ?, ?, ?)",
                (session_id, project_id, role, content[:2000]),
            )
            conn.commit()

    def _migrate_task_steps(self, conn):
        """Migrate mid-flight task steps from old sequence (t_sota, t_design) to new (t_plan)."""
        tasks = conn.execute(
            "SELECT id, current_step, completed_steps FROM tasks"
        ).fetchall()
        for task in tasks:
            current = task["current_step"]
            completed = json.loads(task["completed_steps"])
            changed = False

            # Remap current step
            if current in ("t_sota", "t_design"):
                current = "t_plan"
                changed = True

            # Remap completed steps
            new_completed = []
            for s in completed:
                if s in ("t_sota", "t_design"):
                    if "t_plan" not in new_completed:
                        new_completed.append("t_plan")
                    changed = True
                else:
                    new_completed.append(s)

            if changed:
                conn.execute(
                    "UPDATE tasks SET current_step = ?, completed_steps = ? WHERE id = ?",
                    (current, json.dumps(new_completed), task["id"])
                )