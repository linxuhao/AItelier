/** The optional local vector channel (plugin step 21, design-steward 3.8.2): a second ranked
 *  channel beside the built-in BM25, fused with it by RRF in the store. Not a third backend:
 *  it ranks the same local entries, and every hit is read through the store's `entries`.
 *
 *  The embedding server is a local llama.cpp `llama-server --embedding` (OpenAI-compatible
 *  POST <url>/v1/embeddings, L2-normalised); this module only ever talks to the loopback url
 *  the config allows (memoryVectorProblem). Vectors are int8 (x127, rounded), kept in memory
 *  in fixed blocks of Int8Array and on disk in `<root>/<userId>.vectors.jsonl` beside the repository:
 *    {"model":"bge-m3-q8_0","dims":1024,"quant":"int8","input_chars":1000}   the header
 *    {"id":"t-…","v":"<base64 of dims int8 bytes>"}                         one line per entry
 *  The file is derived data: appended without fsync, filtered (never rewritten from memory)
 *  by streaming it into a temporary file, fsync, rename, directory fsync. Every rewrite and
 *  every append runs under the store's write lock; a load only reads (a rename is atomic).
 *
 *  Embedding is never on the write path: one background worker embeds what has no vector,
 *  newest first, one request at a time, after the load and its reconciliation are done.
 *  Logs carry ids, counts and reasons, never memory text. */
import { closeSync, constants, createReadStream, existsSync, openSync, readSync, statSync, writeSync, chmodSync } from 'node:fs';
import { access, open as openFile, readdir, rename, unlink } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { basename, dirname, join } from 'node:path';
import { VECTOR_MODELS } from '../host.mjs';
import { due, turn } from './slices.mjs';

/** Only the first code points of an entry (and of a query) are embedded: bge-m3 is a
 *  bidirectional encoder whose whole input must fit one ubatch (1024 tokens). */
export const INPUT_CHARS = 1000;
/** The retry after the server said the input was too long. */
export const RETRY_CHARS = 400;
export const RRF_K = 60;
/** Each channel's candidates for a top-k search (Resurface's shape). */
export const candidates = k => Math.max(5 * k, 50);
export const DEFAULT_TIMEOUT_MS = 5000;
const YIELD_ROWS = 10000;
/** Rows per block of the vectors in memory (4 MB at 1024 dims). */
const BLOCK_ROWS = 4096;
const DOWN_NOTICE_MS = 10 * 60 * 1000;
const SKIP = Symbol('skip');

/** The first `n` code points (never half a surrogate pair, which the server's JSON parser
 *  would refuse). */
export const firstChars = (text, n) => Array.from(String(text)).slice(0, n).join('');
const yieldLoop = () => new Promise(resolve => setImmediate(resolve));

export function headerLine(model) {
  return JSON.stringify({ model, dims: VECTOR_MODELS[model].dims, quant: 'int8', input_chars: INPUT_CHARS });
}

/** A header line's fields, or undefined when it is not a header. */
export function parseHeader(line) {
  let h;
  try { h = JSON.parse(line); } catch { return undefined; }
  if (!h || typeof h !== 'object' || typeof h.model !== 'string' || !Number.isSafeInteger(h.dims) || h.dims <= 0
    || h.quant !== 'int8' || !Number.isSafeInteger(h.input_chars)) return undefined;
  return h;
}

/** A data line as {id, v: Buffer of `dims` bytes}, or undefined (a torn or foreign line).
 *  With `scratch` (a Buffer the caller reuses) the bytes are decoded into it and `v` is a
 *  view of it, valid until the next call: no Buffer per row (a load made 100k of them). A
 *  string that could decode to more than `scratch` holds takes the allocating path, so the
 *  answer is the same either way. */
