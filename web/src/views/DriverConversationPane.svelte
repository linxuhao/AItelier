<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { st } from '../lib/stateI18n.svelte';
  import { readLastProject, rememberProject } from '../lib/navigation.svelte';
  import { clampPaneWidth, isNarrow, paneMaxFor, readPaneWidth, writePaneWidth,
    NARROW_BREAKPOINT, PANE_DEFAULT, PANE_MIN } from '../lib/splitPane';
  import type { DriverStatusResponse } from '../lib/api';
  import type { StateProjectRow } from '../lib/stateGraph';
  import DriverConversation from './DriverConversation.svelte';
  import DriverRunLog from './DriverRunLog.svelte';
  // The right-hand half of the DAG page: one State DAG at a time, the same
  // selection in both modes, and a width the user owns. Both panels stay mounted
  // once visited and are only hidden, so switching modes never reloads the
  // thread or throws away the page the user had loaded.
  const { projects = [], status = null, onread = () => {} }: {
    projects?: StateProjectRow[]; status?: DriverStatusResponse | null; onread?: () => void;
  } = $props();
  let selected = $state('');
  let mode = $state<'conversation' | 'run'>('conversation');
  let visitedRun = $state(false);
  let viewport = $state(NARROW_BREAKPOINT + 1);
  let preferred = $state(PANE_DEFAULT);
  let drag: {startX: number; startWidth: number} | null = null;
  const width = $derived(clampPaneWidth(preferred, viewport));
  const narrow = $derived(isNarrow(viewport));
  const current = $derived(projects.find(p => p.project_id === selected));
  const driver = $derived(selected ? status?.projects[selected]?.managed : undefined);
  $effect(() => {
    const ids = projects.map(p => p.project_id);
    if (!ids.length) { if (selected) selected = ''; return; }
    if (ids.includes(selected)) return;
    const prior = readLastProject();
    selected = ids.includes(prior) ? prior : ids[0];
  });
  function choose(event: Event) {
    selected = (event.target as HTMLSelectElement).value;
    rememberProject(selected);
  }
  function chooseMode(next: 'conversation' | 'run') {
    mode = next;
    if (next === 'run') visitedRun = true;
  }
  function setWidth(next: number) {
    preferred = clampPaneWidth(next, viewport);
    writePaneWidth(preferred);
  }
  function resizeKey(event: KeyboardEvent) {
    if (event.key === 'ArrowLeft') { event.preventDefault(); setWidth(width + 32); }
    else if (event.key === 'ArrowRight') { event.preventDefault(); setWidth(width - 32); }
    else if (event.key === 'Home') { event.preventDefault(); setWidth(PANE_MIN); }
    else if (event.key === 'End') { event.preventDefault(); setWidth(paneMaxFor(viewport)); }
  }
  function startDrag(event: MouseEvent) {
    event.preventDefault();
    drag = {startX: event.clientX, startWidth: width};
    window.addEventListener('mousemove', onDrag);
    window.addEventListener('mouseup', endDrag);
  }
  function onDrag(event: MouseEvent) {
    if (!drag) return;
    // The pane is on the right: dragging left must widen it.
    preferred = clampPaneWidth(drag.startWidth + (drag.startX - event.clientX), viewport);
  }
  function endDrag() {
    if (drag) writePaneWidth(preferred);
    drag = null;
    window.removeEventListener('mousemove', onDrag);
    window.removeEventListener('mouseup', endDrag);
  }
  onMount(() => {
    preferred = readPaneWidth();
    const measure = () => { viewport = window.innerWidth; };
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  });
  onDestroy(() => endDrag());
