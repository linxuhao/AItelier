# api/_cache_stats.py
# Cache aggregation helper: query skillflow_trace for prompt-cache token usage
# and compute hit ratios at the per-step and run level.
#
# These functions are used by run_routers.py to enrich both the run detail
# response and the dashboard listing with aggregated cache stats.
#
# TWO QUANTITIES, TWO NAMES. A turn's usage carries prompt/completion tokens
# ALWAYS, and cache hit/miss tokens only when the provider reports them
# (Ollama Cloud never does; dec6b11 records that silence as None). So:
#   total_tokens   = prompt + completion over EVERY turn — what was processed
#   covered_tokens = hit + miss over the turns that reported — what the cache
#                    accounting can speak for
#   hit_ratio      = hit / covered_tokens, undefined when nothing was covered
# Before this, `total_tokens` WAS hit + miss. That was right while an unknown
# turn was still summed in as a miss, and became a lie the moment dec6b11 kept
# unknown turns out of the sum: measured on jinyong-numbers 2026-09-02, the
# run had processed 78.8M prompt tokens and the UI said 3.4M (4.3%); t_impl
# was 48.8M / shown 2.1M; step 3 was 12.8M / shown nothing at all. The badge
# read as a total and was a subset.

import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _build_stats_dict(
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> Dict[str, Any]:
    """Build a cache stats dict from raw token counts.

    ``hit_ratio`` = hit / (hit + miss), over the turns whose provider reported
    cache fields; None when no turn did (unknown, never 0%).
    ``total_tokens`` = prompt + completion over every turn — never a subset.
    """
    covered = cache_hit_tokens + cache_miss_tokens
    hit_ratio: Optional[float] = None
    if covered > 0:
        hit_ratio = round(cache_hit_tokens / covered, 4)
    return {
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "hit_ratio": hit_ratio,
        "covered_tokens": covered,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def merge_stats(a: Optional[Dict[str, Any]], b: Dict[str, Any]) -> Dict[str, Any]:
    """Sum two stats dicts (a may be None). The ONE place the merge lives:
    run_routers and repo_routers used to each re-derive total/ratio inline,
    and each would have had to learn the new fields separately."""
    if a is None:
        return dict(b)
    return _build_stats_dict(
        a["cache_hit_tokens"] + b["cache_hit_tokens"],
        a["cache_miss_tokens"] + b["cache_miss_tokens"],
        a.get("prompt_tokens", 0) + b.get("prompt_tokens", 0),
        a.get("completion_tokens", 0) + b.get("completion_tokens", 0),
    )


def compute_cache_stats_per_step(run_id: str) -> Dict[str, Dict[str, Any]]:
    """Aggregate token usage per step_id.

    Queries the per-project trace DB (or shared DB fallback) for
    category='usage' / event='token_usage' entries belonging to the given
    run, groups by step_id, and returns a dict keyed by step_id (string).

    Returns:
        Dict mapping step_id -> {cache_hit_tokens, cache_miss_tokens, hit_ratio,
        covered_tokens, prompt_tokens, completion_tokens, total_tokens}.
        Steps with no token_usage traces are absent from the dict.
    """
    from api.dependencies import get_skillflow

    sf = get_skillflow()
    sql = (
        "SELECT step_id,"
        "  SUM(COALESCE(json_extract(payload_json, '$.cache_hit_tokens'), 0)),"
        "  SUM(COALESCE(json_extract(payload_json, '$.cache_miss_tokens'), 0)),"
        "  SUM(COALESCE(json_extract(payload_json, '$.prompt_tokens'), 0)),"
        "  SUM(COALESCE(json_extract(payload_json, '$.completion_tokens'), 0)) "
        "FROM skillflow_trace "
        "WHERE run_id = ? AND category = 'usage' AND event = 'token_usage' "
        "GROUP BY step_id"
    )
    result: Dict[str, Dict[str, Any]] = {}
    for row in sf.trace_query(run_id, sql, (run_id,)):
        step_id = str(row[0])
        vals = [int(v) if v is not None else 0 for v in row[1:5]]
        result[step_id] = _build_stats_dict(*vals)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# The derived-stats sidecar: why the batch stopped re-reading 483 MB per boot
# ═══════════════════════════════════════════════════════════════════════════
#
# Measured (docs/investigations/run-cold-read-20261118/breakdown.json, this
# tree): the first GET /api/runs of a process spent **8,205.1 ms** of its
# 14,249.4 ms inside `compute_cache_stats_batch`, aggregating 233 per-project
# trace databases — 483 MB of reads for 662 ms of CPU. Those numbers are
# DERIVED: a GROUP BY over rows that were written once and never edited.
# Re-reading every trace database to rebuild them is what made a process
# restart put the whole 14 s back on the first dashboard read; the read cache
# (api/_read_cache.py:252-265) computes an ABSENT key inline, so it softens
# every read AFTER the first and nothing before it.
#
# So the batch persists its own answer in a sidecar SQLite file under the data
# root (`core.datadir.cache_stats_sidecar_path`) — deliberately NOT the State
# database, whose state_* tables must never contend with a dashboard read — and
# serves persisted rows, recomputing only run ids that are missing or older than
# their freshness window. When every id it was asked about already has a row,
# this module opens NO trace database at all.
#
# FIVE RULES, each load-bearing:
#
# **The row is keyed by the trace root it came from.** A row is an answer about
# `<trace root>/<project_id>/trace.db`, so the root is part of its key (and of
# the lookup): a process pointed at a different data directory can never be
# served rows derived from another one. A store that cannot NAME its trace root
# (every unit-test double; skillflow configured against the shared trace DB)
# gets nothing cached at all — exactly the behaviour before this file had a
# sidecar — because "I cannot say what this came from" must not persist.
#
# **Nothing is frozen that can still move.** `completed` is the one status
# skillflow refuses to reactivate (`reactivate_run` raises for it), so a
# completed run's numbers are final and its row is served forever. Every other
# status — including `failed`, because api/project_routers' retry path calls
# `reactivate_run` — keeps a bounded freshness window
# (`AITELIER_CACHE_STATS_FRESHNESS_SECONDS`, default 300 s), so a live run's
# numbers still move. The window is read from the row's stored `frozen` flag:
# deciding freshness per read would cost one run status lookup per row, and the
# stored flag is refreshed every time the row is.
#
# **The sidecar is derived data, so no failure in it may reach a caller.** A
# missing, corrupt or unwritable file logs a warning and falls back to the
# inline computation this module always did — never an exception, never a 500.
# A corrupt file is moved aside (`<name>.corrupt-<stamp>`) and replaced, so a
# half-written sidecar costs one inline read instead of one per dashboard poll
# for the life of the deployment.
#
# **No engine boot on the read path.** `_loaded_engine()` only reports a
# singleton this process has ALREADY built. A process whose engine is still
# cold takes today's inline path (and its per-step reads boot the engine as they
# always did); the sidecar is consulted again on the next call. Nothing here
# starts a 4.5 s boot merely to look for a cached row.
#
# **The payload shape is the response's shape.** A row stores the merged
# `_build_stats_dict` payload verbatim, so `json.loads` gives back exactly what
# the inline path would have produced for the same trace data — same keys, same
# values, same None for an uncovered ratio. A run with no token_usage rows
# stores `{}` (not the absence of a row): "this run has no token data" is
# itself an answer, and without it the runs without traces (36 of the 230 in the
# arm that populated this file) would keep opening their trace database on every
# read, forever, for an answer that cannot have changed.

# The one status skillflow will not move a run out of (`reactivate_run` refuses
# a completed run). Everything else keeps a bounded window — see the rules above.
_FROZEN_STATUSES = frozenset({"completed"})

# How long a NON-frozen row may be served before its trace database is read
# again. Bounded on purpose: a live run's token counts have to move. 300 s is
# 30 dashboard polls at the SPA's 10 s interval and still well inside "the
# numbers on the page are the run's".
_DEFAULT_FRESHNESS_SECONDS = 300.0
_FRESHNESS_ENV = "AITELIER_CACHE_STATS_FRESHNESS_SECONDS"

# Rows for runs that have aged out of every window (the dashboard shows 30 days;
# the archive is one explicit `?include_all=true` request away) are dropped so
# the sidecar does not grow with the run history forever. Pruned once per
# process per trace root, so this never becomes per-request work.
_ROW_RETENTION_SECONDS = 90 * 24 * 3600.0

# SQLite's default limit is 999 bound parameters; 400 keeps the two IN (...) by
# run_id lists comfortably inside it for a dashboard-shaped batch (~233 ids).
_SQL_CHUNK = 400

_SIDECAR_DDL = """
CREATE TABLE IF NOT EXISTS run_cache_stats (
    trace_root  TEXT    NOT NULL,
    run_id      TEXT    NOT NULL,
    stats_json  TEXT    NOT NULL,
    computed_at REAL    NOT NULL,
    frozen      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (trace_root, run_id)
)
"""

# Process-lifetime bookkeeping. Both are sets of strings, bounded by the number
# of data roots one process ever serves (one in practice).
_warned_absent_sidecar: set = set()
_pruned_trace_roots: set = set()

_CORRUPT_HINTS = ("not a database", "malformed", "corrupt", "encrypted")


def freshness_seconds() -> float:
    """The bounded freshness window for a run whose numbers can still move."""
    raw = os.getenv(_FRESHNESS_ENV)
    if not raw:
        return _DEFAULT_FRESHNESS_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("%s=%r is not a number; using %s s",
                       _FRESHNESS_ENV, raw, _DEFAULT_FRESHNESS_SECONDS)
        return _DEFAULT_FRESHNESS_SECONDS


def _loaded_engine():
    """The skillflow singleton IF this process already built it — never boots it.

    Two reasons this is a conditional attribute read and not `get_skillflow()`:
    a cold process must not pay (or start) the once-per-process engine boot just
    to look for a cached row; and the object returned has to be the SAME store
    `compute_cache_stats_per_step` reads traces through, so a host that replaced
    the accessor gets its replacement here too and the sidecar and the trace read
    can never disagree about which engine they describe.
    """
    from api import dependencies
    if getattr(dependencies, "_skillflow_instance", None) is None:
        return None
    try:
        return dependencies.get_skillflow()
    except Exception:  # noqa: BLE001 - a failed accessor means "no engine here"
        return None


def trace_root(engine) -> Optional[str]:
    """The per-project trace-database root ``engine`` reads, or None.

    None means "these numbers cannot be attributed to a trace database", and the
    batch then persists nothing (today's inline behaviour). skillflow keeps the
    root it was constructed with in ``_trace_db_path`` (api/dependencies.py
    passes WS_PATH). If a future skillflow renames that attribute this returns
    None and the write simply stops: correct numbers, base cost, no crash.
    """
    root = getattr(engine, "_trace_db_path", None)
    return str(root) if root else None


def _engine_run(engine, run_id: str):
    """The engine's own row for ``run_id``, or None when it has no such run."""
    if engine is None:
        return None
    try:
        return engine.get_run(run_id)
    except Exception:  # noqa: BLE001 - unknown is not final, and not an error
        return None


def _status_is_frozen(run) -> bool:
    """Whether a run row's status is one skillflow will not move a run out of."""
    if run is None:
        return False
    try:
        status = str(run.get("status") or "")
    except AttributeError:
        try:
            status = str(dict(run).get("status") or "")   # sqlite3.Row
        except Exception:  # noqa: BLE001
            return False
    return status.split(":", 1)[0].strip().lower() in _FROZEN_STATUSES


def _sidecar_path():
    from core import datadir
    return datadir.cache_stats_sidecar_path()


def _looks_corrupt(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(hint in message for hint in _CORRUPT_HINTS)


def _quarantine_corrupt(path, exc):
    """Move a corrupt sidecar aside and return a fresh connection (or None).

    Best effort, and never fatal: if the file cannot be moved (a read-only
    directory, say) or the replacement cannot be created, the caller falls back
    to the inline computation — which is all a corrupt sidecar was ever worth.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    aside = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(str(path), str(aside))
    except OSError as move_exc:
        logger.warning(
            "cache-stats sidecar %s is corrupt (%s) and could not be moved "
            "aside (%s); computing inline", path, exc, move_exc)
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=2.0)
        conn.execute(_SIDECAR_DDL)
        conn.commit()
    except (sqlite3.Error, OSError) as fresh_exc:
        logger.warning(
            "cache-stats sidecar %s was corrupt (%s); a fresh one could not be "
            "created either (%s); computing inline", path, exc, fresh_exc)
        return None
    logger.warning(
        "cache-stats sidecar %s was corrupt (%s); moved it to %s and started a "
        "fresh one — every run in this read is computed inline", path, exc, aside)
    return conn


def _open_sidecar():
    """Open the sidecar and make sure its table exists: ``(conn, path)``.

    Returns ``(None, path)`` after logging a WARNING whenever the file is
    missing-and-uncreatable, corrupt, or unwritable. The caller then behaves
    exactly as this module did before the sidecar existed.
    """
    try:
        path = _sidecar_path()
    except Exception as exc:  # noqa: BLE001 - path resolution must not 500
        logger.warning("cache-stats sidecar path unavailable (%s); computing "
                       "inline", exc)
        return None, None

    absent = not path.exists()
    try:
        if absent:
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=2.0)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("cache-stats sidecar %s cannot be opened (%s); computing "
                       "inline", path, exc)
        return None, path

    try:
        conn.execute(_SIDECAR_DDL)
        conn.commit()
    except sqlite3.DatabaseError as exc:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        if not _looks_corrupt(exc):
            logger.warning("cache-stats sidecar %s is unusable (%s); computing "
                           "inline", path, exc)
            return None, path
        return _quarantine_corrupt(path, exc), path

    if absent:
        # Worth a warning: this is the request that just paid the full cold
        # read. Once per path per process — the next one finds the rows.
        key = str(path)
        if key not in _warned_absent_sidecar:
            _warned_absent_sidecar.add(key)
            logger.warning(
                "cache-stats sidecar %s did not exist; this read computed every "
                "run inline and has now created it", path)
    return conn, path


def _load_rows(conn, root: str,
               run_ids: List[str]) -> Dict[str, Tuple[str, float, bool]]:
    """Persisted ``run_id -> (stats_json, computed_at, frozen)`` for this root."""
    rows: Dict[str, Tuple[str, float, bool]] = {}
    for start in range(0, len(run_ids), _SQL_CHUNK):
        chunk = run_ids[start:start + _SQL_CHUNK]
        marks = ",".join("?" * len(chunk))
        cursor = conn.execute(
            "SELECT run_id, stats_json, computed_at, frozen FROM run_cache_stats "
            "WHERE trace_root = ? AND run_id IN (" + marks + ")",
            (root, *chunk))
        for run_id, payload, computed_at, frozen in cursor.fetchall():
            rows[str(run_id)] = (payload, float(computed_at or 0.0), bool(frozen))
    return rows


def _prune(conn, root: str, now: float) -> None:
    """Drop rows no window can reach any more. Once per root per process."""
    if root in _pruned_trace_roots:
        return
    _pruned_trace_roots.add(root)
    try:
        conn.execute("DELETE FROM run_cache_stats WHERE computed_at < ?",
                     (now - _ROW_RETENTION_SECONDS,))
        conn.commit()
    except sqlite3.Error as exc:  # noqa: BLE001 - housekeeping only
        logger.debug("cache-stats sidecar prune failed: %s", exc)


def _store_rows(conn, root: str, now: float,
                computed: Dict[str, Optional[Dict[str, Any]]],
                engine) -> int:
    """Upsert one row per computed run id; returns how many were written.

    A run id the engine does not know is NOT persisted. That is not an
    optimisation, it is what makes the row an answer about a real run: an id
    nobody can resolve is an id nobody will ask about again, and writing it would
    leave the sidecar holding made-up run identities (a unit-test double's
    "run-1", say) that a later reader could be served under a real trace root.
    """
    payloads = []
    for run_id, stats in computed.items():
        if engine is not None:
            run = _engine_run(engine, run_id)
            if run is None:
                logger.debug("cache-stats sidecar: run %s is unknown to the "
                             "engine; not persisting it", run_id)
                continue
            frozen = 1 if _status_is_frozen(run) else 0
        else:
            frozen = 0
        payloads.append((root, run_id,
                         json.dumps(stats if stats is not None else {}),
                         now, frozen))
    if not payloads:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO run_cache_stats "
        "(trace_root, run_id, stats_json, computed_at, frozen) "
        "VALUES (?, ?, ?, ?, ?)",
        payloads)
    conn.commit()
    _prune(conn, root, now)
    return len(payloads)


def compute_cache_stats_batch(
    run_ids: List[str],
    *,
    source_root: Optional[str] = None,
    max_age_seconds: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    """Aggregate per-run stats (batch mode), served from a derived-stats sidecar.

    With per-project trace DBs, a single cross-database query is no longer
    possible: each run's numbers come from ``<trace root>/<project>/trace.db``.
    The answer is DERIVED data, so it is persisted in a sidecar SQLite file under
    the data root and served from there; only run ids that are missing, or older
    than their freshness window, are computed inline through
    ``compute_cache_stats_per_step``. When every requested id has a usable row,
    no trace database is opened at all (the module header states the contract,
    and why each rule is load-bearing).

    Run IDs with no token_usage traces are absent from the returned dict (callers
    treat missing keys as zero/no-data) — but they ARE persisted, as an empty
    payload, because "this run has no token data" is an answer whose absence
    would reopen its trace database on every read.

    ``source_root`` attributes the rows to a trace-database root explicitly
    (default: the loaded engine's own root, see ``trace_root``).
    ``max_age_seconds`` overrides the non-frozen freshness window for this call
    (default: ``freshness_seconds()``). A non-positive value means "recompute
    everything", which is what a witness that just inserted a trace row wants.
    """
    if not run_ids:
        return {}

    window = (freshness_seconds() if max_age_seconds is None
              else max(0.0, float(max_age_seconds)))
    root = source_root if source_root is not None else trace_root(_loaded_engine())
    conn = None
    persisted: Dict[str, Tuple[str, float, bool]] = {}
    if root:
        conn, _sidecar = _open_sidecar()
        if conn is not None:
            try:
                persisted = _load_rows(conn, root, run_ids)
            except (sqlite3.Error, OSError) as exc:
                logger.warning("cache-stats sidecar read failed (%s); computing "
                               "inline", exc)
                persisted = {}
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                conn = None

    result: Dict[str, Dict[str, Any]] = {}
    todo: List[str] = []
    now = time.time()
    for run_id in run_ids:
        row = persisted.get(run_id)
        if row is not None and (row[2] or now - row[1] <= window):
            try:
                payload = json.loads(row[0]) if row[0] else {}
            except (TypeError, ValueError):
                logger.warning(
                    "cache-stats sidecar row for run %s is unreadable; "
                    "recomputing it", run_id)
            else:
                if payload:
                    result[run_id] = payload
                continue
        todo.append(run_id)

    computed: Dict[str, Optional[Dict[str, Any]]] = {}
    for run_id in todo:
        try:
            per_step = compute_cache_stats_per_step(run_id)
        except Exception as e:
            logger.warning(
                "Failed to compute cache stats for run %s, skipping: %s",
                run_id, e
            )
            continue
        merged: Optional[Dict[str, Any]] = None
        for step_stats in per_step.values():
            merged = merge_stats(merged, step_stats)
        computed[run_id] = merged
        if merged is not None:
            result[run_id] = merged

    try:
        if computed:
            if conn is None and root is None:
                # The per-step reads boot the engine lazily, so a process that
                # was cold on entry can name its trace root by now. Asking again
                # is what lets the FIRST read of a fresh data root populate the
                # sidecar instead of leaving the next read just as expensive.
                root = (source_root if source_root is not None
                        else trace_root(_loaded_engine()))
                if root:
                    conn, _sidecar = _open_sidecar()
            if conn is not None and root:
                written = _store_rows(conn, root, time.time(), computed,
                                      _loaded_engine())
                logger.debug("cache-stats sidecar: wrote %d row(s) under %s",
                             written, root)
    except (sqlite3.Error, OSError) as exc:
        # Unwritable file/directory, a disk that filled up, a lock we could not
        # take: the computed values are already in `result`, so this read is
        # still correct — the next one just pays it again.
        logger.warning("cache-stats sidecar write failed (%s); serving the "
                       "computed values and keeping them in memory only", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return result
