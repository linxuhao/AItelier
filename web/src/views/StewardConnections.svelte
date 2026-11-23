<script lang="ts">
  import { onMount } from 'svelte';
  import { confirmStewardIdentity, deleteStewardConnection, listStewardConfirmations, listStewardConnections,
    probeStewardConnection, saveStewardConnection, setStewardConnectionSecret, setStewardConnectionTools,
    setStewardOAuthClient, setStewardOAuthScope, signOutStewardOAuth, startDshDriver, startStewardOAuth, stewardConnectionTools, stopDshDriver,
    type StewardConfirmation, type StewardConnection, type StewardConnectionInput, type StewardConnectionTool,
    type StewardConnections, type StewardToolChange } from '../lib/api';
  import { STEWARD_PROJECT_ID } from '../lib/steward';
  import { t } from '../lib/i18n.svelte';
  import ConfirmDialog from './ConfirmDialog.svelte';
  // Step 24.1c: a connection to open for its secret / sign-in (a proposal's "set the secret").
  const { focus = null }: {focus?: string | null} = $props();
  let focused = '';
  // The steward's own MCP connections (design 7.8.10): add / edit / delete a server, read
  // its tools, tick each one (allowed, outward, egress, blocked arguments, daily cap).
  // Saving takes effect at once - the gateway tells the steward its tool list changed, no
  // restart. Descriptions and server texts come from someone else's server: plain text only.
  let data = $state<StewardConnections | null>(null);
  let error = $state(''), notice = $state(''), busy = $state('');
  let editing = $state<(StewardConnectionInput & {isNew: boolean; secret: string; clientId: string; clientSecret: string;
    scope: string; scopeWas: string}) | null>(null);
  // Step 24.1b: the confirmations of outward actions, with the text that went out.
  let confirmations = $state<StewardConfirmation[]>([]);
  let open = $state<string | null>(null);
  let tools = $state<StewardConnectionTool[]>([]), toolsError = $state(''), instructions = $state<string | null>(null);
  // `cap` is bound to a number input: Svelte stores a number, or null when it is cleared.
  let drafts = $state<Record<string, StewardToolChange & {blocked: string; cap: string | number | null}>>({});
  let deleteAsk = $state(false), target = $state<string | null>(null);
  function failed(e: unknown) { error = String(e instanceof Error ? e.message : e); }
  async function load() {
    try { data = await listStewardConnections(); error = ''; }
    catch (e) { failed(e); }
    const target = data?.connections.find(c => c.conn_id === focus);
    if (target && focused !== target.conn_id) { focused = target.conn_id; edit(target); }
    try { confirmations = (await listStewardConfirmations()).confirmations; } catch { confirmations = []; }
  }
  function blank() {
    editing = {isNew: true, conn_id: '', label: '', url: '', auth_kind: 'none', header_name: 'Authorization',
      visibility: 'private', enabled: false, secret: '', clientId: '', clientSecret: '', scope: '', scopeWas: ''};
  }
  function edit(c: StewardConnection) {
    editing = {isNew: false, conn_id: c.conn_id, label: c.label, url: c.url, auth_kind: c.auth_kind,
      header_name: c.header_name ?? 'Authorization', visibility: c.visibility, enabled: c.enabled, secret: '',
      clientId: '', clientSecret: '', scope: c.oauth_scope ?? '', scopeWas: c.oauth_scope ?? ''};
  }
  async function save() {
    if (!editing || busy) return;
    busy = 'save'; error = ''; notice = '';
    const {isNew: _isNew, secret, clientId, clientSecret, scope, scopeWas, ...input} = editing;
    try {
      await saveStewardConnection({...input, header_name: input.auth_kind === 'header' ? input.header_name : null});
      if (input.auth_kind === 'header' && secret) await setStewardConnectionSecret(input.conn_id, secret);
      if (input.auth_kind === 'oauth' && clientId) await setStewardOAuthClient(input.conn_id, clientId, clientSecret || null);
      if (input.auth_kind === 'oauth' && scope.trim() !== scopeWas) await setStewardOAuthScope(input.conn_id, scope.trim() || null);
      editing = null; notice = t('steward.connSaved');
      await load();
    } catch (e) { failed(e); }
    finally { busy = ''; }
  }
  function draftsOf(rows: StewardConnectionTool[]) {
    drafts = Object.fromEntries(rows.map(r => [r.raw_name, {raw_name: r.raw_name, allowed: r.allowed && r.state !== 'changed',
      outward: r.outward, egress: r.egress, blocked: r.blocked_args.join(', '), cap: r.daily_cap === null ? '' : String(r.daily_cap)}]));
  }
  async function showTools(connId: string, probe = false) {
    open = connId; toolsError = ''; busy = probe ? 'probe' : busy;
    try {
      const answer = probe ? await probeStewardConnection(connId) : await stewardConnectionTools(connId);
      if (answer.error) toolsError = t('steward.connProbeFailed').replace('{code}', answer.error);
      const listed = answer.error ? await stewardConnectionTools(connId) : answer;
      tools = listed.tools; instructions = listed.instructions; draftsOf(listed.tools);
      if (probe) await load();
    } catch (e) { toolsError = String(e instanceof Error ? e.message : e); }
    finally { if (probe) busy = ''; }
  }
  async function saveTools() {
    if (!open || busy) return;
    busy = 'tools'; toolsError = ''; notice = '';
    try {
      // Each tool names the definition it was shown: the server refuses to allow one that
      // changed since (definition_changed), so a save never approves a description unseen.
      const changes: StewardToolChange[] = tools.map(r => {
        const d = drafts[r.raw_name];
        const cap = d.cap === null || d.cap === undefined || String(d.cap).trim() === '' ? null : Number(d.cap);
        return {raw_name: r.raw_name, allowed: d.allowed, outward: d.outward, egress: d.egress,
          blocked_args: d.blocked.split(',').map(s => s.trim()).filter(Boolean), daily_cap: cap,
          schema_sha256: r.schema_sha256};
      });
      const answer = await setStewardConnectionTools(open, changes);
      tools = answer.tools; draftsOf(answer.tools); notice = t('steward.connSaved');
      await load();
    } catch (e) { toolsError = String(e instanceof Error ? e.message : e); }
    finally { busy = ''; }
  }
  // OAuth (7.8.6): the browser signs in at the service in a new tab and comes back to the
  // callback; the account it bound is shown and must be confirmed before enabling.
  // The tab opens in the click itself (a popup blocker allows only that); the address
  // follows once the backend answers it. A blocked tab leaves the address as a link.
  let authorizeLink = $state<{conn: string; url: string} | null>(null);
  async function authorize(connId: string) {
    if (busy) return;
    busy = 'oauth'; error = ''; authorizeLink = null;
    const tab = window.open('about:blank', '_blank');
    try {
      const {authorize_url} = await startStewardOAuth(connId);
      if (tab) { tab.opener = null; tab.location.href = authorize_url; }
      else authorizeLink = {conn: connId, url: authorize_url};
    }
    catch (e) { tab?.close(); failed(e); }
    finally { busy = ''; }
  }
  async function confirmIdentity(c: StewardConnection) {
    if (busy || !c.auth_identity) return;
    busy = 'identity'; error = '';
    try { await confirmStewardIdentity(c.conn_id, c.auth_identity); await load(); }
    catch (e) { failed(e); }
    finally { busy = ''; }
  }
  async function signOut(connId: string) {
    if (busy) return;
    busy = 'signout'; error = '';
    try { await signOutStewardOAuth(connId); await load(); }
    catch (e) { failed(e); }
    finally { busy = ''; }
  }
  function askDelete(connId: string) { target = connId; deleteAsk = true; }
  async function remove() {
    if (!target) return;
    try { await deleteStewardConnection(target); if (open === target) open = null; await load(); }
    catch (e) { failed(e); }
  }
  async function restartSteward() {
    if (busy) return;
    busy = 'restart'; error = '';
    try { await stopDshDriver(STEWARD_PROJECT_ID); await startDshDriver(STEWARD_PROJECT_ID); notice = t('steward.connRestarted'); await load(); }
    catch (e) { failed(e); }
    finally { busy = ''; }
  }
  const authLabel = (c: StewardConnection) => c.auth_kind === 'none' ? t('steward.connAuthNone')
    : c.secret === 'set' ? t('steward.connSecretSet') : c.secret === 'unset' ? t('steward.connSecretUnset') : t('steward.connReauth');
  const source = (c: StewardConnection) => c.last_change === 'dashboard' ? t('steward.connFromDashboard') : c.last_change;
  onMount(() => { void load(); });
