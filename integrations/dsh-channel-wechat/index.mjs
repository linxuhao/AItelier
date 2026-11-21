/** dsh-channel-wechat: the owner's WeChat as a channel of the `ownerChannels` service
 *  (dsh-director). The WeChat login itself lives in the wechat-bridge process
 *  (integrations/wechat-bridge), which survives restarts of this one; this plugin
 *  only long-polls its /events into ownerChannels.inbound, sends through its /send,
 *  reports its /status (while a scan is needed: detail 'scan needed' and the login
 *  QR as the bridge drew it, `qrSvg`, which the host shows the owner on the dashboard;
 *  the QR is a login token and is never logged, nor put into the detail) and asks the
 *  bridge for a new login QR through its POST /login (`login()`, for the dashboard's
 *  button and the steward's tool; the answer never carries the QR).
 *
 *  Config (all optional): `url` of the bridge (default http://127.0.0.1:<port>, port
 *  from WECHAT_BRIDGE_PORT or DEFAULT_BRIDGE_PORT: the host port run.sh publishes the
 *  bridge's container on) and `tokenFile` (default
 *  ~/.aitelier-secrets/wechat-bridge-token). No token → the channel is registered as
 *  disconnected ("not configured") and nothing is polled. */
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

export const name = 'dsh-channel-wechat';
export const inject = ['ownerChannels'];

export const CHANNEL = 'wechat';
// No message is dropped for its age: after a restart of this plugin the bridge's
// ring (up to 500 messages) is passed on again and the host takes each message
// ref once; an approval phrase is dated by the host from `at` (refused when too old
// or undated), and the message itself is written either way.
// The answers that are TRANSIENT, not final: the host or the backend cannot be reached
// (`unavailable`), the channel's minute's share is spent (`rate_limited`: the replay of
// the bridge's ring after an outage is a burst by nature), or the decision this message
// carries is still running (`approval_pending`). The message is kept and asked again -
// the cursor does not move past it - never dropped.
const RETRY_REASONS = new Set(['unavailable', 'rate_limited', 'approval_pending']);

const defaultTokenFile = () => join(homedir(), '.aitelier-secrets', 'wechat-bridge-token');
// 18741, not the bridge's in-container 18731: on the owner's host 127.0.0.1:18731 is
// taken by an unrelated service.
export const DEFAULT_BRIDGE_PORT = 18741;
const defaultUrl = () => `http://127.0.0.1:${process.env.WECHAT_BRIDGE_PORT || DEFAULT_BRIDGE_PORT}`;

function readToken(file) {
  try { return readFileSync(file, 'utf8').trim(); } catch { return ''; }
}

const sleep = (ms, signal) => new Promise(resolve => {
  if (signal.aborted) return resolve();
  const timer = setTimeout(done, ms);
  function done() { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); }
  signal.addEventListener('abort', done, { once: true });
});

/** Registers the channel and starts polling; returns `{ stop, polled }` (`polled`
 *  settles when the poll loop has ended). Timings are parameters for the tests. */
/** The bridge's /status as this channel's status. A bridge without `phase` (before the
 *  on-demand QR) is read as before: not logged in is 'scan needed'. */
export function statusFromBridge(body) {
  if (body.logged_in) return { connected: true };
  const qrSvg = typeof body.qr_svg === 'string' && body.qr_svg ? { qrSvg: body.qr_svg } : {};
  switch (body.phase) {
    case 'starting': return { connected: false, detail: 'QR requested' };
    case 'scanned': return { connected: false, detail: 'QR scanned - confirm on the phone' };
    case 'idle': return { connected: false,
      detail: `no QR (${String(body.idle_reason ?? 'idle').slice(0, 40)}) - request one on the dashboard` };
    default: return { connected: false, detail: 'scan needed', ...qrSvg };
  }
}

/** `fastEveryMs`/`fastForMs`: after an accepted login() the status is read this often for
 *  this long, so the new QR reaches the dashboard in seconds, not at the next 30 s read. */
