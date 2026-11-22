/** wechat-bridge: one WeChat login kept alive in its own process, served over
 *  loopback HTTP to the steward's channel plugin (dsh-channel-wechat).
 *
 *  Why a separate process: puppet-neo keeps the login in memory only, so whoever
 *  holds it must outlive steward and host restarts, or every restart would need a
 *  new QR scan (design §7.2).
 *
 *  A login QR is asked of WeChat only on request (POST /login: the dashboard's button or
 *  the steward's tool, design §7.2 "按需二维码"), one at a time. A QR that expires or is
 *  cancelled, a logout and a puppet failure before a login stop the puppet and leave the
 *  bridge `idle` - the process stays up and asks WeChat for nothing until the next
 *  request. Each request builds a fresh bot (`makeBot`: a new Wechaty with a new
 *  puppet-neo, so a new session and QR) and an attempt that ends stops its own bot; no
 *  puppet state is shared between attempts, so a slow stop of an old one never touches
 *  the next, and whatever an old bot still emits is ignored.
 *
 *  This file is the HTTP layer, the event filter and the login's phases, with no
 *  dependency of its own: `makeBot()` returns a Wechaty instance (or a fake with the same
 *  events, start/stop and Contact.find) and `types` is Wechaty's `types`, both handed in
 *  by main.mjs, so the tests never load the real puppet. `renderQr` (qr-svg.mjs, also
 *  handed in) draws the login QR as an SVG for the dashboard. */
import { createServer } from 'node:http';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { createReadStream, rmSync } from 'node:fs';
import { open } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pipeline } from 'node:stream/promises';

// Loopback by default: the token is the only lock, so the socket must not be
// reachable from another machine. Inside its container the bridge has to listen on
// the container's interface (its loopback cannot be published); there, and only
// there, WECHAT_BRIDGE_BIND=0.0.0.0 is accepted, and the run script publishes the
// port on the host's 127.0.0.1 only.
export const HOST = '127.0.0.1';
export const CONTAINER_HOST = '0.0.0.0';
export const DEFAULT_PORT = 18731;
export const RING_SIZE = 500;
/** Step 23 (design-steward 7.7.11): the group path keeps its own ring, so a burst of @-messages
 *  while the host is away never pushes the owner's 1:1 messages out of theirs, and - once the
 *  host has named the bound account (`PUT /rooms` owner_ref) - anyone else's 1:1 messages go to
 *  a small stranger ring for the same reason. All rings share one `seq`. */
export const GROUP_RING_SIZE = 200;
export const STRANGER_RING_SIZE = 50;
/** The platform director's ruling of 2026-11-20 (design 7.7.10): the bounds this bridge
 *  enforces are code constants. Every group send waits at least sendGapMs after the previous
 *  one (one queue for all groups); a group past backstopPerHour sends in an hour is answered
 *  429 - a backstop against a bug, looser than the backend's own 60 + 1 + 1 per group and hour.
 *  The backend keeps the question quotas (core/state_groups.py GROUP_LIMITS) and the plugin its
 *  turn bounds (dsh-director/lib/channels.mjs GROUP_LIMITS); no key is in two places. */
export const GROUP_SEND_LIMITS = Object.freeze({ sendGapMs: 10_000, backstopPerHour: 90 });
// A group code as the owner posts it after the @ prefix: G-XXXX-XXXX (Crockford base32).
const GROUP_CODE = /^[Gg](?:[\s-]*[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]){8}$/;
const MENTION_GAP = /[\u2005 ]/;
export const MAX_WAIT_SECONDS = 25;
// WeChat cuts long texts in the client; splitting here keeps each part whole.
export const SEND_CHUNK = 1500;
const MAX_BODY_BYTES = 64 * 1024;
const MiB = 1024 * 1024;
/** Step 24.2 (design-steward 7.8.12 "微信"): the media bounds. A 1:1 image, voice or file
 *  whose size WeChat reports above its bound (image 20 MiB, file 25 MiB, the voice's SILK
 *  source 2 MiB) is described `too_large` and never fetched; what GET /media/:media_id
 *  actually receives is counted and cut at the same bound (image, file) or, for a voice the
 *  puppet hands over as WAV, at 2 min 5 s of audio by the WAV header's byte rate (at most
 *  voiceWavMax; voiceWavUnknown when the header says no rate). POST /send-file takes at most
 *  sendBody bytes - its own limit; POST /send keeps its 64 KiB. */
export const MEDIA_LIMITS = Object.freeze({ image: 20 * MiB, file: 25 * MiB, voiceSource: 2 * MiB, voiceMs: 125_000,
  voiceWavMax: 12 * MiB, voiceWavUnknown: 6 * MiB, sendBody: 25 * MiB });
const WAV_HEADER_SLACK = 4096;
/** The FileBox name decides what the fork sends (the platform director's interface): `.silk` a
 *  voice bar (the addon encodes), one of these an image, anything else a file attachment. */
export const IMAGE_EXTENSIONS = Object.freeze(['jpg', 'jpeg', 'png', 'gif', 'webp']);
const IMAGE_MIME_EXTENSIONS = { 'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp' };
const MEDIA_KINDS = ['image', 'voice', 'file'];
/** POST /login's bounds: at least minGapMs between two accepted requests, at most perHour
 *  in any hour and perDay in any 24 hours (counted in this process's memory: a restart starts
 *  `idle` and asks for nothing by itself; the director's ownerChannels.login keeps the same
 *  count on its side, so a bridge restart does not reset it there); a requested QR that has
 *  not appeared within qrWaitMs gives up (`idle`, login_failed), and one that appeared but
 *  was neither logged in with nor reported over within qrLifeMs ends here (`idle`,
 *  qr_expired) whatever the puppet still does - neo's own loop ends in about 2 minutes. */
