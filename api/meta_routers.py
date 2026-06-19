# api/meta_routers.py
# Stateless REST endpoints for meta conversation agents.
# Client owns conversation history and sends it with each request.
# Auth-optional: ownership checks are no-ops when user=None (CLI mode).

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional

from models.schemas import InteractionMeta
from core.db_manager import DBManager
from core.meta_conversation import MetaConversationAgent, TaskMetaConversationAgent, detect_intent
from core.interaction_meta import (
    for_assessment_asking,
    for_brief_review,
    for_meta_conversation_asking,
    for_task_meta_asking,
    for_task_meta_complete,
    for_checkpoint_waiting,
)
from api.dependencies import get_db_manager, get_workspace_manager, get_skillflow, check_write_owner, check_read_owner
from api.auth import CurrentUser, get_optional_user
from api.sse_manager import stream_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/meta", tags=["Meta Conversation"])


# ── Request / Response models ──

class HistoryTurn(BaseModel):
    message: str
    answer: str


class MetaStartRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    project_id: str


class MetaNextRequest(BaseModel):
    project_id: str
    history: list[HistoryTurn] = []
    answer: str = Field(..., min_length=1, max_length=4000)


class MetaForceRequest(BaseModel):
    project_id: str
    history: list[HistoryTurn] = []


class MetaResponse(BaseModel):
    status: str
    message: Optional[str] = None
    analysis_so_far: Optional[str] = None
    project_brief: Optional[dict] = None
    interaction: Optional[InteractionMeta] = None


class ReviseBriefRequest(BaseModel):
    project_id: str
    project_brief: dict
    feedback: str = Field(..., min_length=1, max_length=4000)


# ── Intent detection ──

class IntentDetectRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)


class IntentDetectResponse(BaseModel):
    intent: str  # "new_project" | "existing_code" | "unclear"
    reasoning: Optional[str] = None


@router.post("/detect-intent", response_model=IntentDetectResponse)
def meta_detect_intent(
    request: IntentDetectRequest,
    user: CurrentUser | None = Depends(get_optional_user),
):
    """Detect whether a user prompt is about a new project or existing code."""
    result = detect_intent(request.prompt)
    return IntentDetectResponse(
        intent=result["intent"],
        reasoning=result.get("reasoning"),
    )


# ── Pre-project assessment (no project_id required) ──

class AssessRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    history: list[HistoryTurn] = []


class AssessResponse(BaseModel):
    status: str
    message: Optional[str] = None
    intent: Optional[str] = None
    project_brief: Optional[dict] = None
    interaction: Optional[InteractionMeta] = None


@router.post("/assess", response_model=AssessResponse)
def meta_assess(
    request: AssessRequest,
    user: CurrentUser | None = Depends(get_optional_user),
):
    """Unified pre-project assessment: validate prompt, detect intent, gather brief.
    No project_id required — used before project creation."""
    try:
        agent = MetaConversationAgent()
        if not request.history:
            result = agent.start(request.prompt)
        else:
            for turn in request.history:
                agent._history.append({
                    "assistant_message": turn.message,
                    "user_answer": turn.answer,
                })
            agent._turn_count = len(request.history)
            if request.history:
                agent._last_message = request.history[-1].message
            result = agent.next_turn(request.prompt)
    except Exception as e:
        logger.exception("meta_assess failed")
        raise HTTPException(500, f"Assessment failed: {e}")
    # Build interaction meta based on result status
    if result["status"] == "asking":
        interaction = for_assessment_asking(turn=len(request.history))
    elif result["status"] == "complete" and result.get("project_brief"):
        interaction = for_brief_review()
    else:
        interaction = None

    return AssessResponse(
        status=result["status"],
        message=result.get("message"),
        intent=result.get("intent"),
        project_brief=result.get("project_brief"),
        interaction=interaction,
    )


# ── Agent replay helper ──

def _replay_agent(history: list[HistoryTurn]) -> MetaConversationAgent:
    """Create a fresh agent and replay client-provided history."""
    agent = MetaConversationAgent()
    for turn in history:
        agent._history.append({
            "assistant_message": turn.message,
            "user_answer": turn.answer,
        })
    agent._turn_count = len(history)
    if history:
        agent._last_message = history[-1].message
    return agent


# ── Project meta endpoints ──

