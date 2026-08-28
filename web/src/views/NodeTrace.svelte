<script lang="ts">
  /**
   * One step instance's durable trace, read like a chat history.
   *
   * The full-page trace view answers "what happened in this run"; this answers
   * "what is this step doing right now", which is the question you have while
   * looking at the graph. Same records, filtered to ONE step instance -- not a
   * step_id, because a looped step has an instance per item and their records
   * interleave in the run-wide trace.
   *
   * Paging is tail-first on purpose: a step mid-run has its interesting end at
   * the END, so the first page is fetched newest-first and reversed, then live
   * updates append with an `after_seq` cursor. Loading from seq 0 forward would
   * open on the step's first prompt and need N pages to reach what is happening
   * now.
   */
  import { onDestroy } from 'svelte';
  import { getTrace } from '../lib/api';
  import { on, off } from '../lib/sse';
  import { formatTokens } from '../lib/format';
  import { extractPayloadText, shortTime, traceSummary, modelBinding } from '../lib/traceFormat';
  import { t } from '../lib/i18n.svelte';

  interface Props {
    /** Run id or project id — the backend resolves both. */
    runId: string;
    /** skillflow_steps.id of the instance to follow, or null for "nothing picked". */
    stepInstanceId: number | null;
    /** Poll for new records. False once the instance has finished. */
    live?: boolean;
  }
  const { runId, stepInstanceId, live = false }: Props = $props();

  const TAIL = 40;      // records on the first page
  const POLL_MS = 3000; // matches the run page's own refresh cadence

  let entries = $state<Record<string, any>[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let hasEarlier = $state(false);
  let loadingEarlier = $state(false);
  let expanded = $state<Set<number>>(new Set());

  // Cursors. maxSeq drives the live append, minSeq the "load earlier" page.
  let maxSeq: number | null = null;
  let minSeq: number | null = null;
  // Guards a slow response from landing after the selection moved on.
  let token = 0;

  let listEl = $state<HTMLDivElement | null>(null);
  let stickToBottom = true;

  function onScroll(): void {
    if (!listEl) return;
    const gap = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight;
    // Only auto-follow when the reader is already at the bottom; yanking the
    // view down while they are reading an older record is the thing every
    // auto-scrolling log does wrong.
    stickToBottom = gap < 40;
  }

  function scrollToBottom(): void {
    if (listEl && stickToBottom) listEl.scrollTop = listEl.scrollHeight;
  }

  async function loadTail(id: number, myToken: number): Promise<void> {
    loading = true;
    error = null;
    try {
      const data = await getTrace(runId, {
        stepInstanceId: id, order: 'desc', limit: TAIL,
      });
      if (myToken !== token) return;
      const rows = ((data?.traces ?? []) as Record<string, any>[]).slice().reverse();
      entries = rows;
      maxSeq = rows.length ? (rows[rows.length - 1].seq as number) : null;
      minSeq = rows.length ? (rows[0].seq as number) : null;
      hasEarlier = !!data?.has_more;
      stickToBottom = true;
      queueMicrotask(scrollToBottom);
    } catch (e) {
      if (myToken === token) error = e instanceof Error ? e.message : String(e);
    } finally {
      if (myToken === token) loading = false;
    }
  }

  async function pollNew(): Promise<void> {
    if (!live || stepInstanceId == null || loading) return;
    const myToken = token;
    try {
      const data = await getTrace(runId, {
        stepInstanceId, order: 'asc',
        ...(maxSeq != null ? { afterSeq: maxSeq } : {}),
        limit: 100,
      });
      if (myToken !== token) return;
      const rows = (data?.traces ?? []) as Record<string, any>[];
      if (!rows.length) return;
      entries = [...entries, ...rows];
      maxSeq = rows[rows.length - 1].seq as number;
      if (minSeq == null) minSeq = rows[0].seq as number;
      queueMicrotask(scrollToBottom);
    } catch {
      // A failed poll is not worth a visible error: the next tick retries, and
      // the run page's own poll already reports a backend that is truly down.
    }
  }

  async function loadEarlier(): Promise<void> {
    if (loadingEarlier || !hasEarlier || stepInstanceId == null) return;
    const myToken = token;
    loadingEarlier = true;
    try {
      const data = await getTrace(runId, {
        stepInstanceId, order: 'desc',
        ...(minSeq != null ? { afterSeq: minSeq } : {}),
        limit: TAIL,
      });
      if (myToken !== token) return;
      const rows = ((data?.traces ?? []) as Record<string, any>[]).slice().reverse();
      if (rows.length) {
        const keepTop = listEl?.scrollHeight ?? 0;
        entries = [...rows, ...entries];
        minSeq = rows[0].seq as number;
        // Keep the reader's place: prepending would otherwise scroll them up.
        queueMicrotask(() => {
          if (listEl) listEl.scrollTop += listEl.scrollHeight - keepTop;
        });
      }
      hasEarlier = !!data?.has_more;
    } finally {
      if (myToken === token) loadingEarlier = false;
    }
  }

  // Selection changed → drop everything and re-open on the new instance's tail.
  $effect(() => {
    const id = stepInstanceId;
    token += 1;
    const myToken = token;
    entries = [];
    expanded = new Set();
    maxSeq = null;
    minSeq = null;
    hasEarlier = false;
    error = null;
    if (id != null && runId) loadTail(id, myToken);
  });

  const timer = setInterval(pollNew, POLL_MS);

  // ── LLM liveness in the live footer ──
  // The trace only gains a row when a TURN finishes; during one completion
  // (up to minutes) it sits silent, which is indistinguishable from a hang.
  // llm_progress ticks (SSE, every ~3s while chunks arrive) fill exactly that
  // gap: chars growing = generating, chars flat = the trickle/hang class.
  // Matched on run alone — only one completion streams per run at a time, and
  // the footer only renders while following the running instance (live=true).
  let llmTick = $state<Record<string, unknown> | null>(null);
  let llmTimer: ReturnType<typeof setTimeout> | null = null;

  function onLlmProgress(ev: Record<string, unknown>): void {
    if (!live) return;
    // runId prop can be a run id OR a project id (the backend resolves both).
    if (ev.run_id !== runId && ev.project_id !== runId) return;
    const phase = (ev.phase as string) || 'llm';
    if (phase === 'llm_done' || phase === 'tool_done') {
      llmTick = null;
      if (llmTimer !== null) clearTimeout(llmTimer);
      llmTimer = null;
      return;
    }
    llmTick = ev;
    if (llmTimer !== null) clearTimeout(llmTimer);
    // Tool phase gets a longer leash: gate tools legitimately run minutes
    // and emit no ticks of their own while they do.
    llmTimer = setTimeout(() => { llmTick = null; },
                          phase === 'tool' ? 600000 : 15000);
  }
  on('llm_progress', onLlmProgress);

  onDestroy(() => {
    clearInterval(timer);
    off('llm_progress', onLlmProgress);
    if (llmTimer !== null) clearTimeout(llmTimer);
  });

  // The endpoint(s) that actually served this step instance, from the usage
  // rows currently loaded. More than one chip = the step failed over mid-run,
  // which is exactly the thing worth seeing at a glance.
  let stepBindings = $derived.by(() => {
    const seen: string[] = [];
    for (const e of entries) {
      const label = modelBinding((e as any).payload);
      if (label && !seen.includes(label)) seen.push(label);
    }
    return seen;
  });

  function toggle(seq: number): void {
    const next = new Set(expanded);
    if (next.has(seq)) next.delete(seq);
    else next.add(seq);
    expanded = next;
  }
</script>

<div class="nt">
  {#if stepInstanceId == null}
    <p class="nt-muted">{t('pipeline.tracePickNode')}</p>
  {:else if loading && entries.length === 0}
    <p class="nt-muted">{t('trace.loading')}</p>
  {:else if error}
    <p class="nt-error">{error}</p>
  {:else if entries.length === 0}
    <p class="nt-muted">{t('pipeline.traceEmpty')}</p>
  {:else}
    {#if stepBindings.length > 0}
      <div class="nt-bindings">
        {#each stepBindings as b (b)}
          <span class="nt-binding-chip">{b}</span>
        {/each}
      </div>
    {/if}
    <div class="nt-list" bind:this={listEl} onscroll={onScroll}>
      {#if hasEarlier}
        <button class="nt-earlier" onclick={loadEarlier} disabled={loadingEarlier}>
          {loadingEarlier ? t('trace.loading') : t('pipeline.traceEarlier')}
        </button>
      {/if}
      <!-- Keyed by POSITION: the trace API can hand back duplicate seq values
           on runs with retry/reclaim history, and a duplicate key in a keyed
           each is fatal in Svelte 5. -->
      {#each entries as e, i (i)}
        {@const cat = (e.category as string) || 'step'}
        {@const isOpen = expanded.has(e.seq as number)}
        <div class="nt-entry cat-{cat}" class:is-open={isOpen}>
          <button class="nt-head" onclick={() => toggle(e.seq as number)}>
            <span class="nt-cat">{cat}</span>
            <span class="nt-event">{(e.event as string) || ''}</span>
            <span class="nt-time">{shortTime(e.created_at as string)}</span>
          </button>
          {#if isOpen}
            <pre class="nt-body">{extractPayloadText(e.payload)}</pre>
          {:else}
            {@const line = traceSummary(e)}
            {#if line}<p class="nt-preview">{line}</p>{/if}
          {/if}
        </div>
      {/each}
      {#if live}
        <p class="nt-live">
          <span class="nt-dot"></span>{t('pipeline.traceLive')}
          {#if llmTick}
            {#if (llmTick.phase as string) === 'tool'}
              <span class="nt-llm">· {t('project.toolRunning')} · {llmTick.tool as string}</span>
            {:else}
              <span class="nt-llm">· {t('project.llmStreaming')} · {formatTokens(llmTick.chars as number)} chars · {llmTick.elapsed as number}s</span>
            {/if}
          {/if}
        </p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .nt-bindings {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    padding: 0.25rem 0;
  }
  .nt-binding-chip {
    font-family: var(--pico-font-family-monospace, monospace);
    font-size: 0.68rem;
    border: 1px solid var(--pico-muted-border-color, #ddd);
    border-radius: 999px;
    padding: 0 0.45rem;
    color: var(--pico-muted-color, #777);
    white-space: nowrap;
  }

  .nt { display: flex; flex-direction: column; min-height: 0; flex: 1; }
  .nt-list {
    /* Deliberately a BLOCK, not a flex column. As a flex column the entries
       shrink to fit the pane's height instead of overflowing it -- so the list
       never scrolled, it just squashed forty records into one crammed stack
       (each one clipped by its own `overflow: hidden`). A block box with
       overflow-y simply overflows, which is the whole point. */
    display: block;
    overflow-y: auto;
    flex: 1 1 auto;
    min-height: 0;
    padding-right: 0.2rem;
  }
  .nt-muted, .nt-error {
    font-size: 0.8rem;
    color: var(--pico-muted-color, #7a7a85);
    margin: 0.4rem 0;
  }
  .nt-error { color: var(--pico-del-color, #b3261e); }
  .nt-entry {
    margin-bottom: 0.25rem;
    border: 1px solid var(--pico-muted-border-color, #e0e0e0);
    border-radius: 4px;
    overflow: hidden;
    background: var(--pico-card-background-color, #fff);
  }
  .nt-head {
    display: flex;
    gap: 0.4rem;
    align-items: center;
    width: 100%;
    margin: 0;
    padding: 0.2rem 0.35rem;
    background: var(--pico-code-background-color, #f6f6f6);
    border: none;
    cursor: pointer;
    font-size: 0.72rem;
    color: inherit;
    text-align: left;
  }
  .nt-cat {
    font-weight: 600;
    padding: 0.02rem 0.3rem;
    border-radius: 3px;
    background: #ddd;
    color: #333;
    min-width: 5.2em;
    text-align: center;
    font-size: 0.68rem;
  }
  .nt-event {
    flex: 1;
    font-family: var(--pico-font-family-monospace, monospace);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .nt-time {
    font-family: var(--pico-font-family-monospace, monospace);
    color: var(--pico-muted-color, #8a8a8a);
  }
  .nt-preview {
    margin: 0;
    padding: 0.3rem 0.4rem;
    font-size: 0.72rem;
    line-height: 1.35;
    color: var(--pico-color, #33353c);
    /* Two lines is enough to recognise a record without turning the pane into
       a wall; the full payload is one click away. */
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .nt-body {
    margin: 0;
    padding: 0.4rem;
    font-size: 0.72rem;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 320px;
    overflow-y: auto;
    border-top: 1px solid var(--pico-muted-border-color, #eee);
  }
  .nt-earlier {
    display: block;
    width: auto;
    margin: 0 auto 0.35rem;
    padding: 0.1rem 0.6rem;
    font-size: 0.72rem;
    background: none;
    border: 1px solid var(--pico-muted-border-color, #ddd);
    border-radius: 999px;
    color: inherit;
    cursor: pointer;
  }
  .nt-live {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    margin: 0.25rem 0 0;
    font-size: 0.7rem;
    color: var(--pico-muted-color, #8a8a8a);
  }
  .nt-llm {
    font-variant-numeric: tabular-nums;
  }
  .nt-dot {
    width: 0.45rem;
    height: 0.45rem;
    border-radius: 50%;
    background: #2f7a45;
    animation: nt-pulse 1.6s ease-in-out infinite;
  }
  @keyframes nt-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.25; }
  }
  @media (prefers-reduced-motion: reduce) {
    .nt-dot { animation: none; }
  }

  /* The conversation — what was asked, what came back — carries a left bar, so
     it stands out from the tool chatter around it at a glance. */
  .cat-prompt { border-left: 3px solid #155724; }
  .cat-response { border-left: 3px solid #0c5460; }
  .cat-error { border-left: 3px solid #721c24; }

  /* Category badge colors — same vocabulary as the full trace view. */
  .cat-prompt .nt-cat { background: #d4edda; color: #155724; }
  .cat-response .nt-cat { background: #d1ecf1; color: #0c5460; }
  .cat-tool_call .nt-cat { background: #fff3cd; color: #856404; }
  .cat-tool_result .nt-cat { background: #e2d1f1; color: #563d7c; }
  .cat-usage .nt-cat { background: #f8d7da; color: #721c24; }
  .cat-error .nt-cat { background: #f5c6cb; color: #721c24; }
  .cat-step .nt-cat { background: #cce5ff; color: #004085; }
  .cat-lifecycle .nt-cat { background: #e2e3e5; color: #383d41; }
</style>
