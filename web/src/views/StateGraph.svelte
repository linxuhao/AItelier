<script lang="ts">
  import { stateLayout, stateTone, shortText, cardTitle, type StateNodeSummary } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  interface Props { nodes: StateNodeSummary[]; selected: string; onselect: (key: string) => void }
  const { nodes, selected, onselect }: Props = $props();
  let domain = $state('');
  let query = $state('');
  let local = $state(false);
  let zoom = $state(1);
  const domains = $derived([...new Set(nodes.map(n => n.domain))].sort());
  const result = $derived.by(() => {
    try { return { layout: stateLayout(nodes, domain, local ? selected : '', query), error: '' }; }
    catch (error) { return { layout: null, error: String(error) }; }
  });
  const positions = $derived(new Map(result.layout?.nodes.map(n => [n.node_key, n]) ?? []));
  const shownList = $derived(nodes.filter(n => (!domain || n.domain === domain) &&
    (!query.trim() || (n.node_key + ' ' + n.title).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))).slice(0, 60));
  function edgePath(from: string, to: string) {
    const a = positions.get(from), b = positions.get(to);
    if (!a || !b) return '';
    const x1 = a.x + 105, y1 = a.y + 102, x2 = b.x + 105, y2 = b.y;
    return `M ${x1} ${y1} C ${x1} ${(y1+y2)/2}, ${x2} ${(y1+y2)/2}, ${x2} ${y2-5}`;
  }
  function keySelect(event: KeyboardEvent, id: string) {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onselect(id); }
  }
</script>

<section class="state-graph" aria-label={st('graph')}>
  <div class="controls">
    <label>{st('domain')}<select bind:value={domain}><option value="">{st('all')}</option>
      {#each domains as d (d)}<option value={d}>{d}</option>{/each}</select></label>
    <label class="search">{st('search')}<input type="search" bind:value={query} placeholder={st('search')} /></label>
    <div class="zoom">
      <button type="button" class="outline" aria-label={st('zoomOut')} disabled={zoom <= .6} onclick={() => zoom = Math.max(.6, zoom - .2)}>−</button>
      <button type="button" class="outline" onclick={() => zoom = 1}>{Math.round(zoom * 100)}%</button>
      <button type="button" class="outline" aria-label={st('zoomIn')} disabled={zoom >= 1.8} onclick={() => zoom = Math.min(1.8, zoom + .2)}>+</button>
    </div>
  </div>
  <label class="focus-toggle"><input type="checkbox" bind:checked={local} disabled={!selected} />{st('local')}</label>
  <p class="legend">{st('legend')}</p>
  {#if result.error}
    <p role="alert">{result.error}</p>
  {:else if result.layout?.tooLarge}
    <p role="status">{st('large')} ({result.layout.count})</p>
    <div class="goal-list">{#each shownList as n (n.node_key)}
      <button type="button" class="outline" onclick={() => { onselect(n.node_key); local = true; domain = ''; query = ''; }}>{n.title}</button>
    {/each}</div>
  {:else if result.layout && !result.layout.nodes.length}
    <p>{nodes.length ? st('noMatch') : st('emptyNodes')}</p>
  {:else if result.layout}
    {#if result.layout.hiddenEdges}<p class="boundary">{st('boundary')}: {result.layout.hiddenEdges}</p>{/if}
    <div class="canvas" role="region" aria-label={st('graph')}>
      <svg width={result.layout.width * zoom} height={result.layout.height * zoom}
           viewBox={`0 0 ${result.layout.width} ${result.layout.height}`} aria-label={st('graph')} role="group">
        <defs><marker id="state-dependency-arrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="currentColor" /></marker></defs>
        {#each result.layout.edges as edge (edge.from + '|' + edge.to)}
          <path class="dependency" d={edgePath(edge.from, edge.to)} marker-end="url(#state-dependency-arrow)" />
        {/each}
        {#each result.layout.nodes as node (node.node_key)}
          <g class="goal {stateTone(node.status)}" class:selected={selected === node.node_key}
             role="button" tabindex="0" aria-pressed={selected === node.node_key}
             aria-label={`${node.title}: ${node.status}, ${st(node.readiness)}`}
             transform={`translate(${node.x}, ${node.y})`}
             onclick={() => onselect(node.node_key)} onkeydown={(event) => keySelect(event, node.node_key)}>
            <title>{node.title} — {node.node_key} — {node.status} / {node.readiness}</title>
            <rect width="210" height="102" rx="9" />
            <text class="goal-key" x="12" y="19">{shortText(node.node_key, 29)}</text>
            <text class="goal-title" x="12" y="42">{cardTitle(node.title)}</text>
            <text class="goal-state" x="12" y="64">{node.status} · r{node.revision}</text>
            <text class="goal-ready" x="12" y="84">{node.hold ? '⏸ ' : ''}{st(node.readiness)}{node.latest_attempt ? ' · ' + node.latest_attempt.status : ''}</text>
            {#if node.outsideDependencies}<text x="195" y="20" text-anchor="end" class="outside">+{node.outsideDependencies}</text>{/if}
          </g>
        {/each}
      </svg>
    </div>
  {/if}
</section>

<style>
  .state-graph { min-width: 0; }
  .controls { display:flex; gap:.7rem; align-items:flex-end; flex-wrap:wrap; }
  label { font-size:.8rem; margin:0; }
  input, select, button { margin:0; font-size:.8rem; padding:.45rem .65rem; min-height:2.1rem; }
  .search { flex:1; min-width:150px; }
  .zoom { display:flex; gap:.25rem; padding-bottom:2px; }
  .focus-toggle { display:flex; align-items:center; gap:.45rem; margin:.7rem 0; }
  .focus-toggle input { min-height:0; width:1rem; }
  .legend,.boundary { font-size:.76rem; color:var(--pico-muted-color,#64748b); margin:.5rem 0; }
  .canvas { width:100%; overflow:auto; max-height:68vh; min-height:180px; border:1px solid var(--pico-muted-border-color,#dce2ea); border-radius:10px; background:var(--pico-card-background-color,#fff); }
  svg { display:block; max-width:none; }
  .dependency { fill:none; stroke:#8593a6; stroke-width:1.6; color:#8593a6; }
  .goal { cursor:pointer; outline:none; }
  .goal rect { fill:var(--pico-card-background-color,#fff); stroke:#94a3b8; stroke-width:1.3; }
  .goal text { fill:var(--pico-color,#243447); pointer-events:none; }
  .goal-key { font-size:10px; opacity:.72; }
  .goal-title { font-size:12px; font-weight:650; }
  .goal-state { font-size:11px; font-weight:600; }
  .goal-ready { font-size:10px; opacity:.8; }
  .outside { font-size:9px; }
  .goal.verified rect { stroke:#1d9468; stroke-width:2; }
  .goal.candidate rect { stroke:#397ed0; }
  .goal.stale rect { stroke:#c48a27; stroke-dasharray:5 3; }
  .goal.superseded { opacity:.55; }
  .goal.selected rect,.goal:focus rect { stroke:var(--pico-primary,#0066cc); stroke-width:3; }
  .goal-list { display:flex; gap:.5rem; flex-wrap:wrap; }
  @media(max-width:650px) { .controls { gap:.5rem; } .canvas { max-height:52vh; } }
</style>
