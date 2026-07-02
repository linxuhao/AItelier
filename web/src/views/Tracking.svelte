<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { push } from 'svelte-spa-router';
  import { authStore } from '../stores/auth';
  import { listAllRuns } from '../lib/api';
  import { on, off } from '../lib/sse';
  import { formatTime, statusClass, stepLabel, parseStatus } from '../lib/format';

  // ── State (runes) ──────────────────────────────────────────────────

  let activeRuns = $state<Record<string, unknown>[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  // Auth-derived
  let canWrite = $derived($authStore?.canWrite ?? false);
  let permissionResolved = $derived($authStore?.permissionResolved ?? false);

  // Derived
  let denied = $derived(permissionResolved && !canWrite);
  let empty = $derived(!loading && !error && activeRuns.length === 0);
  let waitingAuth = $derived(!permissionResolved);

  // ── Data fetching ──────────────────────────────────────────────────

  async function fetchActiveRuns(): Promise<void> {
    if (!canWrite) return;
    loading = activeRuns.length === 0;
    error = null;
    try {
      const data = await listAllRuns('running');
      activeRuns = (data && data.runs) || [];
    } catch (err: any) {
      error = err?.message || 'Failed to load active runs.';
    } finally {
      loading = false;
    }
  }

  function handleRunStatus(event: Record<string, unknown>): void {
    if (!event) return;
    const projectId = event.project_id as string;
    const status = event.status as string;
    // If a run is no longer running, remove it from the list
    if (status && status.split(':')[0] !== 'running') {
      activeRuns = activeRuns.filter((r) => r.project_id !== projectId);
    } else if (projectId) {
      // Update existing run or add new one — refetch to get full data
      const existing = activeRuns.find((r) => r.project_id === projectId);
      if (existing) {
        // Update the status field in-place by replacing
        activeRuns = activeRuns.map((r) =>
          r.project_id === projectId ? { ...r, ...event } : r,
        );
      } else {
        // New run appeared — refetch
        fetchActiveRuns();
      }
    }
  }

  function refresh(): void {
    fetchActiveRuns();
  }

  function navigateToTrace(projectId: string, runId: string): void {
    push(
      '/projects/' + encodeURIComponent(projectId) +
      '/trace/' + encodeURIComponent(runId),
    );
  }

  function computeElapsed(createdAt: string | number | undefined | null): string {
    if (createdAt == null) return '';
    let ts: number;
    if (typeof createdAt === 'number') {
      ts = createdAt;
    } else {
      ts = new Date(createdAt).getTime() / 1000;
    }
    if (!ts || isNaN(ts)) return '';
    const now = Date.now() / 1000;
    const diff = Math.max(0, now - ts);
    if (diff < 60) return Math.floor(diff) + 's';
    if (diff < 3600) return Math.floor(diff / 60) + 'm';
    return Math.floor(diff / 3600) + 'h ' + Math.floor((diff % 3600) / 60) + 'm';
  }

  // ── Lifecycle ──────────────────────────────────────────────────────

  onMount(async () => {
    if (canWrite) {
      await fetchActiveRuns();
      // Poll every 10s as fallback
      pollTimer = setInterval(fetchActiveRuns, 10000);
      on('run_status', handleRunStatus);
    }
  });

  onDestroy(() => {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    off('run_status', handleRunStatus);
  });
</script>

<section id="view-tracking">
  {#if waitingAuth}
    <p class="tracking-loading">Checking permissions&hellip;</p>
  {:else if denied}
    <div class="tracking-denied">
      <h2>Access Denied</h2>
      <p>You need write access to view active run tracking.</p>
      <a href="#/">Go to Dashboard</a>
    </div>
  {:else}
    <!-- Header -->
    <div class="tracking-header">
      <h2>Active Runs</h2>
      <button class="outline" on:click={refresh} disabled={loading}>
        {loading ? 'Refreshing&hellip;' : 'Refresh'}
      </button>
    </div>

    <!-- Loading state -->
    {#if loading}
      <p class="tracking-loading">Loading active runs&hellip;</p>
    {:else if error}
      <p class="tracking-error">Failed to load active runs: {error}</p>
    {:else if empty}
      <p class="tracking-empty">No active runs.</p>
    {:else}
      <!-- Run cards -->
      <div class="run-cards">
        {#each activeRuns as run (run.project_id as string)}
          {@const statusStr = (run.status as string) || ''}
          {@const parsed = parseStatus(statusStr)}
          {@const createdAt = run.created_at as string | number | undefined}
          <div class="run-card">
            <div class="run-card-header">
              <span class="run-status-badge {parsed.className}">
                {parsed.icon} {parsed.text || statusStr}
              </span>
            </div>
            <div class="run-card-body">
              <div class="run-field">
                <span class="run-label">Project:</span>
                <span class="run-value">{run.project_id as string}</span>
              </div>
              <div class="run-field">
                <span class="run-label">Run:</span>
                <span class="run-value run-id">{run.project_id as string}</span>
              </div>
              {#if statusStr.includes(':')}
                <div class="run-field">
                  <span class="run-label">Step:</span>
                  <span class="run-value">{stepLabel(statusStr.split(':')[1])}</span>
                </div>
              {/if}
              {#if createdAt}
                <div class="run-field">
                  <span class="run-label">Elapsed:</span>
                  <span class="run-value">{computeElapsed(createdAt)}</span>
                </div>
              {/if}
            </div>
            <div class="run-card-actions">
              <button
                class="outline"
                on:click={() => navigateToTrace(run.project_id as string, run.project_id as string)}
              >
                View Trace
              </button>
            </div>
          </div>
        {/each}
      </div>
    {/if}
  {/if}
</section>

<style>
  .tracking-loading {
    padding: 2rem;
    text-align: center;
    color: #666;
  }
  .tracking-error {
    padding: 2rem;
    text-align: center;
    color: #b00;
  }
  .tracking-empty {
    padding: 2rem;
    text-align: center;
    color: #666;
  }
  .tracking-denied {
    padding: 2rem;
    text-align: center;
  }
  .tracking-denied h2 {
    color: #b00;
    margin-bottom: 0.5rem;
  }
  .tracking-denied a {
    display: inline-block;
    margin-top: 1rem;
  }
  .tracking-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1rem;
  }
  .tracking-header h2 {
    margin: 0;
  }
  .run-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 1rem;
  }
  .run-card {
    border: 1px solid #ddd;
    border-radius: 6px;
    overflow: hidden;
    background: #fff;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }
  .run-card-header {
    padding: 0.5rem 0.75rem;
    background: #f8f8f8;
    border-bottom: 1px solid #eee;
  }
  .run-status-badge {
    display: inline-block;
    font-size: 0.8rem;
    font-weight: 600;
    padding: 0.2rem 0.5rem;
    border-radius: 3px;
    background: #ddd;
    color: #333;
  }
  .run-status-badge.status-ok {
    background: #d4edda;
    color: #155724;
  }
  .run-status-badge.status-warn {
    background: #fff3cd;
    color: #856404;
  }
  .run-status-badge.status-err {
    background: #f5c6cb;
    color: #721c24;
  }
  .run-card-body {
    padding: 0.75rem;
  }
  .run-field {
    display: flex;
    justify-content: space-between;
    padding: 0.2rem 0;
    font-size: 0.85rem;
  }
  .run-label {
    color: #888;
    font-weight: 500;
  }
  .run-value {
    color: #333;
    font-family: monospace;
    font-size: 0.8rem;
    text-align: right;
    max-width: 60%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .run-id {
    font-family: monospace;
  }
  .run-card-actions {
    padding: 0.5rem 0.75rem;
    border-top: 1px solid #eee;
    text-align: right;
  }
  .run-card-actions button {
    font-size: 0.8rem;
    padding: 0.2rem 0.6rem;
  }
</style>
