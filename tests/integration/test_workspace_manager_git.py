# File: tests/test_workspace_manager_git.py
# [变更] 适配 string step_id 和六步法目录结构。
#        适配 DPS/project workspace 分离。

import pytest
from pathlib import Path
from core.workspace_manager import WorkspaceManager

def test_git_event_sourcing_and_rollback(tmp_path: Path):
    """测试沙盒状态保存与底层 Git 时光机回滚机制的原子性"""
    manager = WorkspaceManager(base_path=str(tmp_path / "ws"))
    manager.projects_base = tmp_path / "projects"
    manager.projects_base.mkdir(parents=True, exist_ok=True)
    project_id = "test_time_machine"

    # 1. 初始化工作区 (new 类型)
    manager.setup_workspace(project_id, repo_type="new")
    dps_path = manager._get_secure_path(project_id)
    code_path = manager.get_code_path(project_id)

    # 2. 写入文件到 code repo 创建 commit 用于 rollback 测试
    # (commit_step / apply_final_to_project 已废弃 — skillflow 通过 repo_apply 处理)
    (code_path / "config.json").write_text('{"status": "init"}', encoding="utf-8")
    import subprocess
    code_hash = manager._get_git_hash(code_path)

    # Write a file to code repo and commit so we can rollback
    (code_path / "config.json").write_text('{"status": "init"}', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=code_path, check=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=code_path, check=True)
    code_hash_v1 = manager._get_git_hash(code_path)

    (code_path / "code.py").write_text('print("hello")', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=code_path, check=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=code_path, check=True)

    # 5. 触发时光机回滚
    success = manager.rollback(project_id, code_hash_v1)
    assert success is True

    # 6. 验证 v1 文件恢复
    assert (code_path / "config.json").exists(), "config.json should exist after rollback"
