<script lang="ts">
  import { onMount } from 'svelte';
  import { createOwnerChannelCode, listOwnerChannels, setDefaultOwnerChannel, unbindOwnerChannel,
    type OwnerChannel, type OwnerChannelName } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  import ConfirmDialog from './ConfirmDialog.svelte';
  // The owner's chat channels (design 7.2): who is bound on Slack / WeChat, the
  // one-time bind code, unbind, and where the steward's unprompted messages go.
  // Binding happens off this page (the owner sends the code to the bot), so while
  // a code is live the list is re-read on a short cadence to show the result; the
  // same while a login QR is shown (it is replaced, or scanned, off this page too).
  const CHANNELS: OwnerChannelName[] = ['slack', 'wechat'];
  const BIND_POLL_MS = 5000;
  let rows = $state<OwnerChannel[]>([]), defaultChannel = $state<OwnerChannelName | null>(null);
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
  const waiting = $derived(CHANNELS.some(c => (codes[c]?.expires ?? 0) > now) || rows.some(r => r.qr_svg));
  onMount(() => {
    alive = true;
    void load();
    const timer = setInterval(() => {
      now = Date.now();
      if (waiting && now - lastLoad >= BIND_POLL_MS) void load();
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
        {#if r.qr_svg}
          <figure class="qr" data-channel-qr>
            <!-- The QR is a reported SVG: only ever an <img> data URL (no script, no external
                 load), never inlined or rendered with {@html}. The backend's allowlist
                 (core/state_channels.py check_qr_svg) is the second line, not the only one. -->
            <img src={svgImage(r.qr_svg)} alt={t('steward.chanScanQr')} width="220" height="220" />
            <figcaption>{t('steward.chanScanQr')}</figcaption>
          </figure>
        {/if}
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
        </div>
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
</style>
