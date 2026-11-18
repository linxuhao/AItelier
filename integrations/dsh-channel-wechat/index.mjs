/** dsh-channel-wechat: the owner's WeChat as a channel of the `ownerChannels` service
 *  (dsh-director). The WeChat login itself lives in the wechat-bridge process
 *  (integrations/wechat-bridge), which survives restarts of this one; this plugin
 *  only long-polls its /events into ownerChannels.inbound, sends through its /send
 *  and reports its /status.
 *
 *  Config (all optional): `url` of the bridge (default http://127.0.0.1:<port>, port
 *  from WECHAT_BRIDGE_PORT or 18731) and `tokenFile` (default
 *  ~/.aitelier-secrets/wechat-bridge-token). No token → the channel is registered as
 *  disconnected ("not configured") and nothing is polled. */
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

export const name = 'dsh-channel-wechat';
export const inject = ['ownerChannels'];

export const CHANNEL = 'wechat';
// A message the owner wrote more than this long ago is not passed on. That covers
// three cases: a restart of this plugin (the bridge still holds up to 500 old
// messages; recent ones may not have been relayed yet and are, since the backend
// drops a repeat of the same message ref), a bridge that logs in again (neo then
// delivers what was written while it was down, `同意 proposal …` included, and an
// approval must not be executed hours after it was written), and a host that stays
// unavailable (the message is held and retried until it is this old).
export const MAX_EVENT_AGE_MS = 10 * 60 * 1000;
// ownerChannels.inbound answers `unavailable` (or throws) when the host or the
// backend cannot be reached: the message is kept, not dropped.
const RETRY_REASONS = new Set(['unavailable']);

const defaultTokenFile = () => join(homedir(), '.aitelier-secrets', 'wechat-bridge-token');
const defaultUrl = () => `http://127.0.0.1:${process.env.WECHAT_BRIDGE_PORT || 18731}`;

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
export function startWechatChannel({ ownerChannels, logger = console, config = {}, fetch = globalThis.fetch,
  now = () => Date.now(), waitSeconds = 25, retryMs = 5000, maxRetryMs = 60_000, pollGraceMs = 10_000,
  statusEveryMs = 30_000 }) {
  const url = String(config.url ?? defaultUrl()).replace(/\/+$/, '');
  const token = readToken(config.tokenFile ?? defaultTokenFile());
  const stopping = new AbortController();
  let status = token ? { connected: false, detail: 'bridge not reached yet' } : { connected: false, detail: 'not configured' };
  const call = async (path, { method = 'GET', body, signal = stopping.signal } = {}) => {
    const response = await fetch(url + path, { method, signal, headers: { authorization: `Bearer ${token}`,
      ...(body ? { 'content-type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(`bridge refused: ${payload.error ?? response.status}`), { code: payload.error, status: response.status });
    return payload;
  };

  async function refreshStatus() {
    try {
      const body = await call('/status');
      status = body.logged_in ? { connected: true }
        : { connected: false, detail: body.qr ? `scan needed: ${body.qr}` : 'scan needed' };
    } catch (error) {
      if (stopping.signal.aborted) return;
      status = { connected: false, detail: error.status === 401 ? 'bridge refused the token' : 'bridge unreachable' };
    }
  }

  // When the owner wrote it; a message without a readable time counts from now.
  const writtenAt = event => {
    const at = Date.parse(event.at);
    return Number.isFinite(at) ? at : now();
  };
  const tooOld = (event, born) => now() - born > MAX_EVENT_AGE_MS;

  /** Passes one message on. Returns once the host has answered it for good (accepted
   *  or refused) or it is too old; while the host is unavailable the message is held
   *  and retried with a growing pause, so the cursor stays on it and nothing after it
   *  overtakes it. */
  async function deliver(event) {
    const born = writtenAt(event);
    const message = { channel: CHANNEL, senderRef: event.sender_ref, messageRef: event.message_ref, text: event.text, at: event.at };
    let pause = retryMs;
    for (let attempt = 1; !stopping.signal.aborted; attempt++) {
      // Never the text in a log line: the message ref and the reason only.
      if (tooOld(event, born)) {
        logger.warn(`wechat: inbound ${event.message_ref} not passed on: written more than ${MAX_EVENT_AGE_MS / 60000} min ago`);
        return;
      }
      let reason;
      try {
        const result = await ownerChannels.inbound(message);
        if (result?.accepted || !RETRY_REASONS.has(result?.reason)) return;
        reason = result.reason;
      } catch (error) {
        reason = String(error?.message ?? error).slice(0, 200);
      }
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
    status: () => status,
  });

  let timer;
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
      unregister?.();
      await polled;
    },
  };
}

export function apply(ctx, config = {}) {
  const channel = startWechatChannel({ ownerChannels: ctx.ownerChannels, logger: ctx.logger, config });
  ctx.on('dispose', () => channel.stop());
}
