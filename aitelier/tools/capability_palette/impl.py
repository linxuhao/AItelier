"""capability_palette — the table a declarer chooses from.

Same reason `forge_palette` exists: an agent asked to name something from a set
it cannot see will invent a plausible name, and a capability name that is not
registered grants nothing at all — quietly. So the set is rendered, and the gate
that checks a card against it (see the step-3 validation) reads from the same
place.
"""


def capability_palette(*, config_name: str = "", **kwargs) -> dict:
    from api.dependencies import get_skillflow
    from core import capability_registry as caps

    sf = get_skillflow()
    if not config_name:
        # The global registry is NOT this pipeline's offer list. Reporting it as
        # one is how a planner ends up declaring a capability the engine then
        # refuses at claim time — silently, from the agent's side.
        return {"error": "config_name was not provided; cannot report what this "
                         "pipeline offers. Do not put a `capabilities` field on "
                         "a task card."}
    pal = caps.palette(sf, config_name)
    if "error" in pal:
        return pal
    rows = pal.get("capabilities", [])
    return {
        "pipeline": pal.get("pipeline", ""),
        "capabilities": rows,
        # A worked example built from rows[:1] of a sorted registry handed every
        # planner the same first name, which reads as a recommendation. Show the
        # SHAPE instead.
        "declare_on_a_task_card_as": {"capabilities": ["<name from the list "
                                                      "above, only if the task "
                                                      "needs it>"]},
        "offered_but_not_registered": pal.get("offered_but_not_registered", []),
        "empty_means": ("this pipeline offers no capabilities — do not put a "
                        "`capabilities` field on any task card"),
    }
