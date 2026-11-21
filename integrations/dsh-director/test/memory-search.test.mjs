/** Plugin step 21 (design-steward 3.8.1, acceptance 3.8.4 items 1-5 without vectors, and
 *  3.7's replay consistency with date words): memory_search gives whole entries grouped by
 *  the owner's calendar day, dated lines, one budget, machine-readable hits; the BM25 index
 *  carries date words that a forget takes back exactly. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Bm25Index, documentTokens } from '../lib/memory/bm25.mjs';
import { dateTokens, dateWords, formatLine, formatProfileLine, stamp } from '../lib/memory/render.mjs';
import { MemoryStore, SEARCH_CHARS } from '../lib/memory/store.mjs';
import { openMemory, registerMemoryTools } from '../lib/memory/tools.mjs';
import { memoryTimeZoneProblem, validateHost } from '../lib/host.mjs';

const USER = 'owner@example.com';
const newRoot = () => mkdtempSync(join(tmpdir(), 'dsh-memsearch-'));
const open = (root, extra = {}) => MemoryStore.open({ root, userId: USER, ...extra });
const PARIS = 'Europe/Paris';
const settle = () => new Promise(resolve => setImmediate(() => setImmediate(resolve)));

/** The index's statistics: what every score is made of. */
const stats = index => ({ size: index.size, totalLength: index.totalLength, df: [...index.df].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)) });
/** A fresh index over these entries, built the way the store builds one. */
function fresh(entries, tz) {
  const index = new Bm25Index();
  for (const e of entries) index.add(e.id, [...documentTokens(e.text), ...dateTokens(e.at, tz)], e.at);
  return index;
}

/** 4a: every line with an id is one hit shown full / see_*, and the omitted hits are the
 *  omitted list, one to one. Entries here have no newline, so a line is a text line. */
function checkHits(answer) {
  const lineIds = answer.text.split('\n').map(l => /^\[[^\]]+\] \S+ \{([^}]+)\}/.exec(l)?.[1]).filter(Boolean).sort();
  const shownIds = answer.hits.filter(h => h.shown !== 'omitted' && h.id !== undefined && h.id !== 'profile').map(h => h.id).sort();
  assert.deepEqual(lineIds, shownIds, 'each line with an id is exactly one shown hit');
  assert.deepEqual(answer.hits.filter(h => h.shown === 'omitted').map(h => h.id ?? h.ref), answer.omitted.map(o => o.id ?? o.ref),
    'the omitted hits are the omitted list, in order');
  for (const h of answer.hits) assert.ok(['full', 'see_grep', 'see_ranked', 'omitted'].includes(h.shown), JSON.stringify(h));
}
/** The characters the placed lines take (the budget counts lines, not the newlines between). */
const lineChars = text => text.split('\n').filter(Boolean).reduce((n, l) => n + l.length, 0);

// ---------------------------------------------------------------- 1. line format and zone

test('1: both forms of `at`, UTC and Europe/Paris (with summer time), seven weekdays, a bad time, the cross-day entry', () => {
  for (const at of ['2026-11-19T10:25:00.123456+00:00', '2026-11-19T10:25:00.123Z'])
    assert.equal(stamp(at, 'UTC'), '2026-11-19 周四 10:25+00:00', at);
  assert.equal(stamp('2026-11-19T10:25:00.123456+00:00', PARIS), '2026-11-19 周四 11:25+01:00');
  assert.equal(stamp('2026-07-01T10:25:00Z', PARIS), '2026-07-01 周三 12:25+02:00', 'summer time: the offset of that moment');
  const week = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  week.forEach((day, i) => assert.equal(stamp(`2026-11-${String(16 + i).padStart(2, '0')}T12:00:00Z`, 'UTC'), `2026-11-${16 + i} ${day} 12:00+00:00`, day));
  for (const bad of ['', 'yesterday', '2026-11-19', '2026-11-19T10:25:00', undefined, 42])
    assert.equal(formatLine({ id: 't-x-0', at: bad, role: 'owner', text: 'hi' }, 'UTC'), '[时间不明] 业主 {t-x-0}: hi', String(bad));
  // Cross-day: 23:30Z is the next day in Paris - in the line, the group and the date words.
  assert.equal(stamp('2026-11-19T23:30:00Z', PARIS), '2026-11-20 周五 00:30+01:00');
  const words = dateWords('2026-11-19T23:30:00Z', PARIS);
  for (const w of ['11月20日', '周五', 'Friday']) assert.ok(words.includes(w), w);
  assert.ok(!words.includes('周四'));
});

