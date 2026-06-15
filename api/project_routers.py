# api/project_routers.py
# REST endpoints for project CRUD, listing, and submission.

from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request

# ── helpers ────────────────────────────────────────────────────────

def _add_task_summary(project: dict) -> dict:
    """Add a task_summary nested dict alongside flat fields for consumers."""
    project["task_summary"] = {
        "total": project.get("task_count", 0),
        "completed": project.get("completed_count", 0),
        "running": project.get("running_count", 0),
        "failed": project.get("failed_count", 0),
        "pending": project.get("pending_count", 0),
    }
    return project


def _add_task_summaries(projects: list[dict]) -> list[dict]:
    return [_add_task_summary(p) for p in projects]
from pydantic import BaseModel, Field
from typing import Optional
from models.schemas import ProjectCreate, ProjectResponse, ProjectWithStats
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager
from api.dependencies import get_db_manager, get_workspace_manager, owner_filter, check_write_owner, check_read_owner, enrich_project_status
from api.auth import CurrentUser, get_optional_user

router = APIRouter(prefix="/api/projects", tags=["Projects"])


@router.get("", response_model=list[ProjectWithStats])
def list_projects(
    request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager)
):
    """List all projects with aggregated task stats."""
    projects = db.list_projects_with_stats(owner_email=owner_filter(user, request))
    projects = [enrich_project_status(p) or p for p in projects]
    return _add_task_summaries(projects)


@router.post("", response_model=ProjectResponse, status_code=201)
def create_project(
    body: ProjectCreate,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager)
):
    """Create a new project. Returns 409 if project_id already exists."""
    existing = db.get_project(body.project_id)
    if existing:
        raise HTTPException(status_code=409, detail="Project already exists")

    # Validate repo inputs
    if body.repo_type == "existing":
        if not body.repo_path:
            raise HTTPException(status_code=400, detail="repo_path is required for repo_type='existing'")
        p = Path(body.repo_path).resolve()
        if not p.exists():
            raise HTTPException(status_code=400, detail=f"repo_path does not exist: {body.repo_path}")
        if not (p / ".git").exists():
            raise HTTPException(status_code=400, detail=f"repo_path is not a git repository: {body.repo_path}")
    elif body.repo_type == "clone":
        if not body.repo_url:
            raise HTTPException(status_code=400, detail="repo_url is required for repo_type='clone'")

    # Compute local repo_path for new/clone types
    repo_path = body.repo_path
    if body.repo_type == "new" and not repo_path:
        projects_base = Path.home() / ".AItelier" / "projects"
        repo_path = str(projects_base / body.project_id)
    elif body.repo_type == "clone" and not repo_path:
        projects_base = Path.home() / ".AItelier" / "projects"
        repo_path = str(projects_base / body.project_id)

    owner = user.email if user else "cli@local"

    # Create project in DB
    project = db.ensure_project(
        body.project_id, name=body.name, owner_email=owner,
        repo_type=body.repo_type, repo_path=repo_path, repo_url=body.repo_url
    )

    # Setup workspace (creates DPS dirs + project repo)
    ws.setup_workspace(
        body.project_id,
        repo_type=body.repo_type,
        repo_path=repo_path,
        repo_url=body.repo_url,
    )

    return project


@router.get("/{project_id}", response_model=ProjectWithStats)
def get_project(
    project_id: str,
    request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager)
):
    """Get a single project with aggregated stats."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_read_owner(user, request, project)

    owner_f = owner_filter(user, request)
    all_projects = db.list_projects_with_stats(owner_email=owner_f)
    for p in all_projects:
        if p["project_id"] == project_id:
            p = enrich_project_status(p) or p
            return _add_task_summary(p)
    raise HTTPException(status_code=404, detail="Project not found")


@router.patch("/{project_id}", response_model=ProjectResponse)
def patch_project(
    project_id: str,
    name: str = None,
    brief: str = None,
    priority: int = None,
    status: str = None,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager)
):
    """Partially update a project."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_write_owner(user, project)
    db.update_project(project_id, name=name, brief=brief, priority=priority, status=status)
    return db.get_project(project_id)


