# Technical Architecture Design: Prompt-Cache Hit Ratio UI

## Overview

Add a prompt-cache hit ratio display to the AItelier web UI. The data already exists in `skillflow_trace` token_usage events. This design adds server-side aggregation and minimal frontend rendering — no new routes, no data mutations.

**Two touchpoints:**
1. **Dashboard (run list):** run-level overall cache hit ratio as a badge in the runs table.
2. **Project detail (pipeline stepper):** per-step cache hit ratio as a badge on each step pill.

**Design principle:** Extend existing endpoints with aggregated fields rather than adding new routes. Use direct SQL aggregation against `skillflow_trace` (already proven in `debugctl.py`, `scripts/cache_prefix_analysis.py`) for efficiency.

---

## Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                        Frontend                               │
│                                                               │
│  dashboard.js                    project.js                   │
│  ┌──────────────┐              ┌──────────────────┐          │
│  │ _buildRuns   │              │ _renderRun       │          │
│  │   Table()    │              │   OverviewHtml() │          │
│  │              │              │                  │          │
│  │  run.cache_  │              │  step.cache_     │          │
│  │  hit_ratio   │              │  hit_ratio       │          │
│  │  → badge     │              │  → badge on pill │          │
│  └──────┬───────┘              └────────┬─────────┘          │
│         │                               │                     │
│    GET /api/runs                   GET /api/runs/{run_id}    │
│         │                               │                     │
├─────────┼───────────────────────────────┼─────────────────────┤
│         │         Backend (FastAPI)      │                     │
│         │                               │                     │
│  ┌──────┴───────────────────────────────┴──────────────────┐ │
│  │                  api/run_routers.py                      │ │
│  │                                                          │ │
│  │  list_all_runs()              get_run_detail()           │ │
│  │  ├─ db.list_projects          ├─ sf.get_run()            │ │
│  │  │  _with_stats()             ├─ sf.get_steps()          │ │
│  │  └─ _attach_cache_stats       └─ _attach_cache_stats     │ │
│  │     _batch(run_ids)              _per_step(internal_id)  │ │
│  └────────────────────┬─────────────────────────────────────┘ │
│                       │                                        │
│  ┌────────────────────┴─────────────────────────────────────┐ │
│  │           _cache_stats.py  (new helper module)           │ │
│  │                                                          │ │
│  │  compute_cache_stats_batch(run_ids) → {run_id: stats}   │ │
│  │  compute_cache_stats_per_step(run_id) → {step_id: stats}│ │
│  │                                                          │ │
│  │  SQL: SUM(json_extract(payload_json, '$.cache_hit_...')) │ │
│  │       FROM skillflow_trace                               │ │
│  │       WHERE run_id=? AND category='usage'                │ │
│  │         AND event='token_usage'                          │ │
│  │       GROUP BY run_id / step_id                          │ │
│  └────────────────────┬─────────────────────────────────────┘ │
│                       │                                        │
│               skillflow_trace (SQLite)                         │
└──────────────────────────────────────────────────────────────┘
```

---

## Component List

### Component 1: Cache Aggregation Helper (`api/_cache_stats.py`)

**Status:** New file.

**Responsibility:** Provide two pure query functions that aggregate `cache_hit_tokens` and `cache_miss_tokens` from `skillflow_trace` and compute `hit_ratio`. Both functions access `skillflow._conn` (the internal SQLite connection) directly, matching the pattern already used in `scheduler.py`, `debugctl.py`, and `api/project_routers.py`.

**Interface:**

```python
def compute_cache_stats_per_step(run_id: str) -> dict[str, dict]:
    """Return per-step cache stats keyed by step_id.

    Returns:
        { "1": {"cache_hit_tokens": 5000, "cache_miss_tokens": 1200, "hit_ratio": 0.8065},
          "2": {"cache_hit_tokens": 0, "cache_miss_tokens": 0, "hit_ratio": null},
          ... }
    Steps with zero total tokens have hit_ratio=null (rendered as "—" in frontend).
    """

def compute_cache_stats_batch(run_ids: list[str]) -> dict[str, dict]:
    """Return run-level cache stats keyed by internal run UUID.

    Returns:
        { "uuid-1": {"cache_hit_tokens": 12000, "cache_miss_tokens": 3400, "hit_ratio": 0.7792},
          "uuid-2": {"cache_hit_tokens": 0, "cache_miss_tokens": 0, "hit_ratio": null},
          ... }
    """
```

**SQL (per-step):**
```sql
SELECT step_id,
       SUM(json_extract(payload_json, '$.cache_hit_tokens'))  AS cache_hit_tokens,
       SUM(json_extract(payload_json, '$.cache_miss_tokens')) AS cache_miss_tokens
