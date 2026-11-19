/** Design-steward 3.8.4 item 12, writes (real model; not CI): write p95 (the store's own
 *  measure, on disk to committed, over 100 notes) with vectors off, on while the worker
 *  backfills 1000 entries, and off again (A-B-A, since the host's load drifts). No searches
 *  run meanwhile. Budget (3.3 / 3.8.2): p95 <= 200 ms with vectors on.
 *  Usage: node item12-writes.mjs <absolute scratch dir>  (needs aitelier-embed up) */
import { rmSync } from 'node:fs';
import { join } from 'node:path';
import { MODEL, STORE, URL_, USER, loadavg, now, r1, rng, save, scratchRoot, sleep, summary, textOf, writeTurns } from './common.mjs';

const { MemoryStore } = await import(STORE);
const { dir, root } = scratchRoot(process.argv[2], 'item12-writes-root');
const rand = rng(1234);
const report = { item: '12-writes', started: now(), loadavg_start: loadavg() };

{
  const s = await MemoryStore.open({ root, userId: USER });
  const start = Date.now() - 300 * 86400000;
  await writeTurns(s, 'w', Array.from({ length: 1000 }, (_, i) => ({ text: textOf(rand, 20 + Math.floor(rand() * 1200), i % 2 ? 'en' : 'zh'),
    at: new Date(start + i * 300 * 86400 * 1000 / 1000).toISOString() })), 25);
  await s.close();
}

async function writes(label, vector) {
  if (vector) rmSync(join(root, USER + '.vectors.jsonl'), { force: true }); // a backfill of everything
  const load = loadavg();
  const store = await MemoryStore.open({ root, userId: USER, ...(vector ? { vector: { url: URL_, model: MODEL } } : {}) });
  if (vector) await store.vector.whenLoaded();
  const onDisk = [];
  for (let i = 0; i < 100; i++) {
    const t = performance.now();
    await store.note(`${label} ${i}: ` + textOf(rand, 60 + Math.floor(rand() * 400), i % 2 ? 'en' : 'zh'));
    onDisk.push(performance.now() - t);
    await store.flush();
    await sleep(100);
  }
  const committed = [...store.writeMs];
  const out = { label, vector_status_end: store.statusLine().vector ?? null, store_write_p95_ms: r1(committed.sort((a, b) => a - b)[Math.floor(committed.length * 0.95)]),
    committed: summary(committed), on_disk: summary(onDisk), loadavg_start: load, loadavg_end: loadavg() };
  await store.close();
  console.log(JSON.stringify(out));
  return out;
}
report.runs = [await writes('off-1', false), await writes('on (backfilling)', true), await writes('off-2', false)];
const on = report.runs[1];
report.verdict = on.store_write_p95_ms <= 200 ? 'PASS' : 'FAIL';
report.finished = now();
report.loadavg_end = loadavg();
save(dir, 'item12-writes.json', report);
console.log(JSON.stringify({ verdict: report.verdict }));
