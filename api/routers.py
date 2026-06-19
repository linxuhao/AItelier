# File: api/routers.py

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json
from models.schemas import TaskCreate, TaskResponse, TaskStatus
from typing import List
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager
from api.dependencies import get_db_manager, get_workspace_manager, owner_filter, check_write_owner, check_read_owner
from api.auth import CurrentUser, get_optional_user
from api.sse_manager import stream_manager

router = APIRouter(prefix="/api/tasks", tags=["Tasks"])

class RollbackRequest(BaseModel):
    """回滚请求专属 Body 模型"""
    commit_hash: str

@router.post("", response_model=TaskResponse)
def create_task(
    task: TaskCreate,
    request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager)
):
    """接收任务意图，初始化安全沙盒并入队"""
    owner = user.email if user else "cli@local"

    # 1. Verify project exists (do NOT auto-create)
    project = db.get_project(task.project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{task.project_id}' not found")

    # 2. 动态生成物理沙盒目录
    ws.setup_workspace(task.project_id)

    # 3. 写入项目简报到 workspace (如果有)
    if task.project_brief:
        project_dir = ws._get_secure_path(task.project_id) / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "project_brief.md").write_text(
            task.project_brief, encoding="utf-8"
        )

    # 4. 任务状态入库
    task_id = db.push_task(task.project_id, task.prompt, owner_email=owner)

    # 4a. Fast-forward to task-level steps if project planning is already done
    project = db.get_project(task.project_id)
    if project:
        from core.workspace_manager import PROJECT_STEP_SEQUENCE
        raw = project.get("completed_project_steps") or "[]"
        completed_proj = json.loads(raw) if isinstance(raw, str) else raw
        if all(s in completed_proj for s in PROJECT_STEP_SEQUENCE):
            pre_done = ["1"] + list(PROJECT_STEP_SEQUENCE)
            db.advance_step(task_id, "t_plan", pre_done, current_subtask=None)

    # 5. 构造 Response
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=500, detail="Database insertion failed")
        return dict(row)



@router.get("", response_model=List[TaskResponse])
def list_tasks(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager)
):
    """分页获取任务列表"""
    return db.list_tasks(limit, offset, owner_email=owner_filter(user, request))

@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: int,
    request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager)
):
    """查询指定任务的执行状态"""
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        task = dict(row)
    check_read_owner(user, request, task)
    return task

@router.post("/{task_id}/rollback")
def rollback_task(
    task_id: int,
    req: RollbackRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager)
):
    """时光机：基于底层 Git Hash 回滚指定任务的文件系统状态"""
    with db.get_connection() as conn:
        row = conn.execute("SELECT project_id, owner_email FROM tasks WHERE id = ?", (task_id,)).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    check_write_owner(user, dict(row))

    success = ws.rollback(row["project_id"], req.commit_hash)
    if not success:
        raise HTTPException(status_code=400, detail="Rollback failed. Invalid hash or untracked state.")

    return {"success": True, "project_id": row["project_id"], "restored_hash": req.commit_hash}


@router.get("/{task_id}/stream")
async def stream_task_logs(task_id: str):
    """
    Server-Sent Events (SSE) 端点。
    前端通过 EventSource 连接此端点，单向接收沙盒内命令执行的实时日志。
    """
    return StreamingResponse(
        stream_manager.event_generator(task_id),
        media_type="text/event-stream"
    )


@router.get("/{task_id}/steps/{step_id}/output")
def get_step_output(
    task_id: int,
    step_id: str,
    request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager)
):
    """获取已完成步骤的 Outbox_Final 文件内容"""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT project_id, owner_email FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    check_read_owner(user, request, dict(row))

    project_id = row["project_id"]
    final_dir = ws.get_final_path(project_id, step_id)

    if not final_dir.exists():
        raise HTTPException(status_code=404, detail=f"No output found for step {step_id}")

    files = {}
    for item in final_dir.rglob("*"):
        if item.is_file() and item.name != "_snapshot.json":
            rel = str(item.relative_to(final_dir))
            files[rel] = item.read_text(encoding="utf-8", errors="replace")

    return {"step_id": step_id, "files": files}


@router.post("/{task_id}/retry")
def retry_task(
    task_id: int,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Retry a failed task — resets to first task step and PENDING status."""
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    task = dict(row)
    check_write_owner(user, task)

    if task["status"] != TaskStatus.FAILED.value:
        raise HTTPException(status_code=400, detail="Only failed tasks can be retried")

    success = db.retry_task(task_id)
    if not success:
        raise HTTPException(status_code=500, detail="Retry failed")

    # Ensure project is back in planning status and can retry the failed step
    project = db.get_project(task["project_id"])
    if project and project["status"] == "failed":
        # Reset project step to the failed one so scheduler re-runs it
        failed_step = project.get("current_project_step", "2")
        db.reset_project_step(task["project_id"], failed_step)
        from core.scheduler import wake_scheduler
        wake_scheduler()

    return {"status": "retried", "task_id": task_id}


@router.patch("/{task_id}")
def patch_task(
    task_id: int,
    status: str = None,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Update task status."""
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    check_write_owner(user, dict(row))

    if status:
        db.update_task_status(task_id, status)
    with db.get_connection() as conn:
        updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(updated)
