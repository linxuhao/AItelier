<script lang="ts">
  import { authStore } from '../stores/auth';
  import { getRunDetail } from '../lib/api';
  import { nt } from '../lib/navigation.svelte';
  import { st } from '../lib/stateI18n.svelte';
  import PipelineGraph from './PipelineGraph.svelte';
  import RunStateLinks from './RunStateLinks.svelte';
  const { params }: {params: {runId: string}} = $props();
  let run = $state<Record<string, any> | null>(null), error = $state(''), retry = $state(0);
  const allowed = $derived($authStore.permissionResolved && $authStore.canWrite);
  $effect(() => {
    const id = params.runId, canRead = allowed; void retry;
    let cancelled = false; run = null; error = '';
    if (canRead) getRunDetail(id).then(result => {
      if (cancelled) return;
      if (result.id !== id) { error = 'An exact run ID is required; project aliases are not shown as pinned runs.'; return; }
      run = result;
    }).catch(e => { if (!cancelled) error = String(e.message ?? e); });
    return () => { cancelled = true; };
  });
</script>
<section class="state-run">
  <a href="#/runs">← {nt('runs')}</a>
  {#if !allowed}<p>{st('private')}</p>
  {:else if error}<p role="alert">{error}</p><button class="outline" onclick={() => retry++}>{st('retry')}</button>
  {:else if !run}<p>{st('loading')}</p>
  {:else}
    <header><div><h1>{st('run')} · {run.config_name ?? run.graph_name}</h1><p><code>{params.runId}</code> · {run.status}</p></div>
      <button class="outline" onclick={() => retry++}>{st('reload')}</button></header>
    <RunStateLinks runId={params.runId} />
    <p class="note">{st('complete')}</p>
    <nav><a href={`#/projects/${encodeURIComponent(run.project_id)}/trace/${encodeURIComponent(params.runId)}`}>{st('trace')} ↗</a>
      <a href={`#/projects/${encodeURIComponent(run.project_id)}`}>{st('dashboard')} ↗</a></nav>
    <PipelineGraph config={run.config_name ?? run.graph_name} runId={params.runId}
      runSteps={run.steps ?? []} cacheByStep={run.cache_stats_by_step ?? {}} />
  {/if}
</section>
<style>
  .state-run { max-width:1280px; margin:auto; min-width:0; }
  header { display:flex; justify-content:space-between; align-items:flex-start; gap:1rem; flex-wrap:wrap; margin:1rem 0; }
  h1 { font-size:1.3rem; margin:.4rem 0; } p { font-size:.85rem; overflow-wrap:anywhere; } code { font-size:.8rem; }
  nav { display:flex; gap:1rem; justify-content:flex-start; flex-wrap:wrap; margin:1rem 0; font-size:.8rem; }
  button { width:auto; font-size:.8rem; padding:.45rem .75rem; } .note { color:var(--pico-muted-color,#667085); }
</style>
