# Technical Architecture Design — SQLite Concurrency Bugfix

## Overview

This design addresses a SQLite concurrency bug where `GET /api/runs` returns HTTP 500, causing the dashboard to redirect to reconnect. The fix involves two independent, minimal changes:

1. **SkillFlow library** (`core.py`): Add the missing `self._lock` acquisition in `_get_project_id` to serialize `sqlite3.Connection` access, matching the established locking pattern used by every other DB method in the class.
2. **AItelier API** (`api/_cache_stats.py`): Harden `compute_cache_stats_batch` so a single run's `compute_cache_stats_per_step` failure is caught, logged, and skipped — rather than crashing the entire `/api/runs` endpoint.

The design is intentionally minimal. Both fixes are one-liner or few-line changes with zero new dependencies, zero new abstractions, and zero schema changes.

---

## Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                        HTTP Request                              │
│                     GET /api/runs?config_name=...                 │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────┐
│  api/run_routers.py:62  list_all_runs()                          │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ 1. db.list_projects_with_stats(owner_email=owner)           │  │
│  │ 2. sf.list_runs(project_id=pid)  ── per project             │  │
│  │ 3. compute_cache_stats_batch(uuid_list)  ◄── FIX #2 here    │  │
│  └────────────────────────────────────────────────────────────┘  │
└───────────────┬──────────────────────────────────┬───────────────┘
                │                                  │
                ▼                                  ▼
┌───────────────────────────────┐   ┌───────────────────────────────┐
│  api/_cache_stats.py          │   │  skillflow (library)           │
│  compute_cache_stats_batch()  │   │  SkillFlow class               │
│  ┌─────────────────────────┐  │   │  ┌─────────────────────────┐  │
│  │ for run_id in run_ids:   │  │   │  │ list_runs()             │  │
│  │   try:                   │  │   │  │   └─► _get_project_id() │  │
│  │     per_step = compute_  │  │   │  │        ◄── FIX #1 here  │  │
│  │       cache_stats_per_   │──┼───┼──│        add with self.   │  │
│  │       step(run_id)       │  │   │  │        _lock: around    │  │
│  │   except Exception as e: │  │   │  │        self._conn.      │  │
│  │     logger.warning(...)  │  │   │  │        execute(...)     │  │
│  │     continue             │  │   │  └─────────────────────────┘  │
│  └─────────────────────────┘  │   │  ┌─────────────────────────┐  │
│                               │   │  │ trace_query(run_id,     │  │
│                               │   │  │   sql, params)          │  │
│                               │   │  │   └─► per-project       │  │
│                               │   │  │        trace DB conn    │  │
│                               │   │  └─────────────────────────┘  │
└───────────────────────────────┘   └───────────────────────────────┘
```

### Data Flow: Request → Response

```
Client
  │  GET /api/runs
  ▼
list_all_runs()                          [api/run_routers.py:62]
  │
  ├─► db.list_projects_with_stats()       [core/db_manager.py]
  │     Returns: List[dict] with project metadata
  │
  ├─► sf.list_runs(project_id=pid)        [skillflow/core.py]
  │     └─► _get_project_id(run_id)       ◄── FIX #1: add lock
  │           SELECT project_id FROM skillflow_runs WHERE run_id=?
  │     Returns: List[dict] with run metadata per project
  │
  └─► compute_cache_stats_batch(uuids)    [api/_cache_stats.py]
        └─► for each run_id:              ◄── FIX #2: try/except
              compute_cache_stats_per_step(run_id)
                └─► sf.trace_query(run_id, sql, (run_id,))
                      SELECT step_id, SUM(cache_hit), SUM(cache_miss)
                      FROM skillflow_trace WHERE run_id=? ...
        Returns: Dict[run_id → cache_stats_dict]
