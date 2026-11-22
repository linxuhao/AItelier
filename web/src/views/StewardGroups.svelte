<script lang="ts">
  // Step 23 (design-steward 7.7.13): the family groups under a channel - since step 23.1 (7.7.24)
  // one row per bound group, each with its own records (withheld replies with their original and
  // reason), member names, family note, family memory, clear and unbind, every action naming that
  // group; below them 「添加群」 (a named group code the owner posts in the group) until the
  // channel has groups_per_channel groups. What a group may see is decided once for everything
  // (public projects), never here.
  import { onMount } from 'svelte';
  import { clearGroupRecords, createGroupCode, deleteFamilyMemory, listOwnerGroups, nameGroupMember, setFamilyNote,
    unbindOwnerGroup, type OwnerChannelName, type OwnerGroup } from '../lib/api';
  import { GROUP_REFUSALS } from '../lib/steward';
  import { t } from '../lib/i18n.svelte';

  let { channel, bound }: { channel: OwnerChannelName; bound: boolean } = $props();
  let groups = $state<OwnerGroup[]>([]);
  let cap = $state(3);
  // The group whose records are open (its group_ref), at most one at a time.
  let openRef = $state<string | null>(null);
  let code = $state<{code: string; expires_at: string} | null>(null);
  let label = $state('');
  let notes = $state<Record<string, string>>({});
  let error = $state('');

  // A refusal of the group routes in words (too_many_groups, group_label_taken, ...), else as it came.
  function said(e: unknown): string {
    const text = String((e as Error)?.message ?? e);
    const code = GROUP_REFUSALS.find(c => text.includes(c));
    return code ? t('steward.groupReason.' + code) : text;
  }
  async function load() {
    try {
      const listed = await listOwnerGroups(openRef ? channel : undefined, openRef ?? undefined);
      groups = listed.groups.filter(g => g.channel === channel);
      cap = listed.groups_per_channel ?? cap;
      if (openRef && !groups.some(g => g.group_ref === openRef)) openRef = null;
      for (const g of groups) if (g.group_ref === openRef) notes[g.group_ref] = g.family_note?.text ?? '';
      error = '';
    } catch (e) { error = said(e); }
  }
  async function act(run: () => Promise<unknown>) {
    try { await run(); await load(); } catch (e) { error = said(e); }
  }
  async function addGroup() {
    const name = label.trim();
    if (!name) { error = t('steward.groupLabelRequired'); return; }
    await act(async () => { code = await createGroupCode(channel, name); label = ''; });
  }
  async function toggle(groupRef: string) { openRef = openRef === groupRef ? null : groupRef; await load(); }
  async function rename(groupRef: string, senderRef: string, current: string) {
    const name = window.prompt(t('steward.groupNameMember'), current);
    if (name && name.trim()) await act(() => nameGroupMember(channel, groupRef, senderRef, name.trim()));
  }
  const full = $derived(groups.length >= cap);
  onMount(load);
</script>

<div class="groups" data-groups={channel}>
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#each groups as group (group.group_ref)}
    <div class="group" data-group={group.group_ref}>
      <p data-group-bound><strong>{t('steward.groupTitle')}</strong> {group.label ?? group.group_ref.slice(-6)}
        · {t('steward.groupCount').replace('{n}', String(group.messages_30d))}
        {#if group.last_message_at}· <small>{new Date(group.last_message_at).toLocaleString()}</small>{/if}</p>
      <div class="actions">
        <button type="button" class="outline" data-group-records={group.group_ref}
          onclick={() => void toggle(group.group_ref)}>{openRef === group.group_ref ? t('steward.groupHide') : t('steward.groupRecords')}</button>
        <button type="button" class="outline" data-group-clear={group.group_ref}
          onclick={() => void act(() => clearGroupRecords(channel, group.group_ref))}>{t('steward.groupClear')}</button>
        <button type="button" class="outline danger" data-group-unbind={group.group_ref}
          onclick={() => window.confirm(t('steward.groupUnbindConfirm').replace('{name}', group.label ?? group.group_ref))
            && void act(() => unbindOwnerGroup(channel, group.group_ref))}>{t('steward.groupUnbind')}</button>
      </div>
      {#if openRef === group.group_ref}
        <h4>{t('steward.groupMembers')}</h4>
        <ul>{#each group.members as m (m.sender_ref)}
          <li>{m.label} <button type="button" class="link" data-member-name={m.sender_ref}
            onclick={() => void rename(group.group_ref, m.sender_ref, m.label)}>{t('steward.groupNameMember')}</button></li>
        {/each}</ul>
        <h4>{t('steward.groupFamilyNote')}</h4>
        <textarea maxlength="1000" bind:value={notes[group.group_ref]} rows="3" data-family-note></textarea>
        <button type="button" class="outline" data-family-note-save
          onclick={() => void act(() => setFamilyNote(channel, group.group_ref, notes[group.group_ref] ?? ''))}>{t('steward.groupSave')}</button>
        <h4>{t('steward.groupFamilyMemory')}</h4>
        <ul>{#each group.family_memory ?? [] as e (e.entry_id)}
          <li data-family-entry>{e.text} <small>{e.added_by_label ?? ''} {new Date(e.at).toLocaleString()}</small>
            <button type="button" class="link" onclick={() => void act(() => deleteFamilyMemory(channel, group.group_ref, e.entry_id))}>{t('steward.groupDelete')}</button></li>
        {/each}</ul>
        {#if (group.family_memory ?? []).length}
          <button type="button" class="outline" onclick={() => void act(() => deleteFamilyMemory(channel, group.group_ref))}>{t('steward.groupClearMemory')}</button>
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
    </div>
  {/each}
  {#if code}
    <div class="code" data-group-code><code>{code.code}</code>
      <p>{t('steward.groupCodeHelp')}</p></div>
  {/if}
  {#if full}
    <p class="muted" data-groups-full>{t('steward.groupFull').replace('{n}', String(cap))}</p>
  {:else}
    <div class="actions">
      <input type="text" maxlength="20" required placeholder={t('steward.groupLabel')} bind:value={label} data-group-label />
      <button type="button" class="outline" disabled={!bound || !label.trim()} data-group-add onclick={() => void addGroup()}>{t('steward.groupAdd')}</button>
    </div>
  {/if}
</div>

<style>
  .groups{margin:8px 0 0;padding:8px 0 0;border-top:1px dashed var(--pico-muted-border-color);}
  .group{margin:0 0 8px;}
  .actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}.actions button,.actions input{width:auto;margin:0;padding:4px 10px;font-size:12px;}
  .link{background:none;border:0;padding:0 4px;color:var(--pico-primary);font-size:12px;width:auto;margin:0;}
  .records{font-size:12px;}.withheld{color:var(--pico-del-color);}.original{color:var(--pico-muted-color);overflow-wrap:anywhere;}
  .code{margin:0 0 8px;padding:8px 10px;border-radius:6px;background:var(--pico-code-background-color);}
  .danger{color:var(--pico-del-color);border-color:var(--pico-del-color);}.error{color:var(--pico-del-color);}
  .muted{color:var(--pico-muted-color);font-size:12px;}
  h4{font-size:13px;margin:8px 0 4px;}
</style>
