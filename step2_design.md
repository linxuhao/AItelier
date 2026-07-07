# Technical Architecture Design

## Overview

Three independent changes to the AItelier web dashboard and backend:
1. **UnifiedDashboard: full git panel** — remove the `compact` prop from `<RepoPanel>` so each expanded repo accordion shows the complete git management interface (status grid, commit history, download-zip, all six action buttons).
2. **Backend locale fix** — force `LC_ALL=C` on all git subprocess calls in `core/workspace_manager.py` and `core/git_ops.py` so git stdout/stderr is always English, preventing French locale leakage in the dashboard's action result messages.
3. **Delete deprecated page files** — remove `web/src/views/Repository.svelte` and `web/src/views/Repositories.svelte`, which have zero remaining imports and whose routes already redirect to the dashboard.

## Architecture Diagram (textual)

```
┌──────────────────────────────────────────────────┐
│  web/src/views/UnifiedDashboard.svelte           │
│  ┌────────────────────────────────────────────┐  │
│  │  Repo accordion (per repo_path)            │  │
│  │  ┌──────────────────────────────────────┐  │  │
│  │  │  RepoPanel.svelte (compact removed)  │  │  │
│  │  │  • Full status grid (path, branch,   │  │  │
│  │  │    remote, upstream, ahead/behind)   │  │  │
│  │  │  • Commit history list               │  │  │
│  │  │  • Download-zip link                 │  │  │
│  │  │  • 6 action buttons                  │  │  │
│  │  │    (Commit, Push, Pull, Force Sync,  │  │  │
│  │  │     Make PR, Set Remote)             │  │  │
│  │  └──────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────┘  │
└────────────────────┬─────────────────────────────┘
                     │ API calls (lib/api.ts)
                     ▼
┌──────────────────────────────────────────────────┐
│  api/repo_routers.py (FastAPI endpoints)          │
│  /api/repos/{id}/status | commit | push | pull   │
│  /api/repos/{id}/sync | set-remote | make-pr     │
└────────────────────┬─────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────┐
│  core/workspace_manager.py                        │
│  ┌────────────────────────────────────────────┐  │
│  │  _GIT_ENV = {"LC_ALL": "C", **os.environ}  │  │
│  │                                           │  │
│  │  All subprocess.run(["git", ...]) calls   │  │
│  │  now pass env=_GIT_ENV                    │  │
│  └────────────────────────────────────────────┘  │
└────────────────────┬─────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────┐
│  core/git_ops.py                                  │
│  ┌────────────────────────────────────────────┐  │
│  │  _GIT_ENV = {"LC_ALL": "C", **os.environ}  │  │
│  │                                           │  │
│  │  git subprocess calls now pass            │  │
│  │  env=_GIT_ENV (defense-in-depth)          │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘

[DELETED FILES]
  web/src/views/Repository.svelte   ✕ (0 remaining imports)
  web/src/views/Repositories.svelte ✕ (0 remaining imports)
```

## Component List

### 1. UnifiedDashboard.svelte — caller-side prop change

- **File:** `web/src/views/UnifiedDashboard.svelte`
- **Change:** Line 507: remove `compact` attribute from `<RepoPanel>` tag
- **Before:**
  ```svelte
  <RepoPanel
    projectId={repo.representative_project_id}
    {canWrite}
    compact
  />
  ```
- **After:**
  ```svelte
  <RepoPanel
    projectId={repo.representative_project_id}
    {canWrite}
  />
  ```
- **Impact:** RepoPanel defaults to `compact=false`, which activates the full git panel layout. All `{#if !compact}` blocks inside RepoPanel render: status grid with all fields, commit history list, download-zip link, and the three additional action buttons (Force Sync, Make PR, Set Remote).
- **No changes needed** to RepoPanel.svelte itself — the `compact` prop definition and all conditional blocks are already in place.

### 2. RepoPanel.svelte — zero changes (verification only)

- **File:** `web/src/views/RepoPanel.svelte`
- **Status:** No structural changes. The component already fully supports both compact and full modes via its `compact` prop (default `false`). The prop definition remains intact for potential future use.
- **Key internal structure (for confirmation):**
  - Line 18: `let { ..., compact = false } = $props()` — default is `false`, matching our change
  - Lines 95-100: `<details>` with `class:repo-panel--compact={compact}` and conditional `<summary>`
  - Lines 109-133: Compact vs full status display (`{#if compact}` shows branch+dirty only; `{:else}` shows full grid with path, branch, remote, upstream, ahead/behind)
  - Lines 135-145: Commit history list — gated by `{#if !compact && ...}`
  - Line 149: Download-zip link — gated by `{#if !compact}`
  - Lines 155-159: Force Sync, Make PR, Set Remote buttons — gated by `{#if !compact}`

