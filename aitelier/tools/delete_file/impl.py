"""delete_file — queue a repo file for removal at this step's delivery time.

Migration/refactor gap: the implementer can create and edit files but has no way
to REMOVE the old ones its new files supersede, so it falls back to fragile
in-place edits (hollowing files out) or un-runnable shell scripts. This records
the deletion intent in the step's draft manifest (``_deletions.json``); the
``repo_delete`` lifecycle hook applies it (git rm + commit) right after
``repo_apply``, and ``repo_apply`` is configured to ignore the manifest so it
never lands in the delivered repo.

Least privilege: the agent only DECLARES intent, with jailed repo-relative
paths. The irreversible ``git rm`` is done deterministically by the hook, is
reviewed (t_impl_review), and is fully git-recoverable.

The manifest location is resolved through ``workspace_manager`` (the single
source of truth for workspace layout), so it stays correct under a future
per-run workspace scheme with no change here.
"""

import json
from pathlib import Path

_MANIFEST = "_deletions.json"


def _validate_rel(name: str) -> str:
    """Return a clean repo-relative POSIX path, or raise ValueError.

    Strict: rejects absolute paths, '..' traversal and the .git dir rather than
    silently rewriting them — the agent must pass a repo-relative path.
    """
    n = (name or "").strip().replace("\\", "/")
    if not n:
        raise ValueError("empty path")
    p = Path(n)
    if n.startswith("/") or p.is_absolute():
        raise ValueError(f"must be repo-relative, not absolute: {name!r}")
    if ".." in p.parts:
        raise ValueError(f"path escapes the repo: {name!r}")
    if p.parts and p.parts[0] == ".git":
        raise ValueError(".git is protected")
    return p.as_posix()


def _append_deletion(draft_dir: Path, rel: str) -> int:
    """Append ``rel`` to the draft dir's deletions manifest (dedup). Returns count."""
    draft_dir.mkdir(parents=True, exist_ok=True)
    manifest = draft_dir / _MANIFEST
    try:
        queued = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(queued, list):
            queued = []
    except Exception:
        queued = []
    if rel not in queued:
        queued.append(rel)
    manifest.write_text(json.dumps(queued, indent=2), encoding="utf-8")
    return len(queued)


def delete_file(name: str, *, run_id: str = "", step_id: str = "",
                **kwargs) -> dict:
    """Queue a repo-relative file for deletion at this step's delivery time."""
    try:
        rel = _validate_rel(name)
    except ValueError as e:
        return {"error": str(e)}
    try:
        from api.dependencies import get_skillflow, get_workspace_manager
        sf = get_skillflow()
        ws = get_workspace_manager()
        run = sf.get_run(run_id) or {}
        project_id = run.get("project_id") or ""
        if not project_id:
            return {"error": f"cannot resolve project for run {run_id!r}"}
        graph_name = run.get("graph_name")
        draft = (ws._draft_dir(project_id, step_id, graph_name) if graph_name
                 else ws._draft_dir(project_id, step_id))
        pending = _append_deletion(draft, rel)
        return {"queued_for_deletion": rel, "pending_deletions": pending}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
