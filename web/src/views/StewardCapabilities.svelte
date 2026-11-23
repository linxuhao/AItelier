<script lang="ts">
  import { onMount } from 'svelte';
  import { listStewardCapabilities, type StewardCapability } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  // The owner's basic-capabilities table. Every route here is the BACKEND's answer:
  // it derives native from a live deployment fact and mcp/tool from a stored connection
  // with that tool ticked. This component has no capability -> provider mapping of its
  // own; it only turns the payload's provider into readable text. Change the payload,
  // and the rendered route changes without touching this file.
  let rows = $state<StewardCapability[]>([]);
  let loaded = $state(false), error = $state('');
  let alive = true;
  async function load() {
    try {
      const answer = await listStewardCapabilities();
      if (!alive) return;
      rows = answer.capabilities ?? [];
      error = ''; loaded = true;
    } catch (e) {
      if (alive) { error = String(e instanceof Error ? e.message : e); loaded = true; }
    }
  }
  onMount(() => {
    alive = true;
    void load();
    return () => { alive = false; };
  });
  function route(row: StewardCapability): string {
    if (row.provider === 'native') return 'native' + (row.detail ? ' — ' + row.detail : '');
    if (row.provider === 'mcp') return `mcp/tool: ${row.conn_id ?? '?'} / ${row.tool ?? '?'}`;
    return 'missing (no route)';
  }
</script>

<section class="steward-capabilities" aria-label="Basic capabilities">
  <table>
    <thead><tr><th scope="col">Capability</th><th scope="col">Route</th></tr></thead>
    <tbody>
      {#each rows as row (row.id)}
        <tr data-capability={row.id} data-provider={row.provider}>
          <td data-capability-label>{row.label}</td>
          <td data-capability-route>{route(row)}</td>
        </tr>
      {/each}
    </tbody>
  </table>
  {#if !loaded && !error}<p aria-busy="true">{t('steward.loading')}</p>{/if}
  {#if error}<p class="error" role="alert">{error}</p>{/if}
</section>

<style>
  .steward-capabilities{font-size:13px;}
  table{width:100%;border-collapse:collapse;}
  th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--pico-muted-border-color);vertical-align:top;}
  th{color:var(--pico-muted-color);font-size:11px;text-transform:uppercase;}
  td[data-capability-route]{overflow-wrap:anywhere;}
  .error{color:var(--pico-del-color);}
</style>