export const LOGIN_LIMITS = Object.freeze({ minGapMs: 2 * 60_000, perHour: 5, perDay: 12, qrWaitMs: 30_000,
  qrLifeMs: 5 * 60_000 });
const HOUR_MS = 3_600_000, DAY_MS = 24 * HOUR_MS;

/** Texts of more than `size` characters, cut into consecutive parts of at most
 *  `size` code points (a surrogate pair is never split). */
export function splitText(text, size = SEND_CHUNK) {
  const points = Array.from(text);
  const parts = [];
  for (let i = 0; i < points.length; i += size) parts.push(points.slice(i, i + size).join(''));
  return parts.length ? parts : [''];
}

/** The address to listen on: `value` (WECHAT_BRIDGE_BIND) unset or 127.0.0.1 is
 *  loopback; 0.0.0.0 only when `inContainer`; anything else is refused. */
export function bindHost(value, inContainer) {
  if (!value || value === HOST) return HOST;
  if (value === CONTAINER_HOST && inContainer) return CONTAINER_HOST;
  throw new Error(`wechat-bridge refused: bind address ${JSON.stringify(value)} (only ${HOST}, or ${CONTAINER_HOST} inside the container)`);
}

const digest = value => createHash('sha256').update(String(value)).digest();
// An error as one log line (never a QR: neither neo nor Wechaty puts one in an error).
const short = error => String(error?.message ?? error?.data ?? error).slice(0, 200);

/** The room a message was posted in, or undefined for a 1:1 message: the message payload's
 *  own `roomId`, the field Wechaty's `message.room()` reads before it loads a Room. Never
 *  `message.room()` here: the id in the payload is all the ring needs, and loading a Room
 *  asks the puppet for a room payload it cannot supply. (The send path still loads one.) */
const roomOf = message => message.payload?.roomId || undefined;

