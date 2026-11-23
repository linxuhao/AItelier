/** dsh-channel-slack: the owner's Slack direct messages as an owner channel (design 7.2).
 *
 *  A DSH plugin that registers the channel 'slack' with the `ownerChannels` service.
 *  It moves text and files: a direct message to the bot goes to ownerChannels.inbound
 *  (its files first through ownerChannels.storeMedia, step 24.3, design-steward 7.8.12),
 *  send(text) posts to a DM and sendFile uploads one (filesUploadV2). Who is bound, what a
 *  message means and what may be sent are decided by the service and its host, never
 *  here - so this plugin keeps no binding and makes no judgement beyond "is this a
 *  person's DM to the bot" and "is this a file Slack hosts".
 *
 *  Socket Mode (an outbound websocket) because the host has no public endpoint. The
 *  two tokens are read from files and never logged; with either missing the channel
 *  registers as disconnected and does nothing else. One process per token pair: Slack
 *  spreads a Socket Mode app's events over all its open connections, so a second
 *  instance would silently take half the owner's messages. */
import { readFileSync } from 'node:fs';
import { readFile, stat } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { join } from 'node:path';

export const name = 'dsh-channel-slack';
export const inject = ['ownerChannels'];

// Slack truncates a message's text past 40000 characters and renders long ones badly
// well before that; 3500 keeps every part readable as one message (contract C).
export const SLACK_TEXT_LIMIT = 3500;
// Reconnect backoff: 1 s, doubling, at most 5 min between attempts. The SDK's own
// reconnect is off (it leaves a rejected promise nobody handles when Slack refuses the
// token, which would end the steward's process); this plugin reconnects instead.
export const reconnectDelay = attempt => Math.min(5 * 60_000, 1000 * 2 ** attempt);
// The answers that are TRANSIENT, not final: the host or the backend could not be
// reached (`unavailable`), the channel's minute's share is spent (`rate_limited` - a
// burst after an outage is ordinary), or the decision this message carries is still
// running (`approval_pending`). The message is held and asked again, never dropped; a
// refusal that names anything else (a body too long, an unbound sender) is final.
const RETRY_REASONS = new Set(['unavailable', 'rate_limited', 'approval_pending']);
// Messages one sender may have waiting while the host is unavailable.
export const HELD_PER_SENDER = 100;

/** Step 24.3 (design-steward 7.8.12, "Slack"): the kinds this channel takes and sends, the
 *  byte bounds it shares with the steward's media library (one table, media-types.json, which
 *  lib/media.mjs reads too), the refusals an attachment may carry into inbound (the host's
 *  closed set), and the most files one message hands in (the host refuses more). */
export const MEDIA_KINDS = Object.freeze(['image', 'voice', 'file']);
export const MEDIA_LIMITS = Object.freeze(JSON.parse(
  readFileSync(new URL('../dsh-director/media-types.json', import.meta.url), 'utf8')).limits);
const KIND_BYTES = { image: MEDIA_LIMITS.image_bytes, voice: MEDIA_LIMITS.voice_bytes, file: MEDIA_LIMITS.file_bytes };
const ATTACHMENT_REFUSALS = new Set(['too_large', 'type', 'unsupported', 'gone']);
export const FILES_PER_MESSAGE = 10;
// Same-host redirects followed on a download (Slack serves files from files.slack.com).
const MAX_REDIRECTS = 3;
const refusal = code => Object.assign(new Error(code), { refused: code });

/** A Slack file's kind as the library names it, from what Slack says it is. The library
 *  decides the real type from the first bytes; this only picks the placeholder's word. */
export const slackFileKind = mimetype => {
  const type = String(mimetype ?? '');
  return type.startsWith('image/') ? 'image' : type.startsWith('audio/') ? 'voice' : 'file';
};

/** `raw` (resolved against `base`) when it is a file on files.slack.com over https on the
 *  default port with no credentials in it, else undefined: the bot token goes nowhere else
 *  (docs.slack.dev/messaging/working-with-files). */
export function slackFileUrl(raw, base) {
  let url;
  try { url = new URL(raw, base); } catch { return undefined; }
  return url.protocol === 'https:' && url.hostname === 'files.slack.com' && url.port === '' && !url.username && !url.password
    ? url.href : undefined;
}

