<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { dshDriverConversation, sendDriverConversationMessage, markDriverConversationRead,
    type DriverConversationEntry } from '../lib/api';
  import { st } from '../lib/stateI18n.svelte';
  const { projectId, projectTitle = '', driverId = '', onclose = () => {}, onread = () => {} }: {
    projectId: string; projectTitle?: string; driverId?: string;
    onclose?: () => void; onread?: () => void;
  } = $props();
  // Displayed oldest-first: one conversation, read like a thread.
  let messages = $state<DriverConversationEntry[]>([]);
  let nextCursor = $state<string | null>(null), hasMore = $state(false), endOfHistory = $state(false);
  let loading = $state(false), ready = $state(false), error = $state('');
  let replyTo = $state<DriverConversationEntry | null>(null);
  let text = $state(''), sending = $state(false), sent = $state(false), sendError = $state('');
  let generation = 0;
  // The thread is its own scroll box, so "show the newest message" is a scroll
  // of that box and never of the page. `bind:this` keeps the element here.
  let thread = $state<HTMLElement | null>(null);
  async function scrollToNewest() {
    await tick();
    if (thread) thread.scrollTop = thread.scrollHeight;
  }

  // The newest page. It also advances the persisted read marker - but only up
  // to the newest message THIS page showed, never "now": a message the driver
  // sends while this panel is open stays unread until the user loads it, so it
  // shows up on the next badge poll instead of being swallowed.
  async function loadNewest() {
    const version = ++generation;
    loading = true; error = '';
    try {
      const page = await dshDriverConversation(projectId);
      if (version !== generation) return;
      messages = [...page.messages].reverse();
      nextCursor = page.next_cursor; hasMore = page.has_more; endOfHistory = page.end_of_history;
      ready = true;
      if (page.messages.length && page.unread_count > 0) {
        try {
          await markDriverConversationRead(projectId, page.messages[0].message_id);
          if (version === generation) onread();
        } catch { /* the badge is a convenience: a failed mark never fails the read */ }
      }
    } catch (e) { if (version === generation) error = String(e instanceof Error ? e.message : e); }
    finally { if (version === generation) loading = false; }
  }
  // Older messages: the same keyset cursor, so each one arrives exactly once.
  async function loadOlder() {
    if (!nextCursor || loading) return;
    const version = ++generation;
    loading = true; error = '';
    try {
      const page = await dshDriverConversation(projectId, nextCursor);
      if (version !== generation) return;
      messages = [...[...page.messages].reverse(), ...messages];
      nextCursor = page.next_cursor; hasMore = page.has_more; endOfHistory = page.end_of_history;
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
  onMount(() => { void loadNewest(); });
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
