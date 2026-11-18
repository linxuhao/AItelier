<script lang="ts">
  import type { Snippet } from 'svelte';
  import { st } from '../lib/stateI18n.svelte';
  import { readLastProject, rememberProject } from '../lib/navigation.svelte';
  import { startDshDriver, stopDshDriver, type DriverStatusResponse } from '../lib/api';
  import DriverStateBadge from './DriverStateBadge.svelte';
  import { stateProjectHref, type StateProjectRow } from '../lib/stateGraph';
  import type { ConversationDraft } from '../lib/conversationDraft';
  import DriverConversation from './DriverConversation.svelte';
  import DriverRunLog from './DriverRunLog.svelte';
  let { projects = [], status = null, onread = () => {}, onrefresh = () => {},
    selected = $bindable(''), mode = $bindable('conversation'), tabs = [], panel, prefill = null, ack = false }: {
    projects?: StateProjectRow[]; status?: DriverStatusResponse | null;
    onread?: () => void; onrefresh?: () => void; selected?: string;
    // A host may add its own tabs after the two built-in ones (the steward's
    // proposals, questions and profile); `panel` renders the chosen one.
    mode?: string; tabs?: {id: string; label: string}[]; panel?: Snippet<[string]>;
    prefill?: {text: string} | null;
    // The steward's pane shows a receipt under the owner's unanswered message
    // (DriverConversation `ack`); a director's pane does not.
    ack?: boolean;
  } = $props();
  let visitedRun = $state(false);
  // Full screen (owner 2026-11-18: on a phone the rest of the dashboard leaves the chat no
  // room): the whole pane - conversation, history and the host's tabs (questions,
  // proposals, ...) - covers the page until toggled off or Escape.
  let fullscreen = $state(false);
  $effect(() => {
    if (!fullscreen) return;
    const prior = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') fullscreen = false; };
    window.addEventListener('keydown', onKey);
    return () => { document.body.style.overflow = prior; window.removeEventListener('keydown', onKey); };
  });
  let busy = $state(false), controlError = $state('');
  // Session-local: switching projects retains text and exact reply citations;
  // nothing from these private conversations is written to browser storage.
  const drafts = new Map<string, ConversationDraft>();
  const current = $derived(projects.find(p => p.project_id === selected));
  const driver = $derived(selected ? status?.projects[selected]?.managed : undefined);
  $effect(() => {
    const ids = projects.map(p => p.project_id);
    if (!ids.length) { if (selected) selected = ''; return; }
    if (ids.includes(selected)) return;
    const prior = readLastProject();
    selected = ids.includes(prior) ? prior : ids[0];
    rememberProject(selected);
  });
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
  function chooseMode(next: string) {
    mode = next;
    if (next === 'run') visitedRun = true;
  }
