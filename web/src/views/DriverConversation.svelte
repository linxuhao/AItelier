<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { dshDriverConversation, sendDriverConversationMessage, markDriverConversationRead,
    type DriverConversationEntry } from '../lib/api';
  import { isAtNewest, mergeThread, shouldMarkReadOnPoll } from '../lib/conversationThread';
  import { railQuestions, readConversationRows, type OpenQuestion } from '../lib/questions';
  import { st } from '../lib/stateI18n.svelte';
  const { projectId, projectTitle = '', driverId = '', onclose, onread = () => {} }: {
    projectId: string; projectTitle?: string; driverId?: string;
    // Only a host that can actually dismiss the thread passes `onclose`: the
    // dashboard pane always shows it, so it passes nothing and gets no close
    // button (the one it had closed nothing there).
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
  // The question the composer is bound to, when the user chose the reply
  // control on a rail item. One question per send: it closes exactly that one.
  let answering = $state<OpenQuestion | null>(null);
  // ── the rail's row source ─────────────────────────────────────────────────
  // The rail must list every open question of the WHOLE conversation, so it
  // never reads the thread's page: with the newest 30 rows loaded, a question
  // followed by 30 later info messages is no longer in them, and deriving the
  // rail from `messages` moved the "washed away by info" failure from scrolling
  // to paging. `railRows` is therefore its own row set - every page the thread
  // reads is merged into it, and behind the thread's first page the rest of the
  // conversation is read by `loadQuestions` below, bounded, on mount.
  //
  // What is OPEN or ANSWERED is still decided by exactly one thing,
  // `railQuestions`, over these rows; this state is a row set, not a second rule.
  let railRows = $state<DriverConversationEntry[]>([]);
  // The walk stopped before the beginning of the conversation (its page bound,
  // or a failed read), so an older question may be missing. The rail says so
  // instead of looking complete; `readConversationRows` owns the bound.
  //
  // It starts optimistic, so the marker means "the walk answered and did not
  // reach the beginning", not "a read is in flight": until that one request
  // comes back the rail shows the rows the thread has read, which is exactly the
  // state this pane shipped in before. The marker exists for the DURABLE bound,
  // which a reader would otherwise have no way of knowing about.
  let railIncomplete = $state(false);
  // Whether the rest of the conversation behind the thread's first page has
  // been read yet. Seeded from the FIRST page that arrives, not from mount, so
  // a failed first read is still completed by the poll that recovers it.
  let railSeeded = false;
  // Every question of the conversation, answered ones included, in the rule's
  // order: the open ones first, then the answered ones, most recent answer first.
  const railList = $derived(railQuestions(railRows));
  const questions = $derived(railList.filter((question) => question.answered === null));
  const answeredQuestions = $derived(railList.filter((question) => question.answered !== null));
  let text = $state(''), sending = $state(false), sendError = $state('');
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
      railRows = mergeThread(railRows, page.messages);
      if (!paged) { nextCursor = page.next_cursor; hasMore = page.has_more; endOfHistory = page.end_of_history; }
      ready = true;
      seedRail(page);
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
  // ── the rail reads the rest of the conversation, once ─────────────────────
  // The first page the thread renders has already been merged into `railRows`,
  // so what may still be missing is everything BEHIND it - the older rows that
  // hold the questions later info has pushed out of view. When the page was the
  // whole conversation (`end_of_history`) there is nothing behind it and the
  // rail already has every row; otherwise its own cursor is handed to the walk,
  // so no row is read twice.
  //
  // This is the only extra read the rail costs, it runs once per mounted pane,
  // and it never touches the thread: not its cursor, not its merge, not its
  // scroll, not the read marker. Later pages only ever add NEWER rows, which the
  // merges below carry in.
  function seedRail(page: {next_cursor: string | null; end_of_history: boolean}) {
    if (railSeeded) return;
    railSeeded = true;
    if (page.end_of_history) return;
    // No cursor while more history is claimed: nothing to walk, so say so
    // rather than showing a list that looks complete.
    if (!page.next_cursor) { railIncomplete = true; return; }
    void loadQuestions(page.next_cursor);
  }
  async function loadQuestions(from: string | null) {
    const asked = projectId;
    try {
      const history = await readConversationRows(
        (cursor, limit) => dshDriverConversation(asked, cursor, limit), {from});
      if (!alive || asked !== projectId) return;
      railRows = mergeThread(railRows, history.rows);
      railIncomplete = !history.complete;
    } catch {
      // Keep the rows already merged - the thread's own page and any page read
      // since - and say they may be incomplete: a silently short list of open
      // questions is the failure this rail exists to prevent.
      if (alive && asked === projectId) {
        railIncomplete = true;
        // One transient failure must not cost the reader an older question for
        // the life of the pane: release the seed so the next successful poll
        // walks again. The walk is bounded, so a retry costs one more walk.
        railSeeded = false;
      }
    }
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
      // The rail wants every row of the conversation, so a page the reader
      // loaded by hand is folded into it as well.
      railRows = mergeThread(railRows, page.messages);
      nextCursor = page.next_cursor; hasMore = page.has_more; endOfHistory = page.end_of_history;
      paged = true;
      ready = true;
    } catch (e) { if (version === generation) error = String(e instanceof Error ? e.message : e); }
    finally { if (version === generation) loading = false; }
  }
  async function send() {
    const body = text.trim();
    if (!body || sending) return;
    sending = true; sendError = '';
    const replying = replyTo;
    // A rail reply closes exactly the question it is bound to. The cited message
    // is never modified; the link lives on this reply's row.
    const cited = answering ? [{message_id: answering.message_id, segment_id: answering.segment_id}] : null;
    try {
      // `answers` is passed only when a question is bound, so an ordinary send is
      // the same options object it always was.
      await sendDriverConversationMessage(projectId, body, {
        replyToMessageId: replying?.message_id, targetDriverId: driverId || undefined,
        ...(cited ? {answers: cited} : {})});
      text = ''; replyTo = null; answering = null;
      await loadNewest();
      await scrollToNewest();
    } catch (e) { sendError = String(e instanceof Error ? e.message : e); }
    finally { sending = false; }
  }
  function reply(entry: DriverConversationEntry) { replyTo = entry; answering = null; sendError = ''; }
  // A rail item binds the composer to that one question, and the thread's reply
  // target to the message that asked it, so the reply threads onto it too.
  function answer(question: OpenQuestion) {
    const asked = messages.find((m) => m.message_id === question.message_id) ?? null;
    answering = question; replyTo = asked; sendError = '';
  }
  function cancelAnswer() { answering = null; replyTo = null; }

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
      railRows = mergeThread(railRows, page.messages);
      // A poll may be the first page that ever arrived (a failed mount): the
      // rail is seeded from it, exactly as it would have been from the mount.
      seedRail(page);
      if (!paged) { nextCursor = page.next_cursor; hasMore = page.has_more; endOfHistory = page.end_of_history; }
      // A poll that succeeds is what recovered a failed first load: the alert
      // that load left behind no longer describes the thread on screen. A failed
      // "load earlier" keeps its alert, because this refresh did not address it.
      if (!ready) error = '';
      ready = true;
      // A poll may advance the marker ONLY for what the reader could actually be
      // reading: the tab is visible and the thread is parked at its newest end,
      // so the message that just arrived is on screen in front of them. This is
      // the difference between "a timer is a look" (still false) and "a timer
      // while the reader sits at the newest end is a look" (true). A reader
      // scrolled up has not reached what sits below, and a hidden tab has seen
      // nothing - both keep their unread.
      if (page.messages.length && page.unread_count > 0) {
        const newest = messages[messages.length - 1];
        const mayMark = shouldMarkReadOnPoll({
          hidden: typeof document === 'undefined' ? false : document.hidden,
          atNewest: before?.atNewest ?? null,
          newestRenderedId: newest?.message_id ?? null,
          newestOnServerId: page.messages[0]?.message_id ?? null,
        });
        if (mayMark) {
          try { await markDriverConversationRead(asked, newest!.message_id); onread(); }
          catch { /* the badge is a convenience: a failed mark never fails the read */ }
        }
      }
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
     scrolled content, so reaching Send never means scrolling anything else.
     The header is only for a host that has to name and dismiss the thread; in
     the dashboard pane the picker above already names it, and every band here is
     height the thread loses. -->
