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
    """Close the meta conversation run as completed (brief approved).

    We do NOT drive the graph to a terminal: in skillflow a checkpoint/gate
    transition cannot resolve `to: null` (None reads as "no matching transition"
    and fails the run). So the host closes the run directly after the brief is
    seeded. This also stops the run being re-detected as an active conversation.
    """
    sf.complete_run(run_id)
