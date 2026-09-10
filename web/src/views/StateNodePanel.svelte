<script lang="ts">
  import { stateNode } from '../lib/api';
  import { attemptLabel, exactRunHref, stateProjectHref, stemOf,
           type StateNodeDetail, type StateNodeSummary } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  import StateAttemptEvidence from './StateAttemptEvidence.svelte';
  interface Props { projectId: string; nodeKey: string; refresh?: number; onselect: (key: string) => void;
                    nodes?: StateNodeSummary[] }
  const { projectId, nodeKey, refresh = 0, onselect, nodes = [] }: Props = $props();
  // The three lanes of one goal. The graph collapses them into a single card,
  // so this is where you step between contract, test and implementation.
  const siblings = $derived.by(() => {
    const here = nodes.find(n => n.node_key === nodeKey);
    if (!here) return [];
    const stem = stemOf(here);
    const family = nodes.filter(n => stemOf(n) === stem);
    return family.length > 1 ? family.sort((a, b) => a.node_key.length - b.node_key.length) : [];
  });
  let data = $state<StateNodeDetail | null>(null);
  let error = $state('');
  let retry = $state(0);
  let openAttempt = $state('');
  let previousTarget = '';
  $effect(() => {
    const project = projectId, node = nodeKey; void refresh; void retry;
    let cancelled = false; error = '';
    const target = project + '/' + node;
    // Only a DIFFERENT node blanks the panel. The dashboard polls every 15s by
    // bumping `refresh`, which re-runs this effect with the SAME target — and
    // clearing data there tore the whole panel down to "Loading…" every tick:
    // measured live, a 1960px column collapsing to 91px, taking any open
    // evidence and the reader's scroll position with it. The fetch replaces the
    // content when it lands; until then the panel keeps showing what it has.
    if (target !== previousTarget) { data = null; openAttempt = ''; }
    previousTarget = target;
    if (!node) { data = null; return; }
    stateNode(project, node).then(value => { if (!cancelled) data = value; })
      .catch(e => { if (!cancelled) error = String(e.message ?? e); });
    return () => { cancelled = true; };
  });
