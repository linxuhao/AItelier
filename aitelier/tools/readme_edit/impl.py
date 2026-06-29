"""readme_edit — update an existing <project_root>/README.md and commit it.

One of four AItelier README tools (readme_create / readme_read / readme_search /
readme_edit) writing DIRECTLY into the delivered project repo. `project_root` is
auto-injected. Two modes: targeted substring replace (`old`+`new`) or full
overwrite (`content`). Errors if README.md is absent. Never raises.
"""

import subprocess
from pathlib import Path


def _commit_readme(repo: Path, msg: str) -> bool:
    """git add + commit README.md only (pathspec-scoped). Best-effort: never
    raises, returns True only if a commit was actually made."""
    if not (repo / ".git").exists():
        return False
    add = subprocess.run(["git", "add", "--", "README.md"], cwd=repo,
                         capture_output=True, text=True)
    if add.returncode != 0:
        return False
    commit = subprocess.run(
        ["git", "-c", "user.name=AItelier", "-c", "user.email=aitelier@localhost",
         "commit", "-m", msg, "--", "README.md"],
        cwd=repo, capture_output=True, text=True)
    return commit.returncode == 0


def readme_edit(*, old: str = "", new: str = "", content: str = "",
                project_root: str = "", **kwargs) -> dict:
    try:
        repo = Path(project_root).resolve() if project_root else None
        if not repo or not repo.exists():
            return {"error": f"repo not found: {project_root}"}
        path = repo / "README.md"
        if not path.exists():
            return {"edited": False,
                    "error": "README.md does not exist — use readme_create"}
        current = path.read_text(encoding="utf-8", errors="replace")
        if old:
            if old not in current:
                return {"edited": False, "error": "`old` text not found in README.md"}
            updated = current.replace(old, new)
        elif content:
            updated = content
        else:
            return {"edited": False,
                    "error": "provide either old/new (targeted) or content (full overwrite)"}
        if updated == current:
            return {"edited": False, "error": "no change (resulting text is identical)"}
        path.write_text(updated, encoding="utf-8")
        committed = _commit_readme(repo, "docs: update README.md")
        return {"edited": True, "path": str(path), "committed": committed}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
