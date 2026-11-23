/** The steward host's media library (step 24.2, design-steward 7.8.12 "存储" and "上限", 7.8.12a
 *  "24.2 要为复用交出的四件").
 *
 *  One writer per subdirectory: `<root>/<userId>/` holds this writer's files and its own
 *  `index.jsonl`. The root comes from the host (its own setting, given to every driver process;
 *  never the process's own `~`, which a host may keep elsewhere). Files are
 *  `<root>/<userId>/<YYYY-MM>/<sha256>.<ext>`, directories 0700 and files 0600, deduplicated
 *  by sha256: a second copy of the same bytes gets its own row and ref but points at the file
 *  the first one wrote (`file`, relative to the root, is therefore in every row - the month
 *  of `at` does not name it).
 *
 *  `ingest(meta, open)` is the library's one way in, and it knows nothing about bindings
 *  (that check belongs to a channel: lib/channels.mjs storeMedia does it BEFORE calling here,
 *  and a dashboard upload, 24.2b, comes in already judged). In order:
 *   1. idempotent by (channel, messageRef, index): a stored one answers its ref, and a
 *      refused one (any refusal of the closed set, recorded in the index too) answers the
 *      same refusal; `open` is not called (a channel's replay fetches nothing again and
 *      gets the first answer, so the relayed message it builds is the same request);
 *   2. a chat channel's inbound media are held to limits.inbound_per_channel_hour, counted
 *      from the index (so a restart keeps the count); one past it is refused too_large -
 *      the design's placeholder for every over-limit attachment;
 *   3. then `open({signal})` gives the bytes, counted as they come and cut at the bound (a
 *      declared size is never trusted) and under a deadline (first byte, then the whole
 *      file: a source that stalls is aborted and answered `unavailable`, so one stuck
 *      channel never holds up the ingests queued behind it); the type is the first bytes'
 *      signature in the shared table (media-types.json), else `type`; an image's pixel
 *      count is read from its header (at most limits.image_pixels) and a voice is read
 *      with ffprobe: a container that holds a video stream (an mp4, webm or ogg video
 *      shares its first bytes with audio) is `type`, and an inbound one's length is at most
 *      limits.voice_ms (no ffprobe, or one that cannot read it: `unsupported` - fail
 *      closed). A `generated` file (send_media's) is held to limits.outbound_bytes and has
 *      no length bound (design 7.8.12 "上限": the per-kind limits are inbound ones).
 *  Refusals of the closed set: too_large, type, unsupported, gone; a failure to read the
 *  source or to write, or a deadline, is `unavailable` (transient: the caller may ask
 *  again, and it is never recorded).
 *
 *  Refs are `m_` + 16 random hex characters, never a hash (they say nothing about whether
 *  two files are the same). Each row carries its AUDIENCE: `owner`, `group:<channel>:
 *  <group_ref>` or (a director's subdirectory, 24.2b) `project:<project_id>`;
 *  `covers(turnAudience, row)` is the one rule media_call and send_media use (an owner
 *  turn covers every ref, a group turn only its own group's).
 *
 *  Kept 30 days (cleanup(), daily while started) and at most limits.total_bytes in all,
 *  oldest first; refs still owed to a relay (`protect()`) are never cleaned. Logs carry a
 *  mime and a size, never a name or a byte.
 *
 *  A director's subdirectory (24.2b, design-steward 7.8.12a "一个写者一个子目录" and "导演给业主
 *  发媒体" items 2, 6, 7; pending 20): `d-<driver_id>/` is held to 1 GiB in all (limitsFor;
 *  the steward's stays 5 GiB), and the files the director sends the owner come in as channel
 *  'director', keyed by (director, request_key, index) with the sha256 the adapter read:
 *   - a keyed replay whose sha256 differs from the stored one (or from a recorded refusal's)
 *     is `idempotency_conflict` - never the old file under a new one's name;
 *   - at most limits.director_per_hour 'director' rows an hour, counted from the index (a
 *     restart keeps the count; a replay adds no row); one past it is `rate_limited` with
 *     `retry_after` seconds; the dashboard's own source is never counted here (its uploads
 *     are counted once, at the backend's upload route);
 *   - a write the disk cannot take (ENOSPC and its kind) is `store_failed`, not the
 *     transient `unavailable` a channel's or the dashboard's ingest answers.
 *  None of these three is recorded: a later call may succeed. media-gc.mjs runs cleanup() on
 *  one subdirectory with that writer's limits (the DSH host, for a director not running). */
