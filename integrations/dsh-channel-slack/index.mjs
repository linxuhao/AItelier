/** dsh-channel-slack: the owner's Slack direct messages as an owner channel (design 7.2), and
 *  the private channels the owner binds as steward groups (step 23, design-steward 7.7.19).
 *
 *  A DSH plugin that registers the channel 'slack' with the `ownerChannels` service.
 *  It moves text and files: a direct message to the bot goes to ownerChannels.inbound
 *  (its files first through ownerChannels.storeMedia, step 24.3, design-steward 7.8.12),
 *  send(text) posts to a DM and sendFile uploads one (filesUploadV2). Who is bound, what a
 *  message means and what may be sent are decided by the service and its host, never
 *  here - so this plugin keeps no binding and makes no judgement beyond "is this a
 *  person's DM to the bot" and "is this a file Slack hosts".
 *
 *  Step 23 (the platform layer of the WeChat group design, as the bridge is for WeChat): only
 *  `app_mention` events come in from channels (an un-mentioned message is never sent to the app,
 *  D6). The host names the bound account and the bound groups (setRooms, its whitelist, in memory);
 *  a mention in a bound channel goes in as a group message (`conversationRef` the channel,
 *  messageRef `<channel>:<ts>`, the bot's own mention taken off), one in any other channel only
 *  when it is the bound account's group code alone - then as a bind. What Slack says of the
 *  conversation is the one judgement made here: a group is a private channel (D4) not shared with
 *  another organization (D5), never a DM or a group DM; a bound channel found otherwise (the
 *  envelope's is_ext_shared_channel, or conversations.info on each message and each send) is
 *  paused - nothing more is taken from it or sent to it in this process - and the channel's status
 *  says so. Replies go into the asker's thread (D8) with unfurls off.
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
/** Step 23: a group code as the owner posts it after the mention, G-XXXX-XXXX (Crockford base32),
 *  the bridge's own pattern (integrations/wechat-bridge/bridge.mjs GROUP_CODE). */
export const GROUP_CODE = /^[Gg](?:[\s-]*[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]){8}$/;
// The threads of the group questions and codes this process saw (messageRef -> the thread's root ts), so a
// reply to a question asked inside a thread lands in that thread; past this many the oldest go
// (a reply to a forgotten one threads on the question's own ts).
export const THREADS_KEPT = 1000;
// The message refs of the group codes this process passed on as binds: Socket Mode sends an event
// again when its ack was not seen, and by then the bind has made the channel a group, so the same
// code message would come back as a question (review rv-sg 1). Past this many the oldest go.
export const BINDS_KEPT = 100;
const BOT_ID = /^[UW][A-Z0-9]+$/;
const OWN_MENTION = /^<@[UW][A-Z0-9]+>$/;
// The answers of conversations.info that say Slack will not describe this conversation to the app:
// final, each named by its own code. The manifest grants groups:read and no channels:read, im:read
// or mpim:read (7.7.19), so Slack answers missing_scope for a public channel, a DM or a group DM;
// when the token's scopes (the answer's `provided`) include groups:read that is the conversation's
// kind, `not_private`, and only otherwise the app's own scope (`missing_scope`, an app installed
// without groups:read). Any other failure is thrown and asked again.
const INFO_REFUSALS = new Set(['missing_scope', 'not_allowed_token_type', 'channel_not_found', 'not_in_channel',
  'method_not_supported_for_channel_type']);
// What the owner does about a paused group, by why it is paused: a scope or a token is put right in
// Slack (the pause lasts until the steward restarts); everything else is unbound.
const REMEDIES = { missing_scope: 'reinstall the Slack app from its manifest, then restart the steward',
  not_allowed_token_type: 'save the bot (xoxb-) token again, then restart the steward' };
