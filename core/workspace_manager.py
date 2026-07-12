# core/workspace_manager.py
# [说明] 物理隔离的工作区管理器。实现物理目录监狱 (Jail)、Git 事件溯源 (Event Sourcing) 以及跨语言运行时初始化。
# [变更] step_id 从 int 改为 str 以支持 Step 1.5 等非整数步骤；
#        目录创建适配六步法完整流程。
#        分离 DPS workspace (Inbox/Outbox) 与 Project workspace (代码仓库)。
#        Outbox_Draft / Outbox_Final / Trace 路径统一委托给 skillflow WorkspaceManager
#        （带 config_name 前缀），Inbox 保留在顶层（AItelier 概念）。

import json
import os
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

# Force English locale for all git subprocess calls — prevents French
# locale leakage in dashboard action result messages (res.detail).
_GIT_ENV = {"LC_ALL": "C", **os.environ}

# 六步法步骤序列（string ID）
STEP_SEQUENCE = ["1", "2", "3", "5"]

# Project-level planning steps (run once per project)
# Step 1 (Researcher, was 1_5) — research + SOTA
PROJECT_STEP_SEQUENCE = ["1", "2", "3"]

# Task-level execution steps (run per task)
# t_sota + t_design merged into t_plan
TASK_STEP_SEQUENCE = ["t_plan", "t_impl"]

# Project-level final verification
FINAL_STEP = "5"