export function parseRow(line, dims, scratch) {
  let row;
  try { row = JSON.parse(line); } catch { return undefined; }
  if (!row || typeof row.id !== 'string' || !row.id || typeof row.v !== 'string') return undefined;
  if (scratch && Math.floor(row.v.length * 3 / 4) <= scratch.length) {
    const n = scratch.write(row.v, 'base64');
    return n === dims ? { id: row.id, v: scratch.subarray(0, dims) } : undefined;
  }
  const v = Buffer.from(row.v, 'base64');
  return v.length === dims ? { id: row.id, v } : undefined;
}

/** A float vector as int8: x127, rounded, clamped. */
export function quantize(values) {
  const out = new Int8Array(values.length);
  for (let i = 0; i < values.length; i++) out[i] = Math.max(-127, Math.min(127, Math.round(values[i] * 127)));
  return out;
}

const rowLine = (id, v) => JSON.stringify({ id, v: Buffer.from(v.buffer, v.byteOffset, v.length).toString('base64') });

async function fsyncDirOf(file) {
  const dir = await openFile(dirname(file), 'r');
  try { await dir.sync(); } finally { await dir.close(); }
}

function lines(file) {
  return createInterface({ input: createReadStream(file, { encoding: 'utf8' }), crlfDelay: Infinity });
}

/** The lines of a file as `lines` gives them (ends: \n, \r\n, \r; a last line without
 *  one; each line decoded whole), read through one reused buffer: the load's 139 MB file at
 *  100k was 2,000 transient 64 KB chunks and 100k row Buffers between the vector blocks in
 *  the allocator, and the RSS kept 20-50 MB of them (plugin step 21 perf follow-up). */
export async function* fileLines(file, size = 1 << 18) {
  const fh = await openFile(file, 'r');
  try {
    const buf = Buffer.allocUnsafe(size);
    let carry = null, skipLf = false;
    for (;;) {
      const { bytesRead } = await fh.read(buf, 0, size, null);
      if (!bytesRead) break;
      let start = 0, i = 0;
      if (skipLf && buf[0] === 0x0a) start = i = 1;
      skipLf = false;
      let cr = buf.indexOf(0x0d, i), lf = buf.indexOf(0x0a, i);
      for (;;) {
        if (cr !== -1 && cr >= bytesRead) cr = -1;
        if (lf !== -1 && lf >= bytesRead) lf = -1;
        const end = cr === -1 ? lf : lf === -1 ? cr : Math.min(cr, lf);
        if (end === -1) break;
        const line = carry ? Buffer.concat([carry, buf.subarray(start, end)]).toString('utf8') : buf.toString('utf8', start, end);
        carry = null;
        let next = end + 1;
        if (buf[end] === 0x0d) { if (next < bytesRead) { if (buf[next] === 0x0a) next++; } else skipLf = true; }
        start = next;
        if (cr !== -1 && cr < start) cr = buf.indexOf(0x0d, start);
        if (lf !== -1 && lf < start) lf = buf.indexOf(0x0a, start);
        yield line;
      }
      if (start < bytesRead) carry = carry ? Buffer.concat([carry, buf.subarray(start, bytesRead)]) : Buffer.from(buf.subarray(start, bytesRead));
    }
    if (carry && carry.length) yield carry.toString('utf8');
  } finally { await fh.close(); }
}

/** Stream a vector file: its header line, its valid data rows, its bad lines, and how many
 *  rows name one of `ids`. `{exists: false}` when there is no file. */
export async function scanVectorFile(file, ids = new Set()) {
  if (!existsSync(file)) return { exists: false };
  let first, header, rows = 0, bad = 0, hits = 0, n = 0;
  for await (const line of lines(file)) {
    if (first === undefined) { first = line; header = parseHeader(line); continue; }
    if (!line) continue;
    const row = header && parseRow(line, header.dims);
    if (!row) bad++;
    else { rows++; if (ids.has(row.id)) hits++; }
    if (++n % YIELD_ROWS === 0) await yieldLoop();
  }
  return { exists: true, headerLine: first ?? '', header, rows, bad, hits };
}

