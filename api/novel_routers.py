# api/novel_routers.py
# Read-only novel LEDGER lookups for a project's novel repo.
#
# Bible cards (novel/bible/) are CURRENT STATE and reach writers through the
# state_probe context pack. Per-chapter HISTORY lives in the append-only
# novel/ledger/<kind>/<entity>.jsonl and is looked up here — by external driver
# agents over HTTP (this router) or MCP (`novel_ledger_query`), and by internal
# agents through the `ledger_query` skillflow tool. Same query function for all
# three: aitelier.novel_state.query_ledger.
#
# Novel content is private (commercial source), so this is a PRIVATE read:
# require_reader here, and `novel_ledger_query` is in the MCP private-read set.

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from api import authz
from api.dependencies import get_db_manager, get_workspace_manager
from aitelier import novel_state as ns

router = APIRouter(prefix="/api/projects", tags=["novel"])

MAX_ROWS = 200


class LedgerLookupError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def resolve_novel_root(project_id: str, db, ws) -> Path:
    """The project's novel repo — the same code path the pipeline hands tools as
    ``project_root``. Refuses unknown projects and repos with no novel bible."""
    if not project_id or "/" in project_id or project_id.startswith("."):
        raise LedgerLookupError(400, f"invalid project id {project_id!r}")
    if not db.get_project(project_id):
        raise LedgerLookupError(404, f"no project '{project_id}'")
    root = ws.get_code_path(project_id)
    if root is None or not ns.bible_exists(root):
        raise LedgerLookupError(
            404, f"project '{project_id}' has no novel bible (novel/bible/overview.md)")
    return Path(root)


def ledger_lookup(project_id: str, *, db, ws, kind: str = "", name: str = "",
                  chapter: int | None = None, chapter_from: int | None = None,
                  chapter_to: int | None = None, field: str = "",
                  entry_type: str = "", limit: int = 50) -> dict:
    if kind and kind not in ns.LEDGER_KINDS:
        raise LedgerLookupError(400, f"kind must be one of {list(ns.LEDGER_KINDS)}")
    root = resolve_novel_root(project_id, db, ws)
    lim = max(1, min(int(limit or MAX_ROWS), MAX_ROWS))
    rows = ns.query_ledger(root, kind=kind or None, name=name or None,
                           chapter=chapter, chapter_from=chapter_from,
                           chapter_to=chapter_to, field=field or None,
                           entry_type=entry_type or None, limit=lim)
    return {"project_id": project_id, "count": len(rows), "limit": lim,
            "rows": rows}


@router.get("/{project_id}/novel/ledger",
            dependencies=[Depends(authz.require_reader)])
def get_novel_ledger(project_id: str,
                     kind: str = Query("", description="characters|factions|settings|threads|arcs"),
                     name: str = Query("", description="entity canonical name, e.g. 尹骁"),
                     chapter: int | None = None,
                     chapter_from: int | None = None,
                     chapter_to: int | None = None,
                     field: str = Query("", description="changes key contains this, e.g. 右臂"),
                     entry_type: str = "",
                     limit: int = 50,
                     db=Depends(get_db_manager), ws=Depends(get_workspace_manager)):
    try:
        return ledger_lookup(project_id, db=db, ws=ws, kind=kind, name=name,
                             chapter=chapter, chapter_from=chapter_from,
                             chapter_to=chapter_to, field=field,
                             entry_type=entry_type, limit=limit)
    except LedgerLookupError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