@router.post("/start", response_model=MetaResponse)
def meta_start(
    request: MetaStartRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Begin a meta conversation. Returns first message or immediate brief."""
    project = db.get_project(request.project_id)
    if not project:
        raise HTTPException(404, f"Project '{request.project_id}' not found")
    check_write_owner(user, project)

    try:
        agent = MetaConversationAgent()
        result = agent.start(request.prompt)
    except Exception as e:
        logger.exception("meta_start failed")
        raise HTTPException(500, f"Meta conversation failed: {e}")
    if result["status"] == "asking":
        interaction = for_meta_conversation_asking(turn=0)
    elif result["status"] == "complete":
        interaction = for_brief_review()
    else:
        interaction = None

    return MetaResponse(
        status=result["status"],
        message=result.get("message"),
        analysis_so_far=result.get("analysis_so_far"),
        project_brief=result.get("project_brief"),
        interaction=interaction,
    )


@router.post("/next", response_model=MetaResponse)
def meta_next(
    request: MetaNextRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Feed user answer + history. Returns next message or brief."""
    project = db.get_project(request.project_id)
    if not project:
        raise HTTPException(404, f"Project '{request.project_id}' not found")
    check_write_owner(user, project)

    try:
        agent = _replay_agent(request.history)
        result = agent.next_turn(request.answer)
    except Exception as e:
        logger.exception("meta_next failed")
        raise HTTPException(500, f"Meta conversation failed: {e}")
    if result["status"] == "asking":
        interaction = for_meta_conversation_asking(turn=len(request.history) + 1)
    elif result["status"] == "complete":
        interaction = for_brief_review()
    else:
        interaction = None

    return MetaResponse(
        status=result["status"],
        message=result.get("message"),
        analysis_so_far=result.get("analysis_so_far"),
        project_brief=result.get("project_brief"),
        interaction=interaction,
    )


@router.post("/force", response_model=MetaResponse)
def meta_force(
    request: MetaForceRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Force the agent to produce a brief immediately."""
    project = db.get_project(request.project_id)
    if not project:
        raise HTTPException(404, f"Project '{request.project_id}' not found")
    check_write_owner(user, project)

    try:
        agent = _replay_agent(request.history)
        result = agent.force_brief()
    except Exception as e:
        logger.exception("meta_force failed")
        raise HTTPException(500, f"Meta conversation failed: {e}")
    return MetaResponse(
        status=result["status"],
        message=result.get("message"),
        project_brief=result.get("project_brief"),
        interaction=for_brief_review() if result["status"] == "complete" else None,
    )


@router.post("/revise-brief", response_model=MetaResponse)
def revise_brief(
    request: ReviseBriefRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Revise an existing brief based on user feedback."""
    project = db.get_project(request.project_id)
    if not project:
        raise HTTPException(404, f"Project '{request.project_id}' not found")
    check_write_owner(user, project)

    try:
        agent = MetaConversationAgent()
        result = agent.revise_brief(request.project_brief, request.feedback)
    except Exception as e:
        logger.exception("revise_brief failed")
        raise HTTPException(500, f"Meta conversation failed: {e}")
    return MetaResponse(
        status=result["status"],
        message=result.get("message"),
        project_brief=result.get("project_brief"),
        interaction=for_brief_review(),
    )


# ── Task-scoped meta endpoints ──

class TaskMetaStartRequest(BaseModel):
    project_id: str
    prompt: str = Field(..., min_length=1, max_length=4000)


class TaskMetaNextRequest(BaseModel):
    task_id: int
    history: list[HistoryTurn] = []
    answer: str = Field(..., min_length=1, max_length=4000)


class TaskMetaForceRequest(BaseModel):
    task_id: int
    history: list[HistoryTurn] = []


class TaskMetaResponse(BaseModel):
    status: str
    message: Optional[str] = None
    task_spec: Optional[dict] = None
    task_id: Optional[int] = None
    interaction: Optional[InteractionMeta] = None


def _replay_task_agent(history: list[HistoryTurn]) -> TaskMetaConversationAgent:
    agent = TaskMetaConversationAgent()
    for turn in history:
        agent._history.append({
            "assistant_message": turn.message,
            "user_answer": turn.answer,
        })
    agent._turn_count = len(history)
    if history:
        agent._last_message = history[-1].message
    return agent


def _check_task_owner(user: CurrentUser | None, db: DBManager, task_id: int):
    """Raise 404 if user is authenticated and does not own the task. No-op for CLI."""
    if user is not None:
        with db.get_connection() as conn:
            row = conn.execute("SELECT owner_email FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row or row["owner_email"] != user.email:
            raise HTTPException(status_code=404, detail="Task not found")


@router.post("/task/start", response_model=TaskMetaResponse)
def task_meta_start(
    request: TaskMetaStartRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Start a task-scoped meta conversation. Creates a pending task."""
    project = db.get_project(request.project_id)
    if not project:
        raise HTTPException(404, f"Project '{request.project_id}' not found")
    check_write_owner(user, project)

    owner = user.email if user else "cli@local"

    try:
        # Create pending task
        task_id = db.push_task(request.project_id, request.prompt, owner_email=owner)

        # Fast-forward past project planning steps if already done
        import json as _json
        raw = project.get("completed_project_steps") or "[]"
        completed_proj = _json.loads(raw) if isinstance(raw, str) else raw
        from core.workspace_manager import PROJECT_STEP_SEQUENCE
        if all(s in completed_proj for s in PROJECT_STEP_SEQUENCE):
            pre_done = ["1"] + list(PROJECT_STEP_SEQUENCE)
            db.advance_step(task_id, "t_plan", pre_done, current_subtask=None)

        # Build agent with project context
        agent = TaskMetaConversationAgent()
        brief = project.get("brief")
        existing_tasks = db.list_tasks_by_project(request.project_id)
        agent.set_project_context(brief, existing_tasks)

        result = agent.start(request.prompt)

        if result["status"] == "complete":
            from core.meta_conversation import format_task_spec_as_prompt
            enriched = format_task_spec_as_prompt(result["task_spec"])
            db.update_task_prompt(task_id, enriched)
    except Exception as e:
        logger.exception("task_meta_start failed")
        raise HTTPException(500, f"Task meta conversation failed: {e}")

    if result["status"] == "asking":
        interaction = for_task_meta_asking(turn=0)
    elif result["status"] == "complete":
        interaction = for_task_meta_complete()
    else:
        interaction = None

    return TaskMetaResponse(
        status=result["status"],
        message=result.get("message"),
        task_spec=result.get("task_spec"),
        task_id=task_id,
        interaction=interaction,
    )


@router.post("/task/next", response_model=TaskMetaResponse)
def task_meta_next(
    request: TaskMetaNextRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Continue task meta conversation."""
    _check_task_owner(user, db, request.task_id)

    try:
        agent = _replay_task_agent(request.history)
        result = agent.next_turn(request.answer)

        if result["status"] == "complete":
            from core.meta_conversation import format_task_spec_as_prompt
            enriched = format_task_spec_as_prompt(result["task_spec"])
            db.update_task_prompt(request.task_id, enriched)
    except Exception as e:
        logger.exception("task_meta_next failed")
        raise HTTPException(500, f"Task meta conversation failed: {e}")

    if result["status"] == "asking":
        interaction = for_task_meta_asking(turn=len(request.history) + 1)
    elif result["status"] == "complete":
        interaction = for_task_meta_complete()
    else:
        interaction = None

    return TaskMetaResponse(
        status=result["status"],
        message=result.get("message"),
        task_spec=result.get("task_spec"),
        task_id=request.task_id,
        interaction=interaction,
    )


@router.post("/task/force", response_model=TaskMetaResponse)
def task_meta_force(
    request: TaskMetaForceRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Force task meta completion."""
    _check_task_owner(user, db, request.task_id)

    try:
        agent = _replay_task_agent(request.history)
        result = agent.force_brief()

        if result.get("task_spec"):
            from core.meta_conversation import format_task_spec_as_prompt
            enriched = format_task_spec_as_prompt(result["task_spec"])
            db.update_task_prompt(request.task_id, enriched)
    except Exception as e:
        logger.exception("task_meta_force failed")
        raise HTTPException(500, f"Task meta conversation failed: {e}")

    return TaskMetaResponse(
        status=result["status"],
        message=result.get("message"),
        task_spec=result.get("task_spec"),
        task_id=request.task_id,
        interaction=for_task_meta_complete() if result["status"] == "complete" else None,
    )


# ── Checkpoint endpoints (DB-direct, no OrchestratorRegistry) ──

class CheckpointResponse(BaseModel):
    checkpoint: Optional[str] = None
    label: Optional[str] = None
    step: Optional[str] = None
    project_id: Optional[str] = None
    timeout_at: Optional[float] = None
    rejection_count: int = 0
    step_output: Optional[dict] = None
    interaction: Optional[InteractionMeta] = None


class CheckpointApprovalRequest(BaseModel):
    project_id: str = ""   # optional — defaults to URL path project_id
    checkpoint: str
    feedback: str = ""


class CheckpointRejectionRequest(BaseModel):
    project_id: str = ""   # optional — defaults to URL path project_id
    checkpoint: str
    feedback: str = Field(..., min_length=1, max_length=4000)


def _read_step_output(project_id: str, step_id: str) -> Optional[dict]:
    """Read step output files and rejection history from the workspace."""
    ws = get_workspace_manager()
    final_dir = ws._final_dir(project_id, step_id)
    if not final_dir.exists():
        return None
    files = {}
    for item in sorted(final_dir.rglob("*")):
        if item.is_file() and item.name != "_snapshot.json" and not item.name.startswith("instruction"):
            try:
                rel = str(item.relative_to(final_dir))
                files[rel] = item.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

    # Read rejection history for this step (migrated from Inbox_{step_id}/ to {step_id}/)
    ws_path = ws._get_secure_path(project_id)
    step_dir = ws._final_dir(project_id, step_id)  # skillflow layout
    rejection_file = step_dir / "user_rejection_history.json"
    rejection_history = None
    if rejection_file.exists():
        import json
        rejection_history = json.loads(rejection_file.read_text())

    return {
        "files": files if files else None,
        "rejection_history": rejection_history
    }


def _get_checkpoint_info(project_id: str) -> tuple[str, str, str, str]:
    """Get checkpoint state from skillflow (source of truth).

    Returns (step_id, label, run_id, graph_name) or empty strings if not at checkpoint.

    A3 fix: now also returns info for runs in 'failed' state IF the last
    completed step is a checkpoint. This allows the user to approve a
    checkpoint after a downstream step (e.g. the verifier) failed
    catastrophically. The approve_checkpoint handler will then reactivate
    the run via sf.reactivate_run() before resuming.
    """
    sf = get_skillflow()
    run = sf.get_run_by_project(project_id)
    if not run or run["status"] not in ("paused", "failed"):
        return "", "", "", ""

    run_id = run["id"]
    graph_name = run["graph_name"]
    step_id = run.get("current_node", "")

    label = "Checkpoint"
    if step_id:
        resolver = sf._get_resolver(graph_name)
        # For checkpoint steps, current_node is the NEXT node after the checkpoint.
        # Find the checkpoint step — it's the last completed step.
        steps = sf.get_steps(run_id)
        for s in reversed(steps):
            if s["status"] == "completed":
                node = resolver.get_node(s["step_id"])
                if node and node.checkpoint:
                    step_id = s["step_id"]
                    label = node.checkpoint_label or label
                    break

    return step_id, label, run_id, graph_name


@router.get("/{project_id}/checkpoint", response_model=CheckpointResponse)
def get_pending_checkpoint(
    project_id: str,
    request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Get the current pending checkpoint for a project, if any."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    check_read_owner(user, request, project)

    step_id, label, _run_id, _graph = _get_checkpoint_info(project_id)
    if not step_id:
        return CheckpointResponse()

    step_output = _read_step_output(project_id, step_id)

    return CheckpointResponse(
        checkpoint=step_id,
        label=label,
        step=step_id,
        project_id=project_id,
        step_output=step_output,
        interaction=for_checkpoint_waiting(step_label=label, rejection_count=0),
    )


@router.post("/{project_id}/checkpoint/approve")
def approve_checkpoint(
    project_id: str,
    request: CheckpointApprovalRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Approve a checkpoint and resume the pipeline."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    check_write_owner(user, project)

    _step_id, _label, run_id, _graph = _get_checkpoint_info(project_id)
    if not run_id:
        raise HTTPException(400, "Project is not waiting for approval")

    # AT-7 idempotency guard: only act if the requested checkpoint is the one the
    # run is actually paused at. A stale modal, double-click, or client retry that
    # targets an already-passed checkpoint must NOT re-trigger reactivate/resume —
    # that can re-traverse the pipeline (duplicate steps). Treat a mismatch as an
    # idempotent no-op rather than acting on the wrong checkpoint.
    if request.checkpoint and _step_id and request.checkpoint != _step_id:
        return {"status": "already_advanced", "checkpoint": request.checkpoint,
                "current_checkpoint": _step_id}

    sf = get_skillflow()
    run = sf.get_run(run_id)
    # Refuse to approve on a completed run — reactivate_run on a completed
    # run is a no-op in skillflow (status stays completed), so the
    # _get_or_create_skillflow_run scheduler path then sees "no active run"
    # and creates a fresh run, restarting the entire pipeline from Step 1.
    # The right path for an already-completed run is to use POST /projects/
    # {id}/retry, not this endpoint.
    if run and run["status"] == "completed":
        raise HTTPException(400, "Run is already completed; use POST /projects/{id}/retry to re-run")

    # SF-7: use skillflow's first-class approve_checkpoint for paused runs.
    # The framework validates the checkpoint state, emits checkpoint_approved
    # outbox event, and resumes the run in one atomic transaction.
    # For failed runs (A3 rescue path), fall back to reactivate + resume.
    next_node = ""
    if run and run["status"] == "paused":
        next_node = sf.approve_checkpoint(run_id)
        # Sync the project status immediately so the scheduler picks it up.
        # Without this, the aitelier DB still shows "checkpoint:..." and the
        # scheduler's status filter (planning/executing/verifying/running)
        # skips the project entirely.
        from core.scheduler import _sync_project_status_to_db
        _sync_project_status_to_db(project_id)
    elif run and run["status"] == "failed":
        sf.reactivate_run(run_id)
        sf.resume_run(run_id)

    # Clear the drafting gate: the user approved the brief, so the scheduler
    # is now allowed to pick up this project. Without this, projects created
    # via the meta conversation would be stuck in meta_state='drafting' forever
    # and the scheduler would never create a DPE run for them.
    db.set_project_meta_state(project_id, None)

    # Wake the scheduler and return immediately. Do NOT run the pipeline tick
    # inline: _execute_skillflow_tick → runner.execute → engine.run_step is a
    # SYNCHRONOUS call that blocks the event loop for the full duration of the
    # next agent step (an LLM call, often 30-120s). Running it here — even via
    # create_task — starves the loop so the approve RESPONSE can't flush until
    # that step finishes, and the client times out (the checkpoint approval
    # timeout). wake_scheduler() already enqueues poll_and_execute on
    # APScheduler, which runs the step decoupled from this request.
    from core.scheduler import wake_scheduler
    import asyncio, json, time, sys
    wake_scheduler()

    # Push the checkpoint_resolved SSE event as a short, non-blocking task.
    try:
        resolved_payload = json.dumps({
            "type": "checkpoint_resolved",
            "project_id": project_id,
            "step": _step_id,
            "label": _label,
            "action": "approved",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

        async def _push_resolved():
            await stream_manager.push_log(project_id, resolved_payload)
            await stream_manager.push_log("__global__", resolved_payload)

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is not None and running_loop.is_running():
            running_loop.create_task(_push_resolved())
    except Exception as e:
        # Never let SSE bookkeeping affect the response — approval is recorded.
        print(f"[approve_checkpoint] SSE push error: {e}", file=sys.stderr)

    return {"status": "approved", "checkpoint": request.checkpoint}


@router.post("/{project_id}/checkpoint/reject")
def reject_checkpoint(
    project_id: str,
    request: CheckpointRejectionRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Reject a checkpoint with required feedback. The pipeline will re-run the step."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    check_write_owner(user, project)

    step_id, _label, run_id, _graph = _get_checkpoint_info(project_id)
    if not run_id or not step_id:
        raise HTTPException(400, "Project is not waiting for approval")

    # AT-7 idempotency guard (see approve_checkpoint): ignore a reject aimed at a
    # checkpoint the run is no longer paused at.
    if request.checkpoint and step_id and request.checkpoint != step_id:
        return {"status": "already_advanced", "checkpoint": request.checkpoint,
                "current_checkpoint": step_id}

    sf = get_skillflow()
    # Same guard as approve_checkpoint — refuse on completed run.
    run = sf.get_run(run_id)
    if run and run["status"] == "completed":
        raise HTTPException(400, "Run is already completed; use POST /projects/{id}/retry to re-run")
    # reject_checkpoint accepts a paused run (normal case) or a failed run
    # (rejecting the last checkpoint after a downstream failure). It performs
    # the state transition itself — resets the checkpoint step to pending,
    # injects the rejection feedback, and flips the run back to 'running'.
    # Do NOT resume/reactivate first: that moves the run out of a rejectable
    # state and reject_checkpoint would then refuse it.
    sf.reject_checkpoint(run_id, step_id, request.feedback)

    from core.scheduler import wake_scheduler
    import asyncio, json, time
    wake_scheduler()
    # Emit checkpoint_resolved SSE event so TUI clears the ⏳ status
    try:
        loop = asyncio.get_event_loop()
        resolved_payload = json.dumps({
            "type": "checkpoint_resolved",
            "project_id": project_id,
            "step": step_id,
            "label": _label,
            "action": "rejected",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        loop.create_task(stream_manager.push_log(project_id, resolved_payload))
        loop.create_task(stream_manager.push_log("__global__", resolved_payload))
    except RuntimeError:
        pass

    return {"status": "rejected", "checkpoint": request.checkpoint}
