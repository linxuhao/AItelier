"""Lessons from real runs, aimed at one version of one config.

A review verdict, a failed drive, a baseline replay that found a regression —
all of these produce a finding about the CONFIG, and all of them used to die
inside the run that produced them. The reviewer told the implementer, the loop
closed, and nothing carried "this pipeline is wrong in this way" back out.

A suggestion is that finding, made durable and made re-checkable:

    create(target, title, content)  →  open, recorded against the version live
                                       at the time
    resolve(sid, "applied", result_version=N)  →  closed, pointing at the
                                       version that fixed it

The one rule that needs a home rather than a docstring is **stale base**. A
suggestion written against v3 may already be moot at v5 — the step it complains
about may be gone, or someone may have fixed it another way. So it is recorded
against a base version and reported as `stale_base` once the config moves on,
which is a prompt to re-read it, not a reason to drop it. Applying an old-base
suggestion to latest without re-reading is how a fix lands on the wrong thing.

This module is the ONE writer, the same shape as `core/capability_registry.py`
and `core/baseline.py`: the invariants live here, not in each caller.
"""

from __future__ import annotations

import uuid

OPEN = "open"
APPLIED = "applied"
REJECTED = "rejected"
_TERMINAL = (APPLIED, REJECTED)
_ORIGINS = ("user", "agent", "system")


def _db():
    """The SERVER's DBManager, not the CLI accessor.

    `core.db_manager.get_db_manager` is documented host/CLI-only, and every
    other server-side module in `core/` goes through `api.dependencies` instead.
    These functions run inside butler tool calls, i.e. in the server: taking the
    CLI accessor there would construct a SECOND DBManager against the live file,
    and its `_init_db` runs the versioned-migration runner — which backs up and
    can rebuild tables — from inside a request handler. Falls back to the CLI
    accessor so the module still works from the TUI.
    """
    try:
        from api.dependencies import get_db_manager as _server_db
        return _server_db()
    except Exception:                                            # noqa: BLE001
        from core.db_manager import get_db_manager as _cli_db
        return _cli_db()


def _engine_reports_versions() -> bool:
    """Can this engine report content versions at all?

    Distinct from "this target has no versions". The difference decides whether
    `applied` may go unversioned: requiring a version an engine cannot supply
    blocks every resolution and makes the whole loop unusable, which is worse
    than a lesson closed without one.
    """
    try:
        from api.dependencies import get_skillflow
        return getattr(get_skillflow(), "list_graph_versions", None) is not None
    except Exception:                                            # noqa: BLE001
        return False


def _live_version(target: str) -> int | None:
    """The config's current content version, or None if it cannot be read.

    Best-effort on purpose: a suggestion is worth recording even when the engine
    is too old to have a version history, or the target is not a registered
    graph at all. Losing the finding to protect the metadata would be backwards.
    """
    try:
        from api.dependencies import get_skillflow
        lister = getattr(get_skillflow(), "list_graph_versions", None)
        if lister is None:
            return None
        from core.baseline import graph_name_of
        rows = lister(graph_name_of(target))
        return rows[0]["version"] if rows else None
    except Exception:                                            # noqa: BLE001
        return None


def create(target: str, title: str, content: str = "", *,
           origin: str = "agent", created_by: str | None = None,
           source_run_id: str | None = None,
           base_version: int | None = None) -> dict:
    """Record a suggestion against *target*'s current version."""
    target = (target or "").strip()
    title = (title or "").strip()
    if not target:
        return {"error": "target is required (the config the lesson is about)."}
    if not title:
        return {"error": "title is required — one line naming what should change."}
    if origin not in _ORIGINS:
        return {"error": f"origin must be one of {_ORIGINS}, got {origin!r}."}

    if base_version is None:
        base_version = _live_version(target)
    sid = uuid.uuid4().hex[:12]
    with _db().get_connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_suggestions "
            "(id, target, base_version, title, content, origin, created_by, "
            " source_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, target, base_version, title, content, origin, created_by,
             source_run_id))
        conn.commit()
    return {"id": sid, "target": target, "base_version": base_version,
            "status": OPEN}