@router.get("/{project_id}/tasks")
def list_project_tasks(
    project_id: str,
    request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager)
):
    """List all tasks for a project."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_read_owner(user, request, project)
    return db.list_tasks_by_project(project_id, owner_email=owner_filter(user, request))


@router.get("/{project_id}/workspace/tree")
def workspace_tree(
    project_id: str,
    subdir: str = None,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Return directory tree of a project's DPS workspace."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_read_owner(user, None, project)

    base = ws._get_secure_path(project_id)
    if subdir:
        base = base / subdir
    if not base.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")

    tree = []
    for item in sorted(base.rglob("*")):
        # Skip .git internals
        if ".git" in item.parts:
            continue
        if item.is_file():
            tree.append(str(item.relative_to(base)))
    return {"project_id": project_id, "tree": tree[:200]}


@router.get("/{project_id}/workspace/file")
def workspace_file(
    project_id: str,
    path: str,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Read a file from the project workspace (path traversal safe)."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_read_owner(user, None, project)

    base = ws._get_secure_path(project_id)
    target = (base / path).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise HTTPException(status_code=403, detail="Path traversal denied")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {path}")

    content = target.read_text(encoding="utf-8", errors="replace")
    return {"path": path, "content": content[:50000]}


@router.delete("/{project_id}")
def delete_project(
    project_id: str,
    cascade: bool = True,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager)
):
    """Delete a project. cascade=True (default) deletes tasks, subtasks, and workspace."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_write_owner(user, project)
    if cascade:
        db.delete_project_cascade(project_id)
    else:
        db.delete_project(project_id)
    return {"success": True}


@router.post("/{project_id}/refresh-planning")
def refresh_planning(
    project_id: str,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager)
):
    """
    Manually trigger a refresh of project-level planning (P1.5 + P2).
    Re-queues P1.5 and P2 for re-execution. Project transitions to 'planning'
    status, runs the refresh, then returns to 'executing'.
    """
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_write_owner(user, project)

    completed = project.get("completed_project_steps", "[]")
    if isinstance(completed, str):
        import json
        completed = json.loads(completed)

    # Remove P1.5 and P2 from completed so they re-run
    for step in ("1", "2"):
        if step in completed:
            completed.remove(step)

    db.set_completed_project_steps(project_id, completed)

    return {"status": "refreshing", "project_id": project_id, "steps_to_rerun": ["1", "2"]}


@router.post("/{project_id}/retry")
def retry_project(
    project_id: str,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager)
):
    """
    Retry a failed project.
    - If planning failed: restart from the failed project step.
    - If execution failed (tasks): skip planning, go directly to executing.
    """
    import json
    from models.schemas import TaskStatus
    from core.scheduler import wake_scheduler

    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_write_owner(user, project)

    # Enrich with skillflow pipeline status (source of truth)
    from api.dependencies import enrich_project_status
    project = enrich_project_status(project) or project

    # Accept both raw DB "failed" and enriched "running:{node}" (skillflow
    # run was already reactivated). Check the raw DB column for the definitive
    # answer — enrichment overrides failed→running:node when reactivate was called.
    raw_status = db.get_project(project_id).get("status", "")
    if not raw_status.startswith("failed"):
        raise HTTPException(status_code=400, detail="Only failed projects can be retried")

    # Clear project meta_state (error info)
    db.set_project_meta_state(project_id, None)

    # Determine failure phase from meta_state
    planning_complete = db.is_project_planning_complete(project_id)

    if not planning_complete:
        # Planning failed — restart from the failed project step
        failed_step = project.get("current_project_step", "1")
        db.reset_project_step(project_id, failed_step)

    # Reset ALL tasks (failed OR pending) on this project to pending
    with db.get_connection() as conn:
        from core.workspace_manager import TASK_STEP_SEQUENCE
        first_task_step = TASK_STEP_SEQUENCE[0] if TASK_STEP_SEQUENCE else "t_plan"
        conn.execute(
            "UPDATE tasks SET status = ?, current_step = ?, completed_steps = '[]', task_meta_state = NULL "
            "WHERE project_id = ? AND status IN ('failed', 'pending')",
            (TaskStatus.PENDING.value, first_task_step, project_id)
        )
        conn.commit()

    # NB-5: explicitly reactivate the skillflow run here. The scheduler no longer
    # auto-reactivates failed runs (that caused runaway/aborted runs to resume on
    # every tick), so retry must reactivate the run itself.
    try:
        from api.dependencies import get_skillflow
        sf = get_skillflow()
        run = sf.get_run_by_project(project_id)
        if run and run.get("status") == "failed":
            sf.reactivate_run(run["id"])
    except Exception:
        pass  # best-effort; scheduler will still pick up reset tasks

    wake_scheduler()

    phase = "executing" if planning_complete else "planning"
    return {"status": "retried", "project_id": project_id, "phase": phase}


