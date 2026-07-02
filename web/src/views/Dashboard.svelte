<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { push } from 'svelte-spa-router';
  import { authStore } from '../stores/auth';
  import { connectionStore } from '../stores/connection';
  import { projectStore, setCurrentProject } from '../stores/project';
  import { listAllRuns, createProject, deleteProject } from '../lib/api';
  import { formatTime, formatTokens, formatTaskProgress, parseStatus } from '../lib/format';

  // ── State ──

  let projects = $state<any[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let pollTimer = $state<ReturnType<typeof setInterval> | null>(null);
  let isRefreshing = $state(false);

  // Create form state
  let createFormVisible = $state(false);
  let newProjectId = $state('');
  let newProjectName = $state('');
  let seedText = $state('');
  let repoType = $state('new');
  let repoPath = $state('');
  let repoUrl = $state('');
  let submitting = $state(false);
  let formErrors = $state<Record<string, string>>({});

  // Delete confirmation state
  let pendingDeleteId = $state<string | null>(null);

  // ── Derived ──

  let canWrite = $derived($authStore.permissionResolved && $authStore.canWrite);
  let connected = $derived($connectionStore.connectionOk);
  let empty = $derived(!loading && !error && projects.length === 0);

  // ── Lifecycle ──

  onMount(async () => {
    await refreshProjects();
    pollTimer = setInterval(refreshProjects, 10000);
  });

  onDestroy(() => {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
    }
  });

  // ── Methods ──

  async function refreshProjects(): Promise<void> {
    // Don't refresh while form is open (prevents input reset)
    if (createFormVisible || isRefreshing) return;
    isRefreshing = true;
    try {
      // GET /api/runs is the ENRICHED project list (cache_stats, config
      // label, task counts) — /api/projects carries no token/cache data.
      const data = await listAllRuns();
      projects = ((data as any)?.runs ?? data) as Record<string, unknown>[];
      error = null;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load projects';
      error = msg;
    } finally {
      loading = false;
      isRefreshing = false;
    }
  }

  function navigateToProject(id: string): void {
    setCurrentProject(id);
    push('#/projects/' + encodeURIComponent(id));
  }

  function toggleCreateForm(): void {
    createFormVisible = !createFormVisible;
    if (!createFormVisible) {
      resetForm();
    }
  }

  function resetForm(): void {
    newProjectId = '';
    newProjectName = '';
    seedText = '';
    repoType = 'new';
    repoPath = '';
    repoUrl = '';
    formErrors = {};
  }

  function validateForm(): { valid: boolean; data: Record<string, unknown> } {
    const errors: Record<string, string> = {};

    // Slug validation (optional but must be valid if provided)
    const slug = newProjectId.trim();
    if (slug && !/^[a-z0-9][a-z0-9_-]*$/.test(slug)) {
      errors.project_id = 'Project ID must start with a letter/digit and contain only a-z, 0-9, _, -';
    }

    // Repo validation
    if (repoType === 'existing' && !repoPath.trim()) {
      errors.repo_path = 'Repo path is required when using existing repo';
    }
    if (repoType === 'clone' && !repoUrl.trim()) {
      errors.repo_url = 'Repo URL is required when cloning';
    }

    formErrors = errors;
    const valid = Object.keys(errors).length === 0;

    const data: Record<string, unknown> = {};
    if (slug) data.project_id = slug;
    if (newProjectName.trim()) data.name = newProjectName.trim();
    if (seedText.trim()) data.seed_text = seedText.trim();
    data.repo_type = repoType;
    if (repoType === 'existing' && repoPath.trim()) data.repo_path = repoPath.trim();
    if (repoType === 'clone' && repoUrl.trim()) data.repo_url = repoUrl.trim();

    return { valid, data };
  }

  async function handleCreate(): Promise<void> {
    const { valid, data } = validateForm();
    if (!valid) return;

    submitting = true;
    try {
      await createProject(data);
      resetForm();
      createFormVisible = false;
      await refreshProjects();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to create project';
      if (msg.indexOf('already exists') !== -1 || msg.indexOf('409') !== -1 || msg.indexOf('conflict') !== -1) {
        formErrors = { project_id: 'Project already exists' };
      } else {
        formErrors = { _general: msg };
      }
    } finally {
      submitting = false;
    }
  }

  function confirmDelete(id: string): void {
    pendingDeleteId = id;
  }

  function cancelDelete(): void {
    pendingDeleteId = null;
  }

  async function handleDelete(): Promise<void> {
    if (pendingDeleteId === null) return;
    const id = pendingDeleteId;
    pendingDeleteId = null;
    try {
      await deleteProject(id);
      await refreshProjects();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to delete project';
      error = msg;
    }
  }
</script>

