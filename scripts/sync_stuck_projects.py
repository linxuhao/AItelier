#!/usr/bin/env python3
"""One-time cleanup: sync stuck project statuses.

After the inline sync fix is deployed (meta_agent sync calls + scheduler
config-name preference + approve_meta tool execution), repair all projects
whose latest skillflow run has a terminal status (completed/failed) but whose
aitelier DB project status does not match — projects that were stuck before the
fix was applied.

Usage:
    python scripts/sync_stuck_projects.py          # dry-run (default)
    python scripts/sync_stuck_projects.py --apply  # actually apply fixes
"""

import sys
import os
import json
import argparse
from pathlib import Path

import dotenv

dotenv.load_dotenv(Path(__file__).resolve().parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.datadir import db_path
from core.db_manager import DBManager


TERMINAL_STATUSES = {"completed", "failed"}


def _get_terminal_run(sf, project_id: str) -> tuple[str | None, str | None, str | None]:
    """Check the latest skillflow run for a project.

    Args:
        sf: SkillFlow orchestrator instance.
        project_id: The project to check.

    Returns:
        (run_status, run_graph_name, run_id) if the latest run is terminal,
        or (None, None, None) if no run exists or the latest run is not terminal.
    """
    try:
        all_runs = sf.list_runs(project_id)  # newest first
        if not all_runs:
            return None, None, None
        latest = all_runs[0]
        status = latest.get("status", "")
        if status in TERMINAL_STATUSES:
            return status, latest.get("graph_name"), latest.get("id")
        return None, None, None
    except Exception:
        return None, None, None


def _project_is_already_terminal(project_status: str, run_status: str) -> bool:
    """Check whether the project status already reflects the terminal run status.

    Handles exact matches (completed == completed), status-prefix matches
    (starts with 'completed:' or 'failed:'), and the 'failed:reason' pattern.
    """
    if project_status == run_status:
        return True
    # e.g. project_status="completed" matches run_status="completed"
    # e.g. project_status="failed:error" starts with "failed"
    if project_status.startswith(f"{run_status}:"):
        return True
    return False


def sync_stuck_projects(dry_run: bool = True) -> list[dict]:
    """Iterate all projects and fix those whose latest skillflow run is terminal
    but project status is mismatched.

    In dry-run mode (default), no changes are made — the function only reports
    what would be fixed.

    Args:
        dry_run: If True (default), only report what would change without applying.

    Returns:
        List of dicts with keys:
            - project_id: str
            - old_status: str
            - new_status: str
            - run_status: str
            - run_graph_name: str
        Empty list if nothing to fix.
    """
    # Lazy imports to keep module-level imports clean — the heavy dependencies
    # (skillflow, scheduler) are only loaded when this function is called.
    from core.scheduler import _sync_project_status_to_db
    from api.dependencies import get_skillflow

    db_mgr = DBManager(db_path())
    sf = get_skillflow()
    results: list[dict] = []

    projects = db_mgr.list_projects_with_stats(owner_email=None)
    print(f"Examining {len(projects)} project(s)...")

    for proj in projects:
        project_id = proj["project_id"]
        project_status = proj.get("status", "")

        run_status, run_graph_name, run_id = _get_terminal_run(sf, project_id)
        if run_status is None:
            # No terminal run found — either no runs exist or the latest run
            # is still running/paused. Skip — never touch active projects.
            continue

        # Check if the project status already reflects the terminal state
        if _project_is_already_terminal(project_status, run_status):
            continue

        old_status = project_status
        new_status = run_status

        entry = {
            "project_id": project_id,
            "old_status": old_status,
            "new_status": new_status,
            "run_status": run_status,
            "run_graph_name": run_graph_name,
        }

        if dry_run:
            results.append(entry)
            print(f"  [DRY-RUN] {project_id}: {old_status} -> {new_status} "
                  f"(run: {run_graph_name})")
        else:
            try:
                _sync_project_status_to_db(project_id)
                # Re-read to confirm the new status took effect
                updated = db_mgr.get_project(project_id)
                actual_new_status = updated.get("status", "unknown") if updated else "unknown"
                entry["new_status"] = actual_new_status
                results.append(entry)
                print(f"  [FIXED]  {project_id}: {old_status} -> {actual_new_status} "
                      f"(run: {run_graph_name})")
            except Exception as e:
                print(f"  [ERROR]  {project_id}: {e}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Sync stuck project statuses to match their latest skillflow run."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply fixes (default: dry-run — no changes made).",
    )
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Stuck Project Sync ({mode}) ===")

    changes = sync_stuck_projects(dry_run=not args.apply)

    print(f"\n=== Summary: {len(changes)} project(s) {'would be' if not args.apply else ''} fixed ===")

    # JSON output for programmatic consumption
    if changes:
        print("\n" + json.dumps(changes, indent=2))


if __name__ == "__main__":
    main()
