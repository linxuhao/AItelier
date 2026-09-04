"""completed_cards — which loop items this run has already dispatched.

Context source for the step-3 reviewer on a goal-loop iteration. The PM must
re-emit every card file (`tasks_manifest_complete` requires a card on disk for
each manifest id), so on disk a completed card is indistinguishable from a
pending one — and the engine skips completed ids and never re-runs them. The
reviewer needs the list to know what is actually up for dispatch.

The project is derived from the injected ``workspace_root``
(``<workspaces_dir>/<project_id>``); the completed set comes from the same
query the 3_budget gate uses. Any failure reads as "nothing completed" so the
worst case is the old behaviour, never a wrong list.
"""
from __future__ import annotations

from pathlib import Path


def _project_id(workspace_root: str) -> str:
    try:
        from core.datadir import workspaces_dir
        rel = Path(workspace_root).resolve().relative_to(Path(workspaces_dir()).resolve())
        return rel.parts[0] if rel.parts else ""
    except Exception:
        return ""


def completed_cards(*, workspace_root: str = "", **kwargs) -> dict:
    pid = _project_id(workspace_root)
    done: list[str] = []
    if pid:
        try:
            from aitelier.tools.task_budget_check.impl import _completed_loop_items
            done = sorted(_completed_loop_items(pid))
        except Exception:
            done = []
    if not done:
        content = ("[completed_cards] no card has been dispatched yet in this run: "
                   "every card in the breakdown is pending.")
    else:
        content = (f"[completed_cards] {len(done)} card(s) are ALREADY COMPLETE in this run's "
                   f"task loop and will NOT run again, whatever the manifest says — the PM "
                   f"re-emits their files because the manifest gate requires a card on disk "
                   f"for every id. Do NOT review them as pending work and do NOT block the "
                   f"plan because their premise is stale or their acceptance is already met; "
                   f"review only the ids NOT in this list: " + ", ".join(done))
    return {"completed": done, "content": content}
