<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { authStore } from '../stores/auth';
  import { driverStatus, startDshDriver, stopDshDriver, dshDriverConversationUnread, type DriverStatusResponse } from '../lib/api';
  import { driverPhaseRank } from '../lib/driverList';
  import DriverConversation from './DriverConversation.svelte';
  import DriverRunLog from './DriverRunLog.svelte';
  import type { StateProjectRow } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  const { projects = [], projectId = '' }: {projects?: StateProjectRow[]; projectId?: string} = $props();
  let status = $state<DriverStatusResponse | null>(null), error = $state(''), busy = $state('');
  let timer: ReturnType<typeof setInterval> | undefined, mounted = false, fetching = false;
  const selected = $derived(status?.projects[projectId]);
  const sortedProjects = $derived([...projects].sort((a, b) =>
    driverPhaseRank(status?.projects[a.project_id]?.managed?.phase) -
    driverPhaseRank(status?.projects[b.project_id]?.managed?.phase)));
  // The run log itself lives in DriverRunLog: the dashboard table and the
  // conversation pane both show those records, so there is one implementation.
  let historyProject = $state('');
  let conversationProject = $state('');
  let unread = $state<Record<string, number>>({});
  function showHistory(id: string) { historyProject = id; }
  function closeHistory() { historyProject = ''; }
  function applyActivity(id: string, phase: string, subagents: number) {
    if (status?.projects[id]?.managed) {
      status.projects[id].managed!.phase = phase;
      status.projects[id].running_subagents = subagents;
    }
  }
  // The unread badge counts persisted rows against the persisted read marker,
  // so it survives a refresh and needs no driver online. It is refreshed on the
  // same tick as the driver table, and the conversation advances the marker
  // only up to what it actually showed.
  async function loadUnread() {
    try {
      const result = await dshDriverConversationUnread();
      if (mounted) unread = result.projects ?? {};
    } catch { /* the badge is a convenience: a failed count hides it, never blocks the page */ }
  }
  function showConversation(id: string) { conversationProject = id; }
  function closeConversation() { conversationProject = ''; void loadUnread(); }
  async function refresh() {
    if (fetching || !$authStore.canWrite) return;
    fetching = true;
    try { const result = await driverStatus(); if (mounted) { status = result; error = ''; void loadUnread(); } }
    catch (e) { if (mounted) error = String(e instanceof Error ? e.message : e); }
    finally { fetching = false; }
  }
  $effect(() => { if ($authStore.permissionResolved && $authStore.canWrite && mounted) void refresh(); });
  $effect(() => { if ($authStore.permissionResolved && !$authStore.canWrite) { historyProject = ''; conversationProject = ''; unread = {}; } });
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
        <tbody>{#each sortedProjects as p (p.project_id)}
          {@const activity = status.projects[p.project_id]}
          <tr><td>{p.title}</td><td>{activity?.total ?? 0} / {activity?.online ?? 0}</td><td>{activity?.running_subagents ?? 0}</td><td>{activity?.running_pipelines ?? 0}</td>
            <td><span>{activity?.managed?.phase ?? '—'}</span>
              <button class="outline" disabled={!!busy} onclick={() => toggle(p.project_id, !!activity?.managed?.enabled)}>{busy === p.project_id ? '…' : activity?.managed?.enabled ? st('driverStop') : st('driverStart')}</button>
              <button class="outline" aria-label={st('driverConversation')} onclick={() => showConversation(p.project_id)}>{st('driverConversation')}{#if unread[p.project_id]}<span class="unread" title={st('driverConversationUnread')}>{unread[p.project_id]}</span>{/if}</button>
              {#if activity?.managed}<button class="outline" onclick={() => showHistory(p.project_id)}>{st('driverHistory')}</button>{/if}
              {#if activity?.managed?.last_error}<small role="status">{activity.managed.last_error}</small>{/if}
            </td></tr>
        {/each}</tbody>
      </table></div>
      {#if historyProject}
        {#key historyProject}
          <DriverRunLog projectId={historyProject}
            projectTitle={projects.find(p => p.project_id === historyProject)?.title ?? historyProject}
            driverId={status.projects[historyProject]?.managed?.driver_id ?? ''}
            driverEnabled={!!status.projects[historyProject]?.managed?.enabled}
            onclose={closeHistory}
            onactivity={(phase, subagents) => applyActivity(historyProject, phase, subagents)} />
        {/key}
      {/if}
      {#if conversationProject}
        <DriverConversation projectId={conversationProject}
          projectTitle={projects.find(p => p.project_id === conversationProject)?.title ?? conversationProject}
          driverId={status.projects[conversationProject]?.managed?.driver_id ?? ''}
          onclose={closeConversation} onread={() => void loadUnread()} />
      {/if}
    {/if}
  </details>
{/if}
<style>
  .driver-status{margin:.45rem 0;font-size:.76rem;border:1px solid var(--pico-muted-border-color,#ddd);border-radius:6px;padding:.5rem .65rem;}
  summary{display:flex;align-items:center;flex-wrap:wrap;gap:.4rem 1rem;margin:0;line-height:1.3;}
  .driver-table{overflow:auto;max-height:240px;}table{font-size:.75rem;margin:.5rem 0 0;}th,td{padding:.35rem .5rem;white-space:nowrap;}
  button{font-size:.7rem;padding:.2rem .5rem;margin:0 0 0 .5rem;width:auto;}small{display:block;max-width:400px;white-space:normal;color:var(--pico-del-color,#b91c1c);}
  p{margin:.4rem 0;}
  .unread{display:inline-block;margin:0 0 0 .35rem;padding:0 .35rem;border-radius:999px;background:var(--pico-primary,#2563eb);color:#fff;font-size:.65rem;line-height:1.5;}
</style>