/** What this channel refuses itself, before storeMedia, for one entry of `event.files`: a
 *  remote file (another service's, not hosted by Slack), one Slack no longer has, one with
 *  no download address on files.slack.com, or one whose declared size is over the bound. */
export function slackFileRefusal(file) {
  if (!file || typeof file !== 'object') return 'unsupported';
  if (file.mode === 'tombstone' || file.mode === 'hidden_by_limit') return 'gone';
  if (file.is_external || file.mode === 'external') return 'unsupported';
  if (!slackFileUrl(file.url_private_download)) return 'unsupported';
  if (Number.isFinite(file.size) && file.size > KIND_BYTES[slackFileKind(file.mimetype)]) return 'too_large';
  return undefined;
}

// A pause that ends early when the channel stops; it never keeps the process alive.
const sleep = (ms, signal) => new Promise(resolve => {
  if (signal.aborted) return resolve();
  const timer = setTimeout(done, ms);
  timer.unref?.();
  function done() { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); }
  signal.addEventListener('abort', done, { once: true });
});

/** The two tokens, or the reason there are none. Only the reason is ever printed. */
export function readTokens(dir) {
  const read = file => { try { return readFileSync(join(dir, file), 'utf8').trim(); } catch { return ''; } };
  const appToken = read('slack-app-token'), botToken = read('slack-bot-token');
  if (!appToken || !botToken) return { reason: 'not configured: slack-app-token / slack-bot-token missing' };
  if (!appToken.startsWith('xapp-')) return { reason: 'slack-app-token is not an app-level (xapp-) token' };
  if (!botToken.startsWith('xoxb-')) return { reason: 'slack-bot-token is not a bot (xoxb-) token' };
  return { appToken, botToken };
}

/** A person's direct message to the bot - not an edit, a join, a bot or another channel. A
 *  message with files comes either with the subtype `file_share` or with none (Slack still
 *  sends both, docs.slack.dev/reference/events/message/file_share); both are taken. */
export function isOwnerDm(event) {
  return event?.type === 'message' && event.channel_type === 'im' && (!event.subtype || event.subtype === 'file_share')
    && !event.bot_id && typeof event.user === 'string' && event.user !== ''
    && (typeof event.text === 'string' || (Array.isArray(event.files) && event.files.length > 0));
}

// Slack escapes exactly these three in message text, both ways
// (docs.slack.dev/messaging/formatting-message-text#escaping): unescaped on the way in
// so the owner's words reach the steward as typed, escaped on the way out so a `<` in
// the steward's text is shown, not read as a link or mention.
export const unescapeSlack = text => text.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
export const escapeSlack = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/** Consecutive parts of at most `limit` characters (code points, so no emoji is cut in
 *  half), each ending at a line break when one falls in its second half. */
export function splitText(text, limit = SLACK_TEXT_LIMIT) {
  const chars = Array.from(text);
  const parts = [];
  while (chars.length > limit) {
    const newline = chars.lastIndexOf('\n', limit - 1);
    const cut = newline >= limit / 2 ? newline + 1 : limit;
    parts.push(chars.splice(0, cut).join(''));
  }
  parts.push(chars.join(''));
  return parts;
}