test('1: the date words are exactly the 3.8.1 list; none for a bad time', () => {
  assert.deepEqual(dateWords('2026-11-19T10:25:00Z', 'UTC'),
    ['2026-11-19', '2026年11月19日', '11月19日', '周四', '星期四', 'November', 'Nov', 'Thursday', 'Thu']);
  assert.deepEqual(dateWords('2026-03-01T00:00:00Z', 'UTC'),
    ['2026-03-01', '2026年3月1日', '3月1日', '周日', '星期日', 'March', 'Mar', 'Sunday', 'Sun']);
  assert.deepEqual(dateWords('not a time', 'UTC'), []);
  assert.deepEqual(dateTokens('not a time', 'UTC'), []);
});

test('1: line shapes: roles, a note, the date-only mark, → 见, the remote line, the profile line', () => {
  const at = '2026-11-19T10:25:00Z';
  assert.equal(formatLine({ id: 't-1-o', at, role: 'owner', text: 'x' }, PARIS), '[2026-11-19 周四 11:25+01:00] 业主 {t-1-o}: x');
  assert.equal(formatLine({ id: 't-1-r', at, role: 'steward', text: 'y' }, 'UTC'), '[2026-11-19 周四 10:25+00:00] 管家 {t-1-r}: y');
  assert.equal(formatLine({ id: 'n-1', at, role: 'steward', source: 'note', text: 'z' }, 'UTC'), '[2026-11-19 周四 10:25+00:00] 管家（笔记） {n-1}: z');
  assert.equal(formatLine({ id: 't-1-o', at, role: 'owner', text: 'x' }, 'UTC', { dateOnly: true }), '[2026-11-19 周四 10:25+00:00] 业主 {t-1-o}（日期命中）: x');
  assert.equal(formatLine({ id: 't-1-o', at, role: 'owner', text: 'x' }, 'UTC', { see: 'grep' }), '[2026-11-19 周四 10:25+00:00] 业主 {t-1-o}: → 见 grep');
  assert.equal(formatLine({ id: 't-1-o', at, role: 'owner', channel: 'wechat', text: 'x' }, 'UTC'), '[2026-11-19 周四 10:25+00:00] 业主（wechat） {t-1-o}: x',
    'an entry relayed from a chat channel names it after the role');
  assert.equal(formatLine({ at, role: 'remote', text: 'fact' }, 'UTC'), '[2026-11-19 周四 10:25+00:00] 远端: fact');
  assert.equal(formatLine({ role: 'remote', text: 'fact' }, 'UTC'), '[时间不明] 远端: fact');
  assert.equal(formatProfileLine(3, 'likes tea'), '[档案 第 3 行] likes tea');
  assert.equal(formatLine({ id: 't-1-o', at, role: 'owner', text: 'a\n"b"\\' }, 'UTC'), '[2026-11-19 周四 10:25+00:00] 业主 {t-1-o}: a\n"b"\\', 'the text as it is, not escaped');
});

