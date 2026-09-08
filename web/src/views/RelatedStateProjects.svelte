<script lang="ts">
  import { authStore } from '../stores/auth';
  import { stateProjects } from '../lib/api';
  import { stateProjectHref, type StateProjectRow } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  const { repoPath }: {repoPath: string} = $props();
  let projects = $state<StateProjectRow[]>([]), error = $state(''), more = $state(false);
  $effect(() => {
    const allowed = $authStore.permissionResolved && $authStore.canWrite, path = repoPath;
    let cancelled = false; projects = []; error = ''; more = false;
    if (allowed) Promise.resolve().then(() => stateProjects(path)).then(result => {
      if (!cancelled) { projects = result.projects; more = Boolean(result.next_after); }
    }).catch(e => { if (!cancelled && e.status !== 403) error = String(e.message ?? e); });
    return () => { cancelled = true; };
  });
</script>
{#if $authStore.permissionResolved && $authStore.canWrite}
  <div class="related-state">
    <strong>{st('related')}</strong>
    {#each projects as project (project.project_id)}<a href={stateProjectHref(project.project_id)}>{project.title} · {project.policy.dispatch} ↗</a>{/each}
    {#if error}<small role="status">{error}</small>{/if}
    {#if !projects.length || more}<a href={`#/state-projects/for-repo/${encodeURIComponent(repoPath)}`}>{st('projects')} →</a>{/if}
  </div>
{/if}
<style>
  .related-state { display:flex; align-items:center; flex-wrap:wrap; gap:.5rem 1rem; margin:.7rem 0; font-size:.78rem; }
  small { color:var(--pico-muted-color,#667085); }
</style>