export function startWechatChannel({ ownerChannels, logger = console, config = {}, fetch = globalThis.fetch,
  waitSeconds = 25, retryMs = 5000, maxRetryMs = 60_000, pollGraceMs = 10_000,
  statusEveryMs = 30_000, fastEveryMs = 5000, fastForMs = 180_000 }) {
  const url = String(config.url ?? defaultUrl()).replace(/\/+$/, '');
  const token = readToken(config.tokenFile ?? defaultTokenFile());
  const stopping = new AbortController();
  let status = token ? { connected: false, detail: 'bridge not reached yet' } : { connected: false, detail: 'not configured' };
  const call = async (path, { method = 'GET', body, signal = stopping.signal } = {}) => {
    const response = await fetch(url + path, { method, signal, headers: { authorization: `Bearer ${token}`,
      ...(body ? { 'content-type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(`bridge refused: ${payload.error ?? payload.refused ?? response.status}`),
      { code: payload.error, status: response.status, payload });
    return payload;
  };

  async function refreshStatus() {
    try {
      status = statusFromBridge(await call('/status'));
    } catch (error) {
      if (stopping.signal.aborted) return;
      status = { connected: false, detail: error.status === 401 ? 'bridge refused the token' : 'bridge unreachable' };
    }
  }

  /** Passes one message on. Returns once the host has answered it for good (accepted
   *  or refused); while the host answers a transient reason (`unavailable`,
   *  `rate_limited`, `approval_pending`) the message is held and retried with a growing
   *  pause, however long that takes, so the cursor stays on it and nothing after it
   *  overtakes it. */
  async function deliver(event) {
    // Step 23: a group's @-message or group code carries its room as the conversation; the
    // host answers it finally (accepted or refused), so it never holds the owner's 1:1 messages.
    const room = event.type === 'group_message' || event.type === 'group_bind'
      ? { conversationRef: event.room_ref, ...(event.type === 'group_bind' ? { groupBind: { prefix: event.prefix, code: event.code } } : {}) } : {};
    const message = { channel: CHANNEL, senderRef: event.sender_ref, messageRef: event.message_ref,
      text: event.type === 'group_bind' ? event.code : event.text, at: event.at, ...room };
    let pause = retryMs;
    for (let attempt = 1; !stopping.signal.aborted; attempt++) {
      let reason;
      try {
        const result = await ownerChannels.inbound(message);
        if (result?.accepted || !RETRY_REASONS.has(result?.reason)) return;
        reason = result.reason;
      } catch (error) {
        reason = String(error?.message ?? error).slice(0, 200);
      }
      // Never the text in a log line: the message ref and the reason only.
      logger.warn(`wechat: inbound ${event.message_ref} not delivered (attempt ${attempt}): ${reason}; retrying`);
      await sleep(pause, stopping.signal);
      pause = Math.min(pause * 2, maxRetryMs);
    }
  }

  async function poll() {
    let cursor = 0;
    while (!stopping.signal.aborted) {
      // A poll the bridge never answers is given up pollGraceMs after its wait; the timer is
      // cleared each round, so finished polls leave nothing pending.
      const late = new AbortController();
      const timer = setTimeout(() => late.abort(new Error('bridge did not answer the poll')), waitSeconds * 1000 + pollGraceMs);
      try {
        const body = await call(`/events?after=${cursor}&wait=${waitSeconds}`,
          { signal: AbortSignal.any([stopping.signal, late.signal]) }).finally(() => clearTimeout(timer));
        for (const event of body.events ?? []) {
          if (stopping.signal.aborted) return;
          await deliver(event);
          if (stopping.signal.aborted) return;
          cursor = event.seq;
        }
        cursor = body.next_after ?? cursor;
      } catch (error) {
        if (stopping.signal.aborted) return;
        logger.warn('wechat: polling the bridge failed: ' + String(error?.message ?? error).slice(0, 200));
        await refreshStatus();
        await sleep(retryMs, stopping.signal);
      }
    }
  }

  const unregister = ownerChannels.register({
    name: CHANNEL,
    // `to` is the account bound now, which the host always names; there is no
    // fallback to an earlier sender, who may have been unbound since.
    async send(text, { to } = {}) {
      if (!token) throw new Error('wechat channel not configured');
      if (typeof to !== 'string' || !to) throw Object.assign(new Error('wechat: no recipient named'), { code: 'recipient_unknown' });
      const { ref } = await call('/send', { method: 'POST', body: { to, text } });
      return ref ? { ref } : {};
    },
    /** Step 23: one message into a bound group (the bridge sends only to rooms it was given). */
    async sendGroup(room, text) {
      if (!token) throw new Error('wechat channel not configured');
      const { ref } = await call('/send', { method: 'POST', body: { room, text } });
      return ref ? { ref } : {};
    },
    /** Step 23: the bridge's whitelist, set whole: the bound account and each bound room. */
    async setRooms({ ownerRef, rooms }) {
      if (!token) return;
      await call('/rooms', { method: 'PUT', body: { owner_ref: ownerRef ?? null, rooms } });
    },
    status: () => status,
    /** A new login QR from the bridge: {requested: true} or {refused, retry_after_s?}
     *  (the bridge's own refusals, not_configured, not_supported for a bridge without
     *  POST /login, bridge_refused_token, bridge_unreachable). Never the QR; never throws. */
    async login() {
      if (!token) return { refused: 'not_configured' };
      try {
        await call('/login', { method: 'POST' });
      } catch (error) {
        if (error.status === 404) return { refused: 'not_supported' };
        if (error.status === 401) return { refused: 'bridge_refused_token' };
        if (error.status === 409 || error.status === 429) {
          const retry = Number(error.payload?.retry_after_s);
          return { refused: String(error.payload?.refused ?? 'refused').slice(0, 40),
            ...(Number.isInteger(retry) && retry > 0 ? { retry_after_s: retry } : {}) };
        }
        logger.warn('wechat: asking the bridge for a login QR failed: ' + String(error?.message ?? error).slice(0, 200));
        return { refused: 'bridge_unreachable' };
      }
      fastUntil = Date.now() + fastForMs;
      if (!fastTimer) {
        fastTimer = setInterval(() => {
          if (Date.now() >= fastUntil) { clearInterval(fastTimer); fastTimer = undefined; return; }
          void refreshStatus();
        }, fastEveryMs);
        fastTimer.unref?.();
      }
      await refreshStatus();
      return { requested: true };
    },
  });

  let timer, fastTimer, fastUntil = 0;
  let polled = Promise.resolve();
  if (token) {
    void refreshStatus();
    timer = setInterval(() => void refreshStatus(), statusEveryMs);
    timer.unref?.();
    polled = poll();
  }
  return {
    polled,
    async stop() {
      stopping.abort();
      clearInterval(timer);
      clearInterval(fastTimer);
      unregister?.();
      await polled;
    },
  };
}

export function apply(ctx, config = {}) {
  const channel = startWechatChannel({ ownerChannels: ctx.ownerChannels, logger: ctx.logger, config });
  ctx.on('dispose', () => channel.stop());
}
