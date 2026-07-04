# Technical Architecture Design — Repository View for AItelier Dashboard

## Overview

Add a top-level "Repositories" view to the AItelier dashboard, grouping projects by `repo_path`. This is achieved through a **lightweight backend grouping endpoint** (`GET /api/repos`) with no DB schema changes, new Svelte 5 views for repository listing and detail, and relocation of `RepoPanel` + `WorkspaceBrowser root="code"` from `Project.svelte` to the new Repository page.

---

## Architecture Diagram (Text)

```
Browser (SPA — svelte-spa-router)
│
├─ /                     → Dashboard.svelte         (flat project table, kept as-is)
├─ /projects/:id         → Project.svelte            (repo panel REMOVED; dps workspace kept)
├─ /repos                → Repositories.svelte       (NEW — repo list from GET /api/repos)
├─ /repos/:repoPath      → Repository.svelte         (NEW — repo detail + RepoPanel + WorkspaceBrowser)
├─ /chat                 → Chat.svelte               (unchanged)
├─ /tracking             → Tracking.svelte           (unchanged)
├─ /projects/:id/trace   → Trace.svelte              (unchanged)
│
▼  HTTP (Fetch API via api.ts)
│
Backend (FastAPI)
│
├─ GET  /api/repos                    → list all repos (grouped by repo_path)
├─ GET  /api/repos/{repo_path}        → single repo detail + projects
├─ GET  /api/projects/:id/repo/status → (existing, used by RepoPanel on Repository page)
├─ POST /api/projects/:id/repo/*      → (existing, all write ops unchanged)
├─ GET  /api/projects/:id/workspace/* → (existing, root="code" used on Repository page)
│
▼
SQLite (runs table — no schema changes)
  Columns: project_id, name, repo_type, repo_path, repo_url, created_at, updated_at
```

---

## Component List

### 1. Backend: `api/repo_routers.py` (NEW FILE)

**Responsibility**: Serve repository grouping data via two read-only API endpoints. Queries `runs` table, groups by `repo_path`, computes metadata per group.

**Routes**:

| Method | Path | Returns |
|--------|------|---------|
| `GET` | `/api/repos` | `[{ repo_path, repo_name, repo_type, repo_url, project_count, representative_project_id, last_activity, projects: [{project_id, name, status, updated_at}] }]` |
| `GET` | `/api/repos/{repo_path}` | Single repo object (same shape) filtered to one `repo_path` |

**Implementation details**:
- `repo_path` route param: FastAPI path param. svelte-spa-router encodes slashes; FastAPI decodes them. The router `prefix` is `/api/repos` so `GET /api/repos/{repo_path}` naturally matches.
- Use `core.db_manager.DBManager` via FastAPI DI (`get_db_manager`).
- `SELECT repo_path, repo_type, repo_url FROM runs WHERE repo_path IS NOT NULL AND repo_path != '' GROUP BY repo_path`
- For each group: `SELECT project_id, name, status, updated_at FROM runs WHERE repo_path = ? ORDER BY updated_at DESC`
  - `representative_project_id` = first row's `project_id` (most recently updated)
  - `project_count` = row count
  - `last_activity` = max `updated_at`
  - `repo_name` = `os.path.basename(repo_path)` if non-empty, else `repo_path`
  - `projects` = full list in that group
- No write gating needed — all data is from existing `runs` table columns already exposed by `/api/projects`.
- **Decouple from list + detail**: use a shared `_build_repo_groups()` helper so both endpoints reuse the same grouping/aggregation logic.

**Dependencies**: `APIRouter`, `Depends`, `HTTPException`, `DBManager`

**Lines**: ~80 Python

### 2. Backend: `api/main.py` (MODIFY)

**Change**: Register the new router:
```python
from api.repo_routers import router as repo_router
# ...
app.include_router(repo_router)
```

Insert after `app.include_router(admin_router)` (line 226) keeping alphabetical-ish order.

### 3. Frontend: `web/src/lib/api.ts` (MODIFY)

**New functions**:
```typescript
// GET /api/repos
export function listRepos(): Promise<RepoItem[]> { ... }

// GET /api/repos/{repo_path}
export function getRepo(repoPath: string): Promise<RepoDetail> { ... }
```

