"""semantic_search — one MCP call to the zvec-grep sidecar's `zvec_grep_search`.

Why: a planner that does not know the words reads whole files instead
(jinyong-nicknames 2026-09-03: three 25K-token files per turn, 126K prompt by
turn 3). zg's paired benchmark (BrowseComp-Plus) cut input tokens 37.6% and
tool calls 43.5% at equal accuracy. This tool is the read side of that bet.

Transport: MCP Streamable HTTP at ZVEC_GREP_SERVER_URL (default
http://127.0.0.1:7999/mcp; docker-compose sets http://zvec-grep:7998/mcp). The
session is initialized once per process and re-initialized on a 4xx.
The `root` sent to zg is the injected project_root; no root → error (never CWD).
"""
import json
import os
import urllib.request
import urllib.error
from pathlib import Path

_DEFAULT_URL = "http://127.0.0.1:7999/mcp"
_MAX_LIMIT = 50
_INTERNAL_STORAGE_GLOB = "!**/.zvec-grep/**"
_session_id: str | None = None


def _url() -> str:
    return os.environ.get("ZVEC_GREP_SERVER_URL") or _DEFAULT_URL


def _post(payload: dict, *, session: str | None, timeout: float = 60.0):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    tok = os.environ.get("ZVEC_GREP_SERVER_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(_url(), data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
        ctype = r.headers.get("Content-Type", "")
        sid = r.headers.get("Mcp-Session-Id")
    if "text/event-stream" in ctype:
        msgs = []
        for ln in body.splitlines():
            if ln.startswith("data:"):
                try:
                    msgs.append(json.loads(ln[5:].strip()))
                except ValueError:
                    pass
        want = payload.get("id")
        for m in msgs:
            if m.get("id") == want:
                return m, sid
        return (msgs[-1] if msgs else {}), sid
    return (json.loads(body) if body.strip() else {}), sid


def _initialize() -> str | None:
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18",
                       "capabilities": {},
                       "clientInfo": {"name": "aitelier-semantic-search", "version": "1"}}}
    _, sid = _post(init, session=None)
    try:
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session=sid)
    except urllib.error.HTTPError:
        pass
    return sid


def _call(arguments: dict) -> dict:
    global _session_id
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "zvec_grep_search", "arguments": arguments}}
    for attempt in (1, 2):
        try:
            if _session_id is None:
                _session_id = _initialize()
            msg, _ = _post(body, session=_session_id)
            return msg
        except urllib.error.HTTPError as e:
            if attempt == 1 and 400 <= e.code < 500:
                _session_id = None      # stale session → re-initialize once
                continue
            raise
    return {}


def _semantic_search(query: str, limit: int = 8, globs: list | None = None,
                    fts: list | None = None, *, project_root: str = "",
                    **kwargs) -> dict:
    if not project_root or not Path(project_root).is_absolute():
        return {"error": "semantic_search: project_root must be an absolute path "
                         "(no repository root injected) — use `search`/`read`",
                "hint": "fall back to search/read"}
    q = (query or "").strip()
    if not q:
        return {"error": "semantic_search: empty query", "hint": "give a phrase or concept"}
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 8
    lim = max(1, min(lim, _MAX_LIMIT))
    args = {"root": str(Path(project_root).resolve()), "query": q, "limit": lim}
    requested_globs = [str(g) for g in globs] if globs else []
    args["globs"] = requested_globs + [_INTERNAL_STORAGE_GLOB]
    if fts:
        args["fts"] = [str(f) for f in fts]
    try:
        msg = _call(args)
    except Exception as e:                                       # sidecar down, timeout, bad URL
        return {"error": f"semantic_search unavailable: {type(e).__name__}: {e}",
                "hint": "the zvec-grep sidecar is not answering — use `search` (exact) and `read`"}
    if "error" in msg:
        err = msg["error"]
        text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return {"error": f"semantic_search: {text}",
                "hint": "index missing or query rejected — use `search`/`read`"}
    result = msg.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return {"error": "semantic_search: malformed MCP result"}
    parts = [c.get("text", "") for c in (result.get("content") or []) if c.get("type") == "text"]
    text = "\n".join(p for p in parts if p).strip()
    if result.get("isError"):
        return {"error": f"semantic_search: {text or 'tool error'}", "hint": "use `search`/`read`"}
    return {"query": q, "root": args["root"], "limit": lim,
            "content": text or "(no results)"}


def _lexical_fallback(query, limit, globs, fts, root, reason):
    """Reuse the engine's source search; report literal, not semantic results."""
    import fnmatch
    import re
    from skillflow.read_tools import unified_search
    terms = [str(t)[:128] for t in fts[:16]] if fts else re.findall(r"[\w-]+", query)[:16]
    pattern = "|".join(re.escape(t) for t in terms if t) or re.escape(query)
    result = unified_search({"working_tree": [("code", root)], "named": {}, "allowed": []},
                            pattern, max_results=min(200, limit * 4))
    positive = [str(g) for g in (globs or []) if not str(g).startswith("!")]
    negative = [str(g)[1:] for g in (globs or []) if str(g).startswith("!")]
    kept = []
    base = Path(root).resolve()
    for hit in result.get("matches", []):
        name = hit.get("path") or hit.get("file") or ""
        target = base / name
        if not name or target.is_symlink() or not target.resolve().is_relative_to(base):
            continue
        def matches(glob):
            return fnmatch.fnmatchcase(name, glob) or (
                glob.startswith("**/") and fnmatch.fnmatchcase(name, glob[3:]))
        if (positive and not any(matches(g) for g in positive)) or any(matches(g) for g in negative):
            continue
        kept.append(hit)
        if len(kept) >= limit:
            break
    text = "\n".join(str(hit) for hit in kept)
    return {"query": query, "root": root, "limit": limit,
            "mode": "literal_fallback", "degraded": True, "reason": reason[:600],
            "matching": "literal tokens, not semantic ranking or FTS-expression equivalence",
            "content": text[:8000] or "(no literal matches)",
            "truncated": bool(result.get("truncated") or len(text) > 8000),
            "fallback_error": result.get("error")}


def semantic_search(query: str, limit: int = 8, globs: list | None = None,
                    fts: list | None = None, *, project_root: str = "", **kwargs) -> dict:
    if not project_root or not Path(project_root).is_absolute() or not (query or "").strip():
        return _semantic_search(query, limit, globs, fts, project_root=project_root)
    from core.run_resources import invalidate, search_lease
    root = str(Path(project_root).resolve())
    try:
        lim = max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        lim = 8
    try:
        with search_lease(project_root) as preparation_error:
            if preparation_error:
                return _lexical_fallback(query, lim, globs, fts, root, preparation_error)
            result = _semantic_search(query, lim, globs, fts, project_root=root)
            if "error" not in result:
                return {**result, "mode": "semantic", "degraded": False}
            reason = result["error"]
            if "INDEX_MISSING" in reason or "index missing" in reason.lower():
                invalidate(root)
            return _lexical_fallback(query, lim, globs, fts, root, reason)
    except Exception as exc:
        # Without a resource lease, reading source as a fallback could race
        # terminal checkout removal. Return a visible degradation, not a retry.
        return {"query": query, "root": root, "limit": lim, "degraded": True,
                "mode": "unavailable", "reason": f"{type(exc).__name__}: {exc}"[:600],
                "content": "(search unavailable; no unleased source read performed)"}