def _row_to_dict(row, live: int | None) -> dict:
    d = dict(row)
    base = d.get("base_version")
    # Three values, not two. Only an OPEN suggestion can be stale — a resolved
    # one is a historical fact, and reporting it stale would invite
    # re-litigating settled work — but an open one whose version cannot be read
    # is UNKNOWN, not fresh. Reporting False there asserts "still current" about
    # a config nobody looked at, which is the whole failure this field exists to
    # prevent; it happens for every suggestion whenever the engine has no
    # version history.
    if d.get("status") != OPEN:
        d["stale_base"] = False
    elif base is None or live is None:
        d["stale_base"] = None
    else:
        d["stale_base"] = base != live
    d["live_version"] = live
    return d


def list_for(target: str | None = None, status: str | None = None) -> list[dict]:
    """Suggestions, newest first, each flagged with whether its base is stale."""
    sql = "SELECT * FROM pipeline_suggestions"
    where, params = [], []
    if target:
        where.append("target = ?")
        params.append(target)
    if status:
        where.append("status = ?")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, rowid DESC"
    with _db().get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    # One version lookup per distinct target, not per row.
    live: dict[str, int | None] = {}
    out = []
    for r in rows:
        t = r["target"]
        if t not in live:
            live[t] = _live_version(t)
        out.append(_row_to_dict(r, live[t]))
    return out


def get(suggestion_id: str) -> dict | None:
    with _db().get_connection() as conn:
        row = conn.execute("SELECT * FROM pipeline_suggestions WHERE id = ?",
                           (suggestion_id,)).fetchone()
    if not row:
        return None
    return _row_to_dict(row, _live_version(row["target"]))


def resolve(suggestion_id: str, status: str, *,
            result_version: int | None = None,
            note: str | None = None) -> dict:
    """Close a suggestion. `applied` must name the version that carries the fix.

    Without that requirement "applied" is unfalsifiable: nothing connects the
    lesson to the change that answered it, and the next reader cannot tell a
    real fix from a tidied-away one.
    """
    if status not in _TERMINAL:
        return {"error": f"status must be one of {_TERMINAL}, got {status!r}."}
    existing = get(suggestion_id)
    if not existing:
        return {"error": f"no suggestion {suggestion_id!r}."}
    if existing["status"] in _TERMINAL:
        return {"error": f"suggestion {suggestion_id} is already "
                         f"{existing['status']} — a resolved suggestion stays "
                         f"resolved; record a new one instead."}
    if status == APPLIED and result_version is None:
        result_version = _live_version(existing["target"])
        # Require a version only where one is OBTAINABLE. An engine with no
        # version history cannot supply one for any target, so demanding it
        # there rejects every `applied` — the loop's closing move — and leaves
        # the agent to either retry forever or misreport the outcome as
        # `rejected`. The invariant is worth having where it can be met; where
        # it cannot, recording the resolution beats losing it.
        if result_version is None and _engine_reports_versions():
            return {"error": "applied needs result_version — the config version "
                             "that carries the change. This config has no "
                             "recorded versions; pass one explicitly, or check "
                             "the target name with pipeline_versions."}
    # `AND status = OPEN`, not a bare `WHERE id = ?`. The read above and this
    # write are separate transactions, so the check alone is advisory: two
    # callers resolving the same suggestion both see `open`, both pass, and the
    # second silently overwrites the first — turning an `applied` with a result
    # version into a `rejected` with no trace, while telling the first caller it
    # succeeded. Let the database decide, and report losing rather than lie.
    with _db().get_connection() as conn:
        cur = conn.execute(
            "UPDATE pipeline_suggestions SET status = ?, result_version = ?, "
            "resolution_note = ?, resolved_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = ?",
            (status, result_version, note, suggestion_id, OPEN))
        conn.commit()
    if cur.rowcount == 0:
        now = get(suggestion_id)
        return {"error": f"suggestion {suggestion_id} was resolved by someone "
                         f"else first (now {now['status'] if now else 'gone'}) "
                         f"— re-read it before resolving again."}
    return {"id": suggestion_id, "status": status,
            "result_version": result_version}


def open_count(target: str) -> int:
    with _db().get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM pipeline_suggestions "
            "WHERE target = ? AND status = ?", (target, OPEN)).fetchone()
    return row["n"] if row else 0