**Types to export**:
```typescript
export interface RepoItem {
  repo_path: string;
  repo_name: string;
  repo_type: string;
  repo_url: string | null;
  project_count: number;
  representative_project_id: string;
  last_activity: string;
  projects: RepoProjectSummary[];
}

export interface RepoProjectSummary {
  project_id: string;
  name: string;
  status: string;
  updated_at: string;
}

export type RepoDetail = RepoItem;  // same shape for /api/repos/{repo_path}
```

`listRepos()` uses the `_get` helper; `getRepo(repoPath)` uses `_get('/api/repos/' + encodeURIComponent(repoPath))`.

### 4. Frontend: `web/src/views/Repositories.svelte` (NEW FILE)

**Responsibility**: List all repositories grouped by `repo_path`. This is the `/repos` route.

**State**:
- `repos: RepoItem[]` — fetched from `GET /api/repos` on mount
- `loading`, `error`, `pollTimer`

**Rendering**:
- Card/table layout showing: repo name (`repo_name`), full path, project count badge, repo type badge ("new"/"existing"/"clone"), last activity timestamp.
- Click navigates to `#/repos/${encodeURIComponent(repo.repo_path)}` via `push()`.
- Handle empty state: "No repositories found" with link back to Dashboard.
- Handle error state: inline retry button.
- 10-second poll interval (same pattern as Dashboard).

**Props**: None (standalone route component).

### 5. Frontend: `web/src/views/Repository.svelte` (NEW FILE)

**Responsibility**: Show a single repository's detail — its projects, RepoPanel, and WorkspaceBrowser root="code". This is the `/repos/:repoPath` route.

**Props** (from svelte-spa-router):
```typescript
let { params = {} as Record<string, string> } = $props();
```
Extracts `repoPath` via `decodeURIComponent(params.repoPath)`.

**State**:
- `repo: RepoDetail | null` — from `GET /api/repos/{repoPath}` on mount + on `repoPath` change
- `loading`, `error`

**Derived**:
- `representativeProjectId = repo?.representative_project_id as string`
- `canWrite = $derived($authStore.permissionResolved && $authStore.canWrite)`

**Rendering**:
1. **Breadcrumb**: Dashboard → Repositories → Repo Name
2. **Repo metadata header**: repo name, full path, repo type, remote URL (if any), project count
3. **Project table**: list of `repo.projects`, each clickable → `#/projects/${p.project_id}`
4. **`<RepoPanel projectId={representativeProjectId} {canWrite} />`** — renders git status, commits, write actions using the representative project as the proxy
5. **`<WorkspaceBrowser projectId={representativeProjectId} root="code" title="Repository Code" />`** — renders the code file tree
6. Error states: 404 → "Repository not found"; generic error → retry button.

