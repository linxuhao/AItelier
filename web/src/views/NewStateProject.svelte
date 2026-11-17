<script lang="ts">
  import { createStateProject, sendDriverConversationMessage, startDshDriver } from '../lib/api';
  import { st } from '../lib/stateI18n.svelte';
  /** Plugin step 10: a new idea gets a State project (and, if asked, its driver)
   *  from the dashboard. The goal is sent as the owner's first message in the
   *  project's conversation, so the director reads it in the owner's own words.
   *  The project is not published. Each step reports its own failure: a project
   *  that was created stays created even when a later step fails. */
  let { oncreated = () => {} }: { oncreated?: (projectId: string) => void } = $props();
  let open = $state(false), busy = $state(false), error = $state(''), done = $state('');
  let projectId = $state(''), title = $state(''), goal = $state(''), source = $state(''), startDriver = $state(true);
  const KEY = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  const message = (e: unknown) => String(e instanceof Error ? e.message : e);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    error = ''; done = '';
    const id = projectId.trim(), name = title.trim();
    if (!KEY.test(id)) { error = st('newProjectIdInvalid'); return; }
    if (!name) { error = st('newProjectTitleRequired'); return; }
    busy = true;
    try {
      try { await createStateProject(id, name, source.trim() || undefined); }
      catch (e) { error = message(e); return; }
      const failures: string[] = [];
      if (goal.trim()) {
        try { await sendDriverConversationMessage(id, goal.trim()); }
        catch (e) { failures.push(st('newProjectGoalFailed') + ': ' + message(e)); }
      }
      if (startDriver) {
        try { await startDshDriver(id); }
        catch (e) { failures.push(st('newProjectStartFailed') + ': ' + message(e)); }
      }
      oncreated(id);
      if (failures.length) { error = st('newProjectCreatedWithErrors') + ' ' + failures.join(' · '); return; }
      done = st('newProjectCreated');
      projectId = ''; title = ''; goal = ''; source = ''; open = false;
    } finally { busy = false; }
  }
</script>
<div class="new-project">
  {#if !open}
    <button type="button" class="outline new-project-open" onclick={() => { open = true; error = ''; done = ''; }}>{st('newProject')}</button>
    {#if done}<p role="status" class="new-project-done">{done}</p>{/if}
  {:else}
    <form class="new-project-form" aria-label={st('newProject')} onsubmit={submit}>
      <label>{st('newProjectId')}<input name="project_id" bind:value={projectId} autocomplete="off" required /></label>
      <label>{st('newProjectTitle')}<input name="title" bind:value={title} required /></label>
      <label>{st('newProjectGoal')}<textarea name="goal" rows="3" bind:value={goal}></textarea></label>
      <label>{st('newProjectSource')}<input name="source_project_id" bind:value={source} autocomplete="off" /></label>
      <label class="new-project-start"><input type="checkbox" name="start_driver" bind:checked={startDriver} /> {st('newProjectStartDriver')}</label>
      <p class="new-project-note">{st('newProjectPrivateNote')}</p>
      {#if error}<p role="alert" class="new-project-error">{error}</p>{/if}
      <div class="new-project-actions">
        <button type="submit" disabled={busy}>{busy ? '…' : st('newProjectCreate')}</button>
        <button type="button" class="outline" disabled={busy} onclick={() => { open = false; error = ''; }}>{st('newProjectCancel')}</button>
      </div>
    </form>
  {/if}
</div>
<style>
  .new-project{margin:0 0 12px;}
  .new-project-open{width:auto;font-size:13px;padding:4px 12px;}
  .new-project-form{display:grid;gap:8px;max-width:520px;padding:12px;border:1px solid var(--pico-muted-border-color);border-radius:8px;}
  .new-project-form label{font-size:13px;margin:0;}
  .new-project-form input:not([type=checkbox]),.new-project-form textarea{margin:4px 0 0;font-size:13px;}
  .new-project-start{display:flex;gap:6px;align-items:center;}
  .new-project-note{font-size:12px;color:var(--pico-muted-color);margin:0;}
  .new-project-error{font-size:12px;color:var(--pico-del-color);margin:0;}
  .new-project-done{font-size:12px;color:var(--pico-ins-color);margin:4px 0 0;}
  .new-project-actions{display:flex;gap:8px;}.new-project-actions button{width:auto;margin:0;font-size:13px;padding:4px 12px;}
</style>