/** The channel itself, with the Slack clients passed in (fakes in tests). */
export function slackChannel({ ownerChannels, appToken, botToken, SocketModeClient, WebClient, logLevel, log, unavailable,
  backoff = reconnectDelay, retryMs = 5000, maxRetryMs = 60_000, fetch = globalThis.fetch }) {
  const configured = !unavailable && appToken && botToken && SocketModeClient && WebClient;
  let state = { connected: false, detail: configured ? 'connecting' : (unavailable ?? 'not configured') };
  let socket, web, stopped = false;
  let connecting, retryTimer, failures = 0;
  const dms = new Map(); // user -> DM channel id
  const stopping = new AbortController();
  const lanes = new Map(); // user -> { run: the delivery of their latest message, held: messages waiting }

  /** storeMedia's `open` for one Slack file: GET its url_private_download with the bot
   *  token, following redirects by hand and only to files.slack.com over https (a redirect
   *  anywhere else is not followed, so the token never leaves Slack's file host). Throws
   *  {refused} for what will not change on a retry: gone (404, 410), unsupported (a redirect
   *  off files.slack.com, 401/403, or a page instead of a file - what Slack serves when the
   *  app lacks files:read). Anything else (429, 5xx, the network) is thrown plainly: the
   *  host answers `unavailable` and the message is held. */
  const openFile = file => async ({ signal } = {}) => {
    let target = slackFileUrl(file.url_private_download);
    for (let hop = 0; ; hop++) {
      const response = await fetch(target, { headers: { authorization: `Bearer ${botToken}` }, redirect: 'manual',
        signal: signal ? AbortSignal.any([stopping.signal, signal]) : stopping.signal });
      if (response.status >= 300 && response.status < 400) {
        await response.body?.cancel().catch(() => {});
        const next = slackFileUrl(response.headers.get('location') ?? '', target);
        if (!next || hop >= MAX_REDIRECTS) {
          log.warn(`slack: a file was not fetched: redirected ${next ? 'too often' : 'off files.slack.com'}`);
          throw refusal('unsupported');
        }
        target = next;
        continue;
      }
      const page = /^text\/html\b/i.test(response.headers.get('content-type') ?? '');
      if (response.ok && response.body && !page) return response.body;
      await response.body?.cancel().catch(() => {});
      if (response.status === 404 || response.status === 410) throw refusal('gone');
      if (page || response.status === 401 || response.status === 403) {
        log.warn(`slack: a file was not fetched: ${page ? 'Slack answered a page' : response.status} `
          + '(does the app have files:read? reinstall it with the manifest\'s scopes)');
        throw refusal('unsupported');
      }
      throw new Error(`slack answered ${response.status} for a file`);
    }
  };

  /** The message's files into `resolved` (kept on the held message across retries, so a
   *  stored file is never fetched twice). What this channel refuses itself goes in without
   *  storeMedia; the rest are stored one by one. Returns 'unbound' when storeMedia answers
   *  channel_sender_not_bound (the rest are not fetched); throws when an answer is transient. */
  async function storeAttachments(message, files, resolved) {
    for (let index = resolved.length; index < files.length; index++) {
      const file = files[index];
      const kind = slackFileKind(file?.mimetype);
      const refused = slackFileRefusal(file);
      if (refused) { resolved.push({ kind, refused }); continue; }
      // A host without storeMedia (before 24.2) cannot take a file: unsupported, never held.
      if (typeof ownerChannels.storeMedia !== 'function') { resolved.push({ kind, refused: 'unsupported' }); continue; }
      const meta = { channel: 'slack', senderRef: message.senderRef, messageRef: message.messageRef, index, kind,
        ...(typeof file.mimetype === 'string' && file.mimetype ? { mime: file.mimetype } : {}),
        ...(typeof file.name === 'string' && file.name ? { name: file.name } : {}) };
      const answer = await ownerChannels.storeMedia(meta, openFile(file));
      if (typeof answer?.ref === 'string' && answer.ref) resolved.push({ kind, ref: answer.ref });
      else if (answer?.refused === 'channel_sender_not_bound') return 'unbound';
      else if (ATTACHMENT_REFUSALS.has(answer?.refused)) resolved.push({ kind, refused: answer.refused });
      else if (answer?.refused === 'invalid_media') resolved.push({ kind, refused: 'unsupported' });
      else throw new Error(`media ${index} not stored: ${String(answer?.refused ?? 'no answer').slice(0, 40)}`);
    }
    return undefined;
  }

  /** Passes one message on. An acknowledged envelope is never redelivered by Slack, so
   *  this is the message's only copy: while the host answers a transient reason
   *  (`unavailable`, `rate_limited`, `approval_pending`) - or throws - it is held and
   *  retried with a growing pause, under the same message ref, which the host takes
   *  once. It ends when the host has answered for good (accepted or refused) or
   *  the channel stops. Its files (step 24.3) are stored first, and inbound is called once
   *  every one has a ref or a refusal; a stranger's (channel_sender_not_bound) go without any. */
  async function deliver(message, files = []) {
    const resolved = [];
    let unbound = false;
    let pause = retryMs;
    for (let attempt = 1; !stopping.signal.aborted; attempt++) {
      let reason;
      try {
        if (!unbound && resolved.length < files.length) unbound = await storeAttachments(message, files, resolved) === 'unbound';
        const result = await ownerChannels.inbound(!unbound && resolved.length ? { ...message, attachments: resolved } : message);
        if (result?.accepted || !RETRY_REASONS.has(result?.reason)) return;
        reason = result.reason;
      } catch (error) {
        reason = error?.message ?? error;
      }
      // Never the text: what a stranger sent is not logged (design 7.2 B3).
      log.warn(`slack: inbound from ${message.senderRef} (${message.text.length} chars) not delivered (attempt ${attempt}): `
        + `${String(reason).slice(0, 200)}; retrying`);
      await sleep(pause, stopping.signal);
      pause = Math.min(pause * 2, maxRetryMs);
    }
  }

  // Each sender's messages go on in the order they arrived: a held one is not overtaken
  // by the next. Other senders are not held up by it. A sender with HELD_PER_SENDER
  // messages already waiting (the host down, a stranger flooding) gets no more held.
  function enqueue(message, files) {
    const lane = lanes.get(message.senderRef) ?? { run: Promise.resolve(), held: 0 };
    if (lane.held >= HELD_PER_SENDER) {
      log.warn(`slack: inbound from ${message.senderRef} (${message.text.length} chars) not held: `
        + `${HELD_PER_SENDER} messages from this sender are already waiting`);
      return lane.run;
    }
    lane.held++;
    lane.run = lane.run.then(() => deliver(message, files)).finally(() => {
      if (--lane.held === 0 && lanes.get(message.senderRef) === lane) lanes.delete(message.senderRef);
    });
    lanes.set(message.senderRef, lane);
    return lane.run;
  }

  async function onEnvelope({ ack, type, body }) {
    // Every envelope is acknowledged, first: Slack redelivers an unacknowledged one after
    // 3 s, and handing a message to the service can take longer than that.
    try { await ack(); } catch (error) { log.warn(`slack: ack failed: ${error?.message ?? error}`); }
    const event = type === 'events_api' ? body?.event : undefined;
    if (!isOwnerDm(event)) return;
    const messageRef = event.client_msg_id || event.ts;
    const at = Number.isFinite(Number(event.ts)) ? new Date(Number(event.ts) * 1000).toISOString() : undefined;
    const files = Array.isArray(event.files) ? event.files.slice(0, FILES_PER_MESSAGE) : [];
    return enqueue({ channel: 'slack', senderRef: event.user, messageRef, text: unescapeSlack(event.text ?? ''), at }, files);
  }

  // One connection attempt at a time; every failure, now or after a drop, is caught here
  // and becomes a later attempt, so nothing the socket does can reject into the process.
  function connect() {
    if (stopped || connecting) return connecting;
    connecting = (async () => {
      try {
        await socket.start();
        failures = 0;
        // stop() ran while this attempt was in flight: close what it just opened.
        if (stopped) await socket.disconnect();
      } catch (error) {
        if (stopped) return;
        // Slack's error code (invalid_auth, ...) names the cause; the token is never in it.
        const cause = error?.data?.error ?? error?.code ?? error?.message ?? 'connection closed';
        state = { connected: false, detail: `disconnected: ${String(cause).slice(0, 200)}` };
        log.warn(`slack: ${state.detail}`);
        retry();
      } finally {
        connecting = undefined;
      }
    })();
    return connecting;
  }

  function retry() {
    if (stopped || retryTimer) return;
    retryTimer = setTimeout(() => { retryTimer = undefined; void connect(); }, backoff(failures++));
    retryTimer.unref?.();
  }

  async function start() {
    if (!configured) return;
    web = new WebClient(botToken, { logLevel });
    socket = new SocketModeClient({ appToken, logLevel, autoReconnectEnabled: false });
    socket.on('connected', () => { if (!stopped) state = { connected: true }; });
    socket.on('disconnected', () => {
      // A failed attempt is handled where it was made; a drop of a live socket starts a new one.
      if (connecting && !stopped) return;
      state = { connected: false, detail: 'disconnected' };
      retry();
    });
    socket.on('slack_event', envelope => { void onEnvelope(envelope); });
    await connect();
  }

  // `to` is the bound account, given by the service on every send (it re-reads the binding
  // each time); this plugin keeps no idea of its own about who the owner is.
  async function send(text, { to } = {}) {
    if (!configured) throw new Error(`slack channel ${state.detail}`);
    if (typeof to !== 'string' || to === '') throw new Error('slack: send needs the account to send to (options.to)');
    const channel = await dmOf(to);
    const refs = [];
    for (const part of splitText(text)) {
      const posted = await web.chat.postMessage({ channel, text: escapeSlack(part) });
      refs.push(posted.ts);
    }
    return { ref: refs[0] };
  }

  async function dmOf(to) {
    if (!dms.has(to)) {
      const opened = await web.conversations.open({ users: to });
      dms.set(to, opened.channel.id);
    }
    return dms.get(to);
  }

  /** Step 24.3: one file from the host's library to the DM of `to`, the bound account, with
   *  filesUploadV2 (files.getUploadURLExternal, the upload, files.completeUploadExternal; the
   *  old files.upload was retired 2025-11-12); a caption goes as its initial comment. Slack
   *  has no bound groups here, so a `groupRef` is refused channel_no_media. The ref is the
   *  Slack file id. */
  async function sendFile({ to, groupRef, kind, name, path, caption } = {}) {
    if (!configured) throw new Error(`slack channel ${state.detail}`);
    if (groupRef !== undefined || !MEDIA_KINDS.includes(kind))
      throw Object.assign(new Error('slack: cannot send this file here'), { code: 'channel_no_media' });
    if (typeof to !== 'string' || to === '') throw Object.assign(new Error('slack: no recipient named'), { code: 'recipient_unknown' });
    const { size } = await stat(path);
    if (size > MEDIA_LIMITS.outbound_bytes) throw Object.assign(new Error('slack: file over the outbound bound'), { code: 'too_large' });
    const channel_id = await dmOf(to);
    const result = await web.filesUploadV2({ channel_id, file: await readFile(path),
      filename: typeof name === 'string' && name ? name : 'file',
      ...(typeof caption === 'string' && caption.trim() ? { initial_comment: escapeSlack(caption) } : {}) });
    const id = result?.files?.[0]?.files?.[0]?.id;
    return typeof id === 'string' && id ? { ref: id } : {};
  }

  const status = () => ({ ...state });
  const stop = async () => {
    if (stopped) return;
    stopped = true;
    stopping.abort();
    clearTimeout(retryTimer);
    retryTimer = undefined;
    state = { connected: false, detail: 'disconnected' };
    await socket?.disconnect?.().catch(error => log.warn(`slack: disconnect failed: ${error?.message ?? error}`));
  };
  return { start, stop, send, sendFile, status, onEnvelope };
}

