<script lang="ts">
  import { stateLayout, stateTone, stateNextAction, shortText, cardLines, groupFacets, stemOf, STATE_CARD, STATE_LANE,
           type StateNodeSummary, type StateLane } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  interface Props { nodes: StateNodeSummary[]; selected: string; onselect: (key: string) => void }
  const { nodes, selected, onselect }: Props = $props();
  let domain = $state('');
  let query = $state('');
  let local = $state(false);
  let zoom = $state(1);
  // Grouped by default: faceting put three nodes where a reader sees one goal.
  let grouped = $state(true);
  const groupable = $derived(nodes.some(n => n.facet === 'contract' || n.facet === 'test'));
  const shown = $derived(grouped && groupable ? groupFacets(nodes) : nodes);
  const byKey = $derived(new Map(nodes.map(n => [n.node_key, n])));
  // A selected lane keeps its group selected, so collapsing never loses the focus.
  const selectedShown = $derived(grouped && groupable && byKey.has(selected)
    ? stemOf(byKey.get(selected)!) : selected);
  const domains = $derived([...new Set(shown.map(n => n.domain))].sort());
  const result = $derived.by(() => {
    try { return { layout: stateLayout(shown, domain, local ? selectedShown : '', query), error: '' }; }
    catch (error) { return { layout: null, error: String(error) }; }
  });
  const positions = $derived(new Map(result.layout?.nodes.map(n => [n.node_key, n]) ?? []));
  const shownList = $derived(shown.filter(n => (!domain || n.domain === domain) &&
    (!query.trim() || (n.node_key + ' ' + n.title).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))).slice(0, 60));
  function lanes(node: StateNodeSummary): StateLane[] { return (node as { lanes?: StateLane[] }).lanes ?? []; }
  function edgePath(from: string, to: string) {
    const a = positions.get(from), b = positions.get(to);
    if (!a || !b) return '';
    const x1 = a.x + STATE_CARD.width/2, y1 = a.y + STATE_CARD.height, x2 = b.x + STATE_CARD.width/2, y2 = b.y;
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
  <div class="toggles">
    <label class="focus-toggle"><input type="checkbox" bind:checked={local} disabled={!selected} />{st('local')}</label>
    {#if groupable}
      <label class="focus-toggle"><input type="checkbox" bind:checked={grouped} />{st('groupFacets')}</label>
      <small>{grouped ? `${shown.length} / ${nodes.length}` : nodes.length}</small>
    {/if}
  </div>
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
           style:width={`${result.layout.width * zoom}px`} style:height={`${result.layout.height * zoom}px`}
           viewBox={`0 0 ${result.layout.width} ${result.layout.height}`} aria-label={st('graph')} role="group">
        <defs><marker id="state-dependency-arrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="currentColor" /></marker></defs>
        {#each result.layout.edges as edge (edge.from + '|' + edge.to)}
          <path class="dependency" d={edgePath(edge.from, edge.to)} marker-end="url(#state-dependency-arrow)" />
        {/each}
        {#each result.layout.nodes as node (node.node_key)}
          {@const nextAction = stateNextAction(node)}
          {@const lane = lanes(node)}
          {@const readyLabel = `${node.hold ? '⏸ ' : ''}${st(nextAction ?? node.readiness)}${node.blocked_by.length ? ` · ${node.blocked_by.length}` : ''}`}
          <!-- A goal with lanes is a FRAME, not a button: its header and each of
               its lane cards are the buttons, which keeps them siblings rather
               than interactive elements nested inside an interactive element. -->
          {#if lane.length}
          <g class="goal framed {stateTone(node.status)}" class:selected={selectedShown === node.node_key}
             data-fact={node.status} data-readiness={node.readiness} data-attempt-status={node.latest_attempt?.status ?? "none"}
             transform={`translate(${node.x}, ${node.y})`}>
            <rect class="card" width={STATE_CARD.width} height={STATE_CARD.height} rx="10" />
              <g class="goal-header" role="button" tabindex="0" aria-pressed={selectedShown === node.node_key}
                 aria-label={`${node.title}: ${st(nextAction ?? node.readiness)} · ${lane.filter(l => l.present).map(l => st('lane_' + l.facet) + ' ' + l.status).join(', ')}`}
                 onclick={() => onselect(node.node_key)} onkeydown={(event) => keySelect(event, node.node_key)}>
                <title>{node.title} — {node.node_key} — {readyLabel}</title>
                <rect class="header-hit" width={STATE_CARD.width} height={STATE_LANE.headerHeight} rx="10" />
                <text class="goal-key" x="14" y="20">{shortText(node.node_key, 34)}</text>
                <text class="goal-title" x="14" y="44">{#each cardLines(node.title) as line, i (i)}<tspan x="14" dy={i === 0 ? 0 : 18}>{line}</tspan>{/each}</text>
                <text class="goal-ready" x="14" y="82">{readyLabel}</text>
              </g>
              {#each lane as item, i (item.facet)}
                {@const at = `translate(${STATE_LANE.x + i * (STATE_LANE.width + STATE_LANE.gap)}, ${STATE_LANE.y})`}
                {#if item.present}
                  <g class="lane {stateTone(item.status)}" class:selected={selected === item.node_key}
                     role="button" tabindex="0" aria-pressed={selected === item.node_key}
                     aria-label={`${item.node_key}: ${item.status}`} transform={at}
                     onclick={() => onselect(item.node_key)} onkeydown={(event) => keySelect(event, item.node_key)}>
                    <title>{item.node_key} — {item.status} · {st(item.readiness)}</title>
                    {@render laneFace(item)}
                  </g>
                {:else}
                  <g class="lane absent" transform={at}>
                    <title>{item.node_key} — {st('laneMissing')}</title>
                    {@render laneFace(item)}
                  </g>
                {/if}
              {/each}
            {#if node.outsideDependencies}<text x="250" y="20" text-anchor="end" class="outside">+{node.outsideDependencies}</text>{/if}
          </g>
          {:else}
          <g class="goal {stateTone(node.status)}" class:selected={selectedShown === node.node_key}
             data-fact={node.status} data-readiness={node.readiness} data-attempt-status={node.latest_attempt?.status ?? "none"}
             role="button" tabindex="0" aria-pressed={selected === node.node_key}
             aria-label={`${node.title}: ${node.status}, ${st(nextAction ?? node.readiness)}`}
             transform={`translate(${node.x}, ${node.y})`}
             onclick={() => onselect(node.node_key)} onkeydown={(event) => keySelect(event, node.node_key)}>
            <title>{node.title} — {node.node_key} — {node.status} / {nextAction ?? node.readiness}</title>
            <rect class="card" width={STATE_CARD.width} height={STATE_CARD.height} rx="10" />
              <text class="goal-key" x="14" y="20">{shortText(node.node_key, 34)}</text>
              <text class="goal-title" x="14" y="44">{#each cardLines(node.title) as line, i (i)}<tspan x="14" dy={i === 0 ? 0 : 18}>{line}</tspan>{/each}</text>
              <rect class="status-background" x="13" y="77" width="153" height="26" rx="6" />
              <text class="goal-state" x="22" y="95">{node.status}</text>
              <text class="goal-revision" x="250" y="95" text-anchor="end">{node.facet ? node.facet + ' · ' : ''}r{node.revision}</text>
              <text class="goal-ready" x="14" y="123">{readyLabel}</text>
              <text class="goal-attempt" x="14" y="144">{st(node.latest_attempt?.execution_kind==='external'?'externalShort':'attemptShort')}: {node.latest_attempt ? node.latest_attempt.status.toUpperCase() : st('notStarted')}</text>
            {#if node.outsideDependencies}<text x="250" y="20" text-anchor="end" class="outside">+{node.outsideDependencies}</text>{/if}
          </g>
          {/if}
        {/each}
      </svg>
    </div>
  {/if}
</section>

{#snippet laneFace(item: StateLane)}
  <rect class="lane-card" width={STATE_LANE.width} height={STATE_LANE.height} rx="7" />
  <text class="lane-facet" x="7" y="16">{st('lane_' + item.facet)}</text>
  <text class="lane-status" x="7" y="33">{item.present ? shortText(item.status, 10) : '—'}</text>
  {#if item.present}<text class="lane-meta" x="7" y="47">r{item.revision} · {st(item.readiness)}</text>{/if}
{/snippet}

<style>
  /* Resolve theme tokens OUTSIDE [role=button]. Pico overrides --pico-color
     on buttons (including SVG g), otherwise white labels sit on white cards. */
  .state-graph { min-width:0; --sg-ink:var(--pico-color,#243447); --sg-surface:var(--pico-card-background-color,#fff); }
  .controls { display:flex; gap:.7rem; align-items:flex-end; flex-wrap:wrap; }
  label { font-size:.8rem; line-height:1.25; margin:0; }
  input, select, button { margin:0; font-size:.8rem; padding:.45rem .65rem; min-height:2.1rem; }
  .search { flex:1; min-width:150px; }
  .zoom { display:flex; gap:.25rem; padding-bottom:2px; }
  .focus-toggle { display:flex; align-items:center; gap:.45rem; margin:.3rem 0; }
  .focus-toggle input { min-height:0; width:1rem; }
  .legend,.boundary { font-size:.76rem; color:var(--pico-muted-color,#64748b); line-height:1.3; margin:.3rem 0; }
  .canvas { width:100%; overflow:auto; max-height:68vh; min-height:180px; border:1px solid var(--pico-muted-border-color,#dce2ea); border-radius:10px; background:var(--pico-card-background-color,#fff); }
  svg { display:block; max-width:none; max-inline-size:none; flex:none; }
  .dependency { fill:none; stroke:#8593a6; stroke-width:1.6; color:#8593a6; }
  .goal { cursor:pointer; outline:none; color:var(--sg-ink); --pico-color:var(--sg-ink); --sg-status:#e8edf3; --sg-status-ink:#25384d; }
  .goal .card { fill:var(--sg-surface); stroke:#8394a8; stroke-width:1.5; }
  .goal text { fill:var(--sg-ink); pointer-events:none; }
  .goal-key { font-size:11px; }
  .goal-title { font-size:13px; font-weight:650; }
  .goal .status-background { fill:var(--sg-status); stroke:none; }
  .goal .goal-state { font-size:12px; font-weight:700; fill:var(--sg-status-ink); }
  .goal-revision,.goal-ready,.goal-attempt { font-size:12px; }
  .outside { font-size:10px; }
  .toggles { display:flex; gap:1rem; align-items:center; flex-wrap:wrap; }
  .toggles small { font-size:.72rem; color:var(--pico-muted-color,#64748b); }
  /* A framed goal is a container: its own fill stays neutral so the lane cards
     inside it carry the colour, otherwise two status tones fight on one card. */
  .goal.framed .card { fill:var(--sg-surface); stroke:#9aa8b9; }
  .goal.framed.selected .card { stroke:var(--pico-primary,#0066cc); stroke-width:3; }
  .goal-header,.lane { cursor:pointer; outline:none; }
  .header-hit { fill:transparent; }
  .goal-header:focus-visible .header-hit { stroke:var(--pico-primary,#0066cc); stroke-width:2; }
  .lane .lane-card { fill:var(--sg-status); stroke:var(--sg-status-ink); stroke-width:1; stroke-opacity:.35; }
  .lane text { fill:var(--sg-status-ink); pointer-events:none; }
  .lane-facet { font-size:10px; font-weight:700; letter-spacing:.04em; }
  .lane-status { font-size:12px; font-weight:650; }
  .lane-meta { font-size:9px; opacity:.75; }
  .lane.verified { --sg-status:#dcf5e7; --sg-status-ink:#145638; }
  .lane.candidate { --sg-status:#e0ecff; --sg-status-ink:#194980; }
  .lane.stale { --sg-status:#fff0ce; --sg-status-ink:#644509; }
  .lane.open { --sg-status:#eef2f7; --sg-status-ink:#25384d; }
  .lane.superseded { --sg-status:#eceff3; --sg-status-ink:#5b6773; }
  .lane.absent .lane-card { fill:none; stroke:var(--pico-muted-border-color,#c9d3df); stroke-dasharray:3 3; stroke-opacity:1; }
  .lane.absent text { fill:var(--pico-muted-color,#8a97a8); }
  .lane.selected .lane-card,.lane:focus-visible .lane-card { stroke:var(--pico-primary,#0066cc); stroke-width:2.5; stroke-opacity:1; }
  .lane:hover .lane-card { stroke-opacity:.9; }
  .goal.verified { --sg-status:#dcf5e7; --sg-status-ink:#145638; }
  .goal.verified .card { stroke:#1d9468; stroke-width:2; }
  .goal.candidate { --sg-status:#e0ecff; --sg-status-ink:#194980; }
  .goal.candidate .card { stroke:#397ed0; }
  .goal.stale { --sg-status:#fff0ce; --sg-status-ink:#644509; }
  .goal.stale .card { stroke:#ad741b; stroke-dasharray:5 3; }
  .goal.superseded .card { stroke-dasharray:2 3; }
  .goal.selected .card,.goal:focus-visible .card { stroke:var(--pico-primary,#0066cc); stroke-width:3; }
  .goal:hover .card { stroke-width:2.5; }
  .goal-list { display:flex; gap:.5rem; flex-wrap:wrap; }
  @media(max-width:650px) { .controls { gap:.5rem; } .canvas { max-height:52vh; } }
</style>
