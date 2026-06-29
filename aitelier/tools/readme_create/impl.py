"""readme_create — create <project_root>/README.md and commit it.

One of four AItelier README tools (readme_create / readme_read / readme_search /
readme_edit) that write DIRECTLY into the delivered project repo, bypassing
skillflow's content-mode staging (steps 3/5 have no repo_apply, so the old
output.fixed README never reached the repo). `project_root` is auto-injected.
Errors if README.md already exists. Never raises — returns {error} on failure.
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


def readme_create(content: str = "", *, project_root: str = "", **kwargs) -> dict:
    try:
        repo = Path(project_root).resolve() if project_root else None
        if not repo or not repo.exists():
            return {"error": f"repo not found: {project_root}"}
        path = repo / "README.md"
        if path.exists():
            return {"created": False,
                    "error": "README.md already exists — use readme_edit to update it"}
        path.write_text(content, encoding="utf-8")
        committed = _commit_readme(repo, "docs: create README.md")
        return {"created": True, "path": str(path), "committed": committed}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
