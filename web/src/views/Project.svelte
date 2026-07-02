<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { push } from 'svelte-spa-router';
  import { authStore } from '../stores/auth';
  import { connectionStore } from '../stores/connection';
  import { projectStore, setCurrentProject } from '../stores/project';
  import {
    getProject,
    listRuns,
    getRunDetail,
    retryProject,
    refreshPlanning,
    patchProject,
    getCheckpoint,
    approveCheckpoint,
    rejectCheckpoint,
  } from '../lib/api';
  import {
    formatTime,
    formatTokens,
    statusClass,
    stepLabel,
    statusIcon,
    parseStatus,
    escapeHtml,
    truncate,
  } from '../lib/format';

  // ── Props (route params from svelte-spa-router) ──

  let { params = {} as Record<string, string> } = $props();

  // ── Core data state ──

  let project = $state<Record<string, unknown> | null>(null);
  let checkpointFeedback = $state('');
  let runs = $state<Record<string, unknown>[]>([]);
  let runDetail = $state<Record<string, unknown> | null>(null);
  let selectedRunId = $state<string | null>(null);
  let checkpoint = $state<Record<string, unknown> | null>(null);

  // ── UI state ──

  let loading = $state(true);
  let error = $state<string | null>(null);
  let activeTab = $state<'runs' | 'config'>('runs');
  let pollTimer = $state<ReturnType<typeof setInterval> | null>(null);
  let isRefreshing = $state(false);
  let expandedSteps = $state<Record<string, boolean>>({});
  let actionLoading = $state<Record<string, boolean>>({});

  // Config editing state
  let isEditingConfig = $state(false);
  let configFormData = $state<Record<string, string>>({});
  let configSaving = $state(false);
  let configError = $state<string | null>(null);

  // ── Derived ──

  let canWrite = $derived($authStore.permissionResolved && $authStore.canWrite);
  let connected = $derived($connectionStore.connectionOk);
  let projectId = $derived(params.id || '');

  // ── Lifecycle ──

  onMount(async () => {
    if (projectId) {
      setCurrentProject(projectId);
      await refreshData();
    }
    pollTimer = setInterval(refreshData, 3000);
  });

  onDestroy(() => {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
    }
  });

  // Re-fetch when route param changes
  $effect(() => {
    const pid = params.id;
    if (pid && pid !== $projectStore.currentProjectId) {
      setCurrentProject(pid);
      loading = true;
      error = null;
      selectedRunId = null;
      runDetail = null;
      checkpoint = null;
      expandedSteps = {};
      activeTab = 'runs';
      refreshData();
    }
  });

  // ── Data fetching ──

  async function refreshData(): Promise<void> {
    const pid = params.id;
    if (!pid || isRefreshing) return;
    isRefreshing = true;
    try {
      const [proj, runsData, cpData] = await Promise.all([
        getProject(pid),
        listRuns(pid),
        getCheckpoint(pid).catch(() => null),
      ]);
      // Guard against stale responses when navigating away quickly
      if (params.id !== pid) return;
      // Keep last good project if project data is temporarily null during polling
      if (proj) {
        project = proj;
      }
      const runList = (runsData as any)?.runs ?? runsData ?? [];
      if (Array.isArray(runList)) {
        runs = runList as Record<string, unknown>[];
      }
      // The endpoint returns an EMPTY CheckpointResponse ({checkpoint: null,
      // ...}) when nothing is pending — truthy as an object, which rendered a
      // blank "pending checkpoint" card on finished projects.
      checkpoint = (cpData && (cpData as Record<string, unknown>).checkpoint)
        ? cpData : null;
      error = null;
    } catch (err: unknown) {
      if (params.id !== pid) return;
      const status = (err as any)?.status;
      if (status === 404) {
        // Project not found — redirect to dashboard
        push('#/');
        return;
      }
      error = err instanceof Error ? err.message : 'Failed to load project data';
    } finally {
      if (params.id === pid) {
        loading = false;
        isRefreshing = false;
      }
    }
  }

  async function loadRunDetail(runId: string): Promise<void> {
    if (selectedRunId === runId && runDetail) {
      // Deselect
      selectedRunId = null;
      runDetail = null;
      return;
    }
    selectedRunId = runId;
    runDetail = null;
    try {
      const detail = await getRunDetail(runId);
      if (selectedRunId === runId) {
        runDetail = detail;
      }
    } catch (err: unknown) {
      if (selectedRunId === runId) {
        error = err instanceof Error ? err.message : 'Failed to load run detail';
      }
    }
  }

  // ── Action handlers ──

  async function handleRetry(): Promise<void> {
    const pid = params.id;
    if (!pid || actionLoading['retry']) return;
    actionLoading = { ...actionLoading, retry: true };
    try {
      await retryProject(pid);
    } catch (err: unknown) {
      error = err instanceof Error ? err.message : 'Failed to retry project';
    } finally {
      actionLoading = { ...actionLoading, retry: false };
      await refreshData();
    }
  }

  async function handleRefreshPlanning(): Promise<void> {
    const pid = params.id;
    if (!pid || actionLoading['refresh']) return;
    actionLoading = { ...actionLoading, refresh: true };
    try {
      await refreshPlanning(pid);
    } catch (err: unknown) {
      error = err instanceof Error ? err.message : 'Failed to refresh planning';
    } finally {
      actionLoading = { ...actionLoading, refresh: false };
      await refreshData();
    }
  }

  async function handlePauseResume(): Promise<void> {
    const pid = params.id;
    if (!pid || actionLoading['pause']) return;
    actionLoading = { ...actionLoading, pause: true };
    try {
      const currentStatus = (project?.status as string) || '';
      if (currentStatus === 'paused') {
        await patchProject(pid, { status: 'running' });
      } else {
        await patchProject(pid, { status: 'paused' });
      }
    } catch (err: unknown) {
      error = err instanceof Error ? err.message : 'Failed to update project status';
    } finally {
      actionLoading = { ...actionLoading, pause: false };
      await refreshData();
    }
  }

  function handleViewTraces(): void {
    const pid = params.id;
    if (!pid) return;
    push('#/projects/' + encodeURIComponent(pid) + '/trace');
  }

  function navigateToChat(): void {
    const pid = params.id;
    if (!pid) return;
    push('#/projects/' + encodeURIComponent(pid) + '/chat');
  }

  function navigateToTraceForRun(runId: string): void {
    const pid = params.id;
    if (!pid) return;
    push('#/projects/' + encodeURIComponent(pid) + '/trace/' + encodeURIComponent(runId));
  }

  function toggleStepExpanded(stepId: string): void {
    expandedSteps = { ...expandedSteps, [stepId]: !expandedSteps[stepId] };
  }

  // ── Checkpoint handlers ──

  async function handleCheckpointApprove(): Promise<void> {
    const pid = params.id;
    const cp = checkpoint as Record<string, unknown> | null;
    if (!pid || !cp || actionLoading['cpApprove']) return;
    actionLoading = { ...actionLoading, cpApprove: true };
    try {
      await approveCheckpoint(pid, checkpointFeedback || undefined);
      checkpoint = null;
    } catch (err: unknown) {
      error = err instanceof Error ? err.message : 'Failed to approve checkpoint';
    } finally {
      actionLoading = { ...actionLoading, cpApprove: false };
      await refreshData();
    }
  }

  async function handleCheckpointReject(): Promise<void> {
    const pid = params.id;
    const cp = checkpoint as Record<string, unknown> | null;
    if (!pid || !cp || actionLoading['cpReject']) return;
    const feedback = checkpointFeedback || '';
    if (!feedback.trim()) {
      error = 'Feedback is required when rejecting a checkpoint.';
      return;
    }
    actionLoading = { ...actionLoading, cpReject: true };
    try {
      await rejectCheckpoint(pid, feedback);
      checkpoint = null;
    } catch (err: unknown) {
      error = err instanceof Error ? err.message : 'Failed to reject checkpoint';
    } finally {
      actionLoading = { ...actionLoading, cpReject: false };
      await refreshData();
    }
  }

  // ── Config editing ──

  function startConfigEdit(): void {
    if (!project) return;
    configFormData = {
      name: (project.name as string) || '',
      brief: (project.brief as string) || '',
    };
    isEditingConfig = true;
    configError = null;
  }

  function cancelConfigEdit(): void {
    isEditingConfig = false;
    configFormData = {};
    configError = null;
  }

  async function saveConfig(): Promise<void> {
    const pid = params.id;
    if (!pid || configSaving) return;
    configSaving = true;
    configError = null;
    try {
      await patchProject(pid, configFormData);
      isEditingConfig = false;
    } catch (err: unknown) {
      configError = err instanceof Error ? err.message : 'Failed to save config';
    } finally {
      configSaving = false;
      await refreshData();
    }
  }

  // ── Helpers for template ──

  function projectStatusParsed(): { text: string; className: string; icon: string } {
    if (!project) return { text: '', className: '', icon: '?' };
    const status = (project.status as string) || '';
    return parseStatus(status);
  }

  function runDisplayId(run: Record<string, unknown>): string {
    return truncate((run.run_id as string) || (run.id as string) || '', 12);
  }

  function runDuration(run: Record<string, unknown>): string {
    const created = run.created_at as number | undefined;
    const updated = (run.updated_at as number) || (run.completed_at as number) || undefined;
    if (!created || !updated) return '';
    const secs = Math.max(0, updated - created);
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
    return Math.floor(secs / 3600) + 'h ' + Math.floor((secs % 3600) / 60) + 'm';
  }

  function stepDuration(step: Record<string, unknown>): string {
    const created = step.created_at as number | undefined;
    const updated = (step.updated_at as number) || undefined;
    if (!created) return '';
    if (!updated) return '…';
    const secs = Math.max(0, updated - created);
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
    return Math.floor(secs / 3600) + 'h ' + Math.floor((secs % 3600) / 60) + 'm';
  }

  function stepStatusLabel(step: Record<string, unknown>): string {
    const st = (step.status as string) || '';
    const parsed = parseStatus(st);
    return parsed.text || st;
  }

  function isStepExpanded(stepId: string): boolean {
    return !!expandedSteps[stepId];
  }
