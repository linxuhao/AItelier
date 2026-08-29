# api/meta_routers.py
# Stateless REST endpoints for meta conversation agents.
# Client owns conversation history and sends it with each request.
# Auth-optional: ownership checks are no-ops when user=None (CLI mode).

import logging
import re

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
from api.auth import CurrentUser, get_optional_user, creator_email
from api.sse_manager import stream_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/meta", tags=["Meta Conversation"])

# Free-text intake cap (chars). Not a UX limit — it only exists so a runaway or
# hostile client can't push an unbounded body through the LLM calls these
# endpoints make. It must stay large enough for the real intake: users paste
# whole requirements documents (a 42 KB API spec is ordinary), and that verbatim
# text is what survives into project/spec.md — truncating it at the door would
# silently drop requirements the pipeline is supposed to build against.
MAX_INTAKE_CHARS = 200_000


# ── Request / Response models ──

class HistoryTurn(BaseModel):
    message: str
    answer: str


class MetaStartRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=MAX_INTAKE_CHARS)
    project_id: str


class MetaNextRequest(BaseModel):
    project_id: str
    history: list[HistoryTurn] = []
    answer: str = Field(..., min_length=1, max_length=MAX_INTAKE_CHARS)


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
    feedback: str = Field(..., min_length=1, max_length=MAX_INTAKE_CHARS)


# ── Intent detection ──

class IntentDetectRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=MAX_INTAKE_CHARS)


class IntentDetectResponse(BaseModel):
    intent: str  # "new_project" | "existing_code" | "unclear"
    reasoning: Optional[str] = None


@router.post("/detect-intent", response_model=IntentDetectResponse)
def meta_detect_intent(
    request: IntentDetectRequest,
    user: CurrentUser | None = Depends(get_optional_user),
):
    """Detect whether a user prompt is about a new project or existing code."""
    result = detect_intent(request.prompt, user_lang=user.lang if user else None)
    return IntentDetectResponse(
        intent=result["intent"],
        reasoning=result.get("reasoning"),
    )


# ── Pre-project assessment (no project_id required) ──

class AssessRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=MAX_INTAKE_CHARS)
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
        agent = MetaConversationAgent(user_lang=user.lang if user else None)
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

def _replay_agent(history: list[HistoryTurn],
                  user_lang: str | None = None) -> MetaConversationAgent:
    """Create a fresh agent and replay client-provided history."""
    agent = MetaConversationAgent(user_lang=user_lang)
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
        agent = MetaConversationAgent(user_lang=user.lang if user else None)
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
        agent = _replay_agent(request.history, user_lang=user.lang if user else None)
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
        agent = _replay_agent(request.history, user_lang=user.lang if user else None)
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
        agent = MetaConversationAgent(user_lang=user.lang if user else None)
        result = agent.revise_brief(request.project_brief, request.feedback,
                                     user_lang=user.lang if user else None)
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
    prompt: str = Field(..., min_length=1, max_length=MAX_INTAKE_CHARS)


class TaskMetaNextRequest(BaseModel):
    task_id: int
    history: list[HistoryTurn] = []
    answer: str = Field(..., min_length=1, max_length=MAX_INTAKE_CHARS)


class TaskMetaForceRequest(BaseModel):
    task_id: int
    history: list[HistoryTurn] = []


class TaskMetaResponse(BaseModel):
    status: str
    message: Optional[str] = None
    task_spec: Optional[dict] = None
    task_id: Optional[int] = None
    interaction: Optional[InteractionMeta] = None


