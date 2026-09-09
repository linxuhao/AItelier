<script lang="ts">
  import { onMount } from 'svelte';
  import { authStore } from '../stores/auth';
  import { runHistory } from '../lib/api';
  import { nt, type RunRow } from '../lib/navigation.svelte';
  import { st } from '../lib/stateI18n.svelte';
  import { exactRunHref, stateProjectHref } from '../lib/stateGraph';
  import { formatTime, stepLabel } from '../lib/format';
  const { params = {} }: {params?:{projectId?:string}}=$props();
  const allowed=$derived($authStore.permissionResolved && $authStore.canWrite);
  let rows=$state<RunRow[]>([]), loading=$state(false), error=$state(''), next=$state<number|null>(null), total=$state(0), retry=$state(0);
  let q=$state(''), status=$state(''), workflow=$state('');
  let filter=$state({q:'',status:'',workflow:''});
  let generation=0, priorScope='';
  $effect(()=>{
    const permit=allowed, scope=params.projectId??'', identity=$authStore.email;
    const filters={...filter};void retry;
    const version=++generation;
    const key=JSON.stringify([scope,identity,filters]);
    if(key!==priorScope){rows=[];total=0;next=null;priorScope=key;}
    error='';loading=permit;
    if(!permit){rows=[];total=0;next=null;return;}
    runHistory({...filters,state_project_id:scope}).then(result=>{
      if(version===generation){rows=result.runs;total=result.total;next=result.next_offset;}
    }).catch(e=>{if(version===generation){error=String(e.message??e);if(e.status===403){rows=[];total=0;next=null;}}})
      .finally(()=>{if(version===generation)loading=false;});
    return ()=>{generation++;};
  });
  onMount(()=>{const timer=setInterval(()=>{if(allowed && !loading && document.visibilityState==='visible')retry++;},15000);return ()=>clearInterval(timer);});
  function activeStepText(run: RunRow): string {
    if (run.status !== 'running' || !run.active_step) return '';
    const id = String(run.active_step.step_id || '');
    if (!id) return '';
    const label = stepLabel(id);
    const item = String(run.active_step.loop_item || '');
    return `${id}${label !== id ? ` · ${label}` : ''}${item ? ` · ${item}` : ''}`;
  }
  async function more(){
    if(next===null || loading)return;
    const version=generation;loading=true;
    try{const result=await runHistory({...filter,state_project_id:params.projectId??'',offset:next});
      if(version===generation){const seen=new Set(rows.map(r=>r.id));rows=[...rows,...result.runs.filter(r=>!seen.has(r.id))];next=result.next_offset;total=result.total;}}
    catch(e){if(version===generation)error=String(e instanceof Error?e.message:e);}
    finally{if(version===generation)loading=false;}
  }
</script>
<section class="runs-page" aria-label={nt('runs')}>
  <header><div><h1>{nt('runs')}</h1><p>{nt('runIntro')}</p></div><a href="#/repos">{nt('repoTools')} ↗</a></header>
  {#if !allowed}<p role="status">{st('private')}</p>{:else}
    {#if params.projectId}<p><a href={stateProjectHref(params.projectId)}>{params.projectId}</a> · {nt('currentOnly')}</p>{/if}
    <form class="filters" onsubmit={e=>{e.preventDefault();filter={q:q.trim(),status,workflow:workflow.trim()};}}>
      <label class="query">{nt('search')}<input type="search" bind:value={q} maxlength="200" placeholder={nt('search')} /></label>
      <label>{nt('status')}<select bind:value={status}><option value="">{nt('all')}</option>{#each ['pending','running','paused','completed','failed'] as s(s)}<option value={s}>{s.toUpperCase()}</option>{/each}</select></label>
      <label>{nt('workflow')}<input bind:value={workflow} maxlength="128" placeholder="coding_impl / investigate" /></label>
      <button type="submit" disabled={loading}>{nt('search')}</button><button type="button" class="outline" disabled={loading} onclick={()=>retry++}>{nt('reload')}</button>
    </form>
    <p class="note">{total} {nt('results')} · {nt('countNote')}</p>
    {#if error}<div role="alert"><p>{rows.length?nt('stale'):''} {error}</p><button class="outline" onclick={()=>retry++}>{nt('retry')}</button></div>{/if}
    {#if loading && !rows.length}<p role="status">{st('loading')}</p>{/if}
    {#if !loading && !error && !rows.length}<p>{nt('emptyRuns')}</p>{/if}
    <div class="run-grid">
      {#each rows as run(run.id)}
        {@const activeStep = activeStepText(run)}
        <article class="history-run" data-run-id={run.id}>
          <div class="run-top"><a class="run-name" href={exactRunHref(run.id)}>{run.config_name}</a><span class="run-badge {run.status}">{run.status.toUpperCase()}</span></div>
          <h2><a href={exactRunHref(run.id)}>{run.execution_name}</a></h2>
          <p class="identity"><code>{run.id}</code> · graph v{run.graph_version??'?'}</p>
          {#if run.state_project_id}<p><a href={stateProjectHref(run.state_project_id,run.state_node_key??undefined)}>{run.state_project_id} / {run.state_node_key}</a></p>{/if}
          <p class="meta">{#if activeStep}<strong>{nt('runningStep')}:</strong> {activeStep} · {/if}{formatTime(run.updated_at??run.created_at)}</p>
          <div class="links"><a href={exactRunHref(run.id)}>{nt('open')} →</a>{#if run.project_id}<a href={`#/projects/${encodeURIComponent(run.project_id)}`}>{nt('execution')} ↗</a>{/if}</div>
        </article>
      {/each}
    </div>
    {#if next!==null}<button class="outline" disabled={loading} onclick={more}>{nt('more')}</button>{/if}
  {/if}
</section>
<style>
  .runs-page{max-width:1280px;margin:auto;min-width:0;}header{display:flex;gap:1rem;justify-content:space-between;align-items:start;flex-wrap:wrap;}h1{font-size:1.65rem;margin:.2rem 0;}header p{font-size:.85rem;}header a{font-size:.8rem;}
  .filters{display:flex;align-items:end;flex-wrap:wrap;gap:.6rem;}label{font-size:.75rem;margin:0;}.query{flex:1;min-width:170px;}input,select,button{font-size:.8rem;margin:.2rem 0 0;padding:.45rem .7rem;}button{width:auto;}.note,.meta,.identity{font-size:.75rem;overflow-wrap:anywhere;}.note,.meta{color:var(--pico-muted-color,#64748b);}
  .run-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,360px),1fr));gap:.8rem;margin:.8rem 0;}.history-run{padding:1rem;margin:0;min-width:0;border:1px solid var(--pico-muted-border-color,#dbe3ec);border-radius:10px;box-shadow:none;}
  .run-top,.links{display:flex;justify-content:space-between;gap:.7rem;flex-wrap:wrap;font-size:.8rem;}.run-name{font-weight:650;overflow-wrap:anywhere;}h2{font-size:1rem;margin:.7rem 0;overflow-wrap:anywhere;}h2 a{color:inherit;}.history-run p{margin:.45rem 0;font-size:.8rem;overflow-wrap:anywhere;}code{font-size:.7rem;white-space:normal;}
  .run-badge{font-size:.68rem;padding:.15rem .45rem;border-radius:5px;background:#e8edf3;color:#25384d;}.running{background:#e0ecff;color:#194980;}.paused{background:#fff0ce;color:#644509;}.failed{background:#fde5e5;color:#7b2323;}.completed{background:#e0ecff;color:#194980;}
  @media(max-width:600px){.filters label{max-width:100%;}header a{margin-bottom:.5rem;}}
</style>
