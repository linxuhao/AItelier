"""capability_declarations_known — the gate between a declared capability and a
step that silently gets nothing.

The engine refuses a capability the graph does not offer (correctly — the offer
list is what a pipeline advertises, and a hand-edited card must not be able to
grant itself tools). But a refusal at claim time is late and quiet: the
implementer just runs without the tools. Checking the cards at the moment they
are written turns that into a validation failure the planner is told about, on
its own retry budget.

Mirrors `forge_registry_check`'s `role_model_known` rule, which exists for the
identical reason one namespace over.
"""

import json
from pathlib import Path


def capability_declarations_known(*, files=None, workspace_root: str = "",
                                  config_name: str = "", **kwargs) -> dict:
    base = Path(workspace_root or ".")
    try:
        from api.dependencies import get_skillflow
        sf = get_skillflow()
        graph = getattr(sf, "_graphs", {}).get(config_name)
        offers = set(getattr(graph, "capabilities", []) or []) if graph else set()
        registered = set(getattr(sf, "_capabilities", {}) or {})
    except Exception as e:      # no host wired (unit context) → nothing to check
        return {"all_passed": True,
                "results": [{"passed": True, "error": f"registry unavailable: {e}"}]}

    problems = []
    for card in sorted(base.glob("tasks/*.json")):
        try:
            d = json.loads(card.read_text(encoding="utf-8"))
        except Exception as e:
            continue            # card shape is another rule's job
        declared = d.get("capabilities") or []
        if isinstance(declared, str):
            declared = [declared]
        for name in declared:
            if name not in offers:
                problems.append(
                    f"{card.name}: capability {name!r} is not offered by "
                    f"'{config_name}'. Offered here: {sorted(offers) or 'none'}. "
                    "Call capability_palette and declare only what it lists, or "
                    "drop the field.")
            elif name not in registered:
                problems.append(
                    f"{card.name}: capability {name!r} is offered by "
                    f"'{config_name}' but is not registered on this deployment, "
                    "so it would grant nothing. Remove it from the card or "
                    "install the capability.")
    if problems:
        return {"all_passed": False,
                "results": [{"passed": False, "error": p} for p in problems]}
    return {"all_passed": True, "results": [{"passed": True, "error": ""}]}
