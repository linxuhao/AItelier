/** wechat-bridge: one WeChat login kept alive in its own process, served over
 *  loopback HTTP to the steward's channel plugin (dsh-channel-wechat).
 *
 *  Why a separate process: puppet-neo keeps the login in memory, so whoever holds it
 *  must outlive steward and host restarts (design §7.2). Step 19.1 (design §7.2.1, the
 *  owner's rule of 2026-11-22: "a start resumes by itself; with nothing to resume it waits
 *  for a QR to be asked for"): the fork also keeps the login encrypted on disk, and a start
 *  that finds one resumes it (`resuming`) - a fresh bot built with `makeBot({resume: true})`,
 *  never a QR. Nothing to resume (or saving off) starts `idle` exactly as before.
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
 *  dependency of its own: `makeBot(options?)` returns a Wechaty instance (or a fake with the same
 *  events, start/stop and Contact.find) and `types` is Wechaty's `types`, both handed in
 *  by main.mjs, so the tests never load the real puppet. `renderQr` (qr-svg.mjs, also
 *  handed in) draws the login QR as an SVG for the dashboard; `savedLogin` (main.mjs: the
 *  fork's `savedLoginStatus()` and the bridge's own `accountRecord`) is the saved login,
 *  absent when the puppet has none. The bridge never deletes a saved-login file: only the
 *  fork does (refused, ended by WeChat, or another account than expected). */
import { createServer } from 'node:http';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { closeSync, constants, createReadStream, fstatSync, fsyncSync, mkdirSync, openSync, readdirSync, readFileSync,
  renameSync, rmSync, writeSync } from 'node:fs';
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
/** GET /media/:media_id's own deadlines, the host's (design 7.8.12 "入库的时限"): the puppet must
 *  hand over a first byte within firstByteMs and the whole file within totalMs, or the fetch is
 *  abandoned (the lock released, the partial deleted) and answered 503; so is one whose client
 *  went away. A failure that is not a recognised refusal is transient (503), until a media_id has
 *  failed `attempts` times: then it is gone (410), so one file never holds the owner's later
 *  messages forever. */
export const MEDIA_FETCH = Object.freeze({ firstByteMs: 30_000, totalMs: 120_000, attempts: 5 });
/** The bytes of described attachments kept on disk, so a process whose live map holds no entry
 *  for an id answers GET /media for it from the copy instead of asking its puppet. The bound is
 *  64 MiB: the rings could hold RING_SIZE 500 + GROUP_RING_SIZE 200 + STRANGER_RING_SIZE 50 =
 *  750 described attachments at MEDIA_LIMITS.file's 25 MiB, i.e. 18,750 MiB, so an eager copy of
 *  a full ring is not acceptable; 64 MiB holds two 25 MiB files and smaller ones besides.
 *  Eviction is oldest-described-first (the smallest media_id sequence number), and an id whose
 *  event leaves a ring is removed at once. The bound is not the rings': it can evict the copy of
 *  an id whose event is still live. That process still serves the id on demand (the live path),
 *  but a later process, holding no live entry for it, answers 404 - the same answer as for an id
 *  that was never copied. */
export const MEDIA_STORE_BYTES = 64 * MiB;
export const MEDIA_STORE_DIR = 'media';
/** A puppet error that says this message will never be handed over (neo's refusal of a peer it
 *  has not observed, an expired or unknown message; the fork's "payload is no longer cached",
 *  its refusal of a message of another WeChat type, and its refusals of a payload that can
 *  never be fetched: an app message "not a file", one that "carries no encrypted" picture or
 *  attachment, a voice that "states no byte length"): 410 gone at once.
 *  `createBridge({isGone})` replaces it should the fork name its refusals otherwise. */
export const isGoneError = error =>
  /observed peer|expired|not found|no longer cached|from a WeChat message of type|not a file|carries no encrypted|states no byte length/i.test(String(error?.message ?? error));
/** The FileBox name decides what the fork sends (its messageSendFile): `.silk`/`.sil`/`.slk`/`.amr`
 *  a voice bar (the length in ms is the name's leading number), one of these an image, anything
 *  else a file attachment. */
export const IMAGE_EXTENSIONS = Object.freeze(['jpg', 'jpeg', 'png', 'gif', 'webp']);
const VOICE_EXTENSIONS = ['silk', 'sil', 'slk', 'amr'];
/** What the fork can send as a voice bar or an image bubble, nothing else (it transcodes
 *  nothing): a voice bar is SILK its encodeVoice made from 16 kHz mono 16-bit PCM of at most
 *  voiceMaxMs; an image bubble is a real JPEG of at most imageMaxBytes (the crate's small tier).
 *  Anything else goes as a file attachment. */
export const MEDIA_SEND = Object.freeze({ voiceSampleRate: 16_000, voiceMaxMs: 60_000, imageMaxBytes: 64 * 1024 });
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
/** Step 19.1 (design 7.2.1): a resume with no outcome within waitMs is given up (`idle`,
 *  resume_failed) - a display bound only, strictly above the fork's worst case (about 70 s:
 *  seven 10-second requests); the fork's own `busy` keeps two sessions off one login. A
 *  resume that failed for a passing reason (the network, `busy`, no outcome) is tried again
 *  after each of retryMs, after the last bot has stopped; then the bridge waits for a QR to
 *  be asked for. Until the host's first PUT /rooms names the bound account and rooms, what
 *  arrives is held in memory (each kind at most its ring's size, oldest dropped): group
 *  messages at most holdGroupMs after the start - after that they pass the rules with no
 *  room bound, so their content is dropped, never stored - and 1:1 messages until that
 *  first PUT. A held message whose login ended is dropped. countLogMs: how often the fork's
 *  write counts are logged (only when they changed). The fork caps the resumes themselves
 *  (6 an hour, 20 a day, across restarts) and answers `throttled` past them. */
export const RESUME_LIMITS = Object.freeze({ waitMs: 150_000, retryMs: Object.freeze([30_000, 120_000, 600_000]),
  holdGroupMs: 120_000, countLogMs: HOUR_MS });
