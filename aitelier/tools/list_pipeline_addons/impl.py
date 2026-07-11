"""list_pipeline_addons — discover the pipeline addons available to compose.

A butler/agent surface over core.addon_registry.list_addons: returns each addon's
self-describing metadata (name, the base it binds to, its alias, description) so
a caller can pick addons to compose onto a base via run(base, [addons]).
Optionally filter by `base` (only addons for that base) or `query` (substring
over name/description).
"""

from core.addon_registry import list_addons as _list_addons


def list_pipeline_addons(*, base: str = "", query: str = "",
                         workspace_root: str = "", **kwargs) -> dict:
    addons = _list_addons()
    if base:
        addons = [a for a in addons if a.get("base") == base]
    if query:
        q = query.lower()
        addons = [a for a in addons
                  if q in a.get("name", "").lower() or q in a.get("description", "").lower()]
    return {"addons": addons, "count": len(addons)}
