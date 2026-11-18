<script lang="ts">
  import { untrack } from 'svelte';
  import { dshDriverConversation, sendDriverConversationMessage, type DriverStatusResponse } from '../lib/api';
  import { openQuestions, readConversationRows, type OpenQuestion } from '../lib/questions';
  import { totalOpenQuestions } from '../lib/steward';
  import type { StateProjectRow } from '../lib/stateGraph';
  import { t } from '../lib/i18n.svelte';
  // Every project's open questions in one place. Which projects have any, and
  // how many, is the per-project badge map the project list already shows; the
  // questions themselves come from each conversation through `openQuestions`,
  // the same rule the conversation's own questions box uses. Nothing is counted
  // a second way.
  const { questions = {}, projects = [], status = null, onanswer, onanswered = () => {} }: {
    questions?: Record<string, number>; projects?: StateProjectRow[]; status?: DriverStatusResponse | null;
    onanswer: (projectId: string) => void;
    /** A question was answered here: re-read the badges, so the counts follow. */
    onanswered?: () => void;
  } = $props();
  type Group = {questions: OpenQuestion[]; complete: boolean; failed: boolean};
  let groups = $state<Record<string, Group>>({});
  const total = $derived(totalOpenQuestions(questions));
  const ids = $derived(Object.keys(questions).filter(id => questions[id] > 0).sort());
  // Re-read only when the badges themselves change, not on every poll.
  const signature = $derived(ids.map(id => id + ':' + questions[id]).join(','));
  let generation = 0;
  $effect(() => {
    void signature;
    const wanted = untrack(() => ids);
    const version = ++generation;
    for (const id of wanted) {
      readConversationRows((cursor, limit) => dshDriverConversation(id, cursor, limit))
        .then(read => { if (version === generation) groups[id] = {questions: openQuestions(read.rows), complete: read.complete, failed: false}; })
        .catch(() => { if (version === generation) groups[id] = {questions: groups[id]?.questions ?? [], complete: false, failed: true}; });
    }
  });
  // Answering in place (owner 2026-11-18): the same message the project's own
  // conversation sends for a rail answer - that project, the reply threaded onto
  // the asking message, its driver addressed, `answers` citing exactly this question.
  const key = (question: OpenQuestion) => question.message_id + ':' + question.segment_id;
  let opened = $state<Record<string, boolean>>({}), drafts = $state<Record<string, string>>({}),
    sending = $state<Record<string, boolean>>({}), errors = $state<Record<string, string>>({});
  async function send(id: string, question: OpenQuestion) {
    const k = key(question), body = (drafts[k] ?? '').trim();
    if (!body || sending[k]) return;
    sending[k] = true; errors[k] = '';
    try {
      await sendDriverConversationMessage(id, body, {
        replyToMessageId: question.message_id, targetDriverId: status?.projects[id]?.managed?.driver_id || undefined,
        answers: [{message_id: question.message_id, segment_id: question.segment_id}]});
      delete drafts[k]; delete opened[k];
      const group = groups[id];
      if (group) groups[id] = {...group, questions: group.questions.filter(q => key(q) !== k)};
      onanswered();
    } catch (e) { errors[k] = String(e instanceof Error ? e.message : e); }
    finally { sending[k] = false; }
  }
  function title(id: string): string { return projects.find(p => p.project_id === id)?.title ?? id; }
  function excerpt(text: string): string {
    const flat = text.replace(/\s+/g, ' ').trim();
    return flat.length > 200 ? flat.slice(0, 200) + '…' : flat;
  }
</script>
<section class="steward-questions" aria-label={t('steward.tabQuestions')}>
  <p class="total">{t('steward.questionsTotal')}: <strong data-summary-total>{total}</strong></p>
  {#if !ids.length}<p class="empty">{t('steward.questionsEmpty')}</p>{/if}
  {#each ids as id (id)}
    {@const group = groups[id]}
    <article class="question-group" data-summary-project={id}>
      <header>
        <span class="tag">{title(id)}</span>
        <span class="count" data-summary-count={group && !group.failed ? group.questions.length : questions[id]}>{group && !group.failed ? group.questions.length : questions[id]}</span>
        <button type="button" class="secondary outline" onclick={() => onanswer(id)}>{t('steward.goAnswer')}</button>
      </header>
      {#if !group}<p aria-busy="true">{t('steward.loading')}</p>
      {:else}
        {#if group.failed}<p class="note" role="status">{t('steward.questionsReadFailed')}</p>{/if}
        <ul>{#each group.questions as question (key(question))}
          {@const k = key(question)}
          <li data-question={k}>
            <span>{excerpt(question.text)}</span>
            {#if opened[k]}
              <form class="answer" onsubmit={(e) => { e.preventDefault(); void send(id, question); }}>
                <textarea rows="3" bind:value={drafts[k]} aria-label={t('steward.answerLabel')}
                  placeholder={t('steward.answerLabel')} disabled={sending[k]}></textarea>
                {#if errors[k]}<p class="error" role="alert">{errors[k]}</p>{/if}
                <div class="answer-actions">
                  <button type="submit" disabled={sending[k] || !drafts[k]?.trim()} aria-busy={sending[k] || undefined}>{t('steward.answerSend')}</button>
                  <button type="button" class="secondary outline" onclick={() => opened[k] = false}>{t('steward.cancel')}</button>
                </div>
              </form>
            {:else}
              <button type="button" class="outline answer-toggle" onclick={() => { drafts[k] ??= ''; opened[k] = true; }}>{t('steward.answer')}</button>
            {/if}
          </li>
        {/each}</ul>
        {#if !group.complete && !group.failed}<p class="note">{t('steward.questionsIncomplete')}</p>{/if}
      {/if}
    </article>
  {/each}
</section>
<style>
  .total{font-size:13px;margin:0 0 10px;}
  .empty,.note{color:var(--pico-muted-color);font-size:12px;}
  .question-group{margin:0 0 10px;padding:10px 12px;border:1px solid var(--pico-muted-border-color);border-radius:8px;font-size:13px;}
  header{display:flex;align-items:center;gap:8px;margin:0 0 6px;padding:0;background:none;border:0;}
  .tag{padding:1px 8px;border-radius:5px;background:var(--pico-card-sectioning-background-color);font-weight:600;overflow-wrap:anywhere;}
  .count{padding:1px 6px;border-radius:5px;font-size:11px;background:light-dark(#fff3df,#3c3020);color:light-dark(#925508,#f2bd70);}
  header button{margin:0 0 0 auto;width:auto;padding:3px 10px;font-size:12px;}
  ul{margin:0;padding-left:18px;}li{list-style:disc;margin:0 0 8px;overflow-wrap:anywhere;}
  .answer-toggle{display:block;width:auto;margin:4px 0 0;padding:2px 10px;font-size:12px;}
  .answer{margin:6px 0 0;}.answer textarea{margin:0 0 6px;font-size:13px;}
  .answer-actions{display:flex;gap:8px;}.answer-actions button{width:auto;margin:0;padding:4px 12px;font-size:12px;}
  .error{color:var(--pico-del-color);font-size:12px;margin:0 0 6px;}
</style>
