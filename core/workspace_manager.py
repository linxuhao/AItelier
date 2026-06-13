# core/workspace_manager.py
# [说明] 物理隔离的工作区管理器。实现物理目录监狱 (Jail)、Git 事件溯源 (Event Sourcing) 以及跨语言运行时初始化。
# [变更] step_id 从 int 改为 str 以支持 Step 1.5 等非整数步骤；
#        目录创建适配六步法完整流程。
#        分离 DPS workspace (Inbox/Outbox) 与 Project workspace (代码仓库)。
#        Outbox_Draft / Outbox_Final / Trace 路径统一委托给 skillflow WorkspaceManager
#        （带 config_name 前缀），Inbox 保留在顶层（AItelier 概念）。

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

# 六步法步骤序列（string ID）
STEP_SEQUENCE = ["1", "2", "3", "5"]

# Project-level planning steps (run once per project)
# Step 1 (Researcher, was 1_5) — research + SOTA
PROJECT_STEP_SEQUENCE = ["1", "2", "3"]

# Task-level execution steps (run per task)
# t_sota + t_design merged into t_plan
TASK_STEP_SEQUENCE = ["t_plan", "t_impl", "t_verify"]

# Project-level final verification
FINAL_STEP = "5"

# Default DPE graph name — matches the config name in configs/dpe_default.yaml
DPE_GRAPH_NAME = "dpe_default_v2"

# Default base paths
_DPS_BASE = str(Path.home() / ".AItelier" / "workspaces")
_PROJECTS_BASE = str(Path.home() / ".AItelier" / "projects")


