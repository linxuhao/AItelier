# Technical Architecture Design: Unified Dashboard — Merge Repositories & Projects

## Overview

Merge the separate **Dashboard** (`/projects`) and **Repositories** (`/repos`, `/repos/:repoPath`) views in the AItelier Svelte 5 web frontend into a single unified page at `/` (and `/projects` for backward compatibility). The unified page groups projects by their parent repository using collapsible `<details>`/`<summary>` sections, includes a front-end search bar, auto-expands the most-recently-active repo, and shows a compact git-status thumbnail when a repo section is expanded.

All changes are **frontend-only** — the backend `GET /api/repos` endpoint already provides grouped repo+project data and requires no modification.

---

## Architecture Diagram

```
                      ┌──────────────────────────────┐
                      │       App.svelte              │
                      │   svelte-spa-router routes:    │
                      │   '/' → UnifiedDashboard      │
                      │   '/projects' → UnifiedDashboard
                      │   '/projects/:id' → Project   │
                      │   '/repos' → RedirectToDashboard
                      │   '/repos/:repoPath' → RedirectToDashboard
                      └──────────┬───────────────────┘
                                 │
                    ┌────────────▼──────────────────┐
                    │    UnifiedDashboard.svelte     │
                    │  ┌─────────────────────────┐  │
                    │  │  Search Bar (front-end)  │  │
                    │  └─────────────────────────┘  │
                    │  ┌─────────────────────────┐  │
                    │  │  Create Project Form     │  │
                    │  └─────────────────────────┘  │
                    │  ┌─────────────────────────┐  │
                    │  │  Repo Accordion List     │  │
                    │  │  <details> per repo      │  │
                    │  │  ├─ <summary> header     │  │
                    │  │  ├─ Project table        │  │
                    │  │  └─ Compact RepoPanel    │  │
                    │  │     (lazy, on expand)    │  │
                    │  └─────────────────────────┘  │
                    │  ┌─────────────────────────┐  │
                    │  │  "No Repository" section │  │
                    │  │  (orphan projects)       │  │
                    │  └─────────────────────────┘  │
                    │  ┌─────────────────────────┐  │
                    │  │  Delete Confirmation     │  │
                    │  └─────────────────────────┘  │
                    └────────────────────────────────┘

    API (unchanged):
    GET /api/repos                → RepoItem[]   (polled every 10s)
    GET /api/repos/{repo_path}    → RepoDetail   (not used on unified page)
    GET /api/runs                 → { runs: [] } (used for orphan projects only)
    POST /api/projects            → create project
    DELETE /api/projects/{id}     → delete project
    GET /api/projects/{id}/repo/status  → git status (called by RepoPanel)
    POST /api/projects/{id}/repo/commit → git commit
    POST /api/projects/{id}/repo/push   → git push
    POST /api/projects/{id}/repo/pull   → git pull
```

### Data Flow

```
listRepos() every 10s
        │
        ▼
$state repos: RepoItem[]
        │
        ├──▶ $derived filteredRepos (by searchQuery)
        │         │
        │         ▼
        │    render repo sections
        │
        └──▶ on first load: find max(last_activity)
                  → $state expandedRepos Set = { that repo_path }
                         │
                         ▼
                  <details bind:open={expandedRepos.has(r.repo_path)}>
```

---

## Component List

### 1. UnifiedDashboard.svelte (NEW — replaces Dashboard.svelte)

- **File**: `web/src/views/UnifiedDashboard.svelte`
- **Responsibility**: Single-page view that combines the old Dashboard (project list, create/delete) and Repositories (repo grouping, git ops thumbnail) into one view.
- **State**:
  - `repos: RepoItem[]` — polled from `listRepos()` every 10s
  - `orphanProjects: RunItem[]` — projects without a repo_path, fetched once from `listAllRuns()` (supplementary)
  - `searchQuery: string` — bound to search `<input>`
  - `expandedRepos: Set<string>` — tracks which repo sections are open
  - `loading, error, isRefreshing` — standard async states
  - `createFormVisible, newProjectId, newProjectName, seedText, repoType, repoPath, repoUrl, submitting, formErrors` — ported from Dashboard.svelte
  - `pendingDeleteId` — delete confirmation
  - `repoStatusCache: Map<string, RepoItem>` — reused from polling; RepoPanel thumbnail reads from this
