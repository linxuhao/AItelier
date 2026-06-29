"""readme_search — find lines in the project's README.md matching a query.

One of four AItelier README tools (readme_create / readme_read / readme_search /
readme_edit) operating DIRECTLY on `<project_root>/README.md`. `project_root` is
auto-injected by the engine. Never raises — returns {error} on failure.
"""

from pathlib import Path


def readme_search(query: str = "", *, ignore_case: bool = True,
                  project_root: str = "", **kwargs) -> dict:
    try:
        if not query:
            return {"error": "query is required"}
        repo = Path(project_root).resolve() if project_root else None
        if not repo or not repo.exists():
            return {"error": f"repo not found: {project_root}"}
        path = repo / "README.md"
        if not path.exists():
            return {"exists": False, "matches": [], "count": 0}
        needle = query.lower() if ignore_case else query
        matches = []
        text = path.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            hay = line.lower() if ignore_case else line
            if needle in hay:
                matches.append({"line": i, "text": line})
        return {"exists": True, "matches": matches, "count": len(matches)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
