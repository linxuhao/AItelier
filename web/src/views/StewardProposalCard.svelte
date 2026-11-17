<script lang="ts">
  import { onMount } from 'svelte';
  import { ApiError, approveStewardProposal, declineStewardProposal, stewardProposalImpact,
    type StewardImpact, type StewardProposal } from '../lib/api';
  import { impactIsEmpty } from '../lib/steward';
  import { stateProjectHref } from '../lib/stateGraph';
  import { t } from '../lib/i18n.svelte';
  const { proposal, onupdate, onreload, onrevise }: {
    proposal: StewardProposal;
    /** A newer form of this row: the decision's answer, or the current row a 409 carried. */
    onupdate: (row: StewardProposal) => void;
    /** Re-read the list: a 409 that carried no row. */
    onreload: () => void;
    /** "Change it": the steward is asked in the conversation; it proposes again. */
    onrevise: (row: StewardProposal) => void;
  } = $props();
  let busy = $state(false), error = $state(''), changed = $state(false), impactMoved = $state(false),
    confirmStop = $state(false);
  // What stopping would interrupt NOW, read when the card renders: the recorded
  // impact in the payload is what the steward saw when it proposed.
  let impact = $state<StewardImpact | null>(null), impactState = $state<'loading' | 'ready' | 'failed'>('loading');
  const isStop = $derived(proposal.kind === 'stop_driver');
  const recorded = $derived(proposal.payload?.impact as Partial<StewardImpact> | undefined);
  // Unknown (the read failed) is treated like "something would be interrupted":
  // the owner ticks the box, and the server checks again either way.
  const needsConfirm = $derived(isStop && (impactState === 'failed' || (impactState === 'ready' && !impactIsEmpty(impact))));
  const approveBlocked = $derived(isStop && (impactState === 'loading' || (needsConfirm && !confirmStop)));
  onMount(() => {
    if (proposal.kind !== 'stop_driver' || proposal.status !== 'open') return;
    stewardProposalImpact(proposal.proposal_id)
      .then(result => { impact = result; impactState = 'ready'; })
      .catch(() => { impactState = 'failed'; });
  });
  function isRow(value: unknown): value is StewardProposal {
    return !!value && typeof value === 'object' && (value as StewardProposal).proposal_id === proposal.proposal_id;
  }
  /** The impact the approve endpoint measured again at the click, or null when this
   *  refusal is something else. The server refuses a stop whose owner has not
   *  confirmed work the CARD had not seen (api/steward_routers.py, 422
   *  confirm_interrupt_required) and sends that measurement along; without this the
   *  owner would only see a status text and could never reach the tick box. */
  function freshImpact(detail: unknown): StewardImpact | null {
    if (!detail || typeof detail !== 'object') return null;
    const body = detail as {code?: unknown; impact?: unknown};
    if (body.code !== 'confirm_interrupt_required') return null;
    return body.impact && typeof body.impact === 'object' ? {running_subagents: [], unsettled_attempts: [],
      ...(body.impact as Partial<StewardImpact>)} : null;
  }
  async function decide(action: 'approve' | 'decline') {
    if (busy) return;
    busy = true; error = ''; changed = false; impactMoved = false;
    try {
      const row = action === 'approve'
        ? await approveStewardProposal(proposal.proposal_id, proposal.version, isStop && needsConfirm && confirmStop)
        : await declineStewardProposal(proposal.proposal_id, proposal.version);
      onupdate(row);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // The version or status moved under the owner: show the current row and say so.
        changed = true; confirmStop = false;
        if (isRow(e.detail)) onupdate(e.detail); else onreload();
      } else {
        // Work started between rendering this card and pressing Approve: the approve
        // endpoint measured the impact again and refused until the owner confirms.
        const fresh = e instanceof ApiError && e.status === 422 ? freshImpact(e.detail) : null;
        if (fresh) {
          impact = fresh; impactState = 'ready'; confirmStop = false; impactMoved = true;
        } else error = String(e instanceof Error ? e.message : e);
      }
    } finally { busy = false; }
  }
  function show(value: unknown): string {
    return typeof value === 'string' ? value : JSON.stringify(value, null, 1);
  }
