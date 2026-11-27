/** The built-in local host's owner chat channels (host.d.ts `channels`, design-steward 7.2), on
 *  plain files: what lets dsh-channel-slack and dsh-channel-wechat load next to this host as they
 *  do next to a backend's. Only for a steward root (`channels: true` in director-local's config).
 *  lib/channels.mjs does the channel side (receipts, the owed store, mirroring); this file is the
 *  host side it calls - who the owner's account is, what a message from it becomes, and where a
 *  reply went - with the backend's rules where a local file can keep them.
 *
 *  Files under `$ROOT/channels/` (each JSON file written whole: a temporary file, then a rename;
 *  every file mode 0600, since they name the owner's accounts):
 *    channels.json  { bindings: { <channel>: { sender_ref, bound_at, revoked_at } }, default_channel,
 *                   bind_attempts: [{ channel, sender_ref, channel_message_ref, outcome, at }] } -
 *                   the attempts (the newest BIND_ATTEMPTS_KEPT per channel) answer a binding
 *                   message delivered again as the duplicate it is
 *    messages.jsonl one line per channel message, append-only: an owner message relayed in
 *                   ({ message_id, direction: 'in', channel, sender_ref, channel_message_ref, at,
 *                   channel_at?, attachments?, approval? }) and where a steward message went
 *                   ({ message_id, direction: 'out', channel, channel_message_ref, at, error })
 *    sent.jsonl     the steward's messages to its owner, append-only ({ message_id, reply_to, at,
 *                   request_key?, digest?, auto? }): which owner message each one answers
 *    status.json    { <channel>: { connected, detail, at } } as the channel plugin last reported it
 *    <channel>-qr.svg the login QR (WeChat) while one is waiting to be scanned; gone otherwise.
 *                   It never reaches the model: only the /channels command names the file.
 *
 *  A binding code lives in this process only (never on disk): /bind makes one, valid
 *  CODE_TTL_MS, single use, replacing the channel's earlier code; BIND_FAILURES_MAX wrong
 *  attempts while it is live retire it. A restart drops a code that was not used: /bind again.
 *  An owner message is written as a wake file in the root's inbox (`owner-message-<id>.json`)
 *  before it is recorded here, so a stop in between makes the redelivery write the same file
 *  again (its name is the message's id, derived from the channel message) instead of losing it. */
import { readFile, writeFile, appendFile, rename, mkdir, rm, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash, randomInt } from 'node:crypto';

export const CHANNEL_NAMES = ['slack', 'wechat'];
/** The backend's bounds (step 19), kept here: a code's life, wrong attempts per live code, the
 *  binding attempts kept per channel, a message's size, accepted messages per channel per minute
 *  and hour, and how old (or how far ahead) an approval phrase may be. */
export const CODE_TTL_MS = 10 * 60 * 1000;
export const BIND_FAILURES_MAX = 5;
export const BIND_ATTEMPTS_KEPT = 1000;
export const BODY_MAX_BYTES = 4096;
export const RATE_PER_MINUTE = 6;
export const RATE_PER_HOUR = 60;
export const APPROVAL_MAX_AGE_MS = 10 * 60 * 1000;
export const APPROVAL_CLOCK_SKEW_MS = 2 * 60 * 1000;
const CHANNEL_AT_MAX_AHEAD_MS = 2 * 60 * 1000;
const STATUS_DETAIL_MAX = 300;
const QR_SVG_MAX_BYTES = 64 * 1024;
/** The exact approval phrases (the backend's, Chinese and the fixed English alias): the whole
 *  message, trimmed at its ends only, is the phrase and an existing proposal's id. */
const PHRASES = [['同意 proposal ', 'approve'], ['拒绝 proposal ', 'decline'], ['approve proposal ', 'approve'], ['decline proposal ', 'decline']];
/** An owner message's wake file in the root's inbox (local.mjs delivers it). */
export const OWNER_MESSAGE_FILE = /^owner-message-(m-[0-9a-f]{20})\.json$/;

const text = value => typeof value === 'string' && value.length > 0;
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const digest = value => createHash('sha256').update(value).digest('hex');
const exists = path => stat(path).then(() => true, error => { if (error.code === 'ENOENT') return false; throw error; });

/** The digits a text carries, in order, every script's decimal digit written as ASCII (NFKC
 *  folds the compatibility forms; a digit of another script's decimal block is its place in
 *  that block of ten). Anything between the digits is dropped, so `711 833` is `711833`. */
