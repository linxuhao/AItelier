<script lang="ts">
  import { onMount } from 'svelte';
  import { rememberProject, nt } from '../lib/navigation.svelte';
  import { authStore } from '../stores/auth';
  import { stateOverview, stateAttempts, stateRefreshProject } from '../lib/api';
  import { attemptLabel, exactRunHref, stateReadyActionCounts, type StateOverview, type StateAttempt } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  import StateGraph from './StateGraph.svelte';
  import StateRunSummary from './StateRunSummary.svelte';
  import StateNodePanel from './StateNodePanel.svelte';
  import StateAttemptEvidence from './StateAttemptEvidence.svelte';
  const { params, compact = false }: { params: { id: string; nodeKey?: string }; compact?:boolean } = $props();
  let data = $state<StateOverview | null>(null), error = $state(''), loading = $state(false);
  let selected = $state(''), tab = $state('graph'), retry = $state(0), detailRefresh = $state(0);
  let attempts = $state<StateAttempt[]>([]), next = $state<number | null>(null), runError = $state('');
  let runLoading = $state(false), openAttempt = $state('');
  let syncing = $state(false), syncNext = $state<number | null>(null), notice = $state('');
  let generation = 0, runGeneration = 0, priorProject = '', priorWanted = '';
  const allowed = $derived($authStore.permissionResolved && $authStore.canWrite);
  const readyActions = $derived(data ? stateReadyActionCounts(data.nodes, data.ready_action_counts) : { candidate_review: 0, new_attempt: 0 });
  $effect(() => {
    const project = params.id, wanted = params.nodeKey, canRead = allowed; void retry;
    const version = ++generation;
    const routeSelectionChanged = project !== priorProject || (wanted ?? '') !== priorWanted;
    priorWanted = wanted ?? '';
    if (project !== priorProject) { data = null; selected = ''; attempts = []; next = null; tab = 'graph'; notice = ''; syncNext = null; }
    priorProject = project;
    error = '';
    if (!canRead) { data = null; attempts = []; selected = ''; loading = false; return; }
    loading = true;
    stateOverview(project).then(result => {
      if (version !== generation) return;
      data = result; rememberProject(project);
      if (wanted && (routeSelectionChanged || !selected) && result.nodes.some(n => n.node_key === wanted)) selected = wanted;
      else if (!result.nodes.some(n => n.node_key === selected)) selected = result.nodes[0]?.node_key ?? '';
      detailRefresh++;
    }).catch(e => { if (version === generation) { error = String(e.message ?? e); if (e.status === 403) data = null; } })
      .finally(() => { if (version === generation) loading = false; });
    return () => { generation++; };
  });
  $effect(() => {
    const project = params.id, activeTab = tab, canRead = allowed; void detailRefresh;
    if (!canRead || activeTab !== 'runs') return;
    let cancelled = false; const version = ++runGeneration;
    runLoading = true; runError = '';
    stateAttempts(project).then(result => { if (!cancelled && version === runGeneration) { attempts = result.attempts; next = result.next_after; } })
      .catch(e => { if (!cancelled) runError = String(e.message ?? e); })
      .finally(() => { if (!cancelled) runLoading = false; });
    return () => { cancelled = true; runGeneration++; };
  });
  async function moreRuns() {
    if (!next || runLoading) return;
    const version = runGeneration; runLoading = true;
    try { const result = await stateAttempts(params.id, next); if (version === runGeneration) { attempts = [...attempts, ...result.attempts]; next = result.next_after; } }
    catch (e) { if (version === runGeneration) runError = String(e instanceof Error ? e.message : e); }
    finally { if (version === runGeneration) runLoading = false; }
  }
  async function syncRuns() {
    if (syncing) return;
    const project = params.id; const version = generation; syncing = true; notice = '';
    try {
      const result = await stateRefreshProject(project, syncNext ?? 0);
      if (version !== generation) return;
      syncNext = result.next_after;
      const errors = result.results.filter(r => r.error).length;
      notice = `${st('syncDone')}: ${result.results.length}. ${st('failures')}: ${errors}.`;
      retry++;
    } catch (e) { if (version === generation) notice = String(e instanceof Error ? e.message : e); }
    finally { syncing = false; }
  }
  // The dashboard follows persisted state without executing a reconciliation
  // or approving anything. Hidden tabs do not generate background requests.
  onMount(()=>{
    const timer=setInterval(()=>{if(allowed && !loading && !syncing && document.visibilityState==='visible')retry++;},15000);
    return ()=>clearInterval(timer);
  });
  function selectNode(key: string) { selected = key; }
</script>

