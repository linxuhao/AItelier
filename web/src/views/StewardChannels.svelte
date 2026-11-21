<script lang="ts">
  import { onMount } from 'svelte';
  import { createOwnerChannelCode, listOwnerChannels, requestOwnerChannelQr, setDefaultOwnerChannel, unbindOwnerChannel,
    type OwnerChannel, type OwnerChannelLoginRequest, type OwnerChannelName } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  import { channelAlertLine, channelAlertText, type ChannelClock } from '../lib/channelAlert';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import StewardGroups from './StewardGroups.svelte';
  // The owner's chat channels (design 7.2): who is bound on Slack / WeChat, the
  // one-time bind code, unbind, and where the steward's unprompted messages go.
  // Binding happens off this page (the owner sends the code to the bot), so while
  // a code is live the list is re-read on a short cadence to show the result; the
  // same while a login QR is shown (it is replaced, or scanned, off this page too).
  // While any channel is not connected (or has reported nothing yet) and the tab is
  // visible, the list is also re-read slowly: a WeChat login QR can first appear
  // after this page loaded (the host reports it up to about a minute late), and the
  // owner must see it without reloading.
  // The WeChat card asks for a new login QR on demand ("新二维码", design 7.2): the
  // steward's host runs the request and the bridge draws a QR, which then arrives like
  // any other status; while the request is pending the list is re-read on the short
  // cadence. The answer of the newest request is shown for QR_FEEDBACK_MS; "on its way"
  // only for QR_ON_ITS_WAY_MS after the bridge took the request (its 30 s QR wait and a
  // status report): a QR that came and went, or an attempt that failed at once, leaves the
  // status line alone to say why there is no QR.
  // Each card shows its channel-down alert line (design 7.9.2, lib/channelAlert.ts), from the
  // answer's own clock and the owner's zone; an answer without them (an older backend) shows none.
  const QR_FEEDBACK_MS = 10 * 60_000, QR_ON_ITS_WAY_MS = 60_000;
  const CHANNELS: OwnerChannelName[] = ['slack', 'wechat'];
  const BIND_POLL_MS = 5000, OFFLINE_POLL_MS = 15000;
  let rows = $state<OwnerChannel[]>([]), defaultChannel = $state<OwnerChannelName | null>(null);
  let clock = $state<ChannelClock | null>(null);
  let loaded = $state(false), error = $state(''), notice = $state(''), busy = $state('');
  // A live code per channel, with the binding it was issued against: a different
  // binding later means the code was used.
  let codes = $state<Partial<Record<OwnerChannelName, {code: string; expires: number; prior: string | null}>>>({});
  let now = $state(Date.now()), lastLoad = 0;
  let unbindAsk = $state(false), target = $state<OwnerChannelName | null>(null);
  let alive = true;
  const row = (channel: OwnerChannelName): OwnerChannel =>
    rows.find(r => r.channel === channel)
    ?? {channel, bound: false, sender_ref: null, bound_at: null, revoked_at: null, is_default: 0};
  const bindingOf = (r: OwnerChannel) => r.bound ? `${r.sender_ref}@${r.bound_at}` : null;
  function failed(e: unknown) { error = String(e instanceof Error ? e.message : e); }
  async function load() {
    lastLoad = Date.now();
    try {
      const result = await listOwnerChannels();
      if (!alive) return;
      rows = result.channels ?? []; defaultChannel = result.default_channel ?? null;
      clock = result.now && result.owner_tz && result.alert_limits
        ? {now: result.now, owner_tz: result.owner_tz, limits: result.alert_limits} : null;
      error = ''; loaded = true;
      for (const channel of CHANNELS) {
        const live = codes[channel], binding = bindingOf(row(channel));
        if (!live || binding === live.prior) continue;
        // An unbind while the code is live leaves the code usable: keep it.
        if (!binding) { codes = {...codes, [channel]: {...live, prior: null}}; continue; }
        codes = {...codes, [channel]: undefined};
        notice = t('steward.chanBoundNotice').replace('{channel}', t('steward.chan.' + channel));
      }
    } catch (e) { if (alive) failed(e); }
  }
  async function newQr(channel: OwnerChannelName) {
    if (busy) return;
    busy = 'qr:' + channel; notice = ''; error = '';
    try {
      const made = await requestOwnerChannelQr(channel);
      rows = rows.map(r => r.channel === channel ? {...r, login_request: made} : r);
      lastLoad = Date.now();
    } catch (e) { failed(e); }
    finally { busy = ''; }
  }
  // The feedback line of a channel's newest QR request, or '' when there is none to show.
  function qrFeedback(r: OwnerChannel): string {
    const q = r.login_request;
    if (!q || r.status?.connected || now - Date.parse(q.requested_at) > QR_FEEDBACK_MS) return '';
    if (q.status === 'pending') return t('steward.chanQrPending');
    // A QR on screen speaks for itself (also when the host ran the request but could not
    // write its answer back, which then reads as expired).
    if (q.status === 'expired') return r.qr_svg ? '' : t('steward.chanQrExpired');
    if (q.result?.requested) return !r.qr_svg && q.completed_at && now - Date.parse(q.completed_at) < QR_ON_ITS_WAY_MS
      ? t('steward.chanQrRequested') : '';
    const refused = q.result?.refused ?? '';
    if (refused === 'rate_limited') return t('steward.chanQrRefused.rate_limited')
      .replace('{minutes}', String(Math.max(1, Math.ceil((retryUntil(q) - now) / 60_000))));
    const known = t('steward.chanQrRefused.' + refused);
    return known !== 'steward.chanQrRefused.' + refused ? known : t('steward.chanQrRefused').replace('{reason}', refused);
  }
  // Until when the bridge said to wait (0: no wait).
  const retryUntil = (q: OwnerChannelLoginRequest) => q.result?.retry_after_s && q.completed_at
    ? Date.parse(q.completed_at) + q.result.retry_after_s * 1000 : 0;
  const qrBlocked = (r: OwnerChannel) => r.login_request?.status === 'pending'
    || (r.login_request ? retryUntil(r.login_request) > now : false);
  async function bind(channel: OwnerChannelName) {
    if (busy) return;
    busy = channel; notice = ''; error = '';
    try {
      const issued = await createOwnerChannelCode(channel);
      codes = {...codes, [channel]: {code: issued.code, expires: Date.parse(issued.expires_at), prior: bindingOf(row(channel))}};
      now = Date.now();
    } catch (e) { failed(e); }
    finally { busy = ''; }
  }
  function askUnbind(channel: OwnerChannelName) { target = channel; unbindAsk = true; }
  async function unbind() {
    const channel = target;
    if (!channel) return;
    notice = ''; error = '';
    try { await unbindOwnerChannel(channel); await load(); }
    catch (e) { failed(e); }
  }
  async function chooseDefault(value: string) {
    const prior = defaultChannel;
    const next = (value || null) as OwnerChannelName | null;
    defaultChannel = next; notice = ''; error = '';
    try { await setDefaultOwnerChannel(next); await load(); }
    catch (e) { defaultChannel = prior; failed(e); }
  }
  // The QR as an image: a data URL, never markup in the page (the SVG is never parsed
  // into the DOM, so nothing in it can run).
  function svgImage(svg: string): string {
    let binary = '';
    for (const byte of new TextEncoder().encode(svg)) binary += String.fromCharCode(byte);
    return 'data:image/svg+xml;base64,' + btoa(binary);
  }
  function remaining(expires: number): string {
    const s = Math.max(0, Math.ceil((expires - now) / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }
  const waiting = $derived(CHANNELS.some(c => (codes[c]?.expires ?? 0) > now) || rows.some(r => r.qr_svg)
    || rows.some(r => r.login_request?.status === 'pending' || (r.login_request?.result?.requested && !r.qr_svg
      && !r.status?.connected && now - Date.parse(r.login_request.requested_at) < 3 * 60_000)));
  const offline = $derived(CHANNELS.some(c => !row(c).status?.connected));
  onMount(() => {
    alive = true;
    void load();
    const timer = setInterval(() => {
      now = Date.now();
      const every = waiting ? BIND_POLL_MS
        : offline && document.visibilityState === 'visible' ? OFFLINE_POLL_MS : 0;
      if (every && now - lastLoad >= every) void load();
    }, 1000);
    return () => { alive = false; clearInterval(timer); };
  });
</script>
<section class="steward-channels" aria-label={t('steward.tabChannels')}>
  <p class="intro">{t('steward.chanIntro')}</p>
  {#if notice}<p class="notice" role="status">{notice}</p>{/if}
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if !loaded && !error}<p aria-busy="true">{t('steward.loading')}</p>{/if}
  {#if loaded}
    {#each CHANNELS as channel (channel)}
      {@const r = row(channel)}
      {@const live = codes[channel]}
      {@const alert = clock ? channelAlertLine(r, rows, clock) : null}
      {@const alertText = alert && clock ? channelAlertText(alert, clock, t) : ''}
      <article class="channel" data-channel={channel} data-bound={r.bound ? '1' : '0'}>
        <header>
          <strong>{t('steward.chan.' + channel)}</strong>
          {#if r.status}
            <span class="mark" class:off={!r.status.connected} data-channel-status={r.status.connected ? 'connected' : 'disconnected'}>
              {r.status.connected ? t('steward.chanConnected') : t('steward.chanDisconnected')}</span>
            {#if r.status.at}<small class="at" data-channel-at>{new Date(r.status.at).toLocaleString()}</small>{/if}
          {/if}
        </header>
        {#if r.status?.detail}<p class="detail" data-channel-detail>{r.status.detail}</p>{/if}
        {#if alertText}<p class="alert" data-channel-alert={alert?.key}>{alertText}</p>{/if}
        {#if r.qr_svg}
          <figure class="qr" data-channel-qr>
            <!-- The QR is a reported SVG: only ever an <img> data URL (no script, no external
                 load), never inlined or rendered with {@html}. The backend's allowlist
                 (core/state_channels.py check_qr_svg) is the second line, not the only one. -->
            <img src={svgImage(r.qr_svg)} alt={t('steward.chanScanQr')} width="220" height="220" />
            <figcaption>{t('steward.chanScanQr')}</figcaption>
          </figure>
        {/if}
        {#if qrFeedback(r)}<p class="detail" data-channel-qr-feedback role="status">{qrFeedback(r)}</p>{/if}
        <p class="account" data-channel-account>
          {#if r.bound}{t('steward.chanBoundAs')} <code>{r.sender_ref}</code>{:else}{t('steward.chanNotBound')}{/if}
        </p>
        {#if live}
          <div class="code" data-channel-code>
            {#if live.expires > now}
              <code class="digits">{live.code}</code>
              <span data-code-countdown>{t('steward.chanCodeExpires').replace('{time}', remaining(live.expires))}</span>
              <p>{t('steward.chanCodeHelp')}</p>
            {:else}<p data-code-expired>{t('steward.chanCodeExpired')}</p>{/if}
          </div>
        {/if}
        <div class="actions">
          <button type="button" class="outline" disabled={busy === channel} onclick={() => void bind(channel)}>{t('steward.chanBind')}</button>
          {#if r.bound}<button type="button" class="outline danger" onclick={() => askUnbind(channel)}>{t('steward.chanUnbind')}</button>{/if}
          {#if channel === 'wechat' && !r.status?.connected}
            <button type="button" class="outline" data-channel-new-qr disabled={busy !== '' || qrBlocked(r)}
              onclick={() => void newQr(channel)}>{t('steward.chanNewQr')}</button>
          {/if}
        </div>
        {#if channel === 'wechat'}<StewardGroups {channel} bound={r.bound} />{/if}
      </article>
    {/each}
    <label class="default">{t('steward.chanDefault')}
      <select value={defaultChannel ?? ''} onchange={(event) => void chooseDefault(event.currentTarget.value)}>
        <option value="">{t('steward.chanDefaultNone')}</option>
        {#each CHANNELS as channel (channel)}<option value={channel}>{t('steward.chan.' + channel)}</option>{/each}
      </select>
    </label>
  {/if}
</section>
<ConfirmDialog bind:show={unbindAsk} variant="danger" title={t('steward.chanUnbindTitle')}
  message={t('steward.chanUnbindConfirm').replace('{account}', target ? row(target).sender_ref ?? '' : '')
    .replace('{channel}', target ? t('steward.chan.' + target) : '')}
  confirmLabel={t('steward.chanUnbind')} onConfirm={unbind} />
<style>
  .steward-channels{font-size:13px;}
  .intro{color:var(--pico-muted-color);}
  .channel{margin:0 0 10px;padding:12px 14px;border:1px solid var(--pico-muted-border-color);border-radius:8px;}
  header{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 6px;padding:0;background:none;border:0;}
  .mark{font-size:11px;padding:1px 6px;border-radius:5px;background:var(--pico-primary);color:var(--pico-primary-inverse);}
  .mark.off{background:var(--pico-del-color, #c62828);color:#fff;}
  .detail{color:var(--pico-muted-color);overflow-wrap:anywhere;margin:0 0 6px;}
  .at{color:var(--pico-muted-color);font-size:11px;}
  .qr{margin:0 0 8px;}.qr img{display:block;max-width:100%;height:auto;background:#fff;border-radius:6px;}
  .qr figcaption{margin:4px 0 0;}
  .account{margin:0 0 6px;overflow-wrap:anywhere;}
  .code{margin:0 0 8px;padding:8px 10px;border-radius:6px;background:var(--pico-code-background-color);}
  .code p{margin:4px 0 0;}
  .digits{font-size:20px;letter-spacing:.2em;margin:0 10px 0 0;}
  .actions{display:flex;gap:8px;flex-wrap:wrap;margin:0;}.actions button{width:auto;margin:0;padding:4px 10px;font-size:12px;}
  .danger{color:var(--pico-del-color);border-color:var(--pico-del-color);}
  .default{display:block;margin:12px 0 0;font-size:13px;}.default select{margin:4px 0 0;font-size:13px;}
  .notice{color:var(--pico-primary);}.error{color:var(--pico-del-color);}
  .alert{margin:0 0 6px;overflow-wrap:anywhere;}
</style>
