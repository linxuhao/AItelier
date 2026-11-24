<script lang="ts">
  import { onMount } from 'svelte';
  import { ApiError, approveStewardProposal, clearStewardCapabilities, declineStewardProposal, listStewardCapabilities,
    listStewardConnections, setStewardCapability, stewardConnectionTools, unsetStewardCapability,
    type DriverStatusResponse, type StewardCapability, type StewardCapabilityProposal, type StewardConnection,
    type StewardConnectionTool, type StewardRouteEntry } from '../lib/api';
  import { stewardProjectId } from '../lib/steward';
  import { t } from '../lib/i18n.svelte';
  import ConfirmDialog from './ConfirmDialog.svelte';
  // The owner's basic-capabilities table (design 7.8.24). Two things live in a row:
  // - detection (provider/conn_id/tool/detail): the BACKEND's answer, derived from a live
  //   deployment fact or a connection tool ticked under that exact name. This component has
  //   no capability -> provider mapping of its own; it only turns the payload into text, and
  //   shows it as a grey suggestion on a row the owner has not set.
  // - the owner's setting (step 24.4): an ordered route the owner edits here and saves per
  //   row with the row version it read. What the steward uses is the setting alone; detection
  //   and the steward's proposal never write it (only "use this" + save, or approving).
  const { status = null, onconnect }: {
    /** The steward's driver status (StewardPane): the page-top line shows its capability flags. */
    status?: DriverStatusResponse | null;
    /** Open a connection on the connections tab. */
    onconnect?: (connId: string) => void;
  } = $props();
  let rows = $state<StewardCapability[]>([]);
  let loaded = $state(false), error = $state('');
  // Open editors (a row's unsaved route and the row version it started from: a save expects
  // that version, so a write made meanwhile is a version_conflict), per-row save errors, the
  // add-entry picker.
  let drafts = $state<Record<string, {entries: StewardRouteEntry[]; base: number}>>({});
  let rowErrors = $state<Record<string, string>>({});
  let busy = $state('');
  let picker = $state<{cap: string; conn: string; tools: StewardConnectionTool[] | null; error: string} | null>(null);
  let connections = $state<StewardConnection[] | null>(null);
  let dragging: {cap: string; index: number} | null = null;
  let clearAsk = $state(false), clearError = $state('');
  let alive = true;
  async function load() {
    try {
      const answer = await listStewardCapabilities();
      if (!alive) return;
      rows = answer.capabilities ?? [];
      error = ''; loaded = true;
      if (rows.some(row => row.setting)) void ensureConnections();
    } catch (e) {
      if (alive) { error = String(e instanceof Error ? e.message : e); loaded = true; }
    }
  }
  // The connection list feeds the picker and a group-column fallback; read once, when an
  // editor is on screen. A failed read leaves the picker with no connections.
  async function ensureConnections() {
    if (connections !== null) return;
    connections = [];
    try { const answer = await listStewardConnections(); if (alive) connections = answer.connections ?? []; }
    catch { /* nothing to pick from */ }
  }
  onMount(() => {
    alive = true;
    void load();
    return () => { alive = false; };
  });
  function route(row: StewardCapability): string {
    if (row.provider === 'native') return t('steward.capNative') + (row.detail ? ' — ' + row.detail : '');
    if (row.provider === 'mcp') return t('steward.capMcp').replace('{conn}', row.conn_id ?? '?').replace('{tool}', row.tool ?? '?');
    return t('steward.capMissing');
  }
  // The name comes from 'steward.cap.<id>'; the backend's label is only the fallback for
  // an id this build has no key for (t() returns the key itself then).
  function name(row: StewardCapability): string {
    const key = 'steward.cap.' + row.id;
    const text = t(key);
    return text === key ? row.label : text;
  }
  // The 24.4 columns appear with the backend that answers them (an older one sends none).
  const routed = $derived(rows.some(row => row.status !== undefined || row.setting !== undefined));
  // The page-top line: the lines of the steward's status that concern its capabilities -
  // the vision route flag (integrations/dsh-driver/model.mjs STEWARD_ROUTE_FLAG) and the
  // host self-check slot ("basics: ..."). Nothing else of the status is shown here.
  const STATUS_PREFIXES = ['basics:', '管家路线不收图'];
  const statusLines = $derived((status?.projects?.[stewardProjectId(status)]?.managed?.last_error ?? '')
    .split('\n').map(line => line.trim()).filter(line => STATUS_PREFIXES.some(prefix => line.startsWith(prefix))));

  function entryText(entry: StewardRouteEntry | string): string {
    if (typeof entry === 'string') return entry;
    if (entry.kind === 'native') return t('steward.capEntryNative');
    if (entry.kind === 'missing') return t('steward.capEntryMissing');
    if (entry.kind === 'host') return entry.tool;
    return entry.conn_id + ' / ' + entry.raw_name;
  }
  function reasonText(code: string): string {
    const key = 'steward.capReason.' + code;
    return t(key) === key ? code : t(key);
  }
  /** What "use this" puts in the editor from detection: native, or the one detected tool. */
  function detected(row: StewardCapability): StewardRouteEntry[] | null {
    if (row.provider === 'native') return [{kind: 'native'}];
    if (row.provider === 'mcp' && row.conn_id && row.tool) return [{kind: 'conn', conn_id: row.conn_id, raw_name: row.tool}];
    return null;
  }
  /** The saved route as editor entries (a read entry carries its own status `reason`). */
  const saved = (row: StewardCapability) => (row.setting?.route ?? []).map(entry => ({...entry}));
  const shown = (row: StewardCapability) => drafts[row.id]?.entries ?? (row.setting ? saved(row) : null);
  /** The row version a write expects, as read (an unset row keeps its own; 0: never written). */
  const readVersion = (row: StewardCapability) => row.version ?? row.setting?.version ?? 0;
  const base = (row: StewardCapability) => drafts[row.id]?.base ?? readVersion(row);
  function edit(row: StewardCapability, entries: StewardRouteEntry[]) {
    drafts = {...drafts, [row.id]: {entries: entries.map(entry => ({...entry})), base: base(row)}};
    rowErrors = {...rowErrors, [row.id]: ''};
    void ensureConnections();
  }
  function change(row: StewardCapability, mutate: (list: StewardRouteEntry[]) => void) {
    const list = [...(shown(row) ?? [])];
    mutate(list);
    drafts = {...drafts, [row.id]: {entries: list, base: base(row)}};
  }
  function forget(capId: string) {
    const {[capId]: _dropped, ...rest} = drafts;
    drafts = rest;
    if (picker?.cap === capId) picker = null;
  }
  function move(row: StewardCapability, from: number, to: number) {
    if (from === to) return;
    change(row, list => { const [entry] = list.splice(from, 1); list.splice(to, 0, entry); });
  }
  function remove(row: StewardCapability, index: number) { change(row, list => { list.splice(index, 1); }); }
  function add(row: StewardCapability, entry: StewardRouteEntry) { change(row, list => { list.push(entry); }); picker = null; }

  // The picker offers only the kinds this capability allows; a connection entry may be any
  // real tool of the chosen connection (any name), and a private-only row lists only private
  // connections.
  const allows = (row: StewardCapability, kind: string) => (row.spec?.allows ?? []).includes(kind as never);
  const pickable = (row: StewardCapability) => (connections ?? [])
    .filter(c => allows(row, 'conn') && (!row.spec?.private_only || c.visibility === 'private'));
  async function pick(row: StewardCapability, value: string) {
    if (value === 'native' || value === 'missing') add(row, {kind: value});
    else if (value.startsWith('host:')) add(row, {kind: 'host', tool: value.slice(5)});
    else if (value.startsWith('conn:')) {
      const conn = value.slice(5);
      picker = {cap: row.id, conn, tools: null, error: ''};
      try {
        const answer = await stewardConnectionTools(conn);
        if (picker?.cap === row.id && picker.conn === conn) picker = {...picker, tools: answer.tools};
      } catch (e) {
        if (picker?.cap === row.id && picker.conn === conn) picker = {...picker, tools: [], error: String(e instanceof Error ? e.message : e)};
      }
    }
  }
  // The read's own answer wins, null included: a pin whose connection is gone (or was added
  // again under the same conn_id, another conn_uid) reaches no group. The connection list
  // answers only an entry the read did not describe (a draft).
  const visibility = (entry: StewardRouteEntry) => entry.kind !== 'conn' ? undefined
    : entry.visibility !== undefined ? entry.visibility : connections?.find(c => c.conn_id === entry.conn_id)?.visibility;
  function groupText(entry: StewardRouteEntry): string {
    if (entry.kind === 'native' || entry.kind === 'host') return t('steward.capGroupSeeSteward');
    if (entry.kind === 'conn') {
      const seen = visibility(entry);
      if (seen === 'public') return t('steward.capGroupYes');
      if (seen === 'private') return t('steward.capGroupPrivate');
    }
    return '—';
  }

  /** The refusal's closed code: the body's code, else the code a detail string starts with. */
  function codeOf(e: unknown): string {
    if (e instanceof ApiError) {
      if (e.code) return e.code;
      const detail = e.detail as {code?: unknown} | string | null;
      if (detail && typeof detail === 'object' && typeof detail.code === 'string') return detail.code;
      const match = /^[a-z][a-z0-9_]*/.exec(typeof detail === 'string' ? detail : e.message);
      if (match) return match[0];
    }
    return String(e instanceof Error ? e.message : e);
  }
  function refusal(code: string): string {
    const key = 'steward.capError.' + code;
    return t(key) === key ? t('steward.capSaveFailed').replace('{code}', code) : t(key);
  }
  async function failed(row: StewardCapability, e: unknown) {
    const code = codeOf(e);
    let text = refusal(code);
    if (code === 'version_conflict') {
      // Someone else wrote this row: drop the draft and show the row as it is now.
      text += ' ' + t('steward.capConflict');
      forget(row.id);
      await load();
    }
    rowErrors = {...rowErrors, [row.id]: text};
  }
  async function save(row: StewardCapability) {
    const draft = drafts[row.id];
    if (!draft || busy) return;
    busy = row.id; rowErrors = {...rowErrors, [row.id]: ''};
    try {
      await setStewardCapability(row.id, draft.base, draft.entries);
      forget(row.id);
      await load();
    } catch (e) { await failed(row, e); }
    finally { busy = ''; }
  }
  async function unset(row: StewardCapability) {
    if (busy) return;
    if (!row.setting) { forget(row.id); return; }
    busy = row.id; rowErrors = {...rowErrors, [row.id]: ''};
    try {
      await unsetStewardCapability(row.id, row.setting.version);
      forget(row.id);
      await load();
    } catch (e) { await failed(row, e); }
    finally { busy = ''; }
  }
  function cancel(row: StewardCapability) { forget(row.id); rowErrors = {...rowErrors, [row.id]: ''}; }
  // The steward's proposal on an unset row: "use this" / "reject" are the proposal's own
  // approve / decline, at the version shown.
  async function decide(row: StewardCapability, proposal: StewardCapabilityProposal, action: 'approve' | 'decline') {
    if (busy) return;
    busy = row.id; rowErrors = {...rowErrors, [row.id]: ''};
    try {
      const answer = action === 'approve' ? await approveStewardProposal(proposal.proposal_id, proposal.version)
        : await declineStewardProposal(proposal.proposal_id, proposal.version);
      if (answer?.status === 'failed') {
        const result = answer.result as {error?: unknown; code?: unknown} | string | null;
        const code = typeof result === 'string' ? result : String(result?.error ?? result?.code ?? 'failed');
        rowErrors = {...rowErrors, [row.id]: refusal(code)};
      }
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 409)) rowErrors = {...rowErrors, [row.id]: refusal(codeOf(e))};
    } finally { busy = ''; }
    await load();
  }
  async function clearAll() {
    clearError = '';
    try { await clearStewardCapabilities(); drafts = {}; picker = null; }
    catch (e) { clearError = refusal(codeOf(e)); return; }
    await load();
  }
  function dropOn(row: StewardCapability, index: number, event: DragEvent) {
    event.preventDefault();
    if (dragging?.cap === row.id) move(row, dragging.index, index);
    dragging = null;
  }
