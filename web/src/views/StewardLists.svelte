<script lang="ts">
  import { onMount } from 'svelte';
  import { addFamilyListItems, assignFamilyListItems, checkFamilyListItems, clearDoneFamilyListItems, listFamilyListGroups, listFamilyLists,
    removeFamilyListItem, type FamilyListGroup, type FamilyListItem, type FamilyListName, type FamilyListScope } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  // The family lists (step 25 C4, design 7.9.5): shopping and to-do, shared with a family group
  // (they add and check items there too), so the view re-reads on a slow cadence. Step 23.1
  // (7.7.24): each group has its own lists - the page names one group (a choice above the lists
  // when there is more than one: bound groups, unbound ones that keep items - shown and removed,
  // never added to - and the items no group owns).
  const REFRESH_MS = 30000;
  const LISTS: FamilyListName[] = ['shopping', 'todo'];
  const ADD_MAX = 10;
  let lists = $state<Record<FamilyListName, FamilyListItem[]>>({shopping: [], todo: []});
  // Checked items of a list the read did not show (at most 100 are shown, director 505).
  let doneMore = $state<Record<FamilyListName, number>>({shopping: 0, todo: 0});
  let loaded = $state(false), error = $state(''), busy = $state(false);
  let drafts = $state<Record<FamilyListName, string>>({shopping: '', todo: ''});
  let groups = $state<FamilyListGroup[]>([]), unassigned = $state(0);
  // The chosen group: its key (channel + group_ref), or 'unassigned'.
  let chosen = $state('');
  const keyOf = (g: FamilyListGroup) => g.channel + ' ' + g.group_ref;
  const scope = $derived<FamilyListScope | null>(chosen === 'unassigned' ? 'unassigned'
    : (() => { const g = groups.find(x => keyOf(x) === chosen); return g ? {channel: g.channel, group_ref: g.group_ref} : null; })());
  const current = $derived(groups.find(x => keyOf(x) === chosen));
  // Items are added to, and checked on, a bound group's lists only: an unbound group's and the
  // unassigned ones are shown and removed (director 602 defect 5; the backend refuses the rest).
  const addable = $derived(!!current?.bound);
  // Unassigned items are moved into a bound group's lists by the owner (review F2).
  const boundGroups = $derived(groups.filter(g => g.bound));
  function assign(itemId: string, key: string) {
    const g = boundGroups.find(x => keyOf(x) === key);
    if (g) void run(() => assignFamilyListItems({channel: g.channel, group_ref: g.group_ref}, [itemId]));
  }
  let alive = true;
  async function load() {
    try {
      const listed = await listFamilyListGroups();
      if (!alive) return;
      groups = listed.groups ?? []; unassigned = listed.unassigned ?? 0;
      if (chosen !== 'unassigned' || !unassigned) {
        if (!groups.some(g => keyOf(g) === chosen)) chosen = groups[0] ? keyOf(groups[0]) : unassigned ? 'unassigned' : '';
      }
      if (!scope) { lists = {shopping: [], todo: []}; doneMore = {shopping: 0, todo: 0}; loaded = true; error = ''; return; }
      const result = await listFamilyLists(scope);
      if (!alive) return;
      lists = {shopping: result.lists?.shopping ?? [], todo: result.lists?.todo ?? []}; error = ''; loaded = true;
      doneMore = {shopping: result.done_more?.shopping ?? 0, todo: result.done_more?.todo ?? 0};
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
    // One item per line (or separated by 、 , ，), at most 10 at a time: more is refused, not cut.
    const items = drafts[list].split(/[\n、,，]/).map(s => s.trim()).filter(Boolean);
    if (!items.length) return;
    if (items.length > ADD_MAX) { error = t('steward.listTooMany').replace('{n}', String(ADD_MAX)); return; }
    const where = scope;
    if (!where) return;
    void run(async () => { await addFamilyListItems(where, list, items); drafts[list] = ''; });
  }
  const when = (iso: string | null) => iso ? new Date(iso).toLocaleString() : '';
  // An item whose family group was unbound keeps no "who" (added_by / done_by null, design
  // 7.9.5): it reads 「曾由一位成员添加」, never a blank.
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
  {#if groups.length + (unassigned ? 1 : 0) > 1}
    <label class="pick">{t('steward.listsGroup')}
      <select bind:value={chosen} onchange={() => void load()} data-list-group>
        {#each groups as g (keyOf(g))}
          <option value={keyOf(g)}>{g.bound ? (g.label ?? g.group_ref) : t('steward.listsUnbound').replace('{name}', g.label ?? g.group_ref)}</option>
        {/each}
        {#if unassigned}<option value="unassigned">{t('steward.listsUnassigned')}</option>{/if}
      </select></label>
  {/if}
  {#if loaded && !scope}<p class="empty" data-lists-no-group>{t('steward.listsNoGroup')}</p>{/if}
  {#each scope ? LISTS : [] as list (list)}
    {@const items = lists[list]}
    <article class="list" data-list={list}>
      <header>
        <strong>{t('steward.list.' + list)}</strong>
        <small>{t('steward.listOpen').replace('{n}', String(items.filter(i => !i.done).length))}</small>
        {#if items.some(i => i.done)}
          <button type="button" class="outline" disabled={busy} data-list-clear={list}
            onclick={() => void run(() => clearDoneFamilyListItems(scope!, list))}>{t('steward.listClearDone')}</button>
        {/if}
      </header>
      {#if loaded && !items.length}<p class="empty">{t('steward.listEmpty')}</p>{/if}
      <ul>
        {#each items as item (item.item_id)}
          <li class:done={item.done} data-item={item.item_id}>
            <label class="check"><input type="checkbox" checked={item.done} disabled={busy || !addable} data-item-check={item.item_id}
              onchange={(event) => void run(() => checkFamilyListItems(scope!, [item.item_id], event.currentTarget.checked))} />
              <span class="text">{item.text}</span></label>
            <small class="who" data-item-who>{item.added_by ? t('steward.listAddedBy').replace('{who}', item.added_by).replace('{when}', when(item.added_at)) : t('steward.listAddedByFormer').replace('{when}', when(item.added_at))}{#if item.done} · {item.done_by ? t('steward.listDoneBy').replace('{who}', item.done_by) : t('steward.listDoneByFormer')}{/if}</small>
            {#if scope === 'unassigned' && boundGroups.length}
              <select class="assign" disabled={busy} data-item-assign={item.item_id} aria-label={t('steward.listAssign')}
                onchange={(event) => assign(item.item_id, event.currentTarget.value)}>
                <option value="">{t('steward.listAssign')}</option>
                {#each boundGroups as g (keyOf(g))}<option value={keyOf(g)}>{g.label ?? g.group_ref}</option>{/each}
              </select>
            {/if}
            <button type="button" class="outline danger" disabled={busy} data-item-remove={item.item_id}
              onclick={() => void run(() => removeFamilyListItem(scope!, item.item_id))}>{t('steward.listRemove')}</button>
          </li>
        {/each}
      </ul>
      {#if doneMore[list] > 0}<p class="more" data-list-more={list}>{t('steward.listDoneMore').replace('{n}', String(doneMore[list]))}</p>{/if}
      {#if addable}
        <form class="add" onsubmit={(event) => { event.preventDefault(); add(list); }}>
          <input type="text" maxlength="1100" bind:value={drafts[list]} placeholder={t('steward.listAddPlaceholder')}
            aria-label={t('steward.listAdd') + ' ' + t('steward.list.' + list)} data-list-input={list} />
          <button type="submit" class="outline" disabled={busy || !drafts[list].trim()}>{t('steward.listAdd')}</button>
        </form>
      {/if}
    </article>
  {/each}
</section>
<style>
  .steward-lists{font-size:13px;}
  .intro,.empty,.more{color:var(--pico-muted-color);font-size:12px;}
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
  .assign{width:auto;margin:0;padding:2px 8px;font-size:12px;}
  .pick{display:flex;gap:8px;align-items:center;font-size:12px;margin:0 0 8px;}.pick select{width:auto;margin:0;font-size:13px;}
</style>
