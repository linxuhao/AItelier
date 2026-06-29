"""readme_read — read the project's README.md directly from the repo.

One of four AItelier README tools (readme_create / readme_read / readme_search /
readme_edit) that operate DIRECTLY on `<project_root>/README.md` in the delivered
project repo, bypassing skillflow's content-mode staging entirely. `project_root`
is auto-injected by the engine. Never raises — returns {error} on failure.
"""

from pathlib import Path


def readme_read(*, project_root: str = "", **kwargs) -> dict:
    try:
        repo = Path(project_root).resolve() if project_root else None
        if not repo or not repo.exists():
            return {"error": f"repo not found: {project_root}"}
        path = repo / "README.md"
        if not path.exists():
            return {"exists": False, "content": ""}
        return {"exists": True,
                "content": path.read_text(encoding="utf-8", errors="replace")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