<section class="state-project" class:compact>
  <nav class="breadcrumbs"><a href="#/state-projects">{st('projects')}</a><span>/</span><span>{params.id}</span></nav>
  {#if !allowed}<p role="status">{st('private')}</p>
  {:else}
    {#if error}<div class="error" role="alert"><p>{data ? st('staleView') : ''} {error}</p><button class="outline" onclick={() => retry++}>{st('retry')}</button></div>{/if}
    {#if !data && loading}<p aria-live="polite">{st('loading')}</p>{/if}
    {#if data}
      <header><div><p class="eyebrow">{st('project')}</p><h1>{data.project.title}</h1>
        <details class="source" open={!compact}><summary>{st('source')}</summary><code>{data.source.repo_path ?? st('noSource')}</code></details></div>
        <div class="toolbar"><button class="outline" disabled={loading || syncing} onclick={() => retry++}>{st('reload')}</button>
          <button disabled={syncing || loading} onclick={syncRuns}>{syncing ? st('loading') : syncNext ? st('syncMore') : st('sync')}</button></div></header>
      <p class="sync-note">{st('syncNote')}</p>
      {#if notice}<p class="notice" role="status">{notice}</p>{/if}
      <div class="metrics">
        <div><strong>{data.nodes.length}</strong><span>{st('total')}</span></div>
        <div><strong>{data.counts.VERIFIED ?? 0}</strong><span>{st('verified')}</span></div>
        <div><strong>{readyActions.candidate_review}</strong><span>{st('candidatesAwaitingReview')}</span></div>
        <div><strong>{readyActions.new_attempt}</strong><span>{st('readyForNewAttempt')}</span></div>
        <div><strong>{data.readiness_counts.held ?? 0}</strong><span>{st('held')}</span></div>
      </div>
      {#if data.policy.dispatch !== 'active'}<details class="policy" open={!compact}><summary><strong>⏸ {st('policy')}: {data.policy.dispatch}</strong></summary><p>{data.policy.reason}</p><small>{st('holdNote')}</small></details>{/if}
      <nav class="tabs" aria-label={st('overview')}>
        <button class:active={tab === 'graph'} aria-pressed={tab === 'graph'} onclick={() => tab = 'graph'}>{st('graph')}</button>
        <button class:active={tab === 'runs'} aria-pressed={tab === 'runs'} onclick={() => tab = 'runs'}>{st('runs')}</button>
        <button class:active={tab === 'evidence'} aria-pressed={tab === 'evidence'} onclick={() => tab = 'evidence'}>{st('evidence')}</button>
      </nav>
      <p class="snapshot">{st('snapshot')}: {data.observed_at} · event {data.event_seq} {loading ? ' · ' + st('loading') : ''}</p>
      {#if tab === 'graph'}
        <StateRunSummary projectId={params.id} refresh={detailRefresh} onselect={selectNode}/>
        <div class="workspace"><StateGraph nodes={data.nodes} {selected} onselect={selectNode} />
          <StateNodePanel projectId={params.id} nodeKey={selected} refresh={detailRefresh} onselect={selectNode} /></div>
      {:else if tab === 'runs'}
        <p class="sync-note">{st('complete')} · <a href={`#/runs/project/${encodeURIComponent(params.id)}`}>{nt('currentOnly')} — {nt('runs')} ↗</a></p>
        {#if runError}<p role="alert">{runError}</p><button class="outline" onclick={() => detailRefresh++}>{st('retry')}</button>{/if}
        {#if runLoading && !attempts.length}<p>{st('loading')}</p>{/if}
        {#if !attempts.length && !runLoading && !runError}<p>{st('noAttempts')}</p>{/if}
        {#each attempts as attempt (attempt.attempt_id)}
          <article class="run-card">
            <div class="run-row"><div><button class="node-link" onclick={() => { selected = attempt.node_key; tab = 'graph'; }}>{attempt.node_key}</button>
              <p><strong>{attemptLabel(attempt)}</strong> <span class="executor-kind">{st(attempt.execution_kind==='external'?'externalShort':'workflow')}</span> · {attempt.status} · r{attempt.node_revision}</p><small>{attempt.updated_at}</small>{#if attempt.execution_kind==='external'}<p class="external-job">{st('externalJob')}: <code>{attempt.external_id}</code></p>{/if}</div>
              <div class="run-actions"><button class="outline" onclick={() => openAttempt = openAttempt === attempt.attempt_id ? '' : attempt.attempt_id}>{st('inspect')}</button>
                {#if attempt.run_id}<a href={exactRunHref(attempt.run_id)}>{st('workflow')} ↗</a>{/if}</div></div>
            {#if openAttempt === attempt.attempt_id}<StateAttemptEvidence attemptId={attempt.attempt_id} />{/if}
          </article>
        {/each}
        {#if next}<button class="outline" disabled={runLoading} onclick={moreRuns}>{st('more')}</button>{/if}
      {:else}
        <div class="evidence-workspace"><div class="node-index"><h3>{st('total')}</h3>
          {#each data.nodes as node (node.node_key)}<button class:chosen={selected === node.node_key} onclick={() => selected = node.node_key}>{node.title}<small>{node.status}</small></button>{/each}
        </div><StateNodePanel projectId={params.id} nodeKey={selected} refresh={detailRefresh} onselect={selectNode} /></div>
      {/if}
    {/if}
  {/if}
</section>
<style>
  .executor-kind{font-size:.68rem;border:1px solid var(--pico-muted-border-color,#ddd);border-radius:4px;padding:.1rem .3rem;}
  .external-job{font-size:.75rem;overflow-wrap:anywhere;}
  .state-project { max-width:1480px; margin:auto; min-width:0; }
  .breadcrumbs { display:flex; justify-content:flex-start; flex-wrap:wrap; gap:.6rem; font-size:.75rem; margin:.4rem 0 1rem; overflow-wrap:anywhere; }
  header { display:flex; justify-content:space-between; gap:1rem; flex-wrap:wrap; align-items:flex-start; }
  h1 { font-size:1.65rem; margin:.1rem 0 .5rem; } .eyebrow { text-transform:uppercase; letter-spacing:.1em; font-size:.68rem; margin:0; color:var(--pico-muted-color,#667085); }
  .source { max-width:760px; margin:.4rem 0; font-size:.77rem; overflow-wrap:anywhere; } code { font-size:.72rem; white-space:normal; }
  .toolbar { display:flex; gap:.5rem; flex-wrap:wrap; }
  button { font-size:.8rem; width:auto; padding:.45rem .75rem; margin:0; }
  .metrics { display:grid; grid-template-columns:repeat(5,1fr); gap:.8rem; margin:1rem 0; }
  .metrics div { display:flex; gap:.7rem; align-items:baseline; border:1px solid var(--pico-muted-border-color,#dbe3ec); border-radius:9px; padding:.8rem; }
  .metrics strong { font-size:1.6rem; } .metrics span { font-size:.8rem; color:var(--pico-muted-color,#667085); }
  .policy { border-left:4px solid #c48a27; padding:.7rem 1rem; background:color-mix(in srgb,#ecc369 10%,transparent); font-size:.83rem; margin:1rem 0; }
  .policy p { margin:.3rem 0; }
  .tabs { display:flex; gap:.5rem; justify-content:flex-start; border-bottom:1px solid var(--pico-muted-border-color,#ddd); padding-bottom:.5rem; }
  .tabs button { background:transparent; color:var(--pico-color,#334155); border-color:transparent; }
  .tabs .active { border-color:var(--pico-primary,#0066cc); color:var(--pico-primary,#0066cc); }
  .workspace { display:grid; grid-template-columns:minmax(0,1.55fr) minmax(310px,1fr); gap:1rem; align-items:start; }
  .evidence-workspace { display:grid; grid-template-columns:minmax(180px,.7fr) minmax(0,1.8fr); gap:1rem; }
  .snapshot,.sync-note { font-size:.7rem; color:var(--pico-muted-color,#667085); margin:.45rem 0 .8rem; overflow-wrap:anywhere; }
  .notice { font-size:.8rem; border:1px solid var(--pico-muted-border-color,#ddd); padding:.5rem; }
  .error { color:#bd433c; } .node-link { border:0; padding:0; background:none; color:var(--pico-primary,#0066cc); font-size:.8rem; text-align:left; overflow-wrap:anywhere; }
  .run-card { padding:.9rem; margin:.7rem 0; }
  .run-row { display:flex; align-items:center; justify-content:space-between; gap:1rem; flex-wrap:wrap; font-size:.8rem; }
  .run-row p { margin:.35rem 0; } .run-actions { display:flex; gap:.7rem; align-items:center; flex-wrap:wrap; }
  .node-index h3 { font-size:1rem; } .node-index button { display:block; width:100%; text-align:left; background:none; color:var(--pico-color,#334155); border:1px solid var(--pico-muted-border-color,#ddd); margin:.4rem 0; }
  .node-index .chosen { border-color:var(--pico-primary,#0066cc); } .node-index small { display:block; opacity:.65; }
  .source summary{font-size:.75rem;margin-bottom:.25rem;}
  .compact .breadcrumbs,.compact .eyebrow{display:none;}
  .compact header{gap:.5rem;align-items:center;}
  .compact h1{font-size:1.3rem;line-height:1.25;margin:.1rem 0 .25rem;}
  .compact .source{margin:.15rem 0;}
  .compact .sync-note,.compact .snapshot{margin:.3rem 0;line-height:1.3;}
  .compact .metrics{margin:.6rem 0;gap:.5rem;}
  .compact .metrics div{padding:.4rem .65rem;flex-direction:row;gap:.5rem;}
  .compact .metrics strong{font-size:1.2rem;line-height:1.3;}
  .compact .metrics span{font-size:.75rem;}
  .compact .policy{padding:.45rem .7rem;margin:.55rem 0;}
  .policy summary{font-size:.8rem;line-height:1.35;}
  .compact .tabs{padding-bottom:.3rem;gap:.3rem;}
  .compact .tabs button{padding:.3rem .55rem;}
  @media(max-width:950px) { .workspace { grid-template-columns:1fr; } .metrics div { flex-direction:column; gap:.2rem; } }
  @media(max-width:600px) { .evidence-workspace { grid-template-columns:1fr; } .metrics { grid-template-columns:repeat(2,1fr); } h1 { font-size:1.35rem; } .tabs { flex-wrap:wrap; } }
</style>