def _replay_task_agent(history: list[HistoryTurn],
                       user_lang: str | None = None) -> TaskMetaConversationAgent:
    agent = TaskMetaConversationAgent(user_lang=user_lang)
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
    http_request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
):
    """Start a task-scoped meta conversation. Creates a pending task."""
    project = db.get_project(request.project_id)
    if not project:
        raise HTTPException(404, f"Project '{request.project_id}' not found")
    check_write_owner(user, project)

    owner = user.email if user else (creator_email(http_request) or "cli@local")

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
        agent = TaskMetaConversationAgent(user_lang=user.lang if user else None)
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
        agent = _replay_task_agent(request.history, user_lang=user.lang if user else None)
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
        agent = _replay_task_agent(request.history, user_lang=user.lang if user else None)
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
    # Identifies THIS pause, not just the step that paused — see
    # _get_checkpoint_info. A client with an in-progress rejection uses it to
    # notice the checkpoint was replaced by a new instance of the same step.
    checkpoint_instance: int = 0
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
    feedback: str = Field(..., min_length=1, max_length=MAX_INTAKE_CHARS)


def _read_step_output(project_id: str, step_id: str,
                      graph_name: str = "dpe_default_v2",
                      run_id: str = "") -> Optional[dict]:
    """Read step output files and rejection history from the workspace.

    ``graph_name`` must match the run's config — ``_final_dir`` lays out step
    dirs under ``{workspace}/{graph_name}/{step_id}/``. Omitting it (the old
    behavior) silently read the wrong directory for any non-DPE pipeline, so
    the checkpoint modal showed "no files to review".
    """
    ws = get_workspace_manager()
    final_dir = ws._final_dir(project_id, step_id, graph_name)
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

    return {
        "files": files if files else None,
        "rejection_history": _read_rejection_rounds(project_id, step_id,
                                                    graph_name, run_id),
    }


# One round heading per rejection round, written by skillflow's
# `_append_feedback_log` as:  `## 反馈轮 #<N> · <YYYY-MM-DD HH:MM UTC>`
#
# Anchor on the `#<N> ·` run, NOT on "any level-2 heading". The previous pattern
# matched every `## ` line in the log, so a round count — and the "revised N
# time(s)" banner built from it — counted the USER'S OWN markdown headings: one
# rejection whose feedback happened to have five `## ` sections reported six
# rounds. A user who rejects once and is told the step has been revised six times
# has no way to tell a loop from a display artifact, which is the same class of
# defect this reader was written to fix (a banner that misreports the feedback
# history is worse than no banner: it looks like evidence).
#
# Matching skillflow's marker is the RIGHT coupling, not a new one: skillflow
# numbers the rounds by counting this very literal
# (`existing.count("## 反馈轮 #")` in core.py), so writer and reader now agree and
# a wording change breaks both together instead of silently skewing one. The
# `\S+` keeps the localised word itself out of the pattern.
_FEEDBACK_ROUND_RE = re.compile(r"^##[ \t]+\S+[ \t]+#\d+[ \t]*·", re.MULTILINE)


def _read_rejection_rounds(project_id: str, step_id: str,
                           graph_name: str, run_id: str = "") -> Optional[list]:
    """The user's own rejection feedback, for the checkpoint modal's banner.

    This used to read `user_rejection_history.json` out of the step's final dir.
    That file has THREE readers in this repo — here, `prompt_assembler`, and
    `restage`'s skip-list — and NO writer anywhere; `find` over a live workspace
    turns up none. So the banner never rendered: a user who rejected with
    feedback saw the modal reopen looking identical and concluded the rejection
    had failed. The feedback had in fact been saved AND delivered to the agent,
    which acted on it.

    skillflow is the one that persists it (`_append_feedback_log`, since 1.5.15),
    at `<config_dir>/_feedback/<step>.md`. Two things make it easy to look in the
    wrong place, and the old code got both wrong: the log is keyed by the step the
    reject REWINDS to (`checkpoint_reject_to` — `outline`, not `outline_gate`),
    and it lives beside the config dir rather than inside the step's output.
    Resolve the path through skillflow's own helper so this cannot drift.
    """
    try:
        from skillflow.context import feedback_log_path
        sf = get_skillflow()
        target = step_id
        try:
            # Pinned when the caller knows the run: this picks WHICH feedback
            # log to read, and its sibling three hundred lines down was already
            # fixed — the two disagreeing is how the modal shows another step's
            # rejection history, or none.
            # `if run_id` is about the CALLER, not the engine: this helper is
            # reachable with no run in hand.
            # by-name-ok: the no-run half
            _r = (sf._get_resolver_for_run(run_id) if run_id
                  # by-name-ok: the no-run half of the caller-supplied run_id
                  else sf._get_resolver(graph_name))
            node = _r.get_node(step_id)
            target = (node.checkpoint_reject_to or step_id) if node else step_id
        except Exception:
            pass
        cfg_dir = sf._workspace.get_config_path(project_id, graph_name)
        path = feedback_log_path(cfg_dir, target)
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        logging.getLogger(__name__).warning(
            "could not read the checkpoint feedback log", exc_info=True)
        return None

    starts = [m.start() for m in _FEEDBACK_ROUND_RE.finditer(text)]
    if not starts:
        body = text.strip()
        return [_round("", body)] if body else None
    rounds = []
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[s:end]
        heading, _, body = block.partition("\n")
        rounds.append(_round(heading.lstrip("# ").strip(), body.strip()))
    return rounds or None


