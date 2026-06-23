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

            # Structural migrations run FIRST so a legacy project-based DB is
            # renamed to the run-based schema before the CREATE IF NOT EXISTS
            # below — otherwise they would create an empty parallel `runs` table
            # alongside the real (still-named `projects`) one.
            self._apply_versioned_migrations(conn)

            # ── Create tables (current schema) in dependency order ──
            # `runs` — one row per skillflow config run. DPE "projects" are simply
            # runs of the dpe_default_v2 config. project_id stays the primary key
            # and the cross-DB join key into skillflow_runs.project_id.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    config_name TEXT NOT NULL DEFAULT 'dpe_default_v2',
                    status TEXT NOT NULL DEFAULT 'planning',
                    current_project_step TEXT DEFAULT NULL,
                    priority INTEGER DEFAULT 0,
                    owner_email TEXT DEFAULT 'cli@local',
                    meta_state TEXT DEFAULT NULL,
                    repo_type TEXT DEFAULT 'new',
                    repo_path TEXT DEFAULT NULL,
                    repo_url TEXT DEFAULT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_config ON runs(config_name)")
            # DPE-specific run state, extracted out of `runs` so the run row stays
            # config-agnostic. 1:1 with runs(project_id).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dpe_run_state (
                    run_key TEXT PRIMARY KEY,
                    brief TEXT DEFAULT NULL,
                    completed_project_steps TEXT DEFAULT '[]',
                    sota_version INTEGER DEFAULT 1,
                    sota_updated_at DATETIME DEFAULT NULL,
                    tasks_since_arch_update INTEGER DEFAULT 0,
                    FOREIGN KEY (run_key) REFERENCES runs(project_id)
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
                    manifest_key TEXT DEFAULT NULL,
                    owner_email TEXT DEFAULT 'cli@local',
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    last_error TEXT DEFAULT NULL,
                    FOREIGN KEY (project_id) REFERENCES runs(project_id)
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
            # Sentinel CLI user
            conn.execute(
                "INSERT OR IGNORE INTO users (email, display_name, source) VALUES (?, ?, ?)",
                ("cli@local", "CLI User", "cli"),
            )

            # Idempotent column adds for pre-existing tables that predate a
            # column. DPE-state columns (brief / completed_project_steps / sota_* /
            # tasks_since_arch_update) are NOT here — they live in dpe_run_state.
            migrations = [
                "ALTER TABLE tasks ADD COLUMN current_step TEXT DEFAULT '1'",
                "ALTER TABLE tasks ADD COLUMN completed_steps TEXT DEFAULT '[]'",
                "ALTER TABLE tasks ADD COLUMN current_subtask TEXT DEFAULT NULL",
                "ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 0",
                "ALTER TABLE tasks ADD COLUMN step_locked INTEGER DEFAULT 0",
                "ALTER TABLE tasks ADD COLUMN dependencies TEXT DEFAULT '[]'",
                "ALTER TABLE tasks ADD COLUMN task_type TEXT DEFAULT 'normal'",
                "ALTER TABLE tasks ADD COLUMN manifest_key TEXT DEFAULT NULL",
                "ALTER TABLE tasks ADD COLUMN owner_email TEXT DEFAULT 'cli@local'",
                "ALTER TABLE tasks ADD COLUMN retry_count INTEGER DEFAULT 0",
                "ALTER TABLE tasks ADD COLUMN max_retries INTEGER DEFAULT 3",
                "ALTER TABLE runs ADD COLUMN config_name TEXT NOT NULL DEFAULT 'dpe_default_v2'",
                "ALTER TABLE runs ADD COLUMN status TEXT NOT NULL DEFAULT 'planning'",
                "ALTER TABLE runs ADD COLUMN current_project_step TEXT DEFAULT NULL",
                "ALTER TABLE runs ADD COLUMN priority INTEGER DEFAULT 0",
                "ALTER TABLE runs ADD COLUMN owner_email TEXT DEFAULT 'cli@local'",
                "ALTER TABLE runs ADD COLUMN meta_state TEXT DEFAULT NULL",
                "ALTER TABLE runs ADD COLUMN repo_type TEXT DEFAULT 'new'",
                "ALTER TABLE runs ADD COLUMN repo_path TEXT DEFAULT NULL",
                "ALTER TABLE runs ADD COLUMN repo_url TEXT DEFAULT NULL",
            ]
            for sql in migrations:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists

            # Migrate mid-flight task steps from old sequence to new sequence
            self._migrate_task_steps(conn)

            # Migrate existing runs that predate the status column
            conn.execute(
                "UPDATE runs SET status = 'executing' "
                "WHERE status = 'planning' AND project_id IN "
                "(SELECT DISTINCT project_id FROM tasks)"
            )

            # Idempotent migration — add FK: tasks.project_id → runs(project_id)
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

    # ── Versioned migration runner ─────────────────────────────────────
    #
    # The CREATE TABLE IF NOT EXISTS / ALTER ADD COLUMN block in _init_db is
    # idempotent and represents the legacy schema (version 0). Structural
    # migrations that CANNOT be expressed idempotently (RENAME, table rebuild,
    # column drop) are registered here as numbered steps, recorded in the
    # schema_migrations table, and applied exactly once. Each entry is
    # (version:int, name:str, fn(self, conn)). Append new migrations in order.
    _VERSIONED_MIGRATIONS: list = []

    def _apply_versioned_migrations(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        applied = {
            r["version"]
            for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        # Record the legacy idempotent schema as version 0.
        if 0 not in applied:
            conn.execute("INSERT INTO schema_migrations (version) VALUES (0)")
            applied.add(0)

        pending = [m for m in self._VERSIONED_MIGRATIONS if m[0] not in applied]
        if not pending:
            return
        # Back up once before any structural migration — but only for a DB that
        # already has content (skip brand-new databases, which have nothing to
        # lose and would otherwise litter empty .bak files).
        has_content = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name NOT IN ('schema_migrations', 'sqlite_sequence')"
        ).fetchone()[0]
        if has_content:
            self._backup_db(f"premigration-v{pending[0][0]}")
        for version, _name, fn in pending:
            fn(self, conn)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
            )

    def _backup_db(self, suffix: str) -> str:
        """Consistent online backup of the live DB (WAL-safe) to a sibling file.

        Uses a fresh source connection so the backup does not depend on the
        in-flight migration transaction state.
        """
        backup_path = f"{self.db_path}.{suffix}.bak"
        src = sqlite3.connect(self.db_path)
        dest = sqlite3.connect(backup_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        return backup_path

    # DPE-specific columns that move from the run row into dpe_run_state.
    _DPE_STATE_COLUMNS = (
        "brief", "completed_project_steps",
        "sota_version", "sota_updated_at", "tasks_since_arch_update",
    )

    def _mig_001_projects_to_runs(self, conn):
        """Schema v1 — generalize the project-based store into the run-based one.

        Renames ``projects`` → ``runs``, tags every existing row with
        ``config_name='dpe_default_v2'``, and extracts the DPE-only columns into a
        separate ``dpe_run_state`` table so the run row is config-agnostic.

        Re-entrant: each step is guarded so a partial/interrupted migration
        completes cleanly on the next open. A brand-new DB (no ``projects`` table)
        is a no-op — the CREATE block in _init_db builds the current schema.
        """
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "projects" in tables and "runs" not in tables:
            conn.execute("ALTER TABLE projects RENAME TO runs")  # FK refs auto-update
            tables.discard("projects")
            tables.add("runs")
        if "runs" not in tables:
            return  # fresh DB — nothing to transform

        runs_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "config_name" not in runs_cols:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN config_name TEXT NOT NULL "
                "DEFAULT 'dpe_default_v2'"
            )
            runs_cols.add("config_name")
        conn.execute(
            "UPDATE runs SET config_name = 'dpe_default_v2' "
            "WHERE config_name IS NULL OR config_name = ''"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_config ON runs(config_name)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS dpe_run_state (
                run_key TEXT PRIMARY KEY,
                brief TEXT DEFAULT NULL,
                completed_project_steps TEXT DEFAULT '[]',
                sota_version INTEGER DEFAULT 1,
                sota_updated_at DATETIME DEFAULT NULL,
                tasks_since_arch_update INTEGER DEFAULT 0,
                FOREIGN KEY (run_key) REFERENCES runs(project_id)
            )
        """)
        dpe_cols = [c for c in self._DPE_STATE_COLUMNS if c in runs_cols]
        if dpe_cols:
            sel = ", ".join(["project_id"] + dpe_cols)
            ins = ", ".join(["run_key"] + dpe_cols)
            conn.execute(
                f"INSERT OR IGNORE INTO dpe_run_state ({ins}) SELECT {sel} FROM runs"
            )
            for c in dpe_cols:
                try:
                    conn.execute(f"ALTER TABLE runs DROP COLUMN {c}")
                except sqlite3.OperationalError:
                    pass

    def _migrate_tasks_fk(self, conn):
        """
        Migrate tasks table to add FOREIGN KEY (project_id) REFERENCES runs(project_id).
        SQLite doesn't support ALTER TABLE ADD CONSTRAINT, so we must recreate the table.
        This is idempotent: it checks whether the FK already exists first.
        """
        # Check if FK already exists (points at runs; SQLite auto-updates the
        # reference name when the projects→runs rename migration runs).
        fk_rows = conn.execute("PRAGMA foreign_key_list(tasks)").fetchall()
        has_project_fk = any(
            row["table"] == "runs" and row["from"] == "project_id"
            for row in fk_rows
        )
        if has_project_fk:
            return  # already migrated

        # Delete orphan tasks left behind by non-cascade run deletes.
        # Re-creating empty run shells for them is wrong — the run
        # was deleted intentionally and has no brief/settings/context.
        deleted = conn.execute(
            "DELETE FROM tasks "
            "WHERE project_id NOT IN (SELECT project_id FROM runs)"
        )
        if deleted.rowcount:
            import logging
            logging.getLogger("aitelier").warning(
                "_migrate_tasks_fk: removed %d orphan task(s) with no matching project",
                deleted.rowcount
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
                FOREIGN KEY (project_id) REFERENCES runs(project_id)
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

    def complete_task(self, task_id: int, last_step: str = "t_impl"):
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

    # Columns surfaced from dpe_run_state on the run dict (with safe defaults)
    # so consumers that read project["brief"]/["completed_project_steps"] keep
    # working after the DPE-state extraction.
    _RUN_SELECT = (
        "SELECT r.*, "
        "d.brief AS brief, "
        "COALESCE(d.completed_project_steps, '[]') AS completed_project_steps, "
        "COALESCE(d.sota_version, 1) AS sota_version, "
        "d.sota_updated_at AS sota_updated_at, "
        "COALESCE(d.tasks_since_arch_update, 0) AS tasks_since_arch_update "
        "FROM runs r LEFT JOIN dpe_run_state d ON d.run_key = r.project_id"
    )

    def ensure_project(self, project_id: str, name: str = None, owner_email: str = "cli@local",
                       repo_type: str = "new", repo_path: str = None, repo_url: str = None,
                       config_name: str = "dpe_default_v2") -> dict:
        """
        Idempotently create a run row if it does not exist.
        Returns the run dict (existing or newly created).
        """
        if name is None:
            name = project_id.replace("-", " ").replace("_", " ").title()
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT project_id FROM runs WHERE project_id = ?", (project_id,)
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO runs (project_id, name, config_name, owner_email, "
                    "repo_type, repo_path, repo_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (project_id, name, config_name, owner_email, repo_type, repo_path, repo_url)
                )
                conn.commit()
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> dict | None:
        """Return a single run row (with DPE state joined), or None."""
        with self.get_connection() as conn:
            row = conn.execute(
                f"{self._RUN_SELECT} WHERE r.project_id = ?", (project_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_repo_info(self, project_id: str) -> dict:
        """Return repo_type, repo_path, repo_url for a run."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT repo_type, repo_path, repo_url FROM runs WHERE project_id = ?",
                (project_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Project {project_id} not found")
            return {
                "repo_type": row["repo_type"] or "new",
                "repo_path": row["repo_path"],
                "repo_url": row["repo_url"],
            }

    def delete_project(self, project_id: str) -> bool:
        """Delete a run row. Does NOT delete tasks or workspace files."""
        with self.get_connection() as conn:
            conn.execute("DELETE FROM dpe_run_state WHERE run_key = ?", (project_id,))
            cursor = conn.execute(
                "DELETE FROM runs WHERE project_id = ?", (project_id,)
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
            conn.execute("DELETE FROM dpe_run_state WHERE run_key = ?", (project_id,))
            cursor = conn.execute("DELETE FROM runs WHERE project_id = ?", (project_id,))
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
            except Exception as e:
                import logging
                logging.getLogger("aitelier").warning(
                    "delete_project_cascade: skillflow cleanup failed for '%s': %s",
                    project_id, e
                )

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

    @staticmethod
    def _upsert_dpe_state(conn, project_id: str, **fields):
        """Insert-or-update DPE-specific run state (brief, completed steps, sota…)
        in the dpe_run_state side table, keyed by the run's project_id."""
        if not fields:
            return
        cols = list(fields.keys())
        col_list = ", ".join(["run_key"] + cols)
        placeholders = ", ".join(["?"] * (len(cols) + 1))
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols)
        conn.execute(
            f"INSERT INTO dpe_run_state ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT(run_key) DO UPDATE SET {updates}",
            [project_id] + [fields[c] for c in cols],
        )

    def update_project(self, project_id: str, name: str = None,
                       brief: str = None, priority: int = None,
                       status: str = None,
                       current_project_step: str = None,
                       completed_project_steps: str = None) -> bool:
        """Update run fields. Only sets non-None values.

        Run-level fields (name/priority/status/current_project_step) update the
        `runs` row; DPE-specific fields (brief, completed_project_steps) are
        written to the dpe_run_state side table.
        """
        updates = []
        params = []
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if priority is not None:
            updates.append("priority = ?")
            params.append(priority)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if current_project_step is not None:
            updates.append("current_project_step = ?")
            params.append(current_project_step)

        dpe_fields = {}
        if brief is not None:
            dpe_fields["brief"] = brief
        if completed_project_steps is not None:
            dpe_fields["completed_project_steps"] = completed_project_steps

        if not updates and not dpe_fields:
            return False
        changed = False
        with self.get_connection() as conn:
            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                cursor = conn.execute(
                    f"UPDATE runs SET {', '.join(updates)} WHERE project_id = ?",
                    params + [project_id]
                )
                changed = cursor.rowcount > 0
            if dpe_fields:
                self._upsert_dpe_state(conn, project_id, **dpe_fields)
                changed = True
            conn.commit()
            return changed

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
                        p.config_name,
                        p.status,
                        p.current_project_step,
                        p.priority,
                        p.created_at,
                        p.updated_at,
                        p.owner_email,
                        p.repo_type,
                        p.repo_path,
                        p.repo_url,
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
                    FROM runs p
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
                        p.config_name,
                        p.status,
                        p.current_project_step,
                        p.priority,
                        p.created_at,
                        p.updated_at,
                        p.owner_email,
                        p.repo_type,
                        p.repo_path,
                        p.repo_url,
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
                    FROM runs p
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
        from api.dependencies import get_skillflow, get_config_registry
        sf = get_skillflow()
        # Only consider configs whose runs the polling scheduler owns. Butler-
        # driven configs (meta_conversation, skill_converter) are excluded so the
        # scheduler never grabs one and runs it as if it were a DPE build.
        try:
            owned = {m.config_name for m in get_config_registry().list() if m.scheduler_owned}
        except Exception:
            owned = {"dpe_default_v2"}
        if not owned:
            return None
        owned_ph = ",".join("?" * len(owned))
        owned_clause = f"config_name IN ({owned_ph})"

        # Collect project_ids from active skillflow runs of owned configs.
        active_ids: set[str] = set()
        for status in ('running', 'paused'):
            for r in sf.list_runs(status=status):
                pid = r.get("project_id")
                if pid and r.get("graph_name") in owned:
                    active_ids.add(pid)

        # Collect project_ids whose owned-config runs are terminal
        # (completed/failed). Exclude them from the task-subquery fallback so a
        # leftover running task on a finished run doesn't starve active ones.
        terminal_ids: set[str] = set()
        for status in ('completed', 'failed'):
            for r in sf.list_runs(status=status):
                pid = r.get("project_id")
                if pid and r.get("graph_name") in owned:
                    terminal_ids.add(pid)

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
            planning_guard = (
                "(status = 'planning'"
                " AND project_id IN (SELECT run_key FROM dpe_run_state"
                "                    WHERE brief IS NOT NULL AND brief != '')"
                " AND (meta_state IS NULL OR meta_state != 'drafting'))"
            )
            # Task subquery: projects with pending/running tasks, guarded against
            # terminal DPE runs whose leftover tasks would starve active projects.
            if terminal_ids:
                term_ph = ",".join("?" * len(terminal_ids))
                task_clause = (
                    f"(project_id IN ("
                    f"  SELECT DISTINCT t.project_id FROM tasks t"
                    f"  WHERE t.status IN ('pending', 'running')"
                    f") AND project_id NOT IN ({term_ph}))"
                )
            else:
                task_clause = (
                    "project_id IN ("
                    "  SELECT DISTINCT t.project_id FROM tasks t"
                    "  WHERE t.status IN ('pending', 'running')"
                    ")"
                )
            if active_ids:
                placeholders = ",".join("?" * len(active_ids))
                if owner_email:
                    row = conn.execute(f"""
                        SELECT * FROM runs
                        WHERE (project_id IN ({placeholders})
                               OR {task_clause}
                               OR {planning_guard})
                          AND {owned_clause}
                          AND owner_email = ?
                        ORDER BY {ordering}
                        LIMIT 1
                    """, (*active_ids, *terminal_ids, *owned, owner_email)).fetchone()
                else:
                    row = conn.execute(f"""
                        SELECT * FROM runs
                        WHERE (project_id IN ({placeholders})
                               OR {task_clause}
                               OR {planning_guard})
                          AND {owned_clause}
                        ORDER BY {ordering}
                        LIMIT 1
                    """, (*active_ids, *terminal_ids, *owned)).fetchone()
            # Fallback: no active skillflow runs OR none matched in the local DB
            # (e.g., tests use an isolated DB while skillflow uses production DB).
            if row is None:
                # Gate: skip projects whose meta conversation hasn't finished
                # (meta_state='drafting'). Same as in _get_or_create_skillflow_run.
                drafting_guard = "AND (meta_state IS NULL OR meta_state != 'drafting')"
                if owner_email:
                    row = conn.execute(f"""
                        SELECT * FROM runs
                        WHERE (status IN ({','.join('?'*len(STATUSES))})
                               OR {task_clause})
                          AND {owned_clause}
                          AND owner_email = ?
                          {drafting_guard}
                        ORDER BY {ordering}
                        LIMIT 1
                    """, (*STATUSES, *terminal_ids, *owned, owner_email)).fetchone()
                else:
                    row = conn.execute(f"""
                        SELECT * FROM runs
                        WHERE (status IN ({','.join('?'*len(STATUSES))})
                               OR {task_clause})
                          AND {owned_clause}
                          {drafting_guard}
                        ORDER BY {ordering}
                        LIMIT 1
                    """, (*STATUSES, *terminal_ids, *owned)).fetchone()
            return dict(row) if row else None

    def advance_project_step(self, project_id: str) -> str | None:
        """Deprecated: skillflow owns pipeline progression via advance_run()."""
        return None

    def set_project_brief(self, project_id: str, brief: str):
        """Store the project brief markdown (DPE run state)."""
        with self.get_connection() as conn:
            self._upsert_dpe_state(conn, project_id, brief=brief)
            conn.execute(
                "UPDATE runs SET updated_at = CURRENT_TIMESTAMP WHERE project_id = ?",
                (project_id,)
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
                    "current_step, completed_steps, manifest_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (project_id, full_prompt, TaskStatus.PENDING.value,
                     json.dumps(dep_ints), task_type, owner_email,
                     "t_plan", json.dumps([]), str(manifest_id))
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

    def sync_tasks_from_manifest(self, project_id: str, manifest: dict,
                                 owner_email: str = "cli@local") -> list[int]:
        """Merge a (possibly partial) P3 manifest into the tasks table, PRESERVING
        completed tasks.

        On a goal-loop re-decomposition the PM may emit only the new/changed task
        cards (e.g. a single remediation task) while earlier tasks are already
        done. A destructive delete-all + recreate would wipe the completed history
        from the UI (the symptom: "old tasks disappear, only the new one left").
        Instead: keep completed tasks (matched by ``manifest_key``), drop only the
        incomplete ones (they are re-derived from the manifest), and create only
        the manifest tasks that aren't already completed. New tasks' dependencies
        on already-completed tasks resolve to the preserved rows.

        Returns the list of newly-created task IDs.
        """
        tasks = manifest.get("tasks", [])
        with self.get_connection() as conn:
            existing = conn.execute(
                "SELECT id, status, manifest_key FROM tasks WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            # manifest_key → db_id for completed tasks (preserved across resync)
            completed_key_to_id = {
                r["manifest_key"]: r["id"] for r in existing
                if r["status"] == TaskStatus.COMPLETED.value and r["manifest_key"]
            }
            # Drop only incomplete tasks (+ their children); completed rows stay.
            incomplete_ids = [r["id"] for r in existing
                              if r["status"] != TaskStatus.COMPLETED.value]
            if incomplete_ids:
                ph = ",".join("?" * len(incomplete_ids))
                conn.execute(f"DELETE FROM subtasks WHERE task_id IN ({ph})", incomplete_ids)
                conn.execute(f"DELETE FROM io_logs WHERE task_id IN ({ph})", incomplete_ids)
                conn.execute(f"DELETE FROM tasks WHERE id IN ({ph})", incomplete_ids)

            # Seed id_map with preserved completed tasks so new tasks' deps on them
            # resolve to the existing rows.
            id_map = dict(completed_key_to_id)
            created: list[int] = []
            for t in tasks:
                manifest_id = str(t.get("id", "task"))
                if manifest_id in completed_key_to_id:
                    continue  # already done — preserve, never recreate
                deps = t.get("dependencies", [])
                task_type = t.get("task_type", "normal")
                prompt = t.get("description", "")
                detailed = t.get("detailed_requirements", "")
                full_prompt = f"{prompt}\n\n{detailed}" if detailed else prompt
                dep_ints = [id_map[d] for d in deps if d in id_map]
                cur = conn.execute(
                    "INSERT INTO tasks (project_id, prompt, status, dependencies, task_type, "
                    "owner_email, current_step, completed_steps, manifest_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (project_id, full_prompt, TaskStatus.PENDING.value,
                     json.dumps(dep_ints), task_type, owner_email,
                     "t_plan", json.dumps([]), manifest_id),
                )
                db_id = cur.lastrowid
                id_map[manifest_id] = db_id
                created.append(db_id)
            # Second pass: resolve deps now that all new IDs are known.
            for t in tasks:
                manifest_id = str(t.get("id", "task"))
                if manifest_id in completed_key_to_id:
                    continue
                deps = t.get("dependencies", [])
                if deps and manifest_id in id_map:
                    dep_ints = [id_map[d] for d in deps if d in id_map]
                    conn.execute("UPDATE tasks SET dependencies = ? WHERE id = ?",
                                 (json.dumps(dep_ints), id_map[manifest_id]))
            conn.commit()
        return created

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

    def has_active_runs_for_owner(self, owner_email: str) -> bool:
        """True if the owner has any running/paused skillflow run (any config).

        Used by the web scheduler reaper so a task-less config run (no DPE tasks
        rows) isn't reaped mid-flight — owners with an active run still count as
        having work even when has_incomplete_tasks_for_owner is False.
        """
        try:
            from api.dependencies import get_skillflow
            sf = get_skillflow()
        except Exception:
            return False
        active_pids = {
            r["project_id"]
            for status in ("running", "paused")
            for r in sf.list_runs(status=status)
            if r.get("project_id")
        }
        if not active_pids:
            return False
        with self.get_connection() as conn:
            ph = ",".join("?" * len(active_pids))
            row = conn.execute(
                f"SELECT 1 FROM runs WHERE owner_email = ? AND project_id IN ({ph}) LIMIT 1",
                (owner_email, *active_pids)
            ).fetchone()
            return row is not None

    # ── Meta Orchestrator State ──

    def set_project_meta_state(self, project_id: str, state: str | None):
        """Set the meta_state column on a project.

        Used to gate the scheduler: projects with meta_state='drafting'
        are skipped until the meta conversation finishes and clears it.
        """
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE runs SET meta_state = ? WHERE project_id = ?",
                (state, project_id),
            )
            conn.commit()

    def get_project_meta_state(self, project_id: str) -> str | None:
        """Get the current meta_state for a project."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT meta_state FROM runs WHERE project_id = ?",
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
                    "SELECT * FROM runs WHERE status = 'waiting_user_approval' AND owner_email = ?",
                    (owner_email,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE status = 'waiting_user_approval'"
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
                "SELECT completed_project_steps FROM dpe_run_state WHERE run_key = ?",
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
            cur = conn.execute(
                "UPDATE dpe_run_state "
                "SET tasks_since_arch_update = COALESCE(tasks_since_arch_update, 0) + 1 "
                "WHERE run_key = ?",
                (project_id,)
            )
            if cur.rowcount == 0:
                self._upsert_dpe_state(conn, project_id, tasks_since_arch_update=1)
            conn.execute(
                "UPDATE runs SET updated_at = CURRENT_TIMESTAMP WHERE project_id = ?",
                (project_id,)
            )
            conn.commit()

    def reset_tasks_since_update(self, project_id: str):
        """Reset the counter after a planning refresh; bump the SOTA version."""
        with self.get_connection() as conn:
            cur = conn.execute(
                "UPDATE dpe_run_state "
                "SET tasks_since_arch_update = 0, "
                "sota_version = COALESCE(sota_version, 1) + 1, "
                "sota_updated_at = CURRENT_TIMESTAMP "
                "WHERE run_key = ?",
                (project_id,)
            )
            if cur.rowcount == 0:
                self._upsert_dpe_state(
                    conn, project_id, tasks_since_arch_update=0, sota_version=2
                )
            conn.execute(
                "UPDATE runs SET updated_at = CURRENT_TIMESTAMP WHERE project_id = ?",
                (project_id,)
            )
            conn.commit()

    def should_refresh_planning(self, project_id: str, threshold: int = 5) -> bool:
        """Check if run-level planning needs refresh based on task count."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT tasks_since_arch_update FROM dpe_run_state WHERE run_key = ?",
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

    def list_chat_sessions(self, project_id: str | None = None, limit: int = 20) -> list[dict]:
        """List chat sessions with message count and last message preview.

        Args:
            project_id: Optional project filter. None returns sessions across all projects.
            limit: Maximum number of sessions to return (default 20).

        Returns:
            List of dicts, each with keys: session_id, project_id, message_count,
            first_message, last_message, updated_at. Ordered by updated_at DESC.
            ``first_message`` is the session's opening user message (the question),
            used as the dropdown title. Only returns sessions with message_count > 0.
        """
        with self.get_connection() as conn:
            if project_id:
                rows = conn.execute(
                    """SELECT s.id AS session_id,
                              ch.project_id,
                              COUNT(ch.id) AS message_count,
                              (SELECT content FROM chat_history
                               WHERE session_id = s.id AND role = 'user'
                               ORDER BY id ASC LIMIT 1) AS first_message,
                              (SELECT content FROM chat_history
                               WHERE session_id = s.id
                               ORDER BY id DESC LIMIT 1) AS last_message,
                              MAX(ch.created_at) AS updated_at,
                              MAX(ch.id) AS last_msg_id
                       FROM sessions s
                       JOIN chat_history ch ON ch.session_id = s.id
                       WHERE ch.project_id = ?
                       GROUP BY s.id
                       HAVING message_count > 0
                       ORDER BY last_msg_id DESC
                       LIMIT ?""",
                    (project_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT s.id AS session_id,
                              MAX(ch.project_id) AS project_id,
                              COUNT(ch.id) AS message_count,
                              (SELECT content FROM chat_history
                               WHERE session_id = s.id AND role = 'user'
                               ORDER BY id ASC LIMIT 1) AS first_message,
                              (SELECT content FROM chat_history
                               WHERE session_id = s.id
                               ORDER BY id DESC LIMIT 1) AS last_message,
                              MAX(ch.created_at) AS updated_at,
                              MAX(ch.id) AS last_msg_id
                       FROM sessions s
                       JOIN chat_history ch ON ch.session_id = s.id
                       GROUP BY s.id
                       HAVING message_count > 0
                       ORDER BY last_msg_id DESC
                       LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

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


# Register structural migrations (applied once, in order, recorded in
# schema_migrations). Defined here so the migration functions are bound.
DBManager._VERSIONED_MIGRATIONS = [
    (1, "projects_to_runs", DBManager._mig_001_projects_to_runs),
]