</script>
<aside class="conversation-pane" class:fullscreen aria-label={st('conversationPane')}>
  <header class="pane-header">
    <div class="pane-heading">
      <a class="pane-project" href={stateProjectHref(selected)} aria-label={st('conversationPaneOpenDag')}>
        <span class="pane-project-title">{current?.title ?? selected}</span>
      </a>
      <span class="pane-project-id">{st('driverConversation')} · {selected}</span>
    </div>
    <div class="pane-controls">
      <span class="pane-driver-state"><DriverStateBadge phase={driver?.phase} /></span>
      <button type="button" class="outline fullscreen-toggle" aria-pressed={fullscreen}
        aria-label={fullscreen ? st('conversationExitFullscreen') : st('conversationFullscreen')}
        title={fullscreen ? st('conversationExitFullscreen') : st('conversationFullscreen')}
        onclick={() => fullscreen = !fullscreen}>{fullscreen ? '⤡' : '⤢'}</button>
      <button type="button" class="outline driver-toggle" disabled={busy || !selected}
        aria-label={driver?.enabled ? st('driverStop') : st('driverStart')}
        onclick={() => void toggleDriver()}>
        {busy ? '…' : driver?.enabled ? st('driverStop') : st('driverStart')}
      </button>
    </div>
  </header>
  <nav class="pane-navigation" aria-label={st('conversationPaneMode')}>
    <div class="pane-modes" role="tablist" aria-label={st('conversationPaneMode')}>
      <button id="driver-conversation-tab" type="button" role="tab" class:active={mode === 'conversation'} aria-selected={mode === 'conversation'}
        aria-controls="driver-conversation-panel" onclick={() => chooseMode('conversation')}>{st('driverConversation')}</button>
      <button id="driver-run-tab" type="button" role="tab" class:active={mode === 'run'} aria-selected={mode === 'run'}
        aria-controls="driver-run-panel" onclick={() => chooseMode('run')}>{st('driverHistory')}</button>
      {#each tabs as tab (tab.id)}
        <button id="driver-{tab.id}-tab" type="button" role="tab" class:active={mode === tab.id} aria-selected={mode === tab.id}
          aria-controls="driver-{tab.id}-panel" onclick={() => chooseMode(tab.id)}>{tab.label}</button>
      {/each}
    </div>
    <a href={stateProjectHref(selected)}>{st('graph')} ↗</a>
  </nav>
  {#if controlError}<p class="pane-error" role="alert">{controlError}</p>{/if}
  <div class="pane-body">
    {#if selected}
      {#key selected}
        <div id="driver-conversation-panel" class="pane-panel" data-mode-panel="conversation" role="tabpanel"
          aria-labelledby="driver-conversation-tab" hidden={mode !== 'conversation'}>
          <DriverConversation projectId={selected} projectTitle={current?.title ?? selected}
            driverId={driver?.driver_id ?? ''} onread={onread} {drafts} {prefill} ack={ack ? {phase: driver?.phase} : null} />
        </div>
        <div id="driver-run-panel" class="pane-panel scroll" data-mode-panel="run" role="tabpanel"
          aria-labelledby="driver-run-tab" hidden={mode !== 'run'}>
          {#if visitedRun}
            {#if driver}
              <DriverRunLog projectId={selected} projectTitle={current?.title ?? selected}
                driverId={driver.driver_id} driverEnabled={!!driver.enabled} />
            {:else}<p class="identity">{st('driverHistoryEmpty')}</p>{/if}
          {/if}
        </div>
        {#each tabs as tab (tab.id)}
          {#if mode === tab.id}
            <div id="driver-{tab.id}-panel" class="pane-panel scroll" data-mode-panel={tab.id} role="tabpanel"
              aria-labelledby="driver-{tab.id}-tab">{@render panel?.(tab.id)}</div>
          {/if}
        {/each}
      {/key}
    {/if}
  </div>
</aside>
<style>
  .conversation-pane{display:flex;flex-direction:column;min-width:0;min-height:0;height:100%;overflow-x:hidden;overflow-y:hidden;background:var(--pico-background-color);container-type:inline-size;}
  .pane-header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;padding:16px 22px 10px;flex:0 0 auto;}
  .pane-heading{display:flex;flex-direction:column;min-width:0;gap:3px;}
  .pane-project{font-size:16px;font-weight:600;color:inherit;text-decoration:none;overflow-wrap:anywhere;}
  .pane-project:hover{color:var(--pico-primary);}
  .pane-project-id{font-size:12px;color:var(--pico-muted-color);overflow-wrap:anywhere;}
  .pane-controls{display:flex;align-items:center;gap:10px;}
  .pane-driver-state{font-size:12px;}
  .driver-toggle{background:transparent;color:var(--pico-primary);font-size:12px;padding:5px 9px;margin:0;width:auto;}
  .fullscreen-toggle{background:transparent;color:var(--pico-primary);font-size:14px;line-height:1;padding:5px 8px;margin:0;width:auto;}
  .conversation-pane.fullscreen{position:fixed;inset:0;z-index:1000;width:100vw;height:100dvh;max-width:none;}
  .pane-navigation{display:flex;align-items:center;gap:20px;padding:0 22px;border-bottom:1px solid var(--pico-muted-border-color);flex:0 0 auto;}
  .pane-modes{display:flex;gap:20px;}
  .pane-modes button{width:auto;margin:0;padding:8px 0;border:0;border-bottom:2px solid transparent;border-radius:0;background:transparent;color:var(--pico-muted-color);font-size:13px;box-shadow:none;}
  .pane-modes button.active{color:var(--pico-primary);border-bottom-color:var(--pico-primary);}
  .pane-navigation>a{font-size:12px;text-decoration:none;}
  .pane-body,.pane-panel{display:flex;flex-direction:column;flex:1 1 auto;min-height:0;min-width:0;overflow:hidden;}
  .pane-panel[hidden]{display:none;}.pane-panel.scroll{overflow-y:auto;padding:16px;}
  .pane-error{margin:8px 20px;color:var(--pico-del-color);font-size:13px;}
  .identity{font-size:13px;color:var(--pico-muted-color);}
  @container(max-width:500px){.pane-header{padding:12px;}.pane-navigation{padding:0 12px;gap:14px;}.pane-modes{gap:14px;}}
</style>
