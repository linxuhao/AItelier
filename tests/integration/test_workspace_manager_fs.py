# File: tests/test_workspace_manager_fs.py
# [变更] 适配六步法目录结构 (string step_id, Inbox_{step}, Outbox_Draft_{step}, Outbox_Final_{step})。

import pytest
from pathlib import Path
from core.workspace_manager import WorkspaceManager, PROJECT_STEP_SEQUENCE, TASK_STEP_SEQUENCE, FINAL_STEP

def test_setup_workspace(tmp_path: Path):
    """测试系统能否正确生成六步法所需的 Inbox/Outbox 目录"""
    manager = WorkspaceManager(base_path=str(tmp_path / "ws"))
    manager.projects_base = tmp_path / "projects"
    manager.projects_base.mkdir(parents=True, exist_ok=True)
    project_id = "test_sandbox_1"

    # 执行生成逻辑
    manager.setup_workspace(project_id, repo_type="new")
    proj_path = manager._get_secure_path(project_id)

    # 断言项目根目录生成正确
    assert proj_path.exists()
    assert proj_path.is_dir()
    assert proj_path.name == project_id

    # 断言全局挂载点存在
    assert (proj_path / "Global_Mount").exists()

    # 循环断言管线每步的 Draft/Final 目录（graph_name 下，skillflow 布局）
    # Inbox 目录已废弃 — skillflow 已迁移至 {step_id}.tmp/ → {step_id}/
    active_steps = ["1"] + PROJECT_STEP_SEQUENCE + TASK_STEP_SEQUENCE
    if FINAL_STEP not in active_steps:
        active_steps.append(FINAL_STEP)
    for step_id in active_steps:
        # Draft/Final 在 graph_name 下（skillflow 布局）
        for suffix in [".tmp", ""]:
            subdir_path = proj_path / "dpe_default_v2" / f"{step_id}{suffix}"
            assert subdir_path.exists(), f"Missing required directory: dpe_default_v2/{step_id}{suffix}"
            assert subdir_path.is_dir()

def test_path_traversal_blocked(tmp_path: Path):
    """测试路径穿越攻击被拦截"""
    manager = WorkspaceManager(base_path=str(tmp_path / "ws"))
    with pytest.raises(PermissionError, match="Path traversal"):
        manager._get_secure_path("../../../etc/passwd")

def test_write_draft(tmp_path: Path):
    """测试 Draft 写入（使用 skillflow 布局 {step_id}.tmp）"""
    manager = WorkspaceManager(base_path=str(tmp_path / "ws"))
    manager.projects_base = tmp_path / "projects"
    manager.projects_base.mkdir(parents=True, exist_ok=True)
    project_id = "test_rw"
    manager.setup_workspace(project_id, repo_type="new")

    proj_path = manager._get_secure_path(project_id)

    # 写入 Draft (uses {step_id}.tmp under graph_name)
    manager.write_draft(project_id, "1", "output.py", "print('hello')")
    assert (proj_path / "dpe_default_v2" / "1.tmp" / "output.py").exists()

    # 验证文件名 sanitization
    manager.write_draft(project_id, "1", "report.", '{"key": "value"}')
    assert (proj_path / "dpe_default_v2" / "1.tmp" / "report.json").exists()


# ── Repo type tests ──


def test_setup_new_repo_creates_project_dir(tmp_path: Path):
    """repo_type='new' creates a git repo at projects_base/{project_id}."""
    manager = WorkspaceManager(base_path=str(tmp_path / "ws"))
    manager.projects_base = tmp_path / "projects"
    manager.projects_base.mkdir(parents=True, exist_ok=True)

    manager.setup_workspace("new-proj", repo_type="new")

    proj = tmp_path / "projects" / "new-proj"
    assert proj.exists()
    assert (proj / ".git").exists()


def test_setup_existing_repo_uses_provided_path(tmp_path: Path):
    """repo_type='existing' uses the provided local repo path."""
    import subprocess

    # Create a git repo on disk
    existing = tmp_path / "existing-repo"
    existing.mkdir()
    subprocess.run(["git", "init"], cwd=existing, check=True, capture_output=True)
    (existing / "README.md").write_text("test", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=existing, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=existing, check=True)

    manager = WorkspaceManager(base_path=str(tmp_path / "ws"))
    manager.projects_base = tmp_path / "projects"
    manager.projects_base.mkdir(parents=True, exist_ok=True)

    manager.setup_workspace("ext-proj", repo_type="existing", repo_path=str(existing))

    # DPS workspace should still be created with skillflow step dirs
    dps = manager._get_secure_path("ext-proj")
    assert dps.exists()
    assert (dps / "dpe_default_v2" / "1").exists()


def test_setup_existing_repo_validates_git(tmp_path: Path):
    """repo_type='existing' raises if path is not a git repo."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    manager = WorkspaceManager(base_path=str(tmp_path / "ws"))
    manager.projects_base = tmp_path / "projects"
    manager.projects_base.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="not a git repository"):
        manager.setup_workspace("bad-proj", repo_type="existing", repo_path=str(not_a_repo))


def test_setup_clone_repo(tmp_path: Path):
    """repo_type='clone' clones from a URL (local path for testing)."""
    import subprocess

    # Create a source repo to clone from
    src = tmp_path / "source"
    src.mkdir()
    subprocess.run(["git", "init"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=src, check=True)

    manager = WorkspaceManager(base_path=str(tmp_path / "ws"))
    manager.projects_base = tmp_path / "projects"
    manager.projects_base.mkdir(parents=True, exist_ok=True)

    manager.setup_workspace("clone-proj", repo_type="clone", repo_url=str(src))

    clone_target = tmp_path / "projects" / "clone-proj"
    assert clone_target.exists()
    assert (clone_target / ".git").exists()


def test_setup_workspace_no_project_in_dps(tmp_path: Path):
    """DPS workspace should NOT contain a project/ directory."""
    manager = WorkspaceManager(base_path=str(tmp_path / "ws"))
    manager.projects_base = tmp_path / "projects"
    manager.projects_base.mkdir(parents=True, exist_ok=True)

    manager.setup_workspace("no-proj-dir", repo_type="new")

    dps = manager._get_secure_path("no-proj-dir")
    assert not (dps / "project").exists()