</script>
<aside class="conversation-pane" data-narrow={narrow ? 'true' : 'false'} aria-label={st('conversationPane')} style:width={narrow ? undefined : width + 'px'}>
  {#if !narrow}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <!-- A focusable separator is the ARIA window-splitter pattern: the same
         element is both the drag target and the keyboard resize control. -->
    <div class="resize-handle" role="separator" aria-orientation="vertical" tabindex="0"
      aria-label={st('conversationPaneResize')} aria-valuenow={width} aria-valuemin={PANE_MIN}
      aria-valuemax={paneMaxFor(viewport)} onmousedown={startDrag} onkeydown={resizeKey}></div>
  {/if}
  <header class="pane-header">
    {#if projects.length}
      <label class="pane-project">{st('conversationPaneProject')}
        <select aria-label={st('conversationPaneProject')} value={selected} onchange={choose}>
          {#each projects as project (project.project_id)}
            <option value={project.project_id}>{project.title} · {project.project_id}</option>
          {/each}
        </select>
      </label>
    {/if}
    {#if !narrow}
      <div class="pane-size" role="group" aria-label={st('conversationPaneSize')}>
        <button type="button" class="outline" onclick={() => setWidth(paneMaxFor(viewport))}>{st('conversationPaneExpand')}</button>
        <button type="button" class="outline" onclick={() => setWidth(PANE_MIN)}>{st('conversationPaneShrink')}</button>
        <button type="button" class="outline" onclick={() => setWidth(PANE_DEFAULT)}>{st('conversationPaneRestore')}</button>
      </div>
    {/if}
  </header>
  <div class="pane-modes" role="tablist" aria-label={st('conversationPaneMode')}>
    <button type="button" role="tab" class:active={mode === 'conversation'} aria-selected={mode === 'conversation'}
      onclick={() => chooseMode('conversation')}>{st('driverConversation')}</button>
    <button type="button" role="tab" class:active={mode === 'run'} aria-selected={mode === 'run'}
      onclick={() => chooseMode('run')}>{st('driverHistory')}</button>
  </div>
  <div class="pane-body">
    {#if selected}
      {#key selected}
        <div data-mode-panel="conversation" hidden={mode !== 'conversation'}>
          <DriverConversation projectId={selected} projectTitle={current?.title ?? selected}
            driverId={driver?.driver_id ?? ''} onread={onread} />
        </div>
        {#if visitedRun}
          <div data-mode-panel="run" hidden={mode !== 'run'}>
            {#if driver}
              <DriverRunLog projectId={selected} projectTitle={current?.title ?? selected}
                driverId={driver.driver_id} driverEnabled={!!driver.enabled} />
            {:else}<p class="identity">{st('driverHistoryEmpty')}</p>{/if}
          </div>
        {/if}
      {/key}
    {/if}
  </div>
</aside>
<style>
  .conversation-pane{position:sticky;top:.5rem;display:flex;flex-direction:column;flex:0 0 auto;
    max-height:calc(100vh - 1rem);border:1px solid var(--pico-muted-border-color,#dbe3ec);border-radius:12px;
    background:var(--pico-card-background-color,#fff);min-width:0;font-size:.8rem;}
  .resize-handle{position:absolute;top:0;bottom:0;left:-6px;width:12px;cursor:col-resize;z-index:2;}
  .resize-handle::after{content:'';position:absolute;top:50%;left:5px;width:2px;height:2.4rem;margin-top:-1.2rem;
    border-radius:2px;background:var(--pico-muted-border-color,#cbd5e1);}
  .resize-handle:hover::after,.resize-handle:focus::after{background:var(--pico-primary,#0066cc);}
  .pane-header{display:flex;align-items:center;flex-wrap:wrap;gap:.4rem;padding:.6rem .7rem;border-bottom:1px solid var(--pico-muted-border-color,#e6ebf1);}
  .pane-project{display:flex;align-items:center;gap:.4rem;margin:0;font-size:.7rem;flex:1 1 12rem;min-width:0;max-width:none;}
  .pane-project select{margin:0;padding:.3rem 1.6rem .3rem .45rem;font-size:.78rem;min-width:0;}
  .pane-size{display:flex;gap:.25rem;}
  .pane-size button{width:auto;margin:0;font-size:.65rem;padding:.15rem .4rem;}
  .pane-modes{display:flex;gap:.25rem;padding:.45rem .7rem;border-bottom:1px solid var(--pico-muted-border-color,#e6ebf1);}
  .pane-modes button{width:auto;margin:0;font-size:.72rem;padding:.2rem .6rem;background:none;border:1px solid transparent;color:inherit;}
  .pane-modes button.active{border-color:var(--pico-primary,#0066cc);border-radius:6px;color:var(--pico-primary,#0066cc);}
  .pane-body{flex:1 1 auto;min-height:0;overflow:auto;padding:.2rem .7rem .7rem;}
  .identity{color:var(--pico-muted-color,#64748b);overflow-wrap:anywhere;}
  @media (max-width:900px){
    .conversation-pane{position:static;width:100%;max-height:none;}
    .pane-project{flex-basis:100%;}
  }
</style>
