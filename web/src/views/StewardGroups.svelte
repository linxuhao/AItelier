<script lang="ts">
  // Step 23 (design-steward 7.7.13): the family group under a channel. Add a group (a group
  // code the owner posts in the group), read its records (withheld replies with their original
  // and reason), name members, write the family note, delete family memory, clear, unbind.
  // What the group may see is decided once for everything (public projects), never here.
  import { onMount } from 'svelte';
  import { clearGroupRecords, createGroupCode, deleteFamilyMemory, listOwnerGroups, nameGroupMember, setFamilyNote,
    unbindOwnerGroup, type OwnerChannelName, type OwnerGroup } from '../lib/api';
  import { t } from '../lib/i18n.svelte';

  let { channel, bound }: { channel: OwnerChannelName; bound: boolean } = $props();
  let group = $state<OwnerGroup | null>(null);
  let open = $state(false);
  let code = $state<{code: string; expires_at: string} | null>(null);
  let label = $state('');
  let note = $state('');
  let error = $state('');

  async function load() {
    try {
      const listed = await listOwnerGroups(open ? channel : undefined);
      group = listed.groups.find(g => g.channel === channel) ?? null;
      note = group?.family_note?.text ?? '';
      error = '';
    } catch (e) { error = String((e as Error)?.message ?? e); }
  }
  async function act(run: () => Promise<unknown>) {
    try { await run(); await load(); } catch (e) { error = String((e as Error)?.message ?? e); }
  }
  async function addGroup() { await act(async () => { code = await createGroupCode(channel, label.trim() || undefined); }); }
  async function toggle() { open = !open; await load(); }
  async function rename(senderRef: string, current: string) {
    const name = window.prompt(t('steward.groupNameMember'), current);
    if (name && name.trim()) await act(() => nameGroupMember(channel, senderRef, name.trim()));
  }
  onMount(load);
</script>

<div class="groups" data-groups={channel}>
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if group}
    <p data-group-bound><strong>{t('steward.groupTitle')}</strong> {group.label ?? group.group_ref.slice(-6)}
      · {t('steward.groupCount').replace('{n}', String(group.messages_30d))}
      {#if group.last_message_at}· <small>{new Date(group.last_message_at).toLocaleString()}</small>{/if}</p>
    <div class="actions">
      <button type="button" class="outline" onclick={() => void toggle()}>{open ? t('steward.groupHide') : t('steward.groupRecords')}</button>
      <button type="button" class="outline" onclick={() => void act(() => clearGroupRecords(channel))}>{t('steward.groupClear')}</button>
      <button type="button" class="outline danger" data-group-unbind
        onclick={() => window.confirm(t('steward.groupUnbindConfirm')) && void act(() => unbindOwnerGroup(channel))}>{t('steward.groupUnbind')}</button>
    </div>
    {#if open}
      <h4>{t('steward.groupMembers')}</h4>
      <ul>{#each group.members as m (m.sender_ref)}
        <li>{m.label} <button type="button" class="link" data-member-name={m.sender_ref} onclick={() => void rename(m.sender_ref, m.label)}>{t('steward.groupNameMember')}</button></li>
      {/each}</ul>
      <h4>{t('steward.groupFamilyNote')}</h4>
      <textarea maxlength="1000" bind:value={note} rows="3" data-family-note></textarea>
      <button type="button" class="outline" onclick={() => void act(() => setFamilyNote(channel, note))}>{t('steward.groupSave')}</button>
      <h4>{t('steward.groupFamilyMemory')}</h4>
      <ul>{#each group.family_memory ?? [] as e (e.entry_id)}
        <li data-family-entry>{e.text} <small>{e.added_by_label ?? ''} {new Date(e.at).toLocaleString()}</small>
          <button type="button" class="link" onclick={() => void act(() => deleteFamilyMemory(channel, e.entry_id))}>{t('steward.groupDelete')}</button></li>
      {/each}</ul>
      {#if (group.family_memory ?? []).length}
        <button type="button" class="outline" onclick={() => void act(() => deleteFamilyMemory(channel))}>{t('steward.groupClearMemory')}</button>
      {/if}
      <h4>{t('steward.groupRecords')}</h4>
      <ol class="records">{#each group.messages ?? [] as r (r.message_id)}
        <li data-group-record={r.direction} class:withheld={!!r.withheld_reason}>
          <small>{new Date(r.at).toLocaleString()}</small>
          <!-- A group reminder (kind 'notice', sender_role null, design 7.9.4) is the steward's notice. -->
          {r.kind === 'notice' ? t('steward.groupNoticeFrom') + ':' : r.direction === 'in' ? (r.sender_label ?? '') + ':' : '→'}
          {r.body ?? t('steward.groupNoBody').replace('{kind}', r.kind)}
          {#if r.withheld_reason}<div class="original" data-withheld>{t('steward.groupWithheld').replace('{reason}', r.withheld_reason)}: {r.original}</div>{/if}
        </li>
      {/each}</ol>
    {/if}
  {:else}
    {#if code}
      <div class="code" data-group-code><code>{code.code}</code>
        <p>{t('steward.groupCodeHelp')}</p></div>
    {/if}
    <div class="actions">
      <input type="text" maxlength="40" placeholder={t('steward.groupLabel')} bind:value={label} />
      <button type="button" class="outline" disabled={!bound} data-group-add onclick={() => void addGroup()}>{t('steward.groupAdd')}</button>
    </div>
  {/if}
</div>

<style>
  .groups{margin:8px 0 0;padding:8px 0 0;border-top:1px dashed var(--pico-muted-border-color);}
  .actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}.actions button,.actions input{width:auto;margin:0;padding:4px 10px;font-size:12px;}
  .link{background:none;border:0;padding:0 4px;color:var(--pico-primary);font-size:12px;width:auto;margin:0;}
  .records{font-size:12px;}.withheld{color:var(--pico-del-color);}.original{color:var(--pico-muted-color);overflow-wrap:anywhere;}
  .code{margin:0 0 8px;padding:8px 10px;border-radius:6px;background:var(--pico-code-background-color);}
  .danger{color:var(--pico-del-color);border-color:var(--pico-del-color);}.error{color:var(--pico-del-color);}
  h4{font-size:13px;margin:8px 0 4px;}
</style>
