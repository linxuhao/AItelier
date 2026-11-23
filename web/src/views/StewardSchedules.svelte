<script lang="ts">
  import { onMount } from 'svelte';
  import { ApiError, deleteStewardSchedule, listStewardSchedules, updateStewardSchedule,
    type StewardSchedule } from '../lib/api';
  import { SCHEDULE_PRESETS, formatRunAt, scheduleConflict } from '../lib/steward';
  import { t } from '../lib/i18n.svelte';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import StewardScheduleForm from './StewardScheduleForm.svelte';
  // The owner's view of the steward's reminders (design 7.4). Run times move when
  // the steward's host fires a reminder, so the list re-reads on a slow cadence.
  const REFRESH_MS = 30000;
  let rows = $state<StewardSchedule[]>([]), loaded = $state(false), error = $state(''), notice = $state('');
  let editing = $state<StewardSchedule | 'new' | null>(null), busyId = $state('');
  let deleteAsk = $state(false), target = $state<StewardSchedule | null>(null);
  let alive = true;
  async function load() {
    try {
      const result = await listStewardSchedules();
      if (!alive) return;
      rows = result; error = ''; loaded = true;
    } catch (e) { if (alive) error = String(e instanceof Error ? e.message : e); }
  }
  function put(row: StewardSchedule) {
    rows = rows.some(r => r.schedule_id === row.schedule_id)
      ? rows.map(r => r.schedule_id === row.schedule_id ? row : r) : [...rows, row];
  }
  // A 409 carries the row that won: show it and say so; without a row, re-read.
  function failed(e: unknown) {
    const current = scheduleConflict(e);
    if (current) { put(current); notice = t('steward.schedChanged'); }
    else if (e instanceof ApiError && e.status === 409) { notice = t('steward.schedChanged'); void load(); }
    else error = String(e instanceof Error ? e.message : e);
  }
  async function toggle(row: StewardSchedule, input: HTMLInputElement) {
    if (busyId) return;
    busyId = row.schedule_id; notice = ''; error = '';
    try { put(await updateStewardSchedule(row.schedule_id, row.version, {enabled: !row.enabled})); }
    catch (e) { input.checked = !!row.enabled; failed(e); }
    finally { busyId = ''; }
  }
  function askDelete(row: StewardSchedule) { target = row; deleteAsk = true; }
  async function remove() {
    const row = target;
    if (!row) return;
    notice = ''; error = '';
    try {
      await deleteStewardSchedule(row.schedule_id, row.version);
      rows = rows.filter(r => r.schedule_id !== row.schedule_id);
    } catch (e) { failed(e); }
  }
  // A row is named after the preset its expression matches - never after one that fills a
  // title and instruction (the daily brief): any 08:00 reminder shares that expression.
  const presetId = (expr: string) => SCHEDULE_PRESETS.find(p => !p.fill && p.expr === expr)?.id;
  onMount(() => {
    alive = true;
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => { alive = false; clearInterval(timer); };
  });
