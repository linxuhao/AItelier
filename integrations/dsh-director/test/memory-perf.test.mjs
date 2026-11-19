/** Plugin step 21 perf follow-up (design-steward 3.8.2: the loop blocks ≤ 20 ms during a
 *  search, the vectors ≤ 120 MB): what changed for speed or memory gives the answers the old
 *  code gave. Each new piece is checked against the old one, kept here as the reference:
 *  grep's streamed newest-k against parse-all-then-sort, the reused-buffer line reader
 *  against readline, the scratch base64 decode against Buffer.from, the vector scan against
 *  a sort of every row; and the turns give each waiter its own slice. */
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createReadStream, mkdtempSync, writeFileSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { MemoryStore, grepMatches, recordReader } from '../lib/memory/store.mjs';
import { fileLines, parseRow, quantize } from '../lib/memory/vector.mjs';
import { due, turn } from '../lib/memory/slices.mjs';
import { gitEnv } from '../lib/memory/git.mjs';
import { fakeEmbedServer, hashVector } from './fixtures/embed-fake.mjs';

const USER = 'owner@example.com';
const DIMS = 1024;
const newRoot = () => mkdtempSync(join(tmpdir(), 'dsh-memperf-'));

/** A seeded PRNG (mulberry32), so a failure can be replayed. */
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const pick = (rand, list) => list[Math.floor(rand() * list.length)];

// ---------------------------------------------------------------- the old code, as reference

/** #grep before: the whole output decoded, split, parsed; all items sorted newest first. */
function oldGrepItems(stdout, query, k) {
  const items = [];
  const lower = query.toLowerCase();
  for (const record of stdout.toString('utf8').split('\n')) {
    const [file, line, ...rest] = record.split('\0');
    if (!file || rest.length === 0) continue;
    const content = rest.join('\0');
    if (file === 'profile.md') {
      items.push({ at: '', profileLine: Number(line), entry: { text: content }, hit: { id: 'profile', line: Number(line) } });
      continue;
    }
    let e;
    try { e = JSON.parse(content); } catch { continue; }
    if (!e?.text || !e.text.toLowerCase().includes(lower)) continue;
    items.push({ at: e.at, entry: e, hit: { id: e.id, at: e.at, role: e.role } });
  }
  items.sort((a, b) => (b.at > a.at ? 1 : b.at < a.at ? -1 : 0));
  return items.slice(0, k);
}

/** parseRow before: always a Buffer of its own. */
function oldParseRow(line, dims) {
  let row;
  try { row = JSON.parse(line); } catch { return undefined; }
  if (!row || typeof row.id !== 'string' || !row.id || typeof row.v !== 'string') return undefined;
  const v = Buffer.from(row.v, 'base64');
  return v.length === dims ? { id: row.id, v } : undefined;
}

/** Feed `bytes` to a recordReader in random chunks; the records it took, in order. */
async function readInChunks(bytes, rand, take = () => {}) {
  const got = [];
  const reader = recordReader(record => { got.push(record); take(record); });
  for (let i = 0; i < bytes.length;) {
    const n = 1 + Math.floor(rand() * 40);
    reader.push(bytes.subarray(i, i + n));
    i += n;
    if (rand() < 0.3) await new Promise(resolve => setImmediate(resolve));
  }
  await reader.end();
  return got;
}

// ---------------------------------------------------------------- grep

/** A random `git grep -z -n` output: log lines (some not matching in their text, some torn,
 *  some with times shared), profile lines, and odd bytes. */
function grepOutput(rand, n) {
  const ats = ['2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z', '2026-11-02T00:00:00.5Z', '2025-01-01T00:00:00Z', '2026-11-03T00:00:00Z'];
  const words = ['Ledger', 'ledger', 'cat', '记账', 'ledger\u0000x', 'é', 'LEDGER'];
  const out = [];
  for (let i = 0; i < n; i++) {
    const r = rand();
    if (r < 0.15) out.push(Buffer.from(`profile.md\u0000${i + 1}\u0000- ${pick(rand, words)} note ${i}\n`));
    else if (r < 0.2) out.push(Buffer.from(`log/2026-11.jsonl\u0000${i}\u0000{"id":"t-${i}-0","text":"torn ${pick(rand, words)}\n`));
    else if (r < 0.23) out.push(Buffer.from([0xe4, 0xb8, 0x0a])); // a cut multibyte character before a newline
    else {
      const e = { id: `t-${i}-0`, at: pick(rand, ats), role: pick(rand, ['owner', 'steward']), text: `${pick(rand, words)} ${pick(rand, words)} ${i}` };
      out.push(Buffer.from(`log/${e.at.slice(0, 7)}.jsonl\u0000${i + 1}\u0000${JSON.stringify(e)}\n`));
    }
  }
  if (rand() < 0.5) out.push(Buffer.from(`profile.md\u0000999\u0000- ledger without a newline (a killed grep)`));
  return Buffer.concat(out);
}

