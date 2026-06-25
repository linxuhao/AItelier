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
    gather step's ``approved → null`` transition completes the run, after which
    the butler triggers DPE (``core/project_submit.seed_and_trigger``).

No skillflow changes are required.
"""

import json

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


def approve_meta(sf, run_id: str) -> None:
    """Approve the brief and drive the run through its `finalize` tool step.

    Approving the gather checkpoint routes the run to the `finalize` tool step,
    which emits the project artifacts (project_brief.md, spec.md, step1_goals.json)
    inline during ``advance_run``; the graph's node_reached end-condition then
    completes the run.

    The finalize tool is the SOLE producer of those artifacts, so this does NOT
    force-complete a stuck run — a meta run that fails to emit must surface that
    failure (``RuntimeError``) so the caller does not start DPE on missing
    artifacts. Idempotent if already completed.
    """
    run = sf.get_run(run_id)
    if run and run.get("status") == "completed":
        return

    if run and run.get("status") == "paused":
        sf.approve_checkpoint(run_id)

    # Drive the now-running graph to terminal (finalize tool runs inline). The
    # 2-step path (approve → finalize → complete) needs only a couple advances;
    # the budget is a stall guard, not a normal code path.
    for _ in range(8):
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
            sf.advance_run(run_id)
        except Exception as e:
            # A raising finalize tool (e.g. an incomplete brief) lands here.
            raise RuntimeError(f"meta run {run_id} finalize step errored: {e}") from e

    raise RuntimeError(f"meta run {run_id} did not finalize within the step budget")
