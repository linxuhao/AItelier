"""meta_run — drive the meta_conversation skillflow run for the chat butler.

Thin AItelier-layer helpers over skillflow primitives so the butler relays a
deterministic requirements-gathering conversation:

  - The `gather` step pauses (checkpoint) on every completion. The butler reads
    ``gather_state.json`` (via :func:`read_gather_state`) to decide whether to
    relay a clarifying *question* or present the finished *brief*.
  - A user *answer* resumes the gather step with the answer carried as feedback
    (:func:`submit_user_answer`) — implemented with skillflow's existing
    reject-with-redirect resume primitive, so we never expose "reject" semantics
    for a normal answer. The full conversation also lives in the workspace
    transcript ``meta/conversation.md`` (maintained by the butler), which the
    gather step reads as context.
  - Approving the brief (:func:`approve_meta`) resumes the checkpoint; the
    finalize tool emits the reviewed artifacts and completes the run, after which
    the butler triggers DPE (``core/project_submit.seed_and_trigger``).

No skillflow changes are required.
"""

import json
from core.checkpoint_observation import observe_checkpoint, checkpoint_phase

META_GRAPH = "meta_conversation"
GATHER_STEP = "gather"


def read_gather_state(ws, project_id: str) -> dict | None:
    """Read the gather step's committed gather_state.json, or None if absent."""
    path = ws.get_final_path(project_id, GATHER_STEP, graph_name=META_GRAPH) / "gather_state.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def submit_user_answer(sf, run_id: str, answer: str) -> None:
    """Resume the paused gather step with the user's answer.

    Uses reject-with-redirect so the gather step re-runs (status pending,
    current_node=gather) with the answer injected as feedback. The transcript
    file the butler maintains carries the full conversation; this just unblocks
    the run.
    """
    sf.reject_checkpoint(run_id, GATHER_STEP, feedback=answer or "(continue)",
                         redirect_to=GATHER_STEP)


def request_brief_changes(sf, run_id: str, feedback: str) -> None:
    """The user wants the proposed brief changed — re-run gather with feedback.

    Semantically distinct from :func:`submit_user_answer` for the caller, but the
    underlying skillflow operation is identical (re-open gather with feedback).
    """
    sf.reject_checkpoint(run_id, GATHER_STEP, feedback=feedback or "(revise the brief)",
                         redirect_to=GATHER_STEP)


@observe_checkpoint
def approve_meta(sf, run_id: str, step_runner=None, *,
                 expected_checkpoint=None, decision_id="") -> None:
    """Approve the brief and drive the run through its `finalize` tool step.

    Approving the gather checkpoint routes the run to the `finalize` tool step,
    which emits the project artifacts (project_brief.md, spec.md, step1_goals.json).
    The graph's node_reached end-condition then completes the run.

    The finalize tool is the SOLE producer of those artifacts, so this does NOT
    force-complete a stuck run — a meta run that fails to emit must surface that
    failure (``RuntimeError``) so the caller does not start DPE on missing
    artifacts. Idempotent if already completed.

    Args:
        sf: SkillFlow instance.
        run_id: Meta conversation run ID.
        step_runner: Optional callable invoked for each step reached after
            advance_run. Receives a ``skillflow.core.ClaimedStep`` and must return
            a result dict suitable for ``confirm_step``. When None (default),
            steps are NOT claimed or executed — the function only advances graph
            state, preserving existing behavior.
    """
    checkpoint_phase("approve_run_lookup", run_id=run_id, decision_id=decision_id)
    run = sf.get_run(run_id)
    # Completion does not prove THIS decision was recorded. Validate/replay
    # the original identity before returning, even when finalize already ran.
    checkpoint_phase("approve")
    sf.approve_checkpoint(run_id, expected_checkpoint=expected_checkpoint,
                          decision_id=decision_id)
    if run and run.get("status") == "completed":
        return

    # Drive the now-running graph to terminal. The 2-step path (approve →
    # finalize → complete) needs only a couple advances; the budget is a stall
    # guard, not a normal code path.
    for _ in range(8):
        checkpoint_phase("finalize_run_lookup")
        run = sf.get_run(run_id)
        if not run:
            raise RuntimeError(f"meta run {run_id} vanished during finalize")
        status = run.get("status")
        if status == "completed":
            return
        if status == "failed":
            raise RuntimeError(
                f"meta run {run_id} failed during finalize: "
                f"{run.get('error_reason') or 'finalize did not emit artifacts'}")
        try:
            checkpoint_phase("advance_finalize")
            next_node = sf.advance_run(run_id)
        except Exception as e:
            # A raising finalize tool (e.g. an incomplete brief) lands here.
            raise RuntimeError(f"meta run {run_id} finalize step errored: {e}") from e

        # If a step_runner is provided and advance_run transitioned into a new
        # node, claim + execute + confirm the step (tool or agent).
        if next_node is not None and step_runner is not None:
            try:
                checkpoint_phase("claim_finalize")
                claimed = sf.claim_next_step(run_id)
            except Exception:
                continue  # nothing to claim — let the loop re-check status
            if claimed is not None:
                try:
                    checkpoint_phase("execute_finalize")
                    result = step_runner(claimed)
                    checkpoint_phase("confirm_finalize")
                    sf.confirm_step(claimed.token, result)
                except Exception as e:
                    checkpoint_phase("fail_finalize")
                    sf.fail_step(claimed.token, str(e)[:200], retryable=False)
                    raise RuntimeError(
                        f"meta run {run_id} step '{claimed.step_id}' failed: {e}") from e

    raise RuntimeError(f"meta run {run_id} did not finalize within the step budget")


