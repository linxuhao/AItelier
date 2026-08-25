<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { push } from 'svelte-spa-router';
  import { authStore } from '../stores/auth';
  import { connectionStore } from '../stores/connection';
  import { projectStore, setCurrentProject } from '../stores/project';
  import {
    listRepos,
    listAllRuns,
    createProject,
    deleteProject,
    listPipelines,
    pipelineStateFile,
  } from '../lib/api';
  import type { RepoItem, RepoProjectSummary } from '../lib/api';
  import {
    formatTime,
    formatTokens,
    formatBytes,
    formatTaskProgress,
    parseStatus,
    cacheBadgeClass,
    repoTypeLabel,
  } from '../lib/format';
  import { t } from '../lib/i18n.svelte';
  import RepoPanel from './RepoPanel.svelte';
  import WorkspaceBrowser from './WorkspaceBrowser.svelte';
  import PipelineGraph from './PipelineGraph.svelte';

  // ── State ──

  let repos = $state<RepoItem[]>([]);
  let orphanProjects = $state<Record<string, unknown>[]>([]);
  // Authoring runs (generate_pipeline / generate_addon) — repo-independent
  // config-authoring tooling, not projects. Listed in their own auditable
  // section (trace links) rather than as repos.
  let authoringRuns = $state<Record<string, unknown>[]>([]);
  // Runs of GENERATED pipelines (repo-less by design, but NOT authoring) — real
  // work that produces artifacts yet touches no code repo (e.g. gen_cac40).
  // Their own section, distinct from the generation runs that created them.
  let pipelineRuns = $state<Record<string, unknown>[]>([]);
  // Catalog of every RUNNABLE pipeline -- built-in and generated -- with the
  // addon each carries and the durable state it keeps across runs; state file
  // bodies are lazy-loaded into stateFiles on expand.
  let pipelines = $state<Record<string, unknown>[]>([]);
  // Which catalog rows have their graph open. The graph is fetched by the
  // PipelineGraph component on mount, so an unopened row costs nothing.
  let openGraphs = $state<Set<string>>(new Set());
  let stateFiles = $state<Record<string, string>>({});
  // Load failures live here, NOT in stateFiles — a cached error would satisfy
  // the fetch guard and make the failure permanent for the page's lifetime.
  let stateErrors = $state<Record<string, string>>({});
  let openState = $state<Set<string>>(new Set());
  let loading = $state(true);
  let error = $state<string | null>(null);
  let pollTimer = $state<ReturnType<typeof setInterval> | null>(null);
  let isRefreshing = $state(false);

  // Search state
  let searchQuery = $state('');
  let manualExpandedRepos = $state<Set<string>>(new Set());
  let autoExpandedOnce = $state(false);

  // Create form state (ported from Dashboard.svelte)
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

  let filteredRepos = $derived<RepoItem[]>(
    searchQuery.trim()
      ? repos.filter(r => matchesSearch(r, searchQuery))
      : repos,
  );

  // ── Repo-less content, search-filtered once here rather than per section ──
  // These sections used to live inside the repo `{:else}` branch, so a user with
  // zero code repos (all work in generated pipelines) hit the "no repositories"
  // empty state and never saw them at all. They now render independently, and
  // the empty / no-results states below account for them.
  let matchedAuthoring = $derived(filterRuns(authoringRuns, searchQuery));
  let matchedPipelineRuns = $derived(filterRuns(pipelineRuns, searchQuery));
  let matchedOrphans = $derived(filterRuns(orphanProjects, searchQuery));
  let matchedPipelines = $derived(filterPipelines(pipelines, searchQuery));
  let repoLessCount = $derived(
    matchedAuthoring.length + matchedPipelineRuns.length +
    matchedOrphans.length + matchedPipelines.length,
  );

  let empty = $derived(
    !loading && !error && repos.length === 0 && repoLessCount === 0,
  );

  // Derived: when searching, auto-expand all matching repos.
  // When not searching, use the user-controlled expansion set.
  let expandedRepos = $derived<Set<string>>(
    searchQuery.trim()
      ? new Set(filteredRepos.map(r => r.repo_path))
      : manualExpandedRepos,
  );

  // ── Lifecycle ──

  /** Bucket every repo-less run into exactly one section, in one pass.
   *  Precedence is explicit and structural (else-if), not an artifact of the
   *  order three independent .filter() calls happen to be written in:
   *    is_authoring          → pipeline GENERATION (generate_pipeline/addon)
   *    repo_less             → a RUN of a generated pipeline (gen_cac40, novels)
   *    no repo_path          → orphan project (repo-less by accident)
   *  Converters carry BOTH flags (registers_generated_* + repo_mode: none), so
   *  the first arm is what keeps them in "generation". */
  function bucketRuns(runs: Record<string, unknown>[]) {
    const authoring: Record<string, unknown>[] = [];
    const pipeline: Record<string, unknown>[] = [];
    const orphans: Record<string, unknown>[] = [];
    for (const r of runs) {
      if (r.is_authoring) authoring.push(r);
      else if (r.repo_less) pipeline.push(r);
      else if (r.repo_path == null || r.repo_path === '') orphans.push(r);
    }
    authoringRuns = authoring;
    pipelineRuns = pipeline;
    orphanProjects = orphans;
  }

  /** Runs + catalog: independent of each other and of the repo list, so fetch
   *  them concurrently and let each fail on its own. Previously listPipelines()
   *  was nested inside the listAllRuns() try, so a slow/500 /api/runs skipped
   *  the catalog entirely and the whole section vanished with no error. */
  async function refreshRunsAndCatalog() {
    const [runsRes, pipesRes] = await Promise.allSettled([
      listAllRuns(),
      listPipelines(),
    ]);
    if (runsRes.status === 'fulfilled') {
      const data = runsRes.value;
      bucketRuns(((data as any)?.runs ?? data) as Record<string, unknown>[]);
    }
    if (pipesRes.status === 'fulfilled') {
      pipelines = ((pipesRes.value as any)?.pipelines ?? []) as Record<string, unknown>[];
    }
  }

  onMount(async () => {
    // The three lists are independent — serialising them added a full RTT each
    // before first paint (the 4ms catalog call used to wait on the 176ms runs call).
    await Promise.allSettled([refreshData(), refreshRunsAndCatalog()]);
    pollTimer = setInterval(() => {
      // Poll the run tables too: their status badges are the live data on this
      // page. Refreshing only the repo list made a running pipeline look stuck.
      refreshData();
      refreshRunsAndCatalog();
    }, 10000);
  });

  onDestroy(() => {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
    }
  });

  // ── Methods ──

  function matchesSearch(repo: RepoItem, query: string): boolean {
    const q = query.toLowerCase().trim();
    if (!q) return true;
    return (
      repo.repo_name.toLowerCase().includes(q) ||
      repo.repo_path.toLowerCase().includes(q) ||
      repo.projects.some(
        (p) =>
          p.name.toLowerCase().includes(q) ||
          p.project_id.toLowerCase().includes(q),
      )
    );
  }

  async function refreshData(): Promise<void> {
    if (createFormVisible || isRefreshing) return;
    isRefreshing = true;
    try {
      const data = await listRepos();
      repos = data;
      error = null;

      // Auto-expand most recent repo on first successful load
      if (!autoExpandedOnce && data.length > 0) {
        autoExpandMostRecent(data);
        autoExpandedOnce = true;
      }
    } catch (err: unknown) {
      const msg =
        err instanceof Error ? err.message : 'Failed to load repositories';
      error = msg;
    } finally {
      loading = false;
      isRefreshing = false;
    }
  }

  function autoExpandMostRecent(repoList: RepoItem[]): void {
    if (repoList.length === 0) return;
    let mostRecent = repoList[0];
    for (const r of repoList) {
      if (r.last_activity > mostRecent.last_activity) mostRecent = r;
    }
    manualExpandedRepos = new Set([mostRecent.repo_path]);
  }

  function onRepoToggle(repoPath: string, e: Event): void {
    const details = e.target as HTMLDetailsElement;
    const inSet = expandedRepos.has(repoPath);
    // Guard: skip if the DOM state already matches our set — this breaks the
    // feedback loop when Svelte's open={...} binding triggers ontoggle.
    if (details.open === inSet) return;
    // User clicked — sync the set to match the DOM
    const next = new Set(manualExpandedRepos);
    if (details.open) {
      next.add(repoPath);
    } else {
      next.delete(repoPath);
    }
    manualExpandedRepos = next;
  }

  function expandAll(): void {
    manualExpandedRepos = new Set(repos.map(r => r.repo_path));
  }

  function collapseAll(): void {
    manualExpandedRepos = new Set();
  }

  function navigateToProject(id: string): void {
    setCurrentProject(id);
    push('#/projects/' + encodeURIComponent(id));
  }

  function navigateToTrace(id: string): void {
    push('#/projects/' + encodeURIComponent(id) + '/trace');
  }

  // ── Create form (ported from Dashboard.svelte) ──

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

    const slug = newProjectId.trim();
    if (slug && !/^[a-z0-9][a-z0-9_-]*$/.test(slug)) {
      errors.project_id =
        'Project ID must start with a letter/digit and contain only a-z, 0-9, _, -';
    }

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
    if (repoType === 'existing' && repoPath.trim())
      data.repo_path = repoPath.trim();
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
      await refreshData();
    } catch (err: unknown) {
      const msg =
        err instanceof Error ? err.message : 'Failed to create project';
      if (
        msg.indexOf('already exists') !== -1 ||
        msg.indexOf('409') !== -1 ||
        msg.indexOf('conflict') !== -1
      ) {
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
      await refreshData();
    } catch (err: unknown) {
      const msg =
        err instanceof Error ? err.message : 'Failed to delete project';
      error = msg;
    }
  }

  // ── Helper: check if a run is an orphan (used in orphan section filtering) ──

  function orphanMatchesSearch(
    project: Record<string, unknown>,
    query: string,
  ): boolean {
    const q = query.toLowerCase().trim();
    if (!q) return true;
    return (
      ((project.name as string) || '').toLowerCase().includes(q) ||
      ((project.project_id as string) || '').toLowerCase().includes(q)
    );
  }

  function filterRuns(
    runs: Record<string, unknown>[],
    query: string,
  ): Record<string, unknown>[] {
    return query.trim() ? runs.filter(r => orphanMatchesSearch(r, query)) : runs;
  }

  function filterPipelines(
    list: Record<string, unknown>[],
    query: string,
  ): Record<string, unknown>[] {
    const q = query.toLowerCase().trim();
    if (!q) return list;
    return list.filter(
      p =>
        ((p.config_name as string) || '').toLowerCase().includes(q) ||
        ((p.label as string) || '').toLowerCase().includes(q) ||
        // Searching "game" should find dpe_game via its addon, which is the
        // only thing distinguishing it from the base it inherits its label from.
        (((p.addons as string[]) || []).some(a => a.toLowerCase().includes(q))),
    );
  }

  /** Open/close one catalog row's graph. Fetching is the component's job. */
  function toggleGraph(config: string) {
    const next = new Set(openGraphs);
    if (next.has(config)) next.delete(config);
    else next.add(config);
    openGraphs = next;
  }

  // ── Durable-state viewer: lazy-load a pipeline_state/<config>/<file> body ──
  const stateKey = (config: string, file: string) => config + '\u0000' + file;

  async function toggleState(config: string, file: string) {
    const key = stateKey(config, file);
    // Publish the open/closed flip BEFORE any await. Snapshotting openState and
    // assigning it back after the fetch made concurrent toggles last-writer-wins:
    // expanding a second file while the first was in flight silently collapsed it.
    const next = new Set(openState);
    const opening = !next.has(key);
    if (opening) next.add(key);
    else next.delete(key);
    openState = next;
    if (!opening || key in stateFiles) return;

    // A failed load is NOT cached — it goes in stateErrors, so collapsing and
    // re-expanding retries. Caching '(failed to load)' in stateFiles used to
    // satisfy the fetch guard forever, so one 404/500 poisoned the row until F5.
    stateErrors = { ...stateErrors, [key]: '' };
    try {
      const r = await pipelineStateFile(config, file);
      // The API returns the TAIL of an over-cap file (newest entries), so the
      // truncation marker belongs at the top.
      stateFiles = {
        ...stateFiles,
        [key]: (r.truncated ? '… ' + t('dashboard.stateTruncated') + '\n' : '') + r.content,
      };
    } catch {
      stateErrors = { ...stateErrors, [key]: t('dashboard.stateLoadFailed') };
    }
  }