def _round(heading: str, body: str) -> dict:
    """One rejection round, carrying the text under BOTH key names.

    There are three consumers and they do not agree: the web modal and the TUI
    chat pane read `user_feedback or reason`, while `cli/app.py` reads only
    `reason` and would print "Last feedback: N/A". Emitting one name would have
    fixed two surfaces out of three — the same know-one-key defect this whole
    change set is about, so the payload carries both rather than making each
    reader guess.
    """
    return {"round": heading, "user_feedback": body, "reason": body}


# A run that failed on ROUTING cannot be rescued by approving a checkpoint, so
# it must not be offered as one. The A3 rescue path below resumes a failed run
# with sf.reactivate_run(), which re-opens the step named in error_reason — and
# for a routing dead end that step re-executes, produces the same verdict, and
# dead-ends on the same spent edge, because a step's own re-run can never lower
# an edge count. Live on the NL2Repo benchmark (arxiv-mcp-server, 2026-08-17):
# dpe_default's `5_review` spent its `→ 3` goal-loop budget (max_loop=2),
# skillflow failed the run correctly ("Cycle limit exceeded … '5_review' -> '3'
# (max_loop=2 reached)"), and then the heuristic fallback below reported step 3's
# long-passed checkpoint as pending; the harness's auto-approver resurrected the
# run 229 times in 100 minutes (one step instance re-claimed 227 times) until the
# 3-hour wall clock killed the task.
_ROUTING_DEAD_END_MARKERS = ("cycle limit exceeded", "no matching transition")


def _failed_on_routing(run: dict | None) -> bool:
    """True when a failed run's reason is a dead end no re-run can clear."""
    if not run or run.get("status") != "failed":
        return False
    reason = (run.get("error_reason") or "").lower()
    return any(m in reason for m in _ROUTING_DEAD_END_MARKERS)


