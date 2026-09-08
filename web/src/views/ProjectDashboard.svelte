<script lang="ts">
  import { authStore } from '../stores/auth';
  import { stateProjects } from '../lib/api';
  import type { StateProjectRow } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  import { nt, readLastProject, rememberProject } from '../lib/navigation.svelte';
  import StateProject from './StateProject.svelte';
  const allowed = $derived($authStore.permissionResolved && $authStore.canWrite);
  let projects = $state<StateProjectRow[]>([]), selected=$state(''), error=$state(''), loading=$state(false), retry=$state(0);
  let generation=0;
  $effect(()=>{
    const permitted=allowed, identity=$authStore.email; void identity; void retry;
    const version=++generation; projects=[]; selected=''; error=''; loading=permitted;
    if(!permitted) return;
    stateProjects().then(result=>{
      if(version!==generation)return;
      projects=result.projects;
      const prior=readLastProject();
      selected=projects.some(p=>p.project_id===prior)?prior:(projects[0]?.project_id??'');
    }).catch(e=>{if(version===generation)error=String(e.message??e);})
      .finally(()=>{if(version===generation)loading=false;});
    return ()=>{generation++;};
  });
  function choose(event:Event){selected=(event.target as HTMLSelectElement).value;rememberProject(selected);}
</script>
<section class="project-dashboard" aria-label={nt('home')}>
  <div class="project-switcher">
    <div class="switch-title"><strong>{nt('home')}</strong><span>{st('intro')}</span></div>
    {#if allowed && projects.length}<label>{nt('pick')}<select value={selected} onchange={choose}>{#each projects as p(p.project_id)}<option value={p.project_id}>{p.title} · {p.project_id}</option>{/each}</select></label>{/if}
    <a href="#/state-projects">{nt('allProjects')} ↗</a>
  </div>
  {#if !allowed}<p role="status">{st('private')}</p>
  {:else if error}<div role="alert"><p>{error}</p><button class="outline" onclick={()=>retry++}>{nt('retry')}</button></div>
  {:else if loading}<p role="status">{st('loading')}</p>
  {:else if selected}
    {#key selected}<StateProject params={{id:selected}} />{/key}
  {:else}<p>{st('empty')}</p>{/if}
</section>
<style>
  .project-dashboard{max-width:1480px;min-width:0;margin:auto;}
  .project-switcher{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;border-bottom:1px solid var(--pico-muted-border-color,#ddd);padding:.65rem 0 1rem;margin-bottom:.5rem;}
  .switch-title{display:flex;flex-direction:column;gap:.2rem;flex:1;min-width:180px;}
  .switch-title span{font-size:.75rem;color:var(--pico-muted-color,#64748b);}
  label{font-size:.75rem;margin:0;max-width:100%;}select{font-size:.85rem;margin:.2rem 0 0;padding:.45rem 2rem .45rem .65rem;max-width:min(440px,100%);}
  a{font-size:.8rem;}button{width:auto;font-size:.85rem;}
</style>
