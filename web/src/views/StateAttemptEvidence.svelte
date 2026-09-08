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
    {#if data.attempt.run_id}<p><a href={exactRunHref(data.attempt.run_id)}>{st('workflow')} → <code>{data.attempt.run_id}</code></a></p>
    {:else}<p>{st('noRun')}</p>{/if}
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
  {/if}
</div>
<style>
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