FROM skillflow_trace
WHERE run_id = ? AND category = 'usage' AND event = 'token_usage'
GROUP BY step_id
```

**SQL (batch run-level):**
```sql
SELECT run_id,
       SUM(json_extract(payload_json, '$.cache_hit_tokens'))  AS cache_hit_tokens,
       SUM(json_extract(payload_json, '$.cache_miss_tokens')) AS cache_miss_tokens
FROM skillflow_trace
WHERE run_id IN ({placeholders}) AND category = 'usage' AND event = 'token_usage'
GROUP BY run_id
```

**Edge case handling:**
- `json_extract` returns `NULL` for rows where the field is absent → `SUM(NULL)` = `NULL` → coalesce to `0`.
- Division by zero: `hit_ratio = null` when `(cache_hit_tokens + cache_miss_tokens) == 0` (rendered as "—").
- No rows at all: return empty dict — caller treats missing key as all-zeros.

**File path:** `./api/_cache_stats.py`

---

### Component 2: Extend `GET /api/runs/{run_id}` (run detail)

**Status:** Modify existing.

**What changes in `api/run_routers.py` `get_run_detail()`:**

1. After `sf.get_steps(internal_id)` (line 163), call `compute_cache_stats_per_step(internal_id)`.
2. Attach per-step `cache_stats` to each step dict in the `run["steps"]` list comprehension.
3. Attach a top-level `run["cache_stats"]` dict containing the run-level aggregate (sum across all steps).

**New fields in the response:**

```json
{
  "cache_stats": {
    "cache_hit_tokens": 12000,
    "cache_miss_tokens": 3400,
    "hit_ratio": 0.7792
  },
  "steps": [
    {
      "step_id": "1",
      "status": "completed",
      "...": "...",
      "cache_stats": {
        "cache_hit_tokens": 5000,
        "cache_miss_tokens": 1200,
        "hit_ratio": 0.8065
      }
    }
  ]
}
```

**Rationale:** The project detail page already calls this endpoint in `_refresh()` and passes `run` to `_renderRunOverviewHtml()`. Adding the fields inline avoids a second API call on page load.

---

### Component 3: Extend `GET /api/runs` (dashboard list)

**Status:** Modify existing.

**What changes in `api/run_routers.py` `list_all_runs()`:**

1. After `db.list_projects_with_stats()` returns rows, collect the skillflow internal run IDs (currently the rows use `project_id` as the key, but skillflow runs use internal UUIDs).
2. Resolve each `project_id` to its skillflow internal UUID via `sf.list_runs(project_id=pid)` and take the latest.
3. Call `compute_cache_stats_batch(uuid_list)`.
4. Merge `cache_stats` into each run dict.

**Important design note:** The `list_all_runs()` handler currently works with `project_id` values, not skillflow internal UUIDs. It already iterates over rows and enriches them with `config_label` and `has_task_loop`. We add a batch resolution step before the enrichment loop: collect all `project_id` values, resolve each to its latest skillflow internal run UUID, call `compute_cache_stats_batch()`, then merge.

**Alternative considered (and accepted as follow-up):** Modify `list_projects_with_stats()` in `db_manager.py` to LEFT JOIN against `skillflow_trace`. Rejected for MVP because `db_manager.py` does not import skillflow and the SQL would require a cross-database query.

**New fields in each run object:**

```json
{
  "runs": [
    {
      "project_id": "my-project-1",
      "config_name": "dpe_default_v2",
      "status": "running",
      "...": "...",
      "cache_stats": {
        "cache_hit_tokens": 12000,
        "cache_miss_tokens": 3400,
        "hit_ratio": 0.7792
      }
    }
  ]
}
```

---

### Component 4: Frontend — Per-step badge in Pipeline stepper

**Status:** Modify existing.

**File:** `./web/js/views/project.js`

**What changes in `_renderRunOverviewHtml()`:**

In the step pill loop (line 553-593), after rendering retries badge (line 589), add a cache hit ratio badge:

```javascript
// After the retries badge block (line 590)
var cs = step.cache_stats || insts[0] && insts[0].cache_stats;
if (cs && cs.hit_ratio != null && cs.hit_ratio !== undefined) {
  var pct = (cs.hit_ratio * 100).toFixed(1) + "%";
  var badgeClass = "run-step-badge run-step-cache";
  if (cs.hit_ratio >= 0.7) badgeClass += " cache-badge-high";
  else if (cs.hit_ratio >= 0.3) badgeClass += " cache-badge-mid";
  else badgeClass += " cache-badge-low";
  html += '      <span class="' + badgeClass + '">' + pct + '</span>\n';
}
```

**Data source:** `step.cache_stats` comes from the extended run detail endpoint. The `step` variable in the loop is the grouped step data; since `cache_stats` is on each step INSTANCE (from the run detail), we need to pick one — use the first instance's `cache_stats` (they're identical per step_id since aggregation is across attempts).

Actually, per the run detail response shape, `cache_stats` is on each step dict in `run.steps`. The `_renderRunOverviewHtml` function builds `byStep` grouped by `step_id`. So we pick the first instance's `cache_stats`. But simpler: aggregate at the step_id level from the API response. The helper returns per-step_id stats so we can attach them at the step group level.

**Correction:** The `get_run_detail()` endpoint should attach `cache_stats` to each step dict (the instances from `sf.get_steps()`). But since multiple instances of the same step_id will all carry the same aggregated per-step_id stats, picking the first one is correct. Better yet: in the frontend, look up `run._cache_stats_by_step` (a run-level map added by the backend) so the stepper loop can reference `run._cache_stats_by_step[stepId]`.

**Revised backend approach for run detail:** Instead of attaching to each step instance, add a top-level map:

```json
{
  "cache_stats_by_step": {
    "1": {"cache_hit_tokens": 5000, "cache_miss_tokens": 1200, "hit_ratio": 0.8065},
    "2": null,
    ...
  }
}
```

This is cleaner for the frontend stepper loop.

---

### Component 5: Frontend — Run-level badge in Dashboard

**Status:** Modify existing.

**File:** `./web/js/views/dashboard.js`

**What changes:**

1. In `_buildRunsTable()` (line 331), add a "Cache" column header between "Tasks" and "Last Update" (or after "Status").
2. In `_createRow()` (line 217), in the status cell or a new cell, render the cache hit ratio badge if `project.cache_stats` exists.

**Simplest approach (minimal disruption to existing table layout):** Append the cache ratio to the status badge text. In `_createRow()` after line 260 (status badge rendering):

```javascript
// After status badge (cells[2])
var cs = project.cache_stats;
if (cs && cs.hit_ratio != null && cs.hit_ratio !== undefined) {
  var pct = (cs.hit_ratio * 100).toFixed(1) + "%";
  var cacheSpan = document.createElement("span");
  cacheSpan.className = "cache-inline-badge";
  if (cs.hit_ratio >= 0.7) cacheSpan.classList.add("cache-badge-high");
  else if (cs.hit_ratio >= 0.3) cacheSpan.classList.add("cache-badge-mid");
  else cacheSpan.classList.add("cache-badge-low");
  cacheSpan.textContent = " · Cache " + pct;
  badge.parentElement.appendChild(cacheSpan);
}
```

**Alternative (separate column):** Add "Cache" as a new column header in `_buildRunsTable()` and render into `cells[N]`. More work but cleaner. For MVP, inline badge next to status is recommended.

---

### Component 6: CSS — Cache badge styles

**Status:** Modify existing.

**File:** `./web/css/app.css`

**What to add (after line 498, the `.run-step-badge` block):**

```css
/* Cache hit ratio badge (pipeline stepper) */
.run-step-cache {
  font-size: 0.68rem;
  font-weight: 700;
  padding: 0 0.25rem;
  border-radius: 0.5rem;
  margin-left: 0.2rem;
}
.cache-badge-high { color: #2e7d32; background: rgba(46, 125, 50, 0.12); }
.cache-badge-mid  { color: #f57f17; background: rgba(245, 127, 23, 0.12); }
.cache-badge-low  { color: #c62828; background: rgba(198, 40, 40, 0.12); }

/* Cache inline badge (dashboard status cell) */
.cache-inline-badge {
  font-size: 0.8rem;
  font-weight: 600;
  margin-left: 0.3rem;
}
```

---

### Component 7: Test extension

**Status:** Modify existing.

**File:** `./tests/integration/test_run_routers.py`

**What to add:**

```python
def test_run_detail_includes_cache_stats(client):
    """GET /api/runs/{run_id} includes cache_stats at run and step level."""
    # Create a project and get its run_id
    client.post("/api/projects", json={"project_id": "cache_test_proj", "name": "CacheTest"})
    resp = client.get("/api/runs/cache_test_proj")
    assert resp.status_code == 200
    data = resp.json()
    assert "cache_stats" in data  # run-level
    assert "cache_stats_by_step" in data  # per-step map
    # With no usage traces, values should be zero/null
    assert data["cache_stats"]["cache_hit_tokens"] == 0
    assert data["cache_stats"]["hit_ratio"] is None

def test_list_all_runs_includes_cache_stats(client):
    """GET /api/runs includes cache_stats on each run."""
    client.post("/api/projects", json={"project_id": "cache_list_proj", "name": "CacheList"})
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    our_run = next((r for r in runs if r["project_id"] == "cache_list_proj"), None)
    assert our_run is not None
    assert "cache_stats" in our_run
```

---

## Data Flow

### Flow 1: Dashboard page load

```
dashboard.js:_refresh()
  → API.listAllRuns()
    → GET /api/runs
      → list_all_runs() in run_routers.py
        → db.list_projects_with_stats()  [existing, returns project rows]
        → sf.list_runs(project_id=pid) for each row  [existing pattern, get internal UUID]
        → compute_cache_stats_batch(uuid_list)  [new]
        → merge cache_stats into each run dict
  → _renderTable(configs, runs)
    → _buildPipelineSection(cfg, runs)
      → _buildRunsTable(runs)
        → _runRow(run, index)
          → _createRow(project, index)
            → render cache badge if cache_stats present
```

### Flow 2: Project detail page load / poll

```
project.js:_refresh()
  → Promise.all([api.getProject(pid), api.listTasks(pid), api.getRun(pid), ...])
    → GET /api/runs/{run_id}
      → get_run_detail() in run_routers.py
        → _resolve_run(run_id)  [existing]
        → sf.get_steps(internal_id)  [existing]
        → compute_cache_stats_per_step(internal_id)  [new]
        → attach cache_stats (run-level) + cache_stats_by_step to response
  → _renderRunOverviewHtml(run, expandedSteps)
    → step pill loop reads run.cache_stats_by_step[stepId]
    → renders cache badge if hit_ratio != null
```

---

## Technical Decisions & Rationale

| Decision | Rationale |
|----------|-----------|
| Extend existing endpoints rather than add new routes | Zero new API surface; frontend already calls these endpoints. Matching SOTA Solution 5 recommendation. |
| Direct SQL against `skillflow_trace` via `sf._conn` | Already proven in `scheduler.py`, `debugctl.py`, `project_routers.py`. Fastest path — one query per run instead of loading all trace rows into Python. |
| `hit_ratio = null` for zero-total-token steps | Distinguishes "no LLM calls" from "all misses" (0.0). Frontend renders null as "—". |
| Per-step stats as top-level map (`cache_stats_by_step`) | Cleaner for the stepper loop than picking the first instance's stats. |
| Batch query for dashboard | Avoids N+1 queries against skillflow_trace for the dashboard list. |
| Inline badge next to status (dashboard) vs. separate column | Minimal layout change; the table already has tight column widths. Can be promoted to a column later. |
| Color-coded badges (green/yellow/red) | Immediate visual signal: ≥70% green, 30-69% yellow, <30% red. |

---

## Files Changed (Summary)

| File | Action | Description |
|------|--------|-------------|
| `./api/_cache_stats.py` | **NEW** | Aggregation helper functions |
| `./api/run_routers.py` | MODIFY | Extend `get_run_detail()` + `list_all_runs()` |
| `./web/js/views/dashboard.js` | MODIFY | Cache badge in run table rows |
| `./web/js/views/project.js` | MODIFY | Cache badge on step pills in stepper |
| `./web/css/app.css` | MODIFY | Cache badge CSS classes |
| `./tests/integration/test_run_routers.py` | MODIFY | Test cache_stats in responses |

---

## Extension Points

- The `compute_cache_stats_batch()` function accepts arbitrary run UUID lists — can be reused for any future endpoint that lists multiple runs.
- The CSS color tiers (`cache-badge-high/mid/low`) can be adjusted by changing thresholds in JavaScript — no backend change needed.
- If a dedicated `/api/runs/{run_id}/cache-stats` endpoint is desired later, it can wrap the same helper functions with zero duplication.
- The `hit_ratio` field in the response is a float (0.0–1.0 or null); the frontend formats it as a percentage. This keeps the API format-agnostic.

---

## Self-Check

- [x] Design covers all MVP goals: run-level ratio on dashboard, per-step ratio in pipeline stepper.
- [x] Components have single responsibility: aggregation helper, endpoint extension, frontend rendering, CSS styling.
- [x] Researcher-recommended approach (Solution 5: dual approach — extend run detail + batch dashboard) is used.
- [x] Interfaces are specific: SQL query signatures, JSON field shapes, JS function changes. PM can decompose into sub-tasks.
- [x] No unnecessary abstraction: one new helper module, direct SQL, inline frontend changes.
- [x] `linter_manifest.json` will be produced alongside this design.
