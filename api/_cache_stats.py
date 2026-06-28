# api/_cache_stats.py
# Cache aggregation helper: query skillflow_trace for prompt-cache token usage
# and compute hit ratios at the per-step and run level.
#
# These functions are used by run_routers.py to enrich both the run detail
# response and the dashboard listing with aggregated cache stats.

from typing import Any, Dict, List, Optional


def _build_stats_dict(
    cache_hit_tokens: int,
    cache_miss_tokens: int,
) -> Dict[str, Any]:
    """Build a cache stats dict from raw token counts.

    Computes hit_ratio = cache_hit / (cache_hit + cache_miss).
    Returns None for hit_ratio when total tokens == 0 (division by zero guard).
    """
    total = cache_hit_tokens + cache_miss_tokens
    hit_ratio: Optional[float] = None
    if total > 0:
        hit_ratio = round(cache_hit_tokens / total, 4)
    return {
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "hit_ratio": hit_ratio,
        "total_tokens": total,
    }


def compute_cache_stats_per_step(run_id: str) -> Dict[str, Dict[str, Any]]:
    """Aggregate cache_hit_tokens and cache_miss_tokens per step_id.

    Queries skillflow_trace for category='usage' / event='token_usage' entries
    belonging to the given run, groups by step_id, and returns a dict keyed by
    step_id (string) with aggregated stats.

    Args:
        run_id: The skillflow internal run UUID (not project_id).

    Returns:
        Dict mapping step_id -> {cache_hit_tokens, cache_miss_tokens, hit_ratio, total_tokens}.
        Steps with no token_usage traces are absent from the dict (callers treat
        missing keys as zero/no-data).
    """
    from api.dependencies import get_skillflow

    sf = get_skillflow()
    sql = (
        "SELECT step_id,"
        "  SUM(COALESCE(json_extract(payload_json, '$.cache_hit_tokens'), 0)) AS cache_hit_tokens,"
        "  SUM(COALESCE(json_extract(payload_json, '$.cache_miss_tokens'), 0)) AS cache_miss_tokens "
        "FROM skillflow_trace "
        "WHERE run_id = ? AND category = 'usage' AND event = 'token_usage' "
        "GROUP BY step_id"
    )
    result: Dict[str, Dict[str, Any]] = {}
    with sf._conn:
        cursor = sf._conn.execute(sql, (run_id,))
        for row in cursor.fetchall():
            step_id = str(row[0])
            hit = int(row[1]) if row[1] is not None else 0
            miss = int(row[2]) if row[2] is not None else 0
            result[step_id] = _build_stats_dict(hit, miss)
    return result


def compute_cache_stats_batch(run_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Aggregate cache_hit_tokens and cache_miss_tokens per run (batch mode).

    Queries skillflow_trace for category='usage' / event='token_usage' entries
    belonging to any of the given run IDs, groups by run_id, and returns a dict
    keyed by internal run UUID with aggregated stats.

    Args:
        run_ids: List of skillflow internal run UUIDs.

    Returns:
        Dict mapping internal run UUID -> {cache_hit_tokens, cache_miss_tokens, hit_ratio, total_tokens}.
        Run IDs with no token_usage traces are absent from the dict (callers treat
        missing keys as zero/no-data).
    """
    if not run_ids:
        return {}

    from api.dependencies import get_skillflow

    placeholders = ",".join("?" for _ in run_ids)
    sql = (
        "SELECT run_id,"
        "  SUM(COALESCE(json_extract(payload_json, '$.cache_hit_tokens'), 0)) AS cache_hit_tokens,"
        "  SUM(COALESCE(json_extract(payload_json, '$.cache_miss_tokens'), 0)) AS cache_miss_tokens "
        "FROM skillflow_trace "
        f"WHERE run_id IN ({placeholders}) AND category = 'usage' AND event = 'token_usage' "
        "GROUP BY run_id"
    )
    sf = get_skillflow()
    result: Dict[str, Dict[str, Any]] = {}
    with sf._conn:
        cursor = sf._conn.execute(sql, run_ids)
        for row in cursor.fetchall():
            run_uuid = str(row[0])
            hit = int(row[1]) if row[1] is not None else 0
            miss = int(row[2]) if row[2] is not None else 0
            result[run_uuid] = _build_stats_dict(hit, miss)
    return result