# ── Submit endpoints (Meta Agent → DPE bridge) ──

class SubmitProjectRequest(BaseModel):
    project_id: str = Field(..., min_length=1)
    name: Optional[str] = None
    brief: dict = Field(..., description="Project brief from meta conversation (nominator format)")
    repo_type: Optional[str] = "new"
    repo_path: Optional[str] = None
    repo_url: Optional[str] = None


class SubmitTaskRequest(BaseModel):
    project_id: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    task_spec: Optional[dict] = None


@router.post("/submit", status_code=201)
def submit_project(
    body: SubmitProjectRequest,
    request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """
    Submit a project with a brief from meta conversation.
    If project doesn't exist yet, creates it (fresh submit).
    If project already exists (e.g. created via /new), seeds brief and goals into it.
    In both cases, sets status to 'planning' and wakes the scheduler.
    """
    import json
    from core.meta_conversation import format_brief_as_markdown, brief_to_step1_goals
    from core.scheduler import wake_scheduler

    owner = user.email if user else "cli@local"

    # Clear the drafting gate so the scheduler can pick up this project.
    # Projects created via meta conversation have meta_state='drafting' until
    # the user approves the brief checkpoint (which calls this submit endpoint).
    db.set_project_meta_state(body.project_id, None)

    existing = db.get_project(body.project_id)

    # ── Project already exists (e.g. created via /new) ──
    if existing:
        # Check if planning has already completed — don't reset if so
        raw = existing.get("completed_project_steps", "[]")
        existing_steps = json.loads(raw) if isinstance(raw, str) else raw
        planning_done = all(s in existing_steps for s in ["1", "2", "3"])

        if planning_done:
            # Planning already done — don't re-trigger the planning pipeline
            return {"status": "already_planned", "project_id": body.project_id}

        # Skip project + workspace creation, just seed brief and goals
        dps_path = ws._get_secure_path(body.project_id)

        brief_md = format_brief_as_markdown(body.brief)
        db.set_project_brief(body.project_id, brief_md)

        project_dir = dps_path / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "project_brief.md").write_text(brief_md, encoding="utf-8")

        goals = brief_to_step1_goals(body.brief)
        goals_json = json.dumps(goals, indent=2, ensure_ascii=False)
        final_1_dir = ws._final_dir(body.project_id, "1")
        final_1_dir.mkdir(parents=True, exist_ok=True)
        (final_1_dir / "step1_goals.json").write_text(goals_json, encoding="utf-8")

        db.set_completed_project_steps(body.project_id, ["1"])
        wake_scheduler()

        return {"status": "submitted", "project_id": body.project_id, "next_step": "1"}

    # ── New project (full creation) ──

    # 2. Validate repo inputs
    if body.repo_type == "existing":
        if not body.repo_path:
            raise HTTPException(status_code=400, detail="repo_path is required for repo_type='existing'")
        p = Path(body.repo_path).resolve()
        if not p.exists():
            raise HTTPException(status_code=400, detail=f"repo_path does not exist: {body.repo_path}")
        if not (p / ".git").exists():
            raise HTTPException(status_code=400, detail=f"repo_path is not a git repository: {body.repo_path}")
    elif body.repo_type == "clone":
        if not body.repo_url:
            raise HTTPException(status_code=400, detail="repo_url is required for repo_type='clone'")

    # Compute local repo_path for new/clone types
    repo_path = body.repo_path
    if body.repo_type == "new" and not repo_path:
        projects_base = Path.home() / ".AItelier" / "projects"
        repo_path = str(projects_base / body.project_id)
    elif body.repo_type == "clone" and not repo_path:
        projects_base = Path.home() / ".AItelier" / "projects"
        repo_path = str(projects_base / body.project_id)

    # 3. Create project in DB
    db.ensure_project(
        body.project_id,
        name=body.name or body.brief.get("project_name", body.project_id),
        owner_email=owner,
        repo_type=body.repo_type, repo_path=repo_path, repo_url=body.repo_url,
    )

    # 4. Setup workspace
    ws.setup_workspace(
        body.project_id,
        repo_type=body.repo_type, repo_path=repo_path, repo_url=body.repo_url,
    )

    # 5. Write brief markdown to DB + workspace
    brief_md = format_brief_as_markdown(body.brief)
    db.set_project_brief(body.project_id, brief_md)

    dps_path = ws._get_secure_path(body.project_id)
    project_dir = dps_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "project_brief.md").write_text(brief_md, encoding="utf-8")

    # 6. Seed step1_goals.json from brief (populates Step 1 / Nominator output)
    goals = brief_to_step1_goals(body.brief)
    goals_json = json.dumps(goals, indent=2, ensure_ascii=False)
    final_1_dir = ws._final_dir(body.project_id, "1")
    final_1_dir.mkdir(parents=True, exist_ok=True)
    (final_1_dir / "step1_goals.json").write_text(goals_json, encoding="utf-8")

    # 7. Mark Step 1 (goals) as completed, set project to planning at step 1_5
    db.set_completed_project_steps(body.project_id, ["1"])

    # 8. Wake scheduler for immediate pickup
    wake_scheduler()

    return {
        "status": "submitted",
        "project_id": body.project_id,
        "next_step": "1",
    }


