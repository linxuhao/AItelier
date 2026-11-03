<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { dshDriverConversation, sendDriverConversationMessage, markDriverConversationRead,
    type DriverConversationEntry } from '../lib/api';
  import { isAtNewest, mergeThread } from '../lib/conversationThread';
  import { st } from '../lib/stateI18n.svelte';
  const { projectId, projectTitle = '', driverId = '', onclose = () => {}, onread = () => {} }: {
    projectId: string; projectTitle?: string; driverId?: string;
    onclose?: () => void; onread?: () => void;
  } = $props();
  // How often an open thread re-reads its newest page: the same bounded cadence
  // the DAG list polls driver status with, so one tick of latency is the floor.
  // It is a poll and not a stream because the conversation is durable State, not
  // a live session; a missed tick costs nothing but a later message.
  const REFRESH_MS = 10000;
  // Displayed oldest-first: one conversation, read like a thread.
  let messages = $state<DriverConversationEntry[]>([]);
  let nextCursor = $state<string | null>(null), hasMore = $state(false), endOfHistory = $state(false);
  let loading = $state(false), ready = $state(false), error = $state('');
  let replyTo = $state<DriverConversationEntry | null>(null);
  let text = $state(''), sending = $state(false), sent = $state(false), sendError = $state('');
  let generation = 0;
  // An older page is loaded, so the pagination cursor points BELOW the newest
  // page and belongs to that older page. A newest-page refresh must then leave
  // `nextCursor`/`hasMore` alone: overwriting them with the newest page's values
  // would march the reader back through pages they already have.
  let paged = false;
  // The thread is its own scroll box, so "show the newest message" is a scroll
  // of that box and never of the page. `bind:this` keeps the element here.
  let thread = $state<HTMLElement | null>(null);
  async function scrollToNewest() {
    await tick();
    if (thread) thread.scrollTop = thread.scrollHeight;
  }

  // Where the reader was before a fetch. A refresh may follow the newest message
  // for someone already at the bottom, and must not move someone who scrolled up
  // to read: their offset stays valid because a newest-page merge only ever adds
  // rows BELOW what they are reading.
  function snapshot() {
    return thread ? {top: thread.scrollTop, atNewest: isAtNewest(thread)} : null;
  }
  async function keepPlace(before: {top: number; atNewest: boolean} | null) {
    await tick();
    // `top` unchanged means the reader did not scroll while the fetch was in
    // flight; if they did, their newer position wins over the snapshot.
    if (thread && before?.atNewest && thread.scrollTop === before.top) thread.scrollTop = thread.scrollHeight;
  }

  // The newest page, merged into what is rendered. It also advances the persisted
  // read marker - but only up to the newest message THIS page showed, never
  // "now", and only for an explicit visit (mount, Refresh, Send): the background
  // poll below never marks, so a message that arrives on its own stays unread
  // and shows up on the badge instead of being swallowed.
  async function loadNewest() {
    const version = ++generation;
    const before = snapshot();
    loading = true; error = '';
    let page: Awaited<ReturnType<typeof dshDriverConversation>> | undefined;
    try {
      page = await dshDriverConversation(projectId);
      if (version !== generation) return;
      messages = mergeThread(messages, page.messages);
      if (!paged) { nextCursor = page.next_cursor; hasMore = page.has_more; endOfHistory = page.end_of_history; }
      ready = true;
    } catch (e) { if (version === generation) error = String(e instanceof Error ? e.message : e); }
    // `loading` is cleared before the scroll settles: the Refresh button must not
    // stay disabled while the pane is only repositioning itself.
    finally { if (version === generation) loading = false; }
    if (!page || version !== generation) return;
    if (page.messages.length && page.unread_count > 0) {
      try {
        await markDriverConversationRead(projectId, page.messages[0].message_id);
        if (version === generation) onread();
      } catch { /* the badge is a convenience: a failed mark never fails the read */ }
    }
    await keepPlace(before);
  }
  // Older messages: the same keyset cursor, so each one arrives exactly once.
  // The merge is belt and braces against a page that overlaps what is rendered;
  // the cursor now belongs to THIS page, which is why `paged` is set here.
  async function loadOlder() {
    if (!nextCursor || loading) return;
    const version = ++generation;
    loading = true; error = '';
    try {
      const page = await dshDriverConversation(projectId, nextCursor);
      if (version !== generation) return;
      messages = mergeThread(messages, page.messages);
      nextCursor = page.next_cursor; hasMore = page.has_more; endOfHistory = page.end_of_history;
      paged = true;
      ready = true;
    } catch (e) { if (version === generation) error = String(e instanceof Error ? e.message : e); }
    finally { if (version === generation) loading = false; }
  }
  async function send() {
    const body = text.trim();
    if (!body || sending) return;
    sending = true; sent = false; sendError = '';
    const answering = replyTo;
    try {
      await sendDriverConversationMessage(projectId, body, {
        replyToMessageId: answering?.message_id, targetDriverId: driverId || undefined});
      text = ''; replyTo = null; sent = true;
      await loadNewest();
      await scrollToNewest();
    } catch (e) { sendError = String(e instanceof Error ? e.message : e); }
    finally { sending = false; }
  }
  function reply(entry: DriverConversationEntry) { replyTo = entry; sent = false; sendError = ''; }

  // ── the thread refreshes itself ───────────────────────────────────────────
  // A message the driver sends while this pane is open used to need the Refresh
  // button, because the thread was fetched on mount and never again. The poll
  // below re-reads the newest page on REFRESH_MS and MERGES it:
  //
  //   * it never advances the read marker: an automatically refreshed-in message
  //     is unread until the user looks at it through an explicit refresh, so the
  //     badge still reports it instead of being silently swallowed;
  //   * it never resets the pagination cursor, so pages the reader loaded survive;
  //   * it never touches the reader's scroll position unless they were already at
  //     the bottom, in which case following the newest message is what they want;
  //   * a failure keeps whatever is on screen: this is additive, so an error
  //     leaves the thread exactly as it was instead of blanking it;
  //   * one fetch at a time (`refreshing`): a slow request skips a tick rather
  //     than stacking requests behind itself.
  let refreshing = false, alive = true;
  let timer: ReturnType<typeof setInterval> | undefined;
  async function refreshNewest() {
    if (!alive || refreshing) return;
    const asked = projectId;
    const before = snapshot();
    refreshing = true;
    try {
      const page = await dshDriverConversation(asked);
      // The component is keyed by project, but a stale answer must never land in
      // a thread that has moved on - and nothing may write after teardown.
      if (!alive || asked !== projectId) return;
      messages = mergeThread(messages, page.messages);
      if (!paged) { nextCursor = page.next_cursor; hasMore = page.has_more; endOfHistory = page.end_of_history; }
      // A poll that succeeds is what recovered a failed first load: the alert
      // that load left behind no longer describes the thread on screen. A failed
      // "load earlier" keeps its alert, because this refresh did not address it.
      if (!ready) error = '';
      ready = true;
      await keepPlace(before);
    } catch { /* additive only: the thread on screen is better than an empty one */ }
    finally { refreshing = false; }
  }
  // Exactly one interval per mounted pane: `start` clears first, so a repeated
  // visibility change (or a re-entry) can never stack a second timer, and the
  // page cannot accumulate one timer per project the user looked at.
  function stopTimer() {
    if (timer !== undefined) { clearInterval(timer); timer = undefined; }
  }
  function startTimer() {
    stopTimer();
    if (!alive || (typeof document !== 'undefined' && document.hidden)) return;
    timer = setInterval(() => { void refreshNewest(); }, REFRESH_MS);
  }
  // A hidden tab polls nothing: it is not being read. Becoming visible again
  // catches up at once and then resumes the cadence.
  function onVisibility() {
    if (typeof document === 'undefined') return;
    if (document.hidden) { stopTimer(); return; }
    void refreshNewest();
    startTimer();
  }
  onMount(() => {
    alive = true;
    document.addEventListener('visibilitychange', onVisibility);
    void loadNewest();
    startTimer();
    return () => {
      alive = false;
      stopTimer();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  });
</script>
<!-- Three bands: a fixed header, ONE scroll box for the thread, and a composer
     that is a sibling of that box. The composer therefore never moves with the
     messages: it sits at the bottom of the pane's own box, not at the end of the
     scrolled content, so reaching Send never means scrolling anything else. -->
<section class="conversation" aria-label={st('driverConversation')}>
  <header class="conversation-head">
    <strong>{st('driverConversation')} · {projectTitle || projectId}</strong>
    <button class="outline" type="button" disabled={loading} onclick={() => void loadNewest()}>{st('driverMessagesRefresh')}</button>
    <button class="outline" type="button" onclick={onclose}>{st('driverMessagesClose')}</button>
  </header>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if loading && !ready}<p role="status">{st('loading')}</p>{/if}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <!-- A scroll container has to be focusable to be scrollable from the keyboard:
       role=log keeps it a named region instead of a widget. -->
  <div class="conversation-thread" data-conversation-thread role="log" tabindex="0"
    aria-label={st('driverConversationScroll')} bind:this={thread}>
    {#if ready}
      {#if hasMore && nextCursor}
        <button class="outline" type="button" disabled={loading} onclick={() => void loadOlder()}>{st('driverConversationLoadMore')}</button>
      {:else if endOfHistory && messages.length}
        <p class="identity">{st('driverConversationStart')}</p>
      {/if}
      {#if !messages.length}<p>{st('driverConversationEmpty')}</p>{/if}
      {#each messages as entry (entry.message_id)}
        <article class="message" class:mine={entry.sender_kind === 'user'}>
          <header>
            <span class="sender">{entry.sender_kind === 'user' ? st('driverConversationYou') : (entry.sender_driver_id ?? st('driverConversationDriver'))}</span>
            <span class="identity"> · {new Date(entry.created_at).toLocaleString()}</span>
            {#if entry.sender_kind === 'driver'}
              <button class="outline" type="button" aria-label="{st('driverConversationReply')}: {entry.subject}" onclick={() => reply(entry)}>{st('driverConversationReply')}</button>
            {/if}
          </header>
          {#if entry.reply_to_message_id}<p class="identity">{st('driverConversationReplyingTo')} #{entry.reply_to_message_id.slice(0, 8)}</p>{/if}
          <strong class="subject">{entry.subject}</strong>
          {#if entry.body}<pre>{entry.body}</pre>{/if}
        </article>
      {/each}
    {/if}
  </div>
  <form class="composer" data-conversation-composer onsubmit={(event) => {event.preventDefault(); void send();}}>
    {#if replyTo}
      <p class="identity">{st('driverConversationReplyingTo')} · {replyTo.subject}
        <button class="outline" type="button" onclick={() => (replyTo = null)}>{st('driverConversationCancelReply')}</button></p>
    {/if}
    <label>{st('driverConversationCompose')}<textarea bind:value={text} maxlength="8000" rows="2" disabled={sending}></textarea></label>
    <button type="submit" disabled={sending || !text.trim()}>{sending ? '…' : st('driverMessageSend')}</button>
    {#if sent}<span role="status">{st('driverConversationSent')}</span>{/if}
    {#if sendError}<p role="alert">{sendError}</p>{/if}
  </form>
</section>
<style>
  /* The box chain that makes the pane own its scroll: this section is a flex
     column that is allowed to shrink (min-height:0) and clips, the thread is the
     only scroll box, and the composer is a non-shrinking band after it. */
  .conversation{display:flex;flex-direction:column;flex:1 1 auto;min-height:0;overflow-x:hidden;overflow-y:hidden;
    border-top:1px solid var(--pico-muted-border-color,#ddd);margin-top:.35rem;padding-top:.35rem;}
  .conversation-head{display:flex;align-items:center;flex-wrap:wrap;gap:.4rem;flex:0 0 auto;}
  .conversation-thread{flex:1 1 auto;min-height:0;overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain;padding-right:.2rem;}
  .conversation-thread:focus-visible{outline:2px solid var(--pico-primary,#0066cc);outline-offset:-2px;}
  .composer{flex:0 0 auto;margin:.45rem 0 0;padding-top:.45rem;border-top:1px solid var(--pico-muted-border-color,#e6ebf1);}
  button{font-size:.7rem;padding:.2rem .5rem;margin:0 0 0 .5rem;width:auto;}
  p{margin:.4rem 0;}
  .identity{overflow-wrap:anywhere;color:var(--pico-muted-color,#64748b);}
  .message{margin:.45rem 0;padding:.35rem .55rem;border:1px solid var(--pico-muted-border-color,#ddd);border-radius:6px;}
  .message.mine{border-color:var(--pico-primary-border,#93c5fd);}
  .message header{display:block;}
  .message .sender{font-weight:600;}
  .message .subject{display:block;margin:.2rem 0;}
  label{display:block;font-size:.8rem;}
  textarea{font-size:.8rem;margin:.3rem 0;}
  pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:20rem;overflow-y:auto;overflow-x:hidden;font-size:.75rem;padding:.5rem;margin:.25rem 0 0;}
</style>