</script>
{#snippet impactList(value: Partial<StewardImpact> | null | undefined)}
  {#if impactIsEmpty(value)}<span>{t('steward.impactNone')}</span>
  {:else}
    <ul>
      {#each value?.running_subagents ?? [] as item (item.session_id)}<li>{t('steward.impactSubagent')} <code>{item.session_id}</code></li>{/each}
      {#each value?.unsettled_attempts ?? [] as item (item.attempt_id)}<li>{t('steward.impactAttempt')} <code>{item.attempt_id}</code></li>{/each}
    </ul>
  {/if}
{/snippet}
<article class="proposal" data-proposal={proposal.proposal_id} data-status={proposal.status}>
  <header>
    <strong>{t('steward.kind.' + proposal.kind)}</strong>
    <span class="status status-{proposal.status}">{t('steward.status.' + proposal.status)}</span>
  </header>
  <dl>
    {#if proposal.payload?.project_id}
      <dt>{t('steward.project')}</dt><dd><a href={stateProjectHref(String(proposal.payload.project_id))}>{proposal.payload.project_id}</a>{#if proposal.payload.title} · {proposal.payload.title}{/if}</dd>
    {/if}
    {#if proposal.payload?.goal}<dt>{t('steward.goal')}</dt><dd>{proposal.payload.goal}</dd>{/if}
    {#if Array.isArray(proposal.payload?.seed_nodes) && proposal.payload.seed_nodes.length}
      <dt>{t('steward.seedNodes')}</dt>
      <dd><ul>{#each proposal.payload.seed_nodes as node, i (i)}<li>{node.key ? `${node.key}: ${node.goal ?? ''}` : node.title ?? node.node_key ?? show(node)}</li>{/each}</ul></dd>
    {/if}
    {#if proposal.reason}<dt>{t('steward.reason')}</dt><dd>{proposal.reason}</dd>{/if}
  </dl>
  {#if isStop}
    <section class="impact" aria-label={t('steward.impactHeading')}>
      <strong>{t('steward.impactHeading')}</strong>
      <div>{t('steward.impactRecorded')} {@render impactList(recorded)}</div>
      {#if proposal.status === 'open'}
        <div data-impact-now>{t('steward.impactNow')}
          {#if impactState === 'loading'}<span aria-busy="true">{t('steward.impactLoading')}</span>
          {:else if impactState === 'failed'}<span>{t('steward.impactUnknown')}</span>
          {:else}{@render impactList(impact)}{/if}
        </div>
      {/if}
    </section>
  {/if}
  {#if changed}<p class="changed" role="status">{t('steward.changed')}</p>{/if}
  {#if impactMoved}<p class="changed" role="status">{t('steward.impactMoved')}</p>{/if}
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if proposal.status === 'open'}
    {#if needsConfirm}
      <label class="confirm"><input type="checkbox" bind:checked={confirmStop} /> {t('steward.stopConfirm')}</label>
    {/if}
    <div class="actions">
      <button type="button" disabled={busy || approveBlocked} onclick={() => void decide('approve')}>{t('steward.approve')}</button>
      <button type="button" class="secondary outline" disabled={busy} onclick={() => void decide('decline')}>{t('steward.decline')}</button>
      <button type="button" class="outline" disabled={busy} onclick={() => onrevise(proposal)}>{t('steward.change')}</button>
    </div>
  {:else if proposal.status === 'executing'}
    <p class="executing" aria-busy="true">{t('steward.executing')}</p>
  {:else}
    <div class="result">
      {#if proposal.decided_by}<small>{proposal.decided_by}{#if proposal.decided_at} · {new Date(proposal.decided_at).toLocaleString()}{/if}</small>{/if}
      {#if proposal.result !== null && proposal.result !== undefined}<pre data-result>{show(proposal.result)}</pre>{/if}
    </div>
  {/if}
</article>
<style>
  .proposal{margin:0 0 10px;padding:12px 14px;border:1px solid var(--pico-muted-border-color);border-radius:8px;font-size:13px;}
  header{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:0 0 6px;padding:0;background:none;border:0;}
  .status{font-size:11px;padding:1px 6px;border-radius:5px;background:var(--pico-muted-border-color);}
  .status-open{background:var(--pico-primary);color:var(--pico-primary-inverse);}
  .status-failed{background:var(--pico-del-color);color:#fff;}
  dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:2px 10px;margin:0 0 8px;}
  dt{color:var(--pico-muted-color);font-weight:normal;}dd{margin:0;overflow-wrap:anywhere;}
  ul{margin:0;padding-left:18px;}li{list-style:disc;margin:0;}
  .impact{margin:0 0 8px;padding:8px;border-radius:6px;background:var(--pico-card-sectioning-background-color);}
  .confirm{display:flex;gap:6px;align-items:center;font-size:12px;margin:0 0 8px;}.confirm input{margin:0;}
  .actions{display:flex;gap:8px;flex-wrap:wrap;}.actions button{width:auto;margin:0;padding:5px 12px;font-size:13px;}
  .changed{color:var(--pico-primary);margin:0 0 8px;}.error{color:var(--pico-del-color);margin:0 0 8px;}
  .executing{margin:0;color:var(--pico-muted-color);}
  pre{margin:4px 0 0;padding:6px 8px;font-size:11px;white-space:pre-wrap;overflow-wrap:anywhere;}
  small{color:var(--pico-muted-color);}
</style>