</script>
<section class="steward-connections" aria-label={t('steward.tabConnections')}>
  <p class="intro">{t('steward.connIntro')}</p>
  {#if notice}<p class="notice" role="status">{notice}</p>{/if}
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if data?.restart.due.length}
    <div class="pending" data-restart-pending role="status">
      <span>{t('steward.connRestartPending')}</span>
      <button type="button" class="outline" disabled={busy !== ''} onclick={() => void restartSteward()}>{t('steward.connRestart')}</button>
    </div>
  {/if}
  {#each data?.restart.done.filter(d => d.error) ?? [] as failure (failure.id)}
    <p class="error" data-restart-failed>{t('steward.connRestartFailed').replace('{code}', failure.error ?? '')}</p>
  {/each}
  {#if data}
    {#each data.connections as c (c.conn_id)}
      <article class="connection" data-conn={c.conn_id} data-usable={c.usable ? '1' : '0'} data-focus={c.conn_id === focus ? '1' : undefined}>
        <header>
          <strong>{c.label}</strong> <code>{c.conn_id}</code>
          <span class="mark" class:off={c.visibility === 'private'}>{c.visibility === 'private' ? t('steward.connPrivate') : t('steward.connPublic')}</span>
          <span class="mark" class:off={!c.enabled}>{c.enabled ? t('steward.connEnabled') : t('steward.connDisabled')}</span>
        </header>
        <p class="detail"><code>{c.url}</code> · {authLabel(c)} · {t('steward.connSource')}: {source(c)}</p>
        <p class="detail" data-conn-counts>{t('steward.connCounts').replace('{allowed}', String(c.tool_counts.allowed))
          .replace('{outward}', String(c.tool_counts.outward)).replace('{all}', String(c.tool_counts.all))}
          {#if c.tool_counts.changed}<span class="changed"> · {t('steward.connChangedCount').replace('{n}', String(c.tool_counts.changed))}</span>{/if}
          {#if c.tool_counts.new}<span> · {t('steward.connNewCount').replace('{n}', String(c.tool_counts.new))}</span>{/if}</p>
        {#if c.auth_kind === 'oauth'}
          <p class="detail" data-conn-oauth>
            {#if c.auth_identity}<span data-conn-identity>{t('steward.connAuthorizedAs').replace('{who}', c.auth_identity)}</span>{/if}
            {#if c.auth_identity && !c.identity_confirmed}
              <span class="changed"> · {t('steward.connIdentityUnconfirmed')}</span>
              <button type="button" class="outline inline" disabled={busy !== ''} data-conn-confirm-identity
                onclick={() => void confirmIdentity(c)}>{t('steward.connConfirmIdentity')}</button>
            {/if}
            <button type="button" class="outline inline" disabled={busy !== ''} data-conn-authorize
              onclick={() => void authorize(c.conn_id)}>{t('steward.connAuthorize')}</button>
            {#if authorizeLink?.conn === c.conn_id}<a href={authorizeLink.url} target="_blank" rel="noopener noreferrer"
              data-conn-authorize-link>{t('steward.connAuthorize')} ↗</a>{/if}
            {#if c.auth_identity || c.secret === 'set'}<button type="button" class="outline inline" disabled={busy !== ''}
              data-conn-signout onclick={() => void signOut(c.conn_id)}>{t('steward.connSignOut')}</button>{/if}
          </p>
        {/if}
        {#if c.last_probe_at || c.last_error}
          <p class="detail">{c.last_probe_at ? t('steward.connProbedAt').replace('{at}', new Date(c.last_probe_at).toLocaleString()) : ''}
            {#if c.last_error}<span class="error"> {c.last_error}</span>{/if}</p>
        {/if}
        <div class="actions">
          <button type="button" class="outline" onclick={() => edit(c)}>{t('steward.connEdit')}</button>
          <button type="button" class="outline" disabled={busy !== ''} data-conn-probe onclick={() => void showTools(c.conn_id, true)}>{t('steward.connProbe')}</button>
          <button type="button" class="outline" onclick={() => void showTools(c.conn_id)}>{t('steward.connTools')}</button>
          <button type="button" class="outline danger" onclick={() => askDelete(c.conn_id)}>{t('steward.connDelete')}</button>
        </div>
        {#if open === c.conn_id}
          <div class="tools" data-conn-tools>
            {#if toolsError}<p class="error" role="alert">{toolsError}</p>{/if}
            {#if instructions}<details><summary>{t('steward.connInstructions')}</summary><pre class="plain">{instructions}</pre></details>{/if}
            {#each tools as tool (tool.raw_name)}
              {@const d = drafts[tool.raw_name]}
              <div class="tool" class:changed={tool.state === 'changed'} data-tool={tool.raw_name} data-state={tool.state}>
                <div><code>{tool.raw_name}</code>
                  {#if tool.state === 'changed'}<strong class="changed"> {t('steward.connToolChanged')}</strong>{/if}
                  {#if tool.state === 'collision'}<strong class="changed"> {t('steward.connToolCollision')}</strong>{/if}</div>
                <p class="plain">{tool.description}</p>
                {#if tool.annotations}<p class="plain annotations">{JSON.stringify(tool.annotations)}</p>{/if}
                {#if d}
                  <div class="ticks">
                    <label><input type="checkbox" bind:checked={drafts[tool.raw_name].allowed} data-tick="allowed" disabled={tool.state === 'collision'} /> {t('steward.connAllowed')}</label>
                    <label><input type="checkbox" bind:checked={drafts[tool.raw_name].outward} data-tick="outward" /> {t('steward.connOutward')}</label>
                    <label><input type="checkbox" bind:checked={drafts[tool.raw_name].egress} data-tick="egress" /> {t('steward.connEgress')}</label>
                    <label>{t('steward.connBlocked')} <input type="text" bind:value={drafts[tool.raw_name].blocked} data-tick="blocked" /></label>
                    <label>{t('steward.connCap')} <input type="number" min="1" bind:value={drafts[tool.raw_name].cap} data-tick="cap" /></label>
                  </div>
                {/if}
              </div>
            {/each}
            {#if tools.length}<p class="hint">{t('steward.connOutwardHint')}</p>
              <button type="button" disabled={busy !== ''} data-conn-save-tools onclick={() => void saveTools()}>{t('steward.connSave')}</button>{/if}
          </div>
        {/if}
      </article>
    {/each}
    <button type="button" class="outline" data-conn-add onclick={blank}>{t('steward.connAdd')}</button>
    <details class="confirmations" data-confirmations>
      <summary>{t('steward.connConfirmations')}</summary>
      {#each confirmations as item (item.code)}
        <details class="confirmation" data-confirmation={item.code}>
          <summary><code>{item.code}</code> · {item.conn_id} · {item.raw_name} · {item.channel} · {item.status}
            · {new Date(item.created_at).toLocaleString()}</summary>
          <pre class="plain">{item.message_text}</pre>
        </details>
      {:else}<p class="hint">{t('steward.connConfirmationsEmpty')}</p>{/each}
    </details>
  {/if}
  {#if editing}
    <form class="edit" data-conn-form onsubmit={(event) => { event.preventDefault(); void save(); }}>
      <label>{t('steward.connId')} <input type="text" bind:value={editing.conn_id} readonly={!editing.isNew} required /></label>
      <label>{t('steward.connLabel')} <input type="text" bind:value={editing.label} maxlength="40" required /></label>
      <label>{t('steward.connUrl')} <input type="text" bind:value={editing.url} required /></label>
      <label>{t('steward.connAuth')}
        <select bind:value={editing.auth_kind}>
          <option value="none">{t('steward.connAuthNone')}</option>
          <option value="header">{t('steward.connAuthHeader')}</option>
          <option value="oauth">{t('steward.connAuthOauth')}</option>
        </select></label>
      {#if editing.auth_kind === 'header'}
        <label>{t('steward.connHeaderName')} <input type="text" bind:value={editing.header_name} /></label>
        <label>{t('steward.connHeaderValue')} <input type="password" autocomplete="off" bind:value={editing.secret} /></label>
      {/if}
      {#if editing.auth_kind === 'oauth'}
        <label>{t('steward.connClientId')} <input type="text" autocomplete="off" bind:value={editing.clientId} data-oauth-client-id /></label>
        <label>{t('steward.connClientSecret')} <input type="password" autocomplete="off" bind:value={editing.clientSecret} /></label>
        <label>{t('steward.connOAuthScope')} <input type="text" autocomplete="off" bind:value={editing.scope} data-oauth-scope /></label>
      {/if}
      <label>{t('steward.connVisibility')}
        <select bind:value={editing.visibility}>
          <option value="private">{t('steward.connPrivate')}</option>
          <option value="public">{t('steward.connPublic')}</option>
        </select></label>
      <p class="hint">{t('steward.connVisibilityHelp')}</p>
      <label><input type="checkbox" bind:checked={editing.enabled} /> {t('steward.connEnabled')}</label>
      <div class="actions">
        <button type="submit" disabled={busy !== ''}>{t('steward.connSave')}</button>
        <button type="button" class="outline" onclick={() => { editing = null; }}>{t('steward.connCancel')}</button>
      </div>
    </form>
  {/if}
</section>
<ConfirmDialog bind:show={deleteAsk} variant="danger" title={t('steward.connDelete')}
  message={t('steward.connDeleteConfirm').replace('{id}', target ?? '')} confirmLabel={t('steward.connDelete')} onConfirm={remove} />
<style>
  .steward-connections{font-size:13px;}
  .intro,.hint{color:var(--pico-muted-color);}
  .connection{margin:0 0 10px;padding:12px 14px;border:1px solid var(--pico-muted-border-color);border-radius:8px;}
  header{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 6px;padding:0;background:none;border:0;}
  .mark{font-size:11px;padding:1px 6px;border-radius:5px;background:var(--pico-primary);color:var(--pico-primary-inverse);}
  .mark.off{background:var(--pico-muted-border-color);color:var(--pico-color);}
  .detail{color:var(--pico-muted-color);overflow-wrap:anywhere;margin:0 0 6px;}
  .actions{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 0;}.actions button{width:auto;margin:0;padding:4px 10px;font-size:12px;}
  .danger{color:var(--pico-del-color);border-color:var(--pico-del-color);}
  .pending{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 10px;border-radius:6px;background:var(--pico-code-background-color);margin:0 0 10px;}
  .pending button{width:auto;margin:0;padding:4px 10px;font-size:12px;}
  .tools{margin:10px 0 0;}
  .tool{padding:8px 0;border-top:1px solid var(--pico-muted-border-color);}
  .tool.changed{border-left:3px solid var(--pico-del-color);padding-left:8px;}
  .changed{color:var(--pico-del-color);}
  .plain{white-space:pre-wrap;overflow-wrap:anywhere;margin:2px 0;}
  .annotations{font-family:monospace;font-size:11px;color:var(--pico-muted-color);}
  .ticks{display:flex;gap:12px;flex-wrap:wrap;align-items:center;}.ticks label{margin:0;font-size:12px;}
  .ticks input[type=text],.ticks input[type=number]{width:120px;margin:0;padding:2px 6px;font-size:12px;height:auto;}
  .edit{margin:12px 0 0;padding:12px 14px;border:1px solid var(--pico-muted-border-color);border-radius:8px;}
  .notice{color:var(--pico-primary);}.error{color:var(--pico-del-color);}
  button.inline{width:auto;margin:0 0 0 6px;padding:2px 8px;font-size:12px;}
  .confirmations{margin:12px 0 0;}.confirmation summary{font-size:12px;}
</style>