/** The pinned Slack SDK (contract C: @slack/socket-mode 3.1.1, @slack/web-api 8.3.0). It
 *  is installed with the DSH runtime (integrations/dsh-driver/package.json), not beside
 *  this file, so it is resolved from the running DSH entry, as dsh-director resolves the
 *  DSH packages. */
export function loadSlack(resolveFrom = process.argv[1]) {
  const require = createRequire(resolveFrom);
  const { SocketModeClient, LogLevel } = require('@slack/socket-mode');
  const { WebClient } = require('@slack/web-api');
  return { SocketModeClient, WebClient, logLevel: LogLevel.WARN };
}

export function apply(ctx, config = {}) {
  const log = ctx.logger ?? console;
  const tokens = readTokens(config.secretsDir ?? join(homedir(), '.aitelier-secrets'));
  let slack = {}, unavailable = tokens.reason;
  if (!unavailable) {
    try { slack = loadSlack(config.resolveFrom); } catch (error) { unavailable = `slack SDK not installed: ${error?.code ?? error?.message}`; }
  }
  if (unavailable) log.info(`slack channel: ${unavailable}`);
  const channel = slackChannel({ ownerChannels: ctx.ownerChannels, ...tokens, ...slack, log, unavailable });
  const unregister = ctx.ownerChannels.register({ name: 'slack', send: channel.send, status: channel.status,
    sendFile: channel.sendFile, mediaKinds: [...MEDIA_KINDS] });
  void channel.start();
  ctx.on('dispose', () => { unregister?.(); return channel.stop(); });
}