- **Derived**:
  - `filteredRepos: RepoItem[]` — `$derived(repos.filter(r => matchesSearch(r, searchQuery)))`
  - `canWrite` — from `$authStore`
  - `connected` — from `$connectionStore`
  - `empty` — `$derived(!loading && !error && repos.length === 0)`
- **Lifecycle**:
  - `onMount`: call `refreshRepos()`, start 10s polling interval; fetch orphan projects once
  - `onDestroy`: clear interval
- **Key behaviors**:
  - Auto-expand: on first data load, find the repo whose `last_activity` is the maximum; add its `repo_path` to `expandedRepos`
  - Search: filter repos where `repo_name` or any `project.name` matches the query (case-insensitive substring). During active search, auto-expand all repos that have matching projects so results are visible immediately. When search is cleared, restore the previous collapsed/expanded state.
  - Delete: same behavior as old Dashboard.svelte — confirm dialog, then call `deleteProject()`
  - Create form: identical to old Dashboard.svelte — toggles visibility, validates, calls `createProject()`
- **Interfaces**:
  - Props: none (route component)
  - Uses: `listRepos`, `listAllRuns`, `createProject`, `deleteProject` from `api.ts`
  - Uses: `formatTime`, `parseStatus`, `formatTokens`, `formatTaskProgress`, `cacheBadgeClass`, `repoTypeLabel` from `format.ts`
  - Uses: `t()` from `i18n.svelte.ts`
  - Uses: `authStore`, `connectionStore`, `projectStore` from stores
  - Uses: `push` from `svelte-spa-router` for navigation
  - Renders: `<RepoPanel compact={true} projectId={...} canWrite={...} />` inside expanded sections

### 2. RedirectToDashboard.svelte (NEW)

- **File**: `web/src/views/RedirectToDashboard.svelte`
- **Responsibility**: Simple wrapper that calls `push('#/')` on mount for `/repos` and `/repos/:repoPath` backward compatibility.
- **Code**: 3-line component — `import { push, onMount }`, call `push('#/')` in `onMount`.
- **Reason**: `svelte-spa-router` v5 has no redirect directive; a small component is the cleanest approach per SOTA recommendation.

### 3. RepoPanel.svelte (MODIFY — add `compact` prop)

- **File**: `web/src/views/RepoPanel.svelte`
- **Responsibility**: Unchanged in full mode. Adds a `compact` boolean prop that renders a minimized variant for the unified dashboard.
- **New prop**: `compact?: boolean` (default `false`)
- **Compact mode rendering**:
  - **Show**: branch name, dirty indicator (● N uncommitted), 3 action buttons: Commit, Push, Pull
  - **Hide**: commit history list, remote URL, upstream details (ahead/behind), Force sync, Make PR, Set remote, Download ZIP
  - **Layout**: reduced padding, single-line status row with inline buttons
  - The `load()` function runs identically — the same `repoStatus()` API call is made
- **Rationale**: Per SOTA recommendation, avoids duplicating git-ops API logic. The `compact` prop is a simple conditional in the template that hides/show sections.
- **CSS**: Compact mode uses a `repo-panel--compact` CSS class with reduced padding and `font-size: 0.78rem`.

### 4. App.svelte (MODIFY — update routes)

- **File**: `web/src/App.svelte`
- **Changes**:
  1. Replace `import Dashboard from './views/Dashboard.svelte'` with `import UnifiedDashboard from './views/UnifiedDashboard.svelte'`
  2. Add `import RedirectToDashboard from './views/RedirectToDashboard.svelte'`
  3. Remove `import Repositories from './views/Repositories.svelte'` and `import Repository from './views/Repository.svelte'`
  4. Update routes:
     ```
     '/': UnifiedDashboard,
     '/projects': UnifiedDashboard,
     '/repos': RedirectToDashboard,
     '/repos/:repoPath': RedirectToDashboard,
     ```
     Keep `/projects/:id`, `/projects/:id/trace`, `/projects/:id/trace/:runId`, `/chat`, `/tracking` unchanged.

### 5. i18n.svelte.ts (MODIFY — add new keys)