<section id="view-dashboard">
  <!-- Reconnect overlay -->
  {#if !connected}
    <dialog class="reconnect-overlay" open>
      <article>
        <header>
          <h3>Reconnecting…</h3>
        </header>
        <p>The connection to the server is lost. Retrying automatically…</p>
        {#if $connectionStore.reconnectAttempt > 0}
          <p class="reconnect-attempt">Attempt {$connectionStore.reconnectAttempt}</p>
        {/if}
      </article>
    </dialog>
  {/if}

  <!-- Page header -->
  <header class="dashboard-header">
    <h2>Projects</h2>
    {#if canWrite && !createFormVisible}
      <button class="outline" onclick={toggleCreateForm}>+ New Project</button>
    {/if}
  </header>

  <!-- Create project form -->
  {#if createFormVisible && canWrite}
    <article class="create-form">
      <header>
        <h3>New Project</h3>
      </header>
      <form onsubmit={(e) => { e.preventDefault(); handleCreate(); }}>
        {#if formErrors._general}
          <p class="form-error-general">{formErrors._general}</p>
        {/if}

        <label for="new-project-id">
          Project ID (slug)
          <input
            id="new-project-id"
            type="text"
            placeholder="my-project"
            bind:value={newProjectId}
            disabled={submitting}
          />
        </label>
        {#if formErrors.project_id}
          <small class="form-error">{formErrors.project_id}</small>
        {/if}

        <label for="new-project-name">
          Display Name (optional)
          <input
            id="new-project-name"
            type="text"
            placeholder="My Project"
            bind:value={newProjectName}
            disabled={submitting}
          />
        </label>

        <label for="seed-text">
          Build Request (optional)
          <textarea
            id="seed-text"
            placeholder="Describe what you want to build…"
            bind:value={seedText}
            disabled={submitting}
            rows="3"
          ></textarea>
        </label>

        <label for="repo-type">
          Repository
          <select id="repo-type" bind:value={repoType} disabled={submitting}>
            <option value="new">New (auto-create)</option>
            <option value="existing">Existing (local path)</option>
            <option value="clone">Clone from URL</option>
          </select>
        </label>

        {#if repoType === 'existing'}
          <label for="repo-path">
            Repo Path
            <input
              id="repo-path"
              type="text"
              placeholder="/path/to/repo"
              bind:value={repoPath}
              disabled={submitting}
            />
          </label>
          {#if formErrors.repo_path}
            <small class="form-error">{formErrors.repo_path}</small>
          {/if}
        {/if}

        {#if repoType === 'clone'}
          <label for="repo-url">
            Repo URL
            <input
              id="repo-url"
              type="url"
              placeholder="https://github.com/user/repo.git"
              bind:value={repoUrl}
              disabled={submitting}
            />
          </label>
          {#if formErrors.repo_url}
            <small class="form-error">{formErrors.repo_url}</small>
          {/if}
        {/if}

        <div class="form-actions">
          <button type="submit" disabled={submitting}>
            {submitting ? 'Creating…' : 'Create Project'}
          </button>
          <button type="button" class="secondary" onclick={toggleCreateForm} disabled={submitting}>
            Cancel
          </button>
        </div>
      </form>
    </article>
  {/if}

  <!-- Loading state -->
  {#if loading}
    <article aria-busy="true">
      <p>Loading projects…</p>
    </article>
  {:else if error && projects.length === 0}
    <!-- Error state (only show full-page error when no projects loaded) -->
    <article class="error-state">
      <header>
        <h3>Failed to load projects</h3>
      </header>
      <p>{error}</p>
      <button onclick={refreshProjects}>Retry</button>
    </article>
  {:else if empty}
    <!-- Empty state -->
    <article class="empty-state">
      <p>No projects yet.</p>
      {#if canWrite}
        <p>Click <strong>+ New Project</strong> to create your first project.</p>
      {:else}
        <p>Sign in with write access to create a new project.</p>
      {/if}
    </article>
  {:else}
    <!-- Project table -->
    <figure>
      <table class="project-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Project</th>
            <th>Status</th>
            <th>Tasks</th>
            <th>Last Update</th>
            {#if canWrite}
              <th></th>
            {/if}
          </tr>
        </thead>
        <tbody>
          {#each projects as project, idx}
            <tr class="project-row" onclick={() => navigateToProject(project.project_id as string)}>
              <td>{idx + 1}</td>
              <td>
                <a href="#/projects/{encodeURIComponent(project.project_id as string)}"
                   onclick={(e) => { e.preventDefault(); navigateToProject(project.project_id as string); }}>
                  {project.name || (project.project_id as string)}
                </a>
              </td>
              <td>
                {#if project.status}
                  {@const parsed = parseStatus(project.status as string)}
                  <span class="status-badge {parsed.className}" title={parsed.text}>
                    {parsed.icon} {parsed.text}
                  </span>
                {:else}
                  <span class="status-badge">—</span>
                {/if}
                {#if project.cache_stats && (project.cache_stats as Record<string, number>).hit_ratio != null}
                  {@const cs = project.cache_stats as Record<string, number>}
                  <span
                    class="cache-inline-badge"
                    class:cache-badge-high={cs.hit_ratio >= 0.7}
                    class:cache-badge-mid={cs.hit_ratio >= 0.3 && cs.hit_ratio < 0.7}
                    class:cache-badge-low={cs.hit_ratio < 0.3}
                    title="Prompt cache hit ratio · total tokens"
                  >
                    Cache {(cs.hit_ratio * 100).toFixed(1)}%{cs.total_tokens != null ? ' · ' + formatTokens(cs.total_tokens) : ''}
                  </span>
                {/if}
              </td>
              <td>
                <span class="task-progress">{formatTaskProgress(project)}</span>
              </td>
              <td>
                <span class="timestamp">{formatTime(project.last_update as number ?? project.updated_at)}</span>
              </td>
              {#if canWrite}
                <td>
                  <button
                    class="delete-btn"
                    onclick={(e) => { e.stopPropagation(); confirmDelete(project.project_id as string); }}
                    title="Delete project"
                  >✕</button>
                </td>
              {/if}
            </tr>
          {/each}
        </tbody>
      </table>
    </figure>
  {/if}

  <!-- Delete confirmation dialog -->
  {#if pendingDeleteId !== null}
    <dialog class="confirm-dialog" open>
      <article>
        <header>
          <h3>Delete Project</h3>
        </header>
        <p>Are you sure you want to delete project <strong>{pendingDeleteId}</strong>?</p>
        <p class="warning">This action cannot be undone.</p>
        <footer>
          <button class="secondary" onclick={cancelDelete}>Cancel</button>
          <button class="contrast" onclick={handleDelete}>Delete</button>
        </footer>
      </article>
    </dialog>
  {/if}
</section>

<style>
  .dashboard-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--pico-spacing, 1rem);
  }

  .dashboard-header h2 {
    margin: 0;
  }

  /* ── Create form ── */
  .create-form {
    margin-bottom: var(--pico-spacing, 1rem);
  }

  .create-form .form-actions {
    display: flex;
    gap: 0.5rem;
    margin-top: var(--pico-spacing, 1rem);
  }

  .form-error {
    color: var(--pico-color-red-500, #c00);
  }

  .form-error-general {
    color: var(--pico-color-red-500, #c00);
    margin-bottom: var(--pico-spacing, 0.5rem);
    padding: 0.5rem;
    background: var(--pico-color-red-100, #fee);
    border-radius: var(--pico-border-radius, 4px);
  }

  /* ── Project table ── */
  .project-row {
    cursor: pointer;
    transition: background 0.15s;
  }

  .project-row:hover {
    background: var(--pico-table-row-hover-background, rgba(128, 128, 128, 0.05));
  }

  .project-row td:first-child {
    width: 2.5rem;
    color: var(--pico-muted-color, #888);
    font-size: 0.875rem;
  }

  .project-row a {
    font-weight: 600;
  }

  /* ── Status badge ── */
  .status-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.125rem 0.5rem;
    border-radius: var(--pico-border-radius, 4px);
    font-size: 0.875rem;
    white-space: nowrap;
  }

  .status-badge.status-ok {
    background: var(--pico-color-green-100, #efe);
    color: var(--pico-color-green-700, #060);
  }

  .status-badge.status-warn {
    background: var(--pico-color-yellow-100, #ffe);
    color: var(--pico-color-yellow-700, #960);
  }

  .status-badge.status-err {
    background: var(--pico-color-red-100, #fee);
    color: var(--pico-color-red-700, #c00);
  }

  /* ── Task progress ── */
  .task-progress {
    font-variant-numeric: tabular-nums;
    font-size: 0.875rem;
  }

  /* ── Timestamp ── */
  .timestamp {
    font-size: 0.875rem;
    color: var(--pico-muted-color, #888);
  }

  /* ── Delete button ── */
  .delete-btn {
    background: none;
    border: none;
    color: var(--pico-color-red-500, #c00);
    cursor: pointer;
    font-size: 1rem;
    padding: 0.25rem;
    opacity: 0.5;
    transition: opacity 0.15s;
  }

  .delete-btn:hover {
    opacity: 1;
  }

  /* ── Empty / error states ── */
  .empty-state,
  .error-state {
    text-align: center;
    padding: 2rem 1rem;
  }

  /* ── Reconnect overlay ── */
  .reconnect-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--pico-background-color, rgba(0, 0, 0, 0.5));
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .reconnect-overlay article {
    max-width: 400px;
    text-align: center;
  }

  .reconnect-attempt {
    font-size: 0.75rem;
    color: var(--pico-muted-color, #888);
  }

  /* ── Confirm dialog ── */
  .confirm-dialog {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--pico-background-color, rgba(0, 0, 0, 0.5));
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .confirm-dialog article {
    max-width: 400px;
  }

  .confirm-dialog .warning {
    color: var(--pico-color-red-500, #c00);
    font-size: 0.875rem;
  }

  .confirm-dialog footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
  }
</style>
