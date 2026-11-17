<script lang="ts">
  import { onMount } from 'svelte';
  import { runMemoryRequest } from '../lib/steward';
  import type { StewardMemoryKind } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  import ConfirmDialog from './ConfirmDialog.svelte';
  // The owner's profile and forgotten entries (design 3.4 / 3.6). The memory
  // lives in the steward's host, out of the backend's reach, so every operation
  // here is a request the steward's host code runs and answers; when it does not
  // answer in time the steward is offline and the page says so.
  let profile = $state<{text: string; version: string; limit: number} | null>(null);
  let editing = $state(false), draft = $state(''), reason = $state(''), baseVersion = $state('');
  let conflict = $state<{text: string; version: string} | null>(null);
  let forgotten = $state<{id: string; at?: string; by?: string}[] | null>(null);
  let forgetIds = $state(''), forgetReason = $state('');
  let busy = $state(false), notice = $state(''), error = $state('');
  let purgeAsk = $state(false), purgeId = $state('');
  async function request(kind: StewardMemoryKind, payload: Record<string, unknown> = {}): Promise<Record<string, any> | null> {
    try {
      const result = await runMemoryRequest(kind, payload);
      if (result === null) error = t('steward.offline');
      return result;
    } catch (e) { error = String(e instanceof Error ? e.message : e); return null; }
  }
  function refusal(result: Record<string, any>): string {
    return t('steward.refused') + ' ' + String(result.refused ?? result.error ?? result.reason ?? '');
  }
  async function run(action: () => Promise<void>) {
    if (busy) return;
    busy = true; notice = ''; error = '';
    try { await action(); } finally { busy = false; }
  }
  const loadProfile = () => run(async () => {
    const result = await request('profile_read');
    if (result && typeof result.text === 'string') profile = {text: result.text, version: String(result.version ?? ''), limit: Number(result.limit ?? 0)};
    else if (result) error = refusal(result);
  });
  const loadForgotten = () => run(async () => {
    const result = await request('forgotten_list');
    if (result && Array.isArray(result.forgotten)) forgotten = result.forgotten;
    else if (result) error = refusal(result);
  });
  function edit() { draft = profile?.text ?? ''; baseVersion = profile?.version ?? ''; reason = ''; conflict = null; editing = true; }
  const save = () => run(async () => {
    const result = await request('profile_update', {text: draft, expected_version: baseVersion, reason: reason.trim()});
    if (!result) return;
    if (result.updated) {
      profile = {text: draft, version: String(result.version ?? ''), limit: profile?.limit ?? 0};
      editing = false; conflict = null; notice = t('steward.profileSaved');
    } else if (result.refused === 'version_conflict') {
      const current = result.current ?? {};
      conflict = {text: String(current.text ?? ''), version: String(current.version ?? '')};
    } else if (result.refused === 'profile_too_long') {
      error = t('steward.profileTooLong').replace('{limit}', String(result.limit ?? profile?.limit ?? ''));
    } else error = refusal(result);
  });
  // The owner starts again from the text that is there now, at its version.
  function startFromCurrent() {
    if (!conflict) return;
    draft = conflict.text; baseVersion = conflict.version;
    profile = {text: conflict.text, version: conflict.version, limit: profile?.limit ?? 0};
    conflict = null;
  }
  const forget = () => run(async () => {
    const ids = forgetIds.split(/[\s,]+/).filter(Boolean);
    if (!ids.length) return;
    const result = await request('forget', {ids, reason: forgetReason.trim()});
    if (!result) return;
    if (result.refused || result.error) { error = refusal(result); return; }
    notice = t('steward.forgetDone'); forgetIds = ''; forgetReason = '';
    const list = await request('forgotten_list');
    if (list && Array.isArray(list.forgotten)) forgotten = list.forgotten;
  });
  const restore = (id: string) => run(async () => {
    const result = await request('restore', {id});
    if (!result) return;
    if (result.refused || result.error) { error = refusal(result); return; }
    notice = t('steward.restored');
    forgotten = (forgotten ?? []).filter(entry => entry.id !== id);
  });
  function askPurge(id: string) { purgeId = id; purgeAsk = true; }
  const purge = () => run(async () => {
    const id = purgeId;
    const result = await request('purge', {ids: [id]});
    if (!result) return;
    if (result.purged) {
      notice = t('steward.purgeDone');
      forgotten = (forgotten ?? []).filter(entry => entry.id !== id);
    } else error = t('steward.purgeFailed') + ' ' + String(result.reason ?? result.refused ?? result.error ?? '');
  });
  onMount(() => { void loadProfile().then(loadForgotten); });