const remedyOf = why => REMEDIES[why] ?? 'unbind on the dashboard';
// How long a mention waits for the host's first room list (the host's own start may take 2 min);
// past it mentions are judged with no group bound, so a host that never lists holds up nothing.
export const ROOMS_WAIT_MS = 5 * 60_000;
const short = error => String(error?.data?.error ?? error?.message ?? error).slice(0, 200);
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

// When Slack says the message was written (its ts), or undefined.
const atOf = ts => (Number.isFinite(Number(ts)) ? new Date(Number(ts) * 1000).toISOString() : undefined);

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

/** Step 23: a person's mention of the bot in a conversation (the only channel event subscribed):
 *  not a bot's, not an edit, with its sender, channel, ts and text. */
export function isGroupMention(event) {
  return event?.type === 'app_mention' && !event.subtype && !event.bot_id && typeof event.text === 'string'
    && [event.user, event.channel, event.ts].every(value => typeof value === 'string' && value !== '');
}

/** The text with the bot's own mentions (`<@BOT>` or `<@BOT|name>`) taken out, wherever they
 *  are, and trimmed; anyone else's mention stays as Slack wrote it. */
export function withoutMention(text, botId) {
  if (!BOT_ID.test(String(botId ?? ''))) return String(text ?? '').trim();
  return String(text ?? '').replace(new RegExp(`[ \\t\u00a0]*<@${botId}(?:\\|[^>]*)?>[ \\t\u00a0]*`, 'g'), ' ').trim();
}

/** Why a conversation (conversations.info's `channel`) cannot be a steward group, or undefined:
 *  a DM or a group DM (not_a_channel), a Slack Connect channel - shared, or an invitation pending
 *  (shared, D5 (a)) - or a channel that is not private (not_private, D4 (a)). */