def _get_checkpoint_info(project_id: str,
                         run_id: str = "") -> tuple[str, str, str, str, int]:
    """Get checkpoint state from skillflow (source of truth).

    Returns (step_id, label, run_id, graph_name, checkpoint_instance), or
    empty strings / 0 if not at a checkpoint.

    Pass `run_id` when the caller HAS one. Resolving from the project alone
    means `get_run_by_project` — the newest non-completed run of ANY config —
    so a project with a paused DPE run plus a newer run of something else
    answers about the wrong one: MCP handed another run's step id to
    `reject_checkpoint`, and the dashboard showed a checkpoint that was not the
    one being answered. A `graph_name` filter would not be enough; two runs of
    the SAME config collide just as well. The run id is the identity.

    A3 fix: now also returns info for runs in 'failed' state IF the last
    completed step is a checkpoint. This allows the user to approve a
    checkpoint after a downstream step (e.g. the verifier) failed
    catastrophically. The approve_checkpoint handler will then reactivate
    the run via sf.reactivate_run() before resuming. A run that failed on
    ROUTING is excluded — see _failed_on_routing.
    """
    sf = get_skillflow()
    run = sf.get_run(run_id) if run_id else sf.get_run_by_project(project_id)
    if not run or run["status"] not in ("paused", "failed"):
        return "", "", "", "", 0
    if _failed_on_routing(run):
        return "", "", "", "", 0

    run_id = run["id"]
    graph_name = run["graph_name"]
    step_id = run.get("current_node", "")
    # The checkpoint STEP-INSTANCE row id. A goal loop can re-present the SAME
    # step id with the SAME rejection_count (5_review -> 3 sends the run back to
    # step 3, which pauses at its checkpoint again without anyone having
    # rejected), and a client holding a half-written rejection has no other way
    # to tell the new pause from the old one. skillflow appends a new step row
    # per instance, so this id changes and (step_id, rejection_count, instance)
    # does not collide.
    instance = 0

    label = "Checkpoint"
    # The graph this RUN is pinned to, not whatever is registered now.
    #
    # This function alone decides which step a paused run is waiting on, and
    # every surface — SPA, CLI, butler, MCP — takes its answer. Resolving by
    # name was safe only while one name meant one graph; pinning ended that. If
    # a config is edited while a run is paused, `node.checkpoint` is read from
    # the NEW graph and this returns a DIFFERENT step than the one the user is
    # looking at. Reject then rewinds a completed step and injects the feedback
    # into the wrong agent's inputs, and Approve trips the AT-7 idempotency
    # guard, answers "already_advanced", and leaves the run paused forever —
    # the unanswerable-checkpoint failure that guard's own comment records from
    # jinyong-hud, through a new door.
    # `_get_resolver_for_run` exists on every engine, including the deployed
    # one — there it IS the by-name lookup, keyed on run_id, so an older engine
    # degrades to the old behaviour by definition rather than by a fallback.
    resolver = sf._get_resolver_for_run(run_id)
    # When skillflow pauses at a checkpoint it sets current_node to the
    # checkpoint's NEXT node (its checkpoint-guarded transition target) and
    # marks the checkpoint step itself "completed". Identify the exact
    # checkpoint step we are paused at: the most recent completed step whose
    # checkpoint transition points at current_node. This is more precise
    # than "last completed checkpoint step" when a task loop re-runs
    # checkpoint-bearing steps (e.g. step 3) more than once.
    #
    # But current_node is NOT reliable, and this scan must not depend on it.
    # skillflow's `recover_stale_claims` reaps a dead owner's claim by setting
    # the run's current_node to NULL — unconditionally, including on a run that
    # is PAUSED, where the reaped claim says nothing about where the run sits.
    # Restart the container while a checkpoint waits and the pointer is gone;
    # this used to return step_id="" and every answer path — SPA, CLI, butler,
    # MCP — reported "no checkpoint to answer" for a run visibly paused at one.
    # The checkpoint became unanswerable and the run could never be resumed.
    # Measured on jinyong-hud, 2026-08-27, with a human verdict in hand.
    #
    # So the refinement by current_node is now an OPTIMISATION over a scan that
    # stands on its own: absent that pointer, the most recent completed
    # checkpoint step is both the best available inference and the one the run
    # is in fact paused at.
    next_node = step_id
    steps = sf.get_steps(run_id)
    matched = None
    for s in reversed(steps):
        if s["status"] != "completed":
            continue
        node = resolver.get_node(s["step_id"])
        if not (node and node.checkpoint):
            continue
        targets = [t.to for t in node.transitions
                   if t.match and t.match.get("from") == "checkpoint"]
        if matched is None:
            matched = (s["step_id"], node.checkpoint_label, s.get("id") or 0)
        if next_node and next_node in targets:
            matched = (s["step_id"], node.checkpoint_label, s.get("id") or 0)
            break
    if matched:
        step_id, _lbl, instance = matched
        label = _lbl or label

    return step_id, label, run_id, graph_name, instance


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

    step_id, label, _run_id, _graph, _instance = _get_checkpoint_info(project_id)
    if not step_id:
        return CheckpointResponse()

    step_output = _read_step_output(project_id, step_id,
                                    _graph or "dpe_default_v2", _run_id)

    return CheckpointResponse(
        checkpoint=step_id,
        label=label,
        step=step_id,
        checkpoint_instance=_instance,
        project_id=project_id,
        step_output=step_output,
        # The real count, not a hardcoded 0. A user who rejects has no other way
        # to tell their feedback landed: this is the same pause label on the same
        # gate, so without it the modal is indistinguishable from the one they
        # just rejected.
        rejection_count=len((step_output or {}).get("rejection_history") or []),
        interaction=for_checkpoint_waiting(
            step_label=label,
            rejection_count=len((step_output or {}).get("rejection_history") or []),
        ),
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

    # skillflow's approve_checkpoint(run_id) has NO feedback parameter; only
    # reject_checkpoint(run_id, step_id, feedback) does. This handler accepted
    # the field and never read it, and the dashboard put a Feedback box directly
    # above the Approve button — so a person typed binding amendments into a
    # visible textarea, watched the run resume, and had no way to learn the text
    # went nowhere: it reaches neither the run, the trace, nor the next step's
    # context. Refuse BEFORE resolving the checkpoint, so the call is rejected
    # for this reason and cannot half-approve on its way out. Same refusal the
    # MCP answer_checkpoint tool gives (api/mcp_router.py).
    _fb = request.feedback.strip()
    if _fb:
        raise HTTPException(400, (
            f"An approval carries NO feedback channel — skillflow's "
            f"approve_checkpoint(run_id) takes no feedback, so these {len(_fb)} "
            f"characters would be dropped and the next step would never see "
            f"them. What actually delivers them: POST the same text to "
            f"/api/meta/{project_id}/checkpoint/reject to send the step back to "
            f"redo the work against it; or put the requirement in the run's seed "
            f"before launching. Re-send as a rejection, or drop `feedback` if "
            f"you really do mean 'approve as-is'."))

    _step_id, _label, run_id, _graph, _inst = _get_checkpoint_info(project_id)
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
        from core.run_driver import restore_retry_budget
        # Restoring the budget is best-effort; RESUMING is not. Unguarded, a
        # raise here left the run reactivated but never resumed, the drafting
        # gate never cleared (so the scheduler stays off the project), no
        # scheduler wake and no SSE — a worse half-written state than the one
        # the restore was fixing, from a browser's point of view a 500 and a
        # project that silently stops. Log and carry on to resume_run: a run
        # that resumes with a stale retry count still moves; one that never
        # resumes does not.
        try:
            restore_retry_budget(sf, run_id)   # see the docstring: reactivate
        except Exception:                      # alone leaves the blocker at max
            logger.warning("restore_retry_budget failed for run %s; resuming "
                           "anyway — the blocked step may re-fail immediately",
                           run_id, exc_info=True)
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

    step_id, _label, run_id, _graph, _inst = _get_checkpoint_info(project_id)
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
    #
    # Honor the checkpoint node's `checkpoint_reject_to`: a reject may need to
    # rewind to an EARLIER step (e.g. novel_init's design_gate → design), not
    # just re-run the checkpoint step in place. Without this, redirect_to
    # defaults to the checkpoint step itself, so a gate that should revise an
    # upstream maker just re-runs the gate and re-pauses. Older checkpoints all
    # set checkpoint_reject_to == themselves, so this is a no-op for them.
    from core.run_driver import checkpoint_reject_target
    redirect_to = checkpoint_reject_target(sf, _graph, step_id, run_id)
    try:
        sf.reject_checkpoint(run_id, step_id, request.feedback,
                             redirect_to=redirect_to)
    except Exception as e:
        # `redirect_to` is computed from the CURRENT graph while the run
        # executes the version it started with, so a config edited since the
        # run began can name a step the run's version does not have. The engine
        # refuses (rolling back cleanly — the run stays paused and a later valid
        # reject still works), and without this the refusal reached the browser
        # as a bare 500 with its actionable message swallowed. The other two
        # callers of reject_checkpoint already report the reason.
        raise HTTPException(status_code=409, detail=str(e))

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
