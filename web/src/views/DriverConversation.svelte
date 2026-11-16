<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { dshDriverConversation, sendDriverConversationMessage, markDriverConversationRead,
    type DriverConversationEntry } from '../lib/api';
  import { isAtNewest, mergeThread, shouldMarkReadOnPoll } from '../lib/conversationThread';
  import { railQuestions, readConversationRows, type OpenQuestion } from '../lib/questions';
  import { renderMarkdown } from '../lib/markdown';
  import type { ConversationDraft } from '../lib/conversationDraft';
  import { st } from '../lib/stateI18n.svelte';
  const { projectId, projectTitle = '', driverId = '', onclose, onread = () => {}, drafts }: {
    projectId: string; projectTitle?: string; driverId?: string;
    // Only a host that can actually dismiss the thread passes `onclose`: the
    // dashboard pane always shows it, so it passes nothing and gets no close
    // button (the one it had closed nothing there).
    onclose?: () => void; onread?: () => void; drafts?: Map<string, ConversationDraft>;
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
  const questions = $derived(railList.filter((question) => question.answered === null && question.withdrawn === null));
  const answeredQuestions = $derived(railList.filter((question) => question.answered !== null));
  // Questions their asker took back (withdraw_user_question), kept visible with the reason.
  const withdrawnQuestions = $derived(railList.filter((question) => question.withdrawn !== null));
  // The questions box is permanent and foldable. Open by default; the user's
  // toggle is kept here, so a new message (which re-renders the thread, not this
  // box) never resets it for the life of the mounted pane.
  let questionsOpen = $state(true);
  let text = $state(''), sending = $state(false), sendError = $state('');
  let input = $state<HTMLTextAreaElement | null>(null);
  let selectedQuestion = $state('');
  let draftReady = $state(false);
  $effect(() => {
    if (draftReady) drafts?.set(projectId, {text, replyTo, answering});
  });
  function composeKey(event: KeyboardEvent) {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.isComposing) {
      event.preventDefault(); void send();
    }
  }
  function questionKey(question: OpenQuestion) { return question.message_id + ':' + question.segment_id; }
  function replyLabel(entry: DriverConversationEntry): string {
    const parent = messages.find(message => message.message_id === entry.reply_to_message_id);
    return parent ? echoText(parent.sender_kind === 'user' ? messageText(parent) : parent.subject || messageText(parent))
      : '#' + entry.reply_to_message_id?.slice(0, 8);
  }

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
        replyToMessageId: answering?.message_id ?? replying?.message_id, targetDriverId: driverId || undefined,
        ...(cited ? {answers: cited} : {})});
      text = ''; replyTo = null; answering = null;
      // A response can finish after switching projects. Preserve newer text
      // if the user returned and started another draft in the meantime.
      if (drafts?.get(projectId)?.text.trim() === body) drafts.delete(projectId);
      onread();
      await loadNewest();
      await scrollToNewest();
    } catch (e) { sendError = String(e instanceof Error ? e.message : e); }
    finally { sending = false; }
  }
  function focusComposer() {
    const width = thread?.parentElement?.clientWidth ?? 0;
    // On compact layouts the question drawer shares the vertical space with
    // messages. Keep its count visible and give that space to the reply.
    if (width > 0 && width <= 680) questionsOpen = false;
    input?.focus();
  }
  function reply(entry: DriverConversationEntry) { replyTo = entry; answering = null; sendError = ''; focusComposer(); }
  // A rail item binds the composer to that one question, and the thread's reply
  // target to the message that asked it, so the reply threads onto it too.
  function answer(question: OpenQuestion) {
    const asked = messages.find((m) => m.message_id === question.message_id) ?? null;
    answering = question; replyTo = asked; sendError = ''; focusComposer();
  }
  function cancelAnswer() { answering = null; replyTo = null; }
  // ── the composer's echo is bounded ────────────────────────────────────────
  // The band above the input echoes the question this reply is bound to, and a
  // question can be thousands of characters. `.composer` is `flex:0 0 auto`
  // inside `.conversation`, a fixed-height clipped column, so the echo used to
  // grow the band until the input row and Send were pushed past the pane's
  // bottom edge with no scroll path back to them: replying to a long question
  // made the reply box disappear. The echo therefore renders at most ECHO_CHARS
  // characters of the question, whitespace collapsed and trimmed first, and the
  // whole of it stays reachable through the echo element's `title`.
  const ECHO_CHARS = 168;
  function echoText(raw: string): string {
    const flat = (raw ?? '').replace(/\s+/g, ' ').trim();
    return flat.length > ECHO_CHARS ? flat.slice(0, ECHO_CHARS).replace(/\s+$/, '') + '…' : flat;
  }
  // The text a message shows in the thread, once. A body is the whole message;
  // a message sent with an empty body but tagged segments shows its segment texts
  // instead, so segmented text is never lost. No per-segment tags: one message,
  // one piece.
  function messageText(entry: DriverConversationEntry): string {
    if (entry.body) return entry.body;
    return (entry.segments ?? []).map((segment) => segment.text).filter((text) => text.trim()).join('\n');
  }

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
    const draft = drafts?.get(projectId);
    if (draft) { text = draft.text; replyTo = draft.replyTo; answering = draft.answering; }
    draftReady = true;
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
<!-- The message viewport, question sidebar and composer own separate grid areas. -->
<section class="conversation" aria-label={st('driverConversation')}>
  <div class="conversation-status">
    {#if onclose}
      <header class="conversation-head">
        <strong>{st('driverConversation')} · {projectTitle || projectId}</strong>
        <button class="outline" type="button" onclick={onclose}>{st('driverMessagesClose')}</button>
      </header>
    {/if}
    {#if error}<p role="alert">{error}</p>{/if}
    {#if loading && !ready}<p role="status">{st('loading')}</p>{/if}
  </div>
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
        <article class="message" data-message-id={entry.message_id} class:mine={entry.sender_kind === 'user'}>
          <header>
            <span class="sender">{entry.sender_kind === 'user' ? st('driverConversationYou') : (entry.sender_driver_id ?? st('driverConversationDriver'))}</span>
            <time class="identity" datetime={entry.created_at} title={new Date(entry.created_at).toLocaleString()}>{new Date(entry.created_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}</time>
            {#if entry.sender_kind === 'driver'}
              <button class="outline" type="button" aria-label="{st('driverConversationReply')}: {entry.subject}" onclick={() => reply(entry)}>{st('driverConversationReply')}</button>
            {/if}
            {#if entry.segments?.some((segment) => segment.kind === 'question')}
              <span class="asks" title={st('messageAsksQuestion')} aria-label={st('messageAsksQuestion')}>?</span>
            {/if}
          </header>
          {#if entry.reply_to_message_id}<p class="identity">{st('driverConversationReplyingTo')} {replyLabel(entry)}</p>{/if}
          {#if entry.sender_kind !== 'user'}<strong class="subject">{entry.subject}</strong>{/if}
          <!-- The message's text, rendered once: its body, or - when the body is
               empty - its segment texts. A message is not split into info and
               question blocks: a split would scatter one message across the thread,
               and the questions it asks live in the question sidebar, which later
               info messages cannot wash away. -->
          {#if messageText(entry)}
            <div class="message-content" class:plain={entry.sender_kind === 'user'}>
              {#if entry.sender_kind === 'user'}{messageText(entry)}{:else}
                <!-- eslint-disable-next-line svelte/no-at-html-tags -- renderMarkdown sanitizes with DOMPurify -->
                {@html renderMarkdown(messageText(entry))}
              {/if}
            </div>
          {/if}
        </article>
      {/each}
    {/if}
  </div>
  <!-- The complete question history stays independent of thread pagination. -->
  <details class="questions-box" data-questions-box aria-label={st('driverQuestionsRail')} bind:open={questionsOpen}>
    <summary class="questions-summary">
      <span class="questions-summary-label">{st('driverQuestionsHeading')} ({questions.length})</span>
      {#if railIncomplete}
        <span class="questions-note-short" data-questions-incomplete-summary>{st('driverQuestionsIncompleteShort')}</span>
      {/if}
    </summary>
    <!-- Rendered only while open: a collapsed box is its one summary line and
         carries no question item, so nothing below the fold is in the DOM or
         reachable by keyboard. The fold itself is still the native <details>. -->
    {#if questionsOpen}
      <div class="questions-body">
        {#if railIncomplete}
          <p class="questions-note" data-questions-incomplete role="status">{st('driverQuestionsIncomplete')}</p>
        {/if}
        <!-- "No open questions." is a claim about the conversation, so it waits for
             a page to have actually been read (before that, or after a failed first
             load, the box must not assert an empty list it never checked), and it
             never sits under the incomplete caveat: a list that stopped short has
             not shown there are none. -->
        {#if ready && !railIncomplete && railList.length === 0}
          <p class="questions-empty" data-questions-empty>{st('driverQuestionsEmpty')}</p>
        {/if}
        {#if questions.length}
          <ul class="questions-list">
            {#each questions as question (question.message_id + ':' + question.segment_id)}
              <li class="question-item" data-question-message-id={question.message_id} data-question-segment-id={question.segment_id}>
                <button class="question-preview" type="button" aria-expanded={selectedQuestion === questionKey(question)}
                  onclick={() => selectedQuestion = selectedQuestion === questionKey(question) ? '' : questionKey(question)}>
                  <span class="question-subject" title={question.subject}>{question.subject}</span>
                  <span class="question-text" class:expanded={selectedQuestion === questionKey(question)}>{question.text}</span>
                </button>
                <button class="outline" type="button"
                  data-answer-message-id={question.message_id} data-answer-segment-id={question.segment_id}
                  aria-label="{st('driverQuestionsReply')}: {question.text}"
                  onclick={() => answer(question)}>{st('driverQuestionsReply')}</button>
              </li>
            {/each}
          </ul>
        {/if}
        {#if answeredQuestions.length}
          <details class="answered-group"><summary class="questions-title" data-questions-answered>{st('driverQuestionsAnswered')} ({answeredQuestions.length})</summary>
          <ul class="questions-list questions-list-answered">
            {#each answeredQuestions as question (question.message_id + ':' + question.segment_id)}
              <li class="question-item question-item-answered" data-question-message-id={question.message_id} data-question-segment-id={question.segment_id} data-question-answered-by={question.answered?.message_id}>
                <span class="question-subject" title={question.subject}>{question.subject}</span>
                <span class="question-text">{question.text}</span>
                <span class="question-answered-note">{st('driverQuestionsAnswerPrefix')}</span><span class="question-answer-text">{question.answered?.text}</span>
              </li>
            {/each}
          </ul>
          </details>
        {/if}
        {#if withdrawnQuestions.length}
          <details class="answered-group withdrawn-group"><summary class="questions-title" data-questions-withdrawn>{st('driverQuestionsWithdrawn')} ({withdrawnQuestions.length})</summary>
          <ul class="questions-list questions-list-answered">
            {#each withdrawnQuestions as question (question.message_id + ':' + question.segment_id)}
              <li class="question-item question-item-answered" data-question-message-id={question.message_id} data-question-segment-id={question.segment_id} data-question-withdrawn-by={question.withdrawn?.message_id}>
                <span class="question-subject" title={question.subject}>{question.subject}</span>
                <span class="question-text">{question.text}</span>
                <span class="question-answered-note">{st('driverQuestionsWithdrawnPrefix')}</span><span class="question-answer-text">{question.withdrawn?.reason}</span>
              </li>
            {/each}
          </ul>
          </details>
        {/if}
      </div>
    {/if}
  </details>
  <form class="composer" data-conversation-composer onsubmit={(event) => {event.preventDefault(); void send();}}>
    {#if answering}
      <p class="identity composer-echo" data-answering data-answer-message-id={answering.message_id} data-answer-segment-id={answering.segment_id}
        title={answering.text}>
        {st('driverQuestionsAnswering')} · {echoText(answering.text)}
        <button class="outline" type="button" onclick={cancelAnswer}>{st('driverConversationCancelReply')}</button></p>
    {:else if replyTo}
      <p class="identity composer-echo" title={replyTo.subject}>
        {st('driverConversationReplyingTo')} · {echoText(replyTo.subject)}
        <button class="outline" type="button" onclick={() => (replyTo = null)}>{st('driverConversationCancelReply')}</button></p>
    {/if}
    <div class="composer-row">
      <textarea bind:this={input} bind:value={text} maxlength="8000" rows="2" disabled={sending}
        onkeydown={composeKey} placeholder={st('driverConversationPlaceholder')}
        aria-label={st('driverConversationCompose')}></textarea>
    </div>
    <div class="composer-footer"><span>{st('driverConversationShortcut')}</span>
      <button type="submit" disabled={sending || !text.trim()}>{sending ? '…' : st('driverMessageSend')}</button>
    </div>
    {#if sendError}<p role="alert">{sendError}</p>{/if}
  </form>
</section>
<style>
  .conversation{display:grid;grid-template-columns:minmax(0,1fr) 280px;grid-template-rows:auto minmax(0,1fr) auto;flex:1 1 auto;min-height:0;min-width:0;overflow:hidden;font-size:14px;}
  .conversation-status{grid-column:1;grid-row:1;min-width:0;}
  .conversation-status p{font-size:13px;padding:8px 16px;margin:0;}
  .conversation-head{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;font-size:14px;}
  .conversation-thread{grid-column:1;grid-row:2;min-height:0;min-width:0;overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain;padding:20px 24px 8px;}
  .identity{font-size:12px;overflow-wrap:anywhere;color:var(--pico-muted-color);}
  .conversation-thread>.identity{text-align:center;margin:0 0 18px;}
  .message{margin:0 0 24px;padding:0;border:0;border-radius:0;box-shadow:none;background:transparent;min-width:0;overflow-wrap:anywhere;}
  .message.mine{margin-left:15%;}
  .message header{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 8px;padding:0;background:transparent;border:0;font-size:12px;color:var(--pico-muted-color);}
  .mine header{justify-content:flex-end;}
  .sender{font-weight:600;}.message .subject{display:block;margin:0 0 8px;font-size:14px;font-weight:600;}
  .message-content{font-size:14px;line-height:1.7;overflow-wrap:anywhere;min-width:0;}
  .message-content.plain{white-space:pre-wrap;padding:10px 14px;border-radius:10px 2px 10px 10px;background:color-mix(in srgb,var(--pico-primary) 9%,var(--pico-background-color));}
  .message-content :global(p){margin:0 0 10px;}.message-content :global(p:last-child){margin-bottom:0;}
  .message-content :global(ul),.message-content :global(ol){margin:8px 0;padding-left:22px;}
  .message-content :global(pre){max-width:100%;overflow-x:auto;padding:12px;font-size:12px;}
  .message-content :global(code){font-size:12px;overflow-wrap:anywhere;}
  .message-content :global(h1),.message-content :global(h2),.message-content :global(h3){font-size:16px;margin:16px 0 8px;}
  .message-content :global(table){display:block;max-width:100%;overflow-x:auto;font-size:13px;}
  .message-content :global(blockquote){margin:10px 0;padding:4px 12px;font-size:13px;}
  .questions-box{position:relative;grid-column:2;grid-row:1/-1;display:flex;flex-direction:column;min-width:0;min-height:0;margin:0;padding:18px 14px;border:0;border-left:1px solid var(--pico-muted-border-color);background:var(--pico-card-sectioning-background-color);overflow:hidden;}
  .questions-summary{flex:0 0 auto;margin:0;font-size:13px;font-weight:600;overflow-wrap:anywhere;cursor:pointer;}
  .questions-note-short{display:block;margin-top:4px;font-size:11px;font-weight:400;color:var(--pico-muted-color);}
  .questions-body{position:absolute;top:62px;bottom:14px;left:14px;right:14px;min-height:0;overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain;margin:0;}
  .questions-box:has(.questions-note-short) .questions-body{top:82px;}
  .questions-empty,.questions-note{margin:0 0 12px;font-size:12px;color:var(--pico-muted-color);}
  .questions-list{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:10px;}
  .question-item{display:flex;flex-direction:column;align-items:stretch;min-width:0;gap:8px;margin:0;padding:12px;border:1px solid var(--pico-muted-border-color);border-radius:8px;background:var(--pico-background-color);overflow-wrap:anywhere;}
  .question-item:has(.question-preview[aria-expanded=true]){border-color:var(--pico-primary);}
  .question-preview{display:flex;flex-direction:column;gap:6px;width:100%;margin:0;padding:0;border:0;border-radius:0;box-shadow:none;background:transparent;color:inherit;text-align:left;}
  .question-subject{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-width:0;font-size:11px;line-height:1.5;color:var(--pico-muted-color);overflow-wrap:anywhere;}
  .question-text{display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden;min-width:0;font-size:13px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere;}
  .question-text.expanded{-webkit-line-clamp:unset;}
  .question-item>button.outline{background:transparent;color:var(--pico-primary);align-self:flex-start;margin:0;font-size:12px;}
  .answered-group{margin:18px 0 0;padding-top:14px;border-top:1px solid var(--pico-muted-border-color);}
  .questions-title{margin-bottom:12px;font-size:12px;color:var(--pico-muted-color);}
  .question-item-answered .question-text{-webkit-line-clamp:2;}
  .question-answered-note{font-size:11px;color:var(--pico-muted-color);}
  .question-answer-text{font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;}
  .composer{grid-column:1;grid-row:-2/-1;min-width:0;flex:0 0 auto;margin:0;padding:12px 20px 16px;border-top:1px solid var(--pico-muted-border-color);}
  .composer-echo{display:flex;align-items:flex-start;gap:8px;justify-content:space-between;max-height:60px;overflow-y:auto;overflow-wrap:anywhere;margin:0 0 8px;padding:8px 10px;background:color-mix(in srgb,var(--pico-primary) 8%,var(--pico-background-color));border-radius:6px;color:var(--pico-primary);font-size:12px;}
  .composer-echo button{flex-shrink:0;margin:0;}
  .composer-row textarea{width:100%;min-width:0;min-height:64px;max-height:160px;resize:vertical;margin:0;padding:10px 12px;font-size:14px;line-height:1.6;overflow-y:auto;}
  .composer-footer{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-top:8px;}
  .composer-footer span{font-size:11px;color:var(--pico-muted-color);}
  .composer-footer button{margin:0;}
  button{width:auto;font-size:12px;padding:5px 9px;margin:0;}
  .asks{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:18px;border-radius:5px;background:var(--pico-primary);color:var(--pico-primary-inverse);font-size:11px;}
  @container(max-width:900px){.conversation{grid-template-columns:minmax(0,1fr) 240px;}.conversation-thread{padding:16px;}.composer{padding:12px 16px;}}
  @container(max-width:680px){
    .conversation{grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr) auto auto;}
    .questions-box{grid-column:1;grid-row:-3/-2;border-left:0;border-top:1px solid var(--pico-muted-border-color);padding:10px 12px;max-height:170px;}
    .questions-box:has(.questions-note-short) .questions-body{top:60px;}
    .questions-box[open]{height:170px;}
    .questions-body{top:40px;bottom:10px;left:12px;right:12px;}.questions-list{gap:8px;}
    .conversation-thread{padding:14px 12px;}.composer{padding:10px 12px;}
    .composer-row textarea{min-height:54px;font-size:16px;}.composer-footer span{font-size:11px;}
  }
  @media(pointer:coarse){button{min-height:44px;}.question-preview{min-height:44px;}}
</style>
