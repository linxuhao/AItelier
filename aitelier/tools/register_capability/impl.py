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


# A briefing rides the step's per-turn context — deliberately NOT the cacheable
# preamble — so every holder pays it on every turn. Unbounded, it is the same
# token leak this whole mechanism was built to remove, except invisible: every
# place a capability is listed shows only the first line.
_MAX_BRIEFING = 4096


def register_capability(name: str = "", tools=None, briefing: str = "",
                        **kwargs) -> dict:
    from api.dependencies import get_skillflow
    from core import capability_registry as caps

    name = (name or "").strip()
    if not name:
        return {"registered": False, "error": "name is required"}
    tool_list = [t for t in (tools or []) if isinstance(t, str) and t.strip()]
    if not tool_list:
        # The palette teaches that a tools-less capability can still do the
        # important half (inject framework kwargs) — but only the HOST can wire a
        # context_provider, because that is a Python callable, not data. So from
        # here a tools-less capability really would do nothing; say which half is
        # missing rather than contradicting the palette.
        return {"registered": False,
                "error": "tools is required. A capability with no tools is only "
                         "useful when it injects framework kwargs, and that half "
                         "is a host-side callable — it cannot be declared from "
                         "here. Grant at least one tool."}
    if len(briefing or "") > _MAX_BRIEFING:
        return {"registered": False,
                "error": f"briefing is {len(briefing)} bytes, over the "
                         f"{_MAX_BRIEFING} limit. It is re-sent on every turn of "
                         f"every step that holds this capability. Keep the rules "
                         f"that are expensive to get wrong; put the rest behind a "
                         f"tool the step can call."}
    # OWNER IS NOT A PARAMETER. It was, and that made the ownership invariant a
    # suggestion: the refusal message names the owner that would win, and an
    # agent's most natural repair is to echo it back. Passing owner="host" then
    # overwrites a HOST capability — `stateful` loses its context_provider (the
    # state_dir injection dies deployment-wide, persisted, re-applied by the boot
    # scan on every restart), or `tool_creation` is rewritten to grant repo_apply
    # / repo_delete to the very step that holds it.
    r = caps.define(get_skillflow(), name, tools=tool_list,
                    briefing=briefing or "", owner=f"gen:{name}",
                    persist=True)
    if not r.get("ok"):
        return {"registered": False, "error": r.get("error", "unknown error")}
    return {"registered": True, "name": name, "tools": tool_list,
            "owner": r.get("owner"),
            "next": (f"declare it on a step as `capability: {name}`, and if a "
                     f"task card may declare it, add it to the graph's "
                     f"top-level `capabilities:` offer list")}
