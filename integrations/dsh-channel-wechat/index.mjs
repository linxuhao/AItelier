/** dsh-channel-wechat: the owner's WeChat as a channel of the `ownerChannels` service
 *  (dsh-director). The WeChat login itself lives in the wechat-bridge process
 *  (integrations/wechat-bridge), which survives restarts of this one; this plugin
 *  only long-polls its /events into ownerChannels.inbound (step 24.2: a message's files
 *  first through ownerChannels.storeMedia, fetched from the bridge's GET /media/:media_id),
 *  sends through its /send (files through POST /send-file),
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
import { createReadStream, readFileSync } from 'node:fs';
import { stat } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';

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
/** Step 24.2 (design-steward 7.8.12): the kinds the bridge sends (POST /send-file), the most
 *  bytes it takes, and the refusals an attachment may carry into inbound (the host's closed set). */
export const MEDIA_KINDS = Object.freeze(['image', 'voice', 'file']);
export const SEND_FILE_LIMIT = 25 * 1024 * 1024;
const ATTACHMENT_REFUSALS = new Set(['too_large', 'type', 'unsupported', 'gone']);
const refusal = code => Object.assign(new Error(code), { refused: code });

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
 *  settles when the poll loop has ended). Timings, and `wait` (the pause between retries),
 *  are parameters for the tests. */
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
  statusEveryMs = 30_000, fastEveryMs = 5000, fastForMs = 180_000, wait = sleep }) {
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

  /** storeMedia's `open` for one bridge media_id: GET /media/:media_id, streamed. 413 throws
   *  {refused: too_large}, 410 and 404 (its message left the bridge's ring) {refused: gone};
   *  anything else (503 busy, a network error) is thrown plainly - the host answers
   *  `unavailable` and the message is held. */
  const openMedia = mediaId => async ({ signal } = {}) => {
    const response = await fetch(`${url}/media/${encodeURIComponent(mediaId)}`, { headers: { authorization: `Bearer ${token}` },
      signal: signal ? AbortSignal.any([stopping.signal, signal]) : stopping.signal });
    if (response.ok && response.body) return response.body;
    await response.body?.cancel().catch(() => {});
    if (response.status === 413) throw refusal('too_large');
    if (response.status === 410 || response.status === 404) throw refusal('gone');
    throw new Error(`bridge answered ${response.status} for a media fetch`);
  };

  /** Step 24.2: the inbound attachments of an event, into `resolved` (kept on the held message
   *  across retries, so a stored file is never asked for twice). A description the bridge marked
   *  too_large or gone, or that has no media_id, is refused without storeMedia; the rest are
   *  stored one by one. A group event's are stored with its room (`conversationRef`), so the
   *  library keeps them for that room (design/steward-group-media.md, the owner's ruling of
   *  2026-11-22) whether or not the message calls the steward. Returns 'unbound' when storeMedia
   *  answers channel_sender_not_bound (the rest are not fetched); throws when an answer is
   *  transient. */
  async function storeAttachments(event, items, resolved) {
    for (let index = resolved.length; index < items.length; index++) {
      const item = items[index];
      if (item.gone) { resolved.push({ kind: item.kind, refused: 'gone' }); continue; }
      if (item.too_large) { resolved.push({ kind: item.kind, refused: 'too_large' }); continue; }
      // A host without storeMedia (before 24.2) cannot take a file: unsupported, never held.
      if (typeof item.media_id !== 'string' || !item.media_id || typeof ownerChannels.storeMedia !== 'function') {
        resolved.push({ kind: item.kind, refused: 'unsupported' });
        continue;
      }
      const meta = { channel: CHANNEL, senderRef: event.sender_ref, messageRef: event.message_ref, index, kind: item.kind,
        ...(event.type === 'group_message' && event.room_ref ? { conversationRef: event.room_ref } : {}),
        ...(typeof item.mime === 'string' && item.mime ? { mime: item.mime } : {}),
        ...(typeof item.name === 'string' && item.name ? { name: item.name } : {}) };
      const answer = await ownerChannels.storeMedia(meta, openMedia(item.media_id));
      if (typeof answer?.ref === 'string' && answer.ref) resolved.push({ kind: item.kind, ref: answer.ref });
      else if (answer?.refused === 'channel_sender_not_bound') return 'unbound';
      else if (ATTACHMENT_REFUSALS.has(answer?.refused)) resolved.push({ kind: item.kind, refused: answer.refused });
      else if (answer?.refused === 'invalid_media') resolved.push({ kind: item.kind, refused: 'unsupported' });
      else throw new Error(`media ${index} not stored: ${String(answer?.refused ?? 'no answer').slice(0, 40)}`);
    }
    return undefined;
  }

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
    // Step 24.2: a message's files are stored first (storeMedia), and inbound is called once
    // every one has a ref or a refusal; a stranger's (channel_sender_not_bound) go without any.
    // A group message's files take the same path (design/steward-group-media.md item 2): the
    // bridge describes a bound room's image, voice or file whether or not it calls the steward,
    // and the host stores it against that room - a medium kept for a later mention.
    const items = (event.type === 'message' || event.type === 'group_message') && Array.isArray(event.attachments)
      ? event.attachments.filter(item => item && MEDIA_KINDS.includes(item.kind)) : [];
    const resolved = [];
    let unbound = false;
    // Step 23: a group's @-message or group code carries its room as the conversation; the
    // host answers it finally (accepted or refused), so it never holds the owner's 1:1 messages.
    const room = event.type === 'group_message' || event.type === 'group_bind'
      ? { conversationRef: event.room_ref, ...(event.type === 'group_bind' ? { groupBind: { prefix: event.prefix, code: event.code } } : {}) } : {};
    // design/steward-group-media.md item 5: the bridge says whether the message called the
    // steward (`asked`); the host records a group message either way but opens a turn only for
    // the mention. Not sent for 1:1 or a group code, and never invented when the bridge omits it.
    const asked = event.type === 'group_message' && typeof event.asked === 'boolean' ? { asked: event.asked } : {};
    const message = { channel: CHANNEL, senderRef: event.sender_ref, messageRef: event.message_ref,
      text: event.type === 'group_bind' ? event.code : event.text ?? '', at: event.at, ...room, ...asked };
    let pause = retryMs;
    for (let attempt = 1; !stopping.signal.aborted; attempt++) {
      let reason;
      try {
        if (!unbound && resolved.length < items.length) unbound = await storeAttachments(event, items, resolved) === 'unbound';
        const result = await ownerChannels.inbound(!unbound && resolved.length ? { ...message, attachments: resolved } : message);
        if (result?.accepted || !RETRY_REASONS.has(result?.reason)) return;
        reason = result.reason;
      } catch (error) {
        reason = String(error?.message ?? error).slice(0, 200);
      }
      // Never the text in a log line: the message ref and the reason only.
      logger.warn(`wechat: inbound ${event.message_ref} not delivered (attempt ${attempt}): ${reason}; retrying`);
      await wait(pause, stopping.signal);
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
        await wait(retryMs, stopping.signal);
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
    /** Step 24.2: one file from the host's library to the bound owner (`to`) or into a bound
     *  group (`groupRef`), streamed from `path` to the bridge's POST /send-file; a caption
     *  follows as a text. A voice goes as a voice bar, or as a file when WeChat refuses that. */
    async sendFile({ to, groupRef, kind, mime, name, path, caption } = {}) {
      if (!token) throw new Error('wechat channel not configured');
      const target = typeof groupRef === 'string' && groupRef ? { room: groupRef } : typeof to === 'string' && to ? { to } : undefined;
      if (!target) throw Object.assign(new Error('wechat: no recipient named'), { code: 'recipient_unknown' });
      if (!MEDIA_KINDS.includes(kind)) throw Object.assign(new Error('wechat: cannot send this kind'), { code: 'channel_no_media' });
      const { size } = await stat(path);
      if (size > SEND_FILE_LIMIT) throw Object.assign(new Error('wechat: file over 25 MiB'), { code: 'too_large' });
      const query = new URLSearchParams({ ...target, kind, mime: String(mime ?? ''), name: String(name ?? '') });
      const response = await fetch(`${url}/send-file?${query}`, { method: 'POST', signal: stopping.signal, duplex: 'half',
        headers: { authorization: `Bearer ${token}`, 'content-type': 'application/octet-stream' },
        body: Readable.toWeb(createReadStream(path)) });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw Object.assign(new Error(`bridge refused: ${payload.error ?? response.status}`),
        { code: payload.error, status: response.status, payload });
      if (payload.sent_as && payload.sent_as !== kind) logger.warn(`wechat: a ${kind} went out as a ${payload.sent_as}`);
      if (typeof caption === 'string' && caption.trim()) {
        // The file is out: a caption that fails is logged, not reported as a failed send.
        try { await call('/send', { method: 'POST', body: { ...target, text: caption } }); }
        catch (error) { logger.warn('wechat: the caption after a file was not sent: ' + String(error?.message ?? error).slice(0, 200)); }
      }
      return typeof payload.ref === 'string' && payload.ref ? { ref: payload.ref } : {};
    },
    mediaKinds: [...MEDIA_KINDS],
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
