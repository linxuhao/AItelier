"""capabilities_available — the escape hatch for a step that was under-granted.

Least privilege fails silently by construction: a step that was not granted the
asset tools does not see an error, it sees a world in which generating art is
impossible, and the plausible next move is a ColorRect placeholder and a green
report. That failure is invisible to every gate.

This tool converts it into something sayable. It grants nothing — it lists what
the pipeline offers and what this task declared, so the agent can name the
missing capability and fail the task legibly instead of quietly working around
it.
"""


def capabilities_available(*, config_name: str = "", run_id: str = "",
                           step_id: str = "", **kwargs) -> dict:
    from api.dependencies import get_skillflow
    from core import capability_registry as caps

    sf = get_skillflow()
    if not config_name:
        # Without a pipeline name this would answer about the WHOLE registry and
        # label it "offered by this pipeline" — telling an under-granted agent it
        # holds tools it does not. Say so instead.
        return {"error": "config_name was not provided, so this cannot tell you "
                         "what THIS pipeline offers. Report the missing "
                         "capability by name to whoever planned the task."}
    pal = caps.palette(sf, config_name)
    if "error" in pal:
        return pal
    granted = []
    try:
        granted = sf._granted_capabilities(run_id, step_id) if run_id else []
    except Exception:
        granted = []
    offered = pal.get("capabilities", [])
    return {
        "pipeline": pal.get("pipeline", ""),
        "granted_to_this_task": granted,
        "offered_by_this_pipeline": [
            {"name": c["name"], "tools": c["tools"]} for c in offered],
        "offered_but_not_registered": pal.get("offered_but_not_registered", []),
        "note": ("A capability is granted per TASK CARD, by the planning step. "
                 "`granted_to_this_task` is what you actually hold. If this task "
                 "needs one of the others, say which — do not substitute a "
                 "placeholder and report success."),
    }