<section class="conversation" aria-label={st('driverConversation')}>
  {#if onclose}
    <header class="conversation-head">
      <strong>{st('driverConversation')} · {projectTitle || projectId}</strong>
      <button class="outline" type="button" onclick={onclose}>{st('driverMessagesClose')}</button>
    </header>
  {/if}
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
            {#if entry.segments?.some((segment) => segment.kind === 'question')}
              <span class="asks" title={st('messageAsksQuestion')} aria-label={st('messageAsksQuestion')}>?</span>
            {/if}
          </header>
          {#if entry.reply_to_message_id}<p class="identity">{st('driverConversationReplyingTo')} #{entry.reply_to_message_id.slice(0, 8)}</p>{/if}
          <strong class="subject">{entry.subject}</strong>
          {#if entry.segments?.length}
            <!-- Tagged blocks: the question ones are the reason this exists - the
                 reader can see what is being asked without reading a wall of text. -->
            <ul class="segments">
              {#each entry.segments as segment}
                <li class="segment" data-kind={segment.kind}>
                  <span class="seg-tag">{segment.kind === 'question' ? st('segmentQuestion') : st('segmentInfo')}</span>
                  <span class="seg-text">{segment.text}</span>
                </li>
              {/each}
            </ul>
          {:else if entry.body}<pre>{entry.body}</pre>{/if}
        </article>
      {/each}
    {/if}
  </div>
  <!-- The questions rail: a SIBLING of the thread's scroll box, never inside it,
       so the thread scrolling never moves it, and with its own overflow-y.
       It renders nothing when the conversation has no question at all AND the
       walk behind it read the whole conversation, so it takes no width or
       height then. Info blocks never appear here; they stay in the thread. Its
       rows are the WHOLE conversation (`railRows`), not this thread's page, so a
       question 30 or more info messages have pushed off the page is still listed.
       Two groups: the OPEN questions, each with its reply control, and the
       ANSWERED ones (kept so the reader can look back), each marked with the
       answer the user gave and no reply control - it is answered already.
       When the walk stopped short - or failed, or the server claimed more
       history while handing back no cursor - the rail says the list may be
       missing older questions instead of looking complete. That warning is
       rendered even when the shortened list holds NO question: hiding it
       behind the question count was round 2's hole, and a walk that failed
       before finding anything is exactly when the reader needs to be told. -->
  {#if railList.length || railIncomplete}
    <aside class="questions-rail" data-questions-rail aria-label={st('driverQuestionsRail')}>
      {#if questions.length}
        <p class="questions-title">{st('driverQuestionsHeading')} ({questions.length})</p>
      {/if}
      {#if railIncomplete}
        <p class="questions-note" data-questions-incomplete role="status">{st('driverQuestionsIncomplete')}</p>
      {/if}
      {#if questions.length}
        <ul class="questions-list">
          {#each questions as question (question.message_id + ':' + question.segment_id)}
            <li class="question-item" data-question-message-id={question.message_id} data-question-segment-id={question.segment_id}>
              <span class="question-subject">{question.subject}</span>
              <span class="question-text">{question.text}</span>
              <button class="outline" type="button"
                data-answer-message-id={question.message_id} data-answer-segment-id={question.segment_id}
                aria-label="{st('driverQuestionsReply')}: {question.text}"
                onclick={() => answer(question)}>{st('driverQuestionsReply')}</button>
            </li>
          {/each}
        </ul>
      {/if}
      {#if answeredQuestions.length}
        <p class="questions-title" data-questions-answered>{st('driverQuestionsAnswered')} ({answeredQuestions.length})</p>
        <ul class="questions-list questions-list-answered">
          {#each answeredQuestions as question (question.message_id + ':' + question.segment_id)}
            <li class="question-item question-item-answered" data-question-message-id={question.message_id} data-question-segment-id={question.segment_id} data-question-answered-by={question.answered?.message_id}>
              <span class="question-subject">{question.subject}</span>
              <span class="question-text">{question.text}</span>
              <span class="question-answered-note">{st('driverQuestionsAnswerPrefix')}</span><span class="question-answer-text">{question.answered?.text}</span>
            </li>
          {/each}
        </ul>
      {/if}
    </aside>
  {/if}
  <form class="composer" data-conversation-composer onsubmit={(event) => {event.preventDefault(); void send();}}>
    {#if answering}
      <p class="identity" data-answering data-answer-message-id={answering.message_id} data-answer-segment-id={answering.segment_id}>
        {st('driverQuestionsAnswering')} · {answering.text}
        <button class="outline" type="button" onclick={cancelAnswer}>{st('driverConversationCancelReply')}</button></p>
    {:else if replyTo}
      <p class="identity">{st('driverConversationReplyingTo')} · {replyTo.subject}
        <button class="outline" type="button" onclick={() => (replyTo = null)}>{st('driverConversationCancelReply')}</button></p>
    {/if}
    <!-- Send on the input's own row, and the input one line tall by default.
         The visible "write a message" label is gone - the accessible name is on
         the box itself - so the composer costs one line instead of three. The
         box keeps `resize:vertical`, so it still grows when the user drags it. -->
    <div class="composer-row">
      <textarea bind:value={text} maxlength="8000" rows="1" disabled={sending}
        aria-label={st('driverConversationCompose')}></textarea>
      <button type="submit" disabled={sending || !text.trim()}>{sending ? '…' : st('driverMessageSend')}</button>
    </div>
    {#if sendError}<p role="alert">{sendError}</p>{/if}
  </form>
</section>
<style>
  /* The box chain that makes the pane own its scroll: this section is a flex
     column that is allowed to shrink (min-height:0) and clips, the thread is the
     only scroll box, and the composer is a non-shrinking band after it. No
     margin or border of its own: the pane's header already draws the line above
     it, and this band would be height taken from the thread. */
  .conversation{display:flex;flex-direction:column;flex:1 1 auto;min-height:0;overflow-x:hidden;overflow-y:hidden;}
  .conversation-head{display:flex;align-items:center;flex-wrap:wrap;gap:.4rem;flex:0 0 auto;
    padding:.25rem 0;border-top:1px solid var(--pico-muted-border-color,#ddd);}
  .conversation-thread{flex:1 1 auto;min-height:0;overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain;padding-right:.2rem;}
  /* The questions rail: a sibling band between the thread and the composer. It
     scrolls on its own (overflow-y:auto, capped height), so a long list of open
     questions never moves the thread, and the thread never moves it. It is not
     rendered when empty, so it costs no width or height then. */
  .questions-rail{flex:0 1 auto;min-height:0;max-height:9rem;overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain;
    margin:.25rem 0 0;padding:.25rem 0 0;border-top:1px solid var(--pico-muted-border-color,#e6ebf1);}
  .questions-title{margin:0 0 .2rem;font-size:.7rem;font-weight:700;letter-spacing:.03em;text-transform:uppercase;color:var(--pico-muted-color,#64748b);}
  /* The incompleteness marker: one line, ordinary case and muted, so it reads as
     a caveat on the list rather than as another question. */
  .questions-note{margin:0 0 .2rem;font-size:.7rem;line-height:1.3;overflow-wrap:anywhere;color:var(--pico-muted-color,#64748b);}
  .questions-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.3rem;}
  .question-item{display:flex;flex-wrap:wrap;align-items:baseline;gap:.25rem .4rem;font-size:.8rem;line-height:1.35;}
  .question-subject{flex:0 0 auto;color:var(--pico-muted-color,#64748b);font-size:.7rem;}
  .question-text{flex:1 1 8rem;min-width:0;overflow-wrap:anywhere;font-weight:600;}
  /* An answered question: the same row, then the user's own answer under it.
     The answer is bounded to two lines so a long reply cannot take the rail's
     scroll height from the questions still open; the full answer is in the
     thread. The note is muted, so it reads as a caption, not a question. */
  .question-answered-note{flex:0 0 auto;color:var(--pico-muted-color,#64748b);font-size:.7rem;}
  .question-answer-text{flex:1 1 8rem;min-width:0;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
  .question-item-answered{color:var(--pico-muted-color,#475569);}
  .conversation-thread:focus-visible{outline:2px solid var(--pico-primary,#0066cc);outline-offset:-2px;}
  .composer{display:flex;flex-direction:column;flex:0 0 auto;margin:.25rem 0 0;padding-top:.25rem;
    border-top:1px solid var(--pico-muted-border-color,#e6ebf1);}
  /* One row: the input takes the width, the button keeps its own. `min-width:0`
     and `flex-wrap` mean a narrow pane wraps the button under the input instead
     of pushing it out of the box.
     Pico's global form rule is `button[type=submit],select,textarea{width:100%}`:
     left alone it makes the submit button a full-width block and the textarea's
     base size the whole row, so Send would always wrap to a line of its own.
     The two selectors below out-specify that rule and pin the row to one line. */
  .composer-row{display:flex;flex-direction:row;flex-wrap:wrap;align-items:flex-end;gap:.35rem;}
  .composer-row textarea{flex:1 1 6rem;width:auto;min-width:0;margin:0;resize:vertical;font-size:.8rem;
    line-height:1.35;padding:.25rem .4rem;}
  .composer-row button[type="submit"]{flex:0 0 auto;width:auto;margin:0;}
  button{font-size:.7rem;padding:.2rem .5rem;margin:0 0 0 .5rem;width:auto;}
  p{margin:.4rem 0;}
  .asks{display:inline-flex;align-items:center;justify-content:center;width:1.1rem;height:1.1rem;
    border-radius:999px;background:var(--pico-primary,#2563eb);color:#fff;font-size:.7rem;font-weight:700;}
  .segments{list-style:none;margin:.35rem 0 0;padding:0;display:flex;flex-direction:column;gap:.3rem;}
  .segment{display:flex;gap:.4rem;align-items:baseline;font-size:.82rem;line-height:1.45;}
  .seg-tag{flex:0 0 auto;font-size:.62rem;letter-spacing:.04em;text-transform:uppercase;
    padding:.05rem .35rem;border-radius:999px;border:1px solid var(--pico-muted-border-color,#dbe3ec);color:var(--pico-muted-color,#64748b);}
  .segment[data-kind="question"] .seg-tag{border-color:var(--pico-primary,#2563eb);color:var(--pico-primary,#2563eb);}
  .segment[data-kind="question"] .seg-text{font-weight:600;}
  .identity{overflow-wrap:anywhere;color:var(--pico-muted-color,#64748b);}
  .message{margin:.45rem 0;padding:.35rem .55rem;border:1px solid var(--pico-muted-border-color,#ddd);border-radius:6px;}
  .message.mine{border-color:var(--pico-primary-border,#93c5fd);}
  .message header{display:block;}
  .message .sender{font-weight:600;}
  .message .subject{display:block;margin:.2rem 0;}
  pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:20rem;overflow-y:auto;overflow-x:hidden;font-size:.75rem;padding:.5rem;margin:.25rem 0 0;}
</style>