/** Remove the rows naming `drop` from the file ON DISK (design 3.8.2 删行): stream it into a
 *  temporary file, fsync, check both sides, rename, fsync the directory. The original is
 *  untouched unless every check passed. Used by the reconciliation, by forget (best effort)
 *  and by purge step 1, whether or not vectors are configured now: the header is the file's
 *  own, never the config's.
 *
 *  The two-sided check (run on the temporary file, before the rename): (a) no dropped id is
 *  left; (b) its rows = the original's valid rows - the dropped rows found (a torn line is
 *  dropped by the same rule as a load, and counts on neither side); (c) its header line is
 *  the original's, byte for byte; (d) every line parses and holds `dims` bytes. A file whose
 *  first line is not a header is deleted whole (derived: the next load embeds everything
 *  again). A file we may not write is refused (EACCES) when it has rows to lose, though a
 *  rename would replace it. A file with a torn line is rewritten even when no id is dropped
 *  (the torn line goes), so a purge's step 5, which wants every line whole, can pass.
 *  First, the temporary files a filter killed mid-way left (SIGKILL, power loss: its
 *  `finally` never ran) are removed: they hold rows, a purged id's among them. Every caller
 *  holds the write lock and one process holds the store, so none of them is in use.
 *  `write(line, i)` is a test hook over each line written (i = 0 for the header).
 *  Returns {exists: false} | {exists: true, deleted: true} | {exists: true, headerLine, rows, removed}. */
export async function filterVectorFile(file, drop, { write } = {}) {
  await removeStaleFilterFiles(file);
  if (!existsSync(file)) return { exists: false };
  const tmp = `${file}.filter-${process.pid}`;
  let first, header, fh, valid = 0, removed = 0, bad = 0, n = 0, chunk = '';
  const out = async (line, force = false) => {
    if (line !== null) chunk += line + '\n';
    if (fh && (force || chunk.length > 1 << 20)) { await fh.write(chunk); chunk = ''; }
  };
  const emit = (line, i) => (write ? write(line, i) : line);
  try {
    for await (const line of lines(file)) {
      if (first === undefined) {
        first = line; header = parseHeader(line);
        if (!header) break;
        fh = await openFile(tmp, 'w', 0o600);
        await out(emit(line, 0));
        continue;
      }
      if (!line) continue;
      const row = parseRow(line, header.dims);
      if (!row) { bad++; continue; }
      valid++;
      if (drop.has(row.id)) { removed++; continue; }
      await out(emit(line, valid));
      if (++n % YIELD_ROWS === 0) await yieldLoop();
    }
    if (!header) {
      await unlink(file); await fsyncDirOf(file);
      return { exists: true, deleted: true };
    }
    await out(null, true);
    await fh.sync(); await fh.close(); fh = undefined;
    if (!removed && !bad && !write) { await unlink(tmp); return { exists: true, headerLine: first, rows: valid, removed: 0 }; }
    await access(file, constants.W_OK);
    const after = await scanVectorFile(tmp, drop);
    const problems = [];
    if (after.hits) problems.push(`(a) ${after.hits} dropped rows left`);
    if (after.rows !== valid - removed) problems.push(`(b) ${after.rows} rows, expected ${valid} - ${removed}`);
    if (after.headerLine !== first) problems.push('(c) the header line changed');
    if (after.bad || !after.header) problems.push(`(d) ${after.bad} lines do not parse to ${header.dims} bytes`);
    if (problems.length) throw new Error('two-sided check failed: ' + problems.join('; '));
    chmodSync(tmp, 0o600);
    await rename(tmp, file);
    await fsyncDirOf(file);
    return { exists: true, headerLine: first, rows: valid - removed, removed };
  } finally {
    if (fh) await fh.close().catch(() => {});
    if (existsSync(tmp)) await unlink(tmp).catch(() => {});
  }
}

