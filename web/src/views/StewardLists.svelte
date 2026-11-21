<script lang="ts">
  import { onMount } from 'svelte';
  import { addFamilyListItems, checkFamilyListItems, clearDoneFamilyListItems, listFamilyLists, removeFamilyListItem,
    type FamilyListItem, type FamilyListName } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  // The family lists (step 25 C4, design 7.9.5): shopping and to-do, shared with the family group
  // (they add and check items there too), so the view re-reads on a slow cadence.
  const REFRESH_MS = 30000;
  const LISTS: FamilyListName[] = ['shopping', 'todo'];
  let lists = $state<Record<FamilyListName, FamilyListItem[]>>({shopping: [], todo: []});
  let loaded = $state(false), error = $state(''), busy = $state(false);
  let drafts = $state<Record<FamilyListName, string>>({shopping: '', todo: ''});
  let alive = true;
  async function load() {
    try {
      const result = await listFamilyLists();
      if (!alive) return;
      lists = {shopping: result.lists?.shopping ?? [], todo: result.lists?.todo ?? []}; error = ''; loaded = true;
    } catch (e) { if (alive) error = String(e instanceof Error ? e.message : e); }
  }
  async function run(action: () => Promise<unknown>) {
    if (busy) return;
    busy = true; error = '';
    try { await action(); await load(); }
    catch (e) { error = String(e instanceof Error ? e.message : e); }
    finally { busy = false; }
  }
  function add(list: FamilyListName) {
    // One item per line (or separated by 、 , ，), at most 10 at a time.
    const items = drafts[list].split(/[\n、,，]/).map(s => s.trim()).filter(Boolean).slice(0, 10);
    if (!items.length) return;
    void run(async () => { await addFamilyListItems(list, items); drafts[list] = ''; });
  }
  const when = (iso: string | null) => iso ? new Date(iso).toLocaleString() : '';
  onMount(() => {
    alive = true;
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => { alive = false; clearInterval(timer); };
  });
</script>
<section class="steward-lists" aria-label={t('steward.tabLists')}>
  <p class="intro">{t('steward.listsIntro')}</p>
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if !loaded && !error}<p aria-busy="true">{t('steward.loading')}</p>{/if}
  {#each LISTS as list (list)}
    {@const items = lists[list]}
    <article class="list" data-list={list}>
      <header>
        <strong>{t('steward.list.' + list)}</strong>
        <small>{t('steward.listOpen').replace('{n}', String(items.filter(i => !i.done).length))}</small>
        {#if items.some(i => i.done)}
          <button type="button" class="outline" disabled={busy} data-list-clear={list}
            onclick={() => void run(() => clearDoneFamilyListItems(list))}>{t('steward.listClearDone')}</button>
        {/if}
      </header>
      {#if loaded && !items.length}<p class="empty">{t('steward.listEmpty')}</p>{/if}
      <ul>
        {#each items as item (item.item_id)}
          <li class:done={item.done} data-item={item.item_id}>
            <label class="check"><input type="checkbox" checked={item.done} disabled={busy} data-item-check={item.item_id}
              onchange={(event) => void run(() => checkFamilyListItems([item.item_id], event.currentTarget.checked))} />
              <span class="text">{item.text}</span></label>
            <small class="who" data-item-who>{t('steward.listAddedBy').replace('{who}', item.added_by).replace('{when}', when(item.added_at))}{#if item.done && item.done_by} · {t('steward.listDoneBy').replace('{who}', item.done_by)}{/if}</small>
            <button type="button" class="outline danger" disabled={busy} data-item-remove={item.item_id}
              onclick={() => void run(() => removeFamilyListItem(item.item_id))}>{t('steward.listRemove')}</button>
          </li>
        {/each}
      </ul>
      <form class="add" onsubmit={(event) => { event.preventDefault(); add(list); }}>
        <input type="text" maxlength="1100" bind:value={drafts[list]} placeholder={t('steward.listAddPlaceholder')}
          aria-label={t('steward.listAdd') + ' ' + t('steward.list.' + list)} data-list-input={list} />
        <button type="submit" class="outline" disabled={busy || !drafts[list].trim()}>{t('steward.listAdd')}</button>
      </form>
    </article>
  {/each}
</section>
<style>
  .steward-lists{font-size:13px;}
  .intro,.empty{color:var(--pico-muted-color);font-size:12px;}
  .list{margin:0 0 10px;padding:12px 14px;border:1px solid var(--pico-muted-border-color);border-radius:8px;}
  header{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 6px;padding:0;background:none;border:0;}
  header small{color:var(--pico-muted-color);}header button{margin:0 0 0 auto;}
  ul{margin:0 0 8px;padding:0;}
  li{list-style:none;display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0;padding:4px 0;border-bottom:1px solid var(--pico-muted-border-color);}
  li.done .text{text-decoration:line-through;color:var(--pico-muted-color);}
  .check{display:flex;gap:6px;align-items:center;margin:0;font-size:13px;}.check input{margin:0;}
  .who{color:var(--pico-muted-color);font-size:11px;flex:1 1 auto;}
  button{width:auto;margin:0;padding:3px 10px;font-size:12px;}
  .danger{color:var(--pico-del-color);border-color:var(--pico-del-color);}
  .add{display:flex;gap:8px;margin:0;}.add input{margin:0;font-size:13px;}
  .error{color:var(--pico-del-color);}
</style>
