# File: api/routers.py

import re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json
from models.schemas import TaskCreate, TaskResponse, TaskStatus
from typing import List
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager
from api.dependencies import get_db_manager, get_workspace_manager, owner_filter, check_write_owner, check_read_owner
from api.auth import CurrentUser, get_optional_user, creator_email
from api.sse_manager import stream_manager

# A step id is a graph node name — letters, digits, underscore, dash, dot as a
# separator inside names like "1_5". Never a path separator, never a bare dot.
_STEP_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")

# Ceilings for one step-output read: per file, and for the response as a whole.
_STEP_FILE_MAX_BYTES = 2 * 1024 * 1024
_STEP_TOTAL_MAX_BYTES = 16 * 1024 * 1024

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
    owner = user.email if user else (creator_email(request) or "cli@local")

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

    # `step_id` is interpolated into a filesystem path with no validation, so
    # "." walked out of the intended step directory into the workspace root
    # (confirmed: `GET .../steps/./output` returned 200 instead of 404), and
    # ".." is reachable percent-encoded because uvicorn decodes before routing.
    # The read below then rglobs and read_text()s EVERYTHING it finds — pointed
    # at the workspace root that is ~56MB, and the dpe_game 5_compile dirs are
    # ~100MB of PNG frames each. A step id is a graph node name; anything that
    # is not one is a probe, not a typo.
    if not _STEP_ID_RE.fullmatch(step_id):
        raise HTTPException(status_code=404, detail=f"No output found for step {step_id}")

    project_id = row["project_id"]
    final_dir = ws.get_final_path(project_id, step_id)

    if not final_dir.exists():
        raise HTTPException(status_code=404, detail=f"No output found for step {step_id}")

    files = {}
    skipped = []
    total = 0
    for item in sorted(final_dir.rglob("*")):
        if not item.is_file() or item.name == "_snapshot.json":
            continue
        rel = str(item.relative_to(final_dir))
        # Bounded, and it says what it left out. Reading a whole step directory
        # into one JSON response is unbounded by construction — a step that
        # renders frames or vendors a dependency turns one GET into hundreds of
        # megabytes of str. Reporting the skips matters more than the cap: a
        # silent truncation reads as "that is all there was".
        size = item.stat().st_size
        if size > _STEP_FILE_MAX_BYTES or total + size > _STEP_TOTAL_MAX_BYTES:
            skipped.append({"path": rel, "bytes": size})
            continue
        total += size
        files[rel] = item.read_text(encoding="utf-8", errors="replace")

    return {"step_id": step_id, "files": files, "skipped": skipped}


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
