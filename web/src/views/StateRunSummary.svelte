<script lang="ts">
  import { authStore } from '../stores/auth';
  import { stateRunSummary } from '../lib/api';
  import { exactRunHref, type ProjectRunSummary } from '../lib/stateGraph';
  import { elapsedLabel } from '../lib/format';
  import { st } from '../lib/stateI18n.svelte';
  const { projectId, refresh = 0, onselect }: {projectId:string;refresh?:number;onselect?:(nodeKey:string)=>void} = $props();
  let data=$state<ProjectRunSummary|null>(null), error=$state(''), loading=$state(false);
  let generation=0, previousIdentity='';
  const allowed=$derived($authStore.permissionResolved && $authStore.canWrite);
  $effect(()=>{
    const id=projectId, actor=$authStore.email, canRead=allowed;void refresh;
    const identity=JSON.stringify([id,actor]), version=++generation;
    if(identity!==previousIdentity){data=null;previousIdentity=identity;}
    error='';loading=canRead;
    if(!canRead){data=null;loading=false;return;}
    stateRunSummary(id).then(result=>{if(version!==generation)return;if(result.project_id!==id)throw new Error(st('summaryWrongProject'));data=result;})
      .catch(e=>{if(version===generation){data=null;error=String(e.message??e);}})
      .finally(()=>{if(version===generation)loading=false;});
    return ()=>{generation++;};
  });
  function number(value:number|null|undefined):string {
    return value==null?'—':new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(value);
  }
  function count(kind:'running'|'finished'|'failed'):string {
    if(!data)return '—';
    return (data.counts.unavailable?'≥ ':'')+number(data.execution_counts[kind]);
  }
  const tokenTitle=$derived(data ? `${st('summaryTokenDefinition')} ${st('summaryInput')}: ${data.usage.prompt_tokens??'—'}; ${st('summaryOutput')}: ${data.usage.completion_tokens??'—'}` : st('summaryTokenDefinition'));
  const cacheTitle=$derived(data ? `${st('summaryCacheDefinition')} ${data.usage.cache_hit_tokens??'—'} / ${data.usage.cache_covered_tokens}. ${st('summaryCacheTurns')}: ${data.usage.cache_reported_turns}/${data.usage.usage_turns}` : st('summaryCacheDefinition'));
</script>

