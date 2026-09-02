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

from typing import Any, Dict, List, Optional


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


def compute_cache_stats_batch(run_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Aggregate per-run stats (batch mode).

    With per-project trace DBs, a single cross-database query is no longer
    possible.  Instead, iterate each run, query its per-project trace DB via
    ``compute_cache_stats_per_step``, and fold the per-step stats with
    ``merge_stats``.  Run IDs with no token_usage traces are absent from the
    dict (callers treat missing keys as zero/no-data).
    """
    if not run_ids:
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    for run_id in run_ids:
        try:
            per_step = compute_cache_stats_per_step(run_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to compute cache stats for run %s, skipping: %s",
                run_id, e
            )
            continue
        if not per_step:
            continue
        merged: Optional[Dict[str, Any]] = None
        for s in per_step.values():
            merged = merge_stats(merged, s)
        result[run_id] = merged
    return result