**Edge case handling**:
- If `repo.project_count === 1`, the project table shows a single row (no special casing).
- If `representativeProjectId` is missing/null (shouldn't happen with backend filtering), show an error.

### 6. Frontend: `web/src/views/Project.svelte` (MODIFY)

**Removals**:
- Line 788: `<!-- <RepoPanel projectId={projectId} {canWrite} /> -->` (REMOVE entirely)
- Line 789: `<!-- <WorkspaceBrowser projectId={projectId} root="code" title="Project Repository" /> -->` (REMOVE entirely)

**Keep**:
- Line 787: `<WorkspaceBrowser projectId={projectId} root="dps" title="Pipeline Artifacts" />` (KEEP)

**Add** (breadcrumb enhancement):
- In the breadcrumb area, if `project.repo_path` is set, add a "Repositories" link between "Dashboard" and the project name, linking to `#/repos/${encodeURIComponent(project.repo_path as string)}`.

**Also remove**: The `import RepoPanel` line (line 9) if it's no longer used. `WorkspaceBrowser` import stays (still used for root="dps").

### 7. Frontend: `web/src/App.svelte` (MODIFY)

**New imports**:
```typescript
import Repositories from './views/Repositories.svelte';
import Repository from './views/Repository.svelte';
```

**New routes** (add to existing `routes` object):
```typescript
'/repos': Repositories,
'/repos/:repoPath': Repository,
```

**Decision: Keep `/` as Dashboard (unchanged)**. The Dashboard still shows the flat project table. Users navigate to Repositories via the AppBar. This is the lowest-risk approach — existing users are not disrupted. The Dashboard can later be changed to show repos by default, but that is out of scope for this MVP.

### 8. Frontend: `web/src/views/AppBar.svelte` (MODIFY)

**Add nav link** (after Dashboard, before Chat):
```html
<li><a href="#/repos">{t('appbar.repos')}</a></li>
```

### 9. Frontend: `web/src/lib/i18n.svelte.ts` (MODIFY)

**New translation keys** (add to each language block — en, zh-CN, zh-TW, ja, ko, fr, de, es):

| Key | English | zh-CN | Notes |
|-----|---------|-------|-------|
| `appbar.repos` | Repositories | 仓库 | AppBar nav link |
| `repos.title` | Repositories | 仓库 | Page title |
| `repos.count` | `{n} projects` | `{n} 个项目` | Project count per repo |
| `repos.noRepos` | No repositories found. | 未找到仓库。 | Empty state |
| `repos.backToDashboard` | Back to Dashboard | 返回仪表盘 | Link when empty |
| `repos.loading` | Loading repositories… | 加载仓库中… | Loading state |
| `repos.failedToLoad` | Failed to load repositories. | 加载仓库失败。 | Error state |
| `repos.retry` | Retry | 重试 | Retry button |
| `repo.title` | Repository | 仓库 | Single repo page title |
| `repo.projectsInRepo` | Projects in this repository | 此仓库中的项目 | Project list heading |
| `repo.notFound` | Repository not found. | 未找到仓库。 | 404 state |
| `repo.backToRepos` | Back to Repositories | 返回仓库列表 | Breadcrumb link |
| `repo.repoPath` | Path | 路径 | Metadata label |
| `repo.repoType` | Type | 类型 | Metadata label |
| `repo.repoUrl` | Remote URL | 远程地址 | Metadata label |
| `repo.projectCount` | Projects | 项目数 | Metadata label |
| `repo.loading` | Loading repository… | 加载仓库中… | Loading state |
| `repo.failedToLoad` | Failed to load repository. | 加载仓库失败。 | Error state |

**Note**: Existing `repo.title`, `repo.path`, `repo.branch` etc. are already used by `RepoPanel.svelte` and should NOT be renamed.

### 10. Frontend: `web/src/stores/project.ts` (MODIFY — optional enhancement)

**Add to ProjectState**:
```typescript
currentRepoPath: string | null;
```

This tracks which repository the user navigated from, enabling the "Back to Repository" link in Project.svelte. The `Repository.svelte` view sets this before navigating to a project. This is a lightweight enhancement — if omitted, we can pass repo path via query param (`#/projects/:id?repo=...`) but the store is cleaner.

**If store approach**: Add `setCurrentRepoPath(path: string | null)` alongside `setCurrentProject`.

---

## Data Flow

### Repositories List Flow
```
Repositories.svelte (onMount)
  → listRepos()                    [api.ts]
    → GET /api/repos               [repo_routers.py]
      → DBManager                  [db_manager.py: runs table GROUP BY repo_path]
    ← [{ repo_path, ..., projects }]
  → render repo cards
  → 10s poll repeats same flow
```

### Repository Detail Flow
```
Repository.svelte (onMount / $effect on params.repoPath)
  → getRepo(decodedRepoPath)       [api.ts]
    → GET /api/repos/{repo_path}   [repo_routers.py]
      → DBManager (WHERE repo_path = ?)
    ← { repo_path, ..., projects, representative_project_id }
  → render:
      - repo metadata header
      - project table
      - <RepoPanel projectId={representative_project_id} />
      - <WorkspaceBrowser projectId={representative_project_id} root="code" />
```

### RepoPanel/WorkspaceBrowser on Repository Page Flow
```
RepoPanel (onMount)
  → repoStatus(projectId)               [api.ts]
    → GET /api/projects/:id/repo/status [project_routers.py — existing]
      → ws.repo_status(projectId)       [workspace_manager.py — existing]
    ← git status data

WorkspaceBrowser (on first expand)
  → workspaceTree(projectId, root="code")  [api.ts]
    → GET /api/projects/:id/workspace/tree [project_routers.py — existing]
      → ws.get_code_path(projectId)        [workspace_manager.py — existing]
    ← file tree
```

**Key insight**: `WorkspaceManager.get_code_path(projectId)` looks up `repo_path` from the `runs` table per project. All projects sharing the same `repo_path` return the same directory, so using the representative `projectId` is functionally identical to using any other project in the group.

### Navigation Flow
```
AppBar "Repositories" link
  → push('#/repos')
    → Repositories.svelte renders

Repo card click
  → push('#/repos/' + encodeURIComponent(repo.repo_path))
    → Repository.svelte renders

Project row click (in Repository page)
  → push('#/projects/' + project.project_id)
    → Project.svelte renders (with breadcrumb: Dashboard → Repositories → Repo → Project)
```

---

## Success Criteria Mapping

| Criterion | How Addressed |
|-----------|---------------|
| Dashboard shows repos grouped by `repo_path` | `Repositories.svelte` at `/repos` + AppBar link as primary entry point. Flat project table remains at `/` for backward compatibility. |
| Clicking repo → Repository page with filtered projects | `Repository.svelte` fetches `GET /api/repos/{repo_path}`, renders project table from `repo.projects`. |
| Repository page includes RepoPanel + WorkspaceBrowser root="code" | Both rendered using `representative_project_id`. |
| Project page no longer has RepoPanel or WorkspaceBrowser root="code" | Removed from `Project.svelte` (only root="dps" WorkspaceBrowser kept). |
| Existing routes unchanged | `/`, `/projects/:id`, `/chat`, `/tracking`, trace routes all untouched. |
| Breadcrumbs intuitive | Dashboard → Repositories → Repo Name → Project Name via breadcrumb in each view. |

---

## Technical Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Frontend | Svelte 5 (runes mode) + TypeScript | `$state`, `$derived`, `$props`, `$effect` |
| Routing | svelte-spa-router | Client-side hash-based routes |
| API | Fetch wrapper in `api.ts` | `_get()` / `_post()` with timeout + error handling |
| Backend | FastAPI + APIRouter | New `api/repo_routers.py` |
| Database | SQLite via `core/db_manager.py` | No schema changes; queries existing `runs` table |
| i18n | `i18n.svelte.ts` reactive module | 8-language support |
| Auth | `authStore` + `authz.py` write gate | Read-only for repo listing; write ops still project-scoped |

---

## Extension Points (Future)

1. **`repositories` table**: If repo-scoped settings/permissions are needed, a migration can add a proper `repositories` table with `repo_path` as the natural key, foreign key from `runs`. The `GET /api/repos` endpoint would then query the new table — frontend unchanged.
2. **Repo-scoped git endpoints**: `POST /api/repos/{repo_path}/commit` etc. would internally resolve to the representative project and call the same git logic. The existing project-scoped endpoints continue to work.
3. **Dashboard default view**: The `/` route could be changed to `Repositories` instead of `Dashboard`, with the flat project table accessible via a tab. The current design keeps `/` as-is for zero disruption.
4. **Pagination**: If the number of repos grows large, the `GET /api/repos` endpoint can accept `limit`/`offset` params.
5. **Search/filter**: Add query params to filter repos by name, type, or path.

---

## Constraints & Risk Mitigation

| Constraint | Mitigation |
|-----------|------------|
| No DB schema changes | `GET /api/repos` queries `runs` columns only; no migration, no new table. |
| WorkspaceManager.get_code_path() returns same dir for all projects in a group | Verified in SOTA. Representative project ID is deterministic. |
| repo_path as URL param (contains slashes) | `encodeURIComponent` on frontend; FastAPI decodes naturally. |
| Write permissions | `canWrite` already derived from `authStore`; RepoPanel uses it unchanged. User needs write access to the representative project — if they lack it, git write buttons are hidden. |
| Projects without repo_path | Filtered by `WHERE repo_path IS NOT NULL AND repo_path != ''` in the backend query. |

---

## File Inventory (Complete Change List)

| File | Action | Description |
|------|--------|-------------|
| `api/repo_routers.py` | **CREATE** | New router: `GET /api/repos`, `GET /api/repos/{repo_path}` |
| `api/main.py` | **EDIT** | Import + `app.include_router(repo_router)` |
| `web/src/views/Repositories.svelte` | **CREATE** | Repository list view |
| `web/src/views/Repository.svelte` | **CREATE** | Single repository detail view |
| `web/src/views/Project.svelte` | **EDIT** | Remove RepoPanel + WorkspaceBrowser(root="code"); add breadcrumb |
| `web/src/views/AppBar.svelte` | **EDIT** | Add "Repositories" nav link |
| `web/src/App.svelte` | **EDIT** | Add `/repos` and `/repos/:repoPath` routes |
| `web/src/lib/api.ts` | **EDIT** | Add `listRepos()`, `getRepo()`, `RepoItem`/`RepoDetail` types |
| `web/src/lib/i18n.svelte.ts` | **EDIT** | Add ~16 new translation keys × 8 languages |
| `web/src/stores/project.ts` | **EDIT** | Optional: add `currentRepoPath` to `ProjectState` + setter |