- **File**: `web/src/lib/i18n.svelte.ts`
- **New keys to add** (in all 8 language dictionaries):
  - `dashboard.searchPlaceholder` — "Search repos and projects…"
  - `dashboard.noRepos` — "No repositories found."
  - `dashboard.noSearchResults` — "No repos or projects match your search."
  - `dashboard.noRepoProjects` — "No projects in this repository."
  - `dashboard.expandAll` — "Expand all"
  - `dashboard.collapseAll` — "Collapse all"
  - `dashboard.repoGroup` — "Repository"
  - `dashboard.projectCount` — "{n} project(s)"
  - `dashboard.orphanProjects` — "Projects without a repository"

### 6. Dashboard.svelte (KEEP — unused, safe to remove later)

- **File**: `web/src/views/Dashboard.svelte`
- **Decision**: Keep the file but remove its route mapping. It is no longer reachable via the router. If any code imports it, those imports are updated to point to `UnifiedDashboard` instead. The file can be physically deleted in a follow-up cleanup task.

### 7. Repositories.svelte and Repository.svelte (KEEP — unused)

- **Files**: `web/src/views/Repositories.svelte`, `web/src/views/Repository.svelte`
- **Decision**: Same as Dashboard.svelte — keep files, remove route mappings, no longer reachable. Can be deleted later.

---

## Interface Contracts

### RepoPanel.svelte — Updated Props

```typescript
let {
  projectId,
  canWrite = false,
  compact = false,   // NEW
}: {
  projectId: string;
  canWrite?: boolean;
  compact?: boolean;  // NEW
} = $props();
```

### listRepos() → RepoItem[]

Unchanged. Returns the existing `RepoItem[]` shape from `GET /api/repos`:

```typescript
interface RepoItem {
  repo_path: string;
  repo_name: string;
  repo_type: string;
  repo_url: string | null;
  project_count: number;
  representative_project_id: string;
  last_activity: string;       // ISO timestamp
  projects: RepoProjectSummary[];
}

interface RepoProjectSummary {
  project_id: string;
  name: string;
  status: string;
  updated_at: string;           // ISO timestamp
  cache_stats?: Record<string, unknown>;
  config_name?: string;
}
```

### Search matching contract

```typescript
function matchesSearch(repo: RepoItem, query: string): boolean {
  const q = query.toLowerCase().trim();
  if (!q) return true;
  return (
    repo.repo_name.toLowerCase().includes(q) ||
    repo.repo_path.toLowerCase().includes(q) ||
    repo.projects.some(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.project_id.toLowerCase().includes(q)
    )
  );
}
```

### Expanded state contract

```typescript
// Persistent expanded set keyed by repo_path
let expandedRepos = $state<Set<string>>(new Set());

// Auto-expand on first data load:
function autoExpandMostRecent(repos: RepoItem[]): void {
  if (repos.length === 0) return;
  let mostRecent = repos[0];
  for (const r of repos) {
    if (r.last_activity > mostRecent.last_activity) mostRecent = r;
  }
  expandedRepos = new Set([mostRecent.repo_path]);
}

// During search: expand all repos with visible matches
let searchExpanded = $state<Set<string>>(new Set());
// When searchQuery changes:
//   if query non-empty: expand all filtered repos
//   if query empty: restore previous expandedRepos
```

---

## Technical Stack

| Concern | Choice | Rationale |
|---|---|---|
| Collapse/expand | HTML `<details>`/`<summary>` with Svelte `bind:open` | Zero-dependency, accessible, Pico CSS styled. Recommended by SOTA. |
| Reactivity | Svelte 5 `$state` + `$derived` runes | Already the project's model. `$derived` for filtered repos. |
| Search/filter | `Array.filter()` + `String.includes()` | Front-end only, case-insensitive substring. Performs well for <1000 items. Recommended by SOTA. |
| Polling | `setInterval` every 10s via `listRepos()` | Existing pattern in both Dashboard.svelte and Repositories.svelte. Keep as-is. |
| Routing | `svelte-spa-router` v5 | Already in use. Redirect via wrapper component. |
| CSS framework | Pico CSS v2 | Already in use. Styles `<details>`/`<summary>` natively. |
| Git ops | `RepoPanel.svelte` with `compact` prop | Reuses existing logic. Avoids duplication per SOTA recommendation. |
| i18n | Existing `t()` function + 8-language dictionaries | Extend with new `dashboard.*` keys. |

---

## Search/Filter Behavior — Detailed Specification

The search bar is a front-end-only filter with the following UX contract:

1. **Initial state**: Search input is empty, all repos visible, only the most-recently-active repo expanded.
2. **User types a query**: `filteredRepos` is recomputed via `$derived`. Repos and projects that don't match are hidden from the DOM. Repos that DO match (either by repo name or because at least one project matches) remain visible.
3. **During active search**: All visible (matching) repo sections are auto-expanded so the user can immediately see matching projects. The previously-saved `expandedRepos` set is preserved in a backup variable.
4. **User clears the search**: The filter is removed (all repos visible), and the previously-saved `expandedRepos` set is restored.

Implementation pattern:

```typescript
let searchQuery = $state('');
let expandedRepos = $state<Set<string>>(new Set());
let savedExpanded = $state<Set<string>>(new Set()); // backup during search

let filteredRepos = $derived(
  searchQuery.trim()
    ? repos.filter(r => matchesSearch(r, searchQuery))
    : repos
);

// $effect to manage auto-expand during search
$effect(() => {
  if (searchQuery.trim()) {
    // Entering search mode
    savedExpanded = expandedRepos;
    expandedRepos = new Set(filteredRepos.map(r => r.repo_path));
  } else {
    // Exiting search mode
    expandedRepos = savedExpanded;
  }
});
```

---

## Expand/Collapse Behavior — Detailed Specification

Each repo section uses `<details bind:open={expandedRepos.has(repo.repo_path)}>`.

1. **Page load**: `autoExpandMostRecent()` runs once after the first successful `listRepos()` call. It finds the repo with the max `last_activity` timestamp and sets `expandedRepos = new Set([thatRepoPath])`.
2. **User clicks a repo summary**: The browser toggles the `<details>` `open` attribute. A `$effect` or `ontoggle` handler syncs the `expandedRepos` Set.
3. **Compact RepoPanel loads on expand**: The `RepoPanel` component inside each `<details>` section calls `repoStatus()` on mount (its existing `onMount(load)` behavior). Since `<details>` contents are in the DOM even when collapsed, we must **conditionally render** the `RepoPanel` only when the section is open to avoid N unnecessary API calls on page load. Use `{#if expandedRepos.has(repo.repo_path)}<RepoPanel ... />{/if}`.
4. **"Expand all" / "Collapse all" buttons** (optional UX enhancement): If added, they set `expandedRepos` to all repo paths or to an empty set respectively.

---

## Compact RepoPanel Rendering — Detailed Specification

When `compact={true}`:

```
┌─────────────────────────────────────────────┐
│ Repository                                  │
│ main ● 3 uncommitted    [Commit] [Push] [Pull] │
└─────────────────────────────────────────────┘
```

- Single-line layout: branch name + dirty indicator + 3 inline buttons
- Uses existing `<details>` wrapper from RepoPanel (its own `<details class="workspace-section repo-panel">`)
- In compact mode, that outer `<details>` is always `open` (since the parent repo section is already the collapsible container)
- CSS class `repo-panel--compact` applied to the root element when `compact` is true
- The `load()` function and all state management (`status`, `error`, `actionMsg`, `busy`) remain identical to full mode

---

## Orphan Projects Handling

Projects with `repo_path IS NULL OR repo_path = ''` are excluded by the backend `GET /api/repos` query. To ensure these projects are still visible to the user, the unified dashboard fetches `GET /api/runs` once on mount and filters for projects without a `repo_path`.

These "orphan" projects appear in a dedicated section at the bottom of the dashboard:

```
## Projects without a repository
[table of orphan projects, same columns as repo project tables]
```

This section is also filterable by the search bar (orphan projects match by name/id).

---

## Route Migration — Backward Compatibility

| Old Route | New Route | Mechanism |
|---|---|---|
| `/` | `/` | Direct — UnifiedDashboard |
| `/projects` | `/projects` | Direct — UnifiedDashboard (alias) |
| `/repos` | `/` | RedirectToDashboard component calls `push('#/')` |
| `/repos/:repoPath` | `/` | RedirectToDashboard component calls `push('#/')` |

The `RedirectToDashboard.svelte` component:

```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  import { push } from 'svelte-spa-router';
  onMount(() => push('#/'));
</script>
```

---

## i18n Keys — Full List of Additions

