"""register_tool — persist + live-register a generated tool.

Copies a built tool (tool.yaml + impl.py [+ tests]) into the durable, boot-scanned
generated-tools directory and injects that directory into the running ToolLoader so
`list_tools()`/`load_fn()` resolve it immediately — the mechanism that lets
pipeline_forge reference just-built tools as real primitives before the gate runs.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def generated_tools_dir() -> Path:
    """The durable, boot-scanned home for generated tools (mirrors the configs dir).

    Resolved through core.datadir so an AITELIER_HOME override (tests) is
    honored — writing to the production dir from a test run is the accident
    the datadir authority exists to prevent.
    """
    from core.datadir import tools_dir

    d = tools_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_tool_src(source_dir: Path, tool_name: str) -> Path | None:
    """Find the dir holding <tool>/tool.yaml — either source_dir/<name> or source_dir."""
    nested = source_dir / tool_name
    if (nested / "tool.yaml").exists():
        return nested
    if (source_dir / "tool.yaml").exists():
        return source_dir
    # Tolerate an implementer that wrote tool.yaml one level deeper.
    for cand in source_dir.rglob("tool.yaml"):
        if cand.parent.name == tool_name or (cand.parent / "impl.py").exists():
            return cand.parent
    return None


def register_tool(tool_name: str = "", source_dir: str = "",
                  task_name: str = "", **kwargs) -> dict:
    # A loop var like "$current_tool" is NOT interpolated in tool_params (only in
    # context paths), so inside a loop body take the tool name from the injected
    # `task_name` (the loop's current_item). Fall back to an explicit tool_name.
    name = (tool_name or "").strip()
    if not name or name.startswith("$"):
        name = (task_name or "").strip()
    tool_name = name
    if not tool_name:
        return {"registered": False, "error": "tool_name is required (no task_name injected)"}
    src_root = Path(source_dir) if source_dir else None
    if not src_root or not src_root.exists():
        return {"registered": False, "error": f"source_dir not found: {source_dir}"}

    src = _resolve_tool_src(src_root, tool_name)
    if src is None:
        return {"registered": False,
                "error": f"no tool.yaml found for '{tool_name}' under {source_dir}"}
    if not (src / "impl.py").exists():
        return {"registered": False, "error": f"impl.py missing in {src}"}

    dest = generated_tools_dir() / tool_name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)

    # Live-register: ensure the generated-tools dir is on the loader's scan path and
    # invalidate its cache so the new tool is discoverable this session.
    registered_live = False
    try:
        from api.dependencies import get_skillflow
        loader = get_skillflow()._tool_loader
        loader.add_tools_dir(generated_tools_dir())  # add_tools_dir clears the cache
        registered_live = tool_name in loader.list_tools()
    except Exception as e:  # pragma: no cover - defensive
        return {"registered": True, "live": False, "tool_name": tool_name,
                "path": str(dest), "warning": f"persisted but live-register failed: {e}"}

    return {"registered": True, "live": registered_live,
            "tool_name": tool_name, "path": str(dest)}
