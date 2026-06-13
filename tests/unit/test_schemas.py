# File: tests/test_schemas.py

import pytest
from pydantic import ValidationError
from models.schemas import TaskCreate, TaskStatus, IOLogCreate, TaskResponse, ProjectResponse, ProjectWithStats

def test_task_create_schema():
    """测试 TaskCreate 模型正常实例化"""
    task = TaskCreate(project_id="proj_1", prompt="Build a scraper")
    assert task.project_id == "proj_1"
    assert task.prompt == "Build a scraper"

def test_io_log_direction_validation():
    """测试 IOLogCreate 模型及 Enum/Literal 约束"""
    log = IOLogCreate(
        task_id=1,
        step_name="step_1",
        direction="INBOX",
        git_commit_hash="abc",
        content_summary="sum"
    )
    assert log.direction == "INBOX"

def test_task_create_missing_fields_raises_error():
    """测试缺少必填字段时，是否正确触发 Pydantic ValidationError"""
    with pytest.raises(ValidationError) as excinfo:
        TaskCreate(project_id="proj_1") # 故意遗漏 prompt 字段
    assert "prompt" in str(excinfo.value)

def test_io_log_invalid_direction_raises_error():
    """测试非法的数据流转方向时，是否正确拦截"""
    with pytest.raises(ValidationError) as excinfo:
        IOLogCreate(
            task_id=1,
            step_name="step_1",
            direction="MIDDLEBOX", # 非法字段，应仅限 INBOX/OUTBOX
            git_commit_hash="abc",
            content_summary="sum"
        )
    assert "Input should be 'INBOX' or 'OUTBOX'" in str(excinfo.value)

def test_task_response_status_enum():
    """测试 TaskResponse 枚举解析功能"""
    response = TaskResponse(
        id=100,
        project_id="proj_2",
        status="pending" # 测试字符串能否正确隐式转换为 Enum
    )
    assert response.status == TaskStatus.PENDING


# ── owner_email field tests ──


def test_task_response_default_owner():
    """TaskResponse defaults owner_email to cli@local."""
    resp = TaskResponse(id=1, project_id="p", status="pending")
    assert resp.owner_email == "cli@local"


def test_task_response_custom_owner():
    """TaskResponse accepts custom owner_email."""
    resp = TaskResponse(id=1, project_id="p", status="pending", owner_email="user@test.com")
    assert resp.owner_email == "user@test.com"


def test_project_response_default_owner():
    """ProjectResponse defaults owner_email to cli@local."""
    from datetime import datetime, timezone
    resp = ProjectResponse(
        project_id="p", name="P",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert resp.owner_email == "cli@local"


def test_project_with_stats_default_owner():
    """ProjectWithStats defaults owner_email to cli@local."""
    from datetime import datetime, timezone
    resp = ProjectWithStats(
        project_id="p", name="P",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert resp.owner_email == "cli@local"


# ── Repo type field tests ──


def test_project_create_default_repo_type():
    """ProjectCreate defaults to repo_type='new'."""
    from models.schemas import ProjectCreate
    pc = ProjectCreate(project_id="test")
    assert pc.repo_type == "new"
    assert pc.repo_path is None
    assert pc.repo_url is None


def test_project_create_with_repo_fields():
    """ProjectCreate accepts repo_type, repo_path, repo_url."""
    from models.schemas import ProjectCreate
    pc = ProjectCreate(
        project_id="test",
        repo_type="clone",
        repo_path="/some/path",
        repo_url="https://github.com/user/repo.git",
    )
    assert pc.repo_type == "clone"
    assert pc.repo_path == "/some/path"
    assert pc.repo_url == "https://github.com/user/repo.git"


def test_project_response_repo_fields():
    """ProjectResponse includes repo fields."""
    from datetime import datetime, timezone
    resp = ProjectResponse(
        project_id="p", name="P",
        repo_type="existing", repo_path="/local/repo",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert resp.repo_type == "existing"
    assert resp.repo_path == "/local/repo"