</script>

<section class="steward-capabilities" aria-label={t('steward.capAria')}>
  {#if statusLines.length}<p class="status-line" data-capabilities-status>{t('steward.capStatusLine')} {statusLines.join(' · ')}</p>{/if}
  <table>
    <thead><tr><th scope="col">{t('steward.capColName')}</th><th scope="col">{t('steward.capColRoute')}</th>
      {#if routed}<th scope="col">{t('steward.capColGroup')}</th><th scope="col">{t('steward.capColStatus')}</th>{/if}</tr></thead>
    <tbody>
      {#each rows as row (row.id)}
        {@const list = shown(row)}
        <tr data-capability={row.id} data-provider={row.provider} data-set={row.setting ? '1' : '0'}>
          <td data-capability-label>{name(row)}</td>
          <td class="route-cell">
            {#if list}
              <div class="editor" data-capability-editor={row.id}>
                <ol class="chips">
                  {#each list as entry, i (i)}
                    <li class="chip" draggable="true" data-entry={i} data-entry-kind={entry.kind}
                      ondragstart={(event) => { dragging = {cap: row.id, index: i}; event.dataTransfer?.setData('text/plain', String(i)); }}
                      ondragover={(event) => event.preventDefault()} ondrop={(event) => dropOn(row, i, event)}>
                      <span class="handle" aria-hidden="true">⋮⋮</span>
                      {#if entry.kind === 'conn' && onconnect}
                        <button type="button" class="link" data-entry-conn={entry.conn_id} onclick={() => onconnect(entry.conn_id)}>{entry.conn_id}</button> / {entry.raw_name}
                      {:else}{entryText(entry)}{/if}
                      {#if entry.kind === 'conn' && entry.reason}<small class="reason reason-{entry.reason}" data-entry-reason={entry.reason}>{reasonText(entry.reason)}</small>{/if}
                      <button type="button" class="remove" data-entry-remove onclick={() => remove(row, i)}>×</button>
                    </li>
                  {/each}
                </ol>
                {#if row.spec}
                  <select data-cap-add value="" onchange={(event) => { const value = event.currentTarget.value; event.currentTarget.value = ''; void pick(row, value); }}>
                    <option value="">{t('steward.capAddEntry')}</option>
                    {#if allows(row, 'native')}<option value="native">{t('steward.capEntryNative')}</option>{/if}
                    {#if allows(row, 'missing')}<option value="missing">{t('steward.capEntryMissing')}</option>{/if}
                    {#if allows(row, 'host')}{#each row.spec.host_tools as tool (tool)}<option value={'host:' + tool}>{tool}</option>{/each}{/if}
                    {#each pickable(row) as c (c.conn_id)}<option value={'conn:' + c.conn_id}>{c.label} ({c.conn_id})</option>{/each}
                  </select>
                  {#if picker?.cap === row.id}
                    {@const conn = picker.conn}
                    <div class="tools" data-cap-picker={conn}>
                      {#if picker.tools === null}<span aria-busy="true">{t('steward.loading')}</span>{/if}
                      {#if picker.error}<span class="error">{picker.error}</span>{/if}
                      {#each picker.tools ?? [] as tool (tool.raw_name)}
                        <button type="button" class="outline" data-cap-tool={tool.raw_name} title={tool.description}
                          onclick={() => add(row, {kind: 'conn', conn_id: conn, raw_name: tool.raw_name,
                            visibility: connections?.find(c => c.conn_id === conn)?.visibility})}>{tool.raw_name}</button>
                      {/each}
                    </div>
                  {/if}
                  {#if allows(row, 'conn')}<p class="hint">{t('steward.capNotAllowedHint')}</p>{/if}
                {/if}
                <div class="actions">
                  {#if drafts[row.id]}
                    <button type="button" data-cap-save disabled={busy !== '' || !list.length} onclick={() => void save(row)}>{t('steward.capSave')}</button>
                    <button type="button" class="outline" data-cap-cancel disabled={busy !== ''} onclick={() => cancel(row)}>{t('steward.capCancel')}</button>
                  {/if}
                  {#if row.setting}
                    <button type="button" class="outline secondary" data-cap-unset disabled={busy !== ''} onclick={() => void unset(row)}>{t('steward.capUnset')}</button>
                  {/if}
                </div>
              </div>
              {#if row.setting && row.status?.dot === 'red' && !drafts[row.id]}
                <!-- No entry of the saved route resolves any more (c4): detection's grey suggestion
                     comes back beside it; "use this" only fills the editor, the route never moves. -->
                {@const found = detected(row)}
                {#if found}
                  <div class="suggestion" data-capability-suggestion="detected">
                    <span>{t('steward.capSuggestedDetected')}</span>
                    <span data-capability-route>{route(row)}</span>
                    <button type="button" class="outline" data-cap-adopt onclick={() => edit(row, found)}>{t('steward.capAdopt')}</button>
                  </div>
                {/if}
              {/if}
            {:else if row.proposal}
              {@const proposal = row.proposal}
              <div class="suggestion" data-capability-suggestion="proposal">
                <span>{t('steward.capSuggestedProposal')}</span>
                <span data-capability-route>{proposal.route.map(entryText).join(' → ')}</span>
                <button type="button" class="outline" data-cap-adopt-proposal disabled={busy !== ''}
                  onclick={() => void decide(row, proposal, 'approve')}>{t('steward.capAdopt')}</button>
                <button type="button" class="outline secondary" data-cap-reject-proposal disabled={busy !== ''}
                  onclick={() => void decide(row, proposal, 'decline')}>{t('steward.capReject')}</button>
              </div>
            {:else}
              {@const found = detected(row)}
              <div class="suggestion" data-capability-suggestion="detected">
                {#if found && routed}<span>{t('steward.capSuggestedDetected')}</span>{/if}
                <span data-capability-route>{route(row)}</span>
                {#if found && row.spec}<button type="button" class="outline" data-cap-adopt onclick={() => edit(row, found)}>{t('steward.capAdopt')}</button>{/if}
                {#if !found && routed}<small data-cap-not-set>{t('steward.capNotSet')}</small>{/if}
              </div>
              {#if row.spec}<button type="button" class="outline" data-cap-set onclick={() => edit(row, [])}>{t('steward.capSet')}</button>{/if}
            {/if}
            {#if rowErrors[row.id]}<p class="error" role="alert" data-cap-error>{rowErrors[row.id]}</p>{/if}
          </td>
          {#if routed}
            <td data-capability-group>
              {#each row.setting?.route ?? [] as entry, i (i)}<div>{groupText(entry)}</div>{/each}
            </td>
            <td data-capability-status>
              {#if row.status}
                <span class="dot dot-{row.status.dot}" data-status-dot={row.status.dot} aria-hidden="true"></span>
                {row.status.reasons.map(reasonText).join('; ')}
              {/if}
            </td>
          {/if}
        </tr>
      {/each}
    </tbody>
  </table>
  {#if !loaded && !error}<p aria-busy="true">{t('steward.loading')}</p>{/if}
  {#if loaded && !error && rows.length === 0}<p data-capabilities-empty>{t('steward.capEmpty')}</p>{/if}
  {#if error}<p class="error" role="alert">{t('steward.capLoadFailed').replace('{code}', error)}</p>{/if}
  {#if rows.some(row => row.setting)}
    <button type="button" class="outline secondary clear" data-cap-clear onclick={() => { clearAsk = true; }}>{t('steward.capClearAll')}</button>
  {/if}
  {#if clearError}<p class="error" role="alert" data-cap-clear-error>{clearError}</p>{/if}
</section>
<ConfirmDialog bind:show={clearAsk} variant="danger" title={t('steward.capClearAll')}
  message={t('steward.capClearAllConfirm')} confirmLabel={t('steward.capClearAll')} onConfirm={clearAll} />

<style>
  .steward-capabilities{font-size:13px;}
  table{width:100%;border-collapse:collapse;}
  th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--pico-muted-border-color);vertical-align:top;}
  th{color:var(--pico-muted-color);font-size:11px;text-transform:uppercase;}
  .route-cell{overflow-wrap:anywhere;}
  .suggestion{color:var(--pico-muted-color);}
  .chips{display:flex;flex-wrap:wrap;gap:4px;margin:0 0 4px;padding:0;list-style:none;}
  .chip{list-style:none;margin:0;padding:1px 6px;border:1px solid var(--pico-muted-border-color);border-radius:5px;cursor:grab;}
  .handle{color:var(--pico-muted-color);margin-right:2px;}
  .reason{margin-left:4px;color:var(--pico-muted-color);}
  .reason-tool_gone,.reason-tool_not_allowed,.reason-tool_changed,.reason-tool_collision,.reason-connection_off{color:var(--pico-del-color);}
  button{width:auto;margin:2px 4px 2px 0;padding:2px 8px;font-size:12px;}
  button.remove,button.link{border:0;background:none;padding:0 2px;margin:0;color:inherit;}
  button.link{text-decoration:underline;}
  select{width:auto;margin:2px 0;padding:2px 6px;font-size:12px;height:auto;}
  .tools{display:flex;flex-wrap:wrap;gap:2px;}
  .hint{color:var(--pico-muted-color);font-size:11px;margin:2px 0;}
  .actions{display:flex;flex-wrap:wrap;}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;background:var(--pico-muted-border-color);}
  .dot-green{background:#2e9d4f;}.dot-yellow{background:#d6a019;}.dot-red{background:var(--pico-del-color);}
  .status-line{color:var(--pico-del-color);margin:0 0 6px;}
  .clear{margin-top:8px;}
  .error{color:var(--pico-del-color);margin:2px 0;}
</style>