```

---

## Component List

### Component 1: SkillFlow `_get_project_id` (External Library)

- **Location**: `/home/linxuhao/.AItelier/projects/skillflow-review/src/skillflow/core.py` ~L3507–3511
- **Responsibility**: Convert a `run_id` (UUID) to its parent `project_id` (human-readable) via `SELECT project_id FROM skillflow_runs WHERE run_id = ?`
- **Bug**: `self._conn.execute(sql, (run_id,))` is called **without** holding `self._lock`, while all other DB methods (e.g., `list_graphs`, `_get_resolver`, `_get_resolver_for_run`) properly wrap their `self._conn.execute()` calls in `with self._lock:`.
- **Fix**: Add `with self._lock:` around the `self._conn.execute(sql, (run_id,))` call.
- **Interface**: No change. The method signature, return type (`str | None`), and behavior remain identical.
- **Lock Safety Audit**: All callers of `_get_project_id` (`list_runs`, `get_run_by_project`, `meta_agent.py:2472`) do **not** hold `self._lock` when calling it — they acquire the lock at their own `execute()` sites. A plain `threading.Lock` (not `RLock`) is therefore safe (no re-entrant deadlock risk).

### Component 2: `compute_cache_stats_batch` (AItelier API)

- **Location**: `./api/_cache_stats.py` L68–95
- **Responsibility**: Batch-compute per-run cache statistics by iterating over a list of `run_id` strings, calling `compute_cache_stats_per_step(run_id)` for each, and aggregating per-step stats into per-run totals. Returns `Dict[str, Dict[str, Any]]`.
- **Bug**: If `compute_cache_stats_per_step(run_id)` raises any exception (e.g., `sqlite3.InterfaceError` from the missing-lock bug, or `sqlite3.DatabaseError` from a corrupt per-project trace DB), the exception propagates up and crashes the entire `/api/runs` response with HTTP 500.
- **Fix**: Wrap the `compute_cache_stats_per_step(run_id)` call (line 89) in a `try/except Exception` block. On exception, log a warning via `logging.getLogger(__name__).warning(...)` including the `run_id`, then `continue` to the next run.
- **Interface**: No change. The function signature, return type, and behavior for successful runs remain identical. The only behavioral change is that a failing run is silently skipped (with a log warning) instead of crashing the entire batch.

### Component 3: `compute_cache_stats_per_step` (AItelier API)

- **Location**: `./api/_cache_stats.py` L32–65
- **Responsibility**: Query the per-project trace DB for `category='usage' / event='token_usage'` entries belonging to a single `run_id`, group by `step_id`, and return aggregated cache hit/miss stats. **No changes needed** — this component is the callee that may raise exceptions; the hardening is in the caller (`compute_cache_stats_batch`).

### Component 4: `list_all_runs` Endpoint (AItelier API)

- **Location**: `./api/run_routers.py` L62–123
- **Responsibility**: List all runs across all configs, enriched with cache stats via `compute_cache_stats_batch`. **No changes needed** — the endpoint is the consumer that benefits from both fixes. After the fixes, it will consistently return HTTP 200 under concurrent load and gracefully degrade when individual runs have bad trace data.

---

## Interface Contracts

### `_get_project_id` (SkillFlow, pre/post-fix)

```
Signature:  _get_project_id(self, run_id: str) -> str | None
Input:      run_id — skillflow internal UUID string
Output:     project_id string, or None if no matching run exists
Side effects: Reads from self._conn (SQLite SELECT)
Locking:    POST-FIX: acquires self._lock for the duration of the query
Thread safety: POST-FIX: serialized with all other self._conn access
```

### `compute_cache_stats_batch` (AItelier, pre/post-fix)

```
Signature:  compute_cache_stats_batch(run_ids: List[str]) -> Dict[str, Dict[str, Any]]
Input:      run_ids — list of skillflow internal run UUIDs
Output:     Dict mapping run_id → {cache_hit_tokens, cache_miss_tokens, hit_ratio, total_tokens}
            Runs with no token_usage traces are absent from the dict.
            POST-FIX: runs that raise during compute_cache_stats_per_step are also absent
                       (treated same as "no data"), with a warning logged.
Side effects: POST-FIX: may emit logging.WARNING messages for failed runs
```

### `compute_cache_stats_per_step` (unchanged)

```
Signature:  compute_cache_stats_per_step(run_id: str) -> Dict[str, Dict[str, Any]]
Input:      run_id — skillflow internal run UUID
Output:     Dict mapping step_id → {cache_hit_tokens, cache_miss_tokens, hit_ratio, total_tokens}
Exceptions: May raise sqlite3.InterfaceError, sqlite3.DatabaseError, sqlite3.OperationalError
```

---

## Error Handling Strategy

### Fix #1 (SkillFlow): Prevention

The lock fix **prevents** the `sqlite3.InterfaceError` that occurs when two threads concurrently use the same `sqlite3.Connection`. This is a preventive fix — it removes the root cause, so the error class that triggered the investigation should never occur again for `_get_project_id`.

### Fix #2 (AItelier): Defense in Depth

Even after Fix #1, other failure modes exist (corrupt trace DB, disk-full, etc.). The `try/except` in `compute_cache_stats_batch` provides defense in depth:

| Failure Scenario | Behavior (Post-Fix) |
|---|---|
| Single run's trace DB is corrupt | Warning logged; run omitted from cache stats; remaining runs return normally |
| All runs' trace DBs are corrupt | Warning logged for each; empty dict `{}` returned; endpoint returns 200 with `cache_stats: None` for all runs |
| Transient DB lock (WAL busy) | Warning logged; run omitted; other runs unaffected |
| Empty `run_ids` list | Early return `{}` (existing guard, no change) |

### Logging Contract

```python
import logging
logger = logging.getLogger(__name__)

