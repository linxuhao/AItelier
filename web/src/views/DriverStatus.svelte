<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { authStore } from '../stores/auth';
  import { driverStatus, startDshDriver, stopDshDriver, dshDriverHistory, messageDshDriver, dshDriverConversationUnread, type DriverStatusResponse, type DriverHistory, type DriverHistoryEntry } from '../lib/api';
  import DriverConversation from './DriverConversation.svelte';
  import type { StateProjectRow } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  const { projects = [], projectId = '' }: {projects?: StateProjectRow[]; projectId?: string} = $props();
  let status = $state<DriverStatusResponse | null>(null), error = $state(''), busy = $state('');
  let timer: ReturnType<typeof setInterval> | undefined, mounted = false, fetching = false;
  const selected = $derived(status?.projects[projectId]);
  const ranks: Record<string, number> = {running: 0, waiting: 1, starting: 2, stopping: 3, failed: 4, stopped: 5};
  const sortedProjects = $derived([...projects].sort((a, b) =>
    (ranks[status?.projects[a.project_id]?.managed?.phase ?? ''] ?? 6) -
    (ranks[status?.projects[b.project_id]?.managed?.phase ?? ''] ?? 6)));
  let historyProject = $state(''), history = $state<DriverHistory | null>(null), historyLoading = $state(false), historyError = $state('');
  let historyGeneration = 0;
  let live = $state(false), stream: EventSource | undefined;
  let latest = $state(true);
  let pendingLive: DriverHistoryEntry[] = [];
  let message = $state(''), sending = $state(false), messageSent = $state(false), messageError = $state('');
  // ONE conversation per project (both directions) and its persisted unread badge.
  let conversationProject = $state('');
  let unread = $state<Record<string, number>>({});
  function mergeEntries(entries: DriverHistoryEntry[], updates: DriverHistoryEntry[]) {
    return [...new Map([...entries, ...updates].map(e => [e.seq, e])).values()].sort((a, b) => a.seq - b.seq).slice(-100);
  }
  function connectLive(id: string) {
    stream?.close(); stream = undefined; live = false;
    const driver = status?.projects[id]?.managed;
    if (!driver?.enabled || typeof EventSource === 'undefined') return;
    const connection = new EventSource('/api/tasks/' + encodeURIComponent('dsh:' + driver.driver_id) + '/stream');
    stream = connection;
    let connected = false;
    connection.onopen = () => {
      if (stream !== connection) return;
      live = true;
      if (connected) {
        const version = historyGeneration;
        void dshDriverHistory(id).then(page => {
          if (stream === connection && version === historyGeneration && history) {
            const entries = mergeEntries(page.entries, history.entries.filter(e => e.seq > (page.entries.at(-1)?.seq ?? -1)));
            history = {...page, entries, has_more: page.has_more || entries.length === 100, next_before_seq: entries[0]?.seq ?? null};
          }
        }).catch(() => {});
      }
      connected = true;
    };
    connection.onerror = () => { if (stream === connection) live = false; };
    connection.onmessage = event => {
      if (stream !== connection || historyProject !== id) return;
      try {
        const data = JSON.parse(JSON.parse(event.data).log);
        if (data.type !== 'driver_activity') return;
        if (status?.projects[id]?.managed) {
          status.projects[id].managed!.phase = data.phase;
          status.projects[id].running_subagents = data.subagents.length;
        }
        if (data.entry) {
          if (history) {
            if (data.entry.seq < (history.entries[0]?.seq ?? -1)) return;
            const entries = mergeEntries(history.entries, [data.entry]);
            history = {...history, entries, has_more: history.has_more || entries.length === 100, next_before_seq: entries[0]?.seq ?? null};
          } else pendingLive = mergeEntries(pendingLive, [data.entry]);
        }
      } catch { /* Keep the stream open after an unrelated or malformed event. */ }
    };
  }
  async function sendMessage() {
    const id = historyProject, driver = status?.projects[id]?.managed;
    if (!driver || !message.trim() || sending) return;
    const text = message; sending = true; messageSent = false; messageError = '';
    try {
      await messageDshDriver(id, driver.driver_id, text);
      if (historyProject === id) { if (message === text) message = ''; messageSent = true; }
    } catch (e) { if (historyProject === id) messageError = String(e instanceof Error ? e.message : e); }
    finally { sending = false; }
  }
  async function showHistory(id: string, older = false) {
    const before = older ? history?.next_before_seq ?? undefined : undefined;
    const version = ++historyGeneration;
    if (historyProject !== id) { message = ''; messageSent = false; messageError = ''; }
    historyProject = id; historyLoading = true; historyError = ''; latest = !older;
    if (!older) { history = null; pendingLive = []; connectLive(id); }
    else { stream?.close(); stream = undefined; live = false; }
    try {
      const page = await dshDriverHistory(id, before);
      if (mounted && version === historyGeneration) {
        const entries = latest ? mergeEntries(page.entries, pendingLive.filter(e => e.seq > (page.entries.at(-1)?.seq ?? -1))) : page.entries;
        history = {...page, entries, has_more: page.has_more || entries.length === 100, next_before_seq: entries[0]?.seq ?? null};
        pendingLive = [];
      }
    } catch (e) { if (mounted && version === historyGeneration) historyError = String(e instanceof Error ? e.message : e); }
    finally { if (mounted && version === historyGeneration) historyLoading = false; }
  }
  function closeHistory() { stream?.close(); stream = undefined; live = false; pendingLive = []; historyGeneration++; historyProject = ''; history = null; historyLoading = false; }
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
  function recordText(text: string) {
    try {
      const data = JSON.parse(text), content = data.message?.content;
      if (Array.isArray(content)) return content.map(b => b.text ?? JSON.stringify(b, null, 2)).join('\n\n');
    } catch { /* A bounded excerpt can end before the JSON closes. */ }
    return text;
  }
  async function refresh() {
    if (fetching || !$authStore.canWrite) return;
    fetching = true;
    try { const result = await driverStatus(); if (mounted) { status = result; error = ''; void loadUnread(); } }
    catch (e) { if (mounted) error = String(e instanceof Error ? e.message : e); }
    finally { fetching = false; }
  }
  $effect(() => { if ($authStore.permissionResolved && $authStore.canWrite && mounted) void refresh(); });
  $effect(() => { if ($authStore.permissionResolved && !$authStore.canWrite) { closeHistory(); conversationProject = ''; unread = {}; } });
  onMount(() => { mounted = true; void refresh(); timer = setInterval(refresh, 10000); });
  onDestroy(() => { mounted = false; historyGeneration++; stream?.close(); if (timer) clearInterval(timer); });
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
        <section class="history" aria-label={st('driverHistory')}>
          <header><strong>{st('driverHistory')} · {projects.find(p => p.project_id === historyProject)?.title ?? historyProject}</strong>
            {#if latest && status.projects[historyProject]?.managed?.enabled}<span class:live>{live ? st('driverLive') : st('driverReconnecting')}</span>{/if}
            <button class="outline" disabled={historyLoading} onclick={() => showHistory(historyProject)}>{st('driverHistoryRefresh')}</button>
            <button class="outline" onclick={closeHistory}>{st('driverHistoryClose')}</button></header>
          <form onsubmit={(event) => {event.preventDefault(); void sendMessage();}}>
            <label>{st('driverMessage')}<textarea bind:value={message} maxlength="8000" rows="2" disabled={sending}></textarea></label>
            <button type="submit" disabled={sending || !message.trim()}>{sending ? '…' : st('driverMessageSend')}</button>
            {#if messageSent}<span role="status">{st('driverMessageSent')}</span>{/if}
            {#if messageError}<p role="alert">{messageError}</p>{/if}
          </form>
          {#if historyError}<p role="alert">{historyError}</p>{/if}
          {#if historyLoading}<p role="status">{st('loading')}</p>{/if}
          {#if history}
            <p class="identity">{history.driver_id} · {history.session_id}</p>
            {#if history.has_more}<button class="outline" disabled={historyLoading} onclick={() => showHistory(historyProject, true)}>{st('driverHistoryOlder')}</button>{/if}
            {#if !history.entries.length}<p>{st('driverHistoryEmpty')}</p>{/if}
            {#each [...history.entries].reverse() as entry (entry.seq)}
              <details class="record"><summary>#{entry.seq} · {entry.type}{entry.name ? ' · ' + entry.name : ''}{entry.timestamp ? ' · ' + new Date(entry.timestamp).toLocaleString() : ''}{entry.truncated ? ' …' : ''}</summary><pre>{recordText(entry.text)}</pre></details>
            {/each}
          {/if}
        </section>
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
  .history{border-top:1px solid var(--pico-muted-border-color,#ddd);margin-top:.6rem;padding-top:.6rem;}
  .history header{display:flex;align-items:center;flex-wrap:wrap;gap:.4rem;}
  .live{color:#15803d;}form{margin:.7rem 0;}label{display:block;font-size:.8rem;}textarea{font-size:.8rem;margin:.3rem 0;}
  .identity{overflow-wrap:anywhere;color:var(--pico-muted-color,#64748b);}
  .record{margin:.45rem 0;}.record summary{cursor:pointer;}
  .unread{display:inline-block;margin:0 0 0 .35rem;padding:0 .35rem;border-radius:999px;background:var(--pico-primary,#2563eb);color:#fff;font-size:.65rem;line-height:1.5;}
  pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:32rem;overflow:auto;font-size:.75rem;padding:.5rem;}
</style>