# Default DPE graph name — matches the config name in configs/dpe_default.yaml
DPE_GRAPH_NAME = "dpe_default_v2"

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
    def __init__(self, base_path: str, projects_base: str | None = None):
        # base_path is REQUIRED — a prod-pointing default here let stray
        # construction reach real workspaces. Production resolves paths in
        # core.datadir (composed in api/dependencies); tests pass tmp paths.
        # projects_base follows the same env-aware authority when omitted
        # (isolated in tests via AITELIER_HOME), or can be passed explicitly.
        if not base_path:
            raise ValueError(
                "WorkspaceManager requires an explicit base_path (production:"
                " core.datadir.workspaces_dir(); tests: a tmp_path dir)")
        from core import datadir
        self.base_path = Path(base_path).resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.projects_base = Path(
            projects_base or datadir.projects_dir()).resolve()
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
            subprocess.run(["git", "init"], cwd=dps_path, check=True, capture_output=True, env=_GIT_ENV)
            (dps_path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=dps_path, check=True, env=_GIT_ENV)
            subprocess.run(["git", "commit", "-m", "Initial commit: DPS Workspace Created"], cwd=dps_path, env=_GIT_ENV)

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
        if repo_type == "none":
            # Authoring runs (pipeline/addon converters) emit a config/overlay
            # artifact, not code — they get NO code repo, staying fully repo-
            # independent. The skillflow step dirs created above are all they need.
            pass
        elif repo_type == "new":
            code_path = self.projects_base / project_id
            code_path.mkdir(parents=True, exist_ok=True)
            if not (code_path / ".git").exists():
                subprocess.run(["git", "init"], cwd=code_path, check=True, capture_output=True, env=_GIT_ENV)
                (code_path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=code_path, check=True, env=_GIT_ENV)
                subprocess.run(["git", "commit", "-m", "Initial commit: Project workspace"], cwd=code_path, env=_GIT_ENV)

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
                    check=True, capture_output=True, env=_GIT_ENV
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

    def clean_draft_dir(self, project_id: str, step_id: str,
                        graph_name: str = DPE_GRAPH_NAME):
        """Clear ONLY the draft ({step_id}.tmp) staging for a step.

        Called at the start of a step run so a fresh run starts with empty
        staging — otherwise files written by a prior task or a prior (failed)
        attempt linger in the shared, never-cleared {step_id}.tmp and get
        promoted + committed wholesale. The final dir is left intact so the
        step's own prior output ({step: ...} self-context) still resolves.
        """
        d = self._draft_dir(project_id, step_id, graph_name)
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
            subprocess.run(["git", "reset", "--hard", commit_hash], cwd=code_path, check=True, env=_GIT_ENV)
            return True
        except subprocess.CalledProcessError:
            return False

    def _get_git_hash(self, project_path: Path) -> str:
        """获取项目工作区的当前 Git Commit Hash。"""
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_path, capture_output=True, text=True, env=_GIT_ENV
        )
        return res.stdout.strip()

    def repo_status(self, project_id: str, log_limit: int = 20) -> dict:
        """Read-only snapshot of the project code repo for the web UI.

        Returns whether the path is a git repo, the current branch, working-tree
        dirtiness, ahead/behind counts vs the tracked upstream, the configured
        'origin' remote URL (if any), and the most recent commits. Every field
        degrades gracefully — a non-git or empty repo simply reports is_git=False
        or empty/None fields rather than raising.
        """
        code_path = self.get_code_path(project_id)

        def _git(*args: str) -> tuple[int, str]:
            res = subprocess.run(
                ["git", *args], cwd=code_path,
                capture_output=True, text=True, env=_GIT_ENV,
            )
            return res.returncode, res.stdout.strip()

        rc, _ = _git("rev-parse", "--is-inside-work-tree")
        if rc != 0:
            return {"is_git": False, "path": str(code_path)}

        _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        _, porcelain = _git("status", "--porcelain")
        dirty_files = [ln for ln in porcelain.splitlines() if ln.strip()]
        _, remote_url = _git("remote", "get-url", "origin")

        # ahead/behind vs the upstream the current branch tracks (if any).
        ahead = behind = None
        upstream_rc, upstream = _git(
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
        )
        if upstream_rc == 0 and upstream:
            counts_rc, counts = _git(
                "rev-list", "--left-right", "--count", "HEAD...@{upstream}"
            )
            if counts_rc == 0 and counts:
                parts = counts.split()
                if len(parts) == 2:
                    ahead, behind = int(parts[0]), int(parts[1])

        # Recent commits: hash | author | ISO date | subject (NUL-delimited).
        commits: list[dict] = []
        log_rc, log_out = _git(
            "log", f"-{max(1, log_limit)}",
            "--pretty=format:%h%x1f%an%x1f%aI%x1f%s",
        )
        if log_rc == 0 and log_out:
            for line in log_out.splitlines():
                fields = line.split("\x1f")
                if len(fields) == 4:
                    commits.append({
                        "hash": fields[0], "author": fields[1],
                        "date": fields[2], "subject": fields[3],
                    })

        return {
            "is_git": True,
            "path": str(code_path),
            "branch": branch or None,
            "dirty": bool(dirty_files),
            "dirty_count": len(dirty_files),
            "remote_url": remote_url or None,
            "upstream": upstream if upstream_rc == 0 else None,
            "ahead": ahead,
            "behind": behind,
            "commits": commits,
        }

    # ── Repo write operations (web UI repository panel) ───────────────
    #
    # Each raises RuntimeError(stderr) on git failure so the API layer can map
    # it to a 400 with a useful message. Auth for clone/push of GitHub remotes
    # is supplied transparently by the container credential helper (see
    # docker/git-credential-helper.sh); these methods never handle tokens.

    def _run_git_checked(self, code_path: Path, *args: str) -> str:
        """Run a git command, raising RuntimeError(stderr) on non-zero exit."""
        res = subprocess.run(
            ["git", *args], cwd=code_path, capture_output=True, text=True,
            env=_GIT_ENV,
        )
        if res.returncode != 0:
            raise RuntimeError(
                (res.stderr or res.stdout or f"git {args[0]} failed").strip()
            )
        return res.stdout.strip()

    def _require_git_repo(self, project_id: str) -> Path:
        code_path = self.get_code_path(project_id)
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=code_path, capture_output=True, text=True, env=_GIT_ENV,
        )
        if res.returncode != 0:
            raise RuntimeError("Not a git repository")
        return code_path

    def repo_set_remote(self, project_id: str, url: str,
                        name: str = "origin") -> dict:
        """Add the remote, or update its URL if it already exists."""
        code_path = self._require_git_repo(project_id)
        existing = subprocess.run(
            ["git", "remote"], cwd=code_path, capture_output=True, text=True,
            env=_GIT_ENV,
        ).stdout.split()
        if name in existing:
            self._run_git_checked(code_path, "remote", "set-url", name, url)
            action = "updated"
        else:
            self._run_git_checked(code_path, "remote", "add", name, url)
            action = "added"
        return {"remote": name, "url": url, "action": action}

    def repo_commit(self, project_id: str, message: str) -> dict:
        """Stage all changes and commit. No-op (not an error) when clean."""
        code_path = self._require_git_repo(project_id)
        self._run_git_checked(code_path, "add", "-A")
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=code_path, capture_output=True, text=True, env=_GIT_ENV,
        ).stdout.strip()
        if not porcelain:
            return {"committed": False, "message": "Nothing to commit"}
        self._run_git_checked(code_path, "commit", "-m", message)
        return {"committed": True, "hash": self._get_git_hash(code_path)}

    def repo_push(self, project_id: str, branch: str | None = None,
                  set_upstream: bool = True) -> dict:
        """Push a branch to origin (sets upstream by default)."""
        code_path = self._require_git_repo(project_id)
        if not branch:
            branch = self._run_git_checked(
                code_path, "rev-parse", "--abbrev-ref", "HEAD")
        args = ["push"]
        if set_upstream:
            args += ["--set-upstream"]
        args += ["origin", branch]
        out = self._run_git_checked(code_path, *args)
        return {"pushed": True, "branch": branch, "detail": out}

    def repo_push_head(self, project_id: str, branch: str,
                       set_upstream: bool = True) -> dict:
        """Push the current HEAD to origin/<branch>, creating that remote branch.

        Unlike repo_push (which pushes a same-named local branch), this maps the
        current working tree's HEAD onto a possibly-new remote branch name — the
        "push current work to a feature branch, then open a PR" flow.
        """
        code_path = self._require_git_repo(project_id)
        args = ["push"]
        if set_upstream:
            args += ["--set-upstream"]
        args += ["origin", f"HEAD:refs/heads/{branch}"]
        out = self._run_git_checked(code_path, *args)
        return {"pushed": True, "branch": branch, "detail": out}

    def repo_pull(self, project_id: str) -> dict:
        """Fast-forward pull from the tracked upstream (no merge commits).

        Refuses (RuntimeError) rather than creating a merge if the branches have
        diverged — the user should use force-sync deliberately in that case.
        """
        code_path = self._require_git_repo(project_id)
        out = self._run_git_checked(code_path, "pull", "--ff-only")
        return {"pulled": True, "detail": out}

    def repo_force_sync(self, project_id: str, branch: str,
                        backup: bool = True) -> dict:
        """Destructive: fetch origin and hard-reset the working tree to
        origin/<branch>. A timestamped backup branch is created first (default)
        so the discarded state is recoverable.
        """
        code_path = self._require_git_repo(project_id)
        backup_ref = None
        if backup:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_ref = f"backup/{stamp}"
            # Best-effort: a repo with no commits yet can't be branched.
            try:
                self._run_git_checked(code_path, "branch", backup_ref)
            except RuntimeError:
                backup_ref = None
        self._run_git_checked(code_path, "fetch", "origin", branch)
        self._run_git_checked(
            code_path, "reset", "--hard", f"origin/{branch}")
        return {
            "synced": True,
            "branch": branch,
            "backup_branch": backup_ref,
            "head": self._get_git_hash(code_path),
        }

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