export function groupRefusal(info) {
  if (!info || typeof info !== 'object') return 'unreadable';
  if (info.is_im === true || info.is_mpim === true) return 'not_a_channel';
  if (info.is_ext_shared === true || info.is_pending_ext_shared === true) return 'shared';
  if (info.is_private !== true) return 'not_private';
  return undefined;
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
  backoff = reconnectDelay, retryMs = 5000, maxRetryMs = 60_000, fetch = globalThis.fetch, roomsWaitMs = ROOMS_WAIT_MS }) {
  const configured = !unavailable && appToken && botToken && SocketModeClient && WebClient;
  let state = { connected: false, detail: configured ? 'connecting' : (unavailable ?? 'not configured') };
  let socket, web, stopped = false;
  let connecting, retryTimer, failures = 0;
  const dms = new Map(); // user -> DM channel id
  const stopping = new AbortController();
  const lanes = new Map(); // user, or group:<channel> -> { run: the delivery of its latest message, held: messages waiting }
  // Step 23: the host's whitelist (setRooms), in memory: a restart starts with none and waits for
  // the host's first list before any mention is judged.
  let ownerRef, rooms = new Map(); // bound channel -> {prefix: the bot's mention the backend keeps, label: the group's name}
  let roomsListed, roomsKnown = false, roomsWait;
  const listed = new Promise(resolve => { roomsListed = resolve; });
  const paused = new Map(); // bound channel -> why it is paused (shared, not_private, ...)
  const threads = new Map(); // `${channel}:${ts}` of a group question or code -> its thread's root ts
  const binds = new Set(); // `${channel}:${ts}` of the group codes passed on as binds
  let botAsked;

  /** storeMedia's `open` for one Slack file: GET its url_private_download with the bot
   *  token, following redirects by hand and only to files.slack.com over https (a redirect
   *  anywhere else is not followed, so the token never leaves Slack's file host). Throws
   *  {refused} for what will not change on a retry: gone (404, 410, or a 2xx with no body -
   *  204), unsupported (a redirect off files.slack.com - another Slack host included - or past
   *  the MAX_REDIRECTS'th, 401/403, or a page instead of a file - what Slack serves when the
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
      if (response.ok && response.body && response.status !== 204 && !page) return response.body;
      await response.body?.cancel().catch(() => {});
      // An empty success will not fill on a retry: the file is gone, not held.
      if (response.status === 404 || response.status === 410 || (response.ok && !page)) throw refusal('gone');
      if (page || response.status === 401 || response.status === 403) {
        log.warn(`slack: a file was not fetched: ${page ? 'Slack answered a page' : response.status} `
          + '(does the app have files:read? reinstall it with the manifest\'s scopes)');
        throw refusal('unsupported');
      }
      throw new Error(`slack answered ${response.status} for a file`);
    }
  };

  /** The message's files into `resolved` (kept on the held message across retries, so a
   *  stored file is never fetched twice). The first file always goes through storeMedia, which
   *  checks the sender before anything else: one this channel refuses itself goes with an
   *  `open` that throws that refusal (nothing is fetched), so a stranger's message whose files
   *  were all refused here still goes in without attachments. Later files this channel
   *  refuses go in without storeMedia (the sender is known bound by then); the rest are stored
   *  one by one. Returns 'unbound' when storeMedia answers channel_sender_not_bound (the rest
   *  are not fetched); throws when an answer is transient. */
  async function storeAttachments(message, files, resolved) {
    for (let index = resolved.length; index < files.length; index++) {
      const file = files[index];
      const kind = slackFileKind(file?.mimetype);
      const refused = slackFileRefusal(file);
      if (refused && index > 0) { resolved.push({ kind, refused }); continue; }
      // A host without storeMedia (before 24.2) cannot take a file: unsupported, never held.
      if (typeof ownerChannels.storeMedia !== 'function') { resolved.push({ kind, refused: refused ?? 'unsupported' }); continue; }
      const meta = { channel: 'slack', senderRef: message.senderRef, messageRef: message.messageRef, index, kind,
        // Step 23: a group question's files are stored for its channel, and it called the steward.
        ...(message.conversationRef ? { conversationRef: message.conversationRef, asked: true } : {}),
        ...(typeof file?.mimetype === 'string' && file.mimetype ? { mime: file.mimetype } : {}),
        ...(typeof file?.name === 'string' && file.name ? { name: file.name } : {}) };
      const answer = await ownerChannels.storeMedia(meta, refused ? async () => { throw refusal(refused); } : openFile(file));
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
  // messages already waiting (the host down, a stranger flooding) gets no more held. A group's
  // messages (step 23) have one lane per channel, apart from every sender's DMs. `prepare` runs
  // in the lane and gives the message and its files, or nothing (the message is not taken).
  function enqueue({ key, who, length }, prepare) {
    const lane = lanes.get(key) ?? { run: Promise.resolve(), held: 0 };
    if (lane.held >= HELD_PER_SENDER) {
      log.warn(`slack: inbound from ${who} (${length} chars) not held: `
        + `${HELD_PER_SENDER} messages from this sender are already waiting`);
      return lane.run;
    }
    lane.held++;
    lane.run = lane.run.then(async () => {
      const ready = await prepare();
      if (ready) await deliver(ready.message, ready.files);
    }).catch(error => log.warn(`slack: inbound from ${who} (${length} chars) dropped: ${short(error)}`)).finally(() => {
      if (--lane.held === 0 && lanes.get(key) === lane) lanes.delete(key);
    });
    lanes.set(key, lane);
    return lane.run;
  }

  // ---- step 23: private channels as steward groups (design-steward 7.7.19) ----

  /** What `fn` answers, asked again with a growing pause while it throws (Slack or the network
   *  unreachable, a rate limit); undefined once the channel stops. */
  async function persist(what, fn) {
    let pause = retryMs;
    for (let attempt = 1; !stopping.signal.aborted; attempt++) {
      try { return await fn(); } catch (error) {
        log.warn(`slack: ${what} failed (attempt ${attempt}): ${short(error)}; retrying`);
        await sleep(pause, stopping.signal);
        pause = Math.min(pause * 2, maxRetryMs);
      }
    }
    return undefined;
  }

  /** The bot's own user id: the envelope's (`authorizations`), else auth.test, asked once. */
  function botOf(body) {
    const named = body?.authorizations?.[0]?.user_id;
    if (typeof named === 'string' && BOT_ID.test(named)) return Promise.resolve(named);
    botAsked ??= web.auth.test().then(answer => {
      if (!BOT_ID.test(String(answer?.user_id ?? ''))) throw new Error('auth.test named no bot user');
      return answer.user_id;
    }).catch(error => { botAsked = undefined; throw error; });
    return botAsked;
  }

  /** 'ok', or why this conversation cannot be a group (groupRefusal), as Slack says now. A
   *  conversation Slack will not describe to the app (INFO_REFUSALS) is named by Slack's code; a
   *  call that failed otherwise throws (asked again, or the send fails). */
  async function judge(room) {
    let info;
    try {
      info = (await web.conversations.info({ channel: room }))?.channel;
    } catch (error) {
      const code = error?.code === 'slack_webapi_platform_error' ? error?.data?.error : undefined;
      if (!INFO_REFUSALS.has(code)) throw error;
      const provided = String(error.data.provided ?? '').split(',').map(scope => scope.trim());
      return code === 'missing_scope' && provided.includes('groups:read') ? 'not_private' : code;
    }
    return groupRefusal(info) ?? 'ok';
  }

  function suspend(room, why) {
    if (!rooms.has(room) || paused.has(room)) return;
    paused.set(room, why);
    log.warn(`slack: group ${room} paused: ${why} - nothing more is taken from it or sent to it; ${remedyOf(why)}`);
  }

  /** Until the host's first room list, or ROOMS_WAIT_MS after the first mention that waited for
   *  it (said once), or the channel stops. */
  function roomsOrLate() {
    roomsWait ??= new Promise(resolve => {
      const done = () => { clearTimeout(timer); stopping.signal.removeEventListener('abort', done); resolve(); };
      const timer = setTimeout(() => {
        if (!roomsKnown) log.warn(`slack: no group list from the host in ${Math.round(roomsWaitMs / 1000)} s; `
          + 'mentions are judged with no group bound until it comes');
        done();
      }, roomsWaitMs);
      timer.unref?.();
      stopping.signal.addEventListener('abort', done, { once: true });
      void listed.then(done);
    });
    return roomsWait;
  }

  /** A send into `room` may go: it is bound and not paused, and Slack still says it is a private
   *  channel shared with no one (else it is paused now). Throws the bridge's refusal shapes. */
  async function usable(room) {
    if (!rooms.has(room)) throw Object.assign(new Error('slack: that channel is not a bound group'), { status: 409, code: 'room_not_bound' });
    if (!paused.has(room)) {
      const verdict = await judge(room);
      if (verdict !== 'ok') suspend(room, verdict);
    }
    if (paused.has(room)) throw Object.assign(new Error('slack: that group is paused'), { status: 409, code: 'room_paused' });
  }

  /** One mention of the bot in a conversation. In a bound channel: a group question (or a file
   *  with it), unless the channel is paused or is found shared or public now - then it is paused.
   *  Anywhere else: a bind, only when the bound account wrote nothing but a group code and the
   *  conversation is a private channel shared with no one; else nothing, and nothing is answered. */
  function onMention(event, body) {
    const room = event.channel, messageRef = `${room}:${event.ts}`;
    const files = Array.isArray(event.files) ? event.files.slice(0, FILES_PER_MESSAGE) : [];
    const shared = body?.is_ext_shared_channel === true;
    const at = atOf(event.ts);
    return enqueue({ key: 'group:' + room, who: `${event.user} in ${room}`, length: event.text.length }, async () => {
      if (!roomsKnown) await roomsOrLate();
      if (stopping.signal.aborted) return undefined;
      // A code message passed on as a bind is never taken again, as a bind or as a question.
      if (binds.has(messageRef)) return undefined;
      const bot = await persist('reading the bot\'s own id', () => botOf(body));
      if (!bot) return undefined;
      const text = unescapeSlack(withoutMention(event.text, bot));
      if (!rooms.has(room)) {
        const code = !files.length && ownerRef && event.user === ownerRef && GROUP_CODE.test(text) ? text : undefined;
        if (!code) return undefined;
        const verdict = shared ? 'shared' : await persist(`checking ${room}`, () => judge(room));
        if (verdict === undefined) return undefined;
        if (verdict !== 'ok') { log.warn(`slack: a group code in ${room} not taken: ${verdict}`); return undefined; }
        binds.add(messageRef);
        while (binds.size > BINDS_KEPT) binds.delete(binds.values().next().value);
        remember(messageRef, event); // the bind notice answers it in its thread
        return { files: [], message: { channel: 'slack', senderRef: event.user, messageRef, text: code, at,
          conversationRef: room, groupBind: { prefix: `<@${bot}>`, code } } };
      }
      if (paused.has(room)) return undefined;
      if (shared) { suspend(room, 'shared'); return undefined; }
      if (!text && !files.length) return undefined;
      const verdict = await persist(`checking ${room}`, () => judge(room));
      if (verdict === undefined) return undefined;
      if (verdict !== 'ok') { suspend(room, verdict); return undefined; }
      remember(messageRef, event);
      return { files, message: { channel: 'slack', senderRef: event.user, messageRef, text, at, conversationRef: room, asked: true } };
    });
  }

  // The thread a group message was written in (its root ts; its own ts when it is the root).
  function remember(messageRef, event) {
    threads.set(messageRef, typeof event.thread_ts === 'string' && event.thread_ts ? event.thread_ts : event.ts);
    while (threads.size > THREADS_KEPT) threads.delete(threads.keys().next().value);
  }

  async function onEnvelope({ ack, type, body }) {
    // Every envelope is acknowledged, first: Slack redelivers an unacknowledged one after
    // 3 s, and handing a message to the service can take longer than that.
    try { await ack(); } catch (error) { log.warn(`slack: ack failed: ${error?.message ?? error}`); }
    const event = type === 'events_api' ? body?.event : undefined;
    if (isGroupMention(event)) return onMention(event, body);
    if (!isOwnerDm(event)) return;
    const messageRef = event.client_msg_id || event.ts;
    const files = Array.isArray(event.files) ? event.files.slice(0, FILES_PER_MESSAGE) : [];
    const message = { channel: 'slack', senderRef: event.user, messageRef, text: unescapeSlack(event.text ?? ''), at: atOf(event.ts) };
    return enqueue({ key: event.user, who: event.user, length: message.text.length }, async () => ({ message, files }));
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

  /** Step 23: one line into a bound group (a private channel). In the thread of the question it
   *  answers (`replyToRef`, a group messageRef `<channel>:<ts>` of this channel: D8), else into
   *  the channel; unfurls off; escaped like every text, except the bot's own mention (the group's
   *  prefix), which the confirmation line asks people to write. Refused room_not_bound or
   *  room_paused (409, before anything is posted). The ref is `<channel>:<ts>` of the first part. */
  async function sendGroup(room, text, { replyToRef } = {}) {
    if (!configured) throw new Error(`slack channel ${state.detail}`);
    await usable(room);
    const ts = typeof replyToRef === 'string' && replyToRef.startsWith(room + ':') ? replyToRef.slice(room.length + 1) : '';
    const thread = ts ? threads.get(replyToRef) ?? ts : undefined;
    const mention = rooms.get(room)?.prefix;
    const refs = [];
    for (const part of splitText(text)) {
      const escaped = escapeSlack(part);
      const posted = await web.chat.postMessage({ channel: room,
        text: OWN_MENTION.test(mention) ? escaped.split(escapeSlack(mention)).join(mention) : escaped,
        ...(thread ? { thread_ts: thread } : {}), unfurl_links: false, unfurl_media: false });
      refs.push(posted.ts);
    }
    return { ref: `${room}:${refs[0]}` };
  }

  /** Step 23: the host's whitelist, set whole: the bound account (whose group code alone binds)
   *  and each bound group with its prefix and name (`label`, what the dashboard calls it, so a
   *  paused one is named in the status). A group no longer listed (unbound) is no longer paused. */
  async function setRooms({ ownerRef: owner, rooms: list } = {}) {
    ownerRef = typeof owner === 'string' && owner ? owner : undefined;
    rooms = new Map((Array.isArray(list) ? list : []).filter(room => typeof room?.room_ref === 'string' && room.room_ref)
      .map(room => [room.room_ref, { prefix: typeof room.prefix === 'string' ? room.prefix : '',
        label: typeof room.label === 'string' && room.label.trim() ? room.label.trim() : undefined }]));
    for (const room of [...paused.keys()]) if (!rooms.has(room)) paused.delete(room);
    roomsKnown = true;
    roomsListed();
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
   *  old files.upload was retired 2025-11-12); a caption goes as its initial comment. Step 23: a
   *  `groupRef` sends into that bound group's channel instead, held to sendGroup's checks
   *  (room_not_bound, room_paused). The ref is the Slack file id. */
  async function sendFile({ to, groupRef, kind, name, path, caption } = {}) {
    if (!configured) throw new Error(`slack channel ${state.detail}`);
    if (!MEDIA_KINDS.includes(kind)) throw Object.assign(new Error('slack: cannot send this file here'), { code: 'channel_no_media' });
    if (groupRef !== undefined) await usable(groupRef);
    else if (typeof to !== 'string' || to === '') throw Object.assign(new Error('slack: no recipient named'), { code: 'recipient_unknown' });
    const { size } = await stat(path);
    if (size > MEDIA_LIMITS.outbound_bytes) throw Object.assign(new Error('slack: file over the outbound bound'), { code: 'too_large' });
    const channel_id = groupRef !== undefined ? groupRef : await dmOf(to);
    const result = await web.filesUploadV2({ channel_id, file: await readFile(path),
      filename: typeof name === 'string' && name ? name : 'file',
      ...(typeof caption === 'string' && caption.trim() ? { initial_comment: escapeSlack(caption) } : {}) });
    const id = result?.files?.[0]?.files?.[0]?.id;
    return typeof id === 'string' && id ? { ref: id } : {};
  }

  // A paused group (step 23) is said in the channel's status, which the dashboard shows, by the
  // name the dashboard gives it (its label, else its ref's last six) and what to do about it, so the
  // owner knows which to unbind and which needs the app put right; connected or not, after why not.
  const status = () => {
    if (!paused.size) return { ...state };
    const count = paused.size, byRemedy = new Map();
    for (const [room, why] of paused)
      byRemedy.set(remedyOf(why), [...byRemedy.get(remedyOf(why)) ?? [], `「${rooms.get(room)?.label ?? room.slice(-6)}」 ${why}`]);
    const line = `${count} group${count === 1 ? '' : 's'} paused: `
      + [...byRemedy].map(([remedy, named]) => `${named.join(', ')} - ${remedy}`).join('; ');
    return { ...state, detail: state.detail ? `${state.detail}; ${line}` : line };
  };
  const stop = async () => {
    if (stopped) return;
    stopped = true;
    stopping.abort();
    clearTimeout(retryTimer);
    retryTimer = undefined;
    state = { connected: false, detail: 'disconnected' };
    await socket?.disconnect?.().catch(error => log.warn(`slack: disconnect failed: ${error?.message ?? error}`));
  };
  return { start, stop, send, sendFile, sendGroup, setRooms, status, onEnvelope };
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
    sendFile: channel.sendFile, mediaKinds: [...MEDIA_KINDS], sendGroup: channel.sendGroup, setRooms: channel.setRooms });
  void channel.start();
  ctx.on('dispose', () => { unregister?.(); return channel.stop(); });
}