# In compute_cache_stats_batch, on exception:
logger.warning(
    "Failed to compute cache stats for run %s, skipping: %s",
    run_id, e
)
```

The `%s` format (not f-string) follows Python logging best practices — deferred interpolation, no cost when the log level is suppressed.

---

## Technology Stack

| Layer | Technology | Notes |
|---|---|---|
| Language | Python 3.10+ | Already in use |
| Web framework | FastAPI | `run_routers.py` endpoints |
| Database (AItelier) | SQLite via `DBManager` | `db.list_projects_with_stats()` |
| Database (SkillFlow) | SQLite via `sqlite3.Connection` | `self._conn` in `SkillFlow` class |
| Locking primitive | `threading.Lock` | `self._lock` — already exists, no new import |
| Logging | `logging` stdlib | `logging.getLogger(__name__)` |
| No new dependencies | — | Both fixes use only existing imports |

---

## Testing Strategy

### Unit Tests

| Test | File | What it verifies |
|---|---|---|
| `test_compute_cache_stats_batch_skips_failing_run` | `./tests/unit/test_cache_stats.py` (new) | Mock `compute_cache_stats_per_step` to raise on run #2; verify batch returns stats for runs #1 and #3 only; verify warning is logged |
| `test_compute_cache_stats_batch_all_fail` | Same | All runs raise; verify returns `{}` with no crash; verify N warnings logged |
| `test_compute_cache_stats_batch_empty_input` | Same | Verify existing early-return `{}` still works |
| `test_compute_cache_stats_batch_partial_data` | Same | Some runs return empty dicts (no token data) — verify they're absent from result (existing behavior preserved) |

### Integration Tests

| Test | File | What it verifies |
|---|---|---|
| `test_list_runs_concurrent` | `./tests/integration/test_run_routers.py` (extend) | Send 10 concurrent `GET /api/runs` requests; verify all return HTTP 200; verify no `sqlite3.InterfaceError` in server logs |
| `test_list_runs_with_corrupt_trace_db` | Same | Corrupt one project's trace DB file; verify `GET /api/runs` returns 200 with cache stats for remaining projects |

### Manual Verification

```bash
# Concurrent load test
for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/runs &
done
wait
# Expected: all 200

# Error injection (after mocking trace_query to raise)
curl -s http://localhost:8000/api/runs | jq '.runs[0].cache_stats'
# Expected: cache_stats present for healthy runs, null for the corrupted one
```

---

## Edge Cases Addressed

1. **Contention window**: Fix #1 closes the window entirely by serializing `_get_project_id` with all other DB access.
2. **Nested lock (deadlock)**: Audited — no caller holds `self._lock` when calling `_get_project_id`. Safe with `threading.Lock`.
3. **Empty run_ids**: Existing early-return guard preserved.
4. **All runs fail in batch**: Returns `{}` gracefully (same as "no data").
5. **Partial failure in batch**: Failed runs are omitted; successful runs return normally.
6. **Log volume**: Only one warning per failed run per request. Not a concern for production.

---

## Rollback & Safety

Both fixes are **reversible with zero data impact**:

- **Fix #1 (SkillFlow)**: Removing `with self._lock:` reverts to the original (buggy) behavior. No schema changes, no data migration, no side effects. The lock is acquired and released within a single method call — it holds no state across calls.
- **Fix #2 (AItelier)**: Removing the `try/except` reverts to the original (brittle) behavior. The catch block only logs and skips — it mutates no data.

No backup/snapshot is needed because:
- No database schema migrations
- No data writes, deletes, or modifications
- No file-system changes outside the two source files
- Both fixes are purely additive (lock acquisition + exception handler)

---

## Extensibility Considerations

While this design is intentionally minimal, the fix pattern in Fix #2 can serve as a template for hardening other batch operations that iterate over runs. If other endpoints in the future need to process runs in a batch and tolerate partial failures, the same `try/except` + `logger.warning` + `continue` pattern should be used.

The lock fix (Fix #1) should also prompt a broader audit of the SkillFlow codebase for any other `self._conn.execute()` calls that may lack lock protection. However, the SOTA research confirmed this was the only unprotected call site.

---

## File Change Summary

| File | Change | Lines |
|---|---|---|
| `/home/linxuhao/.AItelier/projects/skillflow-review/src/skillflow/core.py` | Add `with self._lock:` around `self._conn.execute()` in `_get_project_id` | ~L3507–3511 |
| `./api/_cache_stats.py` | Wrap `compute_cache_stats_per_step(run_id)` in `try/except` in `compute_cache_stats_batch` | L89 |
| `./tests/unit/test_cache_stats.py` (new) | Unit tests for batch error handling | — |
| `./tests/integration/test_run_routers.py` (extend) | Concurrent + error-injection integration tests | — |