</script>
<section class="steward-profile" aria-label={t('steward.tabProfile')}>
  {#if busy}<p class="working" aria-busy="true">{t('steward.working')}</p>{/if}
  {#if notice}<p class="notice" role="status">{notice}</p>{/if}
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  <h3>{t('steward.tabProfile')}</h3>
  {#if profile}
    <p class="meta"><span>{t('steward.profileVersion')}: <code data-profile-version>{profile.version || '-'}</code></span>
      <span>{(editing ? draft : profile.text).length} / {profile.limit}</span></p>
    {#if editing}
      <label>{t('steward.profileText')}<textarea rows="12" bind:value={draft} data-profile-editor></textarea></label>
      <label>{t('steward.profileReason')}<input type="text" bind:value={reason} /></label>
      {#if conflict}
        <div class="conflict" role="alert">
          <p>{t('steward.profileConflict')}</p>
          <pre data-profile-current>{conflict.text}</pre>
          <button type="button" class="outline" onclick={startFromCurrent}>{t('steward.profileUseCurrent')}</button>
        </div>
      {/if}
      <div class="actions">
        <button type="button" disabled={busy || !reason.trim() || (profile.limit > 0 && draft.length > profile.limit)} onclick={() => void save()}>{t('steward.profileSave')}</button>
        <button type="button" class="secondary outline" disabled={busy} onclick={() => { editing = false; conflict = null; }}>{t('steward.cancel')}</button>
      </div>
    {:else}
      {#if profile.text}<pre class="profile-text">{profile.text}</pre>{:else}<p class="muted">{t('steward.profileEmpty')}</p>{/if}
      <div class="actions"><button type="button" class="outline" disabled={busy} onclick={edit}>{t('steward.profileEdit')}</button></div>
    {/if}
  {:else if !busy}
    <div class="actions"><button type="button" class="outline" onclick={() => void loadProfile()}>{t('steward.retry')}</button></div>
  {/if}
  <h3>{t('steward.forgotten')}</h3>
  <p class="muted">{t('steward.forgetNote')}</p>
  {#if forgotten === null}
    {#if !busy}<div class="actions"><button type="button" class="outline" onclick={() => void loadForgotten()}>{t('steward.retry')}</button></div>{/if}
  {:else if !forgotten.length}<p class="muted">{t('steward.forgottenEmpty')}</p>
  {:else}
    <ul class="forgotten">
      {#each forgotten as entry (entry.id)}
        <li data-forgotten={entry.id}><code>{entry.id}</code>{#if entry.at} <small>{entry.at}</small>{/if}
          <button type="button" class="outline" disabled={busy} onclick={() => void restore(entry.id)}>{t('steward.restore')}</button>
          <button type="button" class="outline danger" disabled={busy} onclick={() => askPurge(entry.id)}>{t('steward.purge')}</button>
        </li>
      {/each}
    </ul>
  {/if}
  <form class="forget" onsubmit={(event) => { event.preventDefault(); void forget(); }}>
    <label>{t('steward.forgetIds')}<input type="text" bind:value={forgetIds} /></label>
    <label>{t('steward.forgetReason')}<input type="text" bind:value={forgetReason} /></label>
    <button type="submit" class="outline" disabled={busy || !forgetIds.trim() || !forgetReason.trim()}>{t('steward.forget')}</button>
  </form>
</section>
<ConfirmDialog bind:show={purgeAsk} variant="danger" title={t('steward.purgeConfirmTitle')}
  message={t('steward.purgeConfirm') + ' (' + purgeId + ')'} confirmLabel={t('steward.purge')}
  onConfirm={purge} />
<style>
  .steward-profile{font-size:13px;}
  h3{font-size:14px;margin:14px 0 6px;}h3:first-of-type{margin-top:0;}
  .meta{display:flex;gap:14px;flex-wrap:wrap;color:var(--pico-muted-color);font-size:12px;margin:0 0 6px;}
  .muted,.working{color:var(--pico-muted-color);font-size:12px;}
  .notice{color:var(--pico-primary);}.error{color:var(--pico-del-color);}
  pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;padding:8px 10px;margin:0 0 8px;}
  label{font-size:12px;}textarea,input{font-size:13px;}
  .conflict{padding:8px;border:1px solid var(--pico-del-color);border-radius:6px;margin:0 0 8px;}
  .actions{display:flex;gap:8px;flex-wrap:wrap;}.actions button,.forgotten button,.forget button{width:auto;margin:0;padding:4px 10px;font-size:12px;}
  .forgotten{padding-left:0;margin:0 0 10px;}.forgotten li{list-style:none;display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 6px;}
  .forgotten .danger{color:var(--pico-del-color);border-color:var(--pico-del-color);}
  .forget{display:grid;gap:4px;margin-top:8px;}.forget label{margin:0;}
</style>
