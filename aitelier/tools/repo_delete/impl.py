"""repo_delete — apply a step's queued file deletions to the project repo.

Runs as an ``after_deliver`` lifecycle hook right after ``repo_apply``: reads the
``_deletions.json`` manifest promoted into ``$STEP_DIR`` (``repo_apply`` is
configured to ignore it, so it never reaches the repo), ``git rm``'s each listed
repo-relative path from the project repo, commits, and clears the manifest.
No-op / no commit when the manifest is absent or empty.

The agent never runs the destructive op — it only declared intent via
``delete_file``; this deterministic hook performs and commits the removal, so it
is reviewable and git-recoverable.
"""

import json
import subprocess
from pathlib import Path

_MANIFEST = "_deletions.json"


def _safe_rel(rel) -> str | None:
    """Clean repo-relative POSIX path, or None if absolute / escaping / .git."""
    s = str(rel or "").strip().replace("\\", "/")
    if not s or s.startswith("/"):
        return None
    p = Path(s)
    if p.is_absolute() or ".." in p.parts or (p.parts and p.parts[0] == ".git"):
        return None
    return p.as_posix()


def repo_delete(source_dir: str = "", *, project_root: str = "",
                workspace_root: str = "", step_id: str = "",
                project_id: str = "", task_name: str = "", **kwargs) -> dict:
    src = Path(source_dir)
    if source_dir and not src.is_absolute():
        src = Path(workspace_root) / source_dir
    manifest = src / _MANIFEST if source_dir else None
    if not manifest or not manifest.exists():
        return {"deleted": [], "committed": False}

    try:
        queued = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(queued, list):
            queued = []
    except Exception as e:
        return {"deleted": [], "committed": False, "error": f"bad manifest: {e}"}

    repo = Path(project_root).resolve()
    removed, skipped = [], []
    for rel in queued:
        safe = _safe_rel(rel)
        if safe is None:
            skipped.append({"path": rel, "reason": "unsafe path"})
            continue
        target = (repo / safe).resolve()
        if repo != target and repo not in target.parents:
            skipped.append({"path": rel, "reason": "escapes repo"})
            continue
        if not target.exists():
            skipped.append({"path": rel, "reason": "not in repo"})
            continue
        r = subprocess.run(["git", "rm", "-f", "--", safe], cwd=repo,
                           capture_output=True, text=True)
        if r.returncode == 0:
            removed.append(safe)
        else:
            skipped.append({"path": rel, "reason": (r.stderr or r.stdout).strip()})

    # Clear the manifest so a re-used / shared step dir can't replay stale
    # deletions on a later task or run.
    try:
        manifest.unlink()
    except Exception:
        pass

    if not removed:
        out = {"deleted": [], "committed": False}
        if skipped:
            out["skipped"] = skipped
        return out

    parts = [f"step: {step_id} delete" if step_id else "delete"]
    if project_id:
        parts.append(f"[{project_id}]")
    if task_name:
        parts.append(task_name)
    parts.append(f"{len(removed)} file(s)")
    r = subprocess.run(["git", "commit", "-m", " ".join(parts)], cwd=repo,
                       capture_output=True, text=True)
    out = {"deleted": removed, "committed": r.returncode == 0}
    if skipped:
        out["skipped"] = skipped
    if r.returncode != 0:
        out["error"] = f"git commit failed: {(r.stderr or r.stdout).strip()}"
    return out
