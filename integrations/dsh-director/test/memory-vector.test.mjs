/** Plugin step 21, the local vector channel (design-steward 3.8.2; acceptance 3.8.4 items 5,
 *  6, 8, 9, 10 and 13), against a deterministic fake embedding server: RRF fusion, the
 *  side-car vector file and its reconciliation, the background worker, degradation with its
 *  owner notice, and the purge's vector steps with their two-sided check. */
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { appendFileSync, chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { MemoryStore } from '../lib/memory/store.mjs';
import { openMemory, profileSection } from '../lib/memory/tools.mjs';
import { headerLine, parseRow, quantize, scanVectorFile } from '../lib/memory/vector.mjs';
import { memoryVectorProblem } from '../lib/host.mjs';
import { gitEnv } from '../lib/memory/git.mjs';
import { axis, fakeEmbedServer, hashVector, refusedPort } from './fixtures/embed-fake.mjs';

const USER = 'owner@example.com';
const MODEL = 'bge-m3-q8_0';
const DIMS = 1024;
const newRoot = () => mkdtempSync(join(tmpdir(), 'dsh-memvec-'));
const repo = root => join(root, USER);
const vfile = root => join(root, USER + '.vectors.jsonl');
const gitIn = (dir, ...args) => spawnSync('git', ['-c', 'safe.directory=' + dir, ...args], { cwd: dir, env: gitEnv(dir), maxBuffer: 1 << 28 });
/** Make filtering the vector file fail: chmod 0400 (refused by the filter's W_OK check) - or,
 *  as root (the shipped image's test run), which writes through any mode, a directory among
 *  the filter's temporary-file names, which the filter cannot remove. Returns the undo. */
const BLOCKER = '.filter-blocked';
function blockFilter(root) {
  if (process.getuid?.() === 0) { mkdirSync(vfile(root) + BLOCKER); return () => rmSync(vfile(root) + BLOCKER, { recursive: true }); }
  chmodSync(vfile(root), 0o400);
  return () => chmodSync(vfile(root), 0o600);
}
const TUNING = { backoffMinMs: 20, backoffMaxMs: 100, workerTimeoutMs: 3000 };
const servers = [];
after(async () => { for (const s of servers) await s.close(); });
async function server(o) { const s = await fakeEmbedServer(o); servers.push(s); return s; }

/** A clock the test moves: every call returns the same instant until `advance`. */
function clock(start = '2026-11-19T10:00:00.000Z') {
  let ms = Date.parse(start);
  const now = () => new Date(ms).toISOString();
  now.advance = d => { ms += d; };
  now.tick = () => { ms += 1000; return new Date(ms).toISOString(); };
  return now;
}

const open = (root, url, extra = {}) => MemoryStore.open({ root, userId: USER,
  ...(url ? { vector: { url, model: extra.model ?? MODEL, timeoutMs: extra.timeoutMs ?? 2000 }, vectorTuning: TUNING } : {}), ...extra });

async function until(predicate, what, ms = 15000) {
  const end = Date.now() + ms;
  while (!(await predicate())) {
    if (Date.now() > end) throw new Error('timed out waiting for ' + what);
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}
/** Loaded, reconciled, everything embedded. */
async function ready(store) {
  await store.vector.whenLoaded();
  await until(() => store.statusLine().vector === 'ok', 'vector ok (' + store.statusLine().vector + ')');
  await store.flush();
}
const fileIds = root => existsSync(vfile(root)) ? readFileSync(vfile(root), 'utf8').split('\n').slice(1).filter(Boolean)
  .map(l => parseRow(l, DIMS)?.id).filter(Boolean) : [];
const row = (id, values) => JSON.stringify({ id, v: Buffer.from(quantize(values).buffer).toString('base64') });
const journalOf = root => readFileSync(join(root, USER + '.purge.jsonl'), 'utf8').split('\n').filter(Boolean).map(JSON.parse);
function treeHash(dir) {
  const h = createHash('sha256');
  const walk = d => { for (const n of readdirSync(d).sort()) { const p = join(d, n); const st = statSync(p);
    h.update(relative(dir, p) + '\0'); if (st.isDirectory()) walk(p); else h.update(readFileSync(p)); } };
  walk(dir);
  return h.digest('hex');
}

// ---------------------------------------------------------------- 13. config

test('13: the config - loopback url only, a known model, exclusive with remote; off is BM25 alone with no vector field', async () => {
  const ok = url => memoryVectorProblem({ vector: { url, model: MODEL } });
  for (const url of ['http://127.0.0.1:18751', 'http://localhost:18751', 'http://[::1]:18751', 'http://LOCALHOST:1/'])
    assert.equal(ok(url), undefined, url);
  for (const url of ['http://10.0.0.5:18751', 'https://127.0.0.1:18751', 'http://example.com', 'http://127.0.0.1.nip.io:1',
    'http://user:pw@127.0.0.1:1', 'ftp://127.0.0.1', 'not a url', undefined, 42])
    assert.match(ok(url) ?? '', /memory\.vector\.url must be/, String(url));
  assert.match(memoryVectorProblem({ vector: { url: 'http://127.0.0.1:1', model: 'bge-m3' } }), /memory\.vector\.model must be one of bge-m3-q8_0, multilingual-e5-small-q8_0/);
  assert.match(memoryVectorProblem({ vector: { url: 'http://127.0.0.1:1', model: MODEL, timeoutMs: -1 } }), /timeoutMs/);
  assert.equal(memoryVectorProblem({ vector: { url: 'http://127.0.0.1:1', model: MODEL }, remote: { url: 'http://x', name: 'r' } }),
    'memory.remote and memory.vector are exclusive');
  // The memory module refuses; the host itself is not refused (like a bad time zone).
  const both = await openMemory({ userId: USER, root: newRoot(), remote: { url: 'http://127.0.0.1:1', name: 'r' }, vector: { url: 'http://127.0.0.1:1', model: MODEL } });
  assert.equal(both.error, 'memory.remote and memory.vector are exclusive');
  const unknown = await openMemory({ userId: USER, root: newRoot(), vector: { url: 'http://127.0.0.1:1', model: 'e5' } });
  assert.match(unknown.error, /memory\.vector\.model/);
  const far = await openMemory({ userId: USER, root: newRoot(), vector: { url: 'http://192.168.1.2:1', model: MODEL } });
  assert.match(far.error, /memory\.vector\.url/);
  await assert.rejects(MemoryStore.open({ root: newRoot(), userId: USER, vector: { url: 'http://127.0.0.1:1', model: 'nope' } }), /memory\.vector\.model/);
  // Off: today's BM25, no vector field anywhere, no vector file.
  const root = newRoot();
  const store = await open(root);
  await store.recordTurn('a', [{ role: 'owner', text: 'apple pie recipe' }, { role: 'steward', text: 'apple tart' }]);
  const r = await store.search('apple');
  assert.equal(r.ranked.source, 'bm25 · 本地索引');
  assert.ok(!('vector' in r.ranked) && !('vector' in r.status), JSON.stringify(r.ranked));
  assert.deepEqual(r.ranked.hits.map(h => h.id), store.index.search('apple', 10).map(h => h.id));
  assert.ok(!existsSync(vfile(root)));
  await store.close();
});

test('13: the vector file is 0600 beside the repository, never in git; its header names model, dims and input size', async () => {
  const fake = await server();
  const root = newRoot();
  const store = await open(root, fake.url);
  await store.recordTurn('a', [{ role: 'owner', text: 'hello vectors' }]);
  await ready(store);
  assert.equal(statSync(vfile(root)).mode & 0o777, 0o600);
  assert.equal(readFileSync(vfile(root), 'utf8').split('\n')[0], '{"model":"bge-m3-q8_0","dims":1024,"quant":"int8","input_chars":1000}');
  assert.deepEqual(fileIds(root), ['t-a-0']);
  assert.ok(!gitIn(repo(root), 'ls-files').stdout.toString().includes('vectors'));
  assert.equal(fake.requests[0].model, MODEL);
  await store.close();
});

// ---------------------------------------------------------------- 6. fusion

test('6: RRF scores equal the hand computation; vector-only hits are not date-only', async () => {
  const fake = await server();
  const now = clock();
  const root = newRoot();
  const store = await open(root, fake.url, { now: now.tick });
  fake.knobs.vectors.set('apple', axis(DIMS, 0));
  fake.knobs.vectors.set('banana split', axis(DIMS, 0));     // vector rank 1
  fake.knobs.vectors.set('apple orange', axis(DIMS, 0, 1));  // vector rank 2
  fake.knobs.vectors.set('apple apple', axis(DIMS, 1));      // vector rank 3
  await store.recordTurn('a', [{ role: 'owner', text: 'apple apple' }]);   // BM25 rank 1
  await store.recordTurn('b', [{ role: 'owner', text: 'apple orange' }]);  // BM25 rank 2
  await store.recordTurn('c', [{ role: 'owner', text: 'banana split' }]);  // no BM25 hit
  await ready(store);
  const r = await store.search('apple', 3);
  assert.equal(r.ranked.source, 'bm25+向量 · 本地索引 · bge-m3-q8_0');
  assert.equal(r.ranked.vector, 'ok');
  const round = x => Math.round(x * 1e4) / 1e4;
  assert.deepEqual(r.ranked.hits.map(h => [h.id, h.score]), [
    ['t-a-0', round(1 / 61 + 1 / 63)], ['t-b-0', round(1 / 62 + 1 / 62)], ['t-c-0', round(1 / 61)]]);
  assert.equal(r.ranked.hits[2].date_only, undefined, 'a vector hit sharing no word and no date is close in meaning, not date-only');
  await store.close();
});

test('6: each channel brings max(5·top_k, 50) candidates (top_k 1, 10, 20)', async () => {
  const fake = await server();
  const root = newRoot();
  const store = await open(root, fake.url);
  const turns = [];
  for (let i = 0; i < 120; i++) turns.push([`m${i}`, [{ role: 'owner', text: `common thing ${i}` }]]);
  for (const [id, messages] of turns) await store.recordTurn(id, messages);
  await ready(store);
  const seen = [];
  store.faults = { onFusion: f => seen.push(f) };
  for (const k of [1, 10, 20]) await store.search('common', k);
  assert.deepEqual(seen, [{ C: 50, bm25: 50, vector: 50 }, { C: 50, bm25: 50, vector: 50 }, { C: 100, bm25: 100, vector: 100 }]);
  await store.close();
});

test('6: equal fused scores put the newer entry first (not the lower id, nor the higher)', async () => {
  // Once with the newer entry holding the higher id, once the lower: only "newer first" passes both.
  for (const [older, newer] of [['x', 'y'], ['y', 'x']]) {
    const fake = await server();
    const now = clock();
    const root = newRoot();
    const store = await open(root, fake.url, { now: now.tick });
    fake.knobs.vectors.set('kiwi', axis(DIMS, 0));
    fake.knobs.vectors.set('kiwi fruit', axis(DIMS, 0));       // vector rank 1
    fake.knobs.vectors.set('kiwi kiwi', axis(DIMS, 0, 1));     // vector rank 2
    await store.recordTurn(older, [{ role: 'owner', text: 'kiwi kiwi' }]);   // older, BM25 rank 1
    await store.recordTurn(newer, [{ role: 'owner', text: 'kiwi fruit' }]);  // newer, BM25 rank 2
    await ready(store);
    const r = await store.search('kiwi', 2);
    assert.equal(r.ranked.hits[0].score, r.ranked.hits[1].score);
    assert.deepEqual(r.ranked.hits.map(h => h.id), [`t-${newer}-0`, `t-${older}-0`]);
    await store.close();
  }
});

// ---------------------------------------------------------------- 5. forget and restore

test('5: a forgotten entry leaves grep, ranked and the vector file; restored, it is embedded again and found by both', async () => {
  const fake = await server();
  const root = newRoot();
  const store = await open(root, fake.url);
  await store.recordTurn('a', [{ role: 'owner', text: 'the blue umbrella' }, { role: 'steward', text: 'noted the blue umbrella' }]);
  await store.recordTurn('b', [{ role: 'owner', text: 'green umbrella stand' }]);
  await ready(store);
  assert.deepEqual(fileIds(root).sort(), ['t-a-0', 't-a-1', 't-b-0']);
  const f = await store.forget(['t-a-0'], { reason: 'asked', by: 'owner' });
  assert.deepEqual(f.forgotten, ['t-a-0']);
  assert.deepEqual(fileIds(root).sort(), ['t-a-1', 't-b-0'], 'out of the file at once');
  const r = await store.search('blue umbrella');
  for (const answer of [r.grep, r.ranked]) assert.ok(!answer.hits.some(h => h.id === 't-a-0'), JSON.stringify(answer.hits));
  assert.ok(!r.ranked.text.includes('{t-a-0}') && !r.grep.text.includes('{t-a-0}'));
  assert.equal(r.ranked.vector, 'ok');
  assert.equal((await store.restore('t-a-0')).restored, 't-a-0');
  await ready(store);
  assert.ok(fileIds(root).includes('t-a-0'), 'embedded again');
  const back = await store.search('the blue umbrella');
  assert.ok(back.grep.hits.some(h => h.id === 't-a-0'));
  assert.ok(back.ranked.hits.some(h => h.id === 't-a-0'));
  await store.close();
});

// ---------------------------------------------------------------- 8. degradation

test('8: a refused server - BM25 alone, said so; since stays put a minute later; grep whole; writes succeed', async () => {
  const port = await refusedPort();
  const url = `http://127.0.0.1:${port}`;
  const now = clock();
  const root = newRoot();
  const store = await open(root, url, { now });
  assert.equal((await store.recordTurn('a', [{ role: 'owner', text: 'pumpkin soup' }])).written, 1, 'a write never waits for vectors');
  const r = await store.search('pumpkin');
  assert.equal(r.ranked.source, 'bm25 · 本地索引 · 向量不可用');
  assert.match(r.ranked.vector, /^down since 2026-11-19T10:00:00\.000Z: /);
  for (const part of [url, MODEL, 'ECONNREFUSED', 'backlog 0/1']) assert.ok(r.ranked.vector.includes(part), part + ' in ' + r.ranked.vector);
  assert.equal(r.status.vector, r.ranked.vector);
  assert.equal(r.grep.complete, true);
  assert.deepEqual(r.ranked.hits.map(h => h.id), ['t-a-0'], 'BM25 still answers');
  now.advance(60000);
  const again = await store.search('pumpkin');
  assert.match(again.ranked.vector, /^down since 2026-11-19T10:00:00\.000Z: /, 'the first failure of this outage, not this query');
  await store.close();
});

test('8: a server that accepts and never answers - the search returns within timeoutMs + 1 s, TIMEOUT', async () => {
  const fake = await server();
  fake.knobs.hang = true;
  const root = newRoot();
  const store = await open(root, fake.url, { timeoutMs: 400 });
  await store.recordTurn('a', [{ role: 'owner', text: 'quiet server' }]);
  const started = Date.now();
  const r = await store.search('quiet');
  assert.ok(Date.now() - started < 1400, `${Date.now() - started} ms`);
  assert.match(r.ranked.vector, /^down since .* · TIMEOUT · /);
  assert.equal(r.ranked.source, 'bm25 · 本地索引 · 向量不可用');
  assert.equal(r.grep.complete, true);
  await store.close();
});

test('8: wrong dimensions are a failure (DIMS), not a vector', async () => {
  const fake = await server();
  fake.knobs.dims = 384;
  const root = newRoot();
  const store = await open(root, fake.url);
  await store.recordTurn('a', [{ role: 'owner', text: 'dims' }]);
  const r = await store.search('dims');
  assert.match(r.ranked.vector, / · DIMS · expected 1024 dimensions, got 384/);
  assert.deepEqual(fileIds(root), []);
  await store.close();
});

test('8: the outage is logged once, noticed once after 10 minutes (also across a restart), and cleared when healthy with no backlog', async () => {
  const fake = await server();
  fake.knobs.status = 500;
  const now = clock();
  const root = newRoot();
  mkdirSync(root, { recursive: true, mode: 0o700 });
  writeFileSync(join(root, USER + '.notices.jsonl'), JSON.stringify({ at: now(), text: 'another notice' }) + '\n', { mode: 0o600 });
  const events = [];
  const logger = (event, fields) => events.push([event, fields]);
  let store = await open(root, fake.url, { now, logger });
  await store.recordTurn('a', [{ role: 'owner', text: 'lantern festival' }]);
  await store.search('lantern');
  await store.search('lantern');
  const downs = () => events.filter(([e]) => e === 'memory/vector-down');
  assert.equal(downs().length, 1, 'one log line for the outage');
  assert.deepEqual(Object.keys(downs()[0][1]).sort(), ['code', 'model', 'since', 'url']);
  const kinds = () => readFileSync(join(root, USER + '.notices.jsonl'), 'utf8').split('\n').filter(Boolean).map(JSON.parse).filter(n => n.kind === 'vector-down');
  now.advance(9 * 60000);
  await store.search('lantern');
  assert.equal(kinds().length, 0, 'not before 10 minutes');
  now.advance(2 * 60000);
  await store.search('lantern');
  await store.search('lantern');
  assert.equal(kinds().length, 1);
  assert.match(kinds()[0].text, /^本地向量检索自 2026-11-19T10:00:00\.000Z 不可用（http:\/\/127\.0\.0\.1:\d+ · bge-m3-q8_0 · HTTP_500），排序检索只用 BM25，可能漏掉意思相近的记录；未嵌入 1 条$/);
  const section = await profileSection({ store });
  assert.equal(section.split('本地向量检索自').length - 1, 1, 'once in the snapshot');
  assert.match(section, /Memory notices - tell your owner:\n- another notice\n- 本地向量检索自/);
  await store.close();
  // A restart during the outage adds no second notice.
  store = await open(root, fake.url, { now, logger });
  await store.search('lantern');
  now.advance(11 * 60000);
  await store.search('lantern');
  assert.equal(kinds().length, 1, 'deduplicated by kind across a restart');
  // Healthy again: the worker catches up, the notice of that kind goes, nothing else does.
  fake.knobs.status = 0;
  await ready(store);
  await store.search('lantern');
  assert.equal(kinds().length, 0);
  assert.deepEqual(store.statusLine().notices, ['another notice']);
  const up = events.filter(([e]) => e === 'memory/vector-up');
  assert.equal(up.length, 1);
  assert.ok(up[0][1].since && up[0][1].until, JSON.stringify(up[0][1]));
  await store.close();
});

test('8: after a restart the backfill completes and vector is ok', async () => {
  const port = await refusedPort();
  const root = newRoot();
  let store = await open(root, `http://127.0.0.1:${port}`);
  for (let i = 0; i < 5; i++) await store.recordTurn('t' + i, [{ role: 'owner', text: 'backfill ' + i }]);
  await store.search('backfill');
  assert.match(store.statusLine().vector, /^down since .*backlog 0\/5$/);
  await store.close();
  const fake = await server();
  store = await open(root, fake.url);
  await ready(store);
  assert.equal(fileIds(root).length, 5);
  assert.equal((await store.search('backfill')).ranked.vector, 'ok');
  await store.close();
});

test('8: an unwritable vector file - forget succeeds and logs; the store still opens; once writable, the next load removes the row', async () => {
  const fake = await server();
  const root = newRoot();
  const events = [];
  const logger = (event, fields) => events.push([event, fields]);
  let store = await open(root, fake.url, { logger });
  await store.recordTurn('a', [{ role: 'owner', text: 'secret garden' }, { role: 'steward', text: 'the garden reply' }]);
  await ready(store);
  const unblock = blockFilter(root);
  const f = await store.forget(['t-a-0'], { reason: 'asked', by: 'owner' });
  assert.deepEqual(f.forgotten, ['t-a-0']);
  const failed = events.find(([e]) => e === 'memory/vector-filter-failed');
  assert.ok(failed, 'logged');
  assert.deepEqual(failed[1].ids, ['t-a-0']);
  assert.ok(!JSON.stringify(failed).includes('secret garden'), 'no text in the log');
  assert.ok(fileIds(root).includes('t-a-0'), 'the row is still on disk');
  const r = await store.search('secret garden');
  assert.ok(![...r.grep.hits, ...r.ranked.hits].some(h => h.id === 't-a-0'), 'never in a result');
  await store.close();
  store = await open(root, fake.url, { logger });
  await store.vector.whenLoaded();
  assert.ok(!store.vector.mem.has('t-a-0'));
  assert.ok(fileIds(root).includes('t-a-0'), 'still unwritable: still there');
  await store.close();
  unblock();
  store = await open(root, fake.url, { logger });
  await store.vector.whenLoaded();
  assert.deepEqual(fileIds(root), ['t-a-1']);
  await store.close();
});

// ---------------------------------------------------------------- 9. reconciliation and races

/** A root with entries a, b on disk and the vector file written from them. */
async function seededFile() {
  const fake = await server();
  const root = newRoot();
  const store = await open(root, fake.url);
  await store.recordTurn('a', [{ role: 'owner', text: 'alpha entry' }]);
  await store.recordTurn('b', [{ role: 'owner', text: 'beta entry' }]);
  await ready(store);
  await store.close();
  return { fake, root };
}

test('9: an extra id leaves the file; a wrong model or input size rebuilds it whole; a torn last line is skipped', async () => {
  let { fake, root } = await seededFile();
  appendFileSync(vfile(root), row('t-ghost-0', hashVector('ghost', DIMS)) + '\n');
  let store = await open(root, fake.url);
  await ready(store);
  assert.deepEqual(fileIds(root).sort(), ['t-a-0', 't-b-0']);
  await store.close();
  for (const header of [JSON.stringify({ model: 'multilingual-e5-small-q8_0', dims: 384, quant: 'int8', input_chars: 1000 }),
    JSON.stringify({ model: MODEL, dims: DIMS, quant: 'int8', input_chars: 500 })]) {
    writeFileSync(vfile(root), header + '\n' + row('t-a-0', hashVector('x', 384)) + '\n');
    const before = fake.embeds();
    store = await open(root, fake.url);
    await ready(store);
    assert.equal(readFileSync(vfile(root), 'utf8').split('\n')[0], headerLine(MODEL), header);
    assert.deepEqual(fileIds(root).sort(), ['t-a-0', 't-b-0']);
    assert.equal(fake.embeds() - before, 2, 'everything embedded again');
    await store.close();
  }
  // A torn last line: skipped; the entry is embedded again, and the append starts on a new line.
  const lines = readFileSync(vfile(root), 'utf8').split('\n').filter(Boolean);
  const kept = lines.filter(l => !l.includes('t-b-0'));
  writeFileSync(vfile(root), kept.join('\n') + '\n' + lines.find(l => l.includes('t-b-0')).slice(0, 40));
  store = await open(root, fake.url);
  await ready(store);
  const scan = await scanVectorFile(vfile(root));
  assert.equal(scan.rows, 2);
  assert.equal(scan.bad, 1, 'the torn line stays a torn line; the new row is whole');
  assert.deepEqual(fileIds(root).sort(), ['t-a-0', 't-b-0']);
  await store.close();
});

test('9: no embedding request before the load is done; a forget during the load leaves neither memory nor file with the id', async () => {
  const { fake, root } = await seededFile();
  let release;
  const paused = new Promise(resolve => { release = resolve; });
  const store = await open(root, fake.url, { faults: { vectorBeforeReconcile: () => paused } });
  await store.recordTurn('c', [{ role: 'owner', text: 'gamma entry written during the load' }]);
  await store.forget(['t-a-0'], { reason: 'during the load', by: 'owner' });
  const before = fake.embeds();
  await new Promise(resolve => setTimeout(resolve, 200));
  assert.equal(fake.embeds(), before, 'the worker waits for the load');
  assert.equal(store.statusLine().vector, 'loading');
  release();
  await ready(store);
  assert.ok(fake.embeds() > before);
  assert.ok(!store.vector.mem.has('t-a-0'));
  assert.deepEqual(fileIds(root).sort(), ['t-b-0', 't-c-0']);
  await store.close();
});

test('9: an embedding in flight when its entry is forgotten is thrown away', async () => {
  const fake = await server();
  const root = newRoot();
  const store = await open(root, fake.url);
  await store.vector.whenLoaded();
  fake.knobs.delayMs = 400;
  await store.recordTurn('a', [{ role: 'owner', text: 'slow to embed' }]);
  await until(() => fake.requests.some(r => r.input === 'slow to embed'), 'the request in flight');
  await store.forget(['t-a-0'], { reason: 'in flight', by: 'owner' });
  await new Promise(resolve => setTimeout(resolve, 600));
  await store.flush();
  assert.deepEqual(fileIds(root), []);
  assert.ok(!store.vector.mem.has('t-a-0'));
  assert.equal(store.statusLine().vector, 'ok');
  await store.close();
});

test('5/3.8.2: an over-long entry is embedded from its first 1000 code points; too long still, 400; then it is skipped', async () => {
  const fake = await server();
  const root = newRoot();
  const store = await open(root, fake.url);
  await store.vector.whenLoaded();
  // Longer than 500 code points is too long; so is anything starting with y, at any length.
  fake.knobs.tooLongIf = input => [...input].length > 500 || input.startsWith('y');
  const long = '😀'.repeat(1200); // surrogate pairs: cut by code point, never half a pair
  await store.recordTurn('a', [{ role: 'owner', text: long }]);
  await store.recordTurn('b', [{ role: 'owner', text: 'y'.repeat(450) }]);
  await ready(store);
  const sent = id => fake.requests.filter(r => r.input.startsWith(id === 'a' ? '😀' : 'y')).map(r => [...r.input].length);
  assert.deepEqual(sent('a'), [1000, 400], 'the first 1000 code points, then the first 400');
  assert.deepEqual(sent('b'), [450, 400], 'too long at 400 as well: skipped, not retried again');
  assert.ok(fake.requests.every(r => !/[\ud800-\udbff]$/.test(r.input)), 'no half surrogate pair');
  assert.equal(store.statusLine().vector_skipped, 1);
  assert.deepEqual(fileIds(root), ['t-a-0']);
  await store.close();
});

// ---------------------------------------------------------------- 10. purge

async function purgeSeed(url) {
  const root = newRoot();
  const store = await open(root, url);
  await store.recordTurn('keep', [{ role: 'owner', text: 'keep this entry' }]);
  await store.recordTurn('gone', [{ role: 'owner', text: 'PURGE-ME secret 4242' }]);
  await store.recordTurn('old', [{ role: 'owner', text: 'forgotten before' }]);
  if (url) await ready(store);
  await store.flush();
  return { root, store };
}

test('10: a purge takes every purged id out of the file, one forgotten earlier with a row put back by hand included', async () => {
  const fake = await server();
  const { root, store } = await purgeSeed(fake.url);
  await store.forget(['t-old-0'], { reason: 'earlier', by: 'owner' });
  appendFileSync(vfile(root), row('t-old-0', hashVector('old', DIMS)) + '\n');
  const r = await store.purge(['t-gone-0', 't-old-0']);
  assert.equal(r.purged, true, JSON.stringify(r));
  assert.deepEqual(fileIds(root), ['t-keep-0']);
  await store.vector.whenLoaded();
  assert.equal(store.statusLine().vector, 'ok');
  await store.close();
});

test('10: purged ids all forgotten before (no forget, no commit) - the step 1 filter still runs', async () => {
  const fake = await server();
  const { root, store } = await purgeSeed(fake.url);
  await store.forget(['t-gone-0'], { reason: 'earlier', by: 'owner' });
  appendFileSync(vfile(root), row('t-gone-0', hashVector('gone', DIMS)) + '\n');
  const r = await store.purge(['t-gone-0']);
  assert.equal(r.purged, true);
  assert.ok(!journalOf(root).some(j => j.phase === 'forgetting'));
  assert.deepEqual(fileIds(root).sort(), ['t-keep-0', 't-old-0']);
  await store.close();
});

test('10: step 1 cannot filter (unwritable file) - aborted before the build, repository bytes unchanged, done{rolled_back}; no "interrupted" next boot', async () => {
  for (const alreadyForgotten of [true, false]) {
    const fake = await server();
    const { root, store } = await purgeSeed(fake.url);
    if (alreadyForgotten) {
      await store.forget(['t-gone-0'], { reason: 'earlier', by: 'owner' });
      // Its row left with the forget; one left behind (a failed best-effort filter) is put back.
      appendFileSync(vfile(root), row('t-gone-0', hashVector('gone', DIMS)) + '\n');
    }
    await store.flush();
    const before = treeHash(repo(root));
    const vectorsBefore = readFileSync(vfile(root));
    const unblock = blockFilter(root);
    let built = false;
    store.faults = { beforeBuild: () => { built = true; } };
    const r = await store.purge(['t-gone-0']);
    assert.equal(r.purged, false);
    assert.match(r.reason, /^vector filter failed: /);
    assert.equal(built, false, 'stopped before step 2');
    if (alreadyForgotten) assert.equal(treeHash(repo(root)), before, 'the old repository is byte for byte the same');
    assert.ok(gitIn(repo(root), 'log', '-p', '--all').stdout.toString().includes('PURGE-ME'), 'nothing purged');
    assert.deepEqual(readFileSync(vfile(root)), vectorsBefore);
    const last = journalOf(root).at(-1);
    assert.equal(last.phase, 'done'); assert.equal(last.outcome, 'rolled_back'); assert.match(last.reason, /^vector filter failed: /);
    assert.deepEqual(readdirSync(root).filter(n => n.includes('.purge-new-') || n.includes('.pre-purge-') || (n.includes('.filter-') && !n.endsWith(BLOCKER))), []);
    await store.close();
    unblock();
    const rows = journalOf(root).length;
    const next = await open(root, fake.url);
    assert.equal(next.statusLine().notices, undefined, 'a refused purge is not reported as interrupted');
    assert.equal(journalOf(root).length, rows, 'the boot wrote no recovery row');
    await next.close();
  }
});

test('10: a commit failing after `forgetting` - commit_failing, done{rolled_back}; the next boot reports nothing', async () => {
  const { root, store } = await purgeSeed();
  writeFileSync(join(repo(root), '.git', 'index.lock'), '');
  const r = await store.purge(['t-gone-0']);
  assert.equal(r.refused, 'commit_failing');
  const journal = journalOf(root);
  assert.deepEqual(journal.map(j => j.phase), ['forgetting', 'done']);
  assert.equal(journal.at(-1).outcome, 'rolled_back');
  await store.close();
  rmSync(join(repo(root), '.git', 'index.lock'));
  const next = await open(root);
  assert.equal(next.statusLine().notices, undefined);
  assert.equal(journalOf(root).length, 2);
  await next.close();
});

test('10: vectors off with an old file beside - purged ids still leave it; a foreign header is filtered, not refused; a broken one deletes it; no file, none made', async () => {
  // An old file of this model.
  let { root, store } = await purgeSeed();
  writeFileSync(vfile(root), headerLine(MODEL) + '\n' + row('t-keep-0', hashVector('k', DIMS)) + '\n' + row('t-gone-0', hashVector('g', DIMS)) + '\n', { mode: 0o600 });
  assert.equal((await store.purge(['t-gone-0'])).purged, true);
  assert.deepEqual(fileIds(root), ['t-keep-0']);
  await store.close();
  // A header of another model (the reconciliation never ran): filtered with its own header.
  ({ root, store } = await purgeSeed());
  const e5 = JSON.stringify({ model: 'multilingual-e5-small-q8_0', dims: 384, quant: 'int8', input_chars: 1000 });
  writeFileSync(vfile(root), e5 + '\n' + row('t-keep-0', hashVector('k', 384)) + '\n' + row('t-gone-0', hashVector('g', 384)) + '\n', { mode: 0o600 });
  assert.equal((await store.purge(['t-gone-0'])).purged, true);
  const lines = readFileSync(vfile(root), 'utf8').split('\n').filter(Boolean);
  assert.equal(lines[0], e5);
  assert.deepEqual(lines.slice(1).map(l => JSON.parse(l).id), ['t-keep-0']);
  await store.close();
  // A broken header: the whole file goes.
  ({ root, store } = await purgeSeed());
  writeFileSync(vfile(root), '{"not":"a header"}\n' + row('t-gone-0', hashVector('g', DIMS)) + '\n', { mode: 0o600 });
  assert.equal((await store.purge(['t-gone-0'])).purged, true);
  assert.ok(!existsSync(vfile(root)));
  await store.close();
  // No file: none is made.
  ({ root, store } = await purgeSeed());
  assert.equal((await store.purge(['t-gone-0'])).purged, true);
  assert.ok(!existsSync(vfile(root)));
  await store.close();
});

test('10: the reconciliation\'s rewrite and a purge at once - the load ends while the purge holds the lock; no purged id is left', async () => {
  const { fake, root } = await seededFile();
  appendFileSync(vfile(root), row('t-ghost-0', hashVector('ghost', DIMS)) + '\n');
  let release;
  const paused = new Promise(resolve => { release = resolve; });
  const store = await open(root, fake.url, { faults: { vectorBeforeReconcile: () => paused } });
  store.faults.afterVerify = () => { release(); return new Promise(resolve => setTimeout(resolve, 50)); };
  const r = await store.purge(['t-a-0']);
  assert.equal(r.purged, true, JSON.stringify(r));
  await store.vector.whenLoaded();
  await ready(store);
  assert.deepEqual(fileIds(root), ['t-b-0']);
  await store.close();
});

for (const [name, write] of [
  ['drops one more line', (line, i) => (i === 1 ? null : line)],
  ['writes an empty file', () => null],
  ['loses the header', (line, i) => (i === 0 ? null : line)],
]) {
  test(`10: the two-sided check catches a filter that ${name}; the vector file is unchanged and the purge stops`, async () => {
    const fake = await server();
    const { root, store } = await purgeSeed(fake.url);
    const before = readFileSync(vfile(root));
    store.faults = { vectorFilterWrite: write };
    const r = await store.purge(['t-gone-0']);
    assert.equal(r.purged, false);
    assert.match(r.reason, /two-sided check failed/);
    assert.deepEqual(readFileSync(vfile(root)), before);
    assert.equal(journalOf(root).at(-1).outcome, 'rolled_back');
    assert.deepEqual(readdirSync(root).filter(n => n.includes('.filter-')), []);
    await store.close();
  });
}

test('10: a purged row written back between step 1 and step 5 - the old repository comes back, done{rolled_back}, the owner is told', async () => {
  const fake = await server();
  const { root, store } = await purgeSeed(fake.url);
  store.faults = { afterSwap: () => appendFileSync(vfile(root), row('t-gone-0', hashVector('gone', DIMS)) + '\n') };
  const r = await store.purge(['t-gone-0']);
  assert.equal(r.purged, false);
  assert.match(r.reason, /old repository is back/);
  assert.ok(r.failures.some(f => /^vector file: 1 purged rows are back; \d+ rows, \d+ after step 1$/.test(f)), JSON.stringify(r.failures));
  const last = journalOf(root).at(-1);
  assert.equal(last.outcome, 'rolled_back');
  assert.ok(gitIn(repo(root), 'log', '-p', '--all').stdout.toString().includes('PURGE-ME'));
  assert.deepEqual(readdirSync(root).filter(n => n.includes('.purge-new-') || n.includes('.pre-purge-')), []);
  await store.close();
});

test('10: the boot\'s recovery filters plan.ids; when it cannot, the memory still opens and the owner is told', async () => {
  const root = newRoot();
  const fixture = new URL('./fixtures/memory-crash.mjs', import.meta.url).pathname;
  // Killed inside step 1, before its vector filter: the purged id is still in the file.
  const child = spawnSync(process.execPath, [fixture, root, USER, 'beforeForgetStrip', '0'], { encoding: 'utf8' });
  assert.equal(child.signal, 'SIGKILL', child.stderr + child.stdout);
  assert.ok(fileIds(root).includes('t-gone-0'));
  const unblock = blockFilter(root);
  const store = await open(root);
  assert.equal(journalOf(root).at(-1).phase, 'done');
  const notices = store.statusLine().notices ?? [];
  assert.ok(notices.some(n => /^向量文件里仍有已彻底删除的编号 t-gone-0，原因 .*；下次载入会再去掉$/.test(n)), JSON.stringify(notices));
  assert.ok(notices.some(n => /^上次彻底删除/.test(n)), 'the interruption notice as before');
  await store.close();
  unblock();
  const fake = await server();
  const next = await open(root, fake.url);
  await next.vector.whenLoaded();
  assert.ok(!fileIds(root).includes('t-gone-0'), 'the load reconciled it away');
  await next.close();
});

test('10: a filter that fails during an ordinary forget keeps the derived row and tells the owner; the next boot removes it and takes the notice back', async () => {
  const fake = await server();
  const root = newRoot();
  let store = await open(root, fake.url);
  await store.recordTurn('a', [{ role: 'owner', text: 'the zebra note FORGET-ME' }]);
  await ready(store);
  assert.deepEqual(fileIds(root), ['t-a-0']);
  const unblock = blockFilter(root);
  const r = await store.forget(['t-a-0'], { reason: 'owner asked', by: 'owner' });
  assert.deepEqual(r.forgotten, ['t-a-0'], 'the forget itself still succeeds');
  assert.ok(fileIds(root).includes('t-a-0'), 'the derived row is still in the file');
  const log = () => readdirSync(join(repo(root), 'log')).filter(n => n.endsWith('.jsonl'))
    .map(n => readFileSync(join(repo(root), 'log', n), 'utf8')).join('');
  assert.ok(!log().includes('FORGET-ME'), 'and the text is out of the log, so no search can return it');
  const notices = store.statusLine().notices ?? [];
  assert.ok(notices.some(n => /^向量文件里仍有已彻底删除的编号 t-a-0，原因 .*；下次载入会再去掉$/.test(n)), JSON.stringify(notices));
  await store.close();
  unblock();
  store = await open(root, fake.url);
  await store.vector.whenLoaded();
  await ready(store);
  assert.ok(!fileIds(root).includes('t-a-0'), 'the boot removed the derived row');
  assert.ok(!(store.statusLine().notices ?? []).some(n => n.startsWith('向量文件里仍有已彻底删除的编号')),
    JSON.stringify(store.statusLine().notices));
  await store.close();
});

// ---------------------------------------------------------------- review of r27's build

test('10: a torn line mid-file and no row for the purged id - step 1 still rewrites the file without it, so step 5 passes', async () => {
  const fake = await server();
  const { root, store } = await purgeSeed(fake.url);
  await store.forget(['t-gone-0'], { reason: 'earlier', by: 'owner' }); // its row leaves the file
  appendFileSync(vfile(root), '{"id":"t-torn-0","v":"AAAA');           // a crash inside an append
  await store.recordTurn('later', [{ role: 'owner', text: 'after the crash' }]);
  await ready(store); // the next append starts with a newline: the torn line stays mid-file
  assert.equal((await scanVectorFile(vfile(root))).bad, 1);
  const r = await store.purge(['t-gone-0']);
  assert.equal(r.purged, true, JSON.stringify(r));
  assert.equal((await scanVectorFile(vfile(root))).bad, 0);
  assert.deepEqual(fileIds(root).sort(), ['t-keep-0', 't-later-0', 't-old-0']);
  await store.close();
});

test('10: a filter killed mid-way left its temporary file - the next purge removes it with the purged row in it', async () => {
  const fake = await server();
  const { root, store } = await purgeSeed(fake.url);
  writeFileSync(vfile(root) + '.filter-999999', headerLine(MODEL) + '\n' + row('t-gone-0', hashVector('gone', DIMS)) + '\n', { mode: 0o600 });
  const r = await store.purge(['t-gone-0']);
  assert.equal(r.purged, true, JSON.stringify(r));
  assert.deepEqual(readdirSync(root).filter(n => n.includes('.filter-')), []);
  assert.ok(!fileIds(root).includes('t-gone-0'));
  await store.close();
});

const downNotices = store => (store.statusLine().notices ?? []).filter(n => n.startsWith('本地向量检索自'));

test('8: an acknowledged vector-down notice is not added again in the same outage; a failure after recovery is a new one', async () => {
  const fake = await server();
  fake.knobs.status = 500;
  const now = clock();
  const root = newRoot();
  const store = await open(root, fake.url, { now });
  await store.recordTurn('a', [{ role: 'owner', text: 'lantern festival' }]);
  await store.search('lantern');
  now.advance(11 * 60000);
  await store.search('lantern');
  assert.equal(downNotices(store).length, 1);
  store.acknowledgeNotices();
  await store.search('lantern');
  now.advance(11 * 60000);
  await store.search('lantern');
  assert.deepEqual(downNotices(store), [], 'the same outage, acknowledged: not again');
  fake.knobs.status = 0;
  await ready(store);
  await store.search('lantern');
  fake.knobs.status = 500;
  await store.search('lantern');
  now.advance(11 * 60000);
  await store.search('lantern');
  assert.equal(downNotices(store).length, 1, 'a failure after recovery is a new outage');
  await store.close();
});

test('8: healthy again with a backlog left - the notice stays until the backlog is 0', async () => {
  const fake = await server();
  fake.knobs.status = 500;
  const now = clock();
  const root = newRoot();
  const store = await open(root, fake.url, { now });
  await store.recordTurn('a', [{ role: 'owner', text: 'lantern festival' }]);
  await store.search('lantern');
  now.advance(11 * 60000);
  await store.search('lantern');
  assert.equal(downNotices(store).length, 1);
  const entryRequests = () => fake.requests.filter(r => r.input === 'lantern festival').length;
  const before = entryRequests();
  fake.knobs.hangIf = input => input === 'lantern festival'; // the worker's embedding of the entry hangs
  fake.knobs.status = 0;
  await until(() => entryRequests() > before, 'the worker asks again');
  const r = await store.search('lantern');
  assert.equal(r.ranked.source, 'bm25+向量 · 本地索引 · bge-m3-q8_0', 'the query itself was answered');
  assert.equal(r.ranked.vector, 'backlog 0/1');
  assert.equal(downNotices(store).length, 1, 'healthy is not enough: the backlog is not 0');
  fake.knobs.hangIf = () => false;
  await until(() => downNotices(store).length === 0, 'the notice cleared once the backlog is 0');
  assert.equal(store.statusLine().vector, 'ok');
  await store.close();
});

test('8: a vector-down notice an earlier process left goes once the server answers - no backlog, no search needed', async () => {
  const fake = await server();
  const root = newRoot();
  let store = await open(root, fake.url);
  await store.recordTurn('a', [{ role: 'owner', text: 'lantern festival' }]);
  await ready(store);
  await store.close();
  const since = '2026-11-19T10:00:00.000Z';
  appendFileSync(join(root, USER + '.notices.jsonl'), JSON.stringify({ at: since, kind: 'vector-down', since,
    text: `本地向量检索自 ${since} 不可用（${fake.url} · ${MODEL} · ECONNREFUSED），排序检索只用 BM25，可能漏掉意思相近的记录；未嵌入 0 条` }) + '\n');
  store = await open(root, fake.url);
  assert.equal(downNotices(store).length, 1);
  await until(() => downNotices(store).length === 0, 'the old notice cleared', 5000);
  assert.equal(store.statusLine().vector, 'ok');
  await store.close();
});

test('3.8.2: the worker embeds what has no vector newest first, whatever order the log holds them in', async () => {
  // The second case writes the entries out of time order (the clock jumps back and forth).
  for (const offsets of [[1, 2, 3, 4], [3, 1, 4, 2]]) {
    const root = newRoot();
    const base = Date.parse('2026-11-19T10:00:00.000Z');
    let i = 0;
    const now = () => new Date(base + (offsets[Math.min(i, offsets.length - 1)] * 60000)).toISOString();
    let store = await open(root, undefined, { now });
    for (const name of ['one', 'two', 'three', 'four']) { await store.recordTurn(name, [{ role: 'owner', text: 'order ' + name }]); i++; }
    await store.close();
    const fake = await server();
    store = await open(root, fake.url);
    await ready(store);
    const names = ['one', 'two', 'three', 'four'];
    const expected = names.map((n, j) => [n, offsets[j]]).sort((a, b) => b[1] - a[1]).map(([n]) => 'order ' + n);
    assert.deepEqual(fake.requests.map(r => r.input).filter(t => t.startsWith('order ')), expected, String(offsets));
    await store.close();
  }
});

test('3.3: the vectors in memory grow by fixed blocks, never by copying; a forgotten row is reused', async () => {
  const fake = await server();
  const root = newRoot();
  const store = await open(root, fake.url);
  for (let j = 0; j < 3; j++) await store.recordTurn('g' + j, [{ role: 'owner', text: 'grow ' + j }]);
  await ready(store);
  const mem = store.vector.mem;
  const block = mem.blocks[0];
  assert.equal(mem.blocks.length, 1);
  await store.forget(['t-g1-0'], { reason: 'test', by: 'owner' });
  const rows = mem.ids.length;
  await store.recordTurn('g3', [{ role: 'owner', text: 'grow 3' }]);
  await ready(store);
  assert.equal(mem.ids.length, rows, 'the forgotten row was reused');
  assert.equal(mem.blocks[0], block, 'never reallocated');
  await store.close();
});