test('1: an invalid memory.timeZone stops the memory module with an error; the host still loads', async () => {
  assert.equal(memoryTimeZoneProblem(undefined), undefined);
  assert.equal(memoryTimeZoneProblem(PARIS), undefined);
  assert.match(memoryTimeZoneProblem('Mars/Olympus'), /memory\.timeZone "Mars\/Olympus" is not a time zone/);
  assert.match(memoryTimeZoneProblem(''), /must be an IANA time zone name/);
  assert.match(memoryTimeZoneProblem(1), /must be an IANA time zone name/);
  const memory = await openMemory({ userId: 'owner', root: newRoot(), timeZone: 'Mars/Olympus' });
  assert.match(memory.error, /Mars\/Olympus/);
  assert.equal(memory.store, undefined);
  const tools = new Map();
  registerMemoryTools({ tools: { register: t => tools.set(t.name, t) } }, { defineTool: t => t, jsonOutput: {}, memory });
  assert.deepEqual(await tools.get('memory_search').execute({ query: 'x' }, {}), { refused: 'memory_unavailable', reason: memory.error });
  await assert.rejects(open(newRoot(), { timeZone: 'Mars/Olympus' }), /not a time zone/);
  // The rest of the steward is not refused for it: validateHost does not judge the zone.
  const tableDir = mkdtempSync(join(tmpdir(), 'dsh-memsearch-roles-'));
  const { writeFileSync } = await import('node:fs');
  const role = prompt => ({ route: 'r', reasoning_effort: 'high', compaction: { thresholdRatio: 0.5 }, prompt, presented: [], forbidden: [] });
  writeFileSync(join(tableDir, 'roles.json'), JSON.stringify({ version: 1, director: role('d.md'), worker: role('w.md') }));
  writeFileSync(join(tableDir, 'd.md'), 'd'); writeFileSync(join(tableDir, 'w.md'), 'w');
  assert.doesNotThrow(() => validateHost({ version: '1.2.0', capabilities: [], context: async () => '', events: async () => ({ stop() {} }),
    config: { sessionId: 's', provider: 'p', model: 'm', stateDir: '/tmp/x', dshEntry: '/x', rolesFile: join(tableDir, 'roles.json'),
      todo: { module: 'dsh_todo', stateDirEnv: 'E' }, memory: { userId: 'owner', timeZone: 'Mars/Olympus' } } }));
  const ok = await openMemory({ userId: 'owner', root: newRoot(), timeZone: PARIS });
  assert.equal(ok.store.timeZone, PARIS);
  await ok.store.close();
});

// ---------------------------------------------------------------- 1a. date words in the index

test('1a: date words rank the right day, grep never sees them, memory_read gives the log text', async () => {
  const root = newRoot(); const store = await open(root);
  const body = 'the weekly sync went fine';
  await store.recordTurn('d19', [{ role: 'owner', at: '2026-11-19T10:00:00Z', text: body }]);
  await store.recordTurn('d12', [{ role: 'owner', at: '2026-11-12T10:00:00Z', text: body }]);
  for (const q of ['11月19日', '2026-11-19', 'Nov 19']) {
    const r = await store.search(q);
    assert.equal(r.ranked.hits[0].id, 't-d19-0', q);
    assert.equal(r.ranked.hits[0].date_only, true, q);
  }
  for (const q of ['周四', 'Thursday'])
    assert.deepEqual((await store.search(q)).ranked.hits.map(h => h.id).sort(), ['t-d12-0', 't-d19-0'], q + ': both are Thursdays');
  const grep = await store.search('11月19日');
  assert.deepEqual(grep.grep.hits, [], 'the date words are not in the text grep reads');
  assert.match(grep.ranked.text, /^\[2026-11-19 周四 10:00\+00:00\] 业主 \{t-d19-0\}（日期命中）: the weekly sync went fine$/m);
  const logged = readdirSync(join(root, USER, 'log')).filter(n => n.endsWith('.jsonl'))
    .flatMap(n => readFileSync(join(root, USER, 'log', n), 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l)));
  assert.deepEqual((await store.read(['t-d19-0', 't-d12-0'])).entries.map(e => e.text), ['t-d19-0', 't-d12-0'].map(id => logged.find(e => e.id === id).text));
  assert.ok(!JSON.stringify(logged).includes('11月19日'), 'the log holds the text only');
  await store.close();
});

test('1a: the date-only mark: only for a hit sharing no word with the query; a 块 in the text is a shared word', async () => {
  const root = newRoot(); const store = await open(root);
  await store.recordTurn('a', [{ role: 'owner', at: '2026-11-19T09:00:00Z', text: 'apple pie for dessert' }]);
  await store.recordTurn('b', [{ role: 'owner', at: '2026-10-19T09:00:00Z', text: '午饭花了 30 块' }]);
  await store.recordTurn('c', [{ role: 'owner', at: '2026-11-03T09:00:00Z', text: '11月19日 要交房租' }]);
  const r = await store.search('11月19日');
  const mark = Object.fromEntries(r.ranked.hits.map(h => [h.id, h.date_only === true]));
  assert.equal(mark['t-a-0'], true, 'met only through its date words');
  assert.equal(mark['t-c-0'], false, 'its text has the query\'s words');
  assert.doesNotMatch(r.ranked.text, /\{t-c-0\}（日期命中）/);
  const price = await store.search('19 块');
  const marked = Object.fromEntries(price.ranked.hits.map(h => [h.id, h.date_only === true]));
  assert.equal(marked['t-a-0'], true, 'a 19th with neither 19 nor 块 in its text');
  assert.equal(marked['t-b-0'], false, 'a 19th whose text has 块 shares a word with the query');
  await store.close();
});