test('grep: streamed newest-k = parse all, sort all, first k (random outputs, random chunking, k 1-20)', async () => {
  const rand = rng(21);
  for (let round = 0; round < 300; round++) {
    const bytes = grepOutput(rand, Math.floor(rand() * 120));
    const query = pick(rand, ['ledger', 'LEDGER', '记账', 'cat', 'é', 'note']);
    const k = 1 + Math.floor(rand() * 20);
    const matches = grepMatches(query, k);
    const records = await readInChunks(bytes, rand, matches.take);
    assert.deepEqual(records, bytes.toString('utf8').split('\n').filter((r, i, all) => i < all.length - 1 || r !== ''), `records, round ${round}`);
    assert.deepEqual(matches.items(), oldGrepItems(bytes, query, k), `round ${round}, query ${query}, k ${k}`);
  }
});

test('grep: a line whose `at` is not a string sorts all of them the old way', () => {
  const rand = rng(7);
  for (let round = 0; round < 100; round++) {
    const lines = [];
    for (let i = 0; i < 30; i++) {
      const at = pick(rand, ['2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z', undefined, 5, null, '']);
      lines.push(`log/x.jsonl\u0000${i}\u0000${JSON.stringify({ id: `t-${i}-0`, at, text: 'ledger ' + i })}\n`);
    }
    const bytes = Buffer.from(lines.join(''));
    const matches = grepMatches('ledger', 7);
    for (const record of bytes.toString('utf8').split('\n')) matches.take(record);
    assert.deepEqual(matches.items(), oldGrepItems(bytes, 'ledger', 7), `round ${round}`);
  }
});

test('grep: what `take` throws comes out of end(), never as an unhandled rejection', async () => {
  let unhandled = 0;
  const count = () => { unhandled++; };
  process.on('unhandledRejection', count);
  try {
    const reader = recordReader(record => { if (record === 'b') throw new TypeError('boom'); });
    reader.push(Buffer.from('a\nb\nc\n'));
    await new Promise(resolve => setTimeout(resolve, 20));
    await assert.rejects(reader.end(), /boom/);
    assert.equal(unhandled, 0);
  } finally { process.off('unhandledRejection', count); }
});

test('grep through the store: thousands of matching lines and the profile - the hits the old code gave', async () => {
  const root = newRoot();
  const store = await MemoryStore.open({ root, userId: USER });
  const rand = rng(3);
  for (let t = 0; t < 60; t++) {
    const messages = [];
    for (let j = 0; j < 50; j++) {
      const at = `2026-11-${String(1 + Math.floor(rand() * 9)).padStart(2, '0')}T0${Math.floor(rand() * 3)}:00:00.000Z`;
      messages.push({ role: j % 2 ? 'steward' : 'owner', at, text: `${rand() < 0.8 ? 'Common' : 'rare'} words ${t}-${j}` });
    }
    await store.recordTurn('b' + t, messages);
  }
  await store.flush();
  const p = await store.profile();
  await store.updateProfile('- common profile line\n- another common one', p.version, 'test', 'owner');
  const dir = join(root, USER);
  for (const [query, k] of [['common', 10], ['COMMON', 20], ['words', 1], ['rare', 7]]) {
    const r = await store.search(query, k);
    const needle = JSON.stringify(query).slice(1, -1);
    const out = spawnSync('git', ['-c', 'safe.directory=' + dir, '-c', 'core.quotePath=false', 'grep', '-F', '-i', '-I', '-n', '-z', '--no-color', '--untracked',
      '-e', needle, '--', 'log', 'profile.md'], { cwd: dir, env: gitEnv(dir), maxBuffer: 1 << 28 }).stdout;
    const old = oldGrepItems(out, query, k);
    assert.ok(out.toString().split('\n').length > (query === 'rare' ? 100 : 2000), 'many matching lines');
    assert.equal(r.grep.complete, true);
    assert.deepEqual(r.grep.hits.map(h => h.id === 'profile' ? 'profile:' + h.line : h.id), old.map(i => i.hit.id === 'profile' ? 'profile:' + i.hit.line : i.hit.id), query);
  }
  await store.close();
});

// ---------------------------------------------------------------- the vector file's load

test('load: the reused-buffer line reader gives the lines readline gives (\\n, \\r\\n, \\r, cut characters, any chunk size)', async () => {
  const rand = rng(11);
  const pieces = ['{"id":"t-1-0","v":"AAEC"}', 'abc', '', '记账小工具', 'é', '\r', '\n', '\r\n', '\n\n', '\r\r', ' ', 'x'];
  const root = newRoot();
  for (let round = 0; round < 300; round++) {
    const parts = [];
    for (let i = Math.floor(rand() * 30); i > 0; i--) {
      const p = rand();
      parts.push(p < 0.08 ? Buffer.from([0xe8, 0xae]) : p < 0.1 ? Buffer.from([0xff]) : Buffer.from(pick(rand, pieces)));
    }
    const file = join(root, `f${round}`);
    writeFileSync(file, Buffer.concat(parts));
    const expected = [];
    for await (const line of createInterface({ input: createReadStream(file, { encoding: 'utf8' }), crlfDelay: Infinity })) expected.push(line);
    const got = [];
    for await (const line of fileLines(file, 1 + Math.floor(rand() * 9))) got.push(line);
    assert.deepEqual(got, expected, `round ${round}: ${JSON.stringify(Buffer.concat(parts).toString('latin1'))}`);
    const whole = [];
    for await (const line of fileLines(file)) whole.push(line);
    assert.deepEqual(whole, expected, `round ${round}, one read`);
  }
});

