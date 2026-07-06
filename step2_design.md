# Technical Architecture Design

## Overview

This design addresses three bugs and one cleanup task related to pipeline run status synchronization in AItelier. The system has two execution models for skillflow pipeline runs:

- **Scheduler-driven**: DPE-owned configs (`scheduler_owned=true`) are polled by `core/scheduler.py`'s `_run_skillflow_tick()`, which calls `_sync_project_status_to_db()` on every tick.
- **Butler-driven (inline)**: Configs with `scheduler_owned=false` (code_review, skill_converter, coding_task, meta_conversation, etc.) are driven imperatively by `core/meta_agent.py` via `_run_pipeline_until_checkpoint()`.

The core failure: inline configs complete their skillflow runs but never sync status to the aitelier DB project row, leaving projects stuck in "planning" or stale intermediate states.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                      AItelier System                     │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────────┐     ┌──────────────────┐              │
│  │ meta_agent.py│     │   scheduler.py   │              │
│  │              │     │                  │              │
│  │ _tool_start_ │     │ _run_skillflow_  │              │
│  │  config_run  │     │    tick()        │              │
│  │ _tool_gener- │     │       │          │              │
│  │  ate_pipeline│     │       ▼          │              │
│  │ _tool_wait_  │     │ _sync_project_   │◄────┐        │
│  │  until_chkpt │     │  status_to_db()  │     │        │
│  │      │       │     │                  │     │        │
│  │      ▼       │     └──────────────────┘     │        │
│  │ _run_pipeline│                              │        │
│  │  _until_chkpt│─── missing sync ─────────────┤        │
│  │              │    (Bug 1 fix: add           │        │
│  │              │     _sync_project_           │        │
│  │              │     status_to_db calls)      │        │
│  └──────────────┘                              │        │
│                                                │        │
│  ┌──────────────┐                              │        │
│  │ meta_run.py  │                              │        │
│  │              │                              │        │
│  │ approve_meta │── advance_run ≠ tool exec ───┘        │
│  │              │   (Bug 3 fix: claim+execute            │
│  └──────────────┘    after advance)                      │
│                                                         │
│  ┌──────────────────────────────────────┐               │
│  │           aitelier.db                 │               │
│  │  projects: {status, config_name, …}  │               │
│  │  runs: {config_name, …}              │               │
│  └──────────────────────────────────────┘               │
│  ┌──────────────────────────────────────┐               │
│  │          skillflow.db                 │               │
│  │  runs: {project_id, graph_name, …}   │               │
│  └──────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────┘
```

### Data Flow — Butler-Driven Run Completion

```
User/Butler invokes tool
  │
  ▼
_tool_start_config_run / _tool_generate_pipeline / _tool_wait_until_checkpoint
  │
  ├─► start_config_run() → creates run in skillflow.db + aitelier.db
  │
  ├─► _run_pipeline_until_checkpoint(run_id)
  │     │
  │     ├─► sf.advance_run() → transitions graph
  │     ├─► sf.claim_next_step() → claims node
  │     ├─► runner.execute(claimed) → runs agent/tool
  │     └─► sf.confirm_step() → marks done
  │     … repeat until checkpoint / terminal
  │
  └─► **NEW**: _sync_project_status_to_db(project_id)  ← Bug 1 fix
        │
        └─► db.update_project(project_id, status=…)
```

### Data Flow — `_sync_project_status_to_db` with Config Preference

```
_sync_project_status_to_db(project_id)
  │
  ├─► run = sf.get_run_by_project(project_id)     # excludes completed
  │     │
  │     └─► if found: use it
  │
  ├─► run = sf.list_runs(project_id)[0]           # fallback, newest first
  │     │
  │     └─► **NEW**: prefer run.graph_name == project.config_name  ← Bug 2 fix
  │           │
  │           ├─► db.get_project(project_id) → get config_name
  │           └─► scan list_runs for matching graph_name first
  │
  └─► db.update_project(project_id, status=derived_status)
