"""What a project id is allowed to be — defined once, because it was not.

A project id is a filesystem path component (`ws.setup_workspace(pid)`,
`ws._get_secure_path(pid)`, `api/project_routers` interpolates it into a path)
AND it is rendered into a `{@html}` block in the SPA's delete confirmation. Its
value can arrive straight from a model: `core/meta_agent` reads
`args.get("project_id")` from the tool call and only falls back to slugifying.

The REST schema grew a `pattern=` first. That guarded `POST /api/projects` —
which is not the door the butler uses — so the constraint existed and the actual
path in was still open. Hence this module: one pattern, both callers, and a test
that counts the callers rather than naming one.
"""
from __future__ import annotations

import re

# Leading char is alphanumeric so an id can never be `.`, `..`, `-flag`, or a
# dotfile. No separators of any kind, so it cannot become a path.
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROJECT_ID_PATTERN = PROJECT_ID_RE.pattern


def is_valid(project_id: str | None) -> bool:
    return bool(project_id) and bool(PROJECT_ID_RE.fullmatch(project_id))


def require_valid(project_id: str | None) -> str:
    """Return the id, or raise ValueError naming what is wrong with it."""
    if not is_valid(project_id):
        raise ValueError(
            f"project_id {project_id!r} is not a valid identifier: it must be "
            f"1-64 characters matching {PROJECT_ID_PATTERN} — it becomes a "
            f"directory name and is displayed verbatim.")
    return project_id