{#if allowed}
<section class="state-run-summary" aria-label={st('summaryHeading')} aria-busy={loading}>
  <div class="summary-top"><h2>{st('summaryHeading')}</h2><small>{st('summaryScope')}</small></div>
  <dl class="run-summary-metrics">
    <div data-metric="total" title={st('summaryTotalDefinition')}><dt>{st('summaryTotal')}</dt><dd>{number(data?.execution_counts.total)}</dd></div>
    <div data-metric="running" title={st('summaryRunningDefinition')}><dt>{st('summaryRunning')}</dt><dd>{count('running')}</dd></div>
    <div data-metric="finished" title={st('summaryFinishedDefinition')}><dt>{st('summaryFinished')}</dt><dd>{count('finished')}</dd></div>
    <div data-metric="failed"><dt>{st('summaryFailed')}</dt><dd>{count('failed')}</dd></div>
    <div data-metric="tokens" title={tokenTitle}><dt>Tokens</dt><dd>{number(data?.usage.total_tokens)}{data?.usage.partial && data?.usage.total_tokens!=null ? '*' : ''}</dd></div>
    <div data-metric="cache" title={cacheTitle}><dt>Cache</dt><dd>{data?.usage.cache_hit_ratio==null?'—':(data.usage.cache_hit_ratio*100).toFixed(1)+'%'}</dd>
      {#if data?.usage.cache_hit_tokens!=null}<small>{number(data.usage.cache_hit_tokens)} {st('summaryHits')}</small>{/if}</div>
  </dl>
  {#if error}<p class="summary-error" role="alert">{st('summaryError')}: {error}</p>
  {:else if data}
    {#if data.running_runs.length}
      <ul class="running-runs" aria-label={st('summaryRunningList')}>
        {#each data.running_runs as run(run.run_id)}
          <li><a href={exactRunHref(run.run_id)} title={`${run.workflow} · ${run.run_id}\n${run.node_keys.join(', ')}${run.current_node?' · '+run.current_node:''}`}>
            <span class="running-dot" aria-hidden="true"></span><strong>{run.workflow}</strong>
            <span class="run-node">{run.node_keys.join(', ')}</span><code>{run.run_id.slice(0,8)}</code>
            {#if elapsedLabel(run.started_at,data.observed_at)}<span class="elapsed" title={st('summaryElapsed')}>⏱ {elapsedLabel(run.started_at,data.observed_at)}</span>{/if}<span aria-hidden="true">↗</span>
          </a></li>
        {/each}
      </ul>
    {:else if !data.counts.unavailable}<p class="summary-note">{st('summaryNoRunning')}</p>{/if}
    {#if data.running_external?.length}
      <p class="external-caption">{st('summaryExternalRunning')} · {data.running_external.length}</p>
      <ul class="external-running" aria-label={st('summaryExternalRunning')}>
        {#each data.running_external as job(job.attempt_id)}
          <li title={`${job.harness} · ${job.external_id}\n${job.node_key} r${job.node_revision}\n${st('reporter')}: ${job.reporting_actor}\n${st('summaryLastReport')}: ${job.last_report_at ?? st('summaryExternalNoReport')}`}>
            <span class="external-dot" aria-hidden="true"></span><strong>{job.harness}</strong>
            {#if onselect}<button class="node-jump" onclick={()=>onselect(job.node_key)}>{job.node_key}</button>
            {:else}<span class="run-node">{job.node_key}</span>{/if}<span class="external-state">{job.status}</span>
            <code class="external-ref">{job.external_id}</code>
            {#if elapsedLabel(job.created_at,data.observed_at)}<span class="elapsed" title={st('summaryElapsed')}>⏱ {elapsedLabel(job.created_at,data.observed_at)}</span>{/if}
            <small title={st('summaryLastReport')}>{job.last_report_at ? '↩ ' + elapsedLabel(job.last_report_at,data.observed_at) : st('summaryExternalNoReport')}</small>
          </li>
        {/each}
      </ul>
      <p class="summary-note">{st('summaryExternalReported')}</p>
    {/if}
    {#if data.counts.unavailable}<p class="summary-note" role="status">{st('summaryUnavailable')}: {data.counts.unavailable}. {st('summaryLowerBounds')}</p>{/if}
    {#if data.counts.total && (data.usage.partial || data.usage.cache_reported_turns!==data.usage.usage_turns || data.usage.usage_errors)}
      <p class="summary-note">{st('summaryCoverage')}: {data.usage.runs_with_token_usage}/{data.counts.total}. {st('summaryUnknown')}</p>
    {/if}
    {#if data.external_attempts_excluded}<p class="summary-note">{st('summaryExternal')}: {data.external_attempts_excluded}.</p>{/if}
  {:else if loading}<p class="summary-note" role="status">{st('loading')}</p>{/if}
</section>
{/if}
<style>
  .state-run-summary{min-width:0;margin:.45rem 0 .75rem;padding:.5rem .65rem;border:1px solid var(--pico-muted-border-color,#dbe3ec);border-radius:8px;}
  .summary-top{display:flex;align-items:baseline;gap:.65rem;flex-wrap:wrap;margin-bottom:.35rem;}
  .summary-top h2{font-size:.76rem;line-height:1.3;margin:0;}.summary-top small{font-size:.65rem;line-height:1.3;color:var(--pico-muted-color,#64748b);}
  .run-summary-metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:.55rem;margin:0;}
  .run-summary-metrics>div{min-width:0;display:flex;align-items:baseline;flex-wrap:wrap;gap:.2rem .45rem;}
  dt{font-size:.7rem;color:var(--pico-muted-color,#64748b);font-weight:500;line-height:1.3;}dd{font-size:1rem;font-weight:650;line-height:1.3;margin:0;font-variant-numeric:tabular-nums;white-space:nowrap;}
  .run-summary-metrics small{font-size:.6rem;line-height:1.3;}
  .running-runs{list-style:none;display:flex;flex-wrap:wrap;gap:.3rem;margin:.4rem 0 0;padding:0;max-height:6.5rem;overflow:auto;}
  .running-runs li{list-style:none;min-width:0;max-width:100%;padding:0;margin:0;}
  .running-runs a{display:flex;align-items:center;gap:.4rem;max-width:100%;border:1px solid var(--pico-muted-border-color,#dbe3ec);border-radius:5px;padding:.3rem .5rem;line-height:1.3;font-size:.75rem;text-decoration:none;}
  .running-runs a:hover,.running-runs a:focus-visible{border-color:var(--pico-primary,#0066cc);text-decoration:underline;}
  .running-runs strong{font-weight:600;overflow-wrap:anywhere;}.run-node{max-width:20rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.65rem;color:var(--pico-muted-color,#64748b);}
  .running-dot{width:.4rem;height:.4rem;border-radius:50%;background:var(--pico-primary,#0066cc);flex:none;}code{font-size:.63rem;white-space:nowrap;}
  .external-caption{font-size:.65rem;line-height:1.35;margin:.45rem 0 0;color:var(--pico-muted-color,#64748b);}
  .external-running{list-style:none;display:flex;flex-wrap:wrap;gap:.3rem;margin:.25rem 0 0;padding:0;max-height:6.5rem;overflow:auto;}
  .external-running li{list-style:none;min-width:0;max-width:100%;display:flex;align-items:center;gap:.4rem;padding:.3rem .5rem;margin:0;
    border:1px dashed var(--pico-muted-border-color,#dbe3ec);border-radius:5px;line-height:1.3;font-size:.75rem;}
  .external-running strong{font-weight:600;overflow-wrap:anywhere;}
  .external-dot{width:.4rem;height:.4rem;border-radius:50%;background:var(--pico-muted-color,#64748b);flex:none;}
  .external-state{font-size:.63rem;border:1px solid var(--pico-muted-border-color,#dbe3ec);border-radius:4px;padding:.05rem .25rem;}
  .external-ref{max-width:11rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .node-jump{border:0;padding:0;margin:0;width:auto;background:none;color:var(--pico-primary,#0066cc);font-size:.65rem;line-height:1.3;
    max-width:20rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .node-jump:hover,.node-jump:focus-visible{text-decoration:underline;}
  .external-running small{font-size:.6rem;color:var(--pico-muted-color,#64748b);white-space:nowrap;}
  .elapsed{font-size:.63rem;white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--pico-muted-color,#64748b);}
  .summary-note,.summary-error{font-size:.65rem;line-height:1.35;margin:.3rem 0 0;overflow-wrap:anywhere;}.summary-note{color:var(--pico-muted-color,#64748b);}.summary-error{color:var(--pico-del-color,#ad2828);}
  @media(max-width:680px){.run-summary-metrics{grid-template-columns:repeat(3,minmax(0,1fr));row-gap:.4rem;}.run-node{max-width:7rem;}.running-runs a{flex-wrap:wrap;}.running-runs{max-height:9rem;}
    .external-running li{flex-wrap:wrap;}.external-ref{max-width:7rem;}.external-running{max-height:9rem;}.node-jump{max-width:7rem;}}
</style>