### 3. workspace_manager.py — locale fix via module-level `_GIT_ENV`

- **File:** `core/workspace_manager.py`
- **Change:** Add module-level constant after imports:
  ```python
  import os
  _GIT_ENV = {"LC_ALL": "C", **os.environ}
  ```
- **All git subprocess call sites** to receive `env=_GIT_ENV`:

| # | Method | Line | Command(s) | Priority | Notes |
|---|--------|------|-----------|----------|-------|
| 1 | `_run_git_checked` | 324 | Any git command | **P0** | Covers push, pull, commit, force_sync, set_remote, push_head — all write operations whose output feeds `res.detail` |
| 2 | `_git()` in `repo_status` | 258 | `rev-parse`, `status`, `remote`, `rev-list`, `log` | **P0** | Covers all dashboard status reads |
| 3 | `_get_git_hash` | 240 | `git rev-parse HEAD` | **P1** | Output appears in commit response |
| 4 | `_require_git_repo` | 335 | `git rev-parse --is-inside-work-tree` | **P1** | Error output is user-visible (HTTP 400) |
| 5 | `repo_commit` porcelain check | 362 | `git status --porcelain` | **P1** | Direct subprocess.run, not covered by helpers |
| 6 | `repo_set_remote` remote check | 347 | `git remote` | **P1** | Direct subprocess.run, not covered by helpers |
| 7 | `setup_workspace` DPS init | 108,110,111 | `git init`, `git add`, `git commit` | P2 | Init-time, output not user-visible in dashboard |
| 8 | `setup_workspace` new repo init | 132,134,135 | `git init`, `git add`, `git commit` | P2 | Init-time, output not user-visible in dashboard |
| 9 | `setup_workspace` clone | 152 | `git clone` | P2 | Init-time, output not user-visible in dashboard |
| 10 | `rollback` | 233 | `git reset --hard` | P2 | Error recovery, output not directly user-visible |

- **Implementation note:** The `_run_git_checked` and `_git` inner function are the two highest-priority sites — together they cover ~90% of user-visible git output. The remaining direct subprocess calls (items 5-10) are included for completeness and defense-in-depth. All sites use the same single-line change: add `env=_GIT_ENV` to the `subprocess.run()` call.

### 4. git_ops.py — locale fix (defense-in-depth)

- **File:** `core/git_ops.py`
- **Change:** Add `import os` (if not already present) and module-level `_GIT_ENV = {"LC_ALL": "C", **os.environ}`. Add `env=_GIT_ENV` to the two git subprocess calls:

| # | Method | Line | Command | Priority |
|---|--------|------|---------|----------|
| 1 | `create_github_pr` | 48-51 | `git remote get-url origin` | P2 |
| 2 | `create_github_pr` | 57-60 | `git rev-parse --abbrev-ref HEAD` | P2 |

- **Rationale:** Lower priority because PR creation errors come from the GitHub REST API, not git stderr. Still cheap to add for defense-in-depth.

### 5. Deleted files

| File | Path | Size | Status |
|------|------|------|--------|
| Repository.svelte | `web/src/views/Repository.svelte` | ~8 KB | Delete — 0 imports across codebase |
| Repositories.svelte | `web/src/views/Repositories.svelte` | ~4 KB | Delete — 0 imports across codebase |

- **Verification performed:** `search_repo_root` for `Repository.svelte` and `Repositories.svelte` across all files in the repo. Only `.aitelier/knowledge.md` references them as historical context — no Svelte/TS/JS import statements.
- **Routing:** `App.svelte` routes `/repos` and `/repos/:repoPath` to `RedirectToDashboard` (which pushes to `/`). Neither old page is imported in `App.svelte`.
- **No test files depend on these** — the `__tests__/views/` directory contains tests for `Project`, `ChatCodingMode`, `WorkspaceBrowser`, and `StepTimeline` only.

## Data Flow

### Status read flow (dashboard poll)

