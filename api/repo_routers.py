# api/repo_routers.py
# REST endpoints for repository grouping — groups projects by repo_path.

import os
from fastapi import APIRouter, Depends, HTTPException
from api.dependencies import get_db_manager, enrich_project_status
from core.db_manager import DBManager

# Imported lazily to avoid circular imports at module level.
# get_skillflow and compute_cache_stats_batch are used in _build_repo_groups.

router = APIRouter(prefix="/api/repos", tags=["repos"])


def _build_repo_groups(db: DBManager, repo_path: str | None = None) -> list[dict]:
    """Build repository groups from the runs table.

    Queries distinct ``repo_path`` values, fetches all projects per group,
    and returns enriched metadata (representative project, project count,
    last activity, etc.).

    Args:
        db: DBManager instance.
        repo_path: If given, returns only the group matching this exact path.
            When None, returns all groups.

    Returns:
        List of repo-group dicts, each containing:
        - repo_path, repo_name, repo_type, repo_url
        - project_count, representative_project_id, last_activity
        - projects: list of {project_id, name, status, updated_at}
    """
    groups: list[dict] = []

    with db.get_connection() as conn:
        # Fetch distinct repo_paths (or filtered to one)
        if repo_path is not None:
            rows = conn.execute(
                """SELECT repo_path, repo_type, repo_url
                   FROM runs
                   WHERE repo_path = ?
                     AND repo_path IS NOT NULL
                     AND repo_path != ''
                   LIMIT 1""",
                (repo_path,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT repo_path, repo_type, repo_url
                   FROM runs
                   WHERE repo_path IS NOT NULL AND repo_path != ''
                   GROUP BY repo_path""",
            ).fetchall()

        for row in rows:
            rp: str = row["repo_path"]
            rt: str | None = row["repo_type"]
            ru: str | None = row["repo_url"]

            # Fetch all projects sharing this repo_path
            project_rows = conn.execute(
                """SELECT project_id, name, status, updated_at
                   FROM runs
                   WHERE repo_path = ?
                   ORDER BY updated_at DESC""",
                (rp,),
            ).fetchall()

            if not project_rows:
                continue  # should not happen, but be defensive

            projects = [dict(p) for p in project_rows]

            # Representative = most recently updated project
            representative = projects[0]

            # Last activity = max updated_at across all projects
            last_activity = max(p["updated_at"] for p in projects)

            # Derive a human-friendly name from the path's basename
            repo_name = os.path.basename(rp) if rp else rp

            # Enrich each project with live skillflow status.
            for p in projects:
                enrich_project_status(p)

            groups.append({
                "repo_path": rp,
                "repo_name": repo_name,
                "repo_type": rt,
                "repo_url": ru,
                "project_count": len(projects),
                "representative_project_id": representative["project_id"],
                "last_activity": last_activity,
                "projects": projects,
            })

    # Batch-fetch cache stats for all projects across all groups.
    from api.dependencies import get_skillflow
    from api._cache_stats import compute_cache_stats_batch
    sf = get_skillflow()
    pid_to_uuids: dict[str, list[str]] = {}
    for g in groups:
        for p in g["projects"]:
            pid = p["project_id"]
            runs = sf.list_runs(project_id=pid)
            if runs:
                pid_to_uuids[pid] = [run["id"] for run in runs]
    uuid_list = [uid for uids in pid_to_uuids.values() for uid in uids]
    if uuid_list:
        batch_stats = compute_cache_stats_batch(uuid_list)
        for g in groups:
            for p in g["projects"]:
                pid = p["project_id"]
                uuids = pid_to_uuids.get(pid, [])
                merged: dict | None = None
                for uid in uuids:
                    s = batch_stats.get(uid)
                    if s is None:
                        continue
                    if merged is None:
                        merged = dict(s)
                    else:
                        merged["cache_hit_tokens"] += s["cache_hit_tokens"]
                        merged["cache_miss_tokens"] += s["cache_miss_tokens"]
                        total = merged["cache_hit_tokens"] + merged["cache_miss_tokens"]
                        merged["total_tokens"] = total
                        merged["hit_ratio"] = round(merged["cache_hit_tokens"] / total, 4) if total > 0 else None
                p["cache_stats"] = merged  # None if no token data
    else:
        for g in groups:
            for p in g["projects"]:
                p["cache_stats"] = None

    return groups


@router.get("")
async def list_repos(db: DBManager = Depends(get_db_manager)):
    """List all repository groups (distinct repo_path values)."""
    return _build_repo_groups(db)


@router.get("/{repo_path:path}")
async def get_repo(repo_path: str, db: DBManager = Depends(get_db_manager)):
    """Get a single repository group by its filesystem path.

    The ``{repo_path:path}`` converter captures slashes in the URL,
    so a repo path like ``/home/user/projects/my-repo`` can be passed
    as a single route parameter.
    """
    groups = _build_repo_groups(db, repo_path)
    if not groups:
        raise HTTPException(status_code=404, detail="Repository not found")
    return groups[0]