async function removeStaleFilterFiles(file) {
  const dir = dirname(file), prefix = basename(file) + '.filter-';
  let names;
  try { names = await readdir(dir); } catch (error) { if (error.code === 'ENOENT') return; throw error; }
  const stale = names.filter(name => name.startsWith(prefix));
  for (const name of stale) await unlink(join(dir, name));
  if (stale.length) await fsyncDirOf(file);
}

/** The vectors in memory: blocks of BLOCK_ROWS rows, one Int8Array each, so growing adds a
 *  block and never copies (a doubling array went from 104 to 208 MB, past the 120 MB budget,
 *  and its copy blocked the loop for 65 ms); a forgotten row is reused by the next new id.
 *  Row i's id at ids[i] (undefined while free), and id -> row. The file's line numbers have
 *  nothing to do with the rows. */
class Vectors {
  constructor(dims) { this.dims = dims; this.blocks = []; this.ids = []; this.rows = new Map(); this.free = []; }
  get size() { return this.rows.size; }
  has(id) { return this.rows.has(id); }
  set(id, v) {
    let row = this.rows.get(id);
    if (row === undefined) {
      if (this.free.length) row = this.free.pop();
      else {
        row = this.ids.length;
        this.ids.push(undefined);
        if (row % BLOCK_ROWS === 0) this.blocks.push(new Int8Array(BLOCK_ROWS * this.dims));
      }
      this.ids[row] = id; this.rows.set(id, row);
    }
    this.blocks[(row / BLOCK_ROWS) | 0].set(v, (row % BLOCK_ROWS) * this.dims);
  }
  delete(id) {
    const row = this.rows.get(id);
    if (row === undefined) return false;
    this.ids[row] = undefined; this.rows.delete(id); this.free.push(row);
    return true;
  }
}

function coded(code, message, extra = {}) {
  return Object.assign(new Error(message), { code, ...extra });
}

/** The channel. `store` gives it what it shares with the store: entries() (the live entries
 *  Map), locked(fn) (the write lock), log(event, fields), now() (ISO),
 *  notice(kind, text | undefined, fields) to add or clear the owner notice of a kind, and
 *  faults() (the store's test hooks). */
export class VectorChannel {
  constructor({ url, model, timeoutMs = DEFAULT_TIMEOUT_MS }, { file, store, tuning = {} }) {
    this.url = url.replace(/\/+$/, '');
    this.modelName = model;
    this.model = VECTOR_MODELS[model];
    this.dims = this.model.dims;
    this.header = headerLine(model);
    this.timeoutMs = timeoutMs;
    this.file = file;
    this.store = store;
    this.backoffMin = tuning.backoffMinMs ?? 5000;
    this.backoffMax = tuning.backoffMaxMs ?? 300000;
    this.workerTimeoutMs = tuning.workerTimeoutMs ?? Math.max(60000, timeoutMs);
    this.backoff = this.backoffMin;
    this.gen = 0;
    this.mem = new Vectors(this.dims);
    this.loading = false;
    this.loaded = false;
    this.fileOk = false;     // whether appends may go to the file (its header is ours, or it is absent)
    this.queue = [];
    this.skipped = new Set();
    this.down = undefined;   // {since, code, message}: the current failure, cleared by the first success
    this.outage = undefined; // {since}: from the first failure until healthy with no backlog
    this.answered = false;   // a request succeeded in this process: health is seen, not assumed
    this.closed = false;
    this.wakeUp = undefined;
    this.appendFailed = false;
    this.loadDone = Promise.resolve();
    this.worker = this.#work();
  }

  get source() { return `bm25+向量 · 本地索引 · ${this.modelName}`; }

