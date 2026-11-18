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
// After a restart of this plugin the bridge still holds up to 500 old messages. Those
// from shortly before the restart may not have been relayed yet, so they are passed
// on (the backend drops a repeat of the same message ref); older ones are not, so a
// restart never replays the whole ring into the owner's conversation.
export const REPLAY_WINDOW_MS = 10 * 60 * 1000;
const INBOUND_ATTEMPTS = 3;
const KNOWN_REFS = 200;

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
  now = () => Date.now(), waitSeconds = 25, retryMs = 5000, statusEveryMs = 30_000 }) {
  const url = String(config.url ?? defaultUrl()).replace(/\/+$/, '');
  const token = readToken(config.tokenFile ?? defaultTokenFile());
  const stopping = new AbortController();
  const startedAt = now();
  let status = token ? { connected: false, detail: 'bridge not reached yet' } : { connected: false, detail: 'not configured' };
  // Who to write to: send() is told which owner message it answers (replyToRef), and
  // the sender of that message is known from the inbound it came with. An unprompted
  // message goes to the last sender the host accepted (only the bound owner is).
  const senderOf = new Map();
  let lastAccepted;

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

  async function deliver(event) {
    const message = { channel: CHANNEL, senderRef: event.sender_ref, messageRef: event.message_ref, text: event.text, at: event.at };
    for (let attempt = 1; attempt <= INBOUND_ATTEMPTS && !stopping.signal.aborted; attempt++) {
      try {
        const result = await ownerChannels.inbound(message);
        if (result?.accepted) {
          lastAccepted = event.sender_ref;
          senderOf.set(event.message_ref, event.sender_ref);
          if (senderOf.size > KNOWN_REFS) senderOf.delete(senderOf.keys().next().value);
        }
        return;
      } catch (error) {
        // Never the text: the message ref and the error only.
        logger.warn(`wechat: inbound ${event.message_ref} failed (attempt ${attempt}): ${String(error?.message ?? error).slice(0, 200)}`);
        if (attempt < INBOUND_ATTEMPTS) await sleep(retryMs, stopping.signal);
      }
    }
  }

  async function poll() {
    let cursor = 0;
    while (!stopping.signal.aborted) {
      // A poll the bridge never answers is given up 10 s after its wait; the timer is
      // cleared each round, so finished polls leave nothing pending.
      const late = new AbortController();
      const timer = setTimeout(() => late.abort(new Error('bridge did not answer the poll')), (waitSeconds + 10) * 1000);
      try {
        const body = await call(`/events?after=${cursor}&wait=${waitSeconds}`,
          { signal: AbortSignal.any([stopping.signal, late.signal]) }).finally(() => clearTimeout(timer));
        for (const event of body.events ?? []) {
          if (stopping.signal.aborted) return;
          if (!(Date.parse(event.at) < startedAt - REPLAY_WINDOW_MS)) await deliver(event);
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
    async send(text, { replyToRef, to } = {}) {
      if (!token) throw new Error('wechat channel not configured');
      const recipient = to ?? senderOf.get(replyToRef) ?? lastAccepted;
      if (!recipient) throw Object.assign(new Error('wechat: no known recipient'), { code: 'recipient_unknown' });
      const { ref } = await call('/send', { method: 'POST', body: { to: recipient, text } });
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