/** The fork's error prefixes for a resume that did not resume, and the idle reason each
 *  leaves; none of them asks for a QR. RESUME_MISMATCH: the saved login was another account
 *  than the expectUsername it was given; the fork has stopped that account and deleted the
 *  one file that attempt read. The bridge's own check on login (attach) stays as a second
 *  guard. RESUME_BUSY (another
 *  resume still running in the fork) is passing: retried, as is any other error. */
const RESUME_ENDS = Object.freeze({ RESUME_REJECTED: 'resume_rejected', RESUME_UNREADABLE: 'resume_unreadable',
  RESUME_THROTTLED: 'resume_throttled', RESUME_NONE: 'boot', RESUME_MISMATCH: 'resume_account_mismatch' });
const RESUME_WORD = /\bRESUME_(?:NONE|UNREADABLE|THROTTLED|REJECTED|BUSY|MISMATCH)\b/;
/** `savedLoginStatus().off`, the fork's closed set of configuration errors. A failed write is
 *  never one of them: writes and write_failures are counted apart. */
const SAVED_LOGIN_OFF = new Set(['not_configured', 'key', 'dir']);
/** The account logged in with a QR (design 7.2.1, the platform director's ruling 9 and his
 *  notes 653-654), recorded by the bridge in its own directory (WECHAT_BRIDGE_ACCOUNT_DIR,
 *  run.sh), never in the fork's data/: a copy of data/ put back cannot bring an old record
 *  with it. Each resume hands it to the fork as expectUsername; the fork compares and answers
 *  RESUME_MISMATCH, and the bridge compares a resumed login's user id with it again itself,
 *  a second guard. None recorded: nothing is passed and nothing is compared. */
export const ACCOUNT_FILE = 'account.user';
const ACCOUNT_FILE_MAX = 4096;

/** The account record in `dir` (the bridge's account directory): readAccount() -> the user id
 *  recorded at the last QR login, or undefined (none, unreadable, a link or not a file);
 *  recordAccount(id) writes it atomically (a 0600 temporary file, fsync, rename, the
 *  directory fsynced) and throws when it cannot. No dir: nothing is read, a write throws. */
export function accountRecord(dir) {
  const file = dir ? join(dir, ACCOUNT_FILE) : undefined;
  return {
    dir,
    readAccount() {
      if (!file) return undefined;
      let fd;
      try {
        fd = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
        const info = fstatSync(fd);
        if (!info.isFile() || info.size > ACCOUNT_FILE_MAX) return undefined;
        const value = JSON.parse(readFileSync(fd, 'utf8'));
        return value?.v === 1 && typeof value.user_id === 'string' && value.user_id ? value.user_id : undefined;
      } catch { return undefined; }
      finally { if (fd !== undefined) closeSync(fd); }
    },
    recordAccount(userId) {
      if (!file) throw new Error('no saved-login directory');
      const temporary = `${file}.tmp-${randomBytes(8).toString('hex')}`;
      try {
        const fd = openSync(temporary, 'wx', 0o600);
        try { writeSync(fd, JSON.stringify({ v: 1, user_id: userId })); fsyncSync(fd); } finally { closeSync(fd); }
        renameSync(temporary, file);
        const directory = openSync(dir, 'r');
        try { fsyncSync(directory); } finally { closeSync(directory); }
      } catch (error) {
        rmSync(temporary, { force: true });
        throw error;
      }
    },
  };
}

const MEDIA_ID = /^\d+\.\d+$/;
const mediaSeq = id => Number(id.slice(0, id.indexOf('.')));

/** The durable copy of described attachments, in its own subdirectory of the bridge's
 *  account directory (the directory accountRecord is given). `bytes` bounds the SUM of the
 *  stored payload sizes; `put(id, {file, size, mime, durationMs})` moves a spooled 0600 file
 *  into it and publishes its metadata last, so an entry exists only once its metadata has
 *  landed: the index is read from the metadata files alone, and a payload without one is not
 *  an entry and is never served. `get(id)` opens a stored payload without following links (a
 *  link at the payload path is refused and the id dropped), and `remove(id)` deletes both
 *  files. The index is rebuilt from the metadata at construction, so the next process serves
 *  the same ids. */