</script>

<section id="view-dashboard">
  <!-- Reconnect overlay -->
  {#if !connected}
    <dialog class="reconnect-overlay" open>
      <article>
        <header>
          <h3>{t('dashboard.reconnecting')}</h3>
        </header>
        <p>{t('dashboard.reconnectDesc')}</p>
        {#if $connectionStore.reconnectAttempt > 0}
          <p class="reconnect-attempt">
            {t('dashboard.attempt').replace('{n}', String($connectionStore.reconnectAttempt))}
          </p>
        {/if}
      </article>
    </dialog>
  {/if}

  <!-- Page header -->
  <header class="dashboard-header">
    <h2>{t('dashboard.projects')}</h2>
    <div class="dashboard-header-controls">
      <input
        type="search"
        bind:value={searchQuery}
        placeholder={t('dashboard.searchPlaceholder')}
        class="search-input"
      />
      <button class="outline" onclick={expandAll}>{t('dashboard.expandAll')}</button>
      <button class="outline" onclick={collapseAll}>{t('dashboard.collapseAll')}</button>
      {#if canWrite && !createFormVisible}
        <button onclick={toggleCreateForm}>{t('dashboard.newProject')}</button>
      {/if}
    </div>
  </header>

  <!-- Create project form -->
  {#if createFormVisible && canWrite}
    <article class="create-form">
      <header>
        <h3>{t('dashboard.newProjectTitle')}</h3>
      </header>
      <form onsubmit={(e) => { e.preventDefault(); handleCreate(); }}>
        {#if formErrors._general}
          <p class="form-error-general">{formErrors._general}</p>
        {/if}

        <label for="new-project-id">
          {t('dashboard.projectId')}
          <input
            id="new-project-id"
            type="text"
            placeholder={t('dashboard.projectIdPlaceholder')}
            bind:value={newProjectId}
            disabled={submitting}
          />
        </label>
        {#if formErrors.project_id}
          <small class="form-error">{formErrors.project_id}</small>
        {/if}

        <label for="new-project-name">
          {t('dashboard.displayName')}
          <input
            id="new-project-name"
            type="text"
            placeholder={t('dashboard.displayNamePlaceholder')}
            bind:value={newProjectName}
            disabled={submitting}
          />
        </label>

        <label for="seed-text">
          {t('dashboard.buildRequest')}
          <textarea
            id="seed-text"
            placeholder={t('dashboard.buildRequestPlaceholder')}
            bind:value={seedText}
            disabled={submitting}
            rows="3"
          ></textarea>
        </label>

        <label for="repo-type">
          {t('dashboard.repository')}
          <select id="repo-type" bind:value={repoType} disabled={submitting}>
            <option value="new">{t('dashboard.repoNew')}</option>
            <option value="existing">{t('dashboard.repoExisting')}</option>
            <option value="clone">{t('dashboard.repoClone')}</option>
          </select>
        </label>

        {#if repoType === 'existing'}
          <label for="repo-path">
            {t('dashboard.repoPath')}
            <input
              id="repo-path"
              type="text"
              placeholder={t('dashboard.repoPathPlaceholder')}
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
            {t('dashboard.repoUrl')}
            <input
              id="repo-url"
              type="url"
              placeholder={t('dashboard.repoUrlPlaceholder')}
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
            {submitting ? t('dashboard.creating') : t('dashboard.createProject')}
          </button>
          <button
            type="button"
            class="secondary"
            onclick={toggleCreateForm}
            disabled={submitting}
          >
            {t('dashboard.cancel')}
          </button>
        </div>
      </form>
    </article>
  {/if}

  <!-- Loading state -->
  {#if loading}
    <article aria-busy="true">
      <p>{t('dashboard.loading')}</p>
    </article>
  {:else if error && repos.length === 0}
    <!-- Error state (only full-page when no repos loaded) -->
    <article class="error-state">
      <header>
        <h3>{t('dashboard.failedToLoad')}</h3>
      </header>
      <p>{error}</p>
      <button onclick={refreshData}>{t('dashboard.retry')}</button>
    </article>
  {:else if empty}
    <!-- Empty state -->
    <article class="empty-state">
      <p>{t('dashboard.noRepos')}</p>
      {#if canWrite}
        <p>{@html t('dashboard.createFirst')}</p>
      {:else}
        <p>{t('dashboard.signInForWrite')}</p>
      {/if}
    </article>
  {:else if searchQuery && filteredRepos.length === 0 && repoLessCount === 0}
    <!-- No search results -->
    <article class="empty-state">
      <p>{t('dashboard.noSearchResults')}</p>
      <button class="outline" onclick={() => { searchQuery = ''; }}>{t('dashboard.retry')}</button>
    </article>
  {:else}
    <!-- Repo accordion list -->
    {#each filteredRepos as repo (repo.repo_path)}
      <details
        class="repo-section"
        open={expandedRepos.has(repo.repo_path)}
        ontoggle={(e) => onRepoToggle(repo.repo_path, e)}
      >
        <summary class="repo-summary">
          <span class="repo-summary-name">
            <strong>{repo.repo_name}</strong>
            <span class="repo-type-badge">{repoTypeLabel(repo.repo_type)}</span>
          </span>
          <span class="repo-summary-meta">
            <span class="project-count">
              {t('dashboard.projectCount').replace('{n}', String(repo.project_count))}
            </span>
            <span class="last-activity">
              {formatTime(repo.last_activity)}
            </span>
          </span>
        </summary>

        <!-- Lazy load RepoPanel + file browser only when expanded -->
        {#if expandedRepos.has(repo.repo_path)}
          <RepoPanel
            projectId={repo.representative_project_id}
            {canWrite}
          />
          <!-- Repository files browser (folded by default) — sits under the repo
               operations panel; restores the file browser lost in the group-by-repo
               migration. -->
          <WorkspaceBrowser
            projectId={repo.representative_project_id}
            root="code"
            title={t('repo.files')}
          />
        {/if}

        <!-- Project table -->
        {#if repo.projects.length > 0}
          <figure>
            <table class="project-table">
              <thead>
                <tr>
                  <th>{t('dashboard.colNum')}</th>
                  <th>{t('dashboard.colProject')}</th>
                  <th>{t('dashboard.colStatus')}</th>
                  <th>{t('dashboard.colTasks')}</th>
                  <th>{t('dashboard.colLastUpdate')}</th>
                  {#if canWrite}
                    <th></th>
                  {/if}
                </tr>
              </thead>
              <tbody>
                {#each repo.projects as project, idx}
                  <tr
                    class="project-row"
                    onclick={() => navigateToProject(project.project_id)}
                  >
                    <td>{idx + 1}</td>
                    <td>
                      <a
                        href="#/projects/{encodeURIComponent(project.project_id)}"
                        onclick={(e) => {
                          e.preventDefault();
                          navigateToProject(project.project_id);
                        }}
                      >
                        {project.name || project.project_id}
                      </a>
                    </td>
                    <td>
                      {#if project.status}
                        {@const parsed = parseStatus(project.status)}
                        <span class="status-badge {parsed.className}" title={parsed.text}>
                          {parsed.icon} {parsed.text}
                        </span>
                      {:else}
                        <span class="status-badge">—</span>
                      {/if}
                      {#if project.cache_stats && (project.cache_stats as Record<string, number>).hit_ratio != null}
                        {@const cs = project.cache_stats as Record<string, number>}
                        <span
                          class="cache-inline-badge {cacheBadgeClass(cs.hit_ratio)}"
                          title={t('chat.cacheHitRatio')}
                        >
                          Cache {(cs.hit_ratio * 100).toFixed(1)}%
                          {cs.total_tokens != null ? ' · ' + formatTokens(cs.total_tokens) : ''}
                        </span>
                      {/if}
                    </td>
                    <td>
                      <span class="task-progress">{formatTaskProgress(project)}</span>
                    </td>
                    <td>
                      <span class="timestamp">{formatTime((project.last_update as number) ?? project.updated_at)}</span>
                    </td>
                    {#if canWrite}
                      <td>
                        <button
                          class="delete-btn"
                          onclick={(e) => {
                            e.stopPropagation();
                            confirmDelete(project.project_id);
                          }}
                          title={t('dashboard.deleteTitle')}
                        >✕</button>
                      </td>
                    {/if}
                  </tr>
                {/each}
              </tbody>
            </table>
          </figure>
        {:else}
          <p class="no-projects-msg">{t('dashboard.noRepoProjects')}</p>
        {/if}
      </details>
    {/each}
  {/if}

  <!-- ── Repo-less sections ──────────────────────────────────────────────
       Rendered OUTSIDE the repo if/else chain above: a user whose work is
       entirely repo-less (generated pipelines, novels) has repos.length === 0,
       which used to land on the "no repositories yet" branch and hide all of
       this. Each section still guards on its own (search-filtered) contents. -->
  {#if !loading && !(error && repos.length === 0)}
    <!-- Orphan projects section -->
    {#if orphanProjects.length > 0}
      {@const filteredOrphans = matchedOrphans}
      {#if filteredOrphans.length > 0}
        <details class="repo-section orphan-section">
          <summary class="repo-summary">
            <span class="repo-summary-name">
              <strong>{t('dashboard.orphanProjects')}</strong>
            </span>
            <span class="repo-summary-meta">
              <span class="project-count">
                {t('dashboard.projectCount').replace('{n}', String(filteredOrphans.length))}
              </span>
            </span>
          </summary>
          <figure>
            <table class="project-table">
              <thead>
                <tr>
                  <th>{t('dashboard.colNum')}</th>
                  <th>{t('dashboard.colProject')}</th>
                  <th>{t('dashboard.colStatus')}</th>
                  <th>{t('dashboard.colTasks')}</th>
                  <th>{t('dashboard.colLastUpdate')}</th>
                  {#if canWrite}
                    <th></th>
                  {/if}
                </tr>
              </thead>
              <tbody>
                {#each filteredOrphans as project, idx}
                  <tr
                    class="project-row"
                    onclick={() => navigateToProject(project.project_id as string)}
                  >
                    <td>{idx + 1}</td>
                    <td>
                      <a
                        href="#/projects/{encodeURIComponent(project.project_id as string)}"
                        onclick={(e) => {
                          e.preventDefault();
                          navigateToProject(project.project_id as string);
                        }}
                      >
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
                          class="cache-inline-badge {cacheBadgeClass(cs.hit_ratio)}"
                          title={t('chat.cacheHitRatio')}
                        >
                          Cache {(cs.hit_ratio * 100).toFixed(1)}%
                          {cs.total_tokens != null ? ' · ' + formatTokens(cs.total_tokens) : ''}
                        </span>
                      {/if}
                    </td>
                    <td>
                      <span class="task-progress">{formatTaskProgress(project)}</span>
                    </td>
                    <td>
                      <span class="timestamp">{formatTime((project.last_update as number) ?? project.updated_at)}</span>
                    </td>
                    {#if canWrite}
                      <td>
                        <button
                          class="delete-btn"
                          onclick={(e) => {
                            e.stopPropagation();
                            confirmDelete(project.project_id as string);
                          }}
                          title={t('dashboard.deleteTitle')}
                        >✕</button>
                      </td>
                    {/if}
                  </tr>
                {/each}
              </tbody>
            </table>
          </figure>
        </details>
      {/if}
    {/if}

    <!-- Reusable run table for a repo-less run bucket (generation / pipeline runs).
         `runs` arrives already search-filtered (matchedAuthoring / matchedPipelineRuns). -->
    {#snippet runSection(runs, titleKey, hintKey)}
      {#if runs.length > 0}
        <details class="repo-section authoring-section">
          <summary class="repo-summary">
            <span class="repo-summary-name"><strong>{t(titleKey)}</strong></span>
            <span class="repo-summary-meta">
              <span class="project-count">
                {t('dashboard.projectCount').replace('{n}', String(runs.length))}
              </span>
            </span>
          </summary>
          <p class="authoring-hint">{t(hintKey)}</p>
          <figure>
            <table class="project-table">
              <thead>
                <tr>
                  <th>{t('dashboard.colNum')}</th>
                  <th>{t('dashboard.colProject')}</th>
                  <th>{t('dashboard.colKind')}</th>
                  <th>{t('dashboard.colStatus')}</th>
                  <th>{t('dashboard.colLastUpdate')}</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {#each runs as run, idx}
                  <tr class="project-row">
                    <td>{idx + 1}</td>
                    <td>
                      <!-- The run PAGE, not its trace. A repo-less run has the
                           same steps, checkpoints and outputs as any other; the
                           trace is one tab inside that, and jumping a reader
                           straight into raw trace rows skipped the level they
                           were asking for. The trace stays one click away. -->
                      <a
                        href="#/projects/{encodeURIComponent(run.project_id)}"
                        onclick={(e) => { e.preventDefault(); navigateToProject(run.project_id); }}
                      >{run.name || run.project_id}</a>
                    </td>
                    <td><span class="repo-type-badge">{run.config_label || run.config_name}</span></td>
                    <td>
                      {#if run.status}
                        {@const parsed = parseStatus(run.status)}
                        <span class="status-badge {parsed.className}" title={parsed.text}>{parsed.icon} {parsed.text}</span>
                      {:else}
                        <span class="status-badge">—</span>
                      {/if}
                    </td>
                    <td><span class="timestamp">{formatTime(run.last_update ?? run.updated_at)}</span></td>
                    <td>
                      <a
                        class="repo-btn"
                        href="#/projects/{encodeURIComponent(run.project_id)}/trace"
                        onclick={(e) => { e.preventDefault(); navigateToTrace(run.project_id); }}
                      >{t('dashboard.viewTrace')}</a>
                      {#if canWrite}
                        <button class="delete-btn" onclick={(e) => { e.stopPropagation(); confirmDelete(run.project_id); }} title={t('dashboard.deleteTitle')}>✕</button>
                      {/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </figure>
        </details>
      {/if}
    {/snippet}

    <!-- Pipeline generation (generate_pipeline / generate_addon). -->
    {@render runSection(matchedAuthoring, 'dashboard.authoringRuns', 'dashboard.authoringHint')}
    <!-- Repo-less pipeline RUNS (gen_* executions like cac40, novels). -->
    {@render runSection(matchedPipelineRuns, 'dashboard.pipelineRuns', 'dashboard.pipelineRunsHint')}

    <!-- Catalog: every pipeline you can run — built-in and generated — with the
         addon each carries and the durable state it keeps between runs. -->
    {#if matchedPipelines.length > 0}
      <details class="repo-section authoring-section">
        <summary class="repo-summary">
          <span class="repo-summary-name"><strong>{t('dashboard.pipelineCatalog')}</strong></span>
          <span class="repo-summary-meta">
            <span class="project-count">{t('dashboard.projectCount').replace('{n}', String(matchedPipelines.length))}</span>
          </span>
        </summary>
        <p class="authoring-hint">{t('dashboard.pipelineCatalogHint')}</p>
        <figure>
          <table class="project-table">
            <thead>
              <tr>
                <th>{t('dashboard.colProject')}</th>
                <th>{t('dashboard.colOrigin')}</th>
                <th>{t('dashboard.colAddons')}</th>
                <th>{t('dashboard.colState')}</th>
              </tr>
            </thead>
            <tbody>
              {#each matchedPipelines as p}
                <tr class="project-row">
                  <td>
                    <button class="graph-toggle" onclick={() => toggleGraph(p.config_name as string)}
                            title={t('dashboard.viewGraphTitle')}>
                      {openGraphs.has(p.config_name as string) ? '▾' : '▸'}
                      <span class="repo-type-badge">{p.label || p.config_name}</span>
                    </button>
                    <small class="muted config-name">{p.config_name}</small>
                  </td>
                  <td>
                    <span class="origin-badge {p.origin}">{t('dashboard.origin.' + p.origin)}</span>
                    <small class="muted">{t('dashboard.stepCount').replace('{n}', String(p.step_count ?? 0))}</small>
                  </td>
                  <td>
                    {#if (p.addons as string[] | undefined)?.length}
                      {#each p.addons as a}
                        <span class="addon-badge">{a}</span>
                      {/each}
                      <!-- The composed name inherits the BASE's label, so without
                           this the combo is indistinguishable from its base. -->
                      <small class="muted">{t('dashboard.onBase').replace('{base}', String(p.base))}</small>
                    {:else}
                      <span class="muted">—</span>
                    {/if}
                  </td>
                  <td>
                    {#if p.state_files?.length}
                      {#each p.state_files as sfile}
                        {@const key = stateKey(p.config_name, sfile.name)}
                        {@const open = openState.has(key)}
                        <div class="state-file">
                          <button class="state-toggle" onclick={() => toggleState(p.config_name, sfile.name)}>
                            {open ? '▾' : '▸'} {sfile.name} <small>({formatBytes(sfile.size)})</small>
                          </button>
                          {#if open}
                            {#if stateErrors[key]}
                              <pre class="state-body state-error">{stateErrors[key]}</pre>
                            {:else}
                              <pre class="state-body">{stateFiles[key] ?? '…'}</pre>
                            {/if}
                          {/if}
                        </div>
                      {/each}
                    {:else}
                      <span class="muted">{t('dashboard.noDurableState')}</span>
                    {/if}
                  </td>
                </tr>
                {#if openGraphs.has(p.config_name as string)}
                  <tr class="graph-row">
                    <td colspan="4">
                      <PipelineGraph config={p.config_name as string} />
                    </td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
        </figure>
      </details>
    {/if}
  {/if}

  <!-- Delete confirmation dialog -->
  {#if pendingDeleteId !== null}
    <dialog class="confirm-dialog" open>
      <article>
        <header>
          <h3>{t('dashboard.deleteTitle')}</h3>
        </header>
        <p>{@html t('dashboard.deleteConfirmMsg').replace('{id}', pendingDeleteId || '')}</p>
        <p class="warning">{t('dashboard.deleteWarning')}</p>
        <footer>
          <button class="secondary" onclick={cancelDelete}>{t('dashboard.cancel')}</button>
          <button class="contrast" onclick={handleDelete}>{t('dashboard.delete')}</button>
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
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .dashboard-header h2 {
    margin: 0;
  }

  .dashboard-header-controls {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .search-input {
    min-width: 200px;
  }

  /* ── Repo section (details/summary accordion) ── */
  .repo-section {
    margin-bottom: var(--pico-spacing, 0.75rem);
    border: 1px solid var(--pico-muted-border-color, #e0e0e0);
    border-radius: 0.4rem;
    padding: 0.25rem 0.5rem;
    background: var(--pico-card-background-color, #fff);
  }

  .repo-summary {
    display: flex;
    justify-content: space-between;
    align-items: center;
    cursor: pointer;
    padding: 0.5rem 0.25rem;
  }

  .repo-summary-name {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .repo-type-badge {
    font-size: 0.7rem;
    color: var(--pico-muted-color, #888);
    border: 1px solid var(--pico-muted-border-color, #ddd);
    border-radius: 0.25rem;
    padding: 0.05rem 0.4rem;
    text-transform: uppercase;
  }

  .repo-summary-meta {
    display: flex;
    align-items: center;
    gap: 1rem;
    font-size: 0.8rem;
    color: var(--pico-muted-color, #888);
  }

  .project-count {
    white-space: nowrap;
  }

  .last-activity {
    white-space: nowrap;
  }

  .no-projects-msg {
    color: var(--pico-muted-color, #888);
    font-size: 0.85rem;
    padding: 0.5rem;
  }

  /* ── Orphan section ── */
  .orphan-section {
    margin-top: 1.5rem;
  }

  .authoring-section {
    margin-top: 1.5rem;
  }
  .authoring-hint {
    margin: 0.25rem 0 0.5rem;
    font-size: 0.8rem;
    color: var(--pico-muted-color, #888);
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

  /* ── Pipeline catalog: durable-state viewer ── */
  .state-file { margin: 0.15rem 0; }
  .state-toggle {
    background: none; border: none; padding: 0.1rem 0.2rem; margin: 0;
    width: auto; font: inherit; cursor: pointer;
    color: var(--pico-primary, #0669c1);
  }
  .state-toggle small { color: var(--pico-muted-color, #8a8a8a); }
  .state-body {
    margin: 0.25rem 0 0.5rem; padding: 0.5rem; max-height: 16rem; overflow: auto;
    font-size: 0.78rem; white-space: pre-wrap; word-break: break-word;
    background: var(--pico-code-background-color, #f4f4f4); border-radius: 4px;
  }
  .state-error { color: var(--pico-del-color, #b3261e); }
  .muted { color: var(--pico-muted-color, #8a8a8a); font-size: 0.85rem; }

  /* ── Pipeline catalog: origin / addon / graph ── */
  .graph-toggle {
    background: none; border: none; padding: 0; margin: 0;
    width: auto; font: inherit; cursor: pointer; text-align: left;
    color: inherit;
  }
  .config-name { display: block; font-size: 0.72rem; }
  .origin-badge {
    display: inline-block; padding: 0.05rem 0.4rem; border-radius: 4px;
    font-size: 0.72rem; white-space: nowrap;
    background: var(--pico-code-background-color, #f4f4f4);
    color: var(--pico-muted-color, #8a8a8a);
  }
  .origin-badge.generated {
    background: var(--pico-color-green-100, #d7f5df);
    color: var(--pico-color-green-700, #256a3a);
  }
  .addon-badge {
    display: inline-block; padding: 0.05rem 0.4rem; margin-right: 0.2rem;
    border-radius: 4px; font-size: 0.72rem;
    background: var(--pico-color-yellow-100, #fdf3d0);
    color: var(--pico-color-yellow-700, #7a5c00);
  }
  .graph-row td { padding-top: 0; }
</style>
