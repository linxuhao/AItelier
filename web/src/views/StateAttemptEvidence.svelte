<script lang="ts">
  import { stateAttemptDetail } from '../lib/api';
  import { exactRunHref, type AttemptDetail } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  const { attemptId }: { attemptId: string } = $props();
  let data = $state<AttemptDetail | null>(null);
  let error = $state('');
  let reload = $state(0);
  $effect(() => {
    const id = attemptId; void reload;
    let cancelled = false; data = null; error = '';
    stateAttemptDetail(id).then(value => { if (!cancelled) data = value; })
      .catch(e => { if (!cancelled) error = String(e.message ?? e); });
    return () => { cancelled = true; };
  });
</script>
<div class="attempt-evidence">
  {#if error}<p role="alert">{error}</p><button class="outline" onclick={() => reload++}>{st('retry')}</button>
  {:else if !data}<p aria-live="polite">{st('loading')}</p>
  {:else}
    <p><strong>{st('artifact')}:</strong> <code>{data.attempt.artifact_ref ?? '—'}</code></p>
    {#if data.attempt.execution_kind==='external'}
      <div class="external-provenance"><p><strong>{st('externalShort')}: {data.attempt.harness}</strong></p>
      <p>{st('externalJob')}: <code>{data.attempt.external_id}</code></p>
      <p>{st('reporter')}: <code>{data.attempt.reporting_actor}</code></p>
      <p>{st('externalNoRun')}</p></div>
    {:else if data.attempt.run_id}<p><a href={exactRunHref(data.attempt.run_id)}>{st('workflow')} → <code>{data.attempt.run_id}</code></a></p>
    {:else}<p>{st('noRun')}</p>{/if}
    {#if data.external_observations?.length}
      <h5>{st('externalObservations')}</h5>
      {#each data.external_observations as observation(observation.observation_id)}
        <details class="external-observation"><summary>{observation.status.toUpperCase()} · v{observation.version} · {st('quiescent')}: {observation.quiescent?'✓':'—'}</summary>
          <p>{observation.detail}</p><p><code>{observation.report_ref}</code></p><p>SHA-256 <code>{observation.report_sha256}</code></p>
          <p>{st('reporter')}: <code>{observation.actor}</code></p><p>Context <code>{observation.context_hash}</code></p>
        </details>
      {/each}
      {#if data.external_observations_truncated}<p>{st('externalTruncated')}</p>{/if}
    {/if}
    <p class="note">{st('trust')}</p>
    <h5>{st('reports')}</h5>
    {#if !data.evidence.length}<p>{st('noEvidence')}</p>{/if}
    {#each data.evidence as item (item.evidence_id)}
      <details class="evidence-row">
        <summary><span class:passed={item.verdict === 'pass'} class:failed={item.verdict !== 'pass'}>{item.verdict.toUpperCase()}</span> · {item.criterion_id} · {item.reviewer}</summary>
        <p>{item.detail}</p><p><code>{item.report_ref}</code></p>
        <p>SHA-256 <code>{item.report_sha256}</code></p>
        <p>{st('artifact')}: <code>{item.artifact_ref}</code></p>
      </details>
    {/each}
    {#if data.evidence_truncated}<p class="note">Latest 200 observations shown. Read full history using the evidence API.</p>{/if}
    <p class="note">Acceptance receipts: {data.receipts.length}. Historical receipts may have been invalidated; the current node state remains authoritative.</p>
    {#each data.receipts as receipt,i(i)}
      {#if typeof receipt.provenance_json==='string' && receipt.provenance_json!=='{}'}
        <details class="receipt-provenance"><summary>{st('acceptanceProvenance')}</summary><pre>{receipt.provenance_json}</pre></details>
      {/if}
    {/each}
  {/if}
</div>
<style>
  .external-provenance{padding:.5rem;border:1px solid var(--pico-muted-border-color,#ddd);border-radius:6px;}
  pre{font-size:.7rem;white-space:pre-wrap;word-break:break-word;}
  .attempt-evidence { font-size:.8rem; padding:.8rem; border-left:3px solid var(--pico-primary,#3877b5); margin:.6rem 0; }
  p { margin:.45rem 0; overflow-wrap:anywhere; }
  code { font-size:.72rem; white-space:normal; overflow-wrap:anywhere; }
  h5 { font-size:.9rem; margin:1rem 0 .5rem; }
  .note { color:var(--pico-muted-color,#64748b); font-size:.75rem; }
  .evidence-row { padding:.5rem 0; border-bottom:1px solid var(--pico-muted-border-color,#ddd); }
  .passed { color:#14744d; font-weight:650; }
  .failed { color:#b74237; font-weight:650; }
  button { padding:.4rem .7rem; width:auto; }
</style>
