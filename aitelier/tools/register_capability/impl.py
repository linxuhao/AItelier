"""register_capability — the forge's way to define a capability.

Mirrors `register_tool`: persist to the durable generated dir
(`~/.AItelier/capabilities/<name>.json`, boot-scanned) AND live-register into the
running SkillFlow, so a later step in the SAME run can already declare it.

Why the forge needs this at all: it can already author tools, and a tool that
needs framework-chosen state (or that only some items of a loop should carry) has
nowhere to attach without a capability. Without it the maker writes the grant by
hand onto a role — which is how a generated tool ends up computing its own
home-relative directory — the failure the capability mechanism exists to stop.
"""


def register_capability(name: str = "", tools=None, briefing: str = "",
                        owner: str = "", **kwargs) -> dict:
    from api.dependencies import get_skillflow
    from core import capability_registry as caps

    name = (name or "").strip()
    if not name:
        return {"registered": False, "error": "name is required"}
    tool_list = [t for t in (tools or []) if isinstance(t, str) and t.strip()]
    if not tool_list:
        return {"registered": False,
                "error": "tools is required — a capability that grants nothing "
                         "and injects nothing has no effect on the step"}
    r = caps.define(get_skillflow(), name, tools=tool_list,
                    briefing=briefing or "", owner=owner or f"gen:{name}",
                    persist=True)
    if not r.get("ok"):
        return {"registered": False, "error": r.get("error", "unknown error")}
    return {"registered": True, "name": name, "tools": tool_list,
            "owner": r.get("owner"),
            "next": (f"declare it on a step as `capability: {name}`, and if a "
                     f"task card may declare it, add it to the graph's "
                     f"top-level `capabilities:` offer list")}
