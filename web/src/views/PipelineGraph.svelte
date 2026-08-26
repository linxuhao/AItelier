<script lang="ts">
  /**
   * Draw one pipeline's COMPOSED graph, optionally with a run folded onto it.
   *
   * Composed, because that is what runs: `dpe_game` is `dpe_default_v2` plus the
   * `game_harness` overlay, has no YAML of its own, and its spliced steps are
   * the whole reason to look at it. They are tinted, so "the pipeline" and "what
   * the addon added to it" are one picture instead of two.
   *
   * With `runSteps`, this replaces the run page's flat step list. The flat list
   * was unreadable exactly where a run is most interesting: inside a loop it
   * showed nine `t_impl` entries in one column with nothing to tell them apart.
   * Here the loop body sits in its own dashed box with an item picker, and the
   * body reports the selected item's state -- possible only since skillflow
   * stamps `loop_item` on each instance.
   *
   * Drawn by hand rather than with a graph library: these are 1-25 node graphs,
   * and the alternative was a megabyte of renderer for a panel that opens on
   * demand.
   */
  import { onMount } from 'svelte';
  import { pipelineGraph } from '../lib/api';
  import { layoutGraph, type Layout, type LaidOutNode } from '../lib/pipelineLayout';
  import {
    runGraphState, rowDuration, type RunStepRow, type RunGraphState,
  } from '../lib/runGraphState';
  import { formatTokens } from '../lib/format';
  import { t } from '../lib/i18n.svelte';
  import NodeTrace from './NodeTrace.svelte';

  interface Props {
    config: string;
    /** Step instances of ONE run. Omit for the plain config view. */
    runSteps?: RunStepRow[];
    /** step_id -> display label from the run's manifest (x-aitelier overrides). */
    labels?: Record<string, string>;
    /** step_id -> token / cache-hit stats, as /api/runs returns them. */
    cacheByStep?: Record<string, Record<string, number>>;
    /** Run (or project) id — required to read a node's trace. */
    runId?: string;
  }
  const { config, runSteps, labels, cacheByStep, runId }: Props = $props();

  const nodeLabel = (id: string): string => labels?.[id] || id;

  let error = $state<string | null>(null);
  let data = $state<Record<string, unknown> | null>(null);
  let layout = $state<Layout | null>(null);
  let selectedItem = $state<string | null>(null);
  let openNode = $state<string | null>(null);
  // Auto-follow the running node until the reader picks one themselves.
  // Following forever would yank the pane away mid-read on every advance.
  let userPicked = $state(false);
  let pickedInstance = $state<number | null>(null);
  let scrollEl = $state<HTMLDivElement | null>(null);

  // Geometry. Vertical: pipelines are long chains, so rank == row keeps the
  // drawing readable at panel width instead of scrolling sideways for 20 steps.
  const NODE_W = 170;
  const NODE_H = 32;
  const X_GAP = 20;
  const Y_GAP = 30;
  const PAD = 14;
  // Room on the right for the return arcs, so a loop never crosses a node box.
  const LOOP_LANE = 46;
  const BOX_INSET = 8;

  let loopOf = $derived(
    Object.fromEntries(
      Object.entries((data?.loops as Record<string, string[]>) ?? {})
        .flatMap(([lid, ids]) => ids.map((id) => [id, lid])),
    ) as Record<string, string>,
  );

  let runState = $derived<RunGraphState | null>(
    runSteps && layout
      ? runGraphState(layout.nodes.map((n) => n.id), runSteps, loopOf, selectedItem)
      : null,
  );

  let rankWidth = $derived((layout?.widest ?? 1) * NODE_W + ((layout?.widest ?? 1) - 1) * X_GAP);
  let width = $derived(PAD * 2 + rankWidth + LOOP_LANE);
  let height = $derived(PAD * 2 + (layout?.rankCount ?? 1) * NODE_H
    + Math.max(0, (layout?.rankCount ?? 1) - 1) * Y_GAP);

  function nodeX(n: LaidOutNode): number {
    const inRank = (layout?.nodes ?? []).filter((m) => m.rank === n.rank).length;
    const rowWidth = inRank * NODE_W + (inRank - 1) * X_GAP;
    return PAD + (rankWidth - rowWidth) / 2 + n.order * (NODE_W + X_GAP);
  }
  const nodeY = (n: LaidOutNode): number => PAD + n.rank * (NODE_H + Y_GAP);

  let positions = $derived(new Map(
    (layout?.nodes ?? []).map((n) => [n.id, { x: nodeX(n), y: nodeY(n), n }]),
  ));

  /** Dashed box around one loop's body, from the body nodes' own extent. */
  let loopBoxes = $derived(
    Object.entries((data?.loops as Record<string, string[]>) ?? {})
      .map(([lid, ids]) => {
        const pts = ids.map((id) => positions.get(id)).filter(Boolean) as
          Array<{ x: number; y: number }>;
        if (pts.length === 0) return null;
        const x = Math.min(...pts.map((p) => p.x)) - BOX_INSET;
        const y = Math.min(...pts.map((p) => p.y)) - BOX_INSET - 10;
        const x2 = Math.max(...pts.map((p) => p.x)) + NODE_W + BOX_INSET;
        const y2 = Math.max(...pts.map((p) => p.y)) + NODE_H + BOX_INSET;
        return { id: lid, x, y, w: x2 - x, h: y2 - y };
      })
      .filter(Boolean) as Array<{ id: string; x: number; y: number; w: number; h: number }>,
  );

  /** Forward edge: bottom of source to top of target, with a gentle S bend. */
  function forwardPath(from: string, to: string): string {
    const a = positions.get(from);
    const b = positions.get(to);
    if (!a || !b) return '';
    const x1 = a.x + NODE_W / 2, y1 = a.y + NODE_H;
    const x2 = b.x + NODE_W / 2, y2 = b.y;
    const mid = (y1 + y2) / 2;
    return `M ${x1} ${y1} C ${x1} ${mid}, ${x2} ${mid}, ${x2} ${y2}`;
  }

  /** Return edge: out the right side, up the loop lane, back in on the right. */
  function backPath(from: string, to: string): string {
    const a = positions.get(from);
    const b = positions.get(to);
    if (!a || !b) return '';
    const lane = PAD + rankWidth + LOOP_LANE / 2;
    const y1 = a.y + NODE_H / 2, y2 = b.y + NODE_H / 2;
    return `M ${a.x + NODE_W} ${y1} L ${lane} ${y1} L ${lane} ${y2} L ${b.x + NODE_W} ${y2}`;
  }

  function midpoint(from: string, to: string): { x: number; y: number } | null {
    const a = positions.get(from);
    const b = positions.get(to);
    if (!a || !b) return null;
    return { x: (a.x + b.x) / 2 + NODE_W / 2, y: (a.y + NODE_H + b.y) / 2 + 4 };
  }

  const statusOf = (id: string): string => runState?.byNode[id]?.status ?? '';

  /**
   * The node the run is ON right now.
   *
   * Before this, "which box is live" was a slightly thicker yellow border among
   * several yellow boxes -- on a 25-node graph scrolled to the top, the live
   * step was often not even on screen.
   */
  let currentNode = $derived(
    runState
      ? ((layout?.nodes ?? []).find(
          (n) => runState.byNode[n.id]?.status === 'running')?.id ?? null)
      : null,
  );

  /** Instances of the open node, and which ONE the trace pane is reading. */
  let activeRows = $derived<RunStepRow[]>(
    openNode && runState ? (runState.byNode[openNode]?.rows ?? []) : [],
  );
  let activeInstance = $derived<number | null>(
    pickedInstance != null && activeRows.some((r) => r.id === pickedInstance)
      ? pickedInstance
      // Default to the instance worth watching: the one still running, else the
      // most recent. A retried step's first attempt is not what you opened for.
      : (activeRows.find((r) => r.status === 'running' || r.status === 'claimed')?.id
         ?? activeRows[activeRows.length - 1]?.id ?? null),
  );
  let activeRow = $derived(activeRows.find((r) => r.id === activeInstance) ?? null);
  let activeLive = $derived(
    !!activeRow && (activeRow.status === 'running' || activeRow.status === 'claimed'),
  );

  // Follow the run until the reader takes over.
  $effect(() => {
    if (!userPicked && currentNode && openNode !== currentNode) {
      openNode = currentNode;
      pickedInstance = null;
    }
  });

  // ...and keep the followed node on screen. Only while following: scrolling a
  // reader's view away from the node they chose is the same rudeness as
  // switching the pane under them.
  $effect(() => {
    const id = currentNode;
    const el = scrollEl;
    if (!id || userPicked || !el) return;
    const p = positions.get(id);
    if (!p) return;
    const top = Math.max(0, p.y - el.clientHeight / 2 + NODE_H / 2);
    // Not every environment has the options form (jsdom has no scrollTo at
    // all), and failing to scroll must never take the graph down with it.
    if (typeof el.scrollTo === 'function') el.scrollTo({ top, behavior: 'smooth' });
    else el.scrollTop = top;
  });

  function selectNode(id: string): void {
    userPicked = true;
    if (openNode === id) {
      openNode = null;
    } else {
      openNode = id;
      pickedInstance = null;
    }
  }

  function pickItem(item: string | null) {
    selectedItem = selectedItem === item ? null : item;
  }

  function fmtDuration(ms: number | null): string {
    if (ms == null) return '—';
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
  }

  onMount(async () => {
    try {
      const r = await pipelineGraph(config);
      data = r as Record<string, unknown>;
      layout = layoutGraph((r.steps ?? []) as never[], (r.begin ?? '') as string);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  });
</script>

<div class="pg">
{#if error}
  <p class="graph-error">{error}</p>
{:else if !layout}
  <p class="graph-loading">{t('pipeline.graphLoading')}</p>
{:else}
  <div class="graph-meta">
    <span class="graph-legend"><i class="sw agent"></i>agent</span>
    <span class="graph-legend"><i class="sw tool"></i>tool</span>
    <span class="graph-legend"><i class="sw gate"></i>gate/loop</span>
    {#if (data?.addon_steps as string[] | undefined)?.length}
      <span class="graph-legend"><i class="sw addon"></i>{(data?.addons as string[]).join(', ')}</span>
    {/if}
    <span class="graph-legend"><i class="sw cp"></i>{t('pipeline.legendCheckpoint')}</span>
  </div>

  {#if runState}
    <!-- Item picker: the loop body below reports the selected task's state.
         Without a selection the body aggregates every item, where one failed
         task makes the node failed. -->
    {#if runState.items.length}
      <div class="item-strip">
        <span class="item-label">{t('pipeline.loopItems').replace('{n}', String(runState.items.length))}</span>
        <button class="item-chip" class:is-active={selectedItem === null}
                onclick={() => pickItem(null)}>{t('pipeline.allItems')}</button>
        {#each runState.items as it}
          <button class="item-chip" class:is-active={selectedItem === it}
                  onclick={() => pickItem(it)}>{it}</button>
        {/each}
      </div>
    {:else if runState.unattributed}
      <p class="item-note">{t('pipeline.itemsNotRecorded')}</p>
    {/if}
  {/if}

  <div class="pg-body">
  <div class="graph-scroll" bind:this={scrollEl}>
    <svg {width} {height} viewBox="0 0 {width} {height}" role="img"
         aria-label={t('pipeline.graphAria').replace('{name}', config)}>
      <defs>
        <marker id="pg-arrow" viewBox="0 0 8 8" refX="7" refY="4"
                markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 0 L 8 4 L 0 8 z" class="arrow-head" />
        </marker>
      </defs>

      <!-- Loop containers first, so they sit behind edges and nodes. -->
      {#each loopBoxes as box (box.id)}
        <g class="loop-box">
          <rect x={box.x} y={box.y} width={box.w} height={box.h} rx="8" />
          <text x={box.x + 6} y={box.y + 11}>
            {box.id}{selectedItem ? ' · ' + selectedItem : ''}
          </text>
        </g>
      {/each}

      {#each layout.edges as e (e.from + '>' + e.to)}
        <path d={e.back ? backPath(e.from, e.to) : forwardPath(e.from, e.to)}
              class="edge {e.back ? 'back' : ''}" marker-end="url(#pg-arrow)" />
        {#if !e.back && e.label}
          {@const m = midpoint(e.from, e.to)}
          {#if m}<text x={m.x} y={m.y} class="edge-label">{e.label}</text>{/if}
        {/if}
        {#if e.back && e.maxLoop}
          {@const a = positions.get(e.from)}
          {#if a}
            <text x={PAD + rankWidth + LOOP_LANE / 2 + 4} y={a.y + NODE_H / 2 - 4}
                  class="edge-label">x{e.maxLoop}</text>
          {/if}
        {/if}
      {/each}

      {#each layout.nodes as n (n.id)}
        {@const p = positions.get(n.id)}
        {#if p}
          {@const st = runState?.byNode[n.id]}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
          <!-- A node is focusable and Enter/Space-activatable ONLY in run mode,
               where it opens that step's instances. The compiler cannot see
               that `role` and `tabindex` are gated by the same condition, so it
               reads the <g> as non-interactive; the pair is always set or
               always absent together. -->
          <g class="node {n.type} {n.from_addon ? 'addon' : ''} {statusOf(n.id)}"
             class:is-open={openNode === n.id}
             class:is-current={currentNode === n.id}
             class:clickable={!!runState}
             role={runState ? 'button' : undefined}
             tabindex={runState ? 0 : undefined}
             onclick={() => { if (runState) selectNode(n.id); }}
             onkeydown={(ev) => {
               if (runState && (ev.key === 'Enter' || ev.key === ' ')) {
                 ev.preventDefault();
                 selectNode(n.id);
               }
             }}>
            {#if currentNode === n.id}
              <!-- Both drawn as their own rects, and BEFORE the node box, so
                   the motion happens around the step instead of over its
                   label. -->
              <rect class="current-pulse" x={p.x - 5} y={p.y - 5}
                    width={NODE_W + 10} height={NODE_H + 10} rx="10" />
              <rect class="current-ring" x={p.x - 4} y={p.y - 4}
                    width={NODE_W + 8} height={NODE_H + 8} rx="9" />
            {/if}
            <rect x={p.x} y={p.y} width={NODE_W} height={NODE_H} rx="6" />
            <text x={p.x + 9} y={p.y + NODE_H / 2 + 4} class="node-id">{nodeLabel(n.id)}</text>
            {#if st && st.runs > 1}
              <!-- How many instances this node produced. Inside a loop with no
                   item selected that is the fan-out; with one selected it is
                   that item's retries and loop-backs. -->
              <text x={p.x + NODE_W - 22} y={p.y + NODE_H / 2 + 4} class="run-count"
                >x{st.runs}</text>
            {/if}
            {#if n.checkpoint}
              <circle cx={p.x + NODE_W - 11} cy={p.y + NODE_H / 2} r="4" class="cp-dot">
                <title>{t('pipeline.legendCheckpoint')}</title>
              </circle>
            {/if}
          </g>
        {/if}
      {/each}
    </svg>
  </div>

  <!-- The trace pane. The drawing is ~430px wide inside a full-width panel,
       so the space to its right was empty; a run's most useful reading -- what
       the live step is actually doing -- was two clicks away on another page. -->
  {#if runState}
    <aside class="trace-pane">
      {#if openNode}
        {@const st = runState.byNode[openNode]}
        <header class="tp-head">
          <strong>{nodeLabel(openNode)}</strong>
          {#if nodeLabel(openNode) !== openNode}<code class="node-real-id">{openNode}</code>{/if}
          <span class="status-pill {st.status}">{t('pipeline.status.' + st.status)}</span>
          {#if cacheByStep?.[openNode]?.total_tokens != null}
            {@const cs = cacheByStep[openNode]}
            <span class="cache-inline-badge">
              {formatTokens(cs.total_tokens)}{cs.hit_ratio != null
                ? ' · ' + Math.round(cs.hit_ratio * 100) + '% cache' : ''}
            </span>
          {/if}
          {#if userPicked && currentNode && openNode !== currentNode}
            <button class="tp-follow" onclick={() => { userPicked = false; }}
                    title={t('pipeline.traceFollowHint')}>{t('pipeline.traceFollow')}</button>
          {/if}
          <button class="close" onclick={() => { userPicked = true; openNode = null; }}
                  aria-label={t('wsbrowser.close')}>✕</button>
        </header>

        {#if st.rows.length === 0}
          <p class="muted">{t('pipeline.noInstances')}</p>
        {:else}
          {#if st.rows.length > 1}
            <!-- One chip per instance: a looped step ran once per item, a
                 retried step more than once, and their records do not mix. -->
            <div class="tp-instances">
              {#each st.rows as r, i (r.id)}
                <button class="item-chip" class:is-active={r.id === activeInstance}
                        onclick={() => (pickedInstance = r.id)}>
                  #{i + 1}{r.loop_item ? ' · ' + r.loop_item : ''}
                </button>
              {/each}
            </div>
          {/if}
          {#if activeRow}
            <div class="tp-meta">
              <span class="status-pill {activeRow.status}">{activeRow.status}</span>
              <span>{fmtDuration(rowDuration(activeRow))}</span>
              {#if activeRow.retry_count}
                <span>{t('pipeline.colRetries')} {activeRow.retry_count}</span>
              {/if}
              {#if activeRow.loop_item}<span class="tp-item">{activeRow.loop_item}</span>{/if}
            </div>
            {#if activeRow.error}
              <pre class="exec-error">{activeRow.error}</pre>
            {/if}
          {/if}
          {#if runId}
            <NodeTrace {runId} stepInstanceId={activeInstance} live={activeLive} />
          {:else}
            <p class="muted">{t('pipeline.traceNoRun')}</p>
          {/if}
        {/if}
      {:else}
        <p class="muted">{t('pipeline.tracePickNode')}</p>
      {/if}
    </aside>
  {/if}
  </div>
{/if}
</div>

<style>
  /* Pico's classless build ships NO `--pico-color-<hue>-<n>` scale, so every
     reference to one in this app has only ever been resolved by its fallback.
     That is survivable in `background` -- an undefined var makes the
     declaration invalid at computed-value time and the property falls back to
     its initial value, i.e. transparent, so a missed fallback is merely
     invisible. In SVG it is not: `fill`'s initial value is BLACK, so a missed
     fallback paints the node solid black OVER its own label. Two rules here
     did exactly that. Everything the SVG paints with therefore comes from the
     literal tokens below, never from a `--pico-color-*` name. */
  .pg {
    --g-green-bg: #e6f7ea;
    --g-green-line: #2f7a45;
    --g-yellow-bg: #fdf3d0;
    --g-yellow-line: #8a6400;
    --g-red-bg: #ffe3e0;
    --g-red-line: #c0392b;
    --g-blue-bg: #dbeafe;
    --g-neutral-bg: var(--pico-code-background-color, #f4f4f4);
    --g-line: var(--pico-muted-border-color, #d8dade);
    --g-muted: var(--pico-muted-color, #7a7a85);
    --g-text: var(--pico-color, #1f2229);
    --g-surface: var(--pico-card-background-color, #ffffff);
    --g-accent: var(--pico-primary, #0669c1);
  }

  .graph-scroll {
    overflow: auto;
    max-height: 60vh;
    border: 1px solid var(--g-line);
    border-radius: var(--pico-border-radius, 4px);
    background: var(--g-surface);
  }
  .graph-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    font-size: 0.75rem;
    color: var(--g-muted);
    margin: 0.25rem 0 0.4rem;
  }
  .graph-legend { display: inline-flex; align-items: center; gap: 0.3rem; }
  .sw {
    width: 0.7rem; height: 0.7rem; border-radius: 3px; display: inline-block;
    border: 1px solid var(--g-line);
  }
  .sw.agent { background: var(--g-blue-bg); }
  .sw.tool { background: var(--g-yellow-bg); }
  .sw.gate { background: var(--g-neutral-bg); }
  .sw.addon { background: var(--g-green-bg); }
  .sw.cp { background: var(--g-accent); border-radius: 50%; }
  .graph-error { color: var(--g-red-line); font-size: 0.85rem; }
  .graph-loading { color: var(--g-muted); font-size: 0.85rem; }

  /* Loop item picker */
  .item-strip {
    display: flex; flex-wrap: wrap; align-items: center; gap: 0.3rem;
    margin: 0 0 0.45rem;
  }
  .item-label { font-size: 0.75rem; color: var(--g-muted); margin-right: 0.2rem; }
  .item-chip {
    background: var(--g-neutral-bg);
    border: 1px solid var(--g-line);
    color: inherit;
    border-radius: 999px; padding: 0.05rem 0.5rem; margin: 0;
    width: auto; font-size: 0.72rem; font-family: var(--pico-font-family-monospace, monospace);
    cursor: pointer;
  }
  .item-chip.is-active {
    background: var(--g-accent); color: var(--pico-primary-inverse, #fff);
    border-color: var(--g-accent);
  }
  .item-note { font-size: 0.75rem; color: var(--g-muted); margin: 0 0 0.45rem; }

  svg { display: block; }
  .edge { fill: none; stroke: var(--g-line); stroke-width: 1.4; }
  .edge.back { stroke-dasharray: 4 3; stroke: var(--g-yellow-line); }
  .arrow-head { fill: var(--g-line); }
  .edge-label { font-size: 9px; fill: var(--g-muted); text-anchor: middle; }

  .loop-box rect {
    fill: none;
    stroke: var(--g-yellow-line);
    stroke-width: 1;
    stroke-dasharray: 5 4;
    opacity: 0.75;
  }
  .loop-box text {
    font-size: 9px;
    fill: var(--g-yellow-line);
    font-family: var(--pico-font-family-monospace, monospace);
  }

  .node rect {
    fill: var(--g-neutral-bg);
    stroke: var(--g-line);
    stroke-width: 1;
  }
  .node.agent rect { fill: var(--g-blue-bg); }
  .node.tool rect { fill: var(--g-yellow-bg); }
  .node.addon rect {
    fill: var(--g-green-bg);
    stroke: var(--g-green-line);
    stroke-width: 1.6;
  }
  .node.clickable { cursor: pointer; }
  /* Run status wins over the node-type tint: on a run page the question is
     "where did it get to", not "what kind of step is this". */
  .node.completed rect { fill: var(--g-green-bg); }
  .node.running rect {
    fill: var(--g-yellow-bg);
    stroke: var(--g-yellow-line); stroke-width: 2;
  }
  .node.failed rect {
    fill: var(--g-red-bg);
    stroke: var(--g-red-line); stroke-width: 2;
  }
  .node.mixed rect {
    fill: var(--g-yellow-bg);
  }
  .node.pending rect, .node.absent rect {
    fill: var(--g-surface);
    stroke-dasharray: 3 3;
  }
  .node.is-open rect { stroke: var(--g-accent); stroke-width: 2; }
  /* The live step. Deliberately the loudest thing on the graph: it is the one
     box the reader came to find, and a fading outline was too easy to miss
     among several yellow ones. Two moving parts, because motion is what the
     eye catches: a marching dashed ring (a marquee never reads as static) and
     a halo that expands out of the box and fades, like a ping. */
  .node.is-current rect.current-ring {
    fill: none;
    stroke: var(--g-accent);
    stroke-width: 2.5;
    stroke-dasharray: 9 6;
    animation: pg-march 0.9s linear infinite;
  }
  .node.is-current rect.current-pulse {
    fill: var(--g-accent);
    stroke: none;
    /* scale about the rect's own centre, not the SVG origin */
    transform-box: fill-box;
    transform-origin: center;
    animation: pg-ping 1.7s ease-out infinite;
  }
  .node.is-current rect:not(.current-ring):not(.current-pulse) {
    stroke: var(--g-accent);
    stroke-width: 2;
  }
  @keyframes pg-march {
    to { stroke-dashoffset: -30; }
  }
  @keyframes pg-ping {
    0%   { transform: scale(0.94); opacity: 0.3; }
    70%  { transform: scale(1.14); opacity: 0; }
    100% { transform: scale(1.14); opacity: 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    /* Still unmistakable standing still: a solid accent ring, no motion. */
    .node.is-current rect.current-ring {
      animation: none; stroke-dasharray: none; opacity: 1;
    }
    .node.is-current rect.current-pulse { animation: none; opacity: 0.16; }
  }
  .node-id {
    font-size: 11px;
    font-family: var(--pico-font-family-monospace, monospace);
    fill: var(--g-text);
  }
  .run-count { font-size: 9px; fill: var(--g-muted); }
  .cp-dot { fill: var(--g-accent); }

  /* Graph left, trace right. The drawing has a fixed intrinsic width, so the
     pane takes whatever the panel has left; below ~900px they stack instead of
     squeezing the trace into a column too narrow to read. */
  .pg-body {
    display: flex;
    align-items: stretch;
    gap: 0.6rem;
    flex-wrap: wrap;
  }
  .pg-body > .graph-scroll { flex: 0 1 auto; }
  .trace-pane {
    flex: 1 1 340px;
    min-width: 0;
    max-height: 60vh;
    display: flex;
    flex-direction: column;
    /* Belt and braces: the trace list inside is the intended scroller, and
       when it works this never fires. If that flex chain is ever broken again,
       the records overflow the PANE and it scrolls -- rather than silently
       squashing them, which is a failure mode that looks like a design. */
    overflow-y: auto;
    padding: 0.45rem 0.55rem;
    border: 1px solid var(--g-line);
    border-radius: var(--pico-border-radius, 4px);
    background: var(--g-surface);
  }
  .tp-head {
    display: flex; align-items: center; gap: 0.4rem; margin-bottom: 0.35rem;
    flex-wrap: wrap;
    flex: 0 0 auto;
  }
  .tp-head .close {
    margin-left: auto; background: none; border: none; width: auto;
    padding: 0 0.2rem; cursor: pointer; color: var(--g-muted);
  }
  .tp-follow {
    background: none; border: 1px solid var(--g-accent); color: var(--g-accent);
    border-radius: 999px; padding: 0 0.45rem; width: auto; margin: 0;
    font-size: 0.68rem; cursor: pointer;
  }
  .tp-instances {
    display: flex; flex-wrap: wrap; gap: 0.25rem; margin-bottom: 0.3rem;
    flex: 0 0 auto;
  }
  .tp-meta {
    display: flex; align-items: center; flex-wrap: wrap; gap: 0.4rem;
    font-size: 0.72rem; color: var(--g-muted); margin-bottom: 0.35rem;
    flex: 0 0 auto;
  }
  .tp-item {
    font-family: var(--pico-font-family-monospace, monospace);
    color: var(--g-accent);
  }
  .node-real-id { font-size: 0.7rem; color: var(--g-muted); }
  .cache-inline-badge {
    font-size: 0.7rem; padding: 0.05rem 0.4rem; border-radius: 4px;
    background: var(--pico-code-background-color, #f4f4f4);
    color: var(--g-muted);
  }
  .status-pill {
    font-size: 0.7rem; padding: 0.05rem 0.4rem; border-radius: 4px;
    background: var(--pico-code-background-color, #f4f4f4);
    color: var(--g-muted);
  }
  .status-pill.completed {
    background: var(--g-green-bg);
    color: var(--g-green-line);
  }
  .status-pill.failed {
    background: var(--g-red-bg);
    color: var(--g-red-line);
  }
  .status-pill.running, .status-pill.claimed, .status-pill.mixed {
    background: var(--g-yellow-bg);
    color: var(--g-yellow-line);
  }
  .exec-error {
    margin: 0.1rem 0 0.3rem; padding: 0.35rem; font-size: 0.72rem;
    white-space: pre-wrap; word-break: break-word;
    background: var(--pico-code-background-color, #f4f4f4);
    color: var(--pico-del-color, #b3261e); border-radius: 4px;
  }
  .muted { color: var(--pico-muted-color, #8a8a8a); font-size: 0.85rem; }
</style>
