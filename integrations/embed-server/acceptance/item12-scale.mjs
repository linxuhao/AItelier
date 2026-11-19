/** Design-steward 3.8.4 item 12 at 100,000 entries (real query embeddings; synthetic
 *  vectors; not CI):
 *   - the DSH process's RSS with the 100k vectors loaded minus without (child processes,
 *     after gc; budget <= 120 MB);
 *   - monitorEventLoopDelay max (budget <= 20 ms) during the vector-file load, during each
 *     search, during one forget (whose vector-file rewrite streams 100k rows) and during the
 *     file filter alone;
 *   - search p50/p95 with the query embedding (budget p95 <= 1.5 s).
 *  Usage: node item12-scale.mjs <absolute scratch dir>  (needs aitelier-embed up) */
import { execFile as execFileCb } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { chmodSync, copyFileSync, createWriteStream, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { monitorEventLoopDelay } from 'node:perf_hooks';
import { promisify } from 'node:util';
import { MODEL, STORE, URL_, USER, VECTOR, loadavg, now, r1, rng, save, scratchRoot, summary, textOf, waitVectorOk } from './common.mjs';

const execFile = promisify(execFileCb);
const { MemoryStore } = await import(STORE);
const { headerLine, filterVectorFile } = await import(VECTOR);
const { dir, root } = scratchRoot(process.argv[2], 'item12-scale-root');
const N = Number(process.env.SCALE_N ?? 100000);
const rand = rng(100);
const report = { item: '12-scale', n: N, started: now(), loadavg_start: loadavg() };

// ---------------------------------------------------------------- build: log files and the vector file
{
  const t = performance.now();
  const s = await MemoryStore.open({ root, userId: USER });
  await s.close();
  const months = new Map();
  const start = Date.now() - 300 * 86400000; // ten months: all inside the synchronous index window
  const ids = [];
  for (let i = 0; i < N; i++) {
    const at = new Date(start + Math.round((i + rand()) * (300 * 86400000 / N))).toISOString();
    const turn = Math.floor(i / 2), id = `t-s${turn}-${i % 2}`;
    const len = rand() < 0.7 ? 20 + Math.floor(rand() * 200) : 200 + Math.floor(rand() * 800);
    const e = { id, at, role: i % 2 ? 'steward' : 'owner', source: 'conversation', text: textOf(rand, len, rand() < 0.5 ? 'zh' : 'en'), batch: 's' + turn };
    const m = at.slice(0, 7);
    if (!months.has(m)) months.set(m, []);
    months.get(m).push(JSON.stringify(e));
    ids.push(id);
  }
  for (const [m, rows] of months) writeFileSync(join(root, USER, 'log', m + '.jsonl'), rows.join('\n') + '\n', { mode: 0o600 });
  const vfile = join(root, USER + '.vectors.jsonl');
  const out = createWriteStream(vfile, { mode: 0o600 });
  out.write(headerLine(MODEL) + '\n');
  let chunk = '';
  for (const id of ids) {
    chunk += JSON.stringify({ id, v: randomBytes(1024).toString('base64') }) + '\n';
    if (chunk.length > 1 << 22) { if (!out.write(chunk)) await new Promise(r => out.once('drain', r)); chunk = ''; }
  }
  out.end(chunk);
  await new Promise(r => out.on('finish', r));
  chmodSync(vfile, 0o600);
  // One open commits the log (#commitIfDirty at boot).
  const c = await MemoryStore.open({ root, userId: USER });
  await c.close();
  report.build = { ms: Math.round(performance.now() - t), months: months.size, vector_file_bytes: statSync(vfile).size, ids_sample: ids.slice(0, 2) };
  report.ids_last = ids.at(-1);
}

// ---------------------------------------------------------------- RSS: vectors on minus off, in child processes
{
  const child = new URL('./item12-rss-child.mjs', import.meta.url).pathname;
  const readings = [];
  for (const mode of ['off', 'on', 'off', 'on']) {
    const { stdout } = await execFile(process.execPath, ['--expose-gc', child, root, mode], { maxBuffer: 1 << 20 });
    readings.push({ ...JSON.parse(stdout.trim().split('\n').at(-1)), loadavg: loadavg() });
  }
  const mean = (mode, k) => readings.filter(r => r.mode === mode).reduce((s, r) => s + r[k], 0) / 2;
  const mb = x => r1(x / 2 ** 20);
  report.rss = { readings, delta_rss_mb: mb(mean('on', 'rss') - mean('off', 'rss')), delta_array_buffers_mb: mb(mean('on', 'arrayBuffers') - mean('off', 'arrayBuffers')),
    delta_heap_used_mb: mb(mean('on', 'heapUsed') - mean('off', 'heapUsed')) };
  report.rss.verdict = report.rss.delta_rss_mb <= 120 ? 'PASS' : 'FAIL';
}

// ---------------------------------------------------------------- in this process: loads, searches, forget
const h = monitorEventLoopDelay({ resolution: 1 });
const ms = ns => r1(ns / 1e6);
async function watched(fn) {
  h.reset(); h.enable();
  const t = performance.now();
  const value = await fn();
  const took = performance.now() - t;
  h.disable();
  return { value, ms: r1(took), loop_max_ms: ms(h.max), loop_p99_ms: ms(h.percentile(99)) };
}

let store;
const openT = performance.now();
const first = await watched(async () => {
  store = await MemoryStore.open({ root, userId: USER, vector: { url: URL_, model: MODEL } });
  report.open_ms = r1(performance.now() - openT); // includes the synchronous #load (BM25 build), not the vector load
  await store.vector.whenLoaded();
});
report.vector_load_first = { ms: first.ms, loop_max_ms_incl_sync_open: first.loop_max_ms, note: 'the monitor ran across MemoryStore.open, so its max includes the synchronous #load (3 s budget, not this one)' };
await waitVectorOk(store, { timeoutMs: 120000, every: 200 });
report.vector_status = store.statusLine().vector;
report.vector_load_again = await watched(() => store.vector.startLoad()).then(({ value, ...r }) => ({ ...r, loadavg: loadavg(), vectors: store.vector.mem.size }));
report.vector_load_again.verdict = report.vector_load_again.loop_max_ms <= 20 ? 'PASS' : 'FAIL';
await waitVectorOk(store, { timeoutMs: 120000, every: 200 });

const QUERIES = ['域名续费', 'expense tracker on mobile', '早上会议的偏好', '猫的疫苗什么时候打', 'train delayed again', '阳台的番茄开花', 'storm and power cut', '做饭放多少盐',
  'library fine', '徒步十五公里', 'sourdough starter', '图书馆借的小说', 'dog chewed the leash', '降温了', 'flea market cast iron', '诗集的译本'];
const searches = [];
const load = loadavg();
for (let i = 0; i < 40; i++) {
  const r = await watched(() => store.search(QUERIES[i % QUERIES.length], 10));
  searches.push({ ms: r.ms, loop_max_ms: r.loop_max_ms, source: r.value.ranked.source, vector: r.value.ranked.vector, grep_complete: r.value.grep.complete });
}
report.search = { total: summary(searches.map(s => s.ms)), loop_max: summary(searches.map(s => s.loop_max_ms)), sources: [...new Set(searches.map(s => s.source))],
  vector: [...new Set(searches.map(s => s.vector))], loadavg_start: load, loadavg_end: loadavg(), raw: searches };
report.search.verdict = { p95: report.search.total.p95 <= 1500 ? 'PASS' : 'FAIL', loop: report.search.loop_max.max <= 20 ? 'PASS' : 'FAIL' };
// The vector channel alone (query embedding + 100k dot products), for the split.
const rankOnly = [];
for (let i = 0; i < 20; i++) rankOnly.push((await watched(() => store.vector.rank(QUERIES[i % QUERIES.length], 50))));
report.vector_rank_only = { total: summary(rankOnly.map(r => r.ms)), loop_max: summary(rankOnly.map(r => r.loop_max_ms)) };
const bmOnly = [];
for (let i = 0; i < 20; i++) bmOnly.push(await watched(async () => store.index.search(QUERIES[i % QUERIES.length], 50)));
report.bm25_only_search = { total: summary(bmOnly.map(r => r.ms)), loop_max: summary(bmOnly.map(r => r.loop_max_ms)) };

// The vector-file filter alone, on a copy (one id dropped: a full streamed rewrite).
{
  const copy = join(dir, 'item12-scale-filter-copy.jsonl');
  copyFileSync(join(root, USER + '.vectors.jsonl'), copy);
  const r = await watched(() => filterVectorFile(copy, new Set([report.build.ids_sample[0]])));
  report.filter_only = { ms: r.ms, loop_max_ms: r.loop_max_ms, loop_p99_ms: r.loop_p99_ms, result: { rows: r.value.rows, removed: r.value.removed }, loadavg: loadavg() };
  report.filter_only.verdict = r.loop_max_ms <= 20 ? 'PASS' : 'FAIL';
}
// One forget through the store: log strip + vector-file rewrite + commit.
{
  const r = await watched(() => store.forget([report.ids_last], { reason: 'acceptance measurement', by: 'owner' }));
  report.forget = { ms: r.ms, loop_max_ms: r.loop_max_ms, loop_p99_ms: r.loop_p99_ms, forgotten: r.value.forgotten, loadavg: loadavg() };
  report.forget.verdict = r.loop_max_ms <= 20 ? 'PASS' : 'FAIL';
}
await store.close();
report.finished = now();
report.loadavg_end = loadavg();
save(dir, 'item12-scale.json', report);
const { raw, ...searchShort } = report.search;
console.log(JSON.stringify({ build: report.build, rss: { ...report.rss, readings: undefined }, open_ms: report.open_ms, first: report.vector_load_first, again: report.vector_load_again,
  search: searchShort, rank: report.vector_rank_only, bm25: report.bm25_only_search, filter: report.filter_only, forget: report.forget }, null, 1));
