# api/project_routers.py
# REST endpoints for project CRUD, listing, and submission.

from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

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

# Default line window for workspace_file when no range is given — replaces the
# old silent 50000-char truncation with line paging + a `truncated` signal.
_WORKSPACE_FILE_MAX_LINES = 2000


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
    root: str = "dps",
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Return directory tree of a project's workspace.

    ``root`` selects which tree to walk:
      - "dps"  (default): the pipeline staging workspace (step dirs 1/, 2/, ...)
      - "code": the actual generated project code repository (get_code_path)
    """
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_read_owner(user, None, project)

    base = ws.get_code_path(project_id) if root == "code" else ws._get_secure_path(project_id)
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
    return {"project_id": project_id, "root": root, "tree": tree[:200]}


@router.get("/{project_id}/workspace/file")
def workspace_file(
    project_id: str,
    path: str,
    root: str = "dps",
    start_line: int | None = None,
    end_line: int | None = None,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Read a file from the project workspace (path traversal safe).

    ``root`` selects "dps" (pipeline staging) or "code" (project code repo).
    Large files are paged by line: pass ``start_line``/``end_line`` (1-based,
    inclusive) to read a range; the response reports ``total_lines`` and a
    ``truncated`` flag so callers can page rather than be silently cut off.
    """
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_read_owner(user, None, project)

    base = ws.get_code_path(project_id) if root == "code" else ws._get_secure_path(project_id)
    base_resolved = base.resolve()
    target = (base_resolved / path).resolve()
    # Proper path-component containment. str.startswith would allow a sibling
    # dir whose name shares the prefix, e.g. ".../proj" vs ".../proj-evil".
    if not target.is_relative_to(base_resolved):
        raise HTTPException(status_code=403, detail="Path traversal denied")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {path}")

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    start_idx = (start_line - 1) if start_line and start_line > 0 else 0
    start_idx = min(start_idx, total)
    if end_line and end_line > 0:
        end_idx = min(end_line, total)
    else:
        end_idx = min(start_idx + _WORKSPACE_FILE_MAX_LINES, total)
    return {
        "path": path,
        "content": "\n".join(lines[start_idx:end_idx]),
        "start_line": start_idx + 1,
        "end_line": end_idx,
        "total_lines": total,
        "truncated": end_idx < total,
    }


@router.get("/{project_id}/repo/status")
def repo_status(
    project_id: str,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Git snapshot of the project code repo (branch, dirty state, ahead/behind,
    remote, recent commits) for the repository panel. Open read — status is
    metadata only; the archive download and all write ops stay writer-only."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_read_owner(user, None, project)
    return ws.repo_status(project_id)


@router.get("/{project_id}/repo/archive")
def repo_archive(
    project_id: str,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Download the project code repo as a zip (working tree, excluding .git).
    Open to readers (owner-scoped via check_read_owner) — same access surface as
    the workspace file-tree/file-content reads, which already expose every file."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_read_owner(user, None, project)

    code_path = ws.get_code_path(project_id).resolve()
    if not code_path.exists():
        raise HTTPException(status_code=404, detail="Repository not found")

    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(code_path.rglob("*")):
            if ".git" in item.parts:
                continue
            if item.is_file():
                zf.write(item, arcname=str(item.relative_to(code_path)))
    buf.seek(0)

    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in project_id)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'},
    )


# ── Repo write operations (POST → covered by the write-gate middleware) ───────

class RepoRemoteBody(BaseModel):
    url: str
    name: str = "origin"


class RepoCommitBody(BaseModel):
    message: str = Field(min_length=1)


class RepoPushBody(BaseModel):
    branch: Optional[str] = None
    set_upstream: bool = True


class RepoSyncBody(BaseModel):
    branch: str = Field(min_length=1)
    confirm: bool = False
    backup: bool = True


class RepoPRBody(BaseModel):
    title: str = Field(min_length=1)
    body: str = ""
    base: str = "main"
    # head = the feature branch the PR is opened FROM. When push=true, the repo's
    # current HEAD is first pushed to origin/<head> (creating it), so the user
    # can turn their current work into a named branch + PR in one action.
    head: Optional[str] = None
    push: bool = True


def _repo_write_project(project_id: str, user, db):
    """Resolve + ownership-check a project for a repo write action."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    check_write_owner(user, project)
    return project


@router.post("/{project_id}/repo/remote")
def repo_set_remote(
    project_id: str,
    body: RepoRemoteBody,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Add or update the project repo's remote URL."""
    _repo_write_project(project_id, user, db)
    try:
        return ws.repo_set_remote(project_id, body.url, body.name)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{project_id}/repo/commit")