test('load: the scratch decode answers what Buffer.from answered (valid, short, long, padded, junk base64)', () => {
  const rand = rng(5);
  const scratch = Buffer.alloc(2 * 16);
  const b64 = n => Buffer.from(Array.from({ length: n }, () => Math.floor(rand() * 256))).toString('base64');
  for (let round = 0; round < 3000; round++) {
    const dims = 16;
    let v = b64(pick(rand, [15, 16, 16, 16, 17, 12, 32, 33, 40, 0]));
    const r = rand();
    if (r < 0.1) v = v.replace(/=+$/, '');
    else if (r < 0.2) v = v.slice(0, 2) + pick(rand, [' ', '\n', '*', '-', '_', '=', '.']) + v.slice(2);
    else if (r < 0.25) v = v + '====';
    else if (r < 0.3) v = '=' + v;
    else if (r < 0.35) v = v.replace(/[A-M]/g, '-');
    const line = rand() < 0.05 ? '{"id":"x","v":3}' : JSON.stringify({ id: rand() < 0.03 ? '' : 't-' + round, v });
    const want = oldParseRow(line, dims), got = parseRow(line, dims, scratch);
    assert.deepEqual(got && { id: got.id, v: [...got.v] }, want && { id: want.id, v: [...want.v] }, line);
    assert.deepEqual(parseRow(line, dims) && [...parseRow(line, dims).v], want && [...want.v], 'without scratch: the old path');
  }
});

// ---------------------------------------------------------------- the vector scan

const servers = [];
after(async () => { for (const s of servers) await s.close(); });

test('rank: the scan (an object only for a row that may rank) = every row sorted by score, newer, id', async () => {
  const fake = await fakeEmbedServer();
  servers.push(fake);
  const root = newRoot();
  const store = await MemoryStore.open({ root, userId: USER, vector: { url: fake.url, model: 'bge-m3-q8_0', timeoutMs: 2000 },
    vectorTuning: { backoffMinMs: 20, backoffMaxMs: 100, workerTimeoutMs: 3000 } });
  const rand = rng(9);
  const seeds = ['a', 'b', 'c', 'd'];
  const ats = ['2026-11-01T00:00:00.000Z', '2026-11-02T00:00:00.000Z', '2026-11-03T00:00:00.000Z'];
  // Few distinct vectors and times: many equal scores, many equal times, then the id decides.
  for (let t = 0; t < 8; t++) {
    const messages = [];
    for (let j = 0; j < 20; j++) {
      const text = `entry ${t} ${j}`;
      fake.knobs.vectors.set(text, hashVector(pick(rand, seeds), DIMS)); // bge-m3: no passage prefix
      messages.push({ role: 'owner', at: pick(rand, ats), text });
    }
    await store.recordTurn('r' + t, messages);
  }
  await store.flush();
  await store.vector.whenLoaded();
  const end = Date.now() + 15000;
  while (store.statusLine().vector !== 'ok') { if (Date.now() > end) throw new Error(store.statusLine().vector); await new Promise(r => setTimeout(r, 10)); }
  await store.forget(['t-r3-4', 't-r5-0'], { reason: 'test', by: 'owner' });
  const mem = store.vector.mem, entries = store.entries;
  for (const query of ['first query', 'second query', 'third query']) {
    const qv = quantize(hashVector(query, DIMS)); // what the fake answers for it (no query prefix)
    const all = [];
    mem.ids.forEach((id, row) => {
      if (id === undefined || !entries.has(id)) return;
      const data = mem.blocks[Math.floor(row / 4096)], base = (row % 4096) * DIMS;
      let score = 0;
      for (let i = 0; i < DIMS; i++) score += data[base + i] * qv[i];
      all.push({ id, score, at: entries.get(id).at });
    });
    all.sort((x, y) => y.score - x.score || (y.at > x.at ? 1 : y.at < x.at ? -1 : 0) || (x.id < y.id ? -1 : x.id > y.id ? 1 : 0));
    for (const count of [1, 7, 50, 200]) {
      const ids = await store.vector.rank(query, count);
      assert.deepEqual(ids, all.slice(0, count).map(x => x.id), `query ${query}, count ${count}`);
    }
  }
  await store.close();
});

// ---------------------------------------------------------------- turns

test('turns: each waiter resumes in a turn of its own, with its own slice, in order', async () => {
  const log = [];
  const loop = async name => {
    for (let i = 0; i < 3; i++) {
      await turn();
      log.push(`${name}${i}:${due() ? 'late' : 'fresh'}`);
      while (!due()) { /* use the whole slice */ }
    }
  };
  await Promise.all([loop('a'), loop('b')]);
  assert.deepEqual(log, ['a0:fresh', 'b0:fresh', 'a1:fresh', 'b1:fresh', 'a2:fresh', 'b2:fresh']);
});
