<script lang="ts">
  /**
   * Draw one pipeline's COMPOSED graph.
   *
   * Composed, because that is what runs: `dpe_game` is `dpe_default_v2` plus the
   * `game_harness` overlay, has no YAML of its own, and its spliced steps are
   * the whole reason to look at it. They are tinted, so "the pipeline" and "what
   * the addon added to it" are one picture instead of two.
   *
   * Drawn by hand rather than with a graph library: these are 1-25 node graphs,
   * and the alternative was a megabyte of renderer for a panel that opens on
   * demand.
   */
  import { onMount } from 'svelte';
  import { pipelineGraph } from '../lib/api';
  import { layoutGraph, type Layout, type LaidOutNode } from '../lib/pipelineLayout';
  import { t } from '../lib/i18n.svelte';

  interface Props { config: string }
  const { config }: Props = $props();

  let error = $state<string | null>(null);
  let data = $state<Record<string, unknown> | null>(null);
  let layout = $state<Layout | null>(null);

  // Geometry. Vertical: pipelines are long chains, so rank == row keeps the
  // drawing readable at panel width instead of scrolling sideways for 20 steps.
  const NODE_W = 170;
  const NODE_H = 32;
  const X_GAP = 20;
  const Y_GAP = 30;
  const PAD = 14;
  // Room on the right for the return arcs, so a loop never crosses a node box.
  const LOOP_LANE = 46;

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

  onMount(async () => {
    try {
      const r = await pipelineGraph(config);
      data = r as Record<string, unknown>;
      layout = layoutGraph(
        (r.steps ?? []) as never[],
        (r.begin ?? '') as string,
      );
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  });
</script>

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
  <div class="graph-scroll">
    <svg {width} {height} viewBox="0 0 {width} {height}" role="img"
         aria-label={t('pipeline.graphAria').replace('{name}', config)}>
      <defs>
        <marker id="pg-arrow" viewBox="0 0 8 8" refX="7" refY="4"
                markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 0 L 8 4 L 0 8 z" class="arrow-head" />
        </marker>
      </defs>

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
          <g class="node {n.type} {n.from_addon ? 'addon' : ''}">
            <rect x={p.x} y={p.y} width={NODE_W} height={NODE_H} rx="6" />
            <text x={p.x + 9} y={p.y + NODE_H / 2 + 4} class="node-id">{n.id}</text>
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
{/if}

<style>
  .graph-scroll {
    overflow: auto;
    max-height: 60vh;
    border: 1px solid var(--pico-muted-border-color);
    border-radius: var(--pico-border-radius);
    background: var(--pico-card-background-color);
  }
  .graph-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    font-size: 0.75rem;
    color: var(--pico-muted-color);
    margin: 0.25rem 0 0.4rem;
  }
  .graph-legend { display: inline-flex; align-items: center; gap: 0.3rem; }
  .sw {
    width: 0.7rem; height: 0.7rem; border-radius: 3px; display: inline-block;
    border: 1px solid var(--pico-muted-border-color);
  }
  .sw.agent { background: var(--pico-color-blue-100, #cfe3ff); }
  .sw.tool { background: var(--pico-color-yellow-100); }
  .sw.gate { background: var(--pico-code-background-color); }
  .sw.addon { background: var(--pico-color-green-100); }
  .sw.cp { background: var(--pico-primary); border-radius: 50%; }
  .graph-error { color: var(--pico-color-red-500); font-size: 0.85rem; }
  .graph-loading { color: var(--pico-muted-color); font-size: 0.85rem; }

  svg { display: block; }
  .edge {
    fill: none;
    stroke: var(--pico-muted-border-color);
    stroke-width: 1.4;
  }
  .edge.back {
    stroke-dasharray: 4 3;
    stroke: var(--pico-color-yellow-700);
  }
  .arrow-head { fill: var(--pico-muted-border-color); }
  .edge-label {
    font-size: 9px;
    fill: var(--pico-muted-color);
    text-anchor: middle;
  }
  .node rect {
    fill: var(--pico-code-background-color);
    stroke: var(--pico-muted-border-color);
    stroke-width: 1;
  }
  .node.agent rect { fill: var(--pico-color-blue-100, #cfe3ff); }
  .node.tool rect { fill: var(--pico-color-yellow-100); }
  .node.addon rect {
    fill: var(--pico-color-green-100);
    stroke: var(--pico-color-green-700);
    stroke-width: 1.6;
  }
  .node-id {
    font-size: 11px;
    font-family: var(--pico-font-family-monospace, monospace);
    fill: var(--pico-color, #222);
  }
  .cp-dot { fill: var(--pico-primary); }
</style>