@router.post("/submit-task", status_code=201)
def submit_task(
    body: SubmitTaskRequest,
    request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """
    Submit a task to an existing project with planning already done.
    Creates a task record and wakes the scheduler.
    If project planning is already done, fast-forwards to task-level steps.
    """
    import json
    from core.scheduler import wake_scheduler
    from core.workspace_manager import PROJECT_STEP_SEQUENCE

    project = db.get_project(body.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Enrich with skillflow pipeline status (source of truth)
    from api.dependencies import enrich_project_status
    project = enrich_project_status(project) or project

    owner = user.email if user else "cli@local"

    prompt = body.prompt
    if body.task_spec:
        from core.meta_conversation import format_task_spec_as_prompt
        prompt = format_task_spec_as_prompt(body.task_spec)

    task_id = db.push_task(body.project_id, prompt, owner_email=owner)

    # Fast-forward to task-level steps if project planning is done
    completed_proj = json.loads(project.get("completed_project_steps") or "[]") if isinstance(project.get("completed_project_steps"), str) else (project.get("completed_project_steps") or [])
    if all(s in completed_proj for s in PROJECT_STEP_SEQUENCE):
        pre_done = ["1"] + list(PROJECT_STEP_SEQUENCE)
        db.advance_step(task_id, "t_plan", pre_done, current_subtask=None)
        # If project is paused at a checkpoint, auto-approve it
        if project.get("status") == "paused":
            from api.dependencies import get_skillflow
            sf = get_skillflow()
            run = sf.get_run_by_project(body.project_id)
            if run:
                sf.resume_run(run["id"])
            db.set_project_meta_state(body.project_id, "")

    wake_scheduler()

    return {
        "status": "submitted",
        "task_id": task_id,
        "project_id": body.project_id,
    }