function digitsOf(body) {
  let out = '';
  for (const char of String(body).normalize('NFKC')) {
    if (char >= '0' && char <= '9') { out += char; continue; }
    if (!/\p{Nd}/u.test(char)) continue;
    const point = char.codePointAt(0);
    let start = point;
    while (start > 0 && /\p{Nd}/u.test(String.fromCodePoint(start - 1))) start--;
    out += String((point - start) % 10);
  }
  return out;
}
/** Every six-digit run of a text's digits (digitsOf). */
const digitWindows = body => { const d = digitsOf(body); const out = new Set(); for (let i = 0; i + 6 <= d.length; i++) out.add(d.slice(i, i + 6)); return out; };

async function readJson(file, fallback) {
  try { return JSON.parse(await readFile(file, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return fallback; throw error; }
}
async function writePrivate(file, data) {
  await writeFile(file + '.tmp', data, { mode: 0o600 });
  await rename(file + '.tmp', file);
}
/** The rows of a JSON-lines file that parse as objects (a line edited by hand into anything
 *  else is skipped, never thrown on); none when the file is not there. */
async function readLines(file) {
  let raw = '';
  try { raw = await readFile(file, 'utf8'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  return raw.split('\n').flatMap(line => { try { const row = JSON.parse(line); return isRecord(row) ? [row] : []; } catch { return []; } });
}

/** @param root the local host's root; @param inbox its inbox directory (owner messages are wake
 *  files there); @param proposals (optional, with projectsDir) { list(): the proposals map,
 *  decide({id, approve, by, via}) }: an exact approval phrase decides through them;
 *  @param memory () => the owner's memory store or undefined: a relayed message and the steward's
 *  reply are recorded as one turn; @param log (line) => void; @param now () => ms. */
export function localChannels({ root, inbox, proposals, memory = () => undefined, log = () => {}, now = Date.now }) {
  const dir = join(root, 'channels');
  const files = { channels: join(dir, 'channels.json'), messages: join(dir, 'messages.jsonl'), sent: join(dir, 'sent.jsonl'),
    status: join(dir, 'status.json') };
  const qrFile = channel => join(dir, `${channel}-qr.svg`);
  // One write at a time: every operation reads, decides and writes as one step.
  let lock = Promise.resolve();
  const locked = fn => { const run = lock.then(fn); lock = run.catch(() => {}); return run; };
  // The binding codes, this process only: channel -> { code, issued, expires, used, failed, retired }.
  const codes = new Map();
  // messages.jsonl and sent.jsonl, read once, then kept in step with every append here.
  let loaded;
  const load = () => loaded ??= (async () => {
    const messages = new Map(), inbound = new Map(), sent = new Map(), replied = new Set();
    for (const row of await readLines(files.messages)) {
      if (!text(row.message_id) || messages.has(row.message_id)) continue;
      messages.set(row.message_id, row);
      if (row.direction === 'in') inbound.set(inKey(row.channel, row.sender_ref, row.channel_message_ref), row);
    }
    for (const row of await readLines(files.sent)) {
      if (!text(row.message_id) || sent.has(row.message_id)) continue;
      sent.set(row.message_id, row);
      if (text(row.reply_to)) replied.add(row.reply_to);
    }
    return { messages, inbound, sent, replied };
  })().catch(error => { loaded = undefined; throw error; });
  const inKey = (channel, senderRef, messageRef) => `${channel}\n${senderRef}\n${messageRef}`;
  const append = async (file, row) => {
    await mkdir(dir, { recursive: true });
    await appendFile(file, JSON.stringify(row) + '\n', { mode: 0o600 });
  };
  const state = async () => {
    const stored = await readJson(files.channels, {});
    const value = isRecord(stored) ? stored : {};
    return { bindings: isRecord(value.bindings) ? value.bindings : {},
      default_channel: CHANNEL_NAMES.includes(value.default_channel) ? value.default_channel : null,
      bind_attempts: Array.isArray(value.bind_attempts) ? value.bind_attempts.filter(isRecord) : [] };
  };
  const saveState = async value => { await mkdir(dir, { recursive: true }); await writePrivate(files.channels, JSON.stringify(value, null, 2) + '\n'); };
  const boundRow = (value, channel) => {
    const row = value.bindings[channel];
    return isRecord(row) && text(row.sender_ref) && !row.revoked_at ? row : undefined;
  };
  const isBound = (value, channel, senderRef) => boundRow(value, channel)?.sender_ref === senderRef;
  const live = channel => {
    const entry = codes.get(channel);
    return entry && !entry.used && !entry.retired && entry.expires > now() ? entry : undefined;
  };
  /** The live code (and its channel) a text carries anywhere in it, or undefined. */
  const codeIn = body => {
    const windows = digitWindows(body);
    if (!windows.size) return undefined;
    for (const channel of CHANNEL_NAMES) { const entry = live(channel); if (entry && windows.has(entry.code)) return { channel, entry }; }
    return undefined;
  };
  const attemptOf = (value, channel, senderRef, messageRef) => value.bind_attempts
    .find(a => a.channel === channel && a.sender_ref === senderRef && a.channel_message_ref === messageRef)?.outcome;
  const recordAttempt = (value, channel, senderRef, messageRef, outcome) => {
    value.bind_attempts.push({ channel, sender_ref: senderRef, channel_message_ref: messageRef, outcome, at: new Date(now()).toISOString() });
    const mine = value.bind_attempts.filter(a => a.channel === channel);
    if (mine.length > BIND_ATTEMPTS_KEPT) {
      const drop = new Set(mine.slice(0, mine.length - BIND_ATTEMPTS_KEPT));
      value.bind_attempts = value.bind_attempts.filter(a => !drop.has(a));
    }
  };
  const badChannel = channel => CHANNEL_NAMES.includes(channel) ? undefined : { refused: 'invalid_channel', reason: `channel must be one of ${CHANNEL_NAMES.join(', ')}` };

  /** An exact approval phrase naming an existing proposal: {action, proposal_id, phrase}. */
  const matchPhrase = async body => {
    if (!proposals) return undefined;
    const phrase = body.trim();
    for (const [prefix, action] of PHRASES) {
      if (!phrase.startsWith(prefix)) continue;
      const id = phrase.slice(prefix.length);
      const all = await proposals.list();
      if (isRecord(all) && Object.hasOwn(all, id) && isRecord(all[id])) return { action, proposal_id: id };
    }
    return undefined;
  };
  /** The decision a relayed phrase carries, as the channel's line reads it (host.d.ts relay
   *  `approval`). A proposal this very message decided already (a stop came between the decision
   *  and its record) is answered as that decision. */
  const decision = async (matched, { channel, senderRef, messageRef, writtenAt }) => {
    const out = { proposal_id: matched.proposal_id, action: matched.action };
    if (writtenAt === undefined) return { ...out, outcome: 'refused', refusal: 'approval_undated' };
    if (now() - writtenAt > APPROVAL_MAX_AGE_MS) return { ...out, outcome: 'refused', refusal: 'approval_too_old' };
    if (writtenAt - now() > APPROVAL_CLOCK_SKEW_MS) return { ...out, outcome: 'refused', refusal: 'approval_clock_skew' };
    const via = inKey(channel, senderRef, messageRef);
    const executed = record => ({ ...out, outcome: 'executed', kind: record.kind, target: record.payload?.project_id ?? null,
      result: { status: record.status, ...(record.result ? { result: record.result } : {}) } });
    const result = await proposals.decide({ id: matched.proposal_id, approve: matched.action === 'approve', by: `owner:${channel}:${senderRef}`, via });
    if (!result?.refused) return executed(result);
    const current = (await proposals.list())?.[matched.proposal_id];
    if (isRecord(current) && current.decided_via === via && current.status !== 'executing') return executed(current);
    return { ...out, outcome: 'refused', refusal: result.refused === 'not_open' ? 'proposal_not_open' : String(result.refused) };
  };

  const ops = {
    /** One owner message from a channel: the wake file, its memory turn, the decision an exact
     *  approval phrase carries, then its record. Idempotent per (channel, sender, message ref). */
    relay: ({ channel, senderRef, messageRef, text: body, at, attachments }) => locked(async () => {
      const wrong = badChannel(channel);
      if (wrong) return wrong;
      if (typeof body !== 'string' || !body.trim()) return { refused: 'empty_body' };
      if (Buffer.byteLength(body) > BODY_MAX_BYTES) { log(`channel message refused: body_too_long channel=${channel} bytes=${Buffer.byteLength(body)}`); return { refused: 'body_too_long' }; }
      if (!text(senderRef) || !text(messageRef)) return { refused: 'invalid_request' };
      let writtenAt;
      if (at !== undefined && at !== null && at !== '') {
        writtenAt = Date.parse(String(at));
        if (Number.isNaN(writtenAt)) return { refused: 'invalid_request', reason: 'at must be an ISO 8601 time' };
      }
      const seen = await load();
      const first = seen.inbound.get(inKey(channel, senderRef, messageRef));
      if (first) return { message_id: first.message_id, duplicate: true, approval: first.approval ?? null };
      const value = await state();
      // A binding message delivered again after it bound its sender: never an owner message.
      if (attemptOf(value, channel, senderRef, messageRef) !== undefined) return { message_id: null, duplicate: true, approval: null };
      if (!isBound(value, channel, senderRef)) {
        log(`channel message refused: channel_sender_not_bound channel=${channel} sender_ref=${senderRef} bytes=${Buffer.byteLength(body)}`);
        return { refused: 'channel_sender_not_bound' };
      }
      // A live binding code sent from the bound account is never written anywhere: it is retired.
      const code = codeIn(body);
      if (code) {
        code.entry.retired = now();
        recordAttempt(value, channel, senderRef, messageRef, 'code_sent_to_bound_account');
        await saveState(value);
        log(`channel binding code retired: sent as a message from the bound account channel=${channel} code_channel=${code.channel}`);
        return { refused: 'code_sent_to_bound_account' };
      }
      const moment = now();
      const recent = [...seen.inbound.values()].filter(row => row.channel === channel);
      for (const [window, limit] of [[60 * 1000, RATE_PER_MINUTE], [3600 * 1000, RATE_PER_HOUR]]) {
        if (recent.filter(row => Date.parse(row.at) > moment - window).length >= limit) {
          log(`channel message refused: rate_limited channel=${channel}`);
          return { refused: 'rate_limited' };
        }
      }
      const id = 'm-' + digest(inKey(channel, senderRef, messageRef)).slice(0, 20);
      const createdAt = new Date(moment).toISOString();
      // The channel's own time, unless it is ahead of this machine's by more than the bound.
      const channelAt = writtenAt !== undefined && writtenAt - moment <= CHANNEL_AT_MAX_AHEAD_MS ? new Date(writtenAt).toISOString() : undefined;
      const attached = Array.isArray(attachments) && attachments.length ? attachments : undefined;
      // The wake first (named by the message id: written again, never twice, by a redelivery).
      const wake = `owner-message-${id}.json`;
      if (!await exists(join(inbox, wake)) && !await exists(join(inbox, 'done', wake))) {
        await mkdir(inbox, { recursive: true });
        await writePrivate(join(inbox, wake), JSON.stringify({ message_id: id, source_channel: channel, created_at: createdAt,
          ...(channelAt ? { channel_at: channelAt } : {}), text: body, ...(attached ? { attachments: attached } : {}) }) + '\n');
      }
      try { await memory()?.recordTurn(id, [{ role: 'owner', part: 'o', at: createdAt, channel, ...(channelAt ? { channel_at: channelAt } : {}), text: body }]); }
      catch (error) { log('memory turn: ' + String(error?.message ?? error).slice(0, 300)); }
      const matched = await matchPhrase(body);
      const approval = matched ? await decision(matched, { channel, senderRef, messageRef, writtenAt }) : null;
      const row = { message_id: id, direction: 'in', channel, sender_ref: senderRef, channel_message_ref: messageRef, at: createdAt,
        ...(channelAt ? { channel_at: channelAt } : {}), ...(attached ? { attachments: attached } : {}), ...(approval ? { approval } : {}) };
      await append(files.messages, row);
      seen.messages.set(id, row);
      seen.inbound.set(inKey(channel, senderRef, messageRef), row);
      return { message_id: id, duplicate: false, approval };
    }),
    /** Bind `senderRef` with the channel's live code (single use; a duplicate delivery of the
     *  same binding message answers its first outcome and counts nothing). */
    bind: ({ channel, senderRef, code, messageRef }) => locked(async () => {
      const wrong = badChannel(channel);
      if (wrong) return wrong;
      if (!text(senderRef)) return { refused: 'invalid_request' };
      const value = await state();
      const first = text(messageRef) ? attemptOf(value, channel, senderRef, messageRef) : undefined;
      if (first !== undefined) return { bound: first === 'bound', channel, sender_ref: senderRef, duplicate: true, ...(first === 'bound' ? {} : { refusal: first }) };
      const entry = codes.get(channel);
      // The channel's unused code, retired or not, while it has not expired: what a wrong guess counts against.
      const pending = entry && !entry.used && entry.expires > now() ? entry : undefined;
      let refusal;
      if (pending?.retired) refusal = 'code_retired';
      else if (!entry || !/^[0-9]{6}$/.test(String(code)) || entry.code !== code) refusal = 'invalid_code';
      else if (entry.used) refusal = 'code_used';
      else if (entry.expires <= now()) refusal = 'code_expired';
      if (text(messageRef)) recordAttempt(value, channel, senderRef, messageRef, refusal ?? 'bound');
      if (refusal) {
        if (pending && !pending.retired && ++pending.failed >= BIND_FAILURES_MAX) {
          pending.retired = now();
          log(`channel binding code retired after ${pending.failed} refused attempts: channel=${channel}`);
        }
        await saveState(value);
        return { refused: refusal };
      }
      entry.used = now();
      value.bindings[channel] = { sender_ref: senderRef, bound_at: new Date(now()).toISOString(), revoked_at: null };
      await saveState(value);
      log(`channel bound: channel=${channel} sender_ref=${senderRef}`);
      return { bound: true, channel, sender_ref: senderRef };
    }),
    async sourceOf(messageId) {
      const row = (await load()).messages.get(messageId);
      if (!row) return null;
      return { direction: row.direction, channel: row.channel, sender_ref: row.sender_ref ?? null,
        channel_message_ref: row.channel_message_ref ?? null, at: row.at, error: row.error ?? null, ...(row.channel_at ? { channel_at: row.channel_at } : {}) };
    },
    /** Where one of the steward's messages went (or why not): once per message. */
    recordDelivery: ({ messageId, channel, channelMessageRef, error }) => locked(async () => {
      const wrong = badChannel(channel);
      if (wrong) return wrong;
      const seen = await load();
      if (!seen.sent.has(messageId)) return { refused: 'unknown_message', reason: 'not a message this steward sent its owner' };
      if (seen.messages.has(messageId)) return { refused: 'delivery_recorded' };
      const row = { message_id: messageId, direction: 'out', channel, channel_message_ref: text(channelMessageRef) ? channelMessageRef : null,
        at: new Date(now()).toISOString(), error: text(error) ? String(error).slice(0, 500) : null };
      await append(files.messages, row);
      seen.messages.set(messageId, row);
      return { recorded: true, message_id: messageId, channel };
    }),
    async list() {
      const value = await state();
      return { channels: CHANNEL_NAMES.map(channel => {
        const row = isRecord(value.bindings[channel]) ? value.bindings[channel] : undefined;
        return { channel, bound: !!boundRow(value, channel), sender_ref: row?.sender_ref ?? null, bound_at: row?.bound_at ?? null,
          revoked_at: row?.revoked_at ?? null, is_default: value.default_channel === channel };
      }), default_channel: value.default_channel };
    },
    /** May the channel client keep this text in its owed store? A bound sender, and no live code in it. */
    async codeCheck({ channel, senderRef, text: body }) {
      if (badChannel(channel)) return { store_text: false };
      return { store_text: isBound(await state(), channel, senderRef) && !codeIn(String(body ?? '')) };
    },
    async senderBound({ channel, senderRef }) {
      if (badChannel(channel)) return { bound: false };
      return { bound: isBound(await state(), channel, senderRef) };
    },
    /** The channel's status into status.json; a login QR into <channel>-qr.svg (0600) while it is
     *  not connected. Neither the detail nor the QR is logged. */
    reportStatus: ({ channel, connected, detail, qrSvg }) => locked(async () => {
      const wrong = badChannel(channel);
      if (wrong) return wrong;
      if (detail !== undefined && detail !== null && (typeof detail !== 'string' || detail.length > STATUS_DETAIL_MAX)) return { refused: 'detail_too_long' };
      const qr = text(qrSvg) && connected !== true ? qrSvg : undefined;
      if (qr && (Buffer.byteLength(qr) > QR_SVG_MAX_BYTES)) return { refused: 'qr_svg_too_large' };
      if (qr && !plainSvg(qr)) return { refused: 'qr_svg_refused' };
      await mkdir(dir, { recursive: true });
      const at = new Date(now()).toISOString();
      const all = await readJson(files.status, {}).catch(() => ({}));
      const next = isRecord(all) ? all : {};
      next[channel] = { connected: connected === true, ...(text(detail) ? { detail } : {}), at };
      await writePrivate(files.status, JSON.stringify(next, null, 2) + '\n');
      if (qr) await writePrivate(qrFile(channel), qr);
      else await rm(qrFile(channel), { force: true });
      return { recorded: true, channel, connected: connected === true, at };
    }),
  };

  /** The owner's commands' side (local-commands.mjs: /bind, /channels, /unbind, /default). */
  const owner = {
    /** A new code for `channel`: the earlier one of that channel stops working. */
    issueCode(channel) {
      const wrong = badChannel(channel);
      if (wrong) return wrong;
      const code = String(randomInt(0, 1000000)).padStart(6, '0');
      const issued = now();
      codes.set(channel, { code, issued, expires: issued + CODE_TTL_MS, used: undefined, failed: 0, retired: undefined });
      return { code, expires_at: new Date(issued + CODE_TTL_MS).toISOString() };
    },
    unbind: channel => locked(async () => {
      const wrong = badChannel(channel);
      if (wrong) return wrong;
      const value = await state();
      const row = boundRow(value, channel);
      if (!row) return { unbound: false, channel };
      value.bindings[channel] = { ...row, revoked_at: new Date(now()).toISOString() };
      await saveState(value);
      return { unbound: true, channel, sender_ref: row.sender_ref };
    }),
    setDefault: channel => locked(async () => {
      if (channel !== null && badChannel(channel)) return badChannel(channel);
      const value = await state();
      value.default_channel = channel;
      await saveState(value);
      return { default_channel: channel };
    }),
    /** Everything /channels shows: the bindings, the default, each channel's status, a code still
     *  waiting (its expiry only) and the QR file when there is one. */
    async view() {
      const value = await state();
      let status = {};
      try { const read = await readJson(files.status, {}); status = isRecord(read) ? read : {}; }
      catch (error) { status = { error: String(error?.message ?? error).slice(0, 200) }; }
      return { default_channel: value.default_channel, status_error: status.error, channels: await Promise.all(CHANNEL_NAMES.map(async channel => ({
        channel, binding: isRecord(value.bindings[channel]) ? value.bindings[channel] : null, bound: !!boundRow(value, channel),
        status: isRecord(status[channel]) ? status[channel] : null, qr: await exists(qrFile(channel)) ? qrFile(channel) : null,
        code_until: live(channel) ? new Date(live(channel).expires).toISOString() : null }))) };
    },
  };

  /** The steward's own messages to its owner (local.mjs: message_owner and the turn's last
   *  words), recorded before they are mirrored: recordDelivery takes only these. */
  const steward = {
    /** The owner message `id` relayed from a channel, or undefined. */
    async inbound(id) { const row = (await load()).messages.get(id); return row?.direction === 'in' ? row : undefined; },
    async sentRow(id) { return (await load()).sent.get(id); },
    /** Among `ids`, the owner messages from a channel that no steward message answers yet, oldest first. */
    async unanswered(ids) {
      const seen = await load();
      return ids.map(id => seen.messages.get(id)).filter(row => row?.direction === 'in' && !seen.replied.has(row.message_id))
        .sort((a, b) => String(a.at).localeCompare(String(b.at)) || a.message_id.localeCompare(b.message_id));
    },
    /** Record one steward message (once per id): {recorded} or {duplicate, row}. */
    record: row => locked(async () => {
      const seen = await load();
      const known = seen.sent.get(row.message_id);
      if (known) return { duplicate: true, row: known };
      const full = { ...row, at: new Date(now()).toISOString() };
      await append(files.sent, full);
      seen.sent.set(row.message_id, full);
      if (text(row.reply_to)) seen.replied.add(row.reply_to);
      return { recorded: true, row: full };
    }),
    /** The steward's words as part of the owner's turn in memory (the turn of the message they answer). */
    async remember(id, replyTo, body) {
      try { await memory()?.recordTurn(replyTo ?? id, [{ role: 'steward', part: id, text: body }]); }
      catch (error) { log('memory turn: ' + String(error?.message ?? error).slice(0, 300)); }
    },
  };
  return { ops, owner, steward, dir, qrFile };
}

/** A QR drawn as plain SVG: one <svg> of g, path and rect elements, with no script, link,
 *  entity, comment or event attribute (what a scanner needs, and nothing a browser runs). */
function plainSvg(svg) {
  const body = svg.trim();
  if (!/^<svg[\s>]/.test(body) || /<!|<\?|&/.test(body)) return false;
  for (const [, name] of body.matchAll(/<\/?\s*([A-Za-z][\w:.-]*)/g)) if (!['svg', 'g', 'path', 'rect'].includes(name)) return false;
  return !/\son[a-z]+\s*=|href\s*=|javascript:/i.test(body);
}
