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

    # A `failed:*` project is refused FIRST — before already_planned and before
    # any mutation. Two review rounds taught the ordering: (1) the scheduler's
    # NB-5 leaves a failed run dormant (only POST /retry reactivates), so
    # accepting produced "submitted" + no_run forever; (2) the check originally
    # sat AFTER the already_planned early-return — and a run that failed in
    # task_loop or step 5 has all three planning steps synced complete, so the
    # COMMON failure class got a success-shaped "already_planned" and never saw
    # the directions; (3) it also sat after three mutations, so a refused
    # submit had already overwritten the cached brief and reset
    # completed_project_steps — /retry then resumed the old run under
    # half-clobbered state.
    status = (existing.get("status") or "")
    if status.startswith("failed:"):
        return {"status": "error", "project_id": project_id,
                "message": (f"project '{project_id}' has a failed run — resume "
                            f"it with POST /api/projects/{project_id}/retry, "
                            f"or start a new project for the new brief")}

    # Don't re-trigger if planning already completed.
    raw = existing.get("completed_project_steps", "[]")
    existing_steps = json.loads(raw) if isinstance(raw, str) else (raw or [])
    if all(s in existing_steps for s in ["1", "2", "3"]):
        return {"status": "already_planned", "project_id": project_id}

    # Host-side brief guard: the DPE researcher reads the FINALIZED brief from the
    # meta_conversation finalize step (step1_goals.json). If it's absent, the build
    # would run brief-less and hallucinate a project — refuse to trigger. The
    # proper flow (butler → meta finalize → seed_and_trigger) always has it; a
    # direct start that skipped meta does not. (skillflow's required-context flag
    # on step 1 also catches this at run time; this fails earlier, at submit, with
    # a clear message.)
    def _refuse(message: str) -> dict:
        # Returning {"status": "error"} does NOT stop the build on its own: the
        # caller (api/project_routers.py::submit_project) has already cleared the
        # drafting gate, and the scheduler starts a DPE run for ANY project that
        # is 'planning' and not 'drafting' — nothing downstream reads this return
        # value. Live: a submit this guard refused still got a dpe_default run
        # created on the next tick, which then spun on "Required context source
        # resolved to no content: finalize" for 47 minutes at running:1. Re-arm
        # the drafting gate (the existing "brief isn't ready" flag, honoured by
        # both get_next_active_project and _get_or_create_skillflow_run) so the
        # refusal actually holds.
        db.set_project_meta_state(project_id, "drafting")
        return {"status": "error", "project_id": project_id, "message": message}

    try:
        from api.dependencies import get_skillflow
        goals = (get_skillflow()._workspace.get_project_path(project_id)
                 / "meta_conversation" / "finalize" / "step1_goals.json")
        if not (goals.is_file() and goals.read_text(encoding="utf-8").strip()):
            return _refuse(
                "Cannot start the build: no finalized brief (the meta "
                "conversation must produce step1_goals.json first). Start the "
                "build through the butler / meta conversation, not directly.")
    except Exception as e:
        # Fail CLOSED. This used to be `pass` ("never block the proper flow on a
        # guard-internal error"), but a guard whose whole job is to refuse must
        # not degrade to "allow" — that starts exactly the brief-less build it
        # exists to prevent, and the failure is invisible.
        return _refuse(
            f"Cannot start the build: the finalized-brief guard could not run "
            f"({type(e).__name__}: {e}).")

    # Clear the drafting gate so the scheduler can pick up this project.
    db.set_project_meta_state(project_id, None)

    # Cache the brief in the DB for the web UI panel. This is a host UI cache,
    # NOT the source of truth — the canonical project_brief.md lives in the
    # skillflow brief slot, emitted by the finalize tool step.
    db.set_project_brief(project_id, format_brief_as_markdown(brief))

    db.update_project(project_id, completed_project_steps=json.dumps(["1"]))

    # And put the project back in a state the poller will actually pick up.
    # `get_active_projects` selects on status IN ('planning','executing',
    # 'verifying','running'); a meta conversation leaves `running:<step>`
    # behind, which matches none of them. Without this the function returns
    # {"status": "submitted", "next_step": "1"} — wakes the scheduler, and the
    # scheduler then skips the project forever, because the row it would have
    # to select is excluded by the status it was left in. Live 2026-08-27,
    # jinyong-neigong: "submitted", then `outcome=idle` on every tick while a
    # finished brief sat on disk waiting.
    #
    # Only the meta-conversation leftovers are normalised. A project already in
    # a real pipeline status is not touched — this function's job is "the brief
    # is ready, go", not "reset whatever was happening".
    status = (db.get_project(project_id) or {}).get("status") or ""
    # (`failed:*` was refused at the top, before any mutation; `paused:*`
    # stays untouched: that is a live run at a checkpoint.)
    if status.startswith("running:") or status in ("", "drafting"):
        db.update_project(project_id, status="planning")

    wake_scheduler()
    return {"status": "submitted", "project_id": project_id, "next_step": "1"}