def repo_commit(
    project_id: str,
    body: RepoCommitBody,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Stage all changes and commit them."""
    _repo_write_project(project_id, user, db)
    try:
        return ws.repo_commit(project_id, body.message)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{project_id}/repo/push")
def repo_push(
    project_id: str,
    body: RepoPushBody,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Push the current (or named) branch to origin."""
    _repo_write_project(project_id, user, db)
    try:
        return ws.repo_push(project_id, body.branch, body.set_upstream)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{project_id}/repo/pull")
def repo_pull(
    project_id: str,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Fast-forward pull from the tracked upstream."""
    _repo_write_project(project_id, user, db)
    try:
        return ws.repo_pull(project_id)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{project_id}/repo/sync")
def repo_sync(
    project_id: str,
    body: RepoSyncBody,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Destructive force-sync: reset the working tree to origin/<branch>.

    Requires confirm=true; creates a backup branch first unless backup=false.
    """
    _repo_write_project(project_id, user, db)
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Force-sync discards local commits — resend with confirm=true",
        )
    try:
        return ws.repo_force_sync(project_id, body.branch, body.backup)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{project_id}/repo/pr")
def repo_pr(
    project_id: str,
    body: RepoPRBody,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Open a GitHub pull request for the project repo.

    When ``push`` is true (default) and ``head`` is given, the current HEAD is
    first pushed to ``origin/<head>`` (creating the remote feature branch), then
    a PR is opened from ``head`` into ``base``.
    """
    _repo_write_project(project_id, user, db)
    from core.git_ops import create_github_pr
    code_path = ws.get_code_path(project_id)
    # Validate head != base BEFORE pushing — otherwise a head==base==main request
    # would push HEAD straight onto the base branch and only then fail the PR.
    if body.head and body.head == body.base:
        raise HTTPException(
            status_code=400,
            detail=f"head and base are the same branch ('{body.base}') — pick a "
                   "different feature branch to open the PR from")
    try:
        if body.push:
            if not body.head:
                raise HTTPException(
                    status_code=400,
                    detail="head (the branch to push your work to) is required "
                           "when push=true")
            # set_upstream=False: this maps the current HEAD onto a differently
            # named remote branch; rebinding the local branch's tracking ref to it
            # would make a later Pull fast-forward from the wrong branch.
            ws.repo_push_head(project_id, body.head, set_upstream=False)
        return create_github_pr(
            code_path, body.title, body.body, body.base, body.head)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


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
            # Guard against the silent-deadlock case: if the run was pointed at a
            # step that no longer exists in the (possibly edited) graph — e.g. a
            # node removed since the run started — advance_run() returns None
            # forever and the run wedges. Detect it, revert to a clean failed
            # state, and tell the user to start fresh. (Newer skillflow also
            # rejects this in reactivate_run → caught as ValueError below; this
            # post-check keeps the guard working with the pinned skillflow too.)
            after = sf.get_run(run["id"]) or {}
            node = after.get("current_node")
            if node and sf._get_resolver(
                    after.get("graph_name", "")).get_node(node) is None:
                with sf._conn:
                    sf._conn.execute(
                        "UPDATE skillflow_runs SET status = 'failed', "
                        "updated_at = datetime('now') WHERE id = ?", (run["id"],))
                raise HTTPException(
                    status_code=409,
                    detail=(f"Cannot retry: this run's resume step '{node}' was "
                            f"removed from the pipeline since the run started. "
                            f"Start a new project instead."))
    except HTTPException:
        raise
    except ValueError as e:
        # skillflow.reactivate_run rejected an unrecoverable resume — surface it
        # instead of swallowing (it would otherwise wedge with advance_run None).
        raise HTTPException(status_code=409, detail=str(e))
    except Exception:
        pass  # other failures best-effort; scheduler will still pick up reset tasks

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
    from core.project_submit import seed_and_trigger

    owner = user.email if user else "cli@local"

    # Clear the drafting gate so the scheduler can pick up this project.
    # Projects created via meta conversation have meta_state='drafting' until
    # the user approves the brief checkpoint (which calls this submit endpoint).
    db.set_project_meta_state(body.project_id, None)

    existing = db.get_project(body.project_id)

    # ── Project already exists (e.g. created via /new) ──
    if existing:
        return seed_and_trigger(db, ws, body.project_id, body.brief)

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

    # 5-8. Seed brief + step-1 goals, mark planning step 1 complete, wake the
    # scheduler. Shared with the existing-project path and the chat butler.
    return seed_and_trigger(db, ws, body.project_id, body.brief)