export function mediaStore(dir, { bytes = MEDIA_STORE_BYTES, log = () => {} } = {}) {
  const root = dir ? join(dir, MEDIA_STORE_DIR) : undefined;
  const entries = new Map();
  let total = 0;
  const bin = id => join(root, `${id}.bin`);
  const meta = id => join(root, `${id}.json`);
  const discard = path => { try { rmSync(path, { force: true }); } catch (error) { log(`media store file not deleted: ${short(error)}`); } };
  function remove(id) {
    if (!root) return;
    const old = entries.get(id);
    if (old) { total -= old.size; entries.delete(id); }
    discard(bin(id));
    discard(meta(id));
  }
  if (root) {
    mkdirSync(root, { recursive: true, mode: 0o700 });
    const found = [];
    for (const name of readdirSync(root)) {
      const id = /^(\d+\.\d+)\.json$/.exec(name)?.[1];
      if (!id) {
        if (name.startsWith('tmp-')) discard(join(root, name));
        continue;
      }
      let value;
      try {
        const fd = openSync(join(root, name), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
        try { value = JSON.parse(readFileSync(fd, 'utf8')); } finally { closeSync(fd); }
        if (value?.v !== 1 || !Number.isSafeInteger(value.size) || value.size < 0 || value.size > bytes
          || (value.mime !== undefined && typeof value.mime !== 'string')
          || (value.durationMs !== undefined && !Number.isFinite(value.durationMs))) throw new Error('bad media metadata');
        const payload = openSync(bin(id), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
        try {
          const info = fstatSync(payload);
          if (!info.isFile() || info.size !== value.size) throw new Error('stored media bytes changed');
        } finally { closeSync(payload); }
      } catch (error) {
        log(`stored media ${id} ignored: ${short(error)}`);
        discard(join(root, name));
        discard(bin(id));
        continue;
      }
      found.push([id, { size: value.size, mime: value.mime, durationMs: value.durationMs }]);
    }
    for (const [id, value] of found.sort((a, b) => mediaSeq(a[0]) - mediaSeq(b[0]))) {
      entries.set(id, value);
      total += value.size;
    }
    for (const id of [...entries.keys()]) { if (total <= bytes) break; remove(id); }
  }
  return {
    dir: root,
    temp: () => join(root, `tmp-${randomBytes(8).toString('hex')}`),
    put(id, { file, size, mime, durationMs }) {
      if (!root || !MEDIA_ID.test(id)) { discard(file); return false; }
      try {
        remove(id);
        renameSync(file, bin(id));
        const temporary = `${meta(id)}.tmp-${randomBytes(8).toString('hex')}`;
        try {
          const fd = openSync(temporary, 'wx', 0o600);
          try { writeSync(fd, JSON.stringify({ v: 1, size, mime, durationMs })); fsyncSync(fd); } finally { closeSync(fd); }
          renameSync(temporary, meta(id));
        } catch (error) { discard(temporary); throw error; }
        entries.set(id, { size, mime, durationMs });
        total += size;
        while (total > bytes && entries.size) remove(entries.keys().next().value);
        return entries.has(id);
      } catch (error) {
        log(`media ${id} not stored: ${short(error)}`);
        discard(bin(id));
        discard(meta(id));
        return false;
      }
    },
    get(id) {
      if (!root || !entries.has(id)) return undefined;
      const value = entries.get(id);
      let fd;
      try {
        fd = openSync(bin(id), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
        const info = fstatSync(fd);
        if (!info.isFile() || info.size !== value.size) throw new Error('stored media bytes changed');
        return { fd, size: value.size, mime: value.mime, durationMs: value.durationMs };
      } catch (error) {
        if (fd !== undefined) closeSync(fd);
        log(`stored media ${id} unreadable: ${short(error)}`);
        remove(id);
        return undefined;
      }
    },
    remove,
    ids: () => [...entries.keys()],
    get bytes() { return total; },
    get count() { return entries.size; },
  };
}

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
/** `promise`, or a rejection with the signal's reason as soon as it aborts (the promise is left to settle). */
const raced = (promise, signal) => new Promise((resolve, reject) => {
  if (signal.aborted) return reject(signal.reason);
  const stop = () => reject(signal.reason);
  signal.addEventListener('abort', stop, { once: true });
  Promise.resolve(promise).then(value => { signal.removeEventListener('abort', stop); resolve(value); },
    error => { signal.removeEventListener('abort', stop); reject(error); });
});

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
 *  one, must agree (a 49 the fork files as Unknown is taken by its raw type). A file is let in
 *  only when the payload carries its XML and that says appmsg type 6 and quotes nothing: a
 *  quoted reply (49/57), or a 49 whose XML cannot be read, is never let in. Sizes and the file name are what WeChat's
 *  XML reports (not trusted: GET /media/:media_id counts the real bytes). */
export function mediaOf(message, types, limits = MEDIA_LIMITS) {
  const type = message.type();
  const kinds = [['image', types.Message?.Image, 3], ['voice', types.Message?.Audio, 34], ['file', types.Message?.Attachment, 49]];
  const payload = message.payload ?? {};
  let found = kinds.find(([, value]) => value !== undefined && value === type);
  // The fork's patch files WeChat's 49 under Unknown: its raw type and the XML checks below decide.
  if (!found && types.Message?.Unknown !== undefined && type === types.Message.Unknown && payload.messageType === 49) found = kinds[2];
  if (!found) return undefined;
  const [kind, , raw] = found;
  if (payload.messageType !== undefined && payload.messageType !== raw) return undefined;
  const xml = contentXml(payload.content);
  if (kind === 'file' && (xml === undefined || /<refermsg[\s>]/.test(xml)
    || element(xml.slice(xml.indexOf('<appmsg')), 'type') !== '6')) return undefined;
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

/** An arriving voice's length: the fork names its WAV `<durationMs>ms.wav`, so the name says it;
 *  a name that does not falls back to the WAV header's byte rate (`head`, `size` bytes in all). */
export function inboundVoiceMs(name, head, size) {
  const named = /^(\d{1,9})ms\.wav$/i.exec(String(name ?? ''));
  if (named && Number(named[1]) > 0) return Number(named[1]);
  const rate = wavByteRate(head);
  return rate ? Math.round(Math.max(0, size - 44) * 1000 / rate) : undefined;
}

/** The PCM a voice bar is encoded from: the data chunk of a RIFF/WAVE file that is PCM, mono,
 *  16 kHz, 16-bit and at most `limits.voiceMaxMs` long; undefined for anything else, which then
 *  goes as a file (nothing is resampled here, and the fork's encoder takes only that). */
export function voicePcm(bytes, limits = MEDIA_SEND) {
  if (bytes.length < 44 || bytes.toString('latin1', 0, 4) !== 'RIFF' || bytes.toString('latin1', 8, 12) !== 'WAVE') return undefined;
  let format, data;
  for (let at = 12; at + 8 <= bytes.length;) {
    const id = bytes.toString('latin1', at, at + 4), size = bytes.readUInt32LE(at + 4), end = at + 8 + size;
    if (end > bytes.length) return undefined;
    if (id === 'fmt ' && !format) format = bytes.subarray(at + 8, end);
    if (id === 'data' && !data) data = bytes.subarray(at + 8, end);
    at = end + (size & 1);
  }
  if (!format || format.length < 16 || !data || !data.length || data.length % 2) return undefined;
  if (format.readUInt16LE(0) !== 1 || format.readUInt16LE(2) !== 1 || format.readUInt32LE(4) !== limits.voiceSampleRate
    || format.readUInt16LE(14) !== 16) return undefined;
  return data.length * 1000 / (limits.voiceSampleRate * 2) > limits.voiceMaxMs ? undefined : data;
}

/** Whether `bytes` can go as an image bubble: a real JPEG (FF D8 FF) of at most imageMaxBytes. */
export const isBubbleJpeg = (bytes, limits = MEDIA_SEND) =>
  bytes.length >= 3 && bytes.length <= limits.imageMaxBytes && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;

const cleanName = name => String(name ?? '').replace(/[\\/\u0000-\u001f\u007f]/g, '_').trim().slice(-120);
const splitName = name => {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? [name.slice(0, dot), name.slice(dot + 1).toLowerCase()] : [name, ''];
};
/** How to send one file, in order until one goes, each step with what it goes out as. An image
 *  that is a bubble JPEG first as an image (under its own .jpg/.jpeg name, else `<stem>.jpg`); a
 *  voice whose bytes are voice-bar PCM (`voicePcm`) first as a voice bar (`pcm`: the bridge
 *  encodes it and names the SILK `<durationMs>.silk` only then); then, and for everything else
 *  at once, the same bytes as a file under their own name, with `.bin` added when that name
 *  would make the fork send it as an image or a voice bar. */
export function sendPlan({ kind, name, bytes }, limits = MEDIA_SEND) {
  const clean = cleanName(name) || kind;
  const [stem, extension] = splitName(clean);
  // The extension as the fork's carrier() reads it (all after the last dot, the whole name when
  // it has none), so '.png', 'png' or 'silk' never reach its image or voice carrier as a file.
  const forkExtension = clean.slice(clean.lastIndexOf('.') + 1).toLowerCase();
  const asFile = { as: 'file', name: IMAGE_EXTENSIONS.includes(forkExtension) || VOICE_EXTENSIONS.includes(forkExtension) ? `${clean}.bin` : clean };
  if (kind === 'image' && isBubbleJpeg(bytes, limits))
    return [{ as: 'image', name: extension === 'jpg' || extension === 'jpeg' ? clean : `${stem}.jpg` }, asFile];
  const pcm = kind === 'voice' ? voicePcm(bytes, limits) : undefined;
  return pcm ? [{ as: 'voice', pcm }, asFile] : [asFile];
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
  mediaLimits = MEDIA_LIMITS, tmpDir = tmpdir(), makeFileBox, fetchLimits = MEDIA_FETCH, isGone = isGoneError,
  mediaSend = MEDIA_SEND, savedLogin, resumeLimits = RESUME_LIMITS, mediaStoreBytes = MEDIA_STORE_BYTES }) {
  if (typeof token !== 'string' || !token.trim()) throw new Error('wechat-bridge refused: no token');
  if (typeof makeBot !== 'function') throw new Error('wechat-bridge refused: no makeBot');
  const expected = digest(token.trim());
  const ring = [], groupRing = [], strangerRing = [];
  /** Step 24.2 (design 7.8.12 "活消息存在哪"): media_id ('<seq>.<index>') -> {message, index,
   *  kind, generation} for each fetchable attachment of an event in a ring. An entry goes in
   *  just before its event is pushed and leaves when that event is cut off a ring's head
   *  (`push`), so it never outlives its event; a restart empties this map. The bytes described
   *  here are also copied into `store` (when the account directory is available), so a later
   *  process whose live map holds no entry for the media_id answers it from the copy, without
   *  asking its own puppet. */
  const live = new Map();
  const store = savedLogin?.dir ? mediaStore(savedLogin.dir, { bytes: mediaStoreBytes, log }) : undefined;
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
  // | resuming (step 19.1: the saved login is being resumed, no QR) | logged_in.
  const state = { phase: 'idle', idle_reason: 'boot', logged_in: false, user_id: undefined, qr: undefined,
    qr_svg: undefined, since: now().toISOString() };
  const setState = change => Object.assign(state, { idle_reason: undefined }, change, { since: now().toISOString() });

  // The QR as an image, or undefined when it cannot be drawn (never logged: it is a login token).
  const drawn = qrcode => {
    try { return renderQr(qrcode) || undefined; }
    catch { log('the login QR could not be drawn'); return undefined; }
  };

  // Step 19.1: the saved login. `saving` is the fork's own word at the start - `off` (its
  // closed set of configuration errors) or no saved-login calls at all leave the bridge as it
  // was before 19.1: no resume, no hold, no account record. A failed write never turns it off.
  const savedStatus = () => {
    try { return savedLogin?.status() ?? undefined; }
    catch (error) { log('saved login status unreadable: ' + short(error)); return undefined; }
  };
  const bootStatus = savedStatus();
  const saving = Boolean(bootStatus) && bootStatus.off === undefined;
  if (bootStatus && !saving) log(`saved login off (${SAVED_LOGIN_OFF.has(bootStatus.off) ? bootStatus.off : 'unknown'})`);
  // The account the last QR login was with, handed to each resume as expectUsername (none
  // recorded, e.g. the first start after the upgrade: nothing is compared).
  let recorded = saving ? savedLogin.readAccount() : undefined;
  // Not resumable until the next QR login: a resume here was another account
  // (RESUME_MISMATCH, or refused on login), or a QR login's account could not be recorded (the record on disk may
  // name another one, so the next resume would be refused).
  let refused = false;
  // After a QR login in this process: the fork's `writes` when its QR was asked for. The
  // saved login on disk is that login's only once the fork has written since; a first write
  // that failed leaves an older one, maybe another account's.
  let qrWrites, writesBefore;
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
  /** What /status calls resumable, never optimistic: a saved login is on disk (the fork's
   *  `present`, from lstat), this process has not refused it, and after a QR login here the
   *  fork has written that login's. */
  const resumable = () => {
    if (!saving || refused) return false;
    const status = savedStatus();
    return Boolean(status?.present) && (writesBefore === undefined || count(status.writes) > writesBefore);
  };

  // changes with every request and every end; each bot's events and each timer carry the
  // generation they were made for, so those of an earlier attempt do nothing. `tasks`
  // holds the starts and stops still running (settled() waits for them).
  let bot, generation = 0, qrTimer, lifeTimer, resumeTimer, retryTimer, retriesLeft = 0;
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
  const clearTimers = () => { clearTimeout(qrTimer); clearTimeout(lifeTimer); clearTimeout(resumeTimer); };
  const settle = async () => { while (tasks.size) await Promise.allSettled([...tasks]); };
  // Step 19.1, the hold (RESUME_LIMITS): until the first PUT /rooms, what arrives waits here
  // as {message, generation, group}, in arrival order.
  let holding = saving, groupHoldOver = false;
  const held = [];
  let groupHoldTimer;
  /** This login attempt is over: idle with `reason`, and its bot stopped so nothing talks
   *  to WeChat until the next request. */
  const end = reason => {
    generation++;
    clearTimers();
    setState({ phase: 'idle', idle_reason: reason, logged_in: false, user_id: undefined, qr: undefined, qr_svg: undefined });
    log(`idle (${reason}): no QR until one is requested`);
    held.length = 0;
    const old = bot;
    bot = undefined;
    if (old) void stopBot(old);
  };

  /** Step 19.1: one resume of the saved login - a fresh bot with `makeBot({resume: true,
   *  expectUsername})` (no expectUsername when none is recorded), its own generation, no QR.
   *  Only from idle with no bot of its own. */
  const resume = () => {
    const mine = ++generation;
    setState({ phase: 'resuming', logged_in: false, user_id: undefined, qr: undefined, qr_svg: undefined });
    log('resuming the saved login (no QR)');
    let fresh;
    try { fresh = makeBot({ resume: true, ...(recorded === undefined ? {} : { expectUsername: recorded }) }); }
    catch (error) {
      log('building the puppet failed: ' + short(error));
      resumeFailed();
      return;
    }
    bot = fresh;
    attach(fresh, mine);
    resumeTimer = setTimeout(() => {
      if (mine !== generation || state.phase !== 'resuming') return;
      log(`the saved login gave no answer in ${Math.round(resumeLimits.waitMs / 1000)} s`);
      resumeFailed();
    }, resumeLimits.waitMs);
    resumeTimer.unref?.();
    void track((async () => {
      try { await fresh.start(); }
      catch (error) {
        log('starting the puppet failed: ' + short(error));
        if (mine === generation && state.phase === 'resuming') resumeFailed();
      }
    })());
  };
  /** A resume, `retryMs.length` more times on a passing failure (RESUME_LIMITS): at the start,
   *  and (the owner's answer to design 7.2.1 pending item 2) when a login ends here while its
   *  saved login is still on disk. `delayMs` 0 with no bot: at once. */
  const resumeEpisode = delayMs => {
    retriesLeft = resumeLimits.retryMs.length;
    if (delayMs || bot || tasks.size) scheduleResume(delayMs);
    else resume();
  };
  /** The next resume after `delayMs`, after the last bot has stopped; void once anything else
   *  happened meanwhile (a QR asked for, a close: the generation moved). */
  const scheduleResume = delayMs => {
    const mine = generation;
    clearTimeout(retryTimer);
    retryTimer = setTimeout(async () => {
      if (mine !== generation || state.phase !== 'idle') return;
      await settle();
      if (mine !== generation || state.phase !== 'idle') return;
      resume();
    }, delayMs);
    retryTimer.unref?.();
  };
  /** A resume that failed for a passing reason: idle (resume_failed), and the next try if any is left. */
  const resumeFailed = () => {
    end('resume_failed');
    if (retriesLeft <= 0) return;
    const delay = resumeLimits.retryMs[resumeLimits.retryMs.length - retriesLeft];
    retriesLeft--;
    log(`the saved login will be tried again in ${Math.round(delay / 1000)} s`);
    scheduleResume(delay);
  };
  /** The hold's release: each held message of the current login through relay(), in arrival
   *  order; one of an ended login is dropped. */
  const release = which => {
    const out = [];
    for (let i = 0; i < held.length;) if (which(held[i])) out.push(...held.splice(i, 1)); else i++;
    for (const entry of out) if (entry.generation === generation) relay(entry.message);
  };
  const hold = message => {
    const group = Boolean(roomOf(message));
    if (group && groupHoldOver) return relay(message);
    held.push({ message, generation, group });
    const size = group ? groupRingSize : ringSize;
    if (held.filter(entry => entry.group === group).length > size) held.splice(held.findIndex(entry => entry.group === group), 1);
  };
  if (holding) {
    groupHoldTimer = setTimeout(() => { groupHoldOver = true; release(entry => entry.group); }, resumeLimits.holdGroupMs);
    groupHoldTimer.unref?.();
  }
  // The fork's write counts, logged at most every countLogMs and only when they changed.
  let countTimer, counted = '0/0';
  if (saving) {
    countTimer = setInterval(() => {
      const status = savedStatus();
      const line = `${count(status?.writes)}/${count(status?.write_failures)}`;
      if (!status || line === counted) return;
      counted = line;
      log(`saved login: ${count(status.writes)} writes, ${count(status.write_failures)} write failures`);
    }, resumeLimits.countLogMs);
    countTimer.unref?.();
  }

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
      const resumed = state.phase === 'resuming';
      // Step 19.1 (ruling 9): a resumed login of another account than the recorded one is not
      // taken - idle, nothing relayed or sent, its bot stopped. The fork already compares
      // expectUsername and answers RESUME_MISMATCH (the 'error' handler), deleting that file;
      // this is the second guard, should such a login ever be reported. The bridge deletes
      // nothing: here the file stays, and every start resumes and refuses it again.
      if (resumed && recorded !== undefined && user?.id !== recorded) {
        refused = true;
        return end('resume_account_mismatch');
      }
      clearTimers();
      setState({ phase: 'logged_in', logged_in: true, user_id: user?.id, qr: undefined, qr_svg: undefined });
      log(resumed ? 'logged in (the saved login resumed)' : 'logged in');
      if (resumed || !saving || !user?.id) return;
      writesBefore = qrWrites;
      try { savedLogin.recordAccount(user.id); recorded = user.id; refused = false; }
      catch (error) {
        refused = true;
        log('the account logged in with could not be recorded, so its saved login will not be resumed: ' + short(error));
      }
    });
    target.on('logout', () => {
      if (!current() || state.phase === 'idle') return;
      if (state.phase === 'resuming') return resumeFailed();
      end('logged_out');
      // The owner's answer to pending item 2: a login that ended HERE (the network, a
      // watchdog) leaves its saved login on disk and is resumed; one WeChat ended took the
      // saved login with it (the fork deletes it first), so present is false and nothing runs.
      if (resumable()) resumeEpisode(0);
    });
    target.on('error', error => {
      log('error: ' + short(error));
      if (!current()) return;
      if (state.phase === 'resuming') {
        // The fork's word may be in the message or in `data` (the puppet's own error shape).
        const said = [error?.message, error?.data].filter(part => part !== undefined).join(' ') || String(error);
        const outcome = RESUME_WORD.exec(said)?.[0];
        if (outcome === 'RESUME_MISMATCH') refused = true;
        return RESUME_ENDS[outcome] ? end(RESUME_ENDS[outcome]) : resumeFailed();
      }
      // An error while logged in is only logged (Wechaty reports message handling failures
      // that way); before a login it ends the attempt: neo then gives up its session.
      if (loginLive()) end('login_failed');
    });
    target.on('message', message => {
      if (!current()) return;
      if (holding) hold(message);
      else relay(message);
    });
  };

  /** POST /login: [status, body]. Accepted only while idle; a refusal changes nothing. */
  function requestLogin() {
    if (!loginEnabled) return [409, { refused: 'login_disabled' }];
    if (state.phase === 'logged_in') return [409, { refused: 'already_logged_in' }];
    if (state.phase === 'waiting_qr' || state.phase === 'scanned') return [409, { refused: 'qr_waiting' }];
    if (state.phase === 'starting') return [409, { refused: 'login_starting' }];
    if (state.phase === 'resuming') return [409, { refused: 'resuming' }];
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
    if (saving) qrWrites = count(savedStatus()?.writes);
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
      for (const attachment of dropped.attachments ?? []) if (attachment.media_id) {
        live.delete(attachment.media_id);
        store?.remove(attachment.media_id);
      }
  };

  /** A group message (step 23): the account's own ones are dropped. In a BOUND room a text
   *  message passes only when it calls the bot with the room's prefix, and a non-text one
   *  that is an image, voice or file (design/steward-group-media.md §1, the owner's ruling of
   *  2026-11-22) passes whether or not it calls the bot: it is described exactly as a 1:1 one
   *  is (`mediaOf`, the same `mediaLimits`), registered in `live` under its own media_id, and
   *  pushed as ONE `group_message` whose `text` is the prefix-stripped caption - '' when it
   *  does not call the bot - and whose `asked` says which of the two it was. An unbound room's
   *  only when it is exactly "@prefix group-code", text or not. Nothing else is kept or logged. */
  function relayGroup(message, roomRef, at) {
    if (message.self()) return false;
    const senderRef = message.talker()?.id;
    if (!roomRef || !senderRef) return false;
    const base = { room_ref: roomRef, sender_ref: senderRef, message_ref: String(message.id), ...(at ? { at } : {}) };
    const media = rooms.has(roomRef) && message.type() !== types.Message.Text
      ? mediaOf(message, types, mediaLimits) : undefined;
    if (media) {
      // The 1:1 path's own shape, unchanged: a description only - nothing is fetched until
      // GET /media/:media_id asks - and one over its bound has no media_id. The group ring
      // shares the one `seq`, so '<seq>.<index>' is unique across the rings.
      const called = calledText(message.text(), rooms.get(roomRef));
      const { too_large: tooLarge, ...described } = media;
      const event = { type: 'group_message', ...base, text: called ?? '', asked: called !== undefined };
      if (tooLarge) event.attachments = [{ ...described, too_large: true }];
      else {
        const mediaId = `${seq + 1}.0`;
        live.set(mediaId, { message, index: 0, kind: media.kind, generation });
        archive(mediaId, { message, index: 0, kind: media.kind });
        event.attachments = [{ media_id: mediaId, ...described }];
      }
      push(groupRing, groupRingSize, event);
      return true;
    }
    if (message.type() !== types.Message.Text) return false;
    if (rooms.has(roomRef)) {
      const text = calledText(message.text(), rooms.get(roomRef));
      if (text === undefined) return false;
      push(groupRing, groupRingSize, { type: 'group_message', ...base, text, asked: true });
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
            archive(mediaId, { message, index: 0, kind: media.kind });
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

  /** A local (tmpfs) failure: never the media's fault, so never counted against it. */
  const local = error => Object.assign(error instanceof Error ? error : new Error(String(error)), { local: true });

  /** The live attachment's bytes into `file` (tmpfs), counted as they come: {size, mime,
   *  durationMs?} or {too_large} at the kind's bound (a voice: 2 min 5 s of WAV by its header's
   *  rate; its length from the fork's `<durationMs>ms.wav` name, else that rate). `signal`
   *  abandons it at once (the source destroyed); `firstByte` is called with each chunk. The
   *  file is opened before the puppet is asked; the caller deletes it. */
  const spool = async (entry, file, signal, firstByte) => {
    const { kind } = entry;
    let handle, source;
    try { handle = await open(file, 'wx', 0o600); } catch (error) { throw local(error); }
    try {
      const box = await raced(fetchBox(entry), signal);
      source = typeof box?.toStream === 'function' ? await raced(box.toStream(), signal)
        : typeof box?.toBuffer === 'function' ? [await raced(box.toBuffer(), signal)] : undefined;
      if (!source) throw new Error('the puppet gave no file');
      const chunks = source[Symbol.asyncIterator]?.() ?? source[Symbol.iterator]();
      let size = 0, head = Buffer.alloc(0), cap = kind === 'voice' ? undefined : mediaLimits[kind];
      for (;;) {
        const { value, done } = await raced(chunks.next(), signal);
        if (done) break;
        firstByte();
        const bytes = Buffer.isBuffer(value) ? value : Buffer.from(value);
        size += bytes.length;
        if (cap === undefined) {
          head = Buffer.concat([head, bytes]).subarray(0, 4096);
          if (head.length >= 512) cap = wavByteCap(head, mediaLimits);
        }
        if (size > (cap ?? mediaLimits.voiceWavMax)) return { too_large: true };
        try { await handle.write(bytes); } catch (error) { throw local(error); }
      }
      if (cap === undefined && size > wavByteCap(head, mediaLimits)) return { too_large: true };
      if (kind !== 'voice') return { size, mime: box.mediaType };
      return { size, mime: 'audio/wav', durationMs: inboundVoiceMs(box.name, head, size) };
    } finally {
      await handle.close().catch(error => log('media file not closed: ' + short(error)));
      // Leaving early (the cut, a deadline, the client gone) stops the source.
      source?.destroy?.();
    }
  };

  /** Side copy of a described attachment into the bounded store: the fetch runs while the
   *  event is still in its ring, and an id whose event is cut before the copy lands is not kept.
   *  GET /media keeps its own on-demand path for a live id, so the busy/gone/too_large/temporary
   *  refusals below are unchanged; a stored copy is read only by a process that has no live
   *  entry for that id. Nothing is stored for a stranger or an over-bound one. */
  function archive(mediaId, entry) {
    if (!store) return;
    const file = store.temp();
    const abandon = new AbortController();
    const timeout = what => () => abandon.abort(new Error(`media store timed out: ${what}`));
    const firstTimer = setTimeout(timeout(`no first byte in ${fetchLimits.firstByteMs} ms`), fetchLimits.firstByteMs);
    const totalTimer = setTimeout(timeout(`not whole in ${fetchLimits.totalMs} ms`), fetchLimits.totalMs);
    firstTimer.unref?.();
    totalTimer.unref?.();
    void (async () => {
      try {
        const saved = await spool(entry, file, abandon.signal, () => clearTimeout(firstTimer));
        if (saved.too_large) return;
        if (!live.has(mediaId)) return;
        store.put(mediaId, { file, ...saved });
      } catch (error) {
        log(`media ${entry.kind} not stored: ${short(error)}`);
      } finally {
        clearTimeout(firstTimer);
        clearTimeout(totalTimer);
        rmSync(file, { force: true });
      }
    })();
  }

  /** A stored id from an earlier process: answer from the copy - this process holds no live
   *  entry for it - with the headers a live answer carries (content-type, content-length,
   *  cache-control, and x-voice-duration-ms for a voice), and without asking the puppet. */
  function serveStored(stored, response) {
    let bytes;
    try { bytes = readFileSync(stored.fd); } finally { closeSync(stored.fd); }
    response.writeHead(200, { 'content-type': typeof stored.mime === 'string' && stored.mime ? stored.mime : 'application/octet-stream',
      'content-length': String(stored.size), 'cache-control': 'no-store',
      ...(stored.durationMs === undefined ? {} : { 'x-voice-duration-ms': String(stored.durationMs) }) });
    return response.end(bytes.length ? bytes : undefined);
  }

  /** GET /media/:media_id: the bytes of one live attachment, fetched now and streamed out (a
   *  voice with its length in `x-voice-duration-ms` when known), or, when this process has no
   *  live entry, the bounded store's copy of a media_id described by an earlier process. 404:
   *  held by neither; 410 gone: another login's message, a refusal `isGone` recognises, or one
   *  that failed fetchLimits.attempts times; 503: another fetch is running (busy, one at a
   *  time), or this one failed, ran out of time or lost its client (unavailable, transient);
   *  413 too_large: more bytes than its bound. A live answer's tmpfs file exists only while it
   *  runs; the store's bounded copy is on disk. */
  async function serveMedia(mediaId, response) {
    const entry = live.get(mediaId);
    if (entry && (entry.generation !== generation || !bot || entry.gone)) return reply(response, 410, { error: 'gone' });
    if (!entry) {
      const stored = store?.get(mediaId);
      if (stored) return serveStored(stored, response);
      return reply(response, 404, { error: 'not_found' });
    }
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
    const abandon = new AbortController();
    const timeout = what => () => abandon.abort(new Error(`media fetch timed out: ${what}`));
    const firstTimer = setTimeout(timeout(`no first byte in ${fetchLimits.firstByteMs} ms`), fetchLimits.firstByteMs);
    const totalTimer = setTimeout(timeout(`not whole in ${fetchLimits.totalMs} ms`), fetchLimits.totalMs);
    const left = () => { if (!response.writableFinished) abandon.abort(new Error('the client went away')); };
    response.on('close', left);
    try {
      let spooled;
      try { spooled = await spool(entry, file, abandon.signal, () => clearTimeout(firstTimer)); }
      catch (error) {
        release();
        let status = 503;
        if (!error?.local && (isGone(error) || (entry.failures = (entry.failures ?? 0) + 1) >= fetchLimits.attempts)) {
          entry.gone = true;
          status = 410;
        }
        log(`media ${entry.kind} not fetched (${status === 410 ? 'gone' : 'held'}): ${short(error)}`);
        if (response.destroyed) return;
        return reply(response, status, { error: status === 410 ? 'gone' : 'unavailable' });
      } finally {
        clearTimeout(firstTimer);
        clearTimeout(totalTimer);
        response.off('close', left);
      }
      if (spooled.too_large) { release(); return reply(response, 413, { error: 'too_large' }); }
      response.writeHead(200, { 'content-type': typeof spooled.mime === 'string' && spooled.mime ? spooled.mime : 'application/octet-stream',
        'content-length': String(spooled.size), 'cache-control': 'no-store',
        ...(spooled.durationMs === undefined ? {} : { 'x-voice-duration-ms': String(spooled.durationMs) }) });
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

  /** The FileBox of one voice bar: `pcm` through the fork's encoder - the live session's
   *  encodeVoice(pcmBase64) -> {voice, durationMs} (base64 SILK) - named `<durationMs>.silk`, the
   *  name the fork's messageSendFile reads the length from. Throws when there is no encoder, it
   *  refuses, or its note is not one of at most voiceMaxMs: the caller then sends a file. */
  async function voiceBox(pcm) {
    const session = bot?.puppet?.session;
    if (typeof session?.encodeVoice !== 'function') throw new Error('the puppet has no voice encoder');
    const note = await session.encodeVoice(pcm.toString('base64'));
    const durationMs = note?.durationMs;
    if (typeof note?.voice !== 'string' || !note.voice || !Number.isSafeInteger(durationMs) || durationMs <= 0
      || durationMs > mediaSend.voiceMaxMs) throw new Error('the voice encoder gave no note a voice bar can carry');
    return makeFileBox(Buffer.from(note.voice, 'base64'), `${durationMs}.silk`);
  }

  /** Sends `bytes` by each step of `plan` (sendPlan) in turn until one goes (a voice bar or an
   *  image refused or failed goes again as a file): [200, {ref, sent_as}]. A peer neo no longer
   *  knows is recipient_unknown; a send whose outcome the fork calls uncertain may have gone, so
   *  it is a failed send and nothing is sent again. */
  async function deliverFile(say, bytes, plan) {
    let failure;
    for (const [i, step] of plan.entries()) {
      try {
        const box = step.as === 'voice' ? await voiceBox(step.pcm) : makeFileBox(bytes, step.name);
        const sent = await say(box);
        return [200, { ref: sent?.id ?? null, sent_as: step.as }];
      } catch (error) {
        if (/observed peer/.test(String(error?.message))) return [409, { error: 'recipient_unknown' }];
        if (/outcome is uncertain/.test(String(error?.message))) throw error;
        failure = error;
        if (i + 1 < plan.length) log(`sending as ${step.as} failed (${short(error)}); sending it as ${plan[i + 1].as}`);
      }
    }
    throw failure;
  }

  async function sendFile({ to, room }, bytes, plan) {
    if (room) return groupSend(room, target => deliverFile(box => target.say(box), bytes, plan));
    if (!state.logged_in || !bot) return [409, { error: 'not_logged_in' }];
    const contact = await bot.Contact.find({ id: to });
    if (!contact) return [409, { error: 'recipient_unknown' }];
    return deliverFile(box => contact.say(box), bytes, plan);
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
          logged_in: state.logged_in, since: state.since, resumable: resumable() };
        // Ruling 6: the fork's write counts, the reader of how often it writes; only with
        // saving on, so a write failure never looks like off and off never shows counts.
        const counts = saving ? savedStatus() : undefined;
        if (counts) Object.assign(body, { writes: count(counts.writes), write_failures: count(counts.write_failures) });
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
      // A media_id is '<seq>.<index>', as the bridge made it; anything else is not one.
      if (request.method === 'GET' && mediaPath) return /^\d+\.\d+$/.test(mediaPath[1])
        ? await serveMedia(mediaPath[1], response) : reply(response, 404, { error: 'not_found' });
      if (request.method === 'POST' && url.pathname === '/send-file') {
        // Step 24.2: ?to= (the bound owner) or ?room= (a bound group), kind, mime, name; the body
        // is the file's bytes, at most mediaLimits.sendBody.
        const bytes = await readBytes(request, mediaLimits.sendBody);
        const query = url.searchParams;
        const to = query.get('to') ?? '', room = query.get('room') ?? '', kind = query.get('kind') ?? '';
        const mime = query.get('mime') ?? '', name = query.get('name') ?? '';
        const plan = MEDIA_KINDS.includes(kind) && name.length <= 255 && mime.length <= 200
          ? sendPlan({ kind, name, bytes }, mediaSend) : undefined;
        if (!plan || !bytes.length || Boolean(to) === Boolean(room)) return reply(response, 400, { error: 'bad_request' });
        if (typeof makeFileBox !== 'function') return reply(response, 501, { error: 'not_supported' });
        const [status, result] = await sendFile({ to, room }, bytes, plan);
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
        if (holding) {
          holding = false;
          clearTimeout(groupHoldTimer);
          if (held.length) log(`released ${held.length} held message(s) at the first PUT /rooms`);
          release(() => true);
        }
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

  // Step 19.1: a start with a saved login resumes it.
  if (saving && loginEnabled && bootStatus.present) resumeEpisode(0);

  return {
    server,
    state,
    /** Long polls waiting now (each holds a timer until it is answered or closed). */
    get waiting() { return waiters.size; },
    /** Live attachments (step 24.2): never more than the ring events that carry a media_id. */
    get live() { return live.size; },
    /** The durable copy: the sum of its stored payload sizes and its entry count, for the
     *  bound's measurement. */
    get storeBytes() { return store?.bytes ?? 0; },
    get storeCount() { return store?.count ?? 0; },
    storedIds: () => store?.ids() ?? [],
    /** Settles once every bot start/stop asked for so far has run. */
    settled: settle,
    listen: (port = DEFAULT_PORT, host = HOST) => new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(port, host, () => { server.off('error', reject); resolve(server.address()); });
    }),
    /** Stops the server and the current bot (main.mjs on SIGINT/SIGTERM). */
    close: async () => {
      generation++;
      clearTimers();
      clearTimeout(retryTimer);
      clearTimeout(groupHoldTimer);
      clearInterval(countTimer);
      held.length = 0;
      const old = bot;
      bot = undefined;
      for (const wake of waiters) wake();
      server.closeAllConnections?.();
      await new Promise(resolve => server.close(() => resolve()));
      if (old) await stopBot(old);
    },
  };
}
