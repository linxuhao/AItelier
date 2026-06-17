"""project_submit — seed a project brief into the workspace and trigger DPE.

Extracted from ``api.project_routers.submit_project`` so the chat butler can
trigger the DPE pipeline deterministically once the meta_conversation brief is
approved (``core/meta_agent.py::_tool_approve_project_brief``), reusing the exact
proven path the ``/api/projects/submit`` endpoint uses: write the canonical
``project/project_brief.md`` + ``{final}/1/step1_goals.json``, mark planning
step ``"1"`` complete, clear the drafting gate, and wake the scheduler (which
creates and drives the ``dpe_default_v2`` run for the project).

The project must already exist (created during the conversation).
"""

import json


def seed_and_trigger(db, ws, project_id: str, brief: dict) -> dict:
    """Seed the brief + step-1 goals and wake the scheduler to run DPE.

    Returns ``{status, project_id, next_step}`` on success,
    ``{status: "already_planned"|"error", ...}`` otherwise.
    """
    from core.meta_conversation import format_brief_as_markdown, brief_to_step1_goals
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

    brief_md = format_brief_as_markdown(brief)
    db.set_project_brief(project_id, brief_md)

    dps_path = ws._get_secure_path(project_id)
    project_dir = dps_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "project_brief.md").write_text(brief_md, encoding="utf-8")

    goals = brief_to_step1_goals(brief)
    final_1_dir = ws._final_dir(project_id, "1")
    final_1_dir.mkdir(parents=True, exist_ok=True)
    (final_1_dir / "step1_goals.json").write_text(
        json.dumps(goals, indent=2, ensure_ascii=False), encoding="utf-8")

    db.set_completed_project_steps(project_id, ["1"])
    wake_scheduler()
    return {"status": "submitted", "project_id": project_id, "next_step": "1"}