class WorkspaceManager:
    """
    负责 DPE 工作区的物理生命周期管理。
    强制执行 Path Traversal 防御与 Git 版本状态锚定。

    两层隔离:
    - DPS workspace (base_path): Inbox/Outbox/Trace 等管线状态, 独立 git 事件溯源
    - Project workspace (code_path): 代码仓库, 独立 git 管理

    Outbox_Draft / Outbox_Final / Trace 路径通过 skillflow WorkspaceManager 构建
    （带 graph_name 前缀如 dpe_default_v2/），Inbox 保留在项目根层级。
    """
    def __init__(self, base_path: str = None):
        if base_path is None:
            base_path = _DPS_BASE
        self.base_path = Path(base_path).resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.projects_base = Path(_PROJECTS_BASE).resolve()
        self.projects_base.mkdir(parents=True, exist_ok=True)

    def _get_secure_path(self, project_id: str) -> Path:
        """
        [物理监狱] 强制使用 resolve() 校验，防止通过 ../ 进行目录穿越攻击。
        返回 DPS workspace 路径。
        """
        project_path = (self.base_path / project_id).resolve()
        if not str(project_path).startswith(str(self.base_path)):
            raise PermissionError(f"Path traversal attempt blocked: {project_id}")
        return project_path

    # ── Path helpers ──────────────────────────────────────────────────
    # Use the same path convention as skillflow (graph_name prefix) but
    # always resolve against THIS instance's base_path.  We do NOT delegate
    # to skillflow's WorkspaceManager because it may have a different base_path
    # (e.g. in tests where aitelier WS uses a tmp_path while skillflow WS
    # was initialised with the global production path).

    def _draft_dir(self, project_id: str, step_id: str,
                   graph_name: str = DPE_GRAPH_NAME) -> Path:
        """Get tmp staging directory for a step ({step_id}.tmp)."""
        return self._get_secure_path(project_id) / graph_name / f"{step_id}.tmp"

    def _final_dir(self, project_id: str, step_id: str,
                   graph_name: str = DPE_GRAPH_NAME) -> Path:
        """Get promoted step directory ({step_id}/)."""
        return self._get_secure_path(project_id) / graph_name / step_id

    # ── Draft / Final (graph_name-prefixed, skillflow layout) ──────────

    def setup_workspace(self, project_id: str, repo_type: str = "new",
                        repo_path: str = None, repo_url: str = None,
                        graph_name: str = DPE_GRAPH_NAME):
        """
        初始化 DPS 物理结构与 Project 代码仓库。

        :param project_id: 项目唯一标识
        :param repo_type: 'new' | 'existing' | 'clone'
        :param repo_path: 代码仓库本地路径 (existing 模式必填, 其他模式自动生成)
        :param repo_url: 远程仓库 URL (clone 模式必填)
        :param graph_name: skillflow graph config name
        """
        dps_path = self._get_secure_path(project_id)
        dps_path.mkdir(parents=True, exist_ok=True)

        # 初始化 DPS Git 仓库 (事件溯源基座)
        if not (dps_path / ".git").exists():
            subprocess.run(["git", "init"], cwd=dps_path, check=True, capture_output=True)
            (dps_path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=dps_path, check=True)
            subprocess.run(["git", "commit", "-m", "Initial commit: DPS Workspace Created"], cwd=dps_path)

        # 创建 DPS 目录结构
        (dps_path / "Global_Mount").mkdir(exist_ok=True)

        # Draft/Final dirs in graph_name/ (skillflow layout).
        # Inbox dirs are no longer created — skillflow deprecated them
        # in favor of {step_id}.tmp/ → {step_id}/. AItelier now follows
        # the same convention.
        active_steps = list(dict.fromkeys(PROJECT_STEP_SEQUENCE + TASK_STEP_SEQUENCE + [FINAL_STEP]))
        if FINAL_STEP not in active_steps:
            active_steps.append(FINAL_STEP)
        for step_id in active_steps:
            self._draft_dir(project_id, step_id, graph_name).mkdir(parents=True, exist_ok=True)
            self._final_dir(project_id, step_id, graph_name).mkdir(parents=True, exist_ok=True)

        # 初始化 Project 代码仓库
        if repo_type == "new":
            code_path = self.projects_base / project_id
            code_path.mkdir(parents=True, exist_ok=True)
            if not (code_path / ".git").exists():
                subprocess.run(["git", "init"], cwd=code_path, check=True, capture_output=True)
                (code_path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=code_path, check=True)
                subprocess.run(["git", "commit", "-m", "Initial commit: Project workspace"], cwd=code_path)

        elif repo_type == "existing":
            if not repo_path:
                raise ValueError("repo_path is required for repo_type='existing'")
            code_path = Path(repo_path).resolve()
            if not code_path.exists():
                raise FileNotFoundError(f"Repo path does not exist: {repo_path}")
            if not (code_path / ".git").exists():
                raise ValueError(f"Path is not a git repository: {repo_path}")

        elif repo_type == "clone":
            if not repo_url:
                raise ValueError("repo_url is required for repo_type='clone'")
            code_path = self.projects_base / project_id
            if not code_path.exists():
                subprocess.run(
                    ["git", "clone", repo_url, str(code_path)],
                    check=True, capture_output=True
                )
            elif not (code_path / ".git").exists():
                raise ValueError(f"Clone target exists but is not a git repo: {code_path}")

        else:
            raise ValueError(f"Unknown repo_type: {repo_type}")

    # ── Draft write (graph_name-prefixed, skillflow layout) ───────────

    @staticmethod
    def _sanitize_filename(filename: str, content: str = "") -> str:
        """
        清理 LLM 输出的文件名。
        - 去除尾部多余点号 (LLM 常输出 "step1_goals." 而非 "step1_goals.json")
        - 检测内容类型，自动补充缺失的扩展名
        - 去除首尾空白
        """
        filename = filename.strip()
        had_trailing_dot = filename.endswith('.') and not filename.endswith('..')
        while filename.endswith('.') and not filename.endswith('..'):
            filename = filename[:-1].rstrip()
        if had_trailing_dot and '.' not in Path(filename).name:
            stripped = content.strip()
            if stripped.startswith('{') or stripped.startswith('['):
                filename += '.json'
            elif stripped.startswith('#') or stripped.startswith('<') or '\n##' in stripped[:200]:
                filename += '.md'
        return filename

    def write_draft(self, project_id: str, step_id: str, filename: str, content: str,
                    graph_name: str = DPE_GRAPH_NAME):
        """写入草案到 Outbox_Draft（skillflow 路径）"""
        filename = self._sanitize_filename(filename, content)
        draft_dir = self._draft_dir(project_id, step_id, graph_name)
        draft_path = draft_dir / filename
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(content, encoding="utf-8")

    def clean_step_dirs(self, project_id: str, step_id: str,
                        graph_name: str = DPE_GRAPH_NAME):
        """Clear draft/final dirs for a step so retried steps start clean."""
        # Draft/Final — graph_name prefix
        for getter in (self._draft_dir, self._final_dir):
            d = getter(project_id, step_id, graph_name)
            if d.exists():
                shutil.rmtree(d)
                d.mkdir(parents=True, exist_ok=True)

    def clean_all_task_step_dirs(self, project_id: str):
        """Clean workspace dirs for all task-level steps (for retries)."""
        for step_id in TASK_STEP_SEQUENCE:
            self.clean_step_dirs(project_id, step_id)

    def get_final_path(self, project_id: str, step_id: str,
                       graph_name: str = DPE_GRAPH_NAME) -> Path:
        """获取某步骤 Outbox_Final 目录的路径"""
        return self._final_dir(project_id, step_id, graph_name)

    # ── Project code repo ─────────────────────────────────────────────

    def rollback(self, project_id: str, commit_hash: str) -> bool:
        """[时光机] 强行重置代码仓库状态"""
        code_path = self.get_code_path(project_id)
        try:
            subprocess.run(["git", "reset", "--hard", commit_hash], cwd=code_path, check=True)
            return True
        except subprocess.CalledProcessError:
            return False

    def _get_git_hash(self, project_path: Path) -> str:
        """获取项目工作区的当前 Git Commit Hash。"""
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_path, capture_output=True, text=True
        )
        return res.stdout.strip()

    def get_project_path(self, project_id: str) -> Path:
        """获取 project 代码仓库路径。"""
        return self.get_code_path(project_id)

    def get_code_path(self, project_id: str) -> Path:
        """
        获取 project 代码仓库路径。
        从 DB 读取 repo_path, 如未设置则默认为 ~/.AItelier/projects/{project_id}/。
        如果 DB 不可用或项目不在 DB 中，使用默认路径。
        """
        try:
            from api.dependencies import get_db_manager
            db = get_db_manager()
            repo_info = db.get_repo_info(project_id)
            repo_path = repo_info.get("repo_path")
            if repo_path:
                code_path = Path(repo_path).resolve()
            else:
                code_path = self.projects_base / project_id
        except (ValueError, ImportError, Exception):
            code_path = self.projects_base / project_id
        code_path.mkdir(parents=True, exist_ok=True)
        return code_path
