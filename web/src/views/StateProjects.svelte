<script lang="ts">
  import { authStore } from '../stores/auth';
  import { stateProjects } from '../lib/api';
  import { stateProjectHref, type StateProjectRow } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  const { params = {} }: {params?: {repoPath?: string}} = $props();
  let rows = $state<StateProjectRow[]>([]), error = $state(''), loading = $state(false);
  let query = $state(''), next = $state<string | null>(null), retry = $state(0);
  let generation = 0;
  const allowed = $derived($authStore.permissionResolved && $authStore.canWrite);
  const filtered = $derived(rows.filter(p => (p.title + ' ' + p.project_id).toLocaleLowerCase().includes(query.toLocaleLowerCase())));
  $effect(() => {
    const canRead = allowed, repo = params.repoPath; void retry;
    const version = ++generation;
    rows = []; next = null; error = '';
    if (!canRead) { loading = false; return; }
    loading = true;
    stateProjects(repo).then(result => {
      if (version !== generation) return;
      rows = result.projects; next = result.next_after;
    }).catch(e => { if (version === generation) error = String(e.message ?? e); })
      .finally(() => { if (version === generation) loading = false; });
    return () => { generation++; };
  });
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
<section class="state-projects">
  <header><div><p class="eyebrow">AITELIER / PROJECTS</p><h1>{st('projects')}</h1><p>{st('intro')}</p></div>
    <a href="#/repos">{st('dashboard')} ↗</a></header>
  {#if !allowed}<p role="status">{st('private')}</p>
  {:else}
    <label>{st('search')}<input type="search" bind:value={query} placeholder={st('search')} /></label>
    {#if params.repoPath}<p><code>{params.repoPath}</code></p>{/if}
    {#if error}<div role="alert"><p>{error}</p><button class="outline" onclick={() => retry++}>{st('retry')}</button></div>{/if}
    {#if loading && !rows.length}<p aria-live="polite">{st('loading')}</p>
    {:else if !rows.length && !error}<article><p>{st('empty')}</p></article>{/if}
    <div class="project-grid">
      {#each filtered as project (project.project_id)}
        <a class="project-card" href={stateProjectHref(project.project_id)}>
          <div class="card-top"><span>{project.project_id}</span><span class:held={project.policy.dispatch !== 'active'}>{project.policy.dispatch}</span></div>
          <h2>{project.title}</h2>
          <div class="counts"><span><strong>{project.node_count}</strong> {st('total')}</span><span><strong>{project.verified_count}</strong> {st('verified')}</span></div>
          {#if project.source.repo_path}<p class="source">{project.source.repo_path}</p>{:else}<p class="source">{st('noSource')}</p>{/if}
          {#if project.policy.dispatch !== 'active'}<p class="hold-reason">⏸ {project.policy.reason}</p>{/if}
          <span class="open">{st('graph')} →</span>
        </a>
      {/each}
    </div>
    {#if rows.length && !filtered.length}<p>{st('noMatch')}</p>{/if}
    {#if next}<button class="outline" disabled={loading} onclick={more}>{st('more')}</button>{/if}
  {/if}
</section>
<style>
  .state-projects { max-width:1280px; margin:0 auto; }
  header { display:flex; justify-content:space-between; gap:1rem; align-items:flex-start; flex-wrap:wrap; margin:.6rem 0 1.2rem; }
  h1 { margin:.2rem 0; font-size:1.65rem; } header p { margin:.4rem 0; font-size:.9rem; }
  .eyebrow { letter-spacing:.12em; font-size:.65rem; color:var(--pico-muted-color,#64748b); }
  header > a { font-size:.8rem; }
  label { max-width:420px; font-size:.8rem; display:block; } input { padding:.55rem; font-size:.85rem; }
  .project-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(min(100%,310px),1fr)); gap:1rem; }
  .project-card { display:block; border:1px solid var(--pico-muted-border-color,#dbe3ec); border-radius:12px; padding:1.05rem; background:var(--pico-card-background-color,#fff); color:inherit; text-decoration:none; }
  .project-card:hover,.project-card:focus { border-color:var(--pico-primary,#0066cc); }
  .card-top { display:flex; justify-content:space-between; gap:.5rem; font-size:.7rem; overflow-wrap:anywhere; color:var(--pico-muted-color,#64748b); }
  h2 { font-size:1.2rem; margin:1rem 0; }
  .counts { display:flex; gap:1.2rem; font-size:.8rem; } .counts strong { font-size:1.4rem; }
  .source { font-size:.7rem; opacity:.75; overflow-wrap:anywhere; margin:.8rem 0; }
  .held,.hold-reason { color:#ae751e; } .hold-reason { font-size:.75rem; }
  .open { display:block; font-size:.8rem; margin-top:1rem; color:var(--pico-primary,#0066cc); }
  button { width:auto; margin-top:.8rem; }
</style>
