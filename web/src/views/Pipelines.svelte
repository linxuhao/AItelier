<script lang="ts">
  import { listPipelines } from '../lib/api';
  import { nt } from '../lib/navigation.svelte';
  import { st } from '../lib/stateI18n.svelte';
  import PipelineCard from './PipelineCard.svelte';
  type Pipeline={config_name:string;label?:string;origin?:string;step_count?:number;base?:string;addons?:string[];state_files?:{name:string;size:number}[]};
  let rows=$state<Pipeline[]>([]), error=$state(''), loading=$state(true), retry=$state(0), q=$state('');
  const filtered=$derived(rows.filter(p=>(p.config_name+' '+(p.label??'')+' '+(p.addons??[]).join(' ')).toLocaleLowerCase().includes(q.toLocaleLowerCase())));
  $effect(()=>{
    void retry;let cancelled=false;loading=true;error='';
    listPipelines().then(result=>{if(!cancelled)rows=result.pipelines as Pipeline[];})
      .catch(e=>{if(!cancelled)error=String(e.message??e);}).finally(()=>{if(!cancelled)loading=false;});
    return ()=>{cancelled=true;};
  });
</script>
<section class="pipelines-page" aria-label={nt('pipelines')}>
  <header><div><h1>{nt('pipelines')}</h1><p>{nt('pipelineIntro')}</p></div><a href="#/runs">{nt('runs')} ↗</a></header>
  <div class="filters"><label>{nt('search')}<input type="search" bind:value={q} placeholder={nt('workflow')} /></label><button class="outline" disabled={loading} onclick={()=>retry++}>{nt('reload')}</button></div>
  {#if error}<div role="alert"><p>{rows.length?nt('stale'):''} {error}</p><button class="outline" onclick={()=>retry++}>{nt('retry')}</button></div>{/if}
  {#if loading && !rows.length}<p role="status">{st('loading')}</p>{/if}
  {#if !loading && !error && !filtered.length}<p>{nt('emptyPipelines')}</p>{/if}
  <div class="catalog">{#each filtered as pipeline(pipeline.config_name)}<PipelineCard {pipeline}/>{/each}</div>
</section>
<style>
  .pipelines-page{max-width:1280px;min-width:0;margin:auto;}header{display:flex;justify-content:space-between;align-items:start;flex-wrap:wrap;gap:1rem;}h1{font-size:1.65rem;margin:.2rem 0;}header p{font-size:.85rem;}header a{font-size:.8rem;}
  .filters{display:flex;align-items:end;gap:.7rem;margin:.8rem 0;}label{font-size:.75rem;flex:1;max-width:460px;margin:0;}input,button{font-size:.8rem;padding:.5rem .7rem;margin:0;}button{width:auto;}.catalog{margin-top:1rem;}
</style>
