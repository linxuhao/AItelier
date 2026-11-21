<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { createStewardSchedule, listOwnerGroups, previewStewardSchedule, updateStewardSchedule,
    type OwnerChannelName, type StewardSchedule, type StewardScheduleFields, type StewardScheduleSpec } from '../lib/api';
  import { SCHEDULE_PRESETS, SCHEDULE_REFUSALS, browserTimeZone, formatRunAt, fromLocalInput,
    scheduleConflict, scheduleRefusal, toLocalInput } from '../lib/steward';
  import { t } from '../lib/i18n.svelte';
  const { row = null, onchange, onclose }: {
    /** The row being edited; null creates a new reminder. */
    row?: StewardSchedule | null;
    /** A newer form of a row: the saved one, or the current one a 409 carried. */
    onchange: (row: StewardSchedule) => void;
    onclose: () => void;
  } = $props();
  // The backend computes every run time; this form only asks it (preview) and saves.
  const PREVIEW_DEBOUNCE_MS = 400;
  let title = $state(''), instruction = $state(''), kind = $state<'once' | 'cron'>('cron'),
    at = $state(''), expr = $state(''), tz = $state(browserTimeZone()), baseVersion = $state(0);
  let busy = $state(false), error = $state(''), conflict = $state<StewardSchedule | null>(null);
  let preview = $state<string[] | null>(null), previewError = $state('');
  // Step 25 C11 (design 7.9.4): 「发到：我 / 家庭群」. The family group can be chosen only while one
  // is bound; with a group on each channel the owner names the channel.
  let target = $state<'owner' | 'family_group'>('owner'), channel = $state<OwnerChannelName | ''>('');
  let groups = $state<OwnerChannelName[]>([]);
  onMount(() => {
    listOwnerGroups().then(r => { groups = (r.groups ?? []).map(g => g.channel); }).catch(() => { groups = []; });
  });
  // A new reminder runs on the browser's zone; an edited one keeps the zone it has.
  function fill(source: StewardSchedule) {
    title = source.title; instruction = source.instruction; kind = source.schedule.kind;
    at = source.schedule.kind === 'once' ? toLocalInput(source.schedule.at) : '';
    expr = source.schedule.kind === 'cron' ? source.schedule.expr : '';
    tz = source.tz; baseVersion = source.version;
    target = source.target ?? 'owner'; channel = source.target_channel ?? '';
  }
  const initial = untrack(() => row);
  if (initial) fill(initial);
  const schedule = $derived<StewardScheduleSpec>(kind === 'cron' ? {kind, expr: expr.trim()} : {kind, at: fromLocalInput(at)});
  const ready = $derived(schedule.kind === 'cron' ? !!schedule.expr : !!schedule.at);
  function refusalText(e: unknown): string {
    const refusal = scheduleRefusal(e);
    if (!refusal) return String(e instanceof Error ? e.message : e);
    const label = SCHEDULE_REFUSALS.includes(refusal.code) ? t('steward.schedReason.' + refusal.code) : refusal.code;
    return refusal.reason ? `${label}: ${refusal.reason}` : label;
  }
  let previewSeq = 0;
  $effect(() => {
    const spec = schedule, zone = tz, go = ready;
    preview = null; previewError = '';
    if (!go) return;
    const mine = ++previewSeq;
    const timer = setTimeout(() => {
      previewStewardSchedule(spec, zone)
        .then(runs => { if (mine === previewSeq) preview = runs; })
        .catch(e => { if (mine === previewSeq) previewError = refusalText(e); });
    }, PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  });
  async function save() {
    if (busy) return;
    busy = true; error = ''; conflict = null;
    const fields: StewardScheduleFields = {title: title.trim(), instruction: instruction.trim(), schedule, tz};
    // Sent only when it says something: a reminder to the group (created or re-pointed), or one
    // turned back to the owner. An edit that keeps the target keeps the group it recorded.
    const was = initial?.target ?? 'owner';
    if (initial ? target !== was : target === 'family_group') fields.target = target;
    if (fields.target === 'family_group' && channel) fields.channel = channel;
    try {
      const saved = initial ? await updateStewardSchedule(initial.schedule_id, baseVersion, fields)
        : await createStewardSchedule(fields);
      onchange(saved); onclose();
    } catch (e) {
      // Edited elsewhere meanwhile (the steward, another tab): show what is there now.
      const current = scheduleConflict(e);
      if (current) { conflict = current; onchange(current); } else error = refusalText(e);
    } finally { busy = false; }
  }
  // A preset sets the expression; one that fills (the daily brief) also writes its title and
  // instruction into whichever of the two is still empty - never over what the owner wrote.
  function usePreset(preset: {id: string; expr: string; fill?: boolean}) {
    expr = preset.expr;
    if (!preset.fill) return;
    if (!title.trim()) title = t('steward.schedPresetTitle.' + preset.id);
    if (!instruction.trim()) instruction = t('steward.schedPresetInstruction.' + preset.id);
  }
  function startFromCurrent() {
    if (!conflict) return;
    fill(conflict); conflict = null;
  }
</script>
<form class="schedule-form" aria-label={initial ? t('steward.schedEdit') : t('steward.schedNew')}
  onsubmit={(event) => { event.preventDefault(); void save(); }}>
  <label>{t('steward.schedTitle')}<input type="text" maxlength="200" bind:value={title} /></label>
  <fieldset class="kind" data-schedule-target>
    <legend>{t('steward.schedTarget')}</legend>
    <label><input type="radio" name="schedule-target" value="owner" bind:group={target} /> {t('steward.schedTargetOwner')}</label>
    <label><input type="radio" name="schedule-target" value="family_group" bind:group={target}
      disabled={!groups.length && initial?.target !== 'family_group'} /> {t('steward.schedTargetGroup')}</label>
    {#if !groups.length}<small class="hint">{t('steward.schedTargetNoGroup')}</small>{/if}
  </fieldset>
  {#if target === 'family_group' && groups.length > 1}
    <label>{t('steward.schedTargetChannel')}<select bind:value={channel} data-schedule-channel>
      {#each groups as g (g)}<option value={g}>{g}</option>{/each}
    </select></label>
  {/if}
  {#if target === 'family_group'}
    <label>{t('steward.schedGroupText')}<textarea rows="3" maxlength="500" bind:value={instruction}></textarea></label>
    <small class="hint">{t('steward.schedGroupTextHelp')}</small>
  {:else}
    <label>{t('steward.schedInstruction')}<textarea rows="3" maxlength="2000" bind:value={instruction}></textarea></label>
  {/if}
  <fieldset class="kind">
    <legend>{t('steward.schedKind')}</legend>
    <label><input type="radio" name="schedule-kind" value="cron" bind:group={kind} /> {t('steward.schedKindCron')}</label>
    <label><input type="radio" name="schedule-kind" value="once" bind:group={kind} /> {t('steward.schedKindOnce')}</label>
  </fieldset>
  {#if kind === 'cron'}
    <label>{t('steward.schedExpr')}<input type="text" bind:value={expr} placeholder="0 9 * * 1-5" data-schedule-expr /></label>
    <div class="presets" role="group" aria-label={t('steward.schedPresets')}>
      {#each SCHEDULE_PRESETS as preset (preset.id)}
        <button type="button" class="outline" onclick={() => usePreset(preset)}>{t('steward.schedPreset.' + preset.id)}</button>
      {/each}
    </div>
  {:else}
    <label>{t('steward.schedAt')}<input type="datetime-local" bind:value={at} data-schedule-at /></label>
  {/if}
  <p class="tz">{t('steward.schedTz')}: <code data-schedule-tz>{tz}</code></p>
  {#if ready}
    <div class="preview" aria-label={t('steward.schedPreview')}>
      <strong>{t('steward.schedPreview')}</strong>
      {#if previewError}<p class="error" data-preview-error>{previewError}</p>
      {:else if preview === null}<p aria-busy="true">{t('steward.loading')}</p>
      {:else}<ol data-preview>{#each preview as run, i (i)}<li>{formatRunAt(run, tz)}</li>{/each}</ol>{/if}
    </div>
  {/if}
  {#if conflict}
    <div class="conflict" role="alert">
      <p>{t('steward.schedConflict')}</p>
      <dl data-schedule-current>
        <dt>{t('steward.schedTitle')}</dt><dd>{conflict.title}</dd>
        <dt>{t('steward.schedKind')}</dt><dd>{conflict.schedule.kind === 'cron' ? conflict.schedule.expr : formatRunAt(conflict.schedule.at, conflict.tz)} ({conflict.tz})</dd>
        <dt>{t('steward.schedInstruction')}</dt><dd>{conflict.instruction}</dd>
        <dt>{t('steward.schedVersion')}</dt><dd>{conflict.version} · {t('steward.by.' + conflict.updated_by)}</dd>
      </dl>
      <button type="button" class="outline" onclick={startFromCurrent}>{t('steward.schedUseCurrent')}</button>
    </div>
  {/if}
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  <div class="actions">
    <button type="submit" disabled={busy || !title.trim() || !instruction.trim() || !ready}>{t('steward.schedSave')}</button>
    <button type="button" class="secondary outline" disabled={busy} onclick={onclose}>{t('steward.cancel')}</button>
  </div>
</form>
<style>
  .schedule-form{margin:0 0 12px;padding:12px 14px;border:1px solid var(--pico-muted-border-color);border-radius:8px;font-size:13px;}
  label,legend{font-size:12px;}input,textarea{font-size:13px;}
  .kind{display:flex;gap:14px;flex-wrap:wrap;margin:0 0 6px;}.kind legend{width:100%;}
  .presets,.actions{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 8px;}
  .presets button,.actions button,.conflict button{width:auto;margin:0;padding:4px 10px;font-size:12px;}
  .tz,.hint{color:var(--pico-muted-color);font-size:12px;margin:0 0 6px;}.hint{display:block;}
  .preview{margin:0 0 8px;padding:8px;border-radius:6px;background:var(--pico-card-sectioning-background-color);}
  .preview ol{margin:4px 0 0;padding-left:20px;}.preview li{list-style:decimal;margin:0;}.preview p{margin:4px 0 0;}
  .conflict{padding:8px;border:1px solid var(--pico-del-color);border-radius:6px;margin:0 0 8px;}
  dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:2px 10px;margin:0 0 8px;}
  dt{color:var(--pico-muted-color);font-weight:normal;}dd{margin:0;overflow-wrap:anywhere;}
  .error{color:var(--pico-del-color);}
</style>
