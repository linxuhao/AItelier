# core/workspace_manager.py
# [说明] 物理隔离的工作区管理器。实现物理目录监狱 (Jail)、Git 事件溯源 (Event Sourcing) 以及跨语言运行时初始化。
# [变更] step_id 从 int 改为 str 以支持 Step 1.5 等非整数步骤；
#        目录创建适配六步法完整流程。
#        分离 DPS workspace (Inbox/Outbox) 与 Project workspace (代码仓库)。
#        Outbox_Draft / Outbox_Final / Trace 路径统一委托给 skillflow WorkspaceManager
#        （带 config_name 前缀），Inbox 保留在顶层（AItelier 概念）。

import json
import os
import shutil
import functools
import hashlib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

# Every repository operation here goes through core.closed_git: the Source
# repository can hold bytes a run delivered, so its hooks, filters, fsmonitor,
# pager, signing and transport helpers are data, never programs the backend
# runs. `_GIT_ENV` is that closed environment (English locale, optional locks
# off so an open status read cannot contend with a running pipeline's
# index.lock, no inherited GIT_* routing and no global/system configuration).
# It is built from nothing inherited; there is no `**os.environ` spread to win.
from core import closed_git
_GIT_ENV = closed_git.closed_env()

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

def _guarded_mutation(kind: str):
    """Hold a write admission around one whole operator mutation.

    A decorator rather than six rewritten bodies: the operations already begin
    by resolving through `_mutation_target`, and that resolution now happens
    inside the admission this wrapper is holding.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, project_id, *args, **kwargs):
            with self._mutation(project_id, kind):
                return fn(self, project_id, *args, **kwargs)
        return wrapper
    return deco


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class RelayDraftChanged(RuntimeError):
    """A relay draft differs from the manifest it was read under."""


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
        from core.project_id import require_valid
        require_valid(project_id)
        dps_path = self._get_secure_path(project_id)
        dps_path.mkdir(parents=True, exist_ok=True)

        # 初始化 DPS Git 仓库 (事件溯源基座)
        if not (dps_path / ".git").exists():
            self._init_with_initial_commit(dps_path, "Initial commit: DPS Workspace Created")

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
                self._init_with_initial_commit(code_path, "Initial commit: Project workspace")

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
                self._clone_without_checkout_programs(repo_url, code_path)
            elif not (code_path / ".git").exists():
                raise ValueError(f"Clone target exists but is not a git repo: {code_path}")

        else:
            raise ValueError(f"Unknown repo_type: {repo_type}")

    @staticmethod
    def _init_with_initial_commit(path: Path, message: str) -> None:
        """`git init` + `.gitignore` + first commit, with no template, hook, filter or followed link.

        A preexisting `.git` or `.gitignore` link is refused before anything is
        created; the `.gitignore` itself is placed by the no-follow writer.
        """
        if (path / ".git").is_symlink():
            raise closed_git.GitRefused(
                f"{path / '.git'} is a symbolic link; the bootstrap never initialises through it")
        ignore = path / ".gitignore"
        if ignore.is_symlink() or (os.path.lexists(ignore) and not ignore.is_file()):
            raise closed_git.GitRefused(
                f"{ignore} is not a regular file; the bootstrap never writes through it")
        closed_git.init_repository(path)
        with closed_git.WorkTree(path) as wt:
            wt.write(".gitignore", 0o100644, b"__pycache__/\n*.pyc\n")
        closed_git.commit_all(path, message)

    @staticmethod
    def _clone_without_checkout_programs(repo_url: str, code_path: Path) -> None:
        """Clone over an allowed transport, then materialise HEAD as raw bytes.

        `git clone` would check out through smudge filters and run the
        post-checkout hook; here the transfer is `--no-checkout` and the files
        and index come from the closed raw writer instead.
        """
        code_path.parent.mkdir(parents=True, exist_ok=True)
        closed_git.clone_no_checkout(repo_url, code_path)
        head = closed_git.resolve_commit(code_path, "HEAD")
        if head:
            repo = closed_git.discover(code_path)
            with closed_git.repo_lock(code_path):
                entries = closed_git.materialize(code_path, code_path, head)
                closed_git.write_index_file(repo.git_dir, entries)

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

    # ── Relay draft (continue a failed attempt's staged files) ─────────
    # A State attempt started with `continue_from` inherits the failed
    # attempt's STAGED files, not only its commits. They are parked here at
    # launch and moved into `{step}.tmp` by the engine AFTER `clean_draft_dir`
    # (which would otherwise wipe them), once, on the step's first execution.

    def _relay_dir(self, project_id: str, step_id: str,
                   graph_name: str = DPE_GRAPH_NAME) -> Path:
        return self._get_secure_path(project_id) / graph_name / "_relay" / step_id

    @staticmethod
    def relay_manifest(src_dir: Path) -> dict[str, str]:
        """Every regular file under `src_dir` with its sha256, relative path →
        hex digest. Symlinks (to files or directories) are neither listed nor
        followed, so the manifest names exactly what a relay may copy."""
        src = Path(src_dir)
        manifest = {}
        for directory, dirnames, filenames in os.walk(src, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not (Path(directory) / d).is_symlink())
            for name in filenames:
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    continue
                manifest[str(path.relative_to(src))] = _sha256(path)
        return manifest

    def stage_relay_draft(self, src_dir: Path, project_id: str, step_id: str,
                          graph_name: str = DPE_GRAPH_NAME, manifest: dict[str, str] | None = None) -> dict[str, str]:
        """Park a prior attempt's staged files for `step_id`, STRICTLY by
        manifest: only the listed paths are copied, each must still be a regular
        file with the listed sha256, and the park is re-hashed afterwards.
        Returns the manifest; raises RelayDraftChanged when the draft no longer
        matches what was read."""
        src = Path(src_dir)
        manifest = self.relay_manifest(src) if manifest is None else dict(manifest)
        if not manifest:
            return {}
        dst = self._relay_dir(project_id, step_id, graph_name)
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        try:
            for rel in sorted(manifest):
                if Path(rel).is_absolute() or ".." in Path(rel).parts:
                    raise RelayDraftChanged(f"{step_id}: manifest path {rel!r} escapes the staging")
                path = src / rel
                if path.is_symlink() or not path.is_file():
                    raise RelayDraftChanged(f"{step_id}: {rel} is no longer a regular file")
                if _sha256(path) != manifest[rel]:
                    raise RelayDraftChanged(f"{step_id}: {rel} changed since the draft was read")
                target = dst / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            if self.relay_manifest(dst) != manifest:
                raise RelayDraftChanged(f"{step_id}: parked copy does not match the manifest")
        except RelayDraftChanged:
            shutil.rmtree(dst, ignore_errors=True)  # a refused relay leaves no half-park behind
            raise
        return manifest

    def seed_relay_draft(self, project_id: str, step_id: str,
                         graph_name: str = DPE_GRAPH_NAME) -> list[str]:
        """Move a parked relay draft into the step's staging. Consumed once:
        the parked dir is renamed `.consumed` so a loop-back or a retry of the
        same step starts from its own state, never from the relay again."""
        src = self._relay_dir(project_id, step_id, graph_name)
        if not src.is_dir():
            return []
        files = sorted(str(f.relative_to(src)) for f in src.rglob("*") if f.is_file())
        draft = self._draft_dir(project_id, step_id, graph_name)
        draft.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, draft, dirs_exist_ok=True)
        consumed = src.with_name(src.name + ".consumed")
        if consumed.exists():
            shutil.rmtree(consumed)
        src.rename(consumed)
        return files

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
        if code_path is None:
            # `cwd=None` runs git in the SERVER's own checkout. A project that
            # declares no repository has nothing to roll back.
            return False
        try:
            target = closed_git.resolve_commit(code_path, commit_hash)
            if not target:
                return False
            closed_git.checkout_tree_raw(
                code_path, target,
                expected_head=closed_git.resolve_commit(code_path, "HEAD"),
                reason=f"reset: moving to {commit_hash}", require_clean_paths=False)
            return True
        except (RuntimeError, OSError):
            return False

    def _get_git_hash(self, project_path: Path) -> str:
        """获取项目工作区的当前 Git Commit Hash。"""
        return closed_git.resolve_commit(project_path, "HEAD") or ""

    def repo_status(self, project_id: str, log_limit: int = 20) -> dict:
        """Read-only snapshot of the project code repo for the web UI.

        Returns whether the path is a git repo, the current branch, working-tree
        dirtiness, ahead/behind counts vs the tracked upstream, the configured
        'origin' remote URL (if any), and the most recent commits. Every field
        degrades gracefully — a non-git or empty repo simply reports is_git=False
        or empty/None fields rather than raising.
        """
        code_path = self.get_code_path(project_id)
        if code_path is None:
            # Not "no git here" but "no here": `cwd=None` would answer about the
            # server's own checkout, and the UI would show its branch.
            return {"is_git": False, "path": None}

        def _git(*args: str) -> tuple[int, str]:
            res = closed_git.run(code_path, *args)
            return res.returncode, res.stdout.strip()

        rc, _ = _git("rev-parse", "--is-inside-work-tree")
        if rc != 0:
            return {"is_git": False, "path": str(code_path)}

        _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        # Working-file state from the raw index, HEAD tree and no-follow file
        # bytes — never `git status`, which refreshes through configured clean
        # filters and fsmonitor hooks.
        notes: list[str] = []
        try:
            raw = closed_git.raw_status(code_path)
            dirty_files = [f"{code} {path}" for path, code in raw.changes.items()]
            dirty_files += [f"?? {path}" for path in raw.untracked]
            notes = raw.notes
        except (RuntimeError, OSError) as e:
            dirty_files = []
            notes = [f"working-tree state unavailable: {str(e)[:200]}"]
        _, remote_url = _git("remote", "get-url", "origin")
        # Never hand a credential-bearing remote to an open read.
        from core.git_ops import redact_url_credentials
        remote_url = redact_url_credentials(remote_url)

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
        # Metadata only: signature verification is off in the closed profile.
        commits: list[dict] = []
        log_rc, log_out = _git(
            "log", f"-{max(1, log_limit)}", "--no-show-signature",
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
            "changes": dirty_files[:200],
            "working_tree_notes": notes,
        }

    # ── Repo write operations (web UI repository panel) ───────────────
    #
    # Each raises RuntimeError(stderr) on git failure so the API layer can map
    # it to a 400 with a useful message. Auth for clone/push of GitHub remotes
    # is supplied transparently by the container credential helper (see
    # docker/git-credential-helper.sh); these methods never handle tokens.

    def _run_git_checked(self, code_path: Path, *args: str) -> str:
        """Run a CLOSED-profile git command, raising RuntimeError(stderr) on failure.

        Only metadata/object/ref commands are accepted; a working-tree or
        transport command raises `GitRefused` (a RuntimeError) before running.
        """
        return closed_git.out(code_path, *args)

    @contextmanager
    def _mutation(self, project_id: str, kind: str):
        """Resolve, admit, and hold the admission for the whole operation.

        `_mutation_target` still validates (database reachable, a git repo, no
        foreign lease) — and then the admission makes it a mutex: a direct run
        cannot take this checkout between the check and the `git commit`, and
        the row is retired in `finally`. Each decorated method re-runs
        `_mutation_target` as its first line, and that call happens INSIDE the
        admission, so what the operation uses is what arbitration granted.
        """
        code_path = self._mutation_target(project_id)
        from core import run_isolation
        from api.dependencies import get_db_manager
        with run_isolation.write_admission(get_db_manager(), code_path,
                                           kind=kind, detail=project_id):
            yield code_path

    def _mutation_target(self, project_id: str):
        """Resolve the repository for a MUTATION, or refuse.

        Ordering is the whole point, and the independent review found it wrong.
        `get_code_path` deliberately treats an unreachable database as "use the
        default layout" — reasonable for a read, and for a WRITE it means a
        database hiccup silently retargets `repo_commit` at
        `projects_base/<project_id>`, which for a `repo_type: new` project is a
        real git repository the operator never named. The lease guard never even
        ran: an earlier resolution had already degraded.

        So a mutation asks for the database FIRST and refuses when it cannot
        have it, then resolves, then checks the lease. Three steps, and nothing
        can skip one of them.
        """
        try:
            from api.dependencies import get_db_manager
            get_db_manager()
        except Exception as e:
            from core import run_isolation
            raise run_isolation.CheckoutLeased(
                f"the checkout lease for project {project_id!r} could not be "
                f"verified ({type(e).__name__}: {e}) — refusing to mutate a "
                f"repository while it is unknown who holds it, and refusing to "
                f"resolve one through a degraded lookup") from e
        code_path = self._require_git_repo(project_id)
        self._require_no_foreign_lease(code_path)
        return code_path

    def _require_no_foreign_lease(self, code_path) -> None:
        """An operator mutation is a write to the same tree a run is using.

        The lease exists to make direct mode exclusive for the WHOLE run, not
        just for the seconds `repo_apply` is committing — so these host APIs
        honour it too. A shell running `git` in that directory by hand is
        outside what any of this can reach, and saying so is more useful than
        implying otherwise.
        """
        from core import run_isolation
        try:
            from api.dependencies import get_db_manager
            db = get_db_manager()
        except Exception as e:
            # FAIL CLOSED. This used to `return` here, so a database the guard
            # could not open turned into a mutation that went ahead unchecked —
            # the exact inversion of the resolution path, which refuses when it
            # cannot tell. A guard that answers "probably fine" when it cannot
            # look is not a guard.
            raise run_isolation.CheckoutLeased(
                f"the checkout lease for {code_path} could not be verified "
                f"({type(e).__name__}: {e}); refusing the mutation rather than "
                f"assuming nobody holds it") from e
        run_isolation.require_no_foreign_lease(db, code_path)

    def _require_git_repo(self, project_id: str) -> Path:
        code_path = self.get_code_path(project_id)
        if code_path is None:
            raise RuntimeError("This project declares no code repository")
        res = closed_git.run(code_path, "rev-parse", "--is-inside-work-tree")
        if res.returncode != 0:
            raise RuntimeError("Not a git repository")
        return code_path

    @_guarded_mutation("repo_set_remote")
    def repo_set_remote(self, project_id: str, url: str,
                        name: str = "origin") -> dict:
        """Add the remote, or update its URL if it already exists."""
        code_path = self._mutation_target(project_id)
        # The common configuration this writes is what push/pull/native-removal
        # preflights read under the same lock; the generic admission does not
        # exclude another generic admission, so it is not that exclusion.
        with closed_git.repo_lock(code_path):
            existing = closed_git.run(code_path, "remote").stdout.split()
            if name in existing:
                self._run_git_checked(code_path, "remote", "set-url", name, url)
                action = "updated"
            else:
                self._run_git_checked(code_path, "remote", "add", name, url)
                action = "added"
        return {"remote": name, "url": url, "action": action}

    @_guarded_mutation("repo_commit")
    def repo_commit(self, project_id: str, message: str) -> dict:
        """Stage all changes and commit. No-op (not an error) when clean.

        "Commit current Source changes" as raw bytes: the actual working files
        (index paths plus untracked nonignored files, read without following
        links) form a private index and tree, a hook/signature-free commit
        object moves HEAD by compare-and-swap, and the settled index is
        installed from an owned index.lock. No `git add`/`git commit`, so a
        delivered hook or filter never runs here; a repository whose configured
        filters would change the stored bytes gets an explicit refusal.
        """
        code_path = self._mutation_target(project_id)
        return closed_git.commit_all(code_path, message)

    @_guarded_mutation("repo_push")
    def repo_push(self, project_id: str, branch: str | None = None,
                  set_upstream: bool = True) -> dict:
        """Push a branch to origin (sets upstream by default)."""
        code_path = self._mutation_target(project_id)
        if not branch:
            branch = self._run_git_checked(
                code_path, "rev-parse", "--abbrev-ref", "HEAD")
        self._require_branch_name(code_path, branch)
        out = self._push(code_path, branch, set_upstream)
        return {"pushed": True, "branch": branch, "detail": out}

    @_guarded_mutation("repo_push_head")
    def repo_push_head(self, project_id: str, branch: str,
                       set_upstream: bool = True) -> dict:
        """Push the current HEAD to origin/<branch>, creating that remote branch.

        Unlike repo_push (which pushes a same-named local branch), this maps the
        current working tree's HEAD onto a possibly-new remote branch name — the
        "push current work to a feature branch, then open a PR" flow.
        """
        code_path = self._mutation_target(project_id)
        self._require_branch_name(code_path, branch)
        out = self._push(code_path, f"HEAD:refs/heads/{branch}", set_upstream)
        return {"pushed": True, "branch": branch, "detail": out}

    @staticmethod
    def _require_branch_name(code_path: Path, branch: str) -> None:
        if (not branch or branch.startswith("-")
                or closed_git.run(code_path, "check-ref-format", "--branch", branch).returncode):
            raise RuntimeError(f"{branch!r} is not a valid branch name")

    @staticmethod
    def _push(code_path: Path, refspec: str, set_upstream: bool) -> str:
        """Push to origin over an allowed transport, with no pre-push hook or helper program."""
        with closed_git.repo_lock(code_path):
            profile = closed_git.remote_transport(code_path, "origin", push=True)
            args = ["push", "--no-verify"]
            if set_upstream:
                args += ["--set-upstream"]
            return closed_git.transport(code_path, *args, "origin", refspec, profile=profile)

    @_guarded_mutation("repo_pull")
    def repo_pull(self, project_id: str) -> dict:
        """Fast-forward pull from the tracked upstream (no merge commits).

        Refuses (RuntimeError) rather than creating a merge if the branches have
        diverged — the user should use force-sync deliberately in that case.
        """
        code_path = self._mutation_target(project_id)
        with closed_git.repo_lock(code_path):
            branch = closed_git.run(code_path, "symbolic-ref", "--quiet", "--short", "HEAD")
            if branch.returncode or not branch.stdout.strip():
                raise RuntimeError("You are not currently on a branch.")
            name = branch.stdout.strip()
            cfg = dict(closed_git.config_list(code_path))
            remote = cfg.get(f"branch.{name}.remote") or ""
            if not remote or remote == ".":
                raise RuntimeError(
                    f"There is no tracking information for the current branch {name}.")
            profile = closed_git.remote_transport(code_path, remote, push=False)
            detail = closed_git.transport(
                code_path, "fetch", "--no-recurse-submodules", "--no-auto-gc",
                remote, profile=profile)
            head = closed_git.resolve_commit(code_path, "HEAD")
            upstream = closed_git.resolve_commit(code_path, "@{upstream}")
            if not upstream:
                raise RuntimeError("the tracked upstream branch is unavailable after fetch")
            if head == upstream:
                return {"pulled": True, "detail": (detail + "\nAlready up to date.").strip()}
            if head and closed_git.run(code_path, "merge-base", "--is-ancestor",
                                       head, upstream).returncode:
                raise RuntimeError("Not possible to fast-forward, aborting.")
            closed_git.checkout_tree_raw(
                code_path, upstream, expected_head=head,
                reason=f"pull --ff-only: fast-forward to {upstream[:12]}",
                require_clean_paths=True)
        return {"pulled": True, "detail": (detail + f"\nFast-forward to {upstream[:12]}").strip()}

    @_guarded_mutation("repo_force_sync")
    def repo_force_sync(self, project_id: str, branch: str,
                        backup: bool = True) -> dict:
        """Destructive: fetch origin and hard-reset the working tree to
        origin/<branch>. A timestamped backup branch is created first (default)
        so the discarded state is recoverable.
        """
        code_path = self._mutation_target(project_id)
        self._require_branch_name(code_path, branch)
        with closed_git.repo_lock(code_path):
            profile = closed_git.remote_transport(code_path, "origin", push=False)
            head = closed_git.resolve_commit(code_path, "HEAD")
            backup_ref = None
            if backup and head:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_ref = f"backup/{stamp}"
                # Best-effort, as before: an existing name leaves no backup.
                made = closed_git.run(code_path, "update-ref", "-m", "force-sync backup",
                                      f"refs/heads/{backup_ref}", head, closed_git.ZERO_SHA)
                if made.returncode:
                    backup_ref = None
            closed_git.transport(code_path, "fetch", "--no-recurse-submodules",
                                 "--no-auto-gc", "origin", branch, profile=profile)
            target = closed_git.resolve_commit(code_path, f"refs/remotes/origin/{branch}") \
                or closed_git.resolve_commit(code_path, "FETCH_HEAD")
            if not target:
                raise RuntimeError(f"origin/{branch} is unavailable after fetch")
            closed_git.checkout_tree_raw(
                code_path, target, expected_head=head,
                reason=f"reset: moving to origin/{branch}", require_clean_paths=False)
        return {
            "synced": True,
            "branch": branch,
            "backup_branch": backup_ref,
            "head": self._get_git_hash(code_path),
        }

    def get_project_path(self, project_id: str) -> Path | None:
        """获取 project 代码仓库路径。"""
        return self.get_code_path(project_id)

    def get_code_path(self, project_id: str,
                      run_id: str | None = None) -> Path | None:
        """The project's code repository, or None if it DECLARES it has none.

        从 DB 读取 repo_path, 如未设置则默认为 ~/.AItelier/projects/{project_id}/。
        如果 DB 不可用或项目不在 DB 中，使用默认路径。

        The None answer exists because this result is what the host hands
        skillflow as `project_root` for every agent-invoked tool
        (`core/dpe_pipeline.py`). Ignoring `repo_type` made the two halves
        disagree about the same run: skillflow, told by the code-path resolver
        that a `repo_mode: none` run owns no repository, attaches no repo read
        layer — while the host handed `create`/`edit` a baseline pointing at one.
        `read`/`list`/`search` saw no repo and the write tools saw a repo; the
        agent could not read what it was writing against. The unconditional
        `mkdir` below is also what materialised the empty directories sitting
        under `~/.AItelier/projects/` for runs that never wanted a repo.

        Same predicate as `api/dependencies._existing_repo_code_path`, and it
        must stay the same: a recorded `repo_path` WINS, because a row can carry
        both answers. `run_launcher` overwrites `repo_type` with `"none"` for a
        `repo_mode: none` config while keeping the caller's `repo_path` — the
        `against_project` shape, a run that emits no code but reads a real repo.
        """
        default = self.projects_base / project_id
        if run_id:
            # Same question, same order, same failure mode as
            # api.dependencies._existing_repo_code_path — they must not
            # disagree about one run, and this one feeds the prompt and the
            # host tool path while that one feeds the engine.
            from core import run_isolation
            from api.dependencies import get_db_manager as _gdb, get_skillflow as _gsf
            answer = run_isolation.resolve_for_resolver(_gdb(), run_id, sf=_gsf())
            if answer is False:
                return None
            if answer:
                return Path(answer)
        try:
            from api.dependencies import get_db_manager
            db = get_db_manager()
            repo_info = db.get_repo_info(project_id)
            repo_path = repo_info.get("repo_path")
            if repo_path:
                code_path = Path(repo_path).resolve()
            elif (repo_info.get("repo_type") or "") == "none":
                return None
            else:
                code_path = default
        except (ValueError, ImportError, Exception):
            # Unknown project / DB unavailable stays "use the default", NOT
            # "owns no repo": failing to read the row is not a declaration, and
            # answering None there would strip the repo from a project that has
            # one every time the lookup hiccups.
            code_path = default
        code_path.mkdir(parents=True, exist_ok=True)
        return code_path