```

## Component List

### Component 1: Sync Call Sites in `core/meta_agent.py`

- **Responsibility**: After `_run_pipeline_until_checkpoint()` returns a terminal or checkpoint result, sync the project status to the aitelier DB so the UI reflects the actual run state.
- **Existing pattern** (already present in 3 callers): 
  ```python
  from core.scheduler import _sync_project_status_to_db
  try:
      _sync_project_status_to_db(project_id)
  except Exception:
      pass
  ```
- **Missing in 3 callers**:

| Caller | Line | `project_id` source |
|--------|------|---------------------|
| `_tool_start_config_run` | ~3337 | `pid` variable (line 3312) |
| `_tool_generate_pipeline` | ~3262 | `pid` variable (line 3240) |
| `_tool_wait_until_checkpoint` | ~3031 | `result.get("project_id")` from `_run_pipeline_until_checkpoint` return |

- **Interface**: No signature changes. Local import + try/except inline at each site.
- **Idempotency**: `_sync_project_status_to_db` is safe to call multiple times — it reads current state from skillflow and writes to aitelier DB, so repeated calls produce the same result.

### Component 2: Config-Name Preference in `core/scheduler.py`

- **Responsibility**: `_sync_project_status_to_db()` must prefer the run whose `graph_name` matches the project's original `config_name` when multiple runs exist for the same project, preventing later runs (e.g., coding_task) from shadowing the DPE run's "completed" status.
- **Current logic** (lines 790–793):
  ```python
  run = sf.get_run_by_project(project_id)
  if not run:
      all_runs = sf.list_runs(project_id)  # newest first
      run = all_runs[0] if all_runs else None
  ```
- **New logic**:
  ```python
  run = sf.get_run_by_project(project_id)
  if not run:
      all_runs = sf.list_runs(project_id)  # newest first
      if all_runs:
          proj = db.get_project(project_id)
          proj_config = proj.get("config_name", "") if proj else ""
          run = all_runs[0]  # default: newest
          if proj_config:
              for r in all_runs:
                  if r.get("graph_name") == proj_config:
                      run = r
                      break
      else:
          run = None
  ```
- **Interface**: No signature change. Internal to `_sync_project_status_to_db()`.
- **Edge case**: If no run matches the project's config_name (e.g., project config was changed or run was deleted), falls back to the most recent run — preserving the existing behavior.

### Component 3: Tool Step Execution in `approve_meta` (`core/meta_run.py`)

- **Responsibility**: `approve_meta()` must ensure the `finalize` tool step actually executes, not just have its graph state transitioned. Currently, `sf.advance_run()` transitions the graph to the finalize node but does not claim or execute the tool step.
- **Root cause**: `advance_run()` only transitions graph state. Tool steps (like `emit_project_artifacts`) require explicit `claim_next_step()` + execution + `confirm_step()` — the same pattern used by the scheduler's `_run_skillflow_tick` and by `_run_pipeline_until_checkpoint`.
- **Approach**: Accept an optional `step_runner` callable parameter. When provided, after each `advance_run` that returns a node, claim and execute the step:
  ```python
  def approve_meta(sf, run_id: str, step_runner=None) -> None:
  ```
  In the loop:
  ```python
  next_node = sf.advance_run(run_id)
  if next_node is None:
      # check terminal/paused as before
      continue
  if step_runner is not None:
      claimed = sf.claim_next_step(run_id)
      if claimed is not None:
          result = step_runner(claimed)       # sync or async — see below
          sf.confirm_step(claimed.token, result)
  ```
- **Caller update** (`core/meta_agent.py` line ~2077): Pass a step runner that uses the AgentStepRunner infrastructure already available in MetaAgent. Since the caller is in an async context (`MetaAgent._tool_approve_brief`), the step runner must be awaitable.
- **Signature**: `step_runner` is `Optional[Callable[[ClaimedStep], Awaitable[dict]]]`.
- **Backward compatibility**: When `step_runner=None`, behavior is unchanged. Existing tests (which mock `sf`) continue to work because the mocked `advance_run` + `get_run` side effects simulate completion without needing actual step execution.
- **Unit test update**: Add a test for `approve_meta` with a `step_runner` that verifies claim+execute+confirm are called.

### Component 4: One-Time Cleanup Script

- **Responsibility**: Fix all currently stuck projects whose latest skillflow run has a terminal status (completed/failed) but whose aitelier DB project status does not match.
- **Location**: A CLI command or standalone script. Recommend adding as a CLI subcommand under `cli/` (e.g., `aitelier admin sync-stuck-projects`) or a one-shot script in `scripts/sync_stuck_projects.py`.
- **Logic**:
  1. Iterate all projects in aitelier DB via `db.list_projects()`
  2. For each project, get the latest skillflow run via `sf.list_runs(project_id)` (newest first)
  3. If the run's status is terminal (`completed` or `failed`) and the project's status does NOT reflect this, call `_sync_project_status_to_db(project_id)`
  4. Log each project updated (dry-run mode recommended for first pass)
- **Safety**: Only updates projects where the latest run is terminal and the project status is mismatched — never touches actively running projects (where the latest run is `running` or `paused`).
- **Interface**:
  ```python
  # scripts/sync_stuck_projects.py or cli command
  def sync_stuck_projects(dry_run: bool = True) -> list[dict]:
      """Returns list of {project_id, old_status, new_status} for each fix."""
  ```

## Interface Specifications

### `_sync_project_status_to_db(project_id: str) -> None`

| Aspect | Detail |
|--------|--------|
| Location | `core/scheduler.py` |
| Signature | Unchanged |
| Behavior change | After `list_runs` fallback, prefer run with `graph_name == project.config_name` |
| Side effects | `db.update_project()`, `db.set_project_meta_state()`, SSE emission |
| Error handling | All exceptions caught internally; logs to `aitelier.scheduler` logger |
| Idempotency | Safe to call repeatedly — reads current state, writes derived status |

### `approve_meta(sf, run_id: str, step_runner=None) -> None`

| Aspect | Detail |
|--------|--------|
| Location | `core/meta_run.py` |
| Signature | `def approve_meta(sf, run_id: str, step_runner: Optional[Callable] = None) -> None` |
| `step_runner` | `Optional[Callable[[ClaimedStep], Awaitable[dict]]]` — executes a claimed step and returns result |
| Raises | `RuntimeError` on failure or timeout (unchanged) |
| Behavior change | When `step_runner` is provided, claims and executes tool/agent steps after each `advance_run` that returns a node |
| Backward compat | `step_runner=None` preserves existing behavior |

### Caller update in `meta_agent.py`

The call at ~line 2077 changes from:
```python
approve_meta(sf, run_id)
```
to:
```python
# Build a step runner from the agent's infrastructure
async def _run_step(claimed):
    from aitelier.runner import AgentStepRunner
    from core.event_bus import event_bus
    runner = AgentStepRunner(
        db_manager=self.db, workspace_manager=self.ws,
        agent_factory=None, prompt_assembler=None,
        event_bus=event_bus,
    )
    return await runner.execute(claimed)

