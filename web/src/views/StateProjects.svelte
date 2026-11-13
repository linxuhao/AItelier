<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { authStore } from '../stores/auth';
  import { driverStatus, dshDriverConversationUnread, stateProjects, type DriverStatusResponse } from '../lib/api';
  import { isArchived, type StateProjectRow } from '../lib/stateGraph';
  import { rememberProject } from '../lib/navigation.svelte';
  import { driverState, driverStateLabelKey, sortDagRows } from '../lib/driverList';
  import { st } from '../lib/stateI18n.svelte';
  import DriverConversationPane from './DriverConversationPane.svelte';
  const { params = {} }: {params?: {repoPath?: string}} = $props();
  let rows = $state<StateProjectRow[]>([]), error = $state(''), loading = $state(false);
  let query = $state(''), next = $state<string | null>(null), retry = $state(0), showArchived = $state(false);
  let generation = 0;
  // The project catalog is a public read; only the write affordances need
  // `canWrite`.
  const canRead = $derived($authStore.permissionResolved);
  const visible = $derived(showArchived ? rows : rows.filter(p => !isArchived(p)));
  const archivedHidden = $derived(rows.length - visible.length);
  const filtered = $derived(visible.filter(p => (p.title + ' ' + p.project_id).toLocaleLowerCase().includes(query.toLocaleLowerCase())));
  // Per-DAG driver state and the two persisted badges: unread driver messages,
  // and the open questions no reply has answered yet. Read from State and then
  // ordered by the SAME running-first rule the driver table uses.
  let drivers = $state<DriverStatusResponse | null>(null), unread = $state<Record<string, number>>({});
  let questions = $state<Record<string, number>>({});
  // Which DAG the conversation pane is about. The list sets it; the pane shows it.
  let paneSelection = $state('');
  let driverTimer: ReturnType<typeof setInterval> | undefined, driverMounted = false;
  const ordered = $derived(sortDagRows(filtered, drivers));
  async function loadDriverState() {
    if (!$authStore.canWrite) return;
    try {
      const [table, badge] = await Promise.all([driverStatus(), dshDriverConversationUnread()]);
      if (!driverMounted) return;
      drivers = table; unread = badge.projects ?? {}; questions = badge.questions ?? {};
    } catch { /* badges are additive: a failed poll never hides the DAG list */ }
  }
  $effect(() => { if ($authStore.permissionResolved && $authStore.canWrite && driverMounted) void loadDriverState(); });
  $effect(() => {
    const permitted = canRead, repo = params.repoPath; void retry;
    const version = ++generation;
    rows = []; next = null; error = '';
    if (!permitted) { loading = false; return; }
    loading = true;
    stateProjects(repo).then(result => {
      if (version !== generation) return;
      rows = result.projects; next = result.next_after;
    }).catch(e => { if (version === generation) error = String(e.message ?? e); })
      .finally(() => { if (version === generation) loading = false; });
    return () => { generation++; };
  });
  $effect(() => { if ($authStore.permissionResolved && !$authStore.canWrite) { drivers = null; unread = {}; questions = {}; } });
  onMount(() => {
    driverMounted = true;
    void loadDriverState();
    driverTimer = setInterval(loadDriverState, 10000);
  });
  onDestroy(() => { driverMounted = false; if (driverTimer) clearInterval(driverTimer); });
  async function more() {
    if (!next || loading) return;
    const version = generation; loading = true; error = '';
    try {
      const result = await stateProjects(params.repoPath, next);
      if (version === generation) { rows = [...rows, ...result.projects]; next = result.next_after; }
    } catch (e) { if (version === generation) error = String(e instanceof Error ? e.message : e); }
    finally { if (version === generation) loading = false; }
  }