test('1a: an entry quoting the query\'s words ranks above one that only its date words meet', async () => {
  const root = newRoot(); const store = await open(root);
  const quoted = '11月要把记账小工具做成手机能用的'; // 17 characters
  await store.recordTurn('q', [{ role: 'owner', at: '2026-10-05T09:00:00Z', text: quoted }]);
  for (let i = 0; i < 5; i++) await store.recordTurn('n' + i, [{ role: 'owner', at: `2026-11-1${i}T09:00:00Z`, text: 'nothing in common here ' + i }]);
  const r = await store.search(quoted);
  assert.equal(r.ranked.hits[0].id, 't-q-0');
  assert.ok(r.ranked.hits.slice(1).every(h => h.date_only === true), JSON.stringify(r.ranked.hits));
  await store.close();
});

test('1a: a change of memory.timeZone rebuilds the date words at the next start', async () => {
  const root = newRoot();
  let store = await open(root);
  await store.recordTurn('late', [{ role: 'owner', at: '2026-11-19T23:30:00Z', text: 'late night thought' }]);
  assert.deepEqual((await store.search('周四')).ranked.hits.map(h => h.id), ['t-late-0']);
  assert.deepEqual((await store.search('周五')).ranked.hits, []);
  await store.flush(); await store.close();
  store = await open(root, { timeZone: PARIS });
  assert.deepEqual((await store.search('周四')).ranked.hits, []);
  const r = await store.search('周五');
  assert.deepEqual(r.ranked.hits.map(h => h.id), ['t-late-0']);
  assert.equal(r.ranked.text, '[2026-11-20 周五 00:30+01:00] 业主 {t-late-0}（日期命中）: late night thought');
  await store.close();
});

test('1a / MUST-FIX 8: add then remove of a dated document leaves the index equal to one built without it', () => {
  const docs = [['a', 'apple pie', '2026-11-19T10:00:00Z'], ['b', '记账小工具', '2026-11-12T10:00:00Z'], ['c', 'apple tart', '2026-11-19T22:00:00Z']];
  const tokens = ([, text, at]) => [...documentTokens(text), ...dateTokens(at, PARIS)];
  const index = new Bm25Index();
  for (const d of docs) index.add(d[0], tokens(d), d[2]);
  assert.equal(index.remove('c', tokens(docs[2])), true);
  const without = new Bm25Index();
  for (const d of docs.slice(0, 2)) without.add(d[0], tokens(d), d[2]);
  assert.deepEqual(stats(index), stats(without));
  for (const q of ['apple', '11月19日', '周四', 'November', '19'])
    assert.deepEqual(index.search(q), without.search(q), q);
});

test('1a / 3.7: 200 entries, 100 forgotten: df and total length equal an index built from the 100 left, and a reload', async () => {
  const root = newRoot(); const store = await open(root, { timeZone: PARIS });
  const turns = [];
  for (let i = 0; i < 200; i++) {
    const day = 10 + (i % 20), hour = (i * 7) % 24;
    turns.push(store.recordTurn('r' + i, [{ role: i % 3 ? 'owner' : 'steward', at: `2026-11-${day}T${String(hour).padStart(2, '0')}:30:00Z`,
      text: `entry ${i} about ${['ledger', '记账', 'trip', '周报'][i % 4]}` }]));
  }
  await Promise.all(turns); await store.flush();
  const gone = Array.from({ length: 100 }, (_, i) => `t-r${i * 2}-0`);
  assert.equal((await store.forget(gone, { reason: 'test', by: 'owner' })).forgotten.length, 100);
  const left = [...store.entries.values()];
  assert.equal(left.length, 100);
  const want = stats(fresh(left, PARIS));
  for (const t of ['11', '19', '周四', 'november']) assert.ok(want.df.some(([k]) => k === t), t);
  assert.deepEqual(stats(store.index), want, 'the forget took back every token it added, date words included');
  await store.flush(); await store.close();
  const reopened = await open(root, { timeZone: PARIS });
  assert.deepEqual(stats(reopened.index), want, 'a reload builds the same index');
  await reopened.close();
});

