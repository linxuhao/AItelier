/** The search's event-loop block at design scale (design 3.8.2: during a search the loop blocks
 *  at most 20 ms). Measured on a real MemoryStore: the largest gap between 1 ms samples while
 *  one search runs, with the process grown to the host's size - because a `git grep` forked by
 *  THIS process costs ~24 ms per GiB of its RSS, which is what the resident grep helper
 *  (lib/memory/grep-helper.mjs) exists to take off it.
 *
 *  Deliberately NOT in test/*.test.mjs: it holds over a gigabyte, so the ordinary suite stays
 *  light. Run it by hand, in a disposable container (the image needs node 24 and git):
 *    docker run --rm --network none --cpus 2 --memory 2g -v "$PWD:/app" -w /app \
 *      <that image> node test/fixtures/grep-block-probe.mjs [heldMiB] [entries] [direct]
 *  `direct` forces the search to fall back to the direct spawn - the path before the helper
 *  existed - so one probe measures both, on the same store and the same RSS. What may not go up
 *  is total_ms and, above all, max_gap_ms (20 is the design's line). */
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { MemoryStore } from '../../lib/memory/store.mjs';

const heldMiB = Number(process.argv[2] ?? 1100);
const entries = Number(process.argv[3] ?? 26);
const direct = process.argv[4] === 'direct'; // the helper cannot start, so each grep is the direct spawn

const root = mkdtempSync(join(tmpdir(), 'dsh-grep-probe-'));
// Opened first, so the helper is forked while this process is still small - the order
// MemoryStore#boot is built for (a store does not wait to grow before it is searchable).
const store = await MemoryStore.open({ root, userId: 'owner@example.com',
  ...(direct ? { grepHelper: { command: '/nonexistent/definitely-not-node' } } : {}) });
await store.recordTurn('probe', Array.from({ length: entries }, (_, i) => ({ role: i % 2 ? 'steward' : 'owner',
  text: `ledger entry ${i}: the quarterly ledger was reconciled and filed` })));
await store.flush();

// What a real host carries: vectors, buffers, a corpus. Held, never touched again - the point is
// the size of this process when it forks, not what is in the memory.
const held = [];
for (let have = 0; have < heldMiB; have += 64) held.push(Buffer.alloc(64 * 1024 * 1024, 0xa5));

let last = performance.now(), sampling = false;
const gaps = [];
const timer = setInterval(() => {
  const now = performance.now();
  if (sampling) gaps.push(now - last);
  last = now;
}, 1);
await new Promise(resolve => setTimeout(resolve, 100)); // let the allocation settle first
sampling = true;
const started = performance.now();
const answer = await store.search('ledger', 10);
const total = performance.now() - started;
sampling = false;
clearInterval(timer);

const maxGap = gaps.length ? Math.max(...gaps) : 0;
console.log(JSON.stringify({
  rss_mb: Math.round(process.memoryUsage().rss / 1048576), held_mb: held.length * 64, entries,
  total_ms: Number(total.toFixed(2)), max_gap_ms: Number(maxGap.toFixed(2)),
  samples_over_20_ms: gaps.filter(gap => gap > 20).length, samples: gaps.length,
  grep_hits: answer.grep.hits.length, complete: answer.grep.complete,
  via: direct ? 'direct-spawn' : 'helper',
  grep_helper_pid: store.grepHelper.pid ?? null,
  grep_helper_reason: store.grepHelper.reason ?? null,
}, null, 2));
await store.close();
