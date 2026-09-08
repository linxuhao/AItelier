<script lang="ts">
  import { pipelineStateFile } from '../lib/api';
  import { nt } from '../lib/navigation.svelte';
  import { formatBytes } from '../lib/format';
  import PipelineGraph from './PipelineGraph.svelte';
  interface Pipeline {config_name:string;label?:string;origin?:string;step_count?:number;base?:string;addons?:string[];state_files?:{name:string;size:number}[]}
  const { pipeline }: {pipeline:Pipeline}=$props();
  let open=$state(false), opened=$state<Record<string,boolean>>({}), bodies=$state<Record<string,string>>({}), errors=$state<Record<string,string>>({});
  async function toggle(name:string){
    opened={...opened,[name]:!opened[name]};
    if(!opened[name] || name in bodies)return;
    errors={...errors,[name]:''};
    try{const result=await pipelineStateFile(pipeline.config_name,name);bodies={...bodies,[name]:(result.truncated?'…\n':'')+result.content};}
    catch(e){errors={...errors,[name]:String(e instanceof Error?e.message:e)};}
  }
</script>
<article class="pipeline-card">
  <header><div><h2>{pipeline.label||pipeline.config_name}</h2><code>{pipeline.config_name}</code></div><span class="origin">{pipeline.origin==='generated'?nt('generated'):nt('native')}</span></header>
  <p class="description">{pipeline.step_count??0} steps · {nt('definition')}{#if pipeline.addons?.length} · {pipeline.base} + {pipeline.addons.join(' + ')}{/if}</p>
  <details ontoggle={event=>open=(event.currentTarget as HTMLDetailsElement).open}>
    <summary>{nt('graph')}</summary>
    {#if open}<PipelineGraph config={pipeline.config_name} />{/if}
  </details>
  {#if pipeline.state_files?.length}
    <details class="notes"><summary>{nt('notes')} ({pipeline.state_files.length})</summary>
      {#each pipeline.state_files as file(file.name)}<div class="note-file"><button class="outline" onclick={()=>toggle(file.name)} aria-expanded={!!opened[file.name]}>{file.name} ({formatBytes(file.size)})</button>
        {#if opened[file.name]}{#if errors[file.name]}<p role="alert">{errors[file.name]}</p>{:else}<pre>{bodies[file.name]??'…'}</pre>{/if}{/if}
      </div>{/each}
    </details>
  {/if}
</article>
<style>
  .pipeline-card{padding:1rem;min-width:0;margin:0 0 1rem;box-shadow:none;border:1px solid var(--pico-muted-border-color,#dbe3ec);border-radius:10px;}
  header{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;}h2{font-size:1.05rem;margin:0 0 .35rem;}code{font-size:.75rem;overflow-wrap:anywhere;white-space:normal;}
  .origin{font-size:.75rem;border:1px solid var(--pico-muted-border-color,#dbe3ec);padding:.2rem .5rem;border-radius:5px;align-self:start;}.description{font-size:.75rem;color:var(--pico-muted-color,#64748b);margin:.65rem 0;overflow-wrap:anywhere;}
  details{margin:.65rem 0 0;}summary{font-size:.8rem;}button{font-size:.8rem;padding:.4rem .6rem;width:auto;}.note-file{margin:.5rem 0;}pre{font-size:.75rem;padding:.7rem;max-height:320px;overflow:auto;white-space:pre-wrap;word-break:break-word;}
</style>
