"""Read a run's durable trace, in pieces small enough to hand a model.

The trace is the answer to "why did it do that" — every prompt, model response,
tool call and review verdict, append-only and never deleted. It is also far too
large to return whole: one DPE run is 1000+ rows. So the surface is three shapes,
and each exists because the other two cannot do its job:

    list    compact lines (seq, step, category, one-line summary) — WHERE to look
    search  substring across payloads — for when you don't know where to look
    read    full payloads for an explicit, small seq range — the actual evidence

Callers pass a `ref` that may be a skillflow run id OR a project id, because the
two circulate together and making a caller guess which one a tool wants is a
needless failure (`api/run_routers._resolve_run` makes the same accommodation).

Extracted from `core/meta_agent.py`, whose butler tools now delegate here — the
MCP endpoint needs the identical semantics, and a second implementation of
"which rows count as an error" would drift from the first on its first edit.
Caller SQL is never accepted: the `where` fragments are built here.
"""

from __future__ import annotations

import json

_TRACE_COLS = "SELECT seq, step_id, category, event, payload_json, created_at"


def resolve_run_row(sf, ref: str) -> dict | None:
    """A run row from either a skillflow run id or a project id."""
    if not ref:
        return None
    run = sf.get_run(ref)
    if run:
        return run
    runs = sf.list_runs(project_id=ref)          # ORDER BY created_at DESC
    return runs[0] if runs else None


def trace_summary(payload: dict, cap: int = 220) -> str:
    """The one line of a trace payload worth reading in a list view."""
    bits: list[str] = []
    if payload.get("passed") is False:
        bits.append("passed=false")
    for key in ("error", "feedback", "preview", "text", "summary", "detail"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            bits.append(val.strip().replace("\n", " "))
            break
    if not bits:
        params = payload.get("params")
        if isinstance(params, dict):
            bits.append("params: " + ", ".join(sorted(params)[:8]))
        elif payload.get("files"):
            bits.append(f"files: {payload['files']}")
        elif payload.get("tool"):
            bits.append(f"tool: {payload['tool']}")
    text = " | ".join(bits) or json.dumps(payload, ensure_ascii=False)[:cap]
    return text[:cap]


def trace_rows(sf, ref: str, where: str, params: list, limit: int,
               order: str = "DESC") -> dict:
    """Shared SELECT against one run's trace. Never accepts caller SQL."""
    run = resolve_run_row(sf, ref)
    if not run:
        return {"error": f"No run found for '{ref}' (tried run id, then project id)."}
    sql = (f"{_TRACE_COLS} FROM skillflow_trace WHERE run_id = ? " + where +
           f" ORDER BY seq {order} LIMIT ?")
    try:
        rows = sf.trace_query(run["id"], sql, tuple([run["id"], *params, limit]))
    except Exception as e:
        return {"error": f"trace query failed: {e}"}
    return {"run": run, "rows": [dict(r) for r in rows]}


def _payload(row) -> dict:
    try:
        return json.loads(row["payload_json"])
    except (ValueError, TypeError):
        return {}


def trace_list(sf, ref: str, *, step: str = "", category: str = "",
               errors_only: bool = False, limit: int = 50,
               order: str = "desc") -> dict:
    """Compact entries — where a failed run's actual reason lives."""
    where, params = "", []
    if (step or "").strip():
        where += " AND step_id = ?"
        params.append(step.strip())
    if (category or "").strip():
        where += " AND category = ?"
        params.append(category.strip())
    limit = max(1, min(int(limit or 50), 200))
    if errors_only:
        # Widen in SQL, then drop the false hits in Python — an empty `error`
        # field is how a PASSING gate records its success, so the SQL filter
        # alone would report every green gate as a failure.
        where += " AND (payload_json LIKE '%error%' OR payload_json LIKE '%false%')"
    order_sql = "ASC" if (order or "desc").lower() == "asc" else "DESC"
    res = trace_rows(sf, ref, where, params,
                     limit * (4 if errors_only else 1), order_sql)
    if "error" in res:
        return res
    out = []
    for r in res["rows"]:
        payload = _payload(r)
        if errors_only:
            err = payload.get("error")
            failed = payload.get("passed") is False or payload.get("status") == "failed"
            if not ((isinstance(err, str) and err.strip()) or failed):
                continue
        out.append({"seq": r["seq"], "step": r["step_id"], "category": r["category"],
                    "event": r["event"], "at": r["created_at"],
                    "summary": trace_summary(payload)})
        if len(out) >= limit:
            break
    run = res["run"]
    return {"run_id": run["id"], "project_id": run.get("project_id"),
            "run_status": run.get("status"),
            "run_error": run.get("error_reason") or run.get("error"),
            "count": len(out), "entries": out,
            "hint": "trace_read(run, seq) for a full payload."}


def trace_search(sf, ref: str, query: str, *, step: str = "",
                 limit: int = 30) -> dict:
    """Substring search across one run's trace payloads."""
    query = (query or "").strip()
    if not query:
        return {"error": "query is required."}
    where, params = " AND payload_json LIKE ?", [f"%{query}%"]
    if (step or "").strip():
        where += " AND step_id = ?"
        params.append(step.strip())
    res = trace_rows(sf, ref, where, params, max(1, min(int(limit or 30), 100)))
    if "error" in res:
        return res
    entries = [{"seq": r["seq"], "step": r["step_id"], "category": r["category"],
                "event": r["event"], "summary": trace_summary(_payload(r))}
               for r in res["rows"]]
    return {"run_id": res["run"]["id"], "query": query,
            "count": len(entries), "entries": entries}


def trace_read(sf, ref: str, seq, seq_end=None) -> dict:
    """Full payloads for an explicit, small seq range."""
    try:
        start = int(seq)
    except (TypeError, ValueError):
        return {"error": "seq is required (an integer from trace_list)."}
    end = int(seq_end) if seq_end is not None else start
    if end < start:
        start, end = end, start
    end = min(end, start + 19)                   # hard cap: 20 rows
    res = trace_rows(sf, ref, " AND seq >= ? AND seq <= ?", [start, end], 20,
                     order="ASC")
    if "error" in res:
        return res
    entries = []
    for r in res["rows"]:
        try:
            payload = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            payload = r["payload_json"]          # keep the raw text, not nothing
        entries.append({"seq": r["seq"], "step": r["step_id"],
                        "category": r["category"], "event": r["event"],
                        "at": r["created_at"], "payload": payload})
    return {"run_id": res["run"]["id"], "count": len(entries), "entries": entries}


def list_runs(sf, *, config: str = "", status: str = "", project_id: str = "",
              limit: int = 30) -> dict:
    """Recent runs, newest first — the entry point when you hold no id at all."""
    limit = max(1, min(int(limit or 30), 200))
    try:
        rows = sf.list_runs(project_id=project_id) if project_id else sf.list_runs()
    except Exception as e:
        return {"error": f"could not list runs: {e}"}
    out = []
    for r in rows:
        r = dict(r)
        if config and r.get("graph_name") != config:
            continue
        if status and r.get("status") != status:
            continue
        out.append({"run_id": r.get("id"), "config": r.get("graph_name"),
                    "project_id": r.get("project_id"), "status": r.get("status"),
                    "current_node": r.get("current_node"),
                    "started_at": r.get("started_at") or r.get("created_at"),
                    "error": (r.get("error_reason") or r.get("error") or "")[:200]})
        if len(out) >= limit:
            break
    return {"count": len(out), "runs": out}