approve_meta(sf, run_id, step_runner=_run_step)
```

## Technical Stack

- **Language**: Python 3.10+
- **Database**: SQLite via custom `db_manager.py` (aitelier.db) + skillflow's internal SQLite
- **Async**: asyncio (FastAPI runtime)
- **Key modules**:
  - `core/meta_agent.py` — butler agent, inline pipeline driver
  - `core/scheduler.py` — polling scheduler, `_sync_project_status_to_db`
  - `core/meta_run.py` — meta conversation helpers
  - `core/db_manager.py` — aitelier DB layer
  - `aitelier/runner.py` — `AgentStepRunner` for step execution
  - `skillflow` (external package) — pipeline engine

## Extensibility Considerations

1. **All inline configs get sync**: The fix in `_tool_start_config_run` covers ALL butler-driven configs started via this tool — not just code_review, but any config with `scheduler_owned=false`. New inline configs added to the registry automatically benefit.

2. **Config-name preference is generic**: The `_sync_project_status_to_db` fix does not hardcode "dpe_default_v2" — it reads whatever `config_name` the project was created with, so it works for any primary config.

3. **Step runner injection in `approve_meta`**: The optional `step_runner` parameter pattern is extensible — if other callers of `approve_meta` need different execution contexts, they can pass their own runner.

4. **Cleanup script reusable**: The one-time cleanup can be re-run safely (idempotent by design) and could be adapted into a periodic health-check if needed.

## File Change Summary

| File | Change Type | Lines Affected | Description |
|------|-------------|----------------|-------------|
| `core/meta_agent.py` | Add sync calls | ~10 lines added | 3 call sites: `_tool_start_config_run`, `_tool_generate_pipeline`, `_tool_wait_until_checkpoint` |
| `core/meta_agent.py` | Update call site | ~5 lines changed | Pass `step_runner` to `approve_meta` |
| `core/scheduler.py` | Modify fallback | ~8 lines changed | Prefer config_name-matching run in `_sync_project_status_to_db` |
| `core/meta_run.py` | Signature + loop | ~15 lines changed | Accept optional `step_runner`, claim+execute after advance |
| `scripts/sync_stuck_projects.py` | New file | ~60 lines | One-time cleanup script |
| `tests/unit/test_meta_run.py` | New test | ~20 lines added | Test `approve_meta` with `step_runner` |

## Rollback Plan

All changes are additive or minor modifications to existing functions:
- **meta_agent.py sync calls**: Remove the added try/except blocks to revert.
- **scheduler.py config preference**: Revert to `run = all_runs[0]` without scanning.
- **meta_run.py step runner**: The `step_runner=None` default means existing callers are unaffected; remove the new parameter to revert.
- **Cleanup script**: Delete the file; no schema changes were made.

No database schema migrations, no file deletions, no irreversible operations.
