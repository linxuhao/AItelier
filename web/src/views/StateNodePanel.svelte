<script lang="ts">
  import { stateNode } from '../lib/api';
  import { exactRunHref, stateProjectHref, type StateNodeDetail } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  import StateAttemptEvidence from './StateAttemptEvidence.svelte';
  interface Props { projectId: string; nodeKey: string; refresh?: number; onselect: (key: string) => void }
  const { projectId, nodeKey, refresh = 0, onselect }: Props = $props();
  let data = $state<StateNodeDetail | null>(null);
  let error = $state('');
  let retry = $state(0);
  let openAttempt = $state('');
  let previousTarget = '';
  $effect(() => {
    const project = projectId, node = nodeKey; void refresh; void retry;
    let cancelled = false; data = null; error = '';
    const target = project + '/' + node;
    if (target !== previousTarget) openAttempt = '';
    previousTarget = target;
    if (node) stateNode(project, node).then(value => { if (!cancelled) data = value; })
      .catch(e => { if (!cancelled) error = String(e.message ?? e); });
    return () => { cancelled = true; };
  });
</script>
<aside class="node-panel" aria-label={st('detail')}>
  {#if !nodeKey}<p>{st('select')}</p>
  {:else if error}<p role="alert">{error}</p><button class="outline" onclick={() => retry++}>{st('retry')}</button>
  {:else if !data}<p aria-live="polite">{st('loading')}</p>
  {:else}
    <div class="panel-heading"><code>{nodeKey}</code><a href={stateProjectHref(projectId, nodeKey)} aria-label="Permalink">↗</a></div>
    <h3>{data.node.goal.split('\n')[0]}</h3>
    <div class="badges"><strong>{data.node.status}</strong><span>{st(data.node.readiness)}</span><span>r{data.node.revision}</span></div>
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
        <p><strong>{attempt.workflow}</strong> · {attempt.status} · r{attempt.node_revision}</p>
        <small>{attempt.updated_at}</small>
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
  .node-panel { min-width:0; background:var(--pico-card-background-color,#fff); border:1px solid var(--pico-muted-border-color,#dbe3ec); border-radius:12px; padding:1rem; font-size:.84rem; overflow-wrap:anywhere; }
  .panel-heading { display:flex; justify-content:space-between; gap:.7rem; }
  h3 { font-size:1.04rem; margin:.7rem 0; }
  h4 { font-size:.88rem; margin:1.3rem 0 .45rem; }
  p { margin:.5rem 0; } code { font-size:.71rem; white-space:normal; overflow-wrap:anywhere; }
  .goal-text { white-space:pre-wrap; }
  .badges { display:flex; flex-wrap:wrap; gap:.4rem; font-size:.72rem; }
  .badges > * { border:1px solid var(--pico-muted-border-color,#ddd); border-radius:5px; padding:.18rem .4rem; }
  .hold { border-left:3px solid #bf8427; background:color-mix(in srgb,#eab447 9%,transparent); padding:.65rem; margin:.8rem 0; }
  .criterion,.reference,.attempt-row { border-top:1px solid var(--pico-muted-border-color,#ddd); padding:.65rem 0; }
  .criterion small,.muted { color:var(--pico-muted-color,#667085); font-size:.75rem; }
  .deps,.attempt-actions { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
  button { width:auto; padding:.3rem .55rem; margin:0; font-size:.75rem; }
  .digest { display:block; opacity:.7; }
  .protected { font-size:.72rem; color:#a36d17; }
</style>
