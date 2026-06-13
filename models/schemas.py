# File: models/schemas.py

from enum import Enum
from typing import Literal, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class TaskStatus(str, Enum):
    """任务状态枚举定义"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"

class TaskCreate(BaseModel):
    """
    任务创建请求的数据模型
    Reference: FastAPI/Pydantic standard request body schemas
    """
    project_id: str = Field(..., description="项目唯一标识ID")
    prompt: str = Field(..., description="用户输入的初始需求/意图")
    project_brief: Optional[str] = Field(None, description="Meta Agent 生成的结构化项目简报 (Markdown)")

class TaskResponse(BaseModel):
    """
    任务状态返回的数据模型
    自动生成 created_at 时间戳
    """
    id: int = Field(..., description="数据库自增ID")
    project_id: str = Field(..., description="关联的项目ID")
    status: TaskStatus = Field(..., description="当前任务状态")
    owner_email: str = Field("cli@local", description="任务所有者邮箱")
    last_error: Optional[str] = Field(None, description="最后一次失败的错误信息")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="任务创建的UTC时间"
    )

class ProjectCreate(BaseModel):
    project_id: str = Field(..., description="Unique project identifier (filesystem-safe slug)")
    name: Optional[str] = Field(None, description="Human-readable project name")
    priority: Optional[int] = Field(0, description="Scheduling priority (higher = sooner)")
    repo_type: Optional[str] = Field("new", description="Repository type: 'new', 'existing', or 'clone'")
    repo_path: Optional[str] = Field(None, description="Local repo path (required for 'existing')")
    repo_url: Optional[str] = Field(None, description="Remote repo URL (required for 'clone')")

class ProjectResponse(BaseModel):
    project_id: str
    name: str
    status: str = "planning"
    current_project_step: Optional[str] = None
    priority: int = 0
    owner_email: str = "cli@local"
    repo_type: Optional[str] = "new"
    repo_path: Optional[str] = None
    repo_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime

class ProjectWithStats(BaseModel):
    project_id: str
    name: str
    status: str = "planning"
    current_project_step: Optional[str] = None
    priority: int = 0
    owner_email: str = "cli@local"
    created_at: datetime
    updated_at: datetime
    task_count: int = 0
    completed_count: int = 0
    running_count: int = 0
    failed_count: int = 0
    pending_count: int = 0
    latest_status: Optional[str] = None
    latest_step: Optional[str] = None
    last_update: Optional[str] = None

class InteractionMeta(BaseModel):
    """Structured interaction guidance for API clients (CLI, web GUI).
    Tells the client what phase the user is in, what actions are available,
    and provides a generic hint. Clients add their own cosmetic layer on top."""
    phase: str  # "assessment" | "brief_review" | "meta_conversation" | "task_meta" | "checkpoint"
    available_actions: list[str]  # ["approve", "reject", "answer", "/skip", "/cancel"]
    hint: str  # Generic hint text for what to do next
    turn: Optional[int] = None  # Current turn number (for multi-turn conversations)
    max_turns: Optional[int] = None  # Max turns for this phase


class IOLogCreate(BaseModel):
    """
    流水线输入输出日志的数据模型
    利用 Literal 强制约束 direction 仅为 INBOX 或 OUTBOX
    """
    task_id: int = Field(..., description="关联的主任务ID")
    step_name: str = Field(..., description="当前执行的具体步骤名")
    direction: Literal["INBOX", "OUTBOX"] = Field(..., description="数据流转方向")
    git_commit_hash: str = Field(..., description="绑定事件溯源的Git Hash")
    content_summary: str = Field(..., description="日志或操作内容的简要摘要")