import { createHash, randomBytes } from 'node:crypto';
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync, appendFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { join, relative, sep } from 'node:path';
import { spawn } from 'node:child_process';
import { Readable } from 'node:stream';

const TABLE = JSON.parse(readFileSync(new URL('../media-types.json', import.meta.url), 'utf8'));
export const MEDIA_TYPES = Object.freeze(TABLE.types);
/** The table's limits, plus the hourly count of the files a director sends the owner (design
 *  7.8.12a "导演给业主发媒体" item 6; JS-side only: the backend never counts them). */
export const MEDIA_LIMITS = Object.freeze({ ...TABLE.limits, director_per_hour: 20 });
/** What a director's subdirectory (`d-<driver_id>`) holds in all (pending 20). */
export const DIRECTOR_TOTAL_BYTES = 1024 * 1024 * 1024;
/** The limits of one writer's subdirectory: a director's `d-<driver_id>` is held to 1 GiB in
 *  all, every other writer (the steward's `<userId>`) to the table's total_bytes. */
export function limitsFor(userId) {
  return typeof userId === 'string' && userId.startsWith('d-')
    ? { ...MEDIA_LIMITS, total_bytes: DIRECTOR_TOTAL_BYTES } : { ...MEDIA_LIMITS };
}
export const MEDIA_KINDS = Object.freeze(['image', 'voice', 'file']);
/** What an attachment that did not come in says (the closed set of design-steward 7.8.12). */
export const MEDIA_REFUSALS = Object.freeze(['too_large', 'type', 'unsupported', 'gone']);
export const REF_PATTERN = /^m_[0-9a-f]{16}$/;
const CHANNEL_SOURCES = new Set(['slack', 'wechat']);
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
/** Write errors that mean the disk cannot take the file (answered store_failed to a director). */
const DISK_FULL = new Set(['ENOSPC', 'EDQUOT', 'EFBIG', 'EROFS', 'EIO']);
const SNIFF_BYTES = 64;
const DAY_MS = 24 * 3600 * 1000;

const signatureMatches = (buffer, signature) => signature.every(({ offset, hex }) => {
  const want = Buffer.from(hex, 'hex');
  return buffer.length >= offset + want.length && buffer.subarray(offset, offset + want.length).equals(want);
});
/** The type the first bytes name in the shared table, or undefined. */
export function sniff(buffer) {
  return MEDIA_TYPES.find(type => type.signatures.some(signature => signatureMatches(buffer, signature)));
}

/** {width, height} from an image's own header (png, gif, webp, jpeg), or undefined. No
 *  decoder: only the few header bytes each format puts its size in. */
export function imageSize(buffer, mime) {
  try {
    if (mime === 'image/png') return buffer.length >= 24 ? { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) } : undefined;
    if (mime === 'image/gif') return buffer.length >= 10 ? { width: buffer.readUInt16LE(6), height: buffer.readUInt16LE(8) } : undefined;
    if (mime === 'image/webp') {
      const chunk = buffer.toString('latin1', 12, 16);
      if (chunk === 'VP8 ' && buffer.length >= 30) return { width: buffer.readUInt16LE(26) & 0x3fff, height: buffer.readUInt16LE(28) & 0x3fff };
      if (chunk === 'VP8L' && buffer.length >= 25) {
        const [b0, b1, b2, b3] = buffer.subarray(21, 25);
        return { width: 1 + (((b1 & 0x3f) << 8) | b0), height: 1 + (((b3 & 0x0f) << 10) | (b2 << 2) | ((b1 & 0xc0) >> 6)) };
      }
      if (chunk === 'VP8X' && buffer.length >= 30) return { width: 1 + buffer.readUIntLE(24, 3), height: 1 + buffer.readUIntLE(27, 3) };
      return undefined;
    }
    if (mime === 'image/jpeg') {
      let i = 2;
      while (i + 4 <= buffer.length) {
        if (buffer[i] !== 0xff) return undefined;
        const marker = buffer[i + 1];
        if (marker === 0xff) { i += 1; continue; }
        if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) { i += 2; continue; }
        if ((marker >= 0xc0 && marker <= 0xcf) && ![0xc4, 0xc8, 0xcc].includes(marker))
          return i + 9 <= buffer.length ? { height: buffer.readUInt16BE(i + 5), width: buffer.readUInt16BE(i + 7) } : undefined;
        i += 2 + buffer.readUInt16BE(i + 2);
      }
    }
  } catch { return undefined; }
  return undefined;
}

