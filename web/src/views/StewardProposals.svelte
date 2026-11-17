<script lang="ts">
  import { onMount } from 'svelte';
  import { listStewardProposals, type StewardProposal } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  import StewardProposalCard from './StewardProposalCard.svelte';
  const { onchange = () => {}, onrevise }: {
    /** Something was decided: the owner's pending count may have moved. */
    onchange?: () => void;
    onrevise: (row: StewardProposal) => void;
  } = $props();
  // An executing proposal settles on its own; the list re-reads on this cadence
  // so the card moves from the spinner to its result without a click.
  const REFRESH_MS = 5000;
  let rows = $state<StewardProposal[]>([]), loaded = $state(false), error = $state('');
  const LIVE = new Set(['open', 'executing']);
  // Open and executing first, newest first inside each half.
  const ordered = $derived([...rows].sort((a, b) =>
    Number(LIVE.has(b.status)) - Number(LIVE.has(a.status)) || (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0)));
  let alive = true;
  async function load() {
    try {
      const result = await listStewardProposals('all', 50);
      if (!alive) return;
      rows = result; error = ''; loaded = true;
    } catch (e) { if (alive) error = String(e instanceof Error ? e.message : e); }
  }
  function update(row: StewardProposal) {
    rows = rows.map(r => r.proposal_id === row.proposal_id ? row : r);
    onchange();
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
  {#if !loaded && !error}<p aria-busy="true">{t('steward.loading')}</p>
  {:else if loaded && !rows.length}<p class="empty">{t('steward.proposalsEmpty')}</p>{/if}
  {#each ordered as proposal (proposal.proposal_id)}
    <StewardProposalCard {proposal} onupdate={update} onreload={() => void load()} {onrevise} />
  {/each}
</section>
<style>
  .empty{color:var(--pico-muted-color);font-size:13px;}
</style>
