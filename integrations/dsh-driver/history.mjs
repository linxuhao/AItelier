/** Read the pinned DSH persistence seam without starting an agent or taking a write lease. */
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { openSync, readSync, closeSync, fstatSync, readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { zstdDecompressSync } from 'node:zlib';
import { resolveDshEntry } from './dsh-entry.mjs';
import { historyPage, visibleTypes } from '../dsh-director/lib/core.mjs';

// historyPage and the visible event types are dsh-director's (lib/core.mjs); this
// file keeps the backend's readers and re-exports them for its callers.
export { historyPage, visibleTypes } from '../dsh-director/lib/core.mjs';

/**
 * Which DSH owns the persistence package this reader imports.
 *
 * The backend that serves history spawns this file with AITELIER_DSH_ENTRY set to
 * the driver binary it launched, so that stays the first answer. Everything else
 * — a test, a checkout, a host install — asks the same resolver the test suite
 * asks, instead of trusting a hard-coded /opt path that only exists in one image.
 */
async function entryForReader(explicit) {
  if (explicit) return explicit;
  if (process.env.AITELIER_DSH_ENTRY) return process.env.AITELIER_DSH_ENTRY;
  const resolution = await resolveDshEntry();
  if (resolution.status !== 'measured')
    throw new Error(`cannot read DSH history: ${resolution.reason}`);
  return resolution.entry;
}

/** Byte ranges of the zstd frames of a DSH session log, read from frame and block
 *  headers only: nothing is decompressed. DSH writes about one frame per event, so a
 *  page of history needs a handful of frames, not the whole log (a full read of the
 *  aitelier director's 160 MB log peaked at ~2.5 GB RSS in the backend's cgroup,
 *  2026-11-15). Returns [start, end, skippable] triples. */
export function zstdFrames(fd, size) {
  const frames = [], head = Buffer.alloc(18), bh = Buffer.alloc(3);
  let pos = 0;
  while (pos < size) {
    const start = pos;
    if (readSync(fd, head, 0, Math.min(18, size - pos), pos) < 8) break;
    const magic = head.readUInt32LE(0);
    if (magic >= 0x184D2A50 && magic <= 0x184D2A5F) { pos += 8 + head.readUInt32LE(4); if (pos > size) break; frames.push([start, pos, true]); continue; }
    if (magic !== 0xFD2FB528) throw new Error(`not a zstd frame at ${pos}`);
    const fhd = head[4], fcs = fhd >> 6, single = (fhd >> 5) & 1, checksum = (fhd >> 2) & 1, did = fhd & 3;
    pos += 5 + (single ? 0 : 1) + [0, 1, 2, 4][did] + [single ? 1 : 0, 2, 4, 8][fcs];
    for (;;) {
      if (readSync(fd, bh, 0, 3, pos) !== 3) { pos = size + 1; break; }
      const v = bh[0] | (bh[1] << 8) | (bh[2] << 16), last = v & 1, type = (v >> 1) & 3, bsize = v >>> 3;
      if (type === 3) throw new Error(`reserved zstd block at ${pos}`);
      pos += 3 + (type === 1 ? 1 : bsize);
      if (last) break;
    }
    if (checksum) pos += 4;
    // A frame still being appended by the live driver ends past the file: stop at
    // the last complete one instead of failing the read.
    if (pos > size) break;
    frames.push([start, pos, false]);
  }
  return frames;
}

function frameEvents(fd, [start, end, skip]) {
  if (skip) return [];
  const raw = Buffer.alloc(end - start);
  readSync(fd, raw, 0, raw.length, start);
  const events = [];
  for (const line of zstdDecompressSync(raw).toString('utf8').split('\n')) {
    if (!line) continue;
    const event = JSON.parse(line);
    if (Number.isInteger(event?.seq)) events.push(event);
  }
  return events;
}

/** The DSH v4 log of a session under a sessions root, or undefined. */
export function sessionLog(root, session) {
  if (!existsSync(root)) return undefined;
  for (const dir of readdirSync(root)) {
    const file = join(root, dir, session, 'session.v4.jsonl.zstd');
    if (existsSync(file)) return file;
  }
  return undefined;
}

/** historyPage over the tail of a zstd log: the same page, decompressing only the
 *  frames it needs (a binary search on seq, then backwards until the page is full). */
export function tailHistory(file, { before_seq = Number.MAX_SAFE_INTEGER, limit = 50 } = {}) {
  const fd = openSync(file, 'r');
  try {
    const frames = zstdFrames(fd, fstatSync(fd).size);
    const firstSeq = k => { for (; k < frames.length; k++) { const e = frameEvents(fd, frames[k]); if (e.length) return e[0].seq; } return Infinity; };
    // The last frame whose first event is before before_seq.
    let lo = 0, hi = frames.length - 1, at = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (firstSeq(mid) < before_seq) { at = mid; lo = mid + 1; } else hi = mid - 1; }
    const selected = [];
    for (let k = at; k >= 0 && selected.length <= limit; k--) {
      const events = frameEvents(fd, frames[k]).filter(e => e.seq < before_seq && visibleTypes.has(e.type));
      selected.unshift(...events);
    }
    return historyPage(selected.slice(-(limit + 1)), { before_seq, limit });
  } finally { closeSync(fd); }
}

export async function readHistory(root, session, { entry, ...options } = {}) {
  const log = sessionLog(root, session);
  if (log) return { session_id: session, ...tailHistory(log, options) };
  return fullReadHistory(root, session, { entry, ...options });
}

/** The whole-log read through DSH's persistence (the previous reader): kept for a
 *  session without a zstd log and as the reference the tail reader is tested against. */
export async function fullReadHistory(root, session, { entry, ...options } = {}) {
  const require = createRequire(await entryForReader(entry));
  const { Context } = await import(require.resolve('@deepseek-ai/cordis'));
  const storage = await import(require.resolve('@deepseek-ai/dsh-session-persistence-jsonl'));
  const ctx = new Context();
  const fork = ctx.plugin(storage, { root });
  let handle;
  try {
    await fork.await();
    handle = await ctx.sessionPersistence.open(session, 'read');
    const result = await handle.read();
    return { session_id: session, ...historyPage(result.events, options) };
  } finally {
    await handle?.close();
    await fork.dispose();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const [root, session, before, limit] = process.argv.slice(2);
    const result = await readHistory(root, session, { before_seq: Number(before), limit: Number(limit) });
    process.stdout.write(JSON.stringify(result));
  } catch (error) { console.error(String(error)); process.exitCode = 1; }
}
