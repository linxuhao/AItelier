<script lang="ts">
  import { onMount } from 'svelte';
  import { listStewardProposals, type StewardProposal } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  import StewardProposalCard from './StewardProposalCard.svelte';
  const { onchange = () => {}, onrevise, onconnect }: {
    /** Something was decided: the owner's pending count may have moved. */
    onchange?: () => void;
    onrevise: (row: StewardProposal) => void;
    /** Step 24.1c: a connection proposal's "set the secret / sign in". */
    onconnect?: (connId: string) => void;
  } = $props();
  // An executing proposal settles on its own; the list re-reads on this cadence
  // so the card moves from the spinner to its result without a click.
  const REFRESH_MS = 5000;
  let rows = $state<StewardProposal[]>([]), loaded = $state(false), error = $state('');
  // Decided cards start folded: the owner sees only what still needs a decision.
  let showPast = $state(false);
  // A conflict answer can carry a row that is ALREADY decided. The row then leaves
  // the active section, its card unmounts, and the notice that card raised ("this
  // proposal changed while you were looking") would unmount with it. So the list
  // says it, and offers the fold where the row now lives.
  let moved = $state<StewardProposal | null>(null);
  // A 409 that carried no row re-reads the list; these ids are the ones whose
  // decided state the re-read is meant to explain.
  let pendingConflict = $state<string[]>([]);
  const LIVE = new Set(['open', 'executing']);
  const newestFirst = (a: StewardProposal, b: StewardProposal) =>
    a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0;
  // filter() copies, so sorting the two halves never reorders `rows` itself.
  const active = $derived(rows.filter(row => LIVE.has(row.status)).sort(newestFirst));
  const past = $derived(rows.filter(row => !LIVE.has(row.status)).sort(newestFirst));
  let alive = true;
  async function load() {
    try {
      const result = await listStewardProposals('all', 50);
      if (!alive) return;
      rows = result; error = ''; loaded = true;
      if (pendingConflict.length) {
        const carried = result.find(row => pendingConflict.includes(row.proposal_id) && !LIVE.has(row.status));
        if (carried) moved = carried;
        pendingConflict = [];
      }
    } catch (e) { if (alive) error = String(e instanceof Error ? e.message : e); }
  }
  function update(row: StewardProposal, meta?: {conflict?: boolean}) {
    rows = rows.map(r => r.proposal_id === row.proposal_id ? row : r);
    // The owner's own decision is not a change "while you were looking": only a
    // refusal reports one, and only when the row it carried has left the section.
    if (meta?.conflict && !LIVE.has(row.status)) moved = row;
    else if (LIVE.has(row.status)) moved = null;
    onchange();
  }
  /** A 409 carried no row: re-read, and explain the row if it comes back decided. */
  function reload(proposalId: string) {
    pendingConflict = [proposalId];
    void load();
  }
  onMount(() => {
    alive = true;
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => { alive = false; clearInterval(timer); };
  });
</script>
<section class="steward-proposals" aria-label={t('steward.tabProposals')}>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if moved}
    <p class="moved" role="status">
      {t('steward.changed')} <strong>{t('steward.kind.' + moved.kind)}</strong>
      <button type="button" class="secondary outline" onclick={() => { showPast = true; moved = null; }}>{t('steward.pastShow').replace('{n}', String(past.length))}</button>
    </p>
  {/if}
  {#if !loaded && !error}<p aria-busy="true">{t('steward.loading')}</p>
  {:else if loaded && !rows.length}<p class="empty">{t('steward.proposalsEmpty')}</p>
  {:else if loaded}
    <section data-proposal-section="active">
      <h3>{t('steward.sectionActive')} <span class="count">{active.length}</span></h3>
      {#if active.length}
        {#each active as proposal (proposal.proposal_id)}
          <StewardProposalCard {proposal} onupdate={update} onreload={reload} {onrevise} {onconnect} />
        {/each}
      {:else}<p class="empty">{t('steward.activeEmpty')}</p>{/if}
    </section>
    <section data-proposal-section="past">
      <h3>
        <button data-past-toggle type="button" aria-expanded={showPast} onclick={() => showPast = !showPast}>
          {t('steward.sectionPast')} · {t(showPast ? 'steward.pastHide' : 'steward.pastShow').replace('{n}', String(past.length))}
        </button>
      </h3>
      {#if showPast}
        {#each past as proposal (proposal.proposal_id)}
          <StewardProposalCard {proposal} onupdate={update} onreload={reload} {onrevise} {onconnect} />
        {/each}
      {/if}
    </section>
  {/if}
</section>
<style>
  .empty{color:var(--pico-muted-color);font-size:13px;}
  .count{color:var(--pico-muted-color);font-size:12px;}
  .moved{font-size:13px;color:var(--pico-muted-color);}
  .moved button{width:auto;margin:0 0 0 6px;padding:1px 8px;font-size:12px;}
  [data-proposal-section="past"] h3 button{width:auto;margin:0;padding:2px 8px;font-size:13px;}
</style>