def retained_gather_state(sf, run_id: str, step_instance_id=None) -> dict:
    """Read the completed reviewed instance from the SDK's retained payload."""
    checkpoint_phase("retained_gather")
    rows = [row for row in sf.get_steps(run_id, include_payloads=True)
            if row["step_id"] == GATHER_STEP and row["status"] == "completed"
            and (step_instance_id is None or row["id"] == step_instance_id)]
    if not rows:
        raise ValueError("The reviewed gather instance does not belong to this run")
    state = json.loads(max(rows, key=lambda row: row["id"])["outputs_json"]).get("gather_state")
    if isinstance(state, str):
        state = json.loads(state)
    if not isinstance(state, dict):
        raise ValueError("The retained gather output is not an object")
    return state


def meta_owns_submission(sf, db, run) -> bool:
    """Follow the scheduler's owner selection before changing its project cache."""
    checkpoint_phase("submission_owner")
    pid = run["project_id"]
    project = db.get_project(pid) or {}
    owner = sf.get_run_by_project(pid)
    if not owner:
        runs = sf.list_runs(pid)
        primary = project.get("config_name") or "dpe_default_v2"
        owner = next((r for r in runs if r["graph_name"] == primary), runs[0] if runs else None)
    if owner and owner["id"] != run["id"]:
        if owner["status"] == "failed":
            raise RuntimeError("The project's failed run needs an explicit retry")
        return False
    return True


def submit_finalized_meta(sf, db, ws, run, expected_checkpoint) -> dict:
    """Submit the retained brief only while this completed meta run owns the cache.

    Callers hold the existing per-project scheduler lock across approval and
    submission; an old replay must not reset a later build's pause or progress.
    """
    from core.meta_conversation import format_brief_as_markdown
    from core.project_submit import seed_and_trigger

    pid = run["project_id"]
    if not meta_owns_submission(sf, db, run):
        return {"status": "already_planned", "project_id": pid}
    brief = retained_gather_state(sf, run["id"], expected_checkpoint["step_instance_id"]).get("brief") or {}
    checkpoint_phase("submission_project")
    project = db.get_project(pid) or {}
    completed = project.get("completed_project_steps") or []
    if isinstance(completed, str):
        completed = json.loads(completed)
    if (not (project.get("status") or "").startswith("failed:")
            and not project.get("meta_state") and "1" in completed
            and project.get("brief") == format_brief_as_markdown(brief)):
        return {"status": "already_planned", "project_id": pid}
    # Only the exact meta owner may normalize the real paused/completed cache.
    # Failed projects still go through seed_and_trigger's explicit-retry refusal.
    if (project.get("status") or "").startswith("checkpoint:") or project.get("status") == "completed":
        checkpoint_phase("submission_ready")
        db.update_project(pid, status="planning")
    checkpoint_phase("seed_build")
    result = seed_and_trigger(db, ws, pid, brief)
    if result.get("status") not in ("submitted", "already_planned"):
        raise RuntimeError(result.get("message") or "The finalized build was not submitted")
    return result


@observe_checkpoint
def approve_and_submit_meta(sf, db, ws, run_id: str, *, expected_checkpoint=None, decision_id="") -> dict:
    """Compose the ordinary SDK approval, genuine finalize and build submission."""
    from core.scheduler import _get_tick_lock

    checkpoint_phase("submission_run", run_id=run_id, decision_id=decision_id)
    run = sf.get_run(run_id)
    if not run or run.get("graph_name") != META_GRAPH:
        raise ValueError("Expected the project's exact meta_conversation run")
    lock = _get_tick_lock(run["project_id"])
    checkpoint_phase("submission_lock", project_lock=lock)
    with lock:
        approve_meta(sf, run_id, expected_checkpoint=expected_checkpoint, decision_id=decision_id)
        return submit_finalized_meta(sf, db, ws, run, expected_checkpoint)
