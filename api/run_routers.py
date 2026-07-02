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
from api._cache_stats import compute_cache_stats_per_step, compute_cache_stats_batch

router = APIRouter(prefix="/api", tags=["Runs & Traces"])


# ── Start a run of any config ─────────────────────────────────────────

class StartRunRequest(BaseModel):
    config_name: str
    project_id: Optional[str] = None     # generated when omitted
    name: Optional[str] = None
    seed_text: Optional[str] = None      # written to the config's seed_file
    priority: int = 0
    repo_type: str = "new"               # 'new' | 'existing' | 'clone'
    repo_url: Optional[str] = None       # required for 'clone'
    repo_path: Optional[str] = None      # required for 'existing'


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
        repo_type=body.repo_type, repo_url=body.repo_url,
        repo_path=body.repo_path,
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

    # Attach cache hit ratio stats per run (batch query)
    sf = get_skillflow()
    pid_to_uuid = {}
    for r in out:
        pid = r["project_id"]
        runs = sf.list_runs(project_id=pid)
        if runs:
            pid_to_uuid[pid] = runs[0]["id"]
    uuid_list = list(pid_to_uuid.values())
    if uuid_list:
        batch_stats = compute_cache_stats_batch(uuid_list)
        for r in out:
            uuid = pid_to_uuid.get(r["project_id"])
            r["cache_stats"] = batch_stats.get(uuid)  # None if no trace data
    else:
        for r in out:
            r["cache_stats"] = None

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
            # Instance id: step_id alone is NOT unique — task-loop steps
            # (t_plan, t_impl, …) repeat per task, and a keyed {#each} on
            # duplicates crashes Svelte 5 outright.
            "id": s["id"],
            "step_id": s["step_id"],
            "status": s["status"],
            # skillflow_steps has no "attempt" column; retries live in
            # retry_count (+ validation_retry_count). Expose them so the UI can
            # show how many times a step was retried (attempt = retry_count + 1).
            "retry_count": s.get("retry_count", 0) or 0,
            "validation_retry_count": s.get("validation_retry_count", 0) or 0,
            "attempt": (s.get("retry_count", 0) or 0) + 1,
            "error": s.get("error", "") or s.get("last_error", "") or "",
            "created_at": s.get("created_at", ""),
            "updated_at": s.get("updated_at", ""),
        }
        for s in steps
    ]
    run["step_count"] = len(steps)
    run["completed_steps"] = sum(1 for s in steps if s["status"] == "completed")
    run["failed_steps"] = sum(1 for s in steps if s["status"] == "failed")

    # Compute cache hit ratio stats from skillflow_trace
    per_step = compute_cache_stats_per_step(internal_id)
    total_hit = sum(v["cache_hit_tokens"] for v in per_step.values())
    total_miss = sum(v["cache_miss_tokens"] for v in per_step.values())
    total = total_hit + total_miss
    run_hit_ratio = total_hit / total if total > 0 else None
    run["cache_stats"] = {
        "cache_hit_tokens": total_hit,
        "cache_miss_tokens": total_miss,
        "hit_ratio": run_hit_ratio,
        "total_tokens": total,
    }
    run["cache_stats_by_step"] = per_step
    return run


# ── Execution trace ───────────────────────────────────────────────────

@router.get("/runs/{run_id}/trace")
def get_run_trace(
    run_id: str,
    step_instance_id: Optional[int] = Query(None, description="Filter by step instance ID (int)"),
    category: Optional[str] = Query(None, description="Filter by trace category (prompt, response, tool_call, error)"),
    after_seq: Optional[int] = Query(None, description="Keyset cursor: the previous page's next_seq (omit for the first page)"),
    order: str = Query("asc", pattern="^(asc|desc)$", description="Chronological order: 'asc' = oldest first, 'desc' = newest first"),
    limit: int = Query(100, ge=1, le=1000, description="Max trace entries per page"),
    user: CurrentUser | None = Depends(get_optional_user),
):
    """
    Read durable execution traces for a pipeline run.

    Keyset-paginated on ``seq`` (monotonic, unique per run): the first page omits
    ``after_seq``; each subsequent page passes the previous page's ``next_seq``.
    ``order`` controls direction — ``asc`` pages oldest→newest, ``desc`` pages
    newest→oldest (the cursor is fed to the matching seq bound server-side, so
    the client treats ``next_seq`` as opaque either way). This is stateless — no
    server-side cursor/cache. ``has_more`` tells the client whether another page
    exists.
    """
    sf = get_skillflow()
    run = _resolve_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    internal_id = run["id"]

    # Keyset pagination on `seq` via skillflow's get_trace (LIMIT in SQL). The
    # opaque cursor maps to seq > after_seq when ascending and seq < before_seq
    # when descending. Fetch one extra row to detect whether a further page
    # exists. Descending order needs skillflow-py with order/before_seq support
    # (>=1.1.8); against an older build we degrade to ascending instead of 500ing.
    import inspect
    supports_desc = "order" in inspect.signature(sf.get_trace).parameters
    effective_order = order if supports_desc else "asc"
    if effective_order == "desc":
        cursor_kwarg = {"order": "desc", "before_seq": after_seq}
    elif supports_desc:
        cursor_kwarg = {"order": "asc", "after_seq": after_seq}
    else:
        cursor_kwarg = {"after_seq": after_seq}
    page = sf.get_trace(
        internal_id, step_instance_id=step_instance_id, category=category,
        limit=limit + 1, **cursor_kwarg,
    )
    has_more = len(page) > limit
    traces = page[:limit]
    next_seq = traces[-1]["seq"] if traces else None

    return {
        "run_id": run_id,
        "step_instance_id_filter": step_instance_id,
        "category_filter": category,
        "count": len(traces),
        "traces": traces,
        "next_seq": next_seq,
        "has_more": has_more,
        "order": effective_order,
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