test('3.7: write, synchronous load, background load and both removes give the same document tokens (a 23:30Z entry in Paris included)', async () => {
  const root = newRoot();
  const now = () => '2026-11-20T12:00:00Z';
  let store = await open(root, { timeZone: PARIS, now });
  const rows = [
    ['old', '2024-03-14T23:30:00Z', 'an old ledger idea'],  // a background month (older than 12)
    ['old2', '2024-03-15T08:00:00Z', 'another old one'],
    ['late', '2026-11-19T23:30:00Z', 'late night ledger'],  // the 20th in Paris
    ['day', '2026-11-19T10:00:00Z', 'day ledger'],
    ['gone', '2026-11-18T23:30:00Z', 'to be forgotten'],
  ];
  for (const [id, at, text] of rows) await store.recordTurn(id, [{ role: 'owner', at, text }]);
  const written = stats(store.index);
  assert.deepEqual(written, stats(fresh([...store.entries.values()], PARIS)), 'as written');
  await store.flush(); await store.close();
  store = await open(root, { timeZone: PARIS, now });
  assert.equal(store.coverage, 'since 2025-12');
  await settle();
  assert.equal(store.coverage, 'all');
  assert.deepEqual(stats(store.index), written, 'synchronous plus background load: the same tokens as at write time');
  assert.deepEqual((await store.search('周五')).ranked.hits.map(h => h.id).sort(), ['t-late-0', 't-old-0', 't-old2-0'],
    'in Paris the 19th 23:30Z is Friday the 20th (and 2024-03-14 23:30Z Friday the 15th); the 18th 23:30Z is Thursday the 19th');
  // #dropFromWorkTree on a background-loaded entry and on a synchronously loaded one.
  await store.forget(['t-old-0', 't-day-0'], { reason: 'test', by: 'owner' });
  assert.deepEqual(stats(store.index), stats(fresh([...store.entries.values()], PARIS)), 'the forget removes what the loads added');
  // #finishForget: a forget that crashed between its record and its strip is finished at boot.
  store.faults = { beforeForgetStrip: () => { throw new Error('injected'); } };
  await assert.rejects(store.forget(['t-gone-0'], { reason: 'test', by: 'owner' }), /injected/);
  await store.close();
  store = await open(root, { timeZone: PARIS, now });
  await settle();
  assert.deepEqual([...store.entries.keys()].sort(), ['t-late-0', 't-old2-0']);
  assert.deepEqual(stats(store.index), stats(fresh([...store.entries.values()], PARIS)), 'the boot finish removed the same tokens');
  await store.close();
});

// ---------------------------------------------------------------- 2. groups

test('2: ranked groups by best rank, grep newest day first, the profile last; oldest first in a group; no entry that is not a hit', async () => {
  const root = newRoot(); const store = await open(root);
  await store.recordTurn('a', [{ role: 'owner', at: '2026-11-10T09:00:00Z', text: 'ledger alpha' },
    { role: 'steward', at: '2026-11-10T09:01:00Z', text: 'unrelated reply' }]);
  await store.recordTurn('b', [{ role: 'owner', at: '2026-11-10T15:00:00Z', text: 'ledger ledger ledger beta' }]);
  await store.recordTurn('c', [{ role: 'owner', at: '2026-11-12T10:00:00Z', text: 'ledger gamma' }]);
  await store.recordTurn('d', [{ role: 'owner', at: '2026-11-12T11:00:00Z', text: 'same day, no hit' }]);
  const p = await store.profile();
  await store.updateProfile('first line\nowner keeps a ledger\nthird\nledger in euros', p.version, 'test');
  const r = await store.search('ledger');
  assert.deepEqual(r.ranked.hits.map(h => h.id), ['t-b-0', 't-c-0', 't-a-0']);
  assert.equal(r.grep.text, [
    '[2026-11-12 周四 10:00+00:00] 业主 {t-c-0}: ledger gamma',
    '',
    '[2026-11-10 周二 09:00+00:00] 业主 {t-a-0}: ledger alpha',
    '[2026-11-10 周二 15:00+00:00] 业主 {t-b-0}: → 见 ranked',
    '',
    '[档案 第 2 行] owner keeps a ledger',
    '[档案 第 4 行] ledger in euros',
  ].join('\n'));
  assert.equal(r.ranked.text, [
    '[2026-11-10 周二 09:00+00:00] 业主 {t-a-0}: → 见 grep',
    '[2026-11-10 周二 15:00+00:00] 业主 {t-b-0}: ledger ledger ledger beta',
    '',
    '[2026-11-12 周四 10:00+00:00] 业主 {t-c-0}: → 见 grep',
  ].join('\n'));
  // hits keep each answer's own rank order, not the grouped order.
  assert.deepEqual(r.grep.hits.map(h => [h.id, h.line ?? h.at, h.shown]), [
    ['t-c-0', '2026-11-12T10:00:00Z', 'full'], ['t-b-0', '2026-11-10T15:00:00Z', 'see_ranked'], ['t-a-0', '2026-11-10T09:00:00Z', 'full'],
    ['profile', 2, 'full'], ['profile', 4, 'full']]);
  assert.ok(r.grep.hits.every(h => !('score' in h)), 'grep has no score');
  assert.ok(r.ranked.hits.every(h => typeof h.score === 'number' && h.ref === h.id));
  const all = r.grep.text + r.ranked.text;
  for (const absent of ['unrelated reply', 'same day, no hit', 'first line', 'third']) assert.ok(!all.includes(absent), absent);
  checkHits(r.grep); checkHits(r.ranked);
  assert.deepEqual([r.grep.omitted, r.ranked.omitted], [[], []]);
  assert.equal(r.budget, `${lineChars(r.grep.text) + lineChars(r.ranked.text)}/6000 字 · omitted 0`);
  await store.close();
});

