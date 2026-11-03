<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { authStore } from '../stores/auth';
  import { driverStatus, startDshDriver, stopDshDriver, type DriverStatusResponse } from '../lib/api';
  import type { StateProjectRow } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  const { projects = [], projectId = '' }: {projects?: StateProjectRow[]; projectId?: string} = $props();
  let status = $state<DriverStatusResponse | null>(null), error = $state(''), busy = $state('');
  let timer: ReturnType<typeof setInterval> | undefined, mounted = false, fetching = false;
  const selected = $derived(status?.projects[projectId]);
  async function refresh() {
    if (fetching || !$authStore.canWrite) return;
    fetching = true;
    try { const result = await driverStatus(); if (mounted) { status = result; error = ''; } }
    catch (e) { if (mounted) error = String(e instanceof Error ? e.message : e); }
    finally { fetching = false; }
  }
  $effect(() => { if ($authStore.permissionResolved && $authStore.canWrite && mounted) void refresh(); });
  onMount(() => { mounted = true; void refresh(); timer = setInterval(refresh, 10000); });
  onDestroy(() => { mounted = false; if (timer) clearInterval(timer); });
  async function toggle(id: string, enabled: boolean) {
    if (busy) return;
    busy = id; error = '';
    try { if (enabled) await stopDshDriver(id); else await startDshDriver(id); await refresh(); }
    catch (e) { error = String(e instanceof Error ? e.message : e); }
    finally { busy = ''; }
  }
</script>
{#if $authStore.permissionResolved && $authStore.canWrite}
  {#if error}<p role="alert">{error}</p>{/if}
  <details class="driver-status">
    <summary>
      <strong>Driver status</strong>
      {#if status}<span>{st('driverTotal')}: {status.total} · {st('driverOnline')}: {status.online}</span>{:else}<span>{st('loading')}</span>{/if}
      {#if selected}<span>{projectId}: {selected.online}/{selected.total} {st('driverOnline')} · {selected.running_subagents} subagents · {selected.running_pipelines} pipelines</span>{/if}
    </summary>
    {#if status}
      <div class="driver-table"><table>
        <thead><tr><th>State DAG</th><th>{st('driverTotal')} / {st('driverOnline')}</th><th>{st('driverRunning')} subagents</th><th>{st('driverRunning')} pipelines</th><th>DSH driver</th></tr></thead>
        <tbody>{#each projects as p (p.project_id)}
          {@const activity = status.projects[p.project_id]}
          <tr><td>{p.title}</td><td>{activity?.total ?? 0} / {activity?.online ?? 0}</td><td>{activity?.running_subagents ?? 0}</td><td>{activity?.running_pipelines ?? 0}</td>
            <td><span>{activity?.managed?.phase ?? '—'}</span>
              <button class="outline" disabled={!!busy} onclick={() => toggle(p.project_id, !!activity?.managed?.enabled)}>{busy === p.project_id ? '…' : activity?.managed?.enabled ? st('driverStop') : st('driverStart')}</button>
              {#if activity?.managed?.last_error}<small role="status">{activity.managed.last_error}</small>{/if}
            </td></tr>
        {/each}</tbody>
      </table></div>
    {/if}
  </details>
{/if}
<style>
  .driver-status{margin:.45rem 0;font-size:.76rem;border:1px solid var(--pico-muted-border-color,#ddd);border-radius:6px;padding:.5rem .65rem;}
  summary{display:flex;align-items:center;flex-wrap:wrap;gap:.4rem 1rem;margin:0;line-height:1.3;}
  .driver-table{overflow:auto;max-height:240px;}table{font-size:.75rem;margin:.5rem 0 0;}th,td{padding:.35rem .5rem;white-space:nowrap;}
  button{font-size:.7rem;padding:.2rem .5rem;margin:0 0 0 .5rem;width:auto;}small{display:block;max-width:400px;white-space:normal;color:var(--pico-del-color,#b91c1c);}
  p{margin:.4rem 0;}
</style>
