<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { push } from 'svelte-spa-router';
  import { getTrace } from '../lib/api';
  import { stepLabel } from '../lib/format';

  // ── Props ──────────────────────────────────────────────────────────

  let { params = {} as Record<string, string> } = $props();

  // ── State (runes) ──────────────────────────────────────────────────

  let projectId = $derived(params.id || '');
  // No explicit runId (project-level "View Traces") → target the project id:
  // the backend's _resolve_run accepts project ids as run identifiers.
  let runId = $derived(params.runId || params.id || '');

  let traces = $state<any[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let hasMore = $state(false);
  let nextSeq = $state<number | null>(null);
  let loadedCount = $state(0);

  // Filter / order controls
  let category = $state('');
  let order = $state<'asc' | 'desc'>('desc');
  let loadingMore = $state(false);

  // Expanded entries (keyed by trace seq)
  let expandedSeqs = $state<Set<number>>(new Set());

  // Categories
  const CATEGORIES = ['', 'prompt', 'response', 'tool_call', 'tool_result',
    'usage', 'step', 'lifecycle', 'error'];

  // Derived
  let empty = $derived(!loading && !error && traces.length === 0);

  // ── Helpers ────────────────────────────────────────────────────────

  function extractPayloadText(payload: any): string {
    if (payload == null) return '';
    if (typeof payload === 'string') return payload;
    if (typeof payload === 'object') {
      const hasToolCalls = Array.isArray(payload.tool_calls) && payload.tool_calls.length;
      if (hasToolCalls || payload.reasoning_content) {
        const parts: string[] = [];
        if (payload.text) parts.push(payload.text);
        if (payload.reasoning_content) parts.push('[reasoning]\n' + payload.reasoning_content);
        if (hasToolCalls) {
          payload.tool_calls.forEach((tc: any, i: number) => {
            let name: string, args: string;
            if (typeof tc === 'string') {
              name = tc;
              args = (Array.isArray(payload.tool_args) && payload.tool_args[i]) || '';
            } else {
              name = tc.name;
              args = tc.arguments || '';
            }
            parts.push('→ ' + name + '(' + args + ')');
          });
        }
        return parts.join('\n\n');
      }
      const direct = payload.content || payload.text || payload.message ||
        payload.response || payload.prompt || payload.error;
      if (typeof direct === 'string' && direct) return direct;
      try { return JSON.stringify(payload, null, 2); } catch { return String(payload); }
    }
    return String(payload);
  }

  function shortTime(ts: string | undefined | null): string {
    if (!ts) return '';
    const m = String(ts).match(/(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : String(ts);
  }

  // ── Data fetching ──────────────────────────────────────────────────

  async function loadTrace(): Promise<void> {
    if (!runId) {
      error = 'No run ID specified.';
      loading = false;
      return;
    }
    loading = true;
    error = null;
    traces = [];
    loadedCount = 0;
    nextSeq = null;
    hasMore = false;

    try {
      const data = await getTrace(runId, {
        limit: 100,
        order,
        ...(category ? { category } : {}),
      });
      traces = (data && data.traces) || [];
      loadedCount = traces.length;
      hasMore = !!(data && data.has_more);
      nextSeq = (data && data.next_seq != null) ? data.next_seq : null;
      // Honor server-reported order
      if (data && data.order && data.order !== order) {
        order = data.order as 'asc' | 'desc';
      }
    } catch (err: any) {
      error = err?.message || 'Failed to load trace.';
    } finally {
      loading = false;
    }
  }

  async function loadMore(): Promise<void> {
    if (loadingMore || !hasMore || !runId) return;
    loadingMore = true;
    try {
      const data = await getTrace(runId, {
        limit: 100,
        order,
        ...(category ? { category } : {}),
        ...(nextSeq != null ? { afterSeq: nextSeq } : {}),
      });
      const newTraces = (data && data.traces) || [];
      traces = [...traces, ...newTraces];
      loadedCount += newTraces.length;
      hasMore = !!(data && data.has_more);
      nextSeq = (data && data.next_seq != null) ? data.next_seq : null;
    } catch (err: any) {
      error = err?.message || 'Failed to load more.';
    } finally {
      loadingMore = false;
    }
  }

  function toggleEntry(seq: number): void {
    const next = new Set(expandedSeqs);
    if (next.has(seq)) {
      next.delete(seq);
    } else {
      next.add(seq);
    }
    expandedSeqs = next;
  }

  function onCategoryChange(e: Event): void {
    const val = (e.target as HTMLSelectElement).value;
    if (val === category) return;
    category = val;
    loadTrace();
  }

  function onOrderChange(e: Event): void {
    const val = (e.target as HTMLSelectElement).value as 'asc' | 'desc';
    if (val === order) return;
    order = val;
    loadTrace();
  }

  function goBack(): void {
    push('/projects/' + encodeURIComponent(projectId));
  }

  // ── Lifecycle ──────────────────────────────────────────────────────

  onMount(() => {
    loadTrace();
  });
</script>

<section id="view-trace" class="trace-view">
  <!-- Back link -->
  <a href="javascript:void(0)" onclick={goBack} class="trace-back">&larr; Back to project #{projectId}</a>

  <!-- Title -->
  <h3>Execution Trace &mdash; {projectId}</h3>

  <!-- Toolbar -->
  <div class="trace-toolbar">
    <label>
      Category:
      <select onchange={onCategoryChange}>
        {#each CATEGORIES as cat}
          <option value={cat} selected={cat === category}>
            {cat === '' ? 'All' : cat}
          </option>
        {/each}
      </select>
    </label>

    <label>
      Order:
      <select onchange={onOrderChange}>
        <option value="asc" selected={order === 'asc'}>Oldest first</option>
        <option value="desc" selected={order === 'desc'}>Newest first</option>
      </select>
    </label>

    <button class="outline" onclick={loadTrace}>Refresh</button>
  </div>

  <!-- Loading state -->
  {#if loading}
    <p class="trace-count">Loading trace&hellip;</p>
  {:else if error}
    <p class="trace-count trace-error">Failed to load trace: {error}</p>
  {:else if empty}
    <p class="trace-count empty-state">
      {category ? 'No trace records for category "' + category + '".' : 'No trace records yet.'}
    </p>
  {:else}
    <!-- Count line -->
    <p class="trace-count">
      {loadedCount} record(s) loaded, {order === 'desc' ? 'newest first' : 'oldest first'}
      {hasMore ? ' &middot; more available' : ' &middot; end of trace'}
      &middot; click a row to expand
    </p>

    <!-- Trace entry list -->
    <div class="trace-list">
      <!-- Keyed by POSITION, not seq: the API can return duplicate seq values
           (runs with retry/reclaim history), and a duplicate key in a keyed
           each is a FATAL error that froze the whole app on "Loading". -->
      {#each traces as entry, entryIdx (entryIdx)}
        {@const seq = entry.seq as number}
        {@const cat = (entry.category as string) || 'step'}
        {@const isExpanded = expandedSeqs.has(seq)}
        <div class="trace-entry cat-{cat}">
          <div
            class="trace-head"
            role="button"
            tabindex="0"
            onclick={() => toggleEntry(seq)}
            onkeypress={(e) => { if (e.key === 'Enter' || e.key === ' ') toggleEntry(seq); }}
          >
            <span class="trace-cat">{cat}</span>
            <span class="trace-step">{stepLabel(entry.step_id as string)}</span>
            <span class="trace-event">{(entry.event as string) || ''}</span>
            <span class="trace-time">{shortTime(entry.created_at as string)}</span>
          </div>
          {#if isExpanded}
            <pre class="trace-body">{extractPayloadText(entry.payload)}</pre>
          {/if}
        </div>
      {/each}
    </div>

    <!-- Load more footer -->
    <div class="trace-footer">
      {#if hasMore}
        <button
          class="outline"
          onclick={loadMore}
          disabled={loadingMore}
        >
          {loadingMore ? 'Loading&hellip;' : 'Load more'}
        </button>
      {/if}
    </div>
  {/if}
</section>

<style>
  .trace-view {
    padding: 1rem;
  }
  .trace-back {
    display: inline-block;
    margin-bottom: 0.75rem;
    font-size: 0.9rem;
  }
  .trace-toolbar {
    display: flex;
    gap: 1rem;
    align-items: center;
    margin-bottom: 0.75rem;
    flex-wrap: wrap;
  }
  .trace-toolbar label {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.85rem;
    margin: 0;
  }
  .trace-toolbar select {
    font-size: 0.85rem;
    padding: 0.2rem 0.4rem;
  }
  .trace-toolbar button {
    font-size: 0.85rem;
    padding: 0.2rem 0.6rem;
  }
  .trace-count {
    font-size: 0.85rem;
    color: #666;
    margin: 0 0 0.5rem 0;
  }
  .trace-error {
    color: #b00;
  }
  .trace-list {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .trace-entry {
    border: 1px solid #ddd;
    border-radius: 4px;
    overflow: hidden;
  }
  .trace-head {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    padding: 0.4rem 0.6rem;
    cursor: pointer;
    user-select: none;
    font-size: 0.85rem;
    background: #f8f8f8;
  }
  .trace-head:hover {
    background: #eee;
  }
  .trace-cat {
    display: inline-block;
    font-size: 0.75rem;
    font-weight: 600;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    background: #ddd;
    color: #333;
    min-width: 5em;
    text-align: center;
  }
  .trace-step {
    font-weight: 500;
    color: #333;
    flex: 1;
  }
  .trace-event {
    color: #555;
    font-family: monospace;
    font-size: 0.8rem;
  }
  .trace-time {
    color: #888;
    font-family: monospace;
    font-size: 0.8rem;
    white-space: nowrap;
  }
  .trace-body {
    margin: 0;
    padding: 0.6rem;
    font-family: monospace;
    font-size: 0.8rem;
    white-space: pre-wrap;
    word-break: break-word;
    background: #fff;
    border-top: 1px solid #eee;
    max-height: 300px;
    overflow-y: auto;
  }
  .trace-footer {
    margin: 0.75rem 0;
    text-align: center;
  }
  .trace-footer button {
    font-size: 0.85rem;
    padding: 0.3rem 1rem;
  }
  /* Per-category badge colors */
  :global(.cat-prompt) .trace-cat { background: #d4edda; color: #155724; }
  :global(.cat-response) .trace-cat { background: #d1ecf1; color: #0c5460; }
  :global(.cat-tool_call) .trace-cat { background: #fff3cd; color: #856404; }
  :global(.cat-tool_result) .trace-cat { background: #e2d1f1; color: #563d7c; }
  :global(.cat-usage) .trace-cat { background: #f8d7da; color: #721c24; }
  :global(.cat-error) .trace-cat { background: #f5c6cb; color: #721c24; }
  :global(.cat-step) .trace-cat { background: #cce5ff; color: #004085; }
  :global(.cat-lifecycle) .trace-cat { background: #e2e3e5; color: #383d41; }
</style>