</script>
<aside class="node-panel" aria-label={st('detail')}>
  {#if !nodeKey}<p>{st('select')}</p>
  {:else if error && !data}<p role="alert">{error}</p><button class="outline" onclick={() => retry++}>{st('retry')}</button>
  {:else if !data}<p aria-live="polite">{st('loading')}</p>
  {:else}
    <!-- A failed refresh annotates the snapshot instead of replacing it: the
         panel already holds a readable, if older, answer. -->
    {#if error}<p class="stale" role="alert">{st('staleView')} {error}
      <button class="outline" onclick={() => retry++}>{st('retry')}</button></p>{/if}
    <div class="panel-heading"><code>{nodeKey}</code><a href={stateProjectHref(projectId, nodeKey)} aria-label="Permalink">↗</a></div>
    <h3>{data.node.goal.split('\n')[0]}</h3>
    <div class="badges"><strong>{data.node.status}</strong><span>{st(data.node.readiness)}</span><span>r{data.node.revision}</span>{#if data.node.facet}<span class="facet">{data.node.facet}</span>{/if}</div>
    {#if siblings.length}
      <div class="lanes" aria-label={st('groupFacets')}>
        {#each siblings as sibling (sibling.node_key)}
          <button type="button" class="lane-link" class:current={sibling.node_key === nodeKey}
                  aria-current={sibling.node_key === nodeKey ? 'true' : undefined}
                  onclick={() => onselect(sibling.node_key)}>
            {st('lane_' + (sibling.facet === 'contract' || sibling.facet === 'test' ? sibling.facet : 'content'))}
            <small>{sibling.status}</small>
          </button>
        {/each}
      </div>
    {/if}
    <p class="goal-text">{data.node.goal}</p>
    {#if data.node.hold}
      <div class="hold" role="note"><strong>⏸ {st('hold')}</strong><p>{data.node.hold.reason}</p><small>{st('holdNote')}</small></div>
    {/if}
    {#if data.node.node_hold?.held && data.node.hold?.scope === 'project'}
      <p class="hold"><strong>{st('protected')}:</strong> {data.node.node_hold.reason}</p>
    {/if}
    <h4>{st('depends')}</h4>
    {#if !data.node.dependencies.length}<p class="muted">—</p>{/if}
    <div class="deps">{#each data.node.dependencies as dep (dep)}
      <button class="outline" type="button" onclick={() => onselect(dep)}>{dep} · {data.dependency_receipts[dep]?.status ?? '?'}</button>
    {/each}</div>
    <h4>{st('contract')}</h4>
    <code class="digest">{data.node.contract_hash}</code>
    {#each data.node.acceptance as criterion (criterion.id)}
      <div class="criterion"><strong>{criterion.id}</strong> <small>{criterion.kind}</small><p>{criterion.description}</p></div>
    {/each}
    <h4>{st('attemptList')}</h4>
    {#if !data.attempts.length}<p class="muted">{st('noAttempts')}</p>{/if}
    {#each data.attempts as attempt (attempt.attempt_id)}
      <div class="attempt-row">
        <p><strong>{attemptLabel(attempt)}</strong> <span class="executor-kind">{st(attempt.execution_kind==='external'?'externalShort':'workflow')}</span> · {attempt.status} · r{attempt.node_revision}</p>
        <small>{attempt.updated_at}</small>{#if attempt.execution_kind==='external'}<p class="external-job">{st('externalJob')}: <code>{attempt.external_id}</code></p>{/if}
        <div class="attempt-actions"><button class="outline" onclick={() => openAttempt = openAttempt === attempt.attempt_id ? '' : attempt.attempt_id}>{st('inspect')}</button>
          {#if attempt.run_id}<a href={exactRunHref(attempt.run_id)}>{st('fullRun')} ↗</a>{/if}</div>
        {#if openAttempt === attempt.attempt_id}<StateAttemptEvidence attemptId={attempt.attempt_id} />{/if}
      </div>
    {/each}
    {#if data.attempts.length === 10}<p class="muted">Latest 10 attempts. The project Runs tab contains the full paginated history.</p>{/if}
    <h4>{st('historical')}</h4>
    <p class="muted">{st('trust')}</p>
    {#if !data.references.references.length}<p class="muted">{st('noHistory')}</p>{/if}
    {#each data.references.references as ref (ref.reference_id)}
      <div class="reference"><p><strong>{ref.label}</strong></p>
        <small>{ref.kind} · {ref.observed_status} · {ref.provenance_actor}</small>
        {#if ref.protection}<p class="protected">⏸ {st('protected')}</p>{/if}
        {#if ref.kind === 'run'}<p><a href={exactRunHref(ref.ref)}>{st('referenceOnly')} ↗</a></p>{/if}
        <code>{ref.ref}</code>
        {#if ref.report_sha256}<p><small>SHA-256</small> <code>{ref.report_sha256}</code></p>{/if}
      </div>
    {/each}
    {#if data.references.next_after}<p class="muted">More references are available through the paginated references API.</p>{/if}
  {/if}
</aside>
<style>
  .executor-kind{font-size:.68rem;border:1px solid var(--pico-muted-border-color,#ddd);border-radius:4px;padding:.1rem .3rem;}
  .external-job{font-size:.75rem;overflow-wrap:anywhere;}
  .node-panel { min-width:0; background:var(--pico-card-background-color,#fff); border:1px solid var(--pico-muted-border-color,#dbe3ec); border-radius:12px; padding:1rem; font-size:.84rem; overflow-wrap:anywhere; }
  .panel-heading { display:flex; justify-content:space-between; gap:.7rem; }
  h3 { font-size:1.04rem; margin:.7rem 0; }
  h4 { font-size:.88rem; margin:1.3rem 0 .45rem; }
  p { margin:.5rem 0; } code { font-size:.71rem; white-space:normal; overflow-wrap:anywhere; }
  .goal-text { white-space:pre-wrap; }
  .lanes { display:flex; gap:.35rem; margin:.45rem 0; flex-wrap:wrap; }
  .lane-link { width:auto; margin:0; padding:.25rem .5rem; font-size:.7rem; line-height:1.25; background:none;
    color:var(--pico-color,#334155); border:1px solid var(--pico-muted-border-color,#dce2ea); border-radius:6px; }
  .lane-link small { display:block; font-size:.6rem; opacity:.7; }
  .lane-link.current { border-color:var(--pico-primary,#0066cc); color:var(--pico-primary,#0066cc); }
  .badges { display:flex; flex-wrap:wrap; gap:.4rem; font-size:.72rem; }
  .badges > * { border:1px solid var(--pico-muted-border-color,#ddd); border-radius:5px; padding:.18rem .4rem; }
  .hold { border-left:3px solid #bf8427; background:color-mix(in srgb,#eab447 9%,transparent); padding:.65rem; margin:.8rem 0; }
  .criterion,.reference,.attempt-row { border-top:1px solid var(--pico-muted-border-color,#ddd); padding:.65rem 0; }
  .criterion small,.muted { color:var(--pico-muted-color,#667085); font-size:.75rem; }
  .deps,.attempt-actions { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
  button { width:auto; padding:.3rem .55rem; margin:0; font-size:.75rem; }
  .digest { display:block; opacity:.7; }
  .protected { font-size:.72rem; color:#a36d17; }
  .stale { font-size:.72rem; color:var(--pico-del-color,#ad2828); }
</style>
