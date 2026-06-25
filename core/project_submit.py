"""project_submit — trigger the DPE pipeline once a brief is approved.

Extracted from ``api.project_routers.submit_project`` so the chat butler can
trigger the DPE pipeline deterministically once the meta_conversation brief is
approved (``core/meta_agent.py::_tool_approve_project_brief``): clear the
drafting gate, cache the brief in the DB (web UI panel), mark planning step
``"1"`` complete, and wake the scheduler (which creates + drives the
``dpe_default_v2`` run).

The canonical artifacts (``project/project_brief.md``, ``project/spec.md``,
``meta_conversation/finalize/step1_goals.json``) are produced by the
meta_conversation ``finalize`` tool step — skillflow owns that data-flow. This
host path deliberately writes NO files into any run's workspace.

The project must already exist (created during the conversation).
"""

import json


def seed_and_trigger(db, ws, project_id: str, brief: dict) -> dict:
    """Cache the brief, mark planning done, and wake the scheduler to run DPE.

    Artifacts are emitted by the meta ``finalize`` tool step, not here. ``ws`` is
    retained for signature stability with the existing callers.

    Returns ``{status, project_id, next_step}`` on success,
    ``{status: "already_planned"|"error", ...}`` otherwise.
    """
    from core.meta_conversation import format_brief_as_markdown
    from core.scheduler import wake_scheduler

    existing = db.get_project(project_id)
    if not existing:
        return {"status": "error", "message": f"Project '{project_id}' not found."}

    # Don't re-trigger if planning already completed.
    raw = existing.get("completed_project_steps", "[]")
    existing_steps = json.loads(raw) if isinstance(raw, str) else (raw or [])
    if all(s in existing_steps for s in ["1", "2", "3"]):
        return {"status": "already_planned", "project_id": project_id}

    # Clear the drafting gate so the scheduler can pick up this project.
    db.set_project_meta_state(project_id, None)

    # Cache the brief in the DB for the web UI panel. This is a host UI cache,
    # NOT the source of truth — the canonical project_brief.md lives in the
    # skillflow brief slot, emitted by the finalize tool step.
    db.set_project_brief(project_id, format_brief_as_markdown(brief))

    db.set_completed_project_steps(project_id, ["1"])
    wake_scheduler()
    return {"status": "submitted", "project_id": project_id, "next_step": "1"}