// The raw message content the fork carries (base64 of WeChat's XML), as text, or undefined.
const contentXml = content => {
  if (typeof content !== 'string' || !content) return undefined;
  const text = Buffer.from(content, 'base64').toString('utf8');
  const start = text.indexOf('<');
  return start < 0 ? undefined : text.slice(start);
};
const xmlText = value => value.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, '&').trim();
const attribute = (xml, tag, name) => {
  const attributes = new RegExp(`<${tag}\\b([^>]*)>`).exec(xml)?.[1];
  return attributes && new RegExp(`\\b${name}\\s*=\\s*"([^"]*)"`).exec(attributes)?.[1];
};
const element = (xml, tag) => {
  const value = new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`).exec(xml)?.[1];
  return value === undefined ? undefined : xmlText(value);
};
const count = value => {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : undefined;
};

/** Step 24.2: what a 1:1 non-text message carries, as a description - {kind, mime?, name?,
 *  size?, too_large?} - or undefined when it is not one of the kinds 24.2 takes (design
 *  7.8.12 "收的白名单与引用回复"): an image (WeChat type 3), a voice (34) or a file (49 with
 *  appmsg type 6). Wechaty's type decides; the fork's raw `messageType`, when the payload has
 *  one, must agree, and a file whose XML the payload carries must say appmsg type 6 and quote
 *  nothing - a quoted reply (49/57) is never let in. Sizes and the file name are what WeChat's
 *  XML reports (not trusted: GET /media/:media_id counts the real bytes). */
export function mediaOf(message, types, limits = MEDIA_LIMITS) {
  const type = message.type();
  const kinds = [['image', types.Message?.Image, 3], ['voice', types.Message?.Audio, 34], ['file', types.Message?.Attachment, 49]];
  const found = kinds.find(([, value]) => value !== undefined && value === type);
  if (!found) return undefined;
  const [kind, , raw] = found;
  const payload = message.payload ?? {};
  if (payload.messageType !== undefined && payload.messageType !== raw) return undefined;
  const xml = contentXml(payload.content);
  if (kind === 'file' && xml !== undefined) {
    if (/<refermsg[\s>]/.test(xml) || element(xml.slice(xml.indexOf('<appmsg')), 'type') !== '6') return undefined;
  }
  const described = { kind };
  let size;
  if (xml !== undefined) {
    if (kind === 'image') size = count(attribute(xml, 'img', 'hdlength')) ?? count(attribute(xml, 'img', 'length'));
    if (kind === 'voice') size = count(attribute(xml, 'voicemsg', 'length'));
    if (kind === 'file') {
      size = count(element(xml, 'totallen'));
      const name = element(xml, 'title');
      if (name) described.name = name.slice(0, 255);
    }
  }
  // GET /media/:media_id hands a voice over as the puppet decoded it: WAV.
  if (kind === 'voice') described.mime = 'audio/wav';
  if (size !== undefined) described.size = size;
  const bound = kind === 'voice' ? limits.voiceSource : limits[kind];
  if (size !== undefined && size > bound) described.too_large = true;
  return described;
}

/** The byte rate a WAV header names (RIFF/WAVE, its `fmt ` chunk), or undefined. */
export function wavByteRate(head) {
  if (head.length < 12 || head.toString('latin1', 0, 4) !== 'RIFF' || head.toString('latin1', 8, 12) !== 'WAVE') return undefined;
  for (let at = 12; at + 8 <= head.length;) {
    const id = head.toString('latin1', at, at + 4), size = head.readUInt32LE(at + 4);
    if (id === 'fmt ') {
      if (at + 20 > head.length) return undefined;
      const rate = head.readUInt32LE(at + 16);
      return rate >= 1000 && rate <= 1_536_000 ? rate : undefined;
    }
    at += 8 + size + (size & 1);
  }
  return undefined;
}

/** How many bytes of WAV a voice may be: 2 min 5 s at the header's byte rate, plus the header. */
export function wavByteCap(head, limits = MEDIA_LIMITS) {
  const rate = wavByteRate(head);
  return rate ? Math.min(limits.voiceWavMax, WAV_HEADER_SLACK + Math.ceil(rate * limits.voiceMs / 1000)) : limits.voiceWavUnknown;
}

/** A voice's length from its WAV header and size, or undefined when it is not a WAV. */
export function wavDurationMs(bytes) {
  const rate = wavByteRate(bytes.subarray(0, 4096));
  return rate ? Math.round(Math.max(0, bytes.length - 44) * 1000 / rate) : undefined;
}

/** The default name of a voice's FileBox: its stem and `.silk` (the fork encodes and reads the
 *  length from the SILK stream). `createBridge({voiceFileName})` replaces it, e.g. with
 *  `voiceNameWithLength` should the fork read the length from the name. */
export const voiceFileName = ({ stem }) => `${stem}.silk`;
export const voiceNameWithLength = ({ stem, durationMs }) => Number.isInteger(durationMs) && durationMs > 0
  ? `${stem}-${durationMs}ms.silk` : `${stem}.silk`;

const cleanName = name => String(name ?? '').replace(/[\\/\u0000-\u001f\u007f]/g, '_').trim().slice(-120);
const splitName = name => {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? [name.slice(0, dot), name.slice(dot + 1).toLowerCase()] : [name, ''];
};
/** The FileBox names to send one file under, in order, each with what it goes out as: an image
 *  under an image extension (its own, or the one its mime names); a voice first as `.silk` (a
 *  voice bar), then - should that be refused or fail - the same bytes under its own name as a
 *  file; a file under its own name, with `.bin` added when that name would make the fork send
 *  it as an image or a voice bar. Undefined when an image has no image extension or mime. */
export function sendNames({ kind, mime, name, durationMs }, voiceName = voiceFileName) {
  const clean = cleanName(name) || kind;
  const [stem, extension] = splitName(clean);
  const asFile = IMAGE_EXTENSIONS.includes(extension) || extension === 'silk' ? `${clean}.bin` : clean;
  if (kind === 'image') {
    if (IMAGE_EXTENSIONS.includes(extension)) return [{ as: 'image', name: clean }];
    const fromMime = IMAGE_MIME_EXTENSIONS[String(mime ?? '').toLowerCase()];
    return fromMime ? [{ as: 'image', name: `${stem}.${fromMime}` }] : undefined;
  }
  if (kind === 'voice') return [{ as: 'voice', name: voiceName({ stem, durationMs }) }, { as: 'file', name: asFile }];
  return [{ as: 'file', name: asFile }];
}

/** Whether this WeChat message is one the owner could have written to the bot:
 *  a 1:1 text from someone else - or, step 24.2, a 1:1 image, voice or file (`mediaOf`).
 *  Rooms, the account's own messages (also those it
 *  sends from the phone) and official accounts are never relayed. Neo files a peer it
 *  heard from as contact type Unknown and one it has not seen as Individual, so official
 *  accounts are recognised by WeChat's `gh_` id prefix as well as by type. */
export function isOwnerCandidate(message, types, limits = MEDIA_LIMITS) {
  if (message.self() || roomOf(message)) return false;
  if (message.type() !== types.Message.Text && !mediaOf(message, types, limits)) return false;
  const talker = message.talker();
  if (!talker?.id || talker.id.startsWith('gh_')) return false;
  return talker.type() !== types.Contact.Official;
}

/** `loginEnabled` false (WECHAT_BRIDGE_SKIP_LOGIN=1) answers every POST /login
 *  `login_disabled`: no bot is ever built or started. */
/** A group message of a bound room that calls the bot: its text after `prefix` (NFKC, leading
 *  whitespace dropped, the prefix followed by U+2005 or a space), else undefined. */
export function calledText(text, prefix) {
  const folded = String(text ?? '').normalize('NFKC').replace(/^\s+/u, '');
  const head = String(prefix ?? '').normalize('NFKC');
  if (!head || !folded.startsWith(head) || !MENTION_GAP.test(folded.charAt(head.length))) return undefined;
  const rest = folded.slice(head.length).trim();
  return rest || undefined;
}

/** A message of a room that is not bound: {prefix, code} only when the WHOLE text is one @
 *  prefix and a group code, else undefined. */
export function groupBindAttempt(text) {
  const folded = String(text ?? '').normalize('NFKC').trim();
  if (!folded.startsWith('@')) return undefined;
  const cut = Math.max(folded.lastIndexOf('\u2005'), folded.lastIndexOf(' '));
  if (cut <= 1) return undefined;
  const prefix = folded.slice(0, cut).trimEnd(), code = folded.slice(cut + 1).trim();
  // "@name G-XXXX XXXX": the code may itself hold one space; take it back from the prefix.
  const spaced = /^(.*)[\u2005 ]([Gg][0-9A-Za-z-]{4,})$/u.exec(prefix);
  if (!GROUP_CODE.test(code) && spaced && GROUP_CODE.test(spaced[2] + ' ' + code))
    return { prefix: spaced[1].trimEnd(), code: spaced[2] + ' ' + code };
  return GROUP_CODE.test(code) && prefix.length > 1 ? { prefix, code } : undefined;
}

export function createBridge({ makeBot, types, token, ringSize = RING_SIZE, maxWaitSeconds = MAX_WAIT_SECONDS, now = () => new Date(),
  log = () => {}, renderQr = () => undefined, loginEnabled = true, loginLimits = LOGIN_LIMITS,
  groupRingSize = GROUP_RING_SIZE, strangerRingSize = STRANGER_RING_SIZE, sendLimits = GROUP_SEND_LIMITS,
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  mediaLimits = MEDIA_LIMITS, tmpDir = tmpdir(), makeFileBox, voiceName = voiceFileName }) {
  if (typeof token !== 'string' || !token.trim()) throw new Error('wechat-bridge refused: no token');
  if (typeof makeBot !== 'function') throw new Error('wechat-bridge refused: no makeBot');
  const expected = digest(token.trim());
  const ring = [], groupRing = [], strangerRing = [];
  /** Step 24.2 (design 7.8.12 "活消息存在哪"): media_id ('<seq>.<index>') -> {message, index,
   *  kind, generation} for each fetchable attachment of an event in a ring. An entry goes in
   *  just before its event is pushed and leaves when that event is cut off a ring's head
   *  (`push`), so it never outlives its event; a restart empties both. */
  const live = new Map();
  let fetching = false;
  // Step 23: the host's whitelist (PUT /rooms), in memory only - a restart starts empty and
  // the host sets it again: the bound account and each bound room with its @ prefix.
  let ownerRef, rooms = new Map();
  // Group sends: one queue, a gap between any two, and each room's sends of the last hour.
  let sendQueue = Promise.resolve(), lastGroupSend = 0;
  const roomSends = new Map();
  // Sequence numbers start at the boot time in ms: a cursor a client kept from
  // before a bridge restart is below every new number, so a restart never hides
  // new messages behind an old cursor (and needs no reset rule).
  let seq = now().getTime();
  let waiters = new Set();
  // phase: idle (no QR, not logged in, no bot running; idle_reason says why) |
  // starting (a QR was requested and has not appeared yet) | waiting_qr (a QR can be
  // scanned; the only phase with `qr`) | scanned (scanned, to be confirmed on the phone)
  // | logged_in.
  const state = { phase: 'idle', idle_reason: 'boot', logged_in: false, user_id: undefined, qr: undefined,
    qr_svg: undefined, since: now().toISOString() };
  const setState = change => Object.assign(state, { idle_reason: undefined }, change, { since: now().toISOString() });

  // The QR as an image, or undefined when it cannot be drawn (never logged: it is a login token).
  const drawn = qrcode => {
    try { return renderQr(qrcode) || undefined; }
    catch { log('the login QR could not be drawn'); return undefined; }
  };

  // `bot` is the current attempt's (undefined before the first request). `generation`
  // changes with every request and every end; each bot's events and each timer carry the
  // generation they were made for, so those of an earlier attempt do nothing. `tasks`
  // holds the starts and stops still running (settled() waits for them).
  let bot, generation = 0, qrTimer, lifeTimer;
  const accepted = [];
  const tasks = new Set();
  const track = promise => {
    const task = promise.catch(error => log('login step failed: ' + short(error))).finally(() => tasks.delete(task));
    tasks.add(task);
    return task;
  };
  const stopBot = old => track((async () => {
    try { await old.stop(); } catch (error) { log('stopping the puppet failed: ' + short(error)); }
  })());
  const loginLive = () => ['starting', 'waiting_qr', 'scanned'].includes(state.phase);
  const clearTimers = () => { clearTimeout(qrTimer); clearTimeout(lifeTimer); };
  /** This login attempt is over: idle with `reason`, and its bot stopped so nothing talks
   *  to WeChat until the next request. */
  const end = reason => {
    generation++;
    clearTimers();
    setState({ phase: 'idle', idle_reason: reason, logged_in: false, user_id: undefined, qr: undefined, qr_svg: undefined });
    log(`idle (${reason}): no QR until one is requested`);
    const old = bot;
    bot = undefined;
    if (old) void stopBot(old);
  };

  /** The event handlers of the bot built for attempt `mine`: an event of any other
   *  attempt's bot (one that ended, still stopping) is ignored, and within the attempt
   *  each event counts only in the phase it can follow. */
  const attach = (target, mine) => {
    const current = () => mine === generation;
    target.on('scan', (qrcode, status) => {
      if (!current()) return;
      if (status === types.ScanStatus.Waiting && qrcode) {
        if (state.phase !== 'starting' && state.phase !== 'waiting_qr') return;
        clearTimeout(qrTimer);
        // The QR's own bound, armed once per attempt: it ends a QR the puppet never reports over.
        if (state.phase === 'starting') {
          lifeTimer = setTimeout(() => { if (current() && loginLive()) end('qr_expired'); }, loginLimits.qrLifeMs);
          lifeTimer.unref?.();
        }
        setState({ phase: 'waiting_qr', logged_in: false, user_id: undefined, qr: qrcode, qr_svg: drawn(qrcode) });
        log('login QR waiting (GET /status carries it)');
      } else if (status === types.ScanStatus.Timeout || status === types.ScanStatus.Cancel) {
        // Only a waiting QR can be scanned; once timed out or cancelled it is gone.
        if (loginLive()) end(status === types.ScanStatus.Timeout ? 'qr_expired' : 'qr_cancelled');
      } else if ((status === types.ScanStatus.Scanned || status === types.ScanStatus.Confirmed)
        && (state.phase === 'waiting_qr' || state.phase === 'scanned')) {
        setState({ phase: 'scanned', logged_in: false, user_id: undefined, qr: undefined, qr_svg: undefined });
      }
    });
    target.on('login', user => {
      if (!current()) return;
      clearTimers();
      setState({ phase: 'logged_in', logged_in: true, user_id: user?.id, qr: undefined, qr_svg: undefined });
      log('logged in');
    });
    target.on('logout', () => {
      if (!current() || state.phase === 'idle') return;
      end('logged_out');
    });
    target.on('error', error => {
      log('error: ' + short(error));
      // An error while logged in is only logged (Wechaty reports message handling failures
      // that way); before a login it ends the attempt: neo then gives up its session.
      if (current() && loginLive()) end('login_failed');
    });
    target.on('message', message => { if (current()) relay(message); });
  };

  /** POST /login: [status, body]. Accepted only while idle; a refusal changes nothing. */
  function requestLogin() {
    if (!loginEnabled) return [409, { refused: 'login_disabled' }];
    if (state.phase === 'logged_in') return [409, { refused: 'already_logged_in' }];
    if (state.phase === 'waiting_qr' || state.phase === 'scanned') return [409, { refused: 'qr_waiting' }];
    if (state.phase === 'starting') return [409, { refused: 'login_starting' }];
    const at = now().getTime();
    while (accepted.length && accepted[0] <= at - DAY_MS) accepted.shift();
    const lastHour = accepted.filter(time => time > at - HOUR_MS);
    const waits = [];
    if (accepted.length) waits.push(accepted.at(-1) + loginLimits.minGapMs - at);
    if (lastHour.length >= loginLimits.perHour) waits.push(lastHour[lastHour.length - loginLimits.perHour] + HOUR_MS - at);
    if (accepted.length >= loginLimits.perDay) waits.push(accepted[accepted.length - loginLimits.perDay] + DAY_MS - at);
    const wait = Math.max(0, ...waits);
    if (wait > 0) return [429, { refused: 'rate_limited', retry_after_s: Math.ceil(wait / 1000) }];
    accepted.push(at);
    const mine = ++generation;
    setState({ phase: 'starting', logged_in: false, user_id: undefined, qr: undefined, qr_svg: undefined });
    log('login QR requested');
    let fresh;
    try { fresh = makeBot(); }
    catch (error) {
      log('building the puppet failed: ' + short(error));
      end('login_failed');
      return [202, { phase: 'starting' }];
    }
    bot = fresh;
    attach(fresh, mine);
    qrTimer = setTimeout(() => { if (mine === generation && state.phase === 'starting') end('login_failed'); },
      loginLimits.qrWaitMs);
    qrTimer.unref?.();
    void track((async () => {
      try { await fresh.start(); }
      catch (error) {
        log('starting the puppet failed: ' + short(error));
        if (mine === generation && state.phase === 'starting') end('login_failed');
      }
    })());
    return [202, { phase: 'starting' }];
  }

  const push = (target, size, event) => {
    target.push({ seq: ++seq, ...event });
    if (target.length <= size) return;
    for (const dropped of target.splice(0, target.length - size))
      for (const attachment of dropped.attachments ?? []) if (attachment.media_id) live.delete(attachment.media_id);
  };

  /** A group message (step 23): the account's own and non-text ones are dropped; a bound
   *  room's message passes only when it calls the bot with the room's prefix; an unbound
   *  room's only when it is exactly "@prefix group-code". Nothing else is kept or logged. */
  function relayGroup(message, roomRef, at) {
    if (message.self() || message.type() !== types.Message.Text) return false;
    const senderRef = message.talker()?.id;
    if (!roomRef || !senderRef) return false;
    const base = { room_ref: roomRef, sender_ref: senderRef, message_ref: String(message.id), ...(at ? { at } : {}) };
    if (rooms.has(roomRef)) {
      const text = calledText(message.text(), rooms.get(roomRef));
      if (text === undefined) return false;
      push(groupRing, groupRingSize, { type: 'group_message', ...base, text });
      return true;
    }
    const attempt = groupBindAttempt(message.text());
    if (!attempt) return false;
    push(groupRing, groupRingSize, { type: 'group_bind', ...base, prefix: attempt.prefix, code: attempt.code });
    return true;
  }

  function relay(message) {
    try {
      // When the owner wrote it, as WeChat says; a message WeChat does not date carries no
      // time (never the bridge's own: the host refuses an undated approval phrase).
      const date = message.date?.();
      const at = date instanceof Date && !Number.isNaN(date.getTime()) ? date.toISOString() : undefined;
      const roomRef = roomOf(message);
      if (roomRef) {
        if (!relayGroup(message, roomRef, at)) return;
      } else {
        if (!isOwnerCandidate(message, types, mediaLimits)) return;
        const media = message.type() === types.Message.Text ? undefined : mediaOf(message, types, mediaLimits);
        const event = { type: 'message', sender_ref: message.talker().id, message_ref: String(message.id),
          text: media ? '' : message.text(), ...(at ? { at } : {}) };
        // Once the host named the bound account, anyone else's 1:1 message is a stranger's: it
        // cannot crowd the owner's out of the main ring.
        const stranger = Boolean(ownerRef && event.sender_ref !== ownerRef);
        if (media) {
          // Step 24.2: a description only - nothing is fetched until GET /media/:media_id asks.
          // A stranger's buffer holds no live message (gone); one over its bound has no media_id.
          const { too_large: tooLarge, ...described } = media;
          if (stranger) event.attachments = [{ ...described, gone: true }];
          else if (tooLarge) event.attachments = [{ ...described, too_large: true }];
          else {
            const mediaId = `${seq + 1}.0`;
            live.set(mediaId, { message, index: 0, kind: media.kind, generation });
            event.attachments = [{ media_id: mediaId, ...described }];
          }
        }
        if (stranger) push(strangerRing, strangerRingSize, event);
        else push(ring, ringSize, event);
      }
      const pending = waiters;
      waiters = new Set();
      for (const wake of pending) wake();
    } catch (error) {
      // Never the text: only that a message could not be read.
      log('message skipped: ' + String(error?.message ?? error).slice(0, 200));
    }
  }

  const after = cursor => [...ring, ...groupRing, ...strangerRing].filter(event => event.seq > cursor)
    .sort((a, b) => a.seq - b.seq);

  // `response` closes before we answer only when the client went away; a request's
  // own 'close' can fire as soon as its (empty) body is read, so it is not used.
  async function events(cursor, waitSeconds, response) {
    let found = after(cursor);
    if (!found.length && waitSeconds > 0) {
      await new Promise(resolve => {
        const done = () => { clearTimeout(timer); waiters.delete(done); response.off('close', done); resolve(); };
        const timer = setTimeout(done, waitSeconds * 1000);
        waiters.add(done);
        response.on('close', done);
      });
      found = after(cursor);
    }
    return { events: found, next_after: found.length ? found.at(-1).seq : cursor };
  }

  async function send(to, text) {
    if (!state.logged_in || !bot) return [409, { error: 'not_logged_in' }];
    // Neo can only write to a peer it has seen in this login (a message from them).
    const contact = await bot.Contact.find({ id: to });
    if (!contact) return [409, { error: 'recipient_unknown' }];
    let ref = null;
    for (const part of splitText(text)) {
      let sent;
      try { sent = await contact.say(part); }
      catch (error) {
        // Wechaty can still hold a contact that neo has dropped from its own list of
        // observed peers; neo then refuses the peer, which is not a failed send.
        if (ref === null && /observed peer/.test(String(error?.message))) return [409, { error: 'recipient_unknown' }];
        throw error;
      }
      ref ??= sent?.id ?? null;
    }
    return [200, { ref }];
  }

  /** POST /send {room, text} (step 23): only a bound room; one queue for every group, at least
   *  sendGapMs between two sends, at most backstopPerHour per room and hour (429 past it). */
  function sendGroup(room, text) {
    return groupSend(room, async target => {
      const sent = await target.say(text);
      return [200, { ref: sent?.id ?? null }];
    });
  }

  /** One send into bound room `room` through the group queue: `act(room handle)` answers it. */
  function groupSend(room, act) {
    if (!rooms.has(room)) return Promise.resolve([409, { error: 'room_not_bound' }]);
    const run = sendQueue.then(async () => {
      if (!state.logged_in || !bot) return [409, { error: 'not_logged_in' }];
      const at = now().getTime();
      const recent = (roomSends.get(room) ?? []).filter(time => time > at - HOUR_MS);
      roomSends.set(room, recent);
      if (recent.length >= sendLimits.backstopPerHour) return [429, { error: 'room_backstop' }];
      const wait = lastGroupSend + sendLimits.sendGapMs - at;
      if (wait > 0) await sleep(wait);
      lastGroupSend = now().getTime();
      recent.push(lastGroupSend);
      return act(bot.Room.load(room));
    });
    sendQueue = run.catch(() => {});
    return run;
  }

  /** The FileBox of one live attachment, through the fork (the platform director's interface):
   *  a voice by `puppet.messageVoice(id)` when the puppet has it (WAV), else every kind by
   *  Wechaty's `message.toFileBox()` (`puppet.messageFile(id)`); an image by
   *  `message.toImage().hd()` (`puppet.messageImage(id, HD)`). */
  const fetchBox = async ({ message, kind }) => {
    if (kind === 'voice' && typeof bot.puppet?.messageVoice === 'function') return bot.puppet.messageVoice(message.id);
    if (kind === 'image' && typeof message.toImage === 'function') return message.toImage().hd();
    return message.toFileBox();
  };

  /** The FileBox's bytes into `file` (tmpfs), counted as they come: {size} or {too_large} at the
   *  kind's bound (a voice: 2 min 5 s of WAV by its header's rate); the caller deletes the file. */
  const spool = async (box, file, kind) => {
    const source = typeof box?.toStream === 'function' ? await box.toStream()
      : typeof box?.toBuffer === 'function' ? [await box.toBuffer()] : undefined;
    if (!source) throw new Error('the puppet gave no file');
    const handle = await open(file, 'wx', 0o600);
    let size = 0, head = Buffer.alloc(0), cap = kind === 'voice' ? undefined : mediaLimits[kind];
    try {
      for await (const chunk of source) {
        const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        size += bytes.length;
        if (cap === undefined) {
          head = Buffer.concat([head, bytes]).subarray(0, 4096);
          if (head.length >= 512) cap = wavByteCap(head, mediaLimits);
        }
        if (size > (cap ?? mediaLimits.voiceWavMax)) return { too_large: true };
        await handle.write(bytes);
      }
      if (cap === undefined && size > wavByteCap(head, mediaLimits)) return { too_large: true };
      return { size, mime: kind === 'voice' ? 'audio/wav' : box.mediaType };
    } finally {
      await handle.close();
      // Leaving the loop early (the cut) stops the source.
      source.destroy?.();
    }
  };

  /** GET /media/:media_id: the bytes of one live attachment, fetched now and streamed out. 404:
   *  not live (never was, or its event left the ring); 410 gone: another login's message, or the
   *  puppet refused it; 503 busy: another fetch is running (one at a time); 413 too_large: more
   *  bytes than its bound. The bytes sit in tmpfs only while this answer runs. */
  async function serveMedia(mediaId, response) {
    const entry = live.get(mediaId);
    if (!entry) return reply(response, 404, { error: 'not_found' });
    if (entry.generation !== generation || !bot) return reply(response, 410, { error: 'gone' });
    if (fetching) return reply(response, 503, { error: 'busy' });
    fetching = true;
    const file = join(tmpDir, `wechat-media-${randomBytes(8).toString('hex')}`);
    // The file is deleted and the next fetch let in before the client can have the whole
    // answer: before a refusal is written, and as the last byte is read out of tmpfs.
    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      try { rmSync(file, { force: true }); } catch (error) { log('media file not deleted: ' + short(error)); }
      fetching = false;
    };
    try {
      let spooled;
      try { spooled = await spool(await fetchBox(entry), file, entry.kind); }
      catch (error) {
        log(`media ${entry.kind} not fetched: ${short(error)}`);
        release();
        return reply(response, 410, { error: 'gone' });
      }
      if (spooled.too_large) { release(); return reply(response, 413, { error: 'too_large' }); }
      response.writeHead(200, { 'content-type': typeof spooled.mime === 'string' && spooled.mime ? spooled.mime : 'application/octet-stream',
        'content-length': String(spooled.size), 'cache-control': 'no-store' });
      if (!spooled.size) { release(); return response.end(); }
      // The client has the whole answer once it has content-length bytes, which may be before
      // the read stream reaches its end: release as the last byte is read.
      const stream = createReadStream(file);
      let read = 0;
      stream.on('data', chunk => { read += chunk.length; if (read >= spooled.size) release(); });
      try { await pipeline(stream, response); }
      catch (error) { log('media answer cut: ' + short(error)); }
    } finally { release(); }
  }

  /** Sends `bytes` under each of `names` in turn until one goes (a voice bar refused or failed
   *  goes again as a file): [200, {ref, sent_as}]. A peer neo no longer knows is recipient_unknown. */
  async function deliverFile(say, bytes, names) {
    let failure;
    for (const [i, { as, name }] of names.entries()) {
      try {
        const sent = await say(makeFileBox(bytes, name));
        return [200, { ref: sent?.id ?? null, sent_as: as }];
      } catch (error) {
        if (/observed peer/.test(String(error?.message))) return [409, { error: 'recipient_unknown' }];
        failure = error;
        if (i + 1 < names.length) log(`sending as ${as} failed (${short(error)}); sending it as ${names[i + 1].as}`);
      }
    }
    throw failure;
  }

  async function sendFile({ to, room }, bytes, names) {
    if (room) return groupSend(room, target => deliverFile(box => target.say(box), bytes, names));
    if (!state.logged_in || !bot) return [409, { error: 'not_logged_in' }];
    const contact = await bot.Contact.find({ id: to });
    if (!contact) return [409, { error: 'recipient_unknown' }];
    return deliverFile(box => contact.say(box), bytes, names);
  }

  const readBytes = (request, limit) => new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on('data', chunk => {
      if (size > limit) return;
      size += chunk.length;
      // The rest of an oversized body is read and dropped, so the 413 reaches the client.
      if (size > limit) { chunks.length = 0; reject(Object.assign(new Error('body_too_large'), { status: 413 })); return; }
      chunks.push(chunk);
    });
    request.on('end', () => { if (size <= limit) resolve(Buffer.concat(chunks)); });
    request.on('error', reject);
  });

  const reply = (response, status, body) => {
    response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
    response.end(JSON.stringify(body));
  };

  const readJson = request => new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on('data', chunk => {
      if (size > MAX_BODY_BYTES) return;
      size += chunk.length;
      // The rest of an oversized body is read and dropped, so the 413 reaches the client.
      if (size > MAX_BODY_BYTES) { chunks.length = 0; reject(Object.assign(new Error('body_too_large'), { status: 413 })); return; }
      chunks.push(chunk);
    });
    request.on('end', () => {
      if (size > MAX_BODY_BYTES) return;
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch { reject(Object.assign(new Error('bad_request'), { status: 400 })); }
    });
    request.on('error', reject);
  });

  const authorized = request => {
    const header = request.headers.authorization ?? '';
    const match = /^Bearer (.+)$/.exec(header);
    return Boolean(match) && timingSafeEqual(digest(match[1].trim()), expected);
  };

  const server = createServer(async (request, response) => {
    try {
      if (!authorized(request)) return reply(response, 401, { error: 'unauthorized' });
      const url = new URL(request.url, 'http://bridge');
      if (request.method === 'GET' && url.pathname === '/status') {
        const body = { phase: state.phase, ...(state.idle_reason ? { idle_reason: state.idle_reason } : {}),
          logged_in: state.logged_in, since: state.since };
        if (state.user_id) body.user_id = state.user_id;
        if (state.qr) body.qr = state.qr;
        if (state.qr_svg) body.qr_svg = state.qr_svg;
        return reply(response, 200, body);
      }
      if (request.method === 'GET' && url.pathname === '/events') {
        const cursor = Number(url.searchParams.get('after') ?? 0);
        const wait = Number(url.searchParams.get('wait') ?? 0);
        if (!Number.isFinite(cursor) || !Number.isFinite(wait) || wait < 0) return reply(response, 400, { error: 'bad_request' });
        return reply(response, 200, await events(cursor, Math.min(wait, maxWaitSeconds), response));
      }
      const mediaPath = /^\/media\/([^/]+)$/.exec(url.pathname);
      if (request.method === 'GET' && mediaPath) return await serveMedia(decodeURIComponent(mediaPath[1]), response);
      if (request.method === 'POST' && url.pathname === '/send-file') {
        // Step 24.2: ?to= (the bound owner) or ?room= (a bound group), kind, mime, name; the body
        // is the file's bytes, at most mediaLimits.sendBody.
        const bytes = await readBytes(request, mediaLimits.sendBody);
        const query = url.searchParams;
        const to = query.get('to') ?? '', room = query.get('room') ?? '', kind = query.get('kind') ?? '';
        const mime = query.get('mime') ?? '', name = query.get('name') ?? '';
        const names = MEDIA_KINDS.includes(kind) && name.length <= 255 && mime.length <= 200
          ? sendNames({ kind, mime, name, durationMs: kind === 'voice' ? wavDurationMs(bytes) : undefined }, voiceName) : undefined;
        if (!names || !bytes.length || Boolean(to) === Boolean(room)) return reply(response, 400, { error: 'bad_request' });
        if (typeof makeFileBox !== 'function') return reply(response, 501, { error: 'not_supported' });
        const [status, result] = await sendFile({ to, room }, bytes, names);
        return reply(response, status, result);
      }
      if (request.method === 'POST' && url.pathname === '/login') {
        const [status, result] = requestLogin();
        return reply(response, status, result);
      }
      if (request.method === 'PUT' && url.pathname === '/rooms') {
        const body = await readJson(request);
        const list = Array.isArray(body?.rooms) ? body.rooms : null;
        if (!list || list.length > 10 || (body.owner_ref != null && (typeof body.owner_ref !== 'string' || !body.owner_ref))
          || list.some(r => typeof r?.room_ref !== 'string' || !r.room_ref || typeof r.prefix !== 'string' || !r.prefix.trim()))
          return reply(response, 400, { error: 'bad_request' });
        ownerRef = body.owner_ref ?? undefined;
        rooms = new Map(list.map(r => [r.room_ref, r.prefix.normalize('NFKC').trim()]));
        return reply(response, 200, { rooms: rooms.size, owner_ref_set: Boolean(ownerRef) });
      }
      if (request.method === 'POST' && url.pathname === '/send') {
        const body = await readJson(request);
        if (typeof body?.room === 'string' && body.room) {
          if (typeof body.text !== 'string' || !body.text.trim()) return reply(response, 400, { error: 'bad_request' });
          const [status, result] = await sendGroup(body.room, body.text);
          return reply(response, status, result);
        }
        if (typeof body?.to !== 'string' || !body.to || typeof body.text !== 'string' || !body.text.trim()) {
          return reply(response, 400, { error: 'bad_request' });
        }
        const [status, result] = await send(body.to, body.text);
        return reply(response, status, result);
      }
      return reply(response, 404, { error: 'not_found' });
    } catch (error) {
      if (error?.status) return reply(response, error.status, { error: error.message });
      // A failed send reaches the caller as one error; the text is never logged.
      log('request failed: ' + short(error));
      return reply(response, 502, { error: 'send_failed', detail: String(error?.message ?? error).slice(0, 200) });
    }
  });

  return {
    server,
    state,
    /** Long polls waiting now (each holds a timer until it is answered or closed). */
    get waiting() { return waiters.size; },
    /** Live attachments (step 24.2): never more than the ring events that carry a media_id. */
    get live() { return live.size; },
    /** Settles once every bot start/stop asked for so far has run. */
    settled: async () => { while (tasks.size) await Promise.allSettled([...tasks]); },
    listen: (port = DEFAULT_PORT, host = HOST) => new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(port, host, () => { server.off('error', reject); resolve(server.address()); });
    }),
    /** Stops the server and the current bot (main.mjs on SIGINT/SIGTERM). */
    close: async () => {
      generation++;
      clearTimers();
      const old = bot;
      bot = undefined;
      for (const wake of waiters) wake();
      server.closeAllConnections?.();
      await new Promise(resolve => server.close(() => resolve()));
      if (old) await stopBot(old);
    },
  };
}
