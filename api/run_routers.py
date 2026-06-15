# api/run_routers.py
# REST endpoints for pipeline run history and execution traces.
# Bridges skillflow's run/trace query APIs to HTTP.

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Optional
from api.dependencies import get_skillflow, get_db_manager
from api.auth import CurrentUser, get_optional_user
from core.db_manager import DBManager

router = APIRouter(prefix="/api", tags=["Runs & Traces"])


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


# ── Single run detail ─────────────────────────────────────────────────

@router.get("/runs/{run_id}")
def get_run_detail(
    run_id: str,
    user: CurrentUser | None = Depends(get_optional_user),
):
    """Get full run detail including all step instances."""
    sf = get_skillflow()
    run = sf.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    steps = sf.get_steps(run_id)
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
    run = sf.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    traces = sf.get_trace(run_id, step_instance_id=step_instance_id, category=category)

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
