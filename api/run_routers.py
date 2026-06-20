# api/run_routers.py
# REST endpoints for pipeline run history and execution traces.
# Bridges skillflow's run/trace query APIs to HTTP.

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Optional
from pydantic import BaseModel
from api.dependencies import (
    get_skillflow, get_db_manager, get_config_registry, owner_filter,
    get_workspace_manager,
)
from api.auth import CurrentUser, get_optional_user
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager

router = APIRouter(prefix="/api", tags=["Runs & Traces"])


# ── Start a run of any config ─────────────────────────────────────────

class StartRunRequest(BaseModel):
    config_name: str
    project_id: Optional[str] = None     # generated when omitted
    name: Optional[str] = None
    seed_text: Optional[str] = None      # written to the config's seed_file
    priority: int = 0


@router.post("/runs", status_code=201)
def start_run(
    body: StartRunRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
    registry=Depends(get_config_registry),
):
    """Start a run of any registered config (generic launch path)."""
    if registry.get(body.config_name) is None:
        raise HTTPException(404, f"Config '{body.config_name}' not found")
    from core.run_launcher import start_config_run, generate_run_id
    owner = user.email if user else "cli@local"
    project_id = body.project_id or generate_run_id(body.config_name)
    result = start_config_run(
        db, ws, body.config_name, project_id,
        seed_text=body.seed_text, name=body.name,
        owner_email=owner, priority=body.priority,
    )
    if result.get("status") == "error":
        raise HTTPException(400, result.get("message", "failed to start run"))
    return result


# ── Run listing (all configs) ────────────────────────────────────────

@router.get("/runs")
def list_all_runs(
    config_name: Optional[str] = Query(None, description="Filter by config name"),
    status: Optional[str] = Query(None, description="Filter by status prefix (e.g. running)"),
    request: Request = None,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    registry=Depends(get_config_registry),
):
    """List config runs across all configs (newest first), with config label and
    step labels attached so any config renders generically."""
    owner = owner_filter(user, request)
    rows = db.list_projects_with_stats(owner_email=owner)
    out = []
    for r in rows:
        cfg = r.get("config_name") or "dpe_default_v2"
        if config_name and cfg != config_name:
            continue
        if status and (r.get("status") or "").split(":")[0] != status:
            continue
        m = registry.get(cfg)
        r["config_label"] = m.label if m else cfg
        r["has_task_loop"] = bool(m and m.has_task_loop)
        out.append(r)
    return {"runs": out}


# ── Run listing (per project) ────────────────────────────────────────

