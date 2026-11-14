<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { authStore } from '../stores/auth';
  import { driverStatus, dshDriverConversationUnread, stateProjects, type DriverStatusResponse } from '../lib/api';
  import { isArchived, stateProjectHref, type StateProjectRow } from '../lib/stateGraph';
  import { rememberProject } from '../lib/navigation.svelte';
  import { driverState, driverStateIcon, driverStateLabelKey, sortDagRows } from '../lib/driverList';
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
  let mobileFiltersOpen = $state(false);
  let driverTimer: ReturnType<typeof setInterval> | undefined, driverMounted = false;
  const ordered = $derived(sortDagRows(filtered, drivers));
  const selectable = $derived(sortDagRows(visible, drivers));
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
{#snippet projectFilters()}
    <label>{st('projectSearch')}<input type="search" bind:value={query} placeholder={st('projectSearch')} /></label>
    <label class="archived-toggle"><input type="checkbox" bind:checked={showArchived} /> {st('showArchived')}{#if archivedHidden} · {st('archivedHidden')}: {archivedHidden}{/if}</label>
{/snippet}
<div class="dag-split" class:workbench={$authStore.canWrite && canRead && visible.length > 0}>
<section class="state-projects">
  <header><div><h1>{st($authStore.canWrite ? 'projectList' : 'projects')}</h1></div>
    <a class="repository-link" href="#/repos">{st('dashboard')} ↗</a></header>
  {#if !canRead}<p role="status">{st('private')}</p>
  {:else}
    <div class="project-filters">{@render projectFilters()}</div>
    <div class="mobile-project-controls">
      <label>{st('currentProject')}<select value={paneSelection} onchange={(event) => {paneSelection = event.currentTarget.value; rememberProject(paneSelection);}}>
        {#if paneSelection && !ordered.some(project => project.project_id === paneSelection)}
          <option value={paneSelection}>{selectable.find(project => project.project_id === paneSelection)?.title ?? paneSelection}</option>
        {/if}
        {#each ordered as project (project.project_id)}<option value={project.project_id}>{project.title}{questions[project.project_id] ? ' · ' + questions[project.project_id] + ' ' + st('pendingShort') : ''}</option>{/each}
      </select></label>
      <details bind:open={mobileFiltersOpen}><summary>{st('projectFilters')}</summary>{#if mobileFiltersOpen}{@render projectFilters()}{/if}</details>
    </div>
    {#if params.repoPath}<p><code>{params.repoPath}</code></p>{/if}
    {#if error}<div role="alert"><p>{error}</p><button class="outline" onclick={() => retry++}>{st('retry')}</button></div>{/if}
    {#if loading && !rows.length}<p aria-live="polite">{st('loading')}</p>
    {:else if !rows.length && !error}<article><p>{st('empty')}</p></article>{/if}
    <div class="project-grid">
      {#each ordered as project (project.project_id)}
        {@const phase = drivers?.projects[project.project_id]?.managed?.phase}
        {@const activity = drivers?.projects[project.project_id]}
        <button type="button" class="project-card" aria-pressed={paneSelection === project.project_id}
          onclick={() => {
            if (!$authStore.canWrite) { window.location.hash = stateProjectHref(project.project_id).slice(1); return; }
            paneSelection = project.project_id; rememberProject(paneSelection);
          }}>
          <div class="card-top"><span>{project.project_id}</span><span class:held={project.policy.dispatch !== 'active'}>{project.policy.dispatch}</span></div>
          <h2>{project.title}</h2>
          {#if $authStore.canWrite}
            <!-- Everything the driver is doing for THIS project, on the button
                 that opens it: phase, drivers online/total, and what is running
                 right now. All four read from the same /api/dsh-drivers row. -->
            <p class="driver-line">
              <span class="driver-state" data-phase={driverState(phase)} data-icon={driverStateIcon(phase)} title={st(driverStateLabelKey(phase))}>{st(driverStateLabelKey(phase))}</span>
              <span class="driver-facts" aria-label={st('driverFacts')}>
                <span class="fact"><strong data-fact="drivers">{activity?.online ?? 0}/{activity?.total ?? 0}</strong> {st('driverCount')}</span>
                <span class="fact"><strong data-fact="subagents">{activity?.running_subagents ?? 0}</strong> {st('driverSubagents')}</span>
                <span class="fact"><strong data-fact="pipelines">{activity?.running_pipelines ?? 0}</strong> {st('driverPipelines')}</span>
              </span>
              {#if unread[project.project_id]}<span class="unread" title={st('driverConversationUnread')}>{unread[project.project_id]} {st('unreadShort')}</span>{/if}
              <!-- Its own pill, not folded into the unread count: unread is
                   about messages and clearing it answers nothing, while this
                   counts questions still owed an answer. Rendered only when a
                   project HAS one, so a zero never reads as a badge. -->
              {#if questions[project.project_id]}<span class="questions" data-pending-questions={questions[project.project_id]} title={st('driverQuestionsBadge')}>{questions[project.project_id]} {st('pendingShort')}</span>{/if}
            </p>
          {/if}
          {#if !$authStore.canWrite}<div class="counts"><span><strong>{project.node_count}</strong> {st('total')}</span><span><strong>{project.verified_count}</strong> {st('verified')}</span></div>{/if}
          {#if project.policy.dispatch !== 'active'}<p class="hold-reason">⏸ {project.policy.reason}</p>{/if}
          {#if !$authStore.canWrite}<span class="open">{st('graph')} →</span>{/if}
        </button>
      {/each}
    </div>
    {#if visible.length && !filtered.length}<p>{st('noMatch')}</p>{/if}
    {#if next}<button class="outline" disabled={loading} onclick={more}>{st('more')}</button>{/if}
  {/if}
</section>
{#if $authStore.canWrite && canRead && visible.length}
  <DriverConversationPane projects={selectable} status={drivers} bind:selected={paneSelection}
    onread={loadDriverState} onrefresh={() => void loadDriverState()} />
{/if}
</div>
<style>
  .dag-split{--workbench-surface:var(--pico-background-color);--workbench-accent:var(--pico-primary);display:grid;grid-template-columns:minmax(0,1fr);height:100%;min-height:0;max-width:1800px;margin:0 auto;font-size:14px;}
  .dag-split.workbench{grid-template-columns:240px minmax(0,1fr);gap:0;border:1px solid var(--pico-muted-border-color);border-radius:12px;overflow:hidden;background:var(--pico-background-color);}
  .state-projects{min-width:0;min-height:0;overflow-y:auto;overflow-x:hidden;padding:18px;}
  .workbench .repository-link{display:none;}
  .workbench .state-projects{padding:16px 12px;background:var(--pico-card-sectioning-background-color);border-right:1px solid var(--pico-muted-border-color);}
  header{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:0 0 16px;}
  h1{font-size:16px;margin:0;}header>a{font-size:12px;}
  label{font-size:12px;margin-bottom:10px;}input[type=search]{font-size:13px;padding:8px 10px;margin:4px 0 0;}
  .archived-toggle{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--pico-muted-color);}
  .archived-toggle input{margin:0;flex-shrink:0;}
  .project-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,280px),1fr));gap:10px;}
  .workbench .project-grid{grid-template-columns:minmax(0,1fr);gap:6px;}
  .project-card{display:block;width:100%;min-width:0;margin:0;padding:14px;text-align:left;font:inherit;border:1px solid var(--pico-muted-border-color);border-radius:8px;background:var(--pico-card-background-color);color:inherit;box-shadow:none;overflow-wrap:anywhere;}
  .workbench .project-card{padding:10px;border-color:transparent;background:transparent;}
  .project-card:hover,.project-card:focus-visible{border-color:var(--pico-primary);}
  .project-card[aria-pressed=true]{border-color:var(--pico-primary);background:color-mix(in srgb,var(--workbench-accent) 8%,var(--workbench-surface));}
  .card-top{display:flex;justify-content:space-between;gap:6px;color:var(--pico-muted-color);font-size:11px;}
  .workbench .card-top{display:none;}
  h2{font-size:14px;line-height:1.5;margin:0 0 6px;}
  .driver-line{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin:0;font-size:12px;}
  .driver-state{display:inline-flex;align-items:center;gap:5px;color:var(--pico-muted-color);}
  .driver-state::before{content:attr(data-icon);color:var(--pico-primary);}
  .driver-state[data-phase=running]::before{color:var(--pico-ins-color);}
  .driver-state[data-phase=failed]::before{color:var(--pico-del-color);}
  .driver-facts{display:flex;flex-wrap:wrap;gap:2px 8px;width:100%;font-size:11px;color:var(--pico-muted-color);}
  .fact{display:inline-flex;gap:3px;align-items:baseline;}.fact strong{font-size:inherit;color:inherit;}
  .unread,.questions{display:inline-block;padding:1px 6px;border-radius:5px;font-size:11px;line-height:1.5;}
  .unread{background:var(--pico-primary);color:var(--pico-primary-inverse);}
  .questions{background:light-dark(#fff3df,#3c3020);color:light-dark(#925508,#f2bd70);}
  .counts{display:flex;gap:16px;font-size:12px;}.counts strong{font-size:20px;}
  .held,.hold-reason{color:var(--pico-muted-color);}.hold-reason{margin:8px 0 0;font-size:12px;}
  .open{display:block;margin-top:12px;font-size:12px;color:var(--pico-primary);}
  .mobile-project-controls{display:none;}
  @media(max-width:1100px){.dag-split.workbench{grid-template-columns:210px minmax(0,1fr);}}
  @media(max-width:760px){
    .dag-split.workbench{grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr);}
    .workbench .state-projects{max-height:220px;border-right:0;border-bottom:1px solid var(--pico-muted-border-color);padding:10px 12px;}
    .workbench header,.workbench .project-filters,.workbench .project-grid{display:none;}
    .workbench .mobile-project-controls{display:block;}
    .mobile-project-controls label{margin:0;}.mobile-project-controls select{margin:4px 0 0;padding:6px 30px 6px 9px;font-size:14px;}
    .mobile-project-controls details{margin:8px 0 0;padding:0;}.mobile-project-controls summary{font-size:12px;color:var(--pico-muted-color);margin-bottom:0;}
    .mobile-project-controls details[open] summary{margin-bottom:10px;}
    .workbench header{margin-bottom:8px;}.workbench label:not(.archived-toggle){margin-bottom:8px;}
    .workbench .project-grid{grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr));}
    .workbench .driver-facts{display:none;}.workbench .archived-toggle{margin-bottom:8px;}
  }
</style>