/** The audience label of a turn ({kind: 'owner'}, a group turn, or a director's
 *  {kind: 'project', project_id}), or undefined when it cannot be named (a group turn of
 *  unknown group, a project turn without its id). */
export function audienceLabel(audience) {
  if (audience?.kind === 'owner') return 'owner';
  if (audience?.kind === 'group' && typeof audience.channel === 'string' && typeof audience.group_ref === 'string')
    return `group:${audience.channel}:${audience.group_ref}`;
  if (audience?.kind === 'project' && typeof audience.project_id === 'string' && audience.project_id)
    return `project:${audience.project_id}`;
  return undefined;
}
/** Whether a turn of `audience` may use this row: an owner turn every ref, a group turn only
 *  its own group's (design-steward 7.8.12 "受众标签"), a director's project turn only its own
 *  project's rows (7.8.12a). Anything unreadable: no. */
export function covers(audience, row) {
  if (audience?.kind === 'owner') return true;
  const label = audienceLabel(audience);
  return label !== undefined && (label.startsWith('group:') || label.startsWith('project:')) && row?.audience === label;
}

const KIND_NAMES = Object.freeze({ image: '图片', voice: '语音', file: '文件' });
const REFUSED_WORDS = Object.freeze({ too_large: '过大未收', type: '类型不收', unsupported: '未收：渠道不支持', gone: '未收：已取不到' });
const clock = ms => { const s = Math.round(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const bytes = n => n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`;
/** The placeholder the owner's conversation keeps for one attachment ([图片], [语音 0:23],
 *  [文件], [图片 过大未收]...): what lib/channels.mjs writes into the body, nothing else does. */
export function placeholder(attachment) {
  const name = KIND_NAMES[attachment?.kind] ?? '文件';
  if (attachment?.refused) return `[${name} ${REFUSED_WORDS[attachment.refused] ?? '未收'}]`;
  if (attachment?.kind === 'voice' && Number.isInteger(attachment.durationMs)) return `[${name} ${clock(attachment.durationMs)}]`;
  return `[${name}]`;
}
/** The one line a wake shows for one attachment ([语音 m_…，0:23], [图片 m_…，image/jpeg，1.2 MB]). */
export function attachmentLine(attachment) {
  if (!attachment?.ref) return placeholder(attachment);
  const name = KIND_NAMES[attachment.kind] ?? '文件';
  if (attachment.kind === 'voice' && Number.isInteger(attachment.durationMs)) return `[${name} ${attachment.ref}，${clock(attachment.durationMs)}]`;
  return `[${name} ${attachment.ref}${attachment.mime ? '，' + attachment.mime : ''}${Number.isInteger(attachment.size) ? '，' + bytes(attachment.size) : ''}]`;
}

class Refused extends Error { constructor(code) { super(code); this.refused = code; } }

/** What ffprobe reads of `path`: {ms, video} - its duration in ms and whether it holds a video
 *  stream (an attached picture, an audio file's cover art, is not one) - or a refusal: no
 *  ffprobe at `command` (or it fails, or times out, or names no duration) is `unsupported` -
 *  the file could not be measured. */
export function probeDuration(command, path, { timeoutMs = 15000 } = {}) {
  return new Promise(resolve => {
    let out = '', done = false;
    const finish = value => { if (!done) { done = true; clearTimeout(timer); resolve(value); } };
    let child;
    try {
      child = spawn(command, ['-v', 'error', '-show_entries', 'format=duration:stream=codec_type:stream_disposition=attached_pic',
        '-of', 'json', path], { stdio: ['ignore', 'pipe', 'ignore'] });
    } catch { finish({ refused: 'unsupported' }); return; }
    const timer = setTimeout(() => { child.kill('SIGKILL'); finish({ refused: 'unsupported' }); }, timeoutMs);
    timer.unref?.();
    child.on('error', () => finish({ refused: 'unsupported' }));
    child.stdout.on('data', chunk => { if (out.length < 65536) out += chunk; });
    child.on('close', code => {
      let probed;
      try { probed = JSON.parse(out); } catch { probed = undefined; }
      const seconds = Number.parseFloat(probed?.format?.duration);
      if (code !== 0 || !Number.isFinite(seconds) || seconds < 0 || !Array.isArray(probed?.streams)) { finish({ refused: 'unsupported' }); return; }
      const video = probed.streams.some(stream => stream?.codec_type === 'video' && stream?.disposition?.attached_pic !== 1);
      finish({ ms: Math.round(seconds * 1000), video });
    });
  });
}

const asIterable = source => {
  if (source && typeof source[Symbol.asyncIterator] === 'function') return source;
  if (source && typeof source.getReader === 'function') return Readable.fromWeb(source);
  if (Buffer.isBuffer(source) || source instanceof Uint8Array) return (async function* () { yield source; })();
  throw new Error('open() gave no byte stream');
};
/** How long a source may take: to its first byte, and to its last (25 MiB at ~210 KB/s). */
export const MEDIA_DEADLINES = Object.freeze({ firstByteMs: 30000, totalMs: 120000 });

/** @param root the media root the host gives; @param userId this writer's subdirectory.
 *  @param ffprobe the ffprobe command (the host's setting; default `ffprobe` on PATH).
 *  @param protect () => refs that must outlive cleanup (a relay not committed yet).
 *  @param limits overrides of this writer's limits (limitsFor(userId): a director's
 *  subdirectory 1 GiB in all, any other the table's), key by key. */
export function createMediaLibrary({ root, userId = 'owner', ffprobe = 'ffprobe', limits: overrides,
  deadlines = MEDIA_DEADLINES, now = () => Date.now(), log = () => {}, protect = () => [], random = n => randomBytes(n) } = {}) {
  if (typeof root !== 'string' || !root) throw new Error('media library: root is required');
  const limits = Object.freeze({ ...limitsFor(userId), ...(overrides ?? {}) });
  if (typeof userId !== 'string' || !/^[A-Za-z0-9_.-]+$/.test(userId) || userId.startsWith('.')) throw new Error('media library: bad userId');
  const home = join(root, userId);
  const indexFile = join(home, 'index.jsonl');
  const ensureDir = path => { mkdirSync(path, { recursive: true, mode: 0o700 }); chmodSync(path, 0o700); };
  let protectFn = protect;
  // `rows` are the stored files; `refusals` the keyed attachments that were refused
  // ({refused, channel, messageRef, index, at}: no ref, no file), kept in the same index so a
  // replay after a restart answers the same refusal.
  let rows = [], refusals = [];
  const isRefusal = row => MEDIA_REFUSALS.includes(row?.refused) && typeof row.channel === 'string'
    && typeof row.messageRef === 'string' && Number.isInteger(row.index) && typeof row.at === 'string';
  try {
    for (const line of readFileSync(indexFile, 'utf8').split('\n').filter(Boolean)) {
      let row;
      try { row = JSON.parse(line); } catch { continue; }
      if (REF_PATTERN.test(row?.ref) && typeof row.file === 'string') rows.push(row);
      else if (isRefusal(row)) refusals.push(row);
    }
  } catch (error) { if (error?.code !== 'ENOENT') log('media_index_unreadable', { error: String(error?.code ?? error).slice(0, 60) }); }
  const absolute = row => join(root, row.file);
  const inside = path => !relative(home, path).startsWith('..') && !relative(home, path).startsWith(sep);
  const rewrite = () => {
    ensureDir(home);
    const temporary = indexFile + '.tmp';
    writeFileSync(temporary, [...rows, ...refusals].map(row => JSON.stringify(row) + '\n').join(''), { mode: 0o600 });
    renameSync(temporary, indexFile);
  };
  const append = row => { ensureDir(home); appendFileSync(indexFile, JSON.stringify(row) + '\n', { mode: 0o600 }); };
  // The per-kind limits are inbound ones; a generated file (send_media's) is held to the outbound one.
  const capFor = (kind, source) => source === 'generated' ? limits.outbound_bytes
    : kind === 'image' ? limits.image_bytes : kind === 'voice' ? limits.voice_bytes : limits.file_bytes;
  const maxBytes = Math.max(limits.image_bytes, limits.file_bytes, limits.voice_bytes, limits.outbound_bytes);

  const find = (channel, messageRef, index) => rows.find(row => row.channel === channel && row.messageRef === messageRef
    && row.index === index && existsSync(absolute(row)));
  const findRefusal = (channel, messageRef, index) => refusals.find(row => row.channel === channel
    && row.messageRef === messageRef && row.index === index);
  const MAX_REFUSALS = 10000;
  const recordRefusal = (meta, refused) => {
    const row = { refused, channel: meta.channel, messageRef: meta.messageRef, index: meta.index,
      ...(typeof meta.sha256 === 'string' ? { sha256: meta.sha256 } : {}), at: new Date(now()).toISOString() };
    refusals.push(row);
    if (refusals.length > MAX_REFUSALS) { refusals = refusals.slice(-MAX_REFUSALS); rewrite(); } else append(row);
  };
  const lookup = ref => {
    if (typeof ref !== 'string' || !REF_PATTERN.test(ref)) return undefined;
    const row = rows.find(r => r.ref === ref);
    if (!row) return undefined;
    const path = absolute(row);
    if (!inside(path) || !existsSync(path)) return undefined;
    const type = MEDIA_TYPES.find(t => t.mime === row.mime);
    return { ...row, path, kind: type?.kind ?? 'file', ext: type?.ext ?? 'bin' };
  };
  const freshRef = () => { for (;;) { const ref = 'm_' + Buffer.from(random(8)).toString('hex'); if (!rows.some(r => r.ref === ref)) return ref; } };

  /** open()'s bytes under the deadlines: open() and every chunk race an abort that fires at
   *  deadlines.firstByteMs with no byte yet, at deadlines.totalMs in all, or when `outer`
   *  aborts; the source is then let go (its iterator returned, open() told by its signal)
   *  and the answer is `unavailable`. */
  async function receive(meta, open, outer) {
    const controller = new AbortController();
    const stop = () => controller.abort();
    const firstByte = setTimeout(stop, deadlines.firstByteMs);
    const total = setTimeout(stop, deadlines.totalMs);
    outer?.addEventListener?.('abort', stop, { once: true });
    if (outer?.aborted) stop();
    const stopped = new Promise((_, reject) => {
      const fail = () => reject(new Refused('unavailable'));
      if (controller.signal.aborted) fail(); else controller.signal.addEventListener('abort', fail, { once: true });
    });
    stopped.catch(() => {});
    const timely = promise => Promise.race([promise, stopped]);
    let iterator, finished = false;
    try {
      let source;
      try { source = await timely(Promise.resolve().then(() => open({ signal: controller.signal }))); }
      catch (error) {
        if (error instanceof Refused) throw error;
        if (MEDIA_REFUSALS.includes(error?.refused)) throw new Refused(error.refused);
        throw new Refused('unavailable');
      }
      if (source && typeof source === 'object' && MEDIA_REFUSALS.includes(source.refused)) throw new Refused(source.refused);
      const chunks = [];
      let size = 0, type, cap = maxBytes, head = Buffer.alloc(0);
      try {
        iterator = asIterable(source)[Symbol.asyncIterator]();
        for (;;) {
          const step = await timely(iterator.next());
          if (step.done) { finished = true; break; }
          clearTimeout(firstByte);
          const piece = Buffer.from(step.value);
          size += piece.length;
          if (!type && head.length < SNIFF_BYTES) head = Buffer.concat([head, piece.subarray(0, SNIFF_BYTES - head.length)]);
          if (!type && head.length >= SNIFF_BYTES) {
            type = sniff(head);
            if (!type) throw new Refused('type');
            cap = capFor(type.kind, meta.source);
          }
          if (size > cap) throw new Refused('too_large');
          chunks.push(piece);
        }
      } catch (error) {
        if (error instanceof Refused) throw error;
        if (MEDIA_REFUSALS.includes(error?.refused)) throw new Refused(error.refused);
        throw new Refused('unavailable');
      }
      type ??= sniff(head);
      if (!type) throw new Refused('type');
      if (size > capFor(type.kind, meta.source)) throw new Refused('too_large');
      return { buffer: Buffer.concat(chunks, size), size, type };
    } finally {
      clearTimeout(firstByte); clearTimeout(total);
      outer?.removeEventListener?.('abort', stop);
      // Let a source we stopped reading go (never awaited: a stalled one may never answer).
      if (iterator && !finished) { controller.abort(); try { Promise.resolve(iterator.return?.()).catch(() => {}); } catch { /* gone */ } }
    }
  }

  /** A write that failed: a director's file the disk cannot take is store_failed (its tool
   *  says so); everything else is unavailable (a channel's or the dashboard's ingest asks again). */
  const writeFailed = (meta, type, size, error) => {
    log('media_store_failed', { mime: type.mime, size, error: String(error?.code ?? 'error').slice(0, 40) });
    return new Refused(meta.channel === 'director' && DISK_FULL.has(error?.code) ? 'store_failed' : 'unavailable');
  };

  async function store(meta, open, signal) {
    const { buffer, size, type } = await receive(meta, open, signal);
    let durationMs;
    if (type.kind === 'image') {
      const dims = imageSize(buffer, type.mime);
      if (!dims || !(dims.width > 0) || !(dims.height > 0)) throw new Refused('unsupported');
      if (dims.width * dims.height > limits.image_pixels) throw new Refused('too_large');
    }
    const sha256 = createHash('sha256').update(buffer).digest('hex');
    const at = new Date(now());
    const month = at.toISOString().slice(0, 7);
    const existing = rows.find(row => row.sha256 === sha256 && existsSync(absolute(row)));
    let file = existing?.file;
    const temporary = join(home, `.incoming-${Buffer.from(random(8)).toString('hex')}`);
    try {
      ensureDir(home);
      if (type.kind === 'voice') {
        if (existing && Number.isInteger(existing.durationMs)) durationMs = existing.durationMs;
        else {
          writeFileSync(temporary, buffer, { mode: 0o600 });
          const probed = await probeDuration(ffprobe, temporary);
          if (probed.refused) throw new Refused(probed.refused);
          // An mp4, webm or ogg VIDEO shares its first bytes with audio: only images, audio
          // and PDF are taken (design 7.8.12 "上限").
          if (probed.video) throw new Refused('type');
          durationMs = probed.ms;
        }
        if (meta.source !== 'generated' && durationMs > limits.voice_ms) throw new Refused('too_large');
      }
      if (!file) {
        ensureDir(join(home, month));
        file = `${userId}/${month}/${sha256}.${type.ext}`;
        if (!existsSync(temporary)) writeFileSync(temporary, buffer, { mode: 0o600 });
        renameSync(temporary, join(root, file));
      }
    } catch (error) {
      if (error instanceof Refused) throw error;
      throw writeFailed(meta, type, size, error);
    } finally { rmSync(temporary, { force: true }); }
    const row = { ref: freshRef(), sha256, mime: type.mime, size, ...(durationMs !== undefined ? { durationMs } : {}),
      audience: meta.audience, source: meta.source, channel: meta.channel,
      ...(typeof meta.messageRef === 'string' ? { messageRef: meta.messageRef } : {}),
      ...(Number.isInteger(meta.index) ? { index: meta.index } : {}), file, at: at.toISOString() };
    try { append(row); }
    catch (error) {
      // The index did not take the row: no ref, and a file this call wrote goes again.
      if (!rows.some(other => other.file === file)) rmSync(join(root, file), { force: true });
      throw writeFailed(meta, type, size, error);
    }
    rows.push(row);
    log('media_stored', { mime: row.mime, size: row.size });
    enforceTotal(new Set([row.ref]));
    return row;
  }

  let lane = Promise.resolve();
  const AUDIENCE = /^(owner|group:.+|project:.+)$/s;
  /** @param meta {channel, messageRef?, index?, audience: 'owner' | 'group:<channel>:<ref>' |
   *    'project:<project_id>', source: 'in' | 'generated', sha256?} (sha256: the bytes the
   *    caller read, for a keyed replay's idempotency_conflict) @param open ({signal}) => a byte
   *    stream (async iterable, web stream or Buffer); throwing {refused: <closed set>} passes
   *    that refusal on. @param signal the caller's: aborted while this waits for the lane, the
   *    answer is `unavailable` at once and nothing is fetched; aborted while it reads, the
   *    read stops. Ingests run one at a time; each source is under the deadlines. */
  const ingest = (meta = {}, open, { signal } = {}) => {
    const run = lane.then(async () => {
      if (typeof meta.audience !== 'string' || !AUDIENCE.test(meta.audience)) return { refused: 'invalid_media' };
      if (!['in', 'generated'].includes(meta.source) || typeof meta.channel !== 'string' || !meta.channel) return { refused: 'invalid_media' };
      if (typeof open !== 'function') return { refused: 'invalid_media' };
      if (meta.sha256 !== undefined && (typeof meta.sha256 !== 'string' || !SHA256_PATTERN.test(meta.sha256))) return { refused: 'invalid_media' };
      if (signal?.aborted) return { refused: 'unavailable' };
      const keyed = typeof meta.messageRef === 'string' && Number.isInteger(meta.index);
      // The same key with other bytes is not a replay (design 7.8.12a "导演给业主发媒体" item 2).
      const conflicts = row => typeof meta.sha256 === 'string' && typeof row.sha256 === 'string' && row.sha256 !== meta.sha256;
      if (keyed) {
        const stored = find(meta.channel, meta.messageRef, meta.index);
        if (stored && conflicts(stored)) { log('media_refused', { reason: 'idempotency_conflict' }); return { refused: 'idempotency_conflict' }; }
        if (stored) return { ref: stored.ref, kind: lookup(stored.ref)?.kind, mime: stored.mime, size: stored.size,
          ...(stored.durationMs !== undefined ? { durationMs: stored.durationMs } : {}), duplicate: true };
        const refusedBefore = findRefusal(meta.channel, meta.messageRef, meta.index);
        if (refusedBefore && conflicts(refusedBefore)) { log('media_refused', { reason: 'idempotency_conflict' }); return { refused: 'idempotency_conflict' }; }
        if (refusedBefore) return { refused: refusedBefore.refused, duplicate: true };
      }
      // A refusal of the closed set is the attachment's answer for good (recorded by its key);
      // `unavailable` is transient and never recorded.
      const refuse = code => {
        if (keyed && MEDIA_REFUSALS.includes(code)) {
          try { recordRefusal(meta, code); }
          catch (error) { log('media_store_failed', { error: String(error?.code ?? 'error').slice(0, 40) }); }
        }
        return { refused: code };
      };
      if (meta.source === 'in' && CHANNEL_SOURCES.has(meta.channel)) {
        const hour = now() - 3600000;
        const taken = rows.filter(row => row.source === 'in' && row.channel === meta.channel && Date.parse(row.at) > hour).length;
        if (taken >= limits.inbound_per_channel_hour) { log('media_refused', { reason: 'rate' }); return refuse('too_large'); }
      }
      if (meta.channel === 'director') {
        const hour = now() - 3600000;
        const taken = rows.filter(row => row.channel === 'director' && Date.parse(row.at) > hour).map(row => Date.parse(row.at)).sort((a, b) => a - b);
        if (taken.length >= limits.director_per_hour) {
          // When the oldest counted row leaves the hour, one more fits.
          const retryAfter = Math.max(1, Math.ceil((taken[taken.length - limits.director_per_hour] - hour) / 1000));
          log('media_refused', { reason: 'rate' });
          return { refused: 'rate_limited', retry_after: retryAfter };
        }
      }
      try {
        const row = await store(meta, open, signal);
        return { ref: row.ref, kind: lookup(row.ref)?.kind, mime: row.mime, size: row.size,
          ...(row.durationMs !== undefined ? { durationMs: row.durationMs } : {}) };
      } catch (error) {
        const code = error instanceof Refused ? error.refused : 'unavailable';
        log('media_refused', { reason: code });
        return refuse(code);
      }
    });
    lane = run.catch(() => {});
    if (!signal) return run;
    // The caller that gives up stops waiting for the lane (its turn, when it comes, is skipped).
    return Promise.race([run, new Promise(resolve => {
      if (signal.aborted) resolve({ refused: 'unavailable' });
      else signal.addEventListener('abort', () => resolve({ refused: 'unavailable' }), { once: true });
    })]);
  };

  const protectedRefs = () => { try { return new Set(protectFn() ?? []); } catch { return new Set(rows.map(r => r.ref)); } };
  const dropRows = victims => {
    if (!victims.size) return 0;
    const gone = rows.filter(row => victims.has(row.ref));
    rows = rows.filter(row => !victims.has(row.ref));
    rewrite();
    for (const row of gone) if (!rows.some(other => other.file === row.file)) {
      const path = absolute(row);
      if (inside(path)) rmSync(path, { force: true });
    }
    return gone.length;
  };
  const totalBytes = () => { const seen = new Map(); for (const row of rows) seen.set(row.file, row.size); return [...seen.values()].reduce((a, b) => a + b, 0); };
  function enforceTotal(keep = new Set()) {
    if (totalBytes() <= limits.total_bytes) return 0;
    const hold = protectedRefs();
    const victims = new Set();
    const ordered = [...rows].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
    let total = totalBytes();
    for (const row of ordered) {
      if (total <= limits.total_bytes) break;
      if (keep.has(row.ref) || hold.has(row.ref)) continue;
      victims.add(row.ref);
      const remaining = rows.filter(r => !victims.has(r.ref));
      const seen = new Map(); for (const r of remaining) seen.set(r.file, r.size);
      total = [...seen.values()].reduce((a, b) => a + b, 0);
    }
    const n = dropRows(victims);
    if (n) log('media_evicted', { rows: n });
    return n;
  }
  /** Rows past retention_days (and their files, once no row points at them), then the total
   *  bound oldest first; a protected ref is never removed. */
  function cleanup() {
    const cutoff = now() - limits.retention_days * DAY_MS, hold = protectedRefs();
    const expired = new Set(rows.filter(row => Date.parse(row.at) < cutoff && !hold.has(row.ref)).map(row => row.ref));
    const removed = dropRows(expired);
    const kept = refusals.filter(row => Date.parse(row.at) >= cutoff);
    if (kept.length !== refusals.length) { refusals = kept; rewrite(); }
    const evicted = enforceTotal();
    if (removed) log('media_expired', { rows: removed });
    return { removed, evicted };
  }
  let timer;
  return {
    root, home, indexFile, limits, ingest, lookup, find, cleanup, enforceTotal, covers,
    get rows() { return rows.map(row => ({ ...row })); },
    totalBytes,
    /** The bytes of a ref (undefined when the ref is unknown or its file is gone). */
    async read(ref) { const row = lookup(ref); return row ? { row, bytes: await readFile(row.path) } : undefined; },
    setProtected(fn) { if (typeof fn === 'function') protectFn = fn; },
    /** Take back a file this host fetched for send_media and did not send (review MF3): the
     *  `generated` row `ref`, and its file once no other row points at it (a file stored before,
     *  shared by sha256, stays). A protected ref, an inbound row or an unknown ref is left alone. */
    withdraw(ref) {
      const row = rows.find(r => r.ref === ref);
      if (!row || row.source !== 'generated' || protectedRefs().has(ref)) return { removed: 0 };
      return { removed: dropRows(new Set([ref])) };
    },
    /** Every file and row of this writer (the dashboard's 清空媒体). */
    clear() { const n = rows.length; rows = []; refusals = []; rmSync(home, { recursive: true, force: true }); return { removed: n }; },
    start(intervalMs = DAY_MS) {
      if (timer) return;
      try { cleanup(); } catch (error) { log('media_cleanup_failed', { error: String(error?.code ?? 'error').slice(0, 40) }); }
      timer = setInterval(() => { try { cleanup(); } catch (error) { log('media_cleanup_failed', { error: String(error?.code ?? 'error').slice(0, 40) }); } }, intervalMs);
      timer.unref?.();
    },
    stop() { clearInterval(timer); timer = undefined; },
  };
}
