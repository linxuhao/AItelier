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


def capabilities_available(*, config_name: str = "", **kwargs) -> dict:
    from api.dependencies import get_skillflow
    from core import capability_registry as caps

    sf = get_skillflow()
    pal = caps.palette(sf, config_name) if config_name else caps.palette(sf)
    if "error" in pal:
        return pal
    offered = pal.get("capabilities", [])
    return {
        "pipeline": pal.get("pipeline", ""),
        "offered_by_this_pipeline": [
            {"name": c["name"], "tools": c["tools"]} for c in offered],
        "offered_but_not_registered": pal.get("offered_but_not_registered", []),
        "note": ("A capability is granted per TASK CARD, by the planning step. "
                 "If this task needs one that is not in your toolset, say so "
                 "and which one — do not substitute a placeholder and report "
                 "success."),
    }
