"""Validate agreement between the complete candidate's manifest and task cards."""

import json
from pathlib import Path


def _project_id_of(root: Path) -> str:
    """The project a step staging dir belongs to, from the workspace layout
    (``<workspaces_dir>/<project_id>/<graph>/<step>.tmp``). A validation tool is
    handed only the staging dir; the layout is AItelier's own convention."""
    try:
        from core.datadir import workspaces_dir
        rel = Path(root).resolve().relative_to(Path(workspaces_dir()).resolve())
        return rel.parts[0] if rel.parts else ""
    except Exception:
        return ""


def _stale_ids(named: list, root: Path) -> list:
    """Ids already in the live run's loop completed_items; [] on any doubt."""
    try:
        from aitelier.tools.task_budget_check.impl import _completed_loop_items
        done = _completed_loop_items(_project_id_of(root))
    except Exception:
        return []
    return [t for t in named if t in done]


def tasks_manifest_complete(files: list[str] | None = None, *,
                            workspace_root: str = "", step_dir: str = "",
                            out_dir: str = "", **_ignored) -> dict:
    # A VALIDATION tool is handed `workspace_root` — StepValidator sets it to
    # this step's staging dir (see skillflow file_exists, the same contract).
    # Written first against `step_dir`, which nothing passes, so the tool looked
    # in "." and failed every run with "tasks_manifest.json not found at
    # tasks_manifest.json" — a validation that blocks the step it was added to
    # protect. The other two names are kept so the same function also works when
    # called as an ordinary tool step.
    root = Path(workspace_root or step_dir or out_dir or ".")
    manifest_path = root / "tasks_manifest.json"
    if not manifest_path.is_file():
        return {"passed": False,
                "error": f"tasks_manifest.json not found at {manifest_path}"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"passed": False, "error": f"tasks_manifest.json unreadable: {e}"}

    named: list[str] = []
    for group in manifest.get("execution_order") or []:
        if isinstance(group, str):
            named.append(group)
        else:
            named.extend(str(t) for t in (group or []))

    # Stale ids: a goal-loop re-plan made only of ids the task loop has ALREADY
    # dispatched runs nothing (skillflow keeps completed_items across re-entry).
    # This check sat in the 3_budget tool node, but the owner's checkpoint is
    # on step 3 — so the PM re-emitted its four r3c_* ids twice (R3b, 2026-09-03)
    # and the owner saw it before the gate did. A validation failure comes back
    # to the PM as "[Previous Attempt Failed Validation — MUST FIX]" instead.
    stale = _stale_ids(named, root)
    if stale and len(stale) == len(named):
        return {"passed": False, "stale_ids": stale,
                "error": (f"Every task id in the manifest ({', '.join(stale)}) has "
                          f"ALREADY been dispatched by this run's task loop "
                          f"(completed_items). The engine skips a completed id and "
                          f"never re-runs it, so this breakdown would run NOTHING. "
                          f"A fix must be a NEW card with a NEW id (e.g. "
                          f"fix_<defect>_2); write the new cards and manifest now.")}

    cards_dir = root / "tasks"
    on_disk = {p.stem for p in cards_dir.glob("*.json")} if cards_dir.is_dir() else set()

    missing = [t for t in named if t not in on_disk]
    orphan = sorted(on_disk - set(named))

    if not missing and not orphan:
        return {"passed": True, "tasks": len(named),
                "summary": f"{len(named)} task(s), every card present"}

    parts = []
    if missing:
        parts.append(
            f"tasks_manifest.json names {len(missing)} task(s) missing from the "
            f"candidate: {', '.join(missing)}. Add these cards, or remove their "
            f"entries if those tasks were intentionally dropped. Validate the "
            f"complete candidate before submitting.")
    if orphan:
        parts.append(
            f"{len(orphan)} card(s) exist but are not in execution_order: "
            f"{', '.join(orphan)} — add them to the manifest or delete them.")
    return {"passed": False, "error": " ".join(parts),
            "missing": missing, "orphan": orphan,
            "named": len(named), "on_disk": len(on_disk)}