// ---------------------------------------------------------------- 3. budget

test('3: one budget, placed in turn (grep 1, ranked 1, grep 2, ...): what does not fit is omitted, a shorter one after it still goes in', async () => {
  const root = newRoot(); const store = await open(root);
  // Literal "kiwi fig" (grep, newest first) and "fig, kiwi;" (ranked only, high in BM25).
  const g = n => 'kiwi fig ' + 'g'.repeat(n);
  const r = (reps) => 'fig, kiwi; '.repeat(reps);
  await store.recordTurn('g1', [{ role: 'owner', at: '2026-11-19T12:00:00Z', text: g(1880) }]);
  await store.recordTurn('g2', [{ role: 'owner', at: '2026-11-19T11:00:00Z', text: g(1880) }]);
  await store.recordTurn('g3', [{ role: 'owner', at: '2026-11-19T10:00:00Z', text: g(20) }]);
  await store.recordTurn('r1', [{ role: 'owner', at: '2026-11-01T12:00:00Z', text: r(171) }]);
  await store.recordTurn('r2', [{ role: 'owner', at: '2026-11-01T11:00:00Z', text: r(170) }]);
  await store.recordTurn('r3', [{ role: 'owner', at: '2026-11-01T10:00:00Z', text: r(4) }]);
  const s = await store.search('kiwi fig', 3);
  assert.deepEqual(s.grep.hits.map(h => [h.id, h.shown]), [['t-g1-0', 'full'], ['t-g2-0', 'full'], ['t-g3-0', 'full']]);
  assert.deepEqual(s.ranked.hits.map(h => [h.id, h.shown]), [['t-r1-0', 'full'], ['t-r2-0', 'omitted'], ['t-r3-0', 'full']]);
  assert.deepEqual(s.ranked.omitted, [{ id: 't-r2-0', at: '2026-11-01T11:00:00Z', length: r(170).length }]);
  const used = lineChars(s.grep.text) + lineChars(s.ranked.text);
  assert.ok(used <= SEARCH_CHARS, String(used));
  assert.equal(s.budget, `${used}/6000 字 · omitted 1`);
  // In turn: r2 was tried right after g2 and did not fit; g3 and r3, shorter, still went in.
  const line = id => [...s.grep.text.split('\n'), ...s.ranked.text.split('\n')].find(l => l.includes(`{${id}}`));
  const beforeR2 = line('t-g1-0').length + line('t-r1-0').length + line('t-g2-0').length;
  assert.ok(beforeR2 <= SEARCH_CHARS && beforeR2 + r(170).length > SEARCH_CHARS, String(beforeR2));
  checkHits(s.grep); checkHits(s.ranked);
  await store.close();
});

