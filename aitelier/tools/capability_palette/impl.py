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
    pal = caps.palette(sf, config_name) if config_name else caps.palette(sf)
    if "error" in pal:
        return pal
    rows = pal.get("capabilities", [])
    return {
        "pipeline": pal.get("pipeline", ""),
        "capabilities": rows,
        "declare_on_a_task_card_as": {"capabilities": [r["name"] for r in rows[:1]]},
        "offered_but_not_registered": pal.get("offered_but_not_registered", []),
        "empty_means": ("this pipeline offers no capabilities — do not put a "
                        "`capabilities` field on any task card"),
    }