@router.get("/projects/{project_id}/runs")
def list_project_runs(
    project_id: str,
    status: Optional[str] = Query(None, description="Filter by run status"),
    request: Request = None,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """List all pipeline runs for a project, newest first."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    sf = get_skillflow()
    runs = sf.list_runs(project_id, status=status)

    # Enrich each run with step summary
    enriched = []
    for r in runs:
        steps = sf.get_steps(r["id"])
        r["steps"] = [
            {
                "step_id": s["step_id"],
                "status": s["status"],
                "attempt": s.get("attempt", 1),
                "error": s.get("error", ""),
            }
            for s in steps
        ]
        r["step_count"] = len(steps)
        r["completed_steps"] = sum(1 for s in steps if s["status"] == "completed")
        r["failed_steps"] = sum(1 for s in steps if s["status"] == "failed")
        enriched.append(r)

    return {"project_id": project_id, "runs": enriched}


# ── Helpers ────────────────────────────────────────────────────────────

def _resolve_run(run_id: str) -> dict | None:
    """Resolve a run by internal UUID or human-readable project_id.

    Tries UUID first (skillflow internal id), then falls back to
    project_id (most recent run for that project).
    """
    sf = get_skillflow()
    run = sf.get_run(run_id)
    if run:
        return run
    runs = sf.list_runs(project_id=run_id)
    return runs[0] if runs else None


# ── Single run detail ─────────────────────────────────────────────────

@router.get("/runs/{run_id}")
def get_run_detail(
    run_id: str,
    user: CurrentUser | None = Depends(get_optional_user),
    registry=Depends(get_config_registry),
):
    """Get full run detail including all step instances + config manifest.

    ``run_id`` accepts both a skillflow internal UUID and a human-readable
    project_id (e.g. ``aitelier-web-ui-2``).
    """
    sf = get_skillflow()
    run = _resolve_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    internal_id = run["id"]

    # Attach config identity + manifest so the client can render labels/checkpoints
    # for any config without hardcoding the DPE step set.
    cfg = run.get("graph_name") or "dpe_default_v2"
    run["config_name"] = cfg
    manifest = registry.get(cfg)
    run["manifest"] = manifest.to_dict() if manifest else None

    steps = sf.get_steps(internal_id)
    run["steps"] = [
        {
            "step_id": s["step_id"],
            "status": s["status"],
            "attempt": s.get("attempt", 1),
            "error": s.get("error", ""),
            "created_at": s.get("created_at", ""),
            "updated_at": s.get("updated_at", ""),
        }
        for s in steps
    ]
    run["step_count"] = len(steps)
    run["completed_steps"] = sum(1 for s in steps if s["status"] == "completed")
    run["failed_steps"] = sum(1 for s in steps if s["status"] == "failed")
    return run


# ── Execution trace ───────────────────────────────────────────────────

@router.get("/runs/{run_id}/trace")
def get_run_trace(
    run_id: str,
    step_instance_id: Optional[int] = Query(None, description="Filter by step instance ID (int)"),
    category: Optional[str] = Query(None, description="Filter by trace category (prompt, response, tool_call, error)"),
    limit: int = Query(100, ge=1, le=1000, description="Max trace entries to return"),
    user: CurrentUser | None = Depends(get_optional_user),
):
    """
    Read durable execution traces for a pipeline run.

    Returns prompt/response pairs, tool calls, and errors recorded during
    pipeline execution. Optionally filter by step_instance_id and/or category.
    """
    sf = get_skillflow()
    run = _resolve_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    internal_id = run["id"]

    traces = sf.get_trace(internal_id, step_instance_id=step_instance_id, category=category)

    # get_trace may return all if no filters given; apply limit
    if len(traces) > limit:
        traces = traces[-limit:]

    return {
        "run_id": run_id,
        "step_instance_id_filter": step_instance_id,
        "category_filter": category,
        "count": len(traces),
        "traces": traces,
    }


# ── Run-id-keyed checkpoints ──────────────────────────────────────────
# Config-agnostic checkpoint routes keyed by run_id. They resolve run_id →
# project_id (the run key) and delegate to the canonical project-keyed handlers
# in meta_routers, so there is ONE implementation behind two routes.

from api.meta_routers import (  # noqa: E402  (avoids a module-load cycle)
    CheckpointResponse,
    CheckpointApprovalRequest,
    CheckpointRejectionRequest,
)


def _run_to_project_id(run_id: str) -> str:
    run = _resolve_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run["project_id"]


@router.get("/runs/{run_id}/checkpoint", response_model=CheckpointResponse)
def get_run_checkpoint(
    run_id: str,
    request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Pending checkpoint for a run (delegates to the project-keyed handler)."""
    from api.meta_routers import get_pending_checkpoint
    return get_pending_checkpoint(_run_to_project_id(run_id), request, user, db)


@router.post("/runs/{run_id}/checkpoint/approve")
def approve_run_checkpoint(
    run_id: str,
    body: CheckpointApprovalRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Approve a run's checkpoint (delegates to the project-keyed handler)."""
    from api.meta_routers import approve_checkpoint
    return approve_checkpoint(_run_to_project_id(run_id), body, user, db)


@router.post("/runs/{run_id}/checkpoint/reject")
def reject_run_checkpoint(
    run_id: str,
    body: CheckpointRejectionRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Reject a run's checkpoint (delegates to the project-keyed handler)."""
    from api.meta_routers import reject_checkpoint
    return reject_checkpoint(_run_to_project_id(run_id), body, user, db)