```
UnifiedDashboard.svelte (10s poll)
  → api.listRepos() → GET /api/repos → DB query (repo metadata)
  ├── Accordion expands → RepoPanel mounts → onMount(load)
  │     → api.repoStatus(projectId) → GET /api/repos/{id}/status
  │           → workspace_manager.repo_status()
  │                 → _git() helper (now with LC_ALL=C) → subprocess.run
  │                 → Returns {is_git, branch, dirty, remote_url, upstream, ahead, behind, commits}
  │     → Renders status grid + commit list + action buttons
```

### Git write flow (user clicks action button)

```
RepoPanel.svelte → doCommit() / run('Push', ...) / doSync() / etc.
  → api.repoCommit(projectId, msg) → POST /api/repos/{id}/commit
  → api.repoPush(projectId)        → POST /api/repos/{id}/push
  → api.repoPull(projectId)        → POST /api/repos/{id}/pull
  → api.repoSync(projectId, ...)   → POST /api/repos/{id}/sync
  → api.repoSetRemote(projectId, url) → POST /api/repos/{id}/set-remote
  → api.repoMakePR(projectId, ...)    → POST /api/repos/{id}/make-pr
        → workspace_manager.repo_*() methods
              → _run_git_checked() (now with LC_ALL=C) → subprocess.run
              → Returns {detail: stdout} or {message: "..."}
        → Frontend run() displays res.message || res.detail in actionMsg
```

### Key: LC_ALL=C ensures `res.detail` (git stdout) is always English
Before: `git pull` on a French server → `res.detail = "Déjà à jour."`
After:  `git pull` with LC_ALL=C → `res.detail = "Already up to date."`

## Technical Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Frontend | Svelte 5 (runes mode) + TypeScript | `$props()`, `$state`, `$derived`, `$effect` |
| Styling | Pico CSS | Already included in project |
| Routing | svelte-spa-router | `App.svelte` route table |
| Backend | Python 3.12+ / FastAPI | `api/repo_routers.py` endpoints |
| Git operations | `subprocess.run` (stdlib) | No external git libraries |
| Locale fix | `env={"LC_ALL": "C", **os.environ}` | Stdlib-only, no new dependencies |

## Extensibility Considerations

- **`compact` prop preserved:** RepoPanel.svelte retains the `compact` prop (default `false`). Future views or embedded contexts can still use `<RepoPanel compact />` without any changes to the component.
- **Module-level `_GIT_ENV` constant:** Using a shared constant (rather than inlining the dict at each call site) makes it trivial to add future git subprocess calls with the correct locale — just pass `env=_GIT_ENV`.
- **No new abstractions:** The design avoids creating a `_git_subprocess()` wrapper or other helper functions — the module-level constant approach is the simplest change with the least risk of introducing bugs. Future refactoring to a centralized wrapper is still possible since all call sites use the same `_GIT_ENV` constant name.
- **File deletion is safe:** The old pages had zero remaining imports. If a rollback is ever needed, the files can be recovered from git history.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `LC_ALL=C` breaks a non-git subprocess | Very Low | Low | `env=` is scoped to individual `subprocess.run` calls — only git commands are affected |
| Removing `compact` causes layout overflow | Low | Medium | RepoPanel's full-mode CSS already exists and was ported from the old Repository.svelte layout. Tested in prior runs. |
| `_GIT_ENV` computed at module import time captures wrong env | Very Low | Low | `os.environ` at import time is the same environment the process uses. Docker containers have stable env. |
| Deleted files needed by an undiscovered reference | Very Low | High | Zero imports confirmed via grep of entire `web/src/` directory. Files recoverable via git. |
| `env=` merge pattern breaks on some Python versions | Very Low | Low | `{**os.environ}` dict unpacking is supported since Python 3.5. Project uses Python 3.12+. |

## Implementation Order (for PM task decomposition)

1. **Task 1:** Backend — Add `_GIT_ENV` constant and apply `env=_GIT_ENV` to all git subprocess calls in `core/workspace_manager.py`
2. **Task 2:** Backend — Add `_GIT_ENV` constant and apply `env=_GIT_ENV` to git subprocess calls in `core/git_ops.py`
3. **Task 3:** Frontend — Remove `compact` prop from `<RepoPanel>` in `web/src/views/UnifiedDashboard.svelte`
4. **Task 4:** Frontend — Delete `web/src/views/Repository.svelte` and `web/src/views/Repositories.svelte`
5. **Task 5:** Verification — Build check, lint, confirm no import errors, spot-check git operation messages are English

Tasks 1-2 are independent of tasks 3-4. Tasks 3 and 4 are independent. All can be parallelized.
