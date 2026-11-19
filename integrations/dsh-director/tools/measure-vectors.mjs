/** Design-steward 3.8.4 item 12, the plugin-side part: event-loop blocking (max delay, via
 *  monitorEventLoopDelay) while a 100,000-row bge-m3 vector file loads, while one search
 *  embeds its query and scans every vector, and while one forget rewrites the file; plus the
 *  RSS the loaded vectors add. A measurement, not a test (too slow and too large for CI):
 *    node tools/measure-vectors.mjs [rows]        (default 100000; prints JSON)
 *  The embedding server is the deterministic fake (test/fixtures/embed-fake.mjs). Everything
 *  is written under a fresh directory in the OS temp dir, removed afterwards. */
import { monitorEventLoopDelay } from 'node:perf_hooks';
import { mkdtempSync, rmSync, writeFileSync, createWriteStream, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { once } from 'node:events';
import { MemoryStore } from '../lib/memory/store.mjs';
import { headerLine } from '../lib/memory/vector.mjs';
import { fakeEmbedServer } from '../test/fixtures/embed-fake.mjs';

const ROWS = Number(process.argv[2] ?? 100000);
const USER = 'measure';
const root = mkdtempSync(join(tmpdir(), 'dsh-measure-vectors-'));
const fake = await fakeEmbedServer();
const result = { rows: ROWS };
const ms = ns => Math.round(ns / 1e5) / 10;

async function measured(name, fn) {
  const h = monitorEventLoopDelay({ resolution: 1 });
  h.enable();
  const started = performance.now();
  const value = await fn();
  h.disable();
  result[name] = { wall_ms: Math.round(performance.now() - started), loop_delay_max_ms: ms(h.max), loop_delay_p99_ms: ms(h.percentile(99)) };
  return value;
}

try {
  // The entries: one month of log, written directly (a measurement, not the write path).
  let store = await MemoryStore.open({ root, userId: USER });
  await store.close();
  const at = i => new Date(Date.parse('2026-11-01T00:00:00Z') + i * 1000).toISOString();
  const log = createWriteStream(join(root, USER, 'log', '2026-11.jsonl'));
  const vectors = createWriteStream(join(root, USER + '.vectors.jsonl'), { mode: 0o600 });
  vectors.write(headerLine('bge-m3-q8_0') + '\n');
  const v = Buffer.alloc(1024);
  for (let i = 0; i < ROWS; i++) {
    const id = `t-m${i}-0`;
    if (!log.write(JSON.stringify({ id, at: at(i), role: 'owner', source: 'conversation', text: `measured entry ${i} about topic ${i % 97}`, batch: `m${i}` }) + '\n')) await once(log, 'drain');
    for (let j = 0; j < 1024; j++) v[j] = (i * 31 + j * 17) & 0xff;
    if (!vectors.write(JSON.stringify({ id, v: v.toString('base64') }) + '\n')) await once(vectors, 'drain');
  }
  log.end(); vectors.end();
  await Promise.all([once(log, 'finish'), once(vectors, 'finish')]);
  result.vector_file_mb = Math.round(statSync(join(root, USER + '.vectors.jsonl')).size / 1e5) / 10;

  const rssBefore = process.memoryUsage().rss;
  let releaseLoad;
  const gate = new Promise(resolve => { releaseLoad = resolve; });
  const openStarted = performance.now();
  store = await MemoryStore.open({ root, userId: USER, vector: { url: fake.url, model: 'bge-m3-q8_0' },
    faults: { vectorLoadPause: () => gate } });
  result.open_ms = Math.round(performance.now() - openStarted);
  global.gc?.();
  const mem0 = process.memoryUsage();
  const rssEntries = mem0.rss;
  await measured('load', () => { releaseLoad(); return store.vector.whenLoaded(); });
  global.gc?.();
  const mem1 = process.memoryUsage();
  const mb = n => Math.round(n / 1e5) / 10;
  result.rss_vectors_mb = mb(mem1.rss - rssEntries);
  // What the loaded vectors keep (after a GC), apart from what RSS keeps as a high-water mark.
  result.retained_vectors_mb = mb(mem1.heapUsed + mem1.arrayBuffers - mem0.heapUsed - mem0.arrayBuffers);
  result.rss_entries_mb = Math.round((rssEntries - rssBefore) / 1e5) / 10;
  result.status_after_load = store.statusLine().vector;
  const search = await measured('search', () => store.search('topic 42 about'));
  result.search_ranked_source = search.ranked.source;
  // The same search's parts: the vector scan alone, and BM25 alone (synchronous, no yields).
  await measured('vector_rank_only', () => store.vector.rank('topic 42 about', 50));
  await measured('vector_rank_only_again', () => store.vector.rank('another query', 50));
  await measured('search_again', () => store.search('yet another query'));
  await measured('bm25_only', async () => store.index.search('topic 42 about', 50));
  await measured('forget_rewrite', () => store.forget(['t-m5-0'], { reason: 'measure', by: 'owner' }));
  await store.close();
} finally {
  await fake.close();
  rmSync(root, { recursive: true, force: true });
}
console.log(JSON.stringify(result, null, 2));