</script>
<section class="steward-schedules" aria-label={t('steward.tabSchedules')}>
  {#if notice}<p class="notice" role="status">{notice}</p>{/if}
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if editing}
    {#key editing}
      <StewardScheduleForm row={editing === 'new' ? null : editing} onchange={put} onclose={() => { editing = null; }} />
    {/key}
  {:else}
    <div class="actions"><button type="button" class="outline" onclick={() => { editing = 'new'; notice = ''; }}>{t('steward.schedNew')}</button></div>
  {/if}
  {#if !loaded && !error}<p aria-busy="true">{t('steward.loading')}</p>
  {:else if loaded && !rows.length}<p class="empty">{t('steward.schedEmpty')}</p>{/if}
  {#each rows as row (row.schedule_id)}
    {@const on = !!row.enabled}
    {@const preset = row.schedule.kind === 'cron' ? presetId(row.schedule.expr) : undefined}
    <article class="schedule" class:disabled={!on} data-schedule={row.schedule_id} data-enabled={on ? '1' : '0'}>
      <header>
        <strong>{row.title}</strong>
        {#if row.updated_by === 'steward'}<span class="mark" data-steward-edited>{t('steward.schedStewardEdited')}</span>{/if}
        <!-- Step 25 C11: posted as written into the family group; marked once that group is unbound. -->
        {#if row.target === 'family_group'}<span class="mark group" data-target-group>{t('steward.schedToGroup').replace('{name}', row.target_group_label ?? row.target_group_ref ?? '')}</span>{/if}
        <!-- Step 23.1 (7.9.4): a one-time reminder a family member asked for in that group. -->
        {#if row.created_via === 'group'}<span class="mark" data-made-by-member>{t('steward.schedByMember').replace('{who}', row.created_by_label ?? '').replace('{group}', row.target_group_label ?? '')}</span>{/if}
        {#if row.target === 'family_group' && row.target_unbound}<span class="mark overdue" data-group-unbound>{t('steward.schedGroupUnbound')}</span>{/if}
        <!-- Review 337: a stopped scheduler must not look healthy. Past its time by more than
             five minutes (the steward's clock fires within seconds and re-looks every 4 min). -->
        {#if on && (row.overdue_seconds ?? 0) > 300}<span class="mark overdue" data-overdue>{t('steward.schedOverdue').replace('{minutes}', String(Math.round((row.overdue_seconds ?? 0) / 60)))}</span>{/if}
        <label class="switch"><input type="checkbox" role="switch" checked={on} disabled={busyId === row.schedule_id}
          onchange={(event) => void toggle(row, event.currentTarget)} /> {t('steward.schedEnabled')}</label>
      </header>
      <p class="when" data-schedule-when>
        {#if row.schedule.kind === 'cron'}
          {#if preset}{t('steward.schedPreset.' + preset)} · {/if}<code>{row.schedule.expr}</code> · {t('steward.schedNext')} {formatRunAt(row.next_run_at, row.tz)}
        {:else}
          {t('steward.schedOnceAt')} {formatRunAt(row.schedule.at, row.tz)}
        {/if}
        <small>({row.tz})</small>
      </p>
      <dl>
        <dt>{t('steward.schedNext')}</dt><dd data-next>{formatRunAt(row.next_run_at, row.tz)}</dd>
        <dt>{t('steward.schedLast')}</dt><dd data-last>{formatRunAt(row.last_run_at, row.tz)}</dd>
        <dt>{t('steward.schedCreatedBy')}</dt><dd data-created-by>{t('steward.by.' + row.created_by)}</dd>
        <dt>{row.target === 'family_group' ? t('steward.schedGroupText') : t('steward.schedInstruction')}</dt><dd>{row.instruction}</dd>
      </dl>
      <div class="actions">
        <button type="button" class="outline" onclick={() => { editing = row; notice = ''; }}>{t('steward.schedEdit')}</button>
        <button type="button" class="outline danger" onclick={() => askDelete(row)}>{t('steward.schedDelete')}</button>
      </div>
    </article>
  {/each}
</section>
<ConfirmDialog bind:show={deleteAsk} variant="danger" title={t('steward.schedDeleteTitle')}
  message={t('steward.schedDeleteConfirm').replace('{title}', target?.title ?? '')} confirmLabel={t('steward.schedDelete')}
  onConfirm={remove} />
<style>
  .steward-schedules{font-size:13px;}
  .schedule{margin:0 0 10px;padding:12px 14px;border:1px solid var(--pico-muted-border-color);border-radius:8px;}
  .schedule.disabled{opacity:.65;}
  header{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 6px;padding:0;background:none;border:0;}
  .mark{font-size:11px;padding:1px 6px;border-radius:5px;background:var(--pico-primary);color:var(--pico-primary-inverse);}
  .mark.overdue{background:var(--pico-del-color, #c62828);color:#fff;}
  .mark.group{background:var(--pico-secondary-background, #546e7a);color:#fff;}
  .switch{margin:0 0 0 auto;font-size:12px;display:flex;gap:6px;align-items:center;}.switch input{margin:0;}
  .when{margin:0 0 6px;}.when small{color:var(--pico-muted-color);}
  dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:2px 10px;margin:0 0 8px;}
  dt{color:var(--pico-muted-color);font-weight:normal;}dd{margin:0;overflow-wrap:anywhere;white-space:pre-wrap;}
  .actions{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 8px;}.actions button{width:auto;margin:0;padding:4px 10px;font-size:12px;}
  .danger{color:var(--pico-del-color);border-color:var(--pico-del-color);}
  .empty{color:var(--pico-muted-color);font-size:13px;}
  .notice{color:var(--pico-primary);}.error{color:var(--pico-del-color);}
</style>