test('3: a 7400-character note at rank 1 is omitted whole, never cut; alone, text is empty and the budget says omitted 1', async () => {
  const root = newRoot(); const store = await open(root);
  const long = 'ZEBRA-NOTE ' + 'n'.repeat(7400 - 11);
  const note = await store.note(long);
  await store.recordTurn('s', [{ role: 'owner', at: '2026-11-01T09:00:00Z', text: 'a short zebra-note reply' }]);
  const both = await store.search('zebra-note');
  assert.deepEqual(both.grep.hits.map(h => [h.id, h.shown]), [[note.id, 'omitted'], ['t-s-0', 'full']]);
  assert.deepEqual(both.grep.omitted, [{ id: note.id, at: both.grep.hits[0].at, length: 7400 }]);
  assert.ok(!both.grep.text.includes('nnnn') && !both.ranked.text.includes('nnnn'), 'no piece of it');
  assert.equal(both.budget.split(' · ')[1], 'omitted 1', 'one entry omitted, though both answers list it');
  const alone = await store.search('n'.repeat(30)); // only the note holds it (and no index word is that)
  assert.equal(alone.grep.text, '');
  assert.equal(alone.budget, '0/6000 字 · omitted 1');
  assert.deepEqual(alone.grep.hits.map(h => h.shown), ['omitted']);
  const ids = [...new Set([...alone.grep.omitted, ...alone.ranked.omitted].map(o => o.id))];
  assert.deepEqual(ids, [note.id]);
  assert.equal((await store.read(ids)).entries[0].text, long, 'memory_read gives it whole');
  await store.close();
});

// ---------------------------------------------------------------- 4. duplicates and 4a hits

test('4 / 4a: an entry both answers found is whole once; hits[].id read back with memory_read are those entries', async () => {
  const root = newRoot(); const store = await open(root, { timeZone: PARIS });
  await store.recordTurn('t1', [{ role: 'owner', at: '2026-11-16T22:31:00.123456+00:00', text: '我想做个记账小工具，手机上能记就行' },
    { role: 'steward', at: '2026-11-16T22:33:00Z', text: '可以，先做最简单的记账' }]);
  await store.recordTurn('t2', [{ role: 'owner', at: '2026-11-03T09:10:00Z', text: 'expenses 那张表要能记账' }]);
  const r = await store.search('记账');
  const whole = '我想做个记账小工具，手机上能记就行';
  assert.equal((r.grep.text + r.ranked.text).split(whole).length - 1, 1, 'the whole text once');
  const both = r.ranked.hits.find(h => h.id === 't-t1-0');
  assert.ok(['see_grep', 'full'].includes(both.shown));
  assert.equal(r.grep.hits.find(h => h.id === 't-t1-0').shown, both.shown === 'see_grep' ? 'full' : 'see_ranked');
  assert.match(r.grep.text + '\n' + r.ranked.text, /\[2026-11-16 周一 23:31\+01:00\] 业主 \{t-t1-0\}: → 见 (grep|ranked)/);
  checkHits(r.grep); checkHits(r.ranked);
  const ids = [...new Set([...r.grep.hits, ...r.ranked.hits].map(h => h.id))];
  const read = await store.read(ids);
  assert.deepEqual(read.entries.map(e => e.id), ids);
  for (const e of read.entries) assert.ok((r.grep.text + r.ranked.text).includes(`{${e.id}}`));
  await store.close();
});

// ---------------------------------------------------------------- 5. forget

test('5: a forgotten entry is in neither answer, nor is its turn-mate or day-mate; restored, both answers find it again', async () => {
  const root = newRoot(); const store = await open(root);
  await store.recordTurn('t1', [{ role: 'owner', at: '2026-11-19T09:00:00Z', text: 'my ledger password hint' },
    { role: 'steward', at: '2026-11-19T09:01:00Z', text: 'noted the ledger hint' }]);
  await store.recordTurn('t2', [{ role: 'owner', at: '2026-11-19T10:00:00Z', text: 'ledger colours' }]);
  await store.forget(['t-t1-0'], { reason: 'owner asked', by: 'owner' });
  const r = await store.search('ledger');
  const all = JSON.stringify(r);
  assert.ok(!all.includes('password hint') && !all.includes('t-t1-0'), 'gone from text and hits');
  assert.ok(r.grep.hits.some(h => h.id === 't-t1-1') && r.ranked.hits.some(h => h.id === 't-t1-1'), 'its turn-mate is found on its own words');
  const day = await store.search('colours');
  assert.ok(!JSON.stringify(day).includes('password'), 'a hit on the same day brings no forgotten neighbour');
  await store.restore('t-t1-0');
  const back = await store.search('password hint');
  assert.equal(back.grep.hits[0].id, 't-t1-0');
  assert.equal(back.ranked.hits[0].id, 't-t1-0');
  await store.close();
});
