<script lang="ts">
  import { authStore } from '../stores/auth';
  import { stateRunOwners } from '../lib/api';
  import { stateProjectHref, type RunOwner } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  const { runId }: { runId: string } = $props();
  let links = $state<RunOwner[]>([]), error = $state('');
  $effect(() => {
    const id = runId, allowed = $authStore.permissionResolved && $authStore.canWrite;
    let cancelled = false; links = []; error = '';
    if (allowed && id) stateRunOwners(id).then(result => { if (!cancelled) links = result.links; })
      .catch(e => { if (!cancelled && e.status !== 403) error = String(e.message ?? e); });
    return () => { cancelled = true; };
  });
</script>
{#if $authStore.canWrite}
  {#if links.length}<nav class="state-owners" aria-label={st('ownership')}>
    {#each links as link (link.project_id + ':' + (link.attempt_id ?? link.reference_id ?? link.node_key))}
      <span><a href={stateProjectHref(link.project_id, link.node_key)}>{link.title} / {link.node_key}</a>
        {#if link.relation === 'reference'}<small>{st('referenceOnly')}</small>{/if}</span>
    {/each}
  </nav>{/if}
  {#if error}<p class="state-link-error" role="status">{st('ownership')}: {error}</p>{/if}
{/if}
<style>
  .state-owners { display:flex; justify-content:flex-start; flex-wrap:wrap; gap:.5rem 1rem; font-size:.8rem; margin:.7rem 0; }
  .state-owners span { overflow-wrap:anywhere; } small { display:block; opacity:.7; font-size:.7rem; }
  .state-link-error { font-size:.7rem; color:var(--pico-muted-color,#667085); }
</style>
