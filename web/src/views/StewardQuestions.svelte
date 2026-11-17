<script lang="ts">
  import { untrack } from 'svelte';
  import { dshDriverConversation } from '../lib/api';
  import { openQuestions, readConversationRows, type OpenQuestion } from '../lib/questions';
  import { totalOpenQuestions } from '../lib/steward';
  import type { StateProjectRow } from '../lib/stateGraph';
  import { t } from '../lib/i18n.svelte';
  // Every project's open questions in one place. Which projects have any, and
  // how many, is the per-project badge map the project list already shows; the
  // questions themselves come from each conversation through `openQuestions`,
  // the same rule the conversation's own questions box uses. Nothing is counted
  // a second way.
  const { questions = {}, projects = [], onanswer }: {
    questions?: Record<string, number>; projects?: StateProjectRow[];
    onanswer: (projectId: string) => void;
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
        <button type="button" class="outline" onclick={() => onanswer(id)}>{t('steward.goAnswer')}</button>
      </header>
      {#if !group}<p aria-busy="true">{t('steward.loading')}</p>
      {:else}
        {#if group.failed}<p class="note" role="status">{t('steward.questionsReadFailed')}</p>{/if}
        <ul>{#each group.questions as question (question.message_id + ':' + question.segment_id)}<li>{excerpt(question.text)}</li>{/each}</ul>
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
  ul{margin:0;padding-left:18px;}li{list-style:disc;margin:0 0 4px;}
</style>