</script>

<section id="view-project">
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

  <!-- Breadcrumb -->
  <nav class="breadcrumb" aria-label="breadcrumb">
    <a href="#/" onclick={(e) => { e.preventDefault(); push('#/'); }}>Dashboard</a>
    <span class="breadcrumb-sep">/</span>
    <span class="breadcrumb-current">{projectId ? truncate(projectId, 24) : '…'}</span>
  </nav>

  <!-- Loading state (initial) -->
  {#if loading && !project}
    <article aria-busy="true">
      <p>Loading project…</p>
    </article>
  {:else if error && !project}
    <!-- Error (full page — no project loaded) -->
    <article class="error-state">
      <header>
        <h3>Failed to load project</h3>
      </header>
      <p>{error}</p>
      <button onclick={refreshData}>Retry</button>
    </article>
  {:else}
    <!-- ══════════════════════════════════════════
         Project Info Card
         ══════════════════════════════════════════ -->
    <article id="project-info-card">
      <header class="flex-between">
        <div class="flex-row gap-1">
          <h3 class="project-name">{project?.name || projectId}</h3>
          {#if project?.status}
            {@const parsed = projectStatusParsed()}
            <span class="status-badge {parsed.className}" title={parsed.text}>
              {parsed.icon} {parsed.text}
            </span>
          {/if}
        </div>
      </header>

      <div class="project-meta">
        {#if project?.brief}
          <p class="project-brief">{project.brief as string}</p>
        {/if}
        <div class="meta-grid">
          <div class="meta-item">
            <span class="meta-label">ID</span>
            <span class="meta-value">{project?.project_id as string || projectId}</span>
          </div>
          {#if project?.created_at}
            <div class="meta-item">
              <span class="meta-label">Created</span>
              <span class="meta-value">{formatTime(project.created_at as number)}</span>
            </div>
          {/if}
          {#if project?.current_step}
            <div class="meta-item">
              <span class="meta-label">Current Step</span>
              <span class="meta-value">{stepLabel(project.current_step as string)}</span>
            </div>
          {/if}
          {#if project?.config_name}
            <div class="meta-item">
              <span class="meta-label">Config</span>
              <span class="meta-value">{project.config_name as string}</span>
            </div>
          {/if}
        </div>
      </div>

      <!-- Action buttons (write-gated) -->
      {#if canWrite}
        <footer class="action-bar">
          <button
            class="outline"
            onclick={handleRetry}
            disabled={actionLoading['retry']}
            title="Retry failed project"
          >
            {actionLoading['retry'] ? 'Retrying…' : 'Retry'}
          </button>
          <button
            class="outline"
            onclick={handleRefreshPlanning}
            disabled={actionLoading['refresh']}
            title="Re-run Researcher + Architect planning"
          >
            {actionLoading['refresh'] ? 'Refreshing…' : 'Refresh Planning'}
          </button>
          <button
            class="outline"
            onclick={handlePauseResume}
            disabled={actionLoading['pause']}
            title={project?.status === 'paused' ? 'Resume project' : 'Pause project'}
          >
            {#if actionLoading['pause']}
              {project?.status === 'paused' ? 'Resuming…' : 'Pausing…'}
            {:else if project?.status === 'paused'}
              Resume
            {:else}
              Pause
            {/if}
          </button>
          <button class="outline" onclick={handleViewTraces}>
            View Trace
          </button>
          <button class="outline" onclick={navigateToChat}>
            Chat
          </button>
        </footer>
      {/if}
    </article>

    <!-- ══════════════════════════════════════════
         Checkpoint Card (if pending)
         ══════════════════════════════════════════ -->
    {#if checkpoint}
      {@const cp = checkpoint as Record<string, unknown>}
      <article id="checkpoint-card" class="checkpoint-card">
        <header>
          <h4>Checkpoint Pending: {escapeHtml((cp.label as string) || (cp.checkpoint as string) || '')}</h4>
        </header>
        <div class="cp-content">
          {#if cp.step_output}
            <pre class="cp-output">{escapeHtml(JSON.stringify(cp.step_output, null, 2))}</pre>
          {/if}
          <label for="cp-feedback">
            Feedback (required for rejection)
            <textarea
              id="cp-feedback"
              bind:value={checkpointFeedback}
              rows="3"
              placeholder="Reason for rejection or approval notes…"
            ></textarea>
          </label>
        </div>
        {#if canWrite}
          <footer class="action-bar">
            <button
              class="primary"
              onclick={handleCheckpointApprove}
              disabled={actionLoading['cpApprove']}
            >
              {actionLoading['cpApprove'] ? 'Approving…' : 'Approve'}
            </button>
            <button
              class="contrast"
              onclick={handleCheckpointReject}
              disabled={actionLoading['cpReject']}
            >
              {actionLoading['cpReject'] ? 'Rejecting…' : 'Reject'}
            </button>
          </footer>
        {/if}
      </article>
    {/if}

    <!-- ══════════════════════════════════════════
         Tab Bar
         ══════════════════════════════════════════ -->
    <nav class="tab-bar">
      <button
        class="tab-btn"
        class:active={activeTab === 'runs'}
        onclick={() => { activeTab = 'runs'; }}
      >
        Runs ({runs.length})
      </button>
      {#if canWrite}
        <button
          class="tab-btn"
          class:active={activeTab === 'config'}
          onclick={() => { activeTab = 'config'; }}
        >
          Config
        </button>
      {/if}
    </nav>

    <!-- ══════════════════════════════════════════
         Runs Tab
         ══════════════════════════════════════════ -->
    {#if activeTab === 'runs'}
      {#if runs.length === 0}
        <article class="empty-state">
          <p>No runs yet for this project.</p>
        </article>
      {:else}
        <div class="runs-layout">
          <!-- Run list table -->
          <figure class="run-list-section">
            <table class="run-table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Status</th>
                  <th>Steps</th>
                  <th>Duration</th>
                  <th>Updated</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {#each runs as run, runIdx ((run.id as string) || (run.run_id as string) || runIdx)}
                  {@const isSelected = selectedRunId === (run.id as string) || selectedRunId === (run.run_id as string)}
                  {@const parsed = parseStatus(run.status as string)}
                  <tr
                    class="run-row"
                    class:selected={isSelected}
                    onclick={() => loadRunDetail((run.id as string) || (run.run_id as string))}
                  >
                    <td>
                      <code>{runDisplayId(run)}</code>
                    </td>
                    <td>
                      <span class="status-badge {parsed.className}" title={parsed.text}>
                        {parsed.icon} {parsed.text}
                      </span>
                    </td>
                    <td>
                      <span class="step-progress">
                        {run.completed_steps as number || 0}/{(run.step_count as number) || (run.steps as any[])?.length || 0}
                      </span>
                    </td>
                    <td>
                      <span class="duration">{runDuration(run)}</span>
                    </td>
                    <td>
                      <span class="timestamp">{formatTime(run.updated_at as number ?? run.created_at as number)}</span>
                    </td>
                    <td>
                      <button
                        class="outline small"
                        onclick={(e) => { e.stopPropagation(); navigateToTraceForRun((run.id as string) || (run.run_id as string)); }}
                      >
                        Trace
                      </button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </figure>

          <!-- Run detail panel -->
          {#if selectedRunId && runDetail}
            <section class="run-detail-panel">
              <header class="flex-between">
                <h4>Run Detail</h4>
                <button class="outline small" onclick={() => { selectedRunId = null; runDetail = null; }}>
                  ✕
                </button>
              </header>

              <!-- Summary -->
              <div class="run-summary">
                <div class="meta-grid">
                  <div class="meta-item">
                    <span class="meta-label">Run ID</span>
                    <span class="meta-value"><code>{truncate((runDetail.run_id as string) || (runDetail.id as string) || '', 16)}</code></span>
                  </div>
                  {#if runDetail.status}
                    {@const parsed = parseStatus(runDetail.status as string)}
                    <div class="meta-item">
                      <span class="meta-label">Status</span>
                      <span class="status-badge {parsed.className}">{parsed.icon} {parsed.text}</span>
                    </div>
                  {/if}
                  {#if runDetail.created_at}
                    <div class="meta-item">
                      <span class="meta-label">Started</span>
                      <span class="meta-value">{formatTime(runDetail.created_at as number)}</span>
                    </div>
                  {/if}
                  {#if runDetail.config_name}
                    <div class="meta-item">
                      <span class="meta-label">Config</span>
                      <span class="meta-value">{runDetail.config_name as string}</span>
                    </div>
                  {/if}
                </div>

                <!-- Cache stats -->
                {#if runDetail.cache_stats}
                  {@const cs = runDetail.cache_stats as Record<string, unknown>}
                  <div class="cache-stats">
                    <span class="meta-label">Cache:</span>
                    <span>{(cs.hit_ratio as number != null) ? Math.round((cs.hit_ratio as number) * 100) + '%' : '—'}</span>
                    <span class="text-muted">({formatTokens(cs.cache_hit_tokens as number)} hit / {formatTokens(cs.cache_miss_tokens as number)} miss)</span>
                  </div>
                {/if}
              </div>

              <!-- Navigation links -->
              <nav class="run-nav">
                <button class="outline small" onclick={navigateToChat}>Chat</button>
                <button class="outline small" onclick={() => navigateToTraceForRun((runDetail.id as string) || (runDetail.run_id as string))}>Trace</button>
              </nav>

              <!-- Step timeline -->
              <div class="step-timeline">
                <h5>Steps</h5>
                {#if runDetail.steps}
                  {#each runDetail.steps as step, stepIdx ((step.id as number) ?? stepIdx)}
                    <div class="step-item" class:step-expanded={isStepExpanded(step.step_id as string)}>
                      <div
                        class="step-header"
                        onclick={() => toggleStepExpanded(step.step_id as string)}
                        role="button"
                        tabindex="0"
                        onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleStepExpanded(step.step_id as string); } }}
                      >
                        <span class="step-toggle">{isStepExpanded(step.step_id as string) ? '▼' : '▶'}</span>
                        <span class="step-label">{stepLabel(step.step_id as string) || (step.step_id as string)}</span>
                        <span class="status-badge {statusClass(step.status as string)}">
                          {statusIcon(step.status as string)} {stepStatusLabel(step)}
                        </span>
                        <span class="step-duration">{stepDuration(step)}</span>
                        {#if (step.attempt as number) > 1}
                          <span class="retry-badge" title="Retry count">↻ {(step.attempt as number) - 1}</span>
                        {/if}
                      </div>
                      {#if isStepExpanded(step.step_id as string)}
                        <div class="step-detail">
                          {#if step.error}
                            <div class="step-error">
                              <strong>Error:</strong>
                              <pre>{escapeHtml(step.error as string)}</pre>
                            </div>
                          {/if}
                          {#if step.cache_stats}
                            {@const scs = step.cache_stats as Record<string, unknown>}
                            <div class="step-cache">
                              <span class="text-muted">Tokens: {formatTokens(scs.cache_hit_tokens as number)} hit / {formatTokens(scs.cache_miss_tokens as number)} miss</span>
                            </div>
                          {/if}
                          <div class="step-meta text-small text-muted">
                            {#if step.created_at}
                              Started: {formatTime(step.created_at as number)}
                            {/if}
                            {#if step.retry_count != null}
                              · Retries: {step.retry_count as number}
                            {/if}
                          </div>
                        </div>
                      {/if}
                    </div>
                  {/each}
                {:else}
                  <p class="text-muted">No step data available.</p>
                {/if}
              </div>
            </section>
          {/if}
        </div>
      {/if}
    {/if}

    <!-- ══════════════════════════════════════════
         Config Tab (write-gated)
         ══════════════════════════════════════════ -->
    {#if activeTab === 'config' && canWrite}
      <section class="config-section">
        {#if isEditingConfig}
          <article class="config-form-card">
            <header>
              <h4>Edit Configuration</h4>
            </header>
            {#if configError}
              <p class="form-error-general">{configError}</p>
            {/if}
            <form onsubmit={(e) => { e.preventDefault(); saveConfig(); }}>
              <label for="edit-name">
                Display Name
                <input
                  id="edit-name"
                  type="text"
                  bind:value={configFormData.name}
                  disabled={configSaving}
                  placeholder="My Project"
                />
              </label>
              <label for="edit-brief">
                Brief
                <textarea
                  id="edit-brief"
                  bind:value={configFormData.brief}
                  disabled={configSaving}
                  rows="4"
                  placeholder="Project description…"
                ></textarea>
              </label>
              <div class="form-actions">
                <button type="submit" disabled={configSaving}>
                  {configSaving ? 'Saving…' : 'Save'}
                </button>
                <button type="button" class="secondary" onclick={cancelConfigEdit} disabled={configSaving}>
                  Cancel
                </button>
              </div>
            </form>
          </article>
        {:else}
          <article class="config-display-card">
            <header class="flex-between">
              <h4>Project Configuration</h4>
              <button class="outline small" onclick={startConfigEdit}>Edit</button>
            </header>
            <div class="config-fields">
              <div class="config-field">
                <span class="meta-label">Name</span>
                <span class="meta-value">{project?.name as string || '—'}</span>
              </div>
              <div class="config-field">
                <span class="meta-label">Brief</span>
                <p class="meta-value config-brief">{project?.brief as string || '—'}</p>
              </div>
              <div class="config-field">
                <span class="meta-label">Config</span>
                <span class="meta-value">{project?.config_name as string || '—'}</span>
              </div>
              <div class="config-field">
                <span class="meta-label">Priority</span>
                <span class="meta-value">{(project?.priority as number) ?? '—'}</span>
              </div>
            </div>
          </article>
        {/if}
      </section>
    {/if}

    <!-- Error toast (non-blocking) -->
    {#if error && project}
      <div class="error-toast">
        <span>{error}</span>
        <button class="small" onclick={() => { error = null; }}>✕</button>
      </div>
    {/if}
  {/if}
</section>

<style>
  /* ── Breadcrumb ── */
  .breadcrumb {
    margin-bottom: var(--pico-spacing, 1rem);
    font-size: 0.9rem;
  }
  .breadcrumb a {
    color: var(--pico-primary, #06c);
    text-decoration: none;
  }
  .breadcrumb a:hover {
    text-decoration: underline;
  }
  .breadcrumb-sep {
    margin: 0 0.4rem;
    color: var(--pico-muted-color, #888);
  }
  .breadcrumb-current {
    color: var(--pico-muted-color, #888);
  }

  /* ── Project info card ── */
  #project-info-card {
    margin-bottom: var(--pico-spacing, 1rem);
  }
  .project-name {
    margin: 0;
  }
  .project-meta {
    padding: 0.5rem 0;
  }
  .project-brief {
    color: var(--pico-muted-color, #666);
    margin-bottom: 0.75rem;
  }
  .meta-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 0.5rem;
  }
  .meta-item {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  .meta-label {
    font-size: 0.75rem;
    color: var(--pico-muted-color, #888);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .meta-value {
    font-size: 0.9rem;
  }
  .action-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    padding-top: 0.5rem;
  }
  .action-bar button {
    font-size: 0.85rem;
  }

  /* ── Checkpoint card ── */
  .checkpoint-card {
    border: 2px solid var(--ait-color-checkpoint, #40b0e0);
    margin-bottom: var(--pico-spacing, 1rem);
  }
  .checkpoint-card header h4 {
    margin: 0;
    color: var(--ait-color-checkpoint, #40b0e0);
  }
  .cp-content {
    max-height: 300px;
    overflow-y: auto;
  }
  .cp-output {
    font-size: 0.8rem;
    background: var(--pico-code-background, #f5f5f5);
    padding: 0.5rem;
    border-radius: var(--pico-border-radius, 4px);
    max-height: 200px;
    overflow: auto;
  }

  /* ── Tab bar ── */
  .tab-bar {
    display: flex;
    gap: 0;
    border-bottom: 2px solid var(--pico-muted-border-color, #ddd);
    margin-bottom: var(--pico-spacing, 1rem);
  }
  .tab-btn {
    background: none;
    border: none;
    padding: 0.5rem 1rem;
    cursor: pointer;
    font-size: 0.9rem;
    color: var(--pico-muted-color, #888);
    border-bottom: 2px solid transparent;
    margin-bottom: -2px;
    transition: color 0.15s, border-color 0.15s;
  }
  .tab-btn:hover {
    color: var(--pico-primary, #06c);
  }
  .tab-btn.active {
    color: var(--pico-primary, #06c);
    border-bottom-color: var(--pico-primary, #06c);
    font-weight: 600;
  }

  /* ── Runs layout ── */
  .runs-layout {
    display: flex;
    flex-direction: column;
    gap: var(--pico-spacing, 1rem);
  }
  .run-list-section {
    margin: 0;
  }
  .run-table {
    width: 100%;
    border-collapse: collapse;
  }
  .run-table th,
  .run-table td {
    padding: 0.4rem 0.5rem;
    text-align: left;
    border-bottom: 1px solid var(--pico-muted-border-color, #eee);
  }
  .run-table th {
    font-size: 0.75rem;
    text-transform: uppercase;
    color: var(--pico-muted-color, #888);
    font-weight: 600;
  }
  .run-row {
    cursor: pointer;
    transition: background 0.15s;
  }
  .run-row:hover {
    background: var(--pico-table-row-hover-background, rgba(128, 128, 128, 0.05));
  }
  .run-row.selected {
    background: var(--pico-primary-background, rgba(0, 102, 204, 0.08));
  }
  .run-row td code {
    font-size: 0.85rem;
  }
  .step-progress {
    font-variant-numeric: tabular-nums;
    font-size: 0.85rem;
  }
  .duration {
    font-size: 0.85rem;
    color: var(--pico-muted-color, #888);
  }
  button.small {
    font-size: 0.8rem;
    padding: 0.2rem 0.5rem;
  }

  /* ── Run detail panel ── */
  .run-detail-panel {
    background: var(--pico-card-background, #fff);
    border: 1px solid var(--pico-muted-border-color, #ddd);
    border-radius: var(--pico-border-radius, 4px);
    padding: 1rem;
  }
  .run-detail-panel header h4 {
    margin: 0;
  }
  .run-summary {
    margin: 0.5rem 0;
  }
  .cache-stats {
    margin-top: 0.5rem;
    font-size: 0.85rem;
    display: flex;
    gap: 0.4rem;
    align-items: center;
  }
  .run-nav {
    display: flex;
    gap: 0.5rem;
    margin: 0.75rem 0;
  }

  /* ── Step timeline ── */
  .step-timeline {
    margin-top: 0.5rem;
  }
  .step-timeline h5 {
    margin: 0 0 0.5rem 0;
    font-size: 0.95rem;
  }
  .step-item {
    border: 1px solid var(--pico-muted-border-color, #eee);
    border-radius: var(--pico-border-radius, 4px);
    margin-bottom: 0.3rem;
    overflow: hidden;
  }
  .step-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0.5rem;
    cursor: pointer;
    font-size: 0.85rem;
    background: var(--pico-surface, #fafafa);
    transition: background 0.15s;
  }
  .step-header:hover {
    background: var(--pico-table-row-hover-background, rgba(128, 128, 128, 0.05));
  }
  .step-toggle {
    font-size: 0.7rem;
    width: 1rem;
    text-align: center;
    color: var(--pico-muted-color, #888);
  }
  .step-label {
    font-weight: 600;
    flex: 1;
  }
  .step-duration {
    font-size: 0.8rem;
    color: var(--pico-muted-color, #888);
    white-space: nowrap;
  }
  .retry-badge {
    font-size: 0.75rem;
    background: var(--pico-color-yellow-100, #ffe);
    color: var(--pico-color-yellow-700, #960);
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
  }
  .step-detail {
    padding: 0.5rem;
    border-top: 1px solid var(--pico-muted-border-color, #eee);
    font-size: 0.85rem;
  }
  .step-error {
    margin-bottom: 0.5rem;
  }
  .step-error pre {
    font-size: 0.8rem;
    background: var(--pico-color-red-50, #fee);
    padding: 0.5rem;
    border-radius: var(--pico-border-radius, 4px);
    max-height: 150px;
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .step-cache {
    margin: 0.25rem 0;
  }
  .step-meta {
    margin-top: 0.25rem;
  }

  /* ── Config section ── */
  .config-section {
    max-width: 600px;
  }
  .config-form-card,
  .config-display-card {
    margin-bottom: var(--pico-spacing, 1rem);
  }
  .config-form-card .form-actions {
    display: flex;
    gap: 0.5rem;
    margin-top: var(--pico-spacing, 1rem);
  }
  .config-fields {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }
  .config-field {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  .config-brief {
    margin: 0;
  }

  /* ── Error toast ── */
  .error-toast {
    position: fixed;
    bottom: 1rem;
    right: 1rem;
    background: var(--pico-color-red-100, #fee);
    border: 1px solid var(--pico-color-red-500, #c00);
    border-radius: var(--pico-border-radius, 4px);
    padding: 0.5rem 1rem;
    font-size: 0.85rem;
    display: flex;
    align-items: center;
    gap: 0.75rem;
    z-index: 500;
    max-width: 400px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
  }
  .error-toast button {
    background: none;
    border: none;
    cursor: pointer;
    color: var(--pico-color-red-500, #c00);
    font-size: 1rem;
    padding: 0;
    line-height: 1;
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

  /* ── Empty / error states ── */
  .empty-state {
    text-align: center;
    padding: 2rem 1rem;
  }
  .error-state {
    text-align: center;
    padding: 2rem 1rem;
  }

  /* ── Utility overrides ── */
  .text-muted {
    color: var(--pico-muted-color, #888);
  }
  .text-small {
    font-size: 0.85rem;
  }
  .flex-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .flex-between {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .gap-1 {
    gap: 1rem;
  }
</style>