  /** Start a (new) load of the file; an older load and the embeddings in flight for it are
   *  void from here on. Called after the store's #load at boot and after a purge's #load. */
  startLoad() {
    const gen = ++this.gen;
    this.loading = true; this.loaded = false; this.fileOk = false;
    this.queue = [];
    this.mem = new Vectors(this.dims);
    this.workerAbort?.abort();
    this.loadDone = this.#load(gen).catch(error => {
      if (gen === this.gen) { this.loading = false; this.loaded = true; }
      this.store.log('memory/vector-load-failed', { reason: String(error?.message ?? error).slice(0, 300) });
      this.#wake();
    });
    return this.loadDone;
  }

  whenLoaded() { return this.loadDone; }

  async #load(gen) {
    if (this.store.faults()?.vectorLoadPause) await this.store.faults().vectorLoadPause();
    if (gen !== this.gen) return;
    const mem = this.mem;
    const entries = this.store.entries();
    const extras = new Set();
    const scratch = Buffer.alloc(2 * this.dims);
    let rebuild;
    if (existsSync(this.file)) {
      let first = true, n = 0;
      const rl = fileLines(this.file);
      for await (const line of rl) {
        if (gen !== this.gen) return;
        if (first) {
          first = false;
          const h = parseHeader(line);
          if (!h) rebuild = 'no header';
          else if (h.model !== this.modelName || h.dims !== this.dims || h.input_chars !== INPUT_CHARS)
            rebuild = `header ${h.model}/${h.dims}/${h.input_chars}, config ${this.modelName}/${this.dims}/${INPUT_CHARS}`;
          if (rebuild) break;
          continue;
        }
        if (!line) continue;
        const row = parseRow(line, this.dims, scratch);
        if (!row) continue; // a torn last line: never acknowledged, the reconciliation queues it again
        // The entry's own id string is kept, not the one parsed from the file (100k strings).
        const entry = entries.get(row.id);
        if (entry) mem.set(entry.id, row.v); else extras.add(row.id);
        if (++n % YIELD_ROWS === 0) await yieldLoop();
      }
      if (first && !rebuild) rebuild = 'empty file';
    }
    if (gen !== this.gen) return;
    if (this.store.faults()?.vectorBeforeReconcile) await this.store.faults().vectorBeforeReconcile();
    if (gen !== this.gen) return;
    // What left `entries` while the load ran is dropped from memory and from the file too.
    for (const id of mem.rows.keys()) if (!entries.has(id)) { mem.delete(id); extras.add(id); }
    let fileOk = true;
    if (rebuild) {
      // 1. Another model, dims or input size: the whole file goes, everything is embedded again.
      await this.store.locked(async () => {
        if (gen !== this.gen) return;
        try { if (existsSync(this.file)) { await unlink(this.file); await fsyncDirOf(this.file); } }
        catch (error) { fileOk = false; this.store.log('memory/vector-filter-failed', { reason: String(error.message).slice(0, 200) }); }
        this.store.log('memory/vector-rebuild', { reason: rebuild });
      });
      mem.rows.clear(); mem.ids.length = 0;
    } else if (extras.size) {
      // 2. Ids no longer in `entries`: out of the file on disk (a failure only waits for the next load).
      await this.store.locked(async () => {
        if (gen !== this.gen) return;
        try { await filterVectorFile(this.file, extras); }
        catch (error) { this.store.log('memory/vector-filter-failed', { ids: [...extras].slice(0, 50), count: extras.size, reason: String(error.message).slice(0, 200) }); }
      });
    }
    if (gen !== this.gen) return;
    // 3. What `entries` has and the file lacks: the worker computes that list itself.
    this.fileOk = fileOk;
    this.loading = false; this.loaded = true;
    this.store.log('memory/vector-loaded', { vectors: mem.size, entries: entries.size, dropped: extras.size });
    this.#healthy();
    this.#wake();
  }

  /** New entries go to the front of the worker's list (#appendEntries). */
  enqueue(ids) {
    this.queue.unshift(...ids);
    this.#wake();
  }

  /** Forget: the vector leaves memory (it cannot fail); the file is the store's business. */
  drop(id) { this.mem.delete(id); }

  close() {
    this.closed = true; this.gen++;
    this.workerAbort?.abort();
    this.#wake();
    this.backoffDone?.();
  }

  #wake() {
    const wake = this.wakeUp;
    this.wakeUp = undefined;
    wake?.();
  }

  /** Sleep until woken (no `ms`), or for `ms`: a backoff that a new entry does not cut short
   *  (only close does). */
  #sleep(ms) {
    return new Promise(resolve => {
      if (this.closed) return resolve();
      if (ms === undefined) { this.wakeUp = resolve; return; }
      this.backoffDone = resolve;
      setTimeout(() => { this.backoffDone = undefined; resolve(); }, ms).unref?.();
    });
  }

  #next() {
    const entries = this.store.entries();
    const wanted = id => entries.has(id) && !this.mem.has(id) && !this.skipped.has(id);
    while (this.queue.length) { const id = this.queue.shift(); if (wanted(id)) return id; }
    // The list is empty: compute it once from the entries (newest first). They come in log
    // order, which is time order unless a restore or a clock step broke it; reversed, that is
    // newest first, and only an out-of-order list pays for a sort (100k: 25 ms at once).
    const list = [];
    let ordered = true;
    for (const e of entries.values()) if (wanted(e.id)) { if (list.length && list[list.length - 1].at > e.at) ordered = false; list.push(e); }
    if (ordered) list.reverse(); else list.sort((a, b) => (b.at > a.at ? 1 : b.at < a.at ? -1 : 0));
    this.queue = list.map(e => e.id);
    return this.queue.shift();
  }

  async #work() {
    await Promise.resolve();
    while (!this.closed) {
      if (!this.loaded) { await this.#sleep(); continue; }
      const gen = this.gen;
      const id = this.#next();
      if (id === undefined && this.answered) { this.#healthy(); await this.#sleep(); continue; }
      // Nothing to embed and no answer seen in this process: one probe, so health is seen and
      // not assumed - a vector-down notice an earlier process left goes once the server answers.
      const entry = id === undefined ? undefined : this.store.entries().get(id);
      let v;
      try { v = entry ? await this.#embedEntry(entry) : await this.#embed(this.model.query + 'health', this.workerTimeoutMs, true); }
      catch (error) {
        if (this.closed) return;
        if (gen === this.gen) {
          if (id !== undefined) this.queue.unshift(id);
          if (error.code !== 'ABORTED') {
            this.#failed(error);
            await this.#sleep(this.backoff);
            this.backoff = Math.min(this.backoff * 2, this.backoffMax);
          }
        }
        continue;
      }
      if (this.closed) return;
      this.backoff = this.backoffMin;
      this.#succeeded();
      if (!entry) continue;
      if (v === SKIP) {
        this.skipped.add(id);
        this.store.log('memory/vector-skipped', { id, length: entry.text.length });
        continue;
      }
      // Embedded outside the lock; kept only if, under the lock, the entry is still live and
      // no new load began (a purge swaps `entries` and starts one).
      await this.store.locked(() => {
        if (this.closed || gen !== this.gen || !this.loaded || !this.store.entries().has(id) || this.mem.has(id)) return;
        this.mem.set(id, v);
        this.#append(id, v);
      });
    }
  }

  async #embedEntry(entry) {
    const text = String(entry.text);
    try { return await this.#embed(this.model.passage + firstChars(text, INPUT_CHARS), this.workerTimeoutMs, true); }
    catch (error) { if (!error.tooLong) throw error; }
    try { return await this.#embed(this.model.passage + firstChars(text, RETRY_CHARS), this.workerTimeoutMs, true); }
    catch (error) { if (error.tooLong || error.status === 400) return SKIP; throw error; }
  }

  /** One embedding request; the vector as int8, or a coded error: TIMEOUT, DIMS, HTTP_<n>,
   *  BAD_RESPONSE, or the connection's own code (ECONNREFUSED, …). */
  async #embed(input, timeoutMs, worker = false) {
    const controller = new AbortController();
    if (worker) this.workerAbort = controller;
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    let response, body;
    try {
      response = await fetch(this.url + '/v1/embeddings', { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ input, model: this.modelName }), signal: controller.signal });
      body = await response.text();
    } catch (error) {
      if (timedOut) throw coded('TIMEOUT', `no answer within ${timeoutMs} ms`);
      if (controller.signal.aborted) throw coded('ABORTED', this.closed ? 'closed' : 'a new load began');
      const cause = error?.cause ?? error;
      throw coded(cause?.code ?? 'FETCH', String(cause?.message ?? error?.message ?? error));
    } finally {
      clearTimeout(timer);
      if (worker && this.workerAbort === controller) this.workerAbort = undefined;
    }
    if (!response.ok) {
      const tooLong = [400, 413, 500].includes(response.status) && /too (large|long)|exceed/i.test(body);
      throw coded(`HTTP_${response.status}`, body.slice(0, 200), { status: response.status, tooLong });
    }
    let values;
    try { values = JSON.parse(body)?.data?.[0]?.embedding; } catch { values = undefined; }
    if (!Array.isArray(values)) throw coded('BAD_RESPONSE', 'no data[0].embedding in the answer');
    if (values.length !== this.dims) throw coded('DIMS', `expected ${this.dims} dimensions, got ${values.length}`);
    return quantize(values);
  }

  /** Append one row (under the lock): no fsync - the file is derived; a lost last row is
   *  queued again by the next load, a torn one skipped. A torn last line gets its newline
   *  first, so the new row is never glued onto it. */
  #append(id, v) {
    if (!this.fileOk) return;
    try {
      let size = 0;
      try { size = statSync(this.file).size; } catch {}
      let prefix = '';
      if (!size) prefix = this.header + '\n';
      else {
        const fd = openSync(this.file, 'r');
        try { const last = Buffer.alloc(1); readSync(fd, last, 0, 1, size - 1); if (last[0] !== 0x0a) prefix = '\n'; } finally { closeSync(fd); }
      }
      const fd = openSync(this.file, constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT, 0o600);
      try { writeSync(fd, prefix + rowLine(id, v) + '\n'); } finally { closeSync(fd); }
      if (!size) chmodSync(this.file, 0o600);
      this.appendFailed = false;
    } catch (error) {
      // The vector stays in memory (search uses it); the next load queues it again.
      if (!this.appendFailed) this.store.log('memory/vector-append-failed', { id, reason: String(error.message).slice(0, 200) });
      this.appendFailed = true;
    }
  }

  /** The vector channel's ranking for `query`: up to `count` live entry ids, best first, or
   *  undefined when the query could not be embedded (the channel is down for this search). */
  async rank(query, count) {
    let q;
    try { q = await this.#embed(this.model.query + firstChars(query, INPUT_CHARS), this.timeoutMs); }
    catch (error) { this.#failed(error); return undefined; }
    this.#succeeded();
    const entries = this.store.entries();
    const mem = this.mem, dims = this.dims;
    const at = id => entries.get(id)?.at ?? '';
    // better(a, b): a ranks before b - higher score, then newer, then id.
    const better = (a, b) => a.score > b.score || (a.score === b.score && (at(a.id) > at(b.id) || (at(a.id) === at(b.id) && a.id < b.id)));
    const top = [];
    // The scan runs in turns (slices.mjs), starting with one of its own. This continuation
    // runs in the poll phase (the answer's socket), and a setImmediate from there runs in
    // the check phase of the same loop iteration, with no timer or I/O between: the first
    // two slices were one block (10-12 ms at 100k, plugin step 21 perf follow-up).
    await turn();
    for (let row = 0; row < mem.ids.length; row++) {
      const id = mem.ids[row];
      if (id !== undefined && entries.has(id)) {
        const data = mem.blocks[(row / BLOCK_ROWS) | 0], base = (row % BLOCK_ROWS) * dims;
        let score = 0;
        for (let i = 0; i < dims; i++) score += data[base + i] * q[i];
        // An object only for a row that may rank: one per row was 100k per search, and the
        // scavenges they caused landed inside the slices.
        const item = top.length < count || score >= top[top.length - 1].score ? { id, score } : undefined;
        if (item && (top.length < count || better(item, top[top.length - 1]))) {
          let i = top.length < count ? top.length : top.length - 1;
          if (top.length >= count) top.pop();
          while (i > 0 && better(item, top[i - 1])) i--;
          top.splice(i, 0, item);
        }
      }
      // Every 10,000 rows, and sooner on a slow (cold or busy) pass: no block past a few ms.
      // The clock is read every 16 rows: the first scan of a process runs unoptimized, where
      // 100 rows took 25 ms.
      if ((row + 1) % YIELD_ROWS === 0 || ((row + 1) % 16 === 0 && due())) await turn();
    }
    // What the caller does next (BM25, the fusion, the rendering) runs in a turn of its own,
    // not after the last slice.
    await turn();
    // Forgotten while the scan yielded: never a hit.
    return top.filter(t => entries.has(t.id)).map(t => t.id);
  }

  #failed(error) {
    const code = String(error.code ?? 'ERROR');
    const message = String(error.message ?? error).slice(0, 200);
    if (!this.down) this.down = { since: this.store.now(), code, message };
    else Object.assign(this.down, { code, message });
    if (!this.outage) {
      this.outage = { since: this.down.since };
      this.store.log('memory/vector-down', { since: this.down.since, url: this.url, model: this.modelName, code });
    }
    // A failure lasting 10 minutes (past Docker's restart backoff) is the owner's to know, once
    // per outage: an owner who acknowledged it is not told again until the next outage.
    if (!this.outage.noticed && Date.parse(this.store.now()) - Date.parse(this.down.since) >= DOWN_NOTICE_MS) {
      this.outage.noticed = true;
      this.store.notice('vector-down', `本地向量检索自 ${this.down.since} 不可用（${this.url} · ${this.modelName} · ${code}），`
        + `排序检索只用 BM25，可能漏掉意思相近的记录；未嵌入 ${this.#backlog().left} 条`, { since: this.down.since });
    }
  }

  #succeeded() {
    this.down = undefined;
    this.answered = true;
    this.#healthy();
  }

  /** Healthy (the server answered and has not failed since) with no backlog: the outage is
   *  over - its notice goes (also one an earlier process left) and vector-up is logged. */
  #healthy() {
    if (this.down || !this.answered || !this.loaded || this.#backlog().left) return;
    const notice = this.store.notice('vector-down', undefined);
    if (this.outage || notice) {
      this.store.log('memory/vector-up', { since: this.outage?.since ?? notice?.since, until: this.store.now() });
      this.outage = undefined;
    }
  }

  #backlog() {
    const entries = this.store.entries();
    let skipped = 0;
    for (const id of this.skipped) if (entries.has(id)) skipped++;
    const embedded = this.mem.size;
    return { embedded, total: entries.size, left: Math.max(0, entries.size - embedded - skipped) };
  }

  skippedCount() {
    const entries = this.store.entries();
    let n = 0;
    for (const id of this.skipped) if (entries.has(id)) n++;
    return n;
  }

  /** ranked.vector and status.vector: ok | loading | backlog <embedded>/<total> |
   *  down since <first failure>: <url> · <model> · <code> · <message>; backlog <e>/<t>. */
  statusText() {
    const b = this.#backlog();
    if (this.down) return `down since ${this.down.since}: ${this.url} · ${this.modelName} · ${this.down.code} · ${this.down.message}; backlog ${b.embedded}/${b.total}`;
    if (this.loading) return 'loading';
    if (b.left) return `backlog ${b.embedded}/${b.total}`;
    return 'ok';
  }
}
