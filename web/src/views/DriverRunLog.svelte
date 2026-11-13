<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { dshDriverHistory, messageDshDriver, type DriverHistory, type DriverHistoryEntry } from '../lib/api';
  import { st } from '../lib/stateI18n.svelte';
  // The run log for ONE project: persisted entries, the live stream that appends
  // them, the older-record page and the inbox message box. Extracted from
  // DriverStatus so the dashboard table and the conversation pane show the same
  // records instead of two implementations drifting apart.
  const { projectId, projectTitle = '', driverId = '', driverEnabled = false,
    onclose, onactivity = () => {} }: {
    projectId: string; projectTitle?: string; driverId?: string; driverEnabled?: boolean;
    // Only a host that can actually dismiss the history passes `onclose`: the
    // dashboard pane always shows the run log, so it passes nothing and gets no
    // close button (the control it had there closed the live stream and never
    // reopened it: the tab sat on "Reconnecting…" with nothing left to press).
    onclose?: () => void; onactivity?: (phase: string, subagents: number) => void;
  } = $props();
  let history = $state<DriverHistory | null>(null), historyLoading = $state(false), historyError = $state('');
  let historyGeneration = 0;
  let live = $state(false), stream: EventSource | undefined;
  let latest = $state(true);
  let pendingLive: DriverHistoryEntry[] = [];
  let message = $state(''), sending = $state(false), messageError = $state('');
  let mounted = false;
  function mergeEntries(entries: DriverHistoryEntry[], updates: DriverHistoryEntry[]) {
    return [...new Map([...entries, ...updates].map(e => [e.seq, e])).values()].sort((a, b) => a.seq - b.seq).slice(-100);
  }
  function connectLive() {
    stream?.close(); stream = undefined; live = false;
    if (!driverEnabled || !driverId || typeof EventSource === 'undefined') return;
    const connection = new EventSource('/api/tasks/' + encodeURIComponent('dsh:' + driverId) + '/stream');
    stream = connection;
    let connected = false;
    connection.onopen = () => {
      if (stream !== connection) return;
      live = true;
      if (connected) {
        const version = historyGeneration;
        void dshDriverHistory(projectId).then(page => {
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
      if (stream !== connection) return;
      try {
        const data = JSON.parse(JSON.parse(event.data).log);
        if (data.type !== 'driver_activity') return;
        onactivity(data.phase, Array.isArray(data.subagents) ? data.subagents.length : 0);
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
  async function load(older = false) {
    const before = older ? history?.next_before_seq ?? undefined : undefined;
    const version = ++historyGeneration;
    if (!older) { message = ''; messageError = ''; }
    historyLoading = true; historyError = ''; latest = !older;
    if (!older) { history = null; pendingLive = []; connectLive(); }
    else { stream?.close(); stream = undefined; live = false; }
    try {
      const page = await dshDriverHistory(projectId, before);
      if (mounted && version === historyGeneration) {
        const entries = latest ? mergeEntries(page.entries, pendingLive.filter(e => e.seq > (page.entries.at(-1)?.seq ?? -1))) : page.entries;
        history = {...page, entries, has_more: page.has_more || entries.length === 100, next_before_seq: entries[0]?.seq ?? null};
        pendingLive = [];
      }
    } catch (e) { if (mounted && version === historyGeneration) historyError = String(e instanceof Error ? e.message : e); }
    finally { if (mounted && version === historyGeneration) historyLoading = false; }
  }
  async function sendMessage() {
    if (!message.trim() || sending) return;
    const text = message; sending = true; messageError = '';
    try {
      await messageDshDriver(projectId, driverId, text);
      if (message === text) message = '';
    } catch (e) { messageError = String(e instanceof Error ? e.message : e); }
    finally { sending = false; }
  }
  function close() { stream?.close(); stream = undefined; live = false; pendingLive = []; historyGeneration++; onclose?.(); }
  function recordText(text: string) {
    try {
      const data = JSON.parse(text), content = data.message?.content;
      if (Array.isArray(content)) return content.map(b => b.text ?? JSON.stringify(b, null, 2)).join('\n\n');
    } catch { /* A bounded excerpt can end before the JSON closes. */ }
    return text;
  }
  onMount(() => { mounted = true; void load(); });
  onDestroy(() => { mounted = false; historyGeneration++; stream?.close(); });
</script>
<section class="history" aria-label={st('driverHistory')}>
  <header><strong>{st('driverHistory')} · {projectTitle || projectId}</strong>
    {#if latest && driverEnabled}<span class:live>{live ? st('driverLive') : st('driverReconnecting')}</span>{/if}
    <button class="outline" type="button" disabled={historyLoading} onclick={() => void load()}>{st('driverHistoryRefresh')}</button>
    {#if onclose}<button class="outline" type="button" onclick={close}>{st('driverHistoryClose')}</button>{/if}</header>
  <!-- One row, the same shape the conversation composer has: the box takes the
       width, Send keeps its own, and the accessible name lives on the textarea
       instead of in a visible label. The completion sentence is gone with it:
       those two were the strings the owner asked to remove, and a run log does
       not need to repeat what the conversation tab already shows. The failure
       line stays - a send that failed silently is worse than one that worked
       silently. -->
  <form class="compose" data-run-log-compose onsubmit={(event) => {event.preventDefault(); void sendMessage();}}>
    <textarea bind:value={message} maxlength="8000" rows="1" disabled={sending}
      aria-label={st('driverMessage')}></textarea>
    <button type="submit" disabled={sending || !message.trim()}>{sending ? '…' : st('driverMessageSend')}</button>
    {#if messageError}<p role="alert">{messageError}</p>{/if}
  </form>
  {#if historyError}<p role="alert">{historyError}</p>{/if}
  {#if historyLoading}<p role="status">{st('loading')}</p>{/if}
  {#if history}
    <p class="identity">{history.driver_id} · {history.session_id}</p>
    {#if history.has_more}<button class="outline" type="button" disabled={historyLoading} onclick={() => void load(true)}>{st('driverHistoryOlder')}</button>{/if}
    {#if !history.entries.length}<p>{st('driverHistoryEmpty')}</p>{/if}
    {#each [...history.entries].reverse() as entry (entry.seq)}
      <details class="record"><summary>#{entry.seq} · {entry.type}{entry.name ? ' · ' + entry.name : ''}{entry.timestamp ? ' · ' + new Date(entry.timestamp).toLocaleString() : ''}{entry.truncated ? ' …' : ''}</summary><pre>{recordText(entry.text)}</pre></details>
    {/each}
  {/if}
</section>
<style>
  .history{border-top:1px solid var(--pico-muted-border-color,#ddd);margin-top:.6rem;padding-top:.6rem;font-size:.76rem;}
  .history header{display:flex;align-items:center;flex-wrap:wrap;gap:.4rem;}
  button{font-size:.7rem;padding:.2rem .5rem;margin:0 0 0 .5rem;width:auto;}
  p{margin:.4rem 0;}
  .live{color:#15803d;}
  /* One row: the box takes the width, Send keeps its own, and a narrow pane wraps
     the button under the box instead of pushing it out. Pico's global form rule is
     `button[type=submit],select,textarea{width:100%}`; the two selectors below
     out-specify it, exactly as the conversation composer does. */
  form.compose{display:flex;flex-direction:row;flex-wrap:wrap;align-items:flex-end;gap:.35rem;margin:.7rem 0;}
  .compose textarea{flex:1 1 6rem;width:auto;min-width:0;margin:0;resize:vertical;font-size:.8rem;line-height:1.35;padding:.25rem .4rem;}
  .compose button[type="submit"]{flex:0 0 auto;width:auto;margin:0;}
  .identity{overflow-wrap:anywhere;color:var(--pico-muted-color,#64748b);}
  .record{margin:.45rem 0;}.record summary{cursor:pointer;}
  pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:32rem;overflow:auto;font-size:.75rem;padding:.5rem;}
</style>
