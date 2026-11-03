<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { st } from '../lib/stateI18n.svelte';
  import { readLastProject, rememberProject } from '../lib/navigation.svelte';
  import { clampPaneWidth, isNarrow, paneMaxFor, readPaneWidth, writePaneWidth,
    NARROW_BREAKPOINT, PANE_DEFAULT, PANE_MIN } from '../lib/splitPane';
  import { startDshDriver, stopDshDriver, type DriverStatusResponse } from '../lib/api';
  import type { StateProjectRow } from '../lib/stateGraph';
  import DriverConversation from './DriverConversation.svelte';
  import DriverRunLog from './DriverRunLog.svelte';
  // The right-hand half of the DAG page: one State DAG at a time, the same
  // selection in both modes, and a width the user owns. Both panels stay mounted
  // once visited and are only hidden, so switching modes never reloads the
  // thread or throws away the page the user had loaded.
  const { projects = [], status = null, onread = () => {}, onrefresh = () => {} }: {
    projects?: StateProjectRow[]; status?: DriverStatusResponse | null;
    onread?: () => void; onrefresh?: () => void;
  } = $props();
  let selected = $state('');
  let mode = $state<'conversation' | 'run'>('conversation');
  let visitedRun = $state(false);
  let viewport = $state(NARROW_BREAKPOINT + 1);
  let preferred = $state(PANE_DEFAULT);
  let drag: {startX: number; startWidth: number} | null = null;
  let busy = $state(false), controlError = $state('');
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
  // Start/stop moved here with the pane: it acts on the DAG selected in this
  // select, which is the same selection the conversation and the run log use.
  // The dashboard list no longer carries a control per row, so this is the one
  // place a driver is started or stopped from the dashboard.
  async function toggleDriver() {
    if (!selected || busy) return;
    busy = true; controlError = '';
    try {
      if (driver?.enabled) await stopDshDriver(selected);
      else await startDshDriver(selected);
      onrefresh();
    } catch (e) { controlError = String(e instanceof Error ? e.message : e); }
    finally { busy = false; }
  }
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
      <select class="pane-project" aria-label={st('conversationPaneProject')} value={selected} onchange={choose}>
        {#each projects as project (project.project_id)}
          <option value={project.project_id}>{project.title} · {project.project_id}</option>
        {/each}
      </select>
    {/if}
    <!-- Icon-only, so the name lives in aria-label (title is the hint). One
         button, two states: it always acts on the DAG this header selects. -->
    <button type="button" class="outline driver-toggle" disabled={busy || !selected}
      aria-label={driver?.enabled ? st('driverStop') : st('driverStart')}
      title={st('conversationPaneDriverControl')}
      onclick={() => void toggleDriver()}>
      {#if busy}<span class="busy" aria-hidden="true">…</span>
      {:else if driver?.enabled}
        <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false"><rect x="3" y="3" width="10" height="10" rx="1.5"/></svg>
      {:else}
        <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false"><path d="M4.5 2.8 13 8l-8.5 5.2Z"/></svg>
      {/if}
    </button>
    <!-- Same row as the picker: the two modes used to take a band of their own,
         and every band above the thread is height the thread does not get. -->
    <div class="pane-modes" role="tablist" aria-label={st('conversationPaneMode')}>
      <button type="button" role="tab" class:active={mode === 'conversation'} aria-selected={mode === 'conversation'}
        onclick={() => chooseMode('conversation')}>{st('driverConversation')}</button>
      <button type="button" role="tab" class:active={mode === 'run'} aria-selected={mode === 'run'}
        onclick={() => chooseMode('run')}>{st('driverHistory')}</button>
    </div>
  </header>
  {#if controlError}<p class="pane-error" role="alert">{controlError}</p>{/if}
  <!-- Bounded, not scrollable itself: each panel below owns whatever has to
       scroll inside it, and every level may shrink (min-height:0), so no child
       can push the pane past the row the page gives it. -->
  <div class="pane-body">
    {#if selected}
      {#key selected}
        <div class="pane-panel" data-mode-panel="conversation" hidden={mode !== 'conversation'}>
          <DriverConversation projectId={selected} projectTitle={current?.title ?? selected}
            driverId={driver?.driver_id ?? ''} onread={onread} />
        </div>
        {#if visitedRun}
          <div class="pane-panel scroll" data-mode-panel="run" hidden={mode !== 'run'}>
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
  /* The pane fills the row the split gives it and never scrolls: it is a flex
     column that clips, so its height is the row's height and cannot follow the
     thread. Reaching the composer then needs no page scroll. Position:relative
     (not sticky) keeps the handle anchored while the box stays bounded. */
  .conversation-pane{position:relative;display:flex;flex-direction:column;flex:0 0 auto;
    height:100%;max-height:100%;min-height:0;overflow-x:hidden;overflow-y:hidden;
    border:1px solid var(--pico-muted-border-color,#dbe3ec);border-radius:12px;
    background:var(--pico-card-background-color,#fff);min-width:0;font-size:.8rem;}
  /* Inside the pane's own edge: the pane clips, so a handle straddling the
     border would be cut in half. 12px of hit area, the line just inside. */
  .resize-handle{position:absolute;top:0;bottom:0;left:0;width:12px;cursor:col-resize;z-index:2;}
  .resize-handle::after{content:'';position:absolute;top:50%;left:4px;width:2px;height:2.4rem;margin-top:-1.2rem;
    border-radius:2px;background:var(--pico-muted-border-color,#cbd5e1);}
  .resize-handle:hover::after,.resize-handle:focus::after{background:var(--pico-primary,#0066cc);}
  /* ONE control row: picker, start/stop icon and both modes. Every band above
     the thread is height the thread does not get, so the padding here and in
     .pane-body is deliberately tight - the vertical budget belongs to the
     thread, which is the only box allowed to grow. */
  .pane-header{display:flex;align-items:center;flex-wrap:wrap;gap:.3rem;padding:.3rem .5rem;border-bottom:1px solid var(--pico-muted-border-color,#e6ebf1);}
  /* No visible label text: the accessible name is on the control itself, and
     the row has to hold four controls without wrapping at a normal width. */
  .pane-project{flex:1 1 8rem;margin:0;padding:.2rem 1.4rem .2rem .35rem;font-size:.75rem;min-width:0;max-width:none;}
  .pane-modes{display:flex;gap:.15rem;flex:0 0 auto;}
  .pane-modes button{width:auto;margin:0;font-size:.7rem;padding:.15rem .45rem;background:none;border:1px solid transparent;color:inherit;}
  .pane-modes button.active{border-color:var(--pico-primary,#0066cc);border-radius:6px;color:var(--pico-primary,#0066cc);}
  .pane-body{display:flex;flex-direction:column;flex:1 1 auto;min-height:0;overflow-x:hidden;overflow-y:hidden;padding:.15rem .5rem .3rem;}
  .pane-panel{display:flex;flex-direction:column;flex:1 1 auto;min-height:0;overflow-x:hidden;overflow-y:hidden;}
  /* `hidden` must win over the panel's own display:flex, or a non-selected tab
     would still take space. */
  .pane-panel[hidden]{display:none;}
  .pane-panel.scroll{overflow-y:auto;}
  /* Square-ish icon button: the label is aria-label, so it costs no width. */
  .driver-toggle{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;
    width:auto;margin:0;padding:.2rem .35rem;line-height:1;}
  .driver-toggle svg{fill:currentColor;}
  .driver-toggle .busy{line-height:1;}
  .pane-error{margin:.2rem .7rem;font-size:.72rem;color:var(--pico-del-color,#b91c1c);}
  .identity{color:var(--pico-muted-color,#64748b);overflow-wrap:anywhere;}
  @media (max-width:900px){
    /* One column: the pane is a normal block under the list and the PAGE
       scrolls again, which is the old, usable behaviour on a phone. */
    .conversation-pane{width:100%;height:auto;max-height:none;}
    .pane-project{flex-basis:100%;}
    .pane-body{overflow-y:visible;}
    /* Still bounded, just by the viewport share instead of the row: the thread
       keeps its own scroll and the composer stays pinned on a phone too. */
    .pane-panel{overflow-y:visible;max-height:70vh;}
    .pane-panel.scroll{overflow-y:auto;}
  }
</style>