| Key | English default |
|---|---|
| `dashboard.searchPlaceholder` | "Search repos and projects…" |
| `dashboard.noRepos` | "No repositories found." |
| `dashboard.noSearchResults` | "No repos or projects match your search." |
| `dashboard.noRepoProjects` | "No projects in this repository." |
| `dashboard.expandAll` | "Expand all" |
| `dashboard.collapseAll` | "Collapse all" |
| `dashboard.repoGroup` | "Repository" |
| `dashboard.projectCount` | "{n} project(s)" |
| `dashboard.orphanProjects` | "Projects without a repository" |

All 8 language dictionaries must be updated with translations for these keys.

---

## Edge Case Coverage

| Edge Case | Handling |
|---|---|
| No repos at all (`listRepos()` returns `[]`) | Show empty state with "No repositories found." and create-project CTA |
| Repo with zero projects (defensive) | Show repo section with "No projects in this repository." text |
| No most-recent project (empty runs) | All repo sections remain collapsed on load |
| Search matches nothing | Show "No repos or projects match your search." with option to clear |
| Orphan projects (no repo_path) | Separate "Projects without a repository" section at bottom |
| User navigates back from project detail | State resets (acceptable per MVP — see SOTA edge case #10) |
| Polling during create-form open | Skip refresh when `createFormVisible` is true (ported from Dashboard.svelte) |
| Rapid expand/collapse clicks | Browser-native `<details>` handles this natively — no extra logic needed |
| RepoPanel API call fails (404, network) | RepoPanel's existing error handling shows inline error message |

---

## File Change Summary

| File | Action | Description |
|---|---|---|
| `web/src/views/UnifiedDashboard.svelte` | **CREATE** | New unified dashboard component |
| `web/src/views/RedirectToDashboard.svelte` | **CREATE** | Redirect wrapper for old `/repos` routes |
| `web/src/views/RepoPanel.svelte` | **MODIFY** | Add `compact` prop and conditional rendering |
| `web/src/App.svelte` | **MODIFY** | Update routes: UnifiedDashboard, RedirectToDashboard |
| `web/src/lib/i18n.svelte.ts` | **MODIFY** | Add 9 new `dashboard.*` i18n keys across all 8 languages |
| `web/src/views/Dashboard.svelte` | **KEEP** | No longer routed; safe to delete in cleanup |
| `web/src/views/Repositories.svelte` | **KEEP** | No longer routed; safe to delete in cleanup |
| `web/src/views/Repository.svelte` | **KEEP** | No longer routed; safe to delete in cleanup |

---

## Extensibility Considerations

1. **Expanded state persistence**: The `expandedRepos` Set can be trivially serialized to `localStorage` (keyed by repo_path) if cross-navigation persistence is desired in the future.
2. **Virtual scrolling**: If repo/project counts grow beyond ~100 repos, the list can be wrapped in a virtual-scroll component without changing the data model.
3. **Additional git operations in compact mode**: The `compact` prop design allows adding more buttons to compact mode without touching full mode.
4. **Search debounce**: Not needed for MVP (simple substring match on <1000 items), but can be added with a Svelte `$effect` + `setTimeout` pattern.
5. **Repo detail deep-link**: The current design redirects `/repos/:repoPath` to `/`. If deep-linking to a specific repo section is desired, a URL hash fragment (e.g., `#/repo/my-repo`) could auto-expand that section — the `expandedRepos` set supports this pattern.

---

## Implementation Order (for PM task decomposition)

| Order | Component | Risk | Dependencies |
|---|---|---|---|
| 1 | `RepoPanel.svelte` — add `compact` prop | Low | None |
| 2 | `i18n.svelte.ts` — add new keys | Low | None |
| 3 | `UnifiedDashboard.svelte` — core component | Medium | 1, 2 |
| 4 | `RedirectToDashboard.svelte` — redirect wrapper | Low | None |
| 5 | `App.svelte` — route updates | Low | 3, 4 |
| 6 | Integration testing / manual verification | Medium | 5 |

---

## Rollback Path

All changes are reversible:

1. Revert `App.svelte` routes to the old Dashboard/Repositories/Repository mapping
2. Remove `UnifiedDashboard.svelte` and `RedirectToDashboard.svelte` (new files, no downstream deps)
3. Revert `RepoPanel.svelte` — remove `compact` prop (or keep it, it's additive and doesn't break existing callers)
4. i18n keys are additive — no need to revert, they don't break anything

No data migrations, backend changes, or irreversible operations.