</script>
<div class="dag-split">
<section class="state-projects">
  <header><div><p class="eyebrow">AITELIER / PROJECTS</p><h1>{st('projects')}</h1><p>{st('intro')}</p></div>
    <a href="#/repos">{st('dashboard')} ↗</a></header>
  {#if !canRead}<p role="status">{st('private')}</p>
  {:else}
    <label>{st('search')}<input type="search" bind:value={query} placeholder={st('search')} /></label>
    <label class="archived-toggle"><input type="checkbox" bind:checked={showArchived} /> {st('showArchived')}{#if archivedHidden} · {st('archivedHidden')}: {archivedHidden}{/if}</label>
    {#if params.repoPath}<p><code>{params.repoPath}</code></p>{/if}
    {#if error}<div role="alert"><p>{error}</p><button class="outline" onclick={() => retry++}>{st('retry')}</button></div>{/if}
    {#if loading && !rows.length}<p aria-live="polite">{st('loading')}</p>
    {:else if !rows.length && !error}<article><p>{st('empty')}</p></article>{/if}
    <div class="project-grid">
      {#each ordered as project (project.project_id)}
        {@const phase = drivers?.projects[project.project_id]?.managed?.phase}
        {@const activity = drivers?.projects[project.project_id]}
        <!-- This was a link into the DAG. Selecting which DAG the conversation
             is about belongs here now, and the way INTO a DAG moved to the
             pane header - the two swapped, they were not both added. -->
        <button type="button" class="project-card" aria-pressed={paneSelection === project.project_id}
          onclick={() => { paneSelection = project.project_id; rememberProject(paneSelection); }}>
          <div class="card-top"><span>{project.project_id}</span><span class:held={project.policy.dispatch !== 'active'}>{project.policy.dispatch}</span></div>
          <h2>{project.title}</h2>
          {#if $authStore.canWrite}
            <!-- Everything the driver is doing for THIS project, on the button
                 that opens it: phase, drivers online/total, and what is running
                 right now. All four read from the same /api/dsh-drivers row. -->
            <p class="driver-line">
              <span class="driver-state" data-phase={driverState(phase)} title={st(driverStateLabelKey(phase))}>{st(driverStateLabelKey(phase))}</span>
              <span class="driver-facts" aria-label={st('driverFacts')}>
                <span class="fact"><strong data-fact="drivers">{activity?.online ?? 0}/{activity?.total ?? 0}</strong> {st('driverCount')}</span>
                <span class="fact"><strong data-fact="subagents">{activity?.running_subagents ?? 0}</strong> {st('driverSubagents')}</span>
                <span class="fact"><strong data-fact="pipelines">{activity?.running_pipelines ?? 0}</strong> {st('driverPipelines')}</span>
              </span>
              {#if unread[project.project_id]}<span class="unread" title={st('driverConversationUnread')}>{unread[project.project_id]}</span>{/if}
              <!-- Its own pill, not folded into the unread count: unread is
                   about messages and clearing it answers nothing, while this
                   counts questions still owed an answer. Rendered only when a
                   project HAS one, so a zero never reads as a badge. -->
              {#if questions[project.project_id]}<span class="questions" data-pending-questions={questions[project.project_id]} title={st('driverQuestionsBadge')}>{questions[project.project_id]}</span>{/if}
            </p>
          {/if}
          <div class="counts"><span><strong>{project.node_count}</strong> {st('total')}</span><span><strong>{project.verified_count}</strong> {st('verified')}</span></div>
          <!-- The binding is the SERVER's absolute path. It is a fact about the
               host, not about the project, so only a writer sees it; an
               anonymous visitor gets no source row at all (not even the
               "no source" placeholder, which would say a binding exists). -->
          {#if $authStore.canWrite}{#if project.source.repo_path}<p class="source">{project.source.repo_path}</p>{:else}<p class="source">{st('noSource')}</p>{/if}{/if}
          {#if project.policy.dispatch !== 'active'}<p class="hold-reason">⏸ {project.policy.reason}</p>{/if}
          <span class="open">{st('graph')} →</span>
        </button>
      {/each}
    </div>
    {#if visible.length && !filtered.length}<p>{st('noMatch')}</p>{/if}
    {#if next}<button class="outline" disabled={loading} onclick={more}>{st('more')}</button>{/if}
  {/if}
</section>
{#if $authStore.canWrite && canRead && visible.length}
  <DriverConversationPane projects={ordered} status={drivers} bind:selected={paneSelection}
    onread={loadDriverState} onrefresh={() => void loadDriverState()} />
{/if}
</div>
<style>
  /* Two bounded columns that fill the view's height instead of growing with
     their content: the list scrolls itself, the pane scrolls itself, and the
     page (#view-container) is left with nothing to scroll. `minmax(0,1fr)` and
     `min-height:0` let the columns shrink below their content, which is what
     makes a child's own overflow-y actually engage. */
  /* The pane is on the LEFT, and the DOM keeps the list first (reading order).
     `order` moves it, NOT grid-column: reassigning the columns also changed how
     they are sized, and the list disappeared - the earlier sizing was already
     correct, so only the paint order changes here. */
  /* `auto` is the pane's own width and `1fr` is whatever the list can have: with
     order:-1 the pane is placed first, so it must be first in the column list too
     or it takes the flexible column and squeezes the list to its content. */
  .dag-split > :global(.conversation-pane) { order:-1; }
  .dag-split { display:grid; grid-template-columns:auto minmax(0,1fr); grid-template-rows:minmax(0,1fr);
    align-items:stretch; gap:1rem; height:100%; min-height:0; max-width:1680px; margin:0 auto; }
  .state-projects { min-width:0; min-height:0; max-width:1280px; margin:0 auto;
    overflow-x:hidden; overflow-y:auto; padding-right:.15rem; }
  header { display:flex; justify-content:space-between; gap:1rem; align-items:flex-start; flex-wrap:wrap; margin:.6rem 0 1.2rem; }
  h1 { margin:.2rem 0; font-size:1.65rem; } header p { margin:.4rem 0; font-size:.9rem; }
  .eyebrow { letter-spacing:.12em; font-size:.65rem; color:var(--pico-muted-color,#64748b); }
  header > a { font-size:.8rem; }
  label { max-width:420px; font-size:.8rem; display:block; } input { padding:.55rem; font-size:.85rem; }
  .archived-toggle { margin:.5rem 0 1rem; } .archived-toggle input { padding:0; }
  .project-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(min(100%,310px),1fr)); gap:1rem; }
  .project-card { display:block; width:100%; text-align:left; font:inherit; cursor:pointer; border:1px solid var(--pico-muted-border-color,#dbe3ec); border-radius:12px; padding:1.05rem; background:var(--pico-card-background-color,#fff); color:inherit; text-decoration:none; }
  .project-card:hover,.project-card:focus { border-color:var(--pico-primary,#0066cc); }
  /* The card that owns the conversation is marked, not merely hovered. */
  .project-card[aria-pressed="true"] { border-color:var(--pico-primary,#0066cc);
    box-shadow:inset 3px 0 0 var(--pico-primary,#0066cc); }
  .card-top { display:flex; justify-content:space-between; gap:.5rem; font-size:.7rem; overflow-wrap:anywhere; color:var(--pico-muted-color,#64748b); }
  h2 { font-size:1.2rem; margin:1rem 0; }
  .driver-line { display:flex; align-items:center; flex-wrap:wrap; gap:.35rem .5rem; margin:0 0 .7rem; font-size:.72rem; }
  .driver-facts { display:flex; align-items:center; flex-wrap:wrap; gap:.25rem .6rem; color:var(--pico-muted-color,#64748b); }
  .driver-facts .fact { display:inline-flex; align-items:baseline; gap:.15rem; }
  /* NOT var(--pico-color): the card became a <button>, and Pico redefines
     --pico-color inside a button to its inverse (white). These numbers went
     white on a white card while the .counts numbers - which do not read that
     variable - stayed visible, which is exactly the report. `inherit` keeps them
     the card's own text colour in every theme. */
  .driver-facts strong { font-size:.8rem; color:inherit; font-weight:600; }
  .driver-state { display:inline-flex; align-items:center; gap:.3rem; color:var(--pico-muted-color,#64748b); }
  .driver-state::before { content:''; width:.45rem; height:.45rem; border-radius:50%; background:#94a3b8; }
  .driver-state[data-phase="running"]::before { background:#15803d; }
  .driver-state[data-phase="idle"]::before { background:#2563eb; }
  .driver-state[data-phase="failed"]::before { background:#b91c1c; }
  .unread { display:inline-block; padding:0 .35rem; border-radius:999px; background:var(--pico-primary,#2563eb); color:#fff; font-size:.6rem; line-height:1.5; }
  /* Amber, not the primary blue: a question is something the USER owes the
     driver, so it must not read as another message waiting to be read. */
  .questions { display:inline-block; padding:0 .35rem; border-radius:999px; background:#b45309; color:#fff; font-size:.6rem; line-height:1.5; }
  .counts { display:flex; gap:1.2rem; font-size:.8rem; } .counts strong { font-size:1.4rem; }
  .source { font-size:.7rem; opacity:.75; overflow-wrap:anywhere; margin:.8rem 0; }
  .held,.hold-reason { color:#ae751e; } .hold-reason { font-size:.75rem; }
  .open { display:block; font-size:.8rem; margin-top:1rem; color:var(--pico-primary,#0066cc); }
  button { width:auto; margin-top:.8rem; }
  @media (max-width:900px) {
    /* One column again: the page scrolls, the list is not its own scroll box. */
    .dag-split { grid-template-columns:minmax(0,1fr); grid-template-rows:auto; height:auto; }
    .dag-split > :global(.conversation-pane) { order:0; }
    .state-projects { width:100%; max-width:none; overflow-y:visible; height:auto; }
  }
</style>
