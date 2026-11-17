/** Plugin step 11 (design-steward 3.7): the memory store - BM25, grep, the git repository,
 *  the profile, forget/restore, the lock, permissions, and the tools. Purge: memory-purge.
 *  Remote index: memory-remote. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdtempSync, readdirSync, readFileSync, statSync, writeFileSync, mkdirSync, rmSync, unlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Bm25Index, documentTokens, queryTokens } from '../lib/memory/bm25.mjs';
import { MemoryStore, MemoryUnavailable, PROFILE_LIMIT, READ_CHARS, READ_IDS_MAX } from '../lib/memory/store.mjs';
import { MEMORY_TOOLS, profileSection, registerMemoryTools } from '../lib/memory/tools.mjs';
import { gitEnv } from '../lib/memory/git.mjs';

const USER = 'owner@example.com';
const newRoot = () => mkdtempSync(join(tmpdir(), 'dsh-memory-'));
const open = (root, extra = {}) => MemoryStore.open({ root, userId: USER, ...extra });
const repo = root => join(root, USER);
const gitIn = (dir, ...args) => spawnSync('git', ['-c', 'safe.directory=' + dir, ...args], { cwd: dir, env: gitEnv(dir), encoding: 'utf8' });
const subjects = root => gitIn(repo(root), 'log', '--format=%s').stdout.trim().split('\n');
const logText = root => readdirSync(join(repo(root), 'log')).filter(n => n.endsWith('.jsonl'))
  .map(n => readFileSync(join(repo(root), 'log', n), 'utf8')).join('');

test('BM25 tokens: words lower-cased, CJK bigrams plus unigrams; a one-character query asks the unigram', () => {
  const tokens = documentTokens('记账app，Ledger!');
  for (const t of ['记账', '\u0001记', '\u0001账', 'app', 'ledger']) assert.ok(tokens.includes(t), t);
  assert.ok(!tokens.some(t => /[，!]/.test(t)), 'punctuation is dropped');
  assert.deepEqual(queryTokens('账'), ['\u0001账']);
  assert.deepEqual(queryTokens('记账本'), ['记账', '账本']);
});

test('BM25 cuts a run at the script boundary: a Latin word glued to Chinese is one word, as when spaced off', () => {
  const glued = '用memory系统记住这条规则';
  const spaced = '用 memory 系统记住这条规则';
  const gluedTokens = documentTokens(glued);
  const spacedTokens = documentTokens(spaced);
  console.log('documentTokens(' + JSON.stringify(glued) + ') =', JSON.stringify(gluedTokens));
  console.log('documentTokens(' + JSON.stringify(spaced) + ') =', JSON.stringify(spacedTokens));
  const index = new Bm25Index();
  index.add('glued', glued, '2026-11-01');
  index.add('spaced', spaced, '2026-11-02');
  const ids = q => index.search(q, 10).map(h => h.id).sort();
  const hits = ids('memory');
  console.log('search("memory") =', JSON.stringify(hits));
  // The two spellings of one sentence are one token list, so the same query reaches both.
  assert.deepEqual(gluedTokens, spacedTokens);
  assert.ok(gluedTokens.includes('memory'), 'the glued Latin word is still a word');
  assert.deepEqual(hits, ['glued', 'spaced']);
});

test('BM25 keeps the kana prolonged sound mark inside the CJK segment: トー and ーク survive', () => {
  // The sample without the mark first: plain kana segmentation is what it was before the change
  // (identical at ed5237edba and 7967e64a74), so the mark is the only difference below.
  const plain = 'カタカナ';
  const plainTokens = documentTokens(plain);
  console.log('documentTokens(' + JSON.stringify(plain) + ') =', JSON.stringify(plainTokens));
  assert.deepEqual(plainTokens, ['\u0001カ', '\u0001タ', '\u0001カ', '\u0001ナ', 'カタ', 'タカ', 'カナ'],
    'without the mark: one CJK segment, every character a unigram, every adjacent pair a bigram');
  const sample = 'カタカナトーク';
  const tokens = documentTokens(sample);
  console.log('documentTokens(' + JSON.stringify(sample) + ') =', JSON.stringify(tokens));
  assert.deepEqual(tokens, [
    '\u0001カ', '\u0001タ', '\u0001カ', '\u0001ナ', '\u0001ト', '\u0001ー', '\u0001ク',
    'カタ', 'タカ', 'カナ', 'ナト', 'トー', 'ーク',
  ], 'one CJK segment: every character a unigram, every adjacent pair a bigram');
  assert.ok(!tokens.includes('ー'), 'the prolonged sound mark is not a free-standing word');
  const query = queryTokens('トーク');
  console.log('queryTokens("トーク") =', JSON.stringify(query));
  assert.deepEqual(query, ['トー', 'ーク']);
  const index = new Bm25Index();
  index.add('jp', sample, '2026-11-01');
  const hits = index.search('トーク', 10).map(h => h.id);
  console.log('search("トーク") =', JSON.stringify(hits));
  assert.deepEqual(hits, ['jp'], 'a query that crosses the mark reaches the record');
});

test('BM25 ranks mixed and pure-Chinese queries, newest first on a tie', () => {
  const index = new Bm25Index();
  index.add('a', '我想做个记账小工具', '2026-11-01');
  index.add('b', '今天天气不错', '2026-11-02');
  index.add('c', '记账本要支持多币种 multi currency', '2026-11-03');
  index.add('d', '我想做个记账小工具', '2026-11-04');
  const ids = q => index.search(q, 10).map(h => h.id);
  assert.deepEqual(ids('记账').slice(0, 2).sort(), ['a', 'd']);
  assert.ok(!ids('记账').includes('b'));
  assert.deepEqual(ids('记账').slice(0, 2), ['d', 'a'], 'equal scores: newest first');
  assert.deepEqual(ids('Currency'), ['c']);
  assert.deepEqual(ids('币').sort(), ['c']);
  assert.equal(ids('multi 记账')[0], 'c', 'the record matching both parts ranks first');
  index.remove('c', '记账本要支持多币种 multi currency');
  assert.deepEqual(ids('currency'), []);
});

test('a turn is on disk when recordTurn resolves; one commit per turn; a replay writes nothing', async () => {
  const root = newRoot(); const store = await open(root);
  const r = await store.recordTurn('turn-1', [{ role: 'owner', text: '我想做个记账小工具' }, { role: 'steward', text: '记下了' }]);
  assert.deepEqual(r, { written: 2 });
  assert.match(logText(root), /记账小工具/, 'the line is on disk before any commit is awaited');
  await store.flush();
  assert.equal(subjects(root)[0], 'turn turn-1 (runtime)');
  assert.deepEqual(await store.recordTurn('turn-1', [{ role: 'owner', text: '我想做个记账小工具' }, { role: 'steward', text: '记下了' }]), { written: 0 });
  await store.flush();
  assert.equal(subjects(root).length, 2, 'init + one turn: the replay made no commit');
  await store.close();
});

test('one search, both answers, each naming its source and marking the other', async () => {
  const root = newRoot(); const store = await open(root);
  await store.recordTurn('t1', [{ role: 'owner', text: 'I want a small Ledger app for 记账' }]);
  await store.recordTurn('t2', [{ role: 'owner', text: '天气很好' }]);
  const r = await store.search('ledger');
  assert.equal(r.grep.source, 'grep · 字面匹配 · 本地记忆日志');
  assert.equal(r.ranked.source, 'bm25 · 本地索引');
  assert.equal(r.grep.complete, true);
  assert.deepEqual(r.grep.hits.map(h => h.id), ['t-t1-0']);
  assert.equal(r.grep.hits[0].also_in_ranked, true);
  assert.equal(r.ranked.hits[0].ref, 't-t1-0');
  assert.equal(r.ranked.hits[0].also_in_grep, true);
  assert.equal(r.ranked.filtered_forgotten, 0, 'reported on every search, 0 included');
  assert.equal(r.ranked.coverage, 'all');
  assert.equal(r.status.git, 'ok');
  // A meaning-only query: grep finds nothing literal, the ranked side still answers.
  const loose = await store.search('small app');
  assert.deepEqual(loose.grep.hits, []);
  assert.equal(loose.ranked.hits[0].ref, 't-t1-0');
  await store.close();
});

test('memory_read: the whole text a search only excerpted, the profile, and the rest of the turn on request', async () => {
  const root = newRoot(); const store = await open(root);
  const long = '开头' + 'x'.repeat(400) + ' LEDGER_MARK ' + 'y'.repeat(400) + '结尾';
  await store.recordTurn('t1', [{ role: 'owner', text: long }, { role: 'steward', text: '好的，先做记账' }, { role: 'owner', text: '再加导出' }]);
  await store.recordTurn('t2', [{ role: 'owner', text: '别的事' }]);
  const p = await store.profile();
  await store.updateProfile('喜欢简洁', p.version, 'test');
  const hit = (await store.search('LEDGER_MARK')).grep.hits[0];
  assert.ok(hit.excerpt.length < long.length && !hit.excerpt.includes('结尾'), 'search gives an excerpt only');
  const r = await store.read([hit.id]);
  assert.deepEqual(r.entries.map(e => [e.id, e.role, e.source, e.turn, e.text]), [['t-t1-0', 'owner', 'conversation', 't1', long]]);
  assert.equal(r.omitted, undefined); assert.equal(r.status.git, 'ok');
  // with_turn: the rest of that turn, in order, marked context; another turn never joins.
  const t = await store.read(['t-t1-1'], { withTurn: true });
  assert.deepEqual(t.entries.map(e => [e.id, e.context ?? false]), [['t-t1-1', false], ['t-t1-0', true], ['t-t1-2', true]]);
  // An id asked for that already came as context is not repeated.
  assert.deepEqual((await store.read(['t-t1-0', 't-t1-2'], { withTurn: true })).entries.map(e => e.id), ['t-t1-0', 't-t1-1', 't-t1-2']);
  const prof = await store.read(['profile']);
  assert.deepEqual(prof.entries.map(e => [e.id, e.text, e.version]), [['profile', '喜欢简洁', (await store.profile()).version]]);
  // Forgotten: named, no text; unknown: not_found; restored: readable again.
  await store.forget(['t-t1-2'], { reason: 'asked', by: 'owner' });
  const f = await store.read(['t-t1-2', 'no-such-id', 't-t2-0']);
  assert.deepEqual(f.entries.map(e => e.id), ['t-t2-0']);
  assert.deepEqual(f.forgotten, ['t-t1-2']); assert.deepEqual(f.not_found, ['no-such-id']);
  assert.ok(!JSON.stringify(f).includes('再加导出'));
  assert.ok(!JSON.stringify(await store.read(['t-t1-0'], { withTurn: true })).includes('再加导出'), 'a forgotten message never rides along as context');
  await store.restore('t-t1-2');
  assert.equal((await store.read(['t-t1-2'])).entries[0].text, '再加导出');
  await store.close();
});

test('memory_read limits: ids capped; texts past the character budget are omitted, one long entry comes in pieces', async () => {
  const root = newRoot(); const store = await open(root);
  assert.deepEqual(await store.read([]), { refused: 'ids_required' });
  assert.deepEqual(await store.read(Array.from({ length: READ_IDS_MAX + 1 }, (_, i) => 'id' + i)), { refused: 'too_many_ids', limit: READ_IDS_MAX });
  const third = Math.floor(READ_CHARS / 3) + 1;
  await store.recordTurn('big', [0, 1, 2].map(i => ({ role: 'owner', text: String(i).repeat(third) })));
  const r = await store.read(['t-big-0', 't-big-1', 't-big-2']);
  assert.deepEqual(r.entries.map(e => e.id), ['t-big-0', 't-big-1']);
  assert.ok(r.entries.every(e => e.text.length === third), 'what fits is whole');
  assert.deepEqual(r.omitted, [{ id: 't-big-2', length: third }]);
  const huge = 'abc'.repeat(Math.ceil((READ_CHARS * 2 + 10) / 3));
  await store.recordTurn('huge', [{ role: 'owner', text: huge }]);
  let offset = 0, joined = '', pieces = 0;
  for (;;) {
    const piece = await store.read(['t-huge-0'], { offset });
    assert.ok(piece.entries[0].text.length <= READ_CHARS);
    joined += piece.entries[0].text; pieces++;
    if (!piece.entries[0].cut) break;
    assert.equal(piece.entries[0].cut.length, huge.length);
    offset = piece.entries[0].cut.next_offset;
  }
  assert.equal(joined, huge, 'the pieces put back together are the entry'); assert.equal(pieces, 3);
  assert.equal((await store.read(['t-huge-0', 't-big-0'], { offset: 5 })).refused, 'offset_needs_one_id');
  assert.equal((await store.read(['t-huge-0'], { offset: 5, withTurn: true })).refused, 'offset_needs_one_id');
  assert.deepEqual(await store.read(['t-huge-0'], { offset: huge.length }), { refused: 'offset_past_end', length: huge.length });
  assert.deepEqual(await store.read(['t-huge-0'], { offset: -1 }), { refused: 'bad_offset' });
  await store.close();
});

test('grep is literal and case-insensitive, quotes included, and sees the profile', async () => {
  const root = newRoot(); const store = await open(root);
  await store.recordTurn('t1', [{ role: 'owner', text: 'She said "ship it" on Friday' }, { role: 'steward', text: '记账' }]);
  const p = await store.profile();
  await store.updateProfile('Owner prefers Friday releases', p.version, 'test');
  assert.deepEqual((await store.search('"SHIP IT"')).grep.hits.map(h => h.id), ['t-t1-0']);
  assert.deepEqual((await store.search('记账')).grep.hits.map(h => h.id), ['t-t1-1']);
  assert.deepEqual((await store.search('friday')).grep.hits.map(h => h.id).sort(), ['profile', 't-t1-0']);
  assert.deepEqual((await store.search('role')).grep.hits, [], 'JSON keys are not matches');
  await store.close();
});

test('a grep past its time limit is killed and reported incomplete', async () => {
  const root = newRoot(); const store = await open(root, { grepTimeoutMs: 1 });
  await store.recordTurn('big', Array.from({ length: 3000 }, (_, i) => ({ role: 'owner', text: 'needle line ' + i })));
  const r = await store.search('needle');
  assert.equal(r.grep.complete, false);
  assert.match(r.grep.reason, /killed/);
  assert.throws(() => process.kill(store.lastGrepPid, 0), { code: 'ESRCH' }, 'the git grep child is gone');
  await store.close();
});

test('profile: limit refused (never truncated), stale version refused with the current text, on-disk edits are the owner\'s', async () => {
  const root = newRoot(); const store = await open(root);
  const p0 = await store.profile();
  const long = await store.updateProfile('x'.repeat(PROFILE_LIMIT + 1), p0.version, 'too long');
  assert.equal(long.refused, 'profile_too_long');
  assert.equal(readFileSync(join(repo(root), 'profile.md'), 'utf8'), '');
  const ok = await store.updateProfile('业主：喜欢简洁', p0.version, 'first facts');
  assert.equal(ok.updated, true);
  assert.notEqual(ok.version, p0.version);
  const stale = await store.updateProfile('覆盖', p0.version, 'stale');
  assert.equal(stale.refused, 'version_conflict');
  assert.equal(stale.current.text, '业主：喜欢简洁');
  writeFileSync(join(repo(root), 'profile.md'), '业主亲手改的');
  const p2 = await store.profile();
  assert.equal(p2.text, '业主亲手改的');
  assert.equal(subjects(root)[0], 'profile (owner): edited on disk');
  assert.equal((await store.updateProfile('管家改', ok.version, 'stale after owner edit')).refused, 'version_conflict');
  assert.match(await profileSection({ store }), new RegExp(`version ${p2.version}[\\s\\S]*业主亲手改的`));
  await store.close();
});

test('a failing commit only degrades: the write succeeds, status says why, the next commit takes the backlog', async () => {
  const root = newRoot(); const store = await open(root);
  writeFileSync(join(repo(root), '.git', 'index.lock'), '');
  assert.deepEqual(await store.recordTurn('t1', [{ role: 'owner', text: 'first while locked' }]), { written: 1 });
  await store.flush();
  const status = (await store.search('first')).status.git;
  assert.match(status, /^commit failing since .*index\.lock/);
  assert.equal((await store.search('first')).grep.hits.length, 1, 'searchable while degraded');
  rmSync(join(repo(root), '.git', 'index.lock'));
  await store.recordTurn('t2', [{ role: 'owner', text: 'second' }]);
  await store.flush();
  assert.equal((await store.search('second')).status.git, 'ok');
  assert.equal(gitIn(repo(root), 'status', '--porcelain').stdout, '', 'nothing left uncommitted');
  assert.match(gitIn(repo(root), 'show', 'HEAD').stdout, /first while locked/, 'the backlog went into the next commit');
  await store.close();
});

test('uncommitted changes found at boot are committed as a recover commit, complete', async () => {
  const root = newRoot(); let store = await open(root);
  await store.recordTurn('t1', [{ role: 'owner', text: 'before crash' }]); await store.flush(); await store.close();
  appendFileSync(join(repo(root), 'log', readdirSync(join(repo(root), 'log')).find(n => n.endsWith('.jsonl'))),
    JSON.stringify({ id: 't-t2-0', at: new Date().toISOString(), role: 'owner', source: 'conversation', text: 'written, not committed', batch: 't2' }) + '\n');
  store = await open(root);
  assert.equal(subjects(root)[0], 'recover: uncommitted changes at boot');
  assert.equal((await store.search('not committed')).grep.hits[0].id, 't-t2-0');
  await store.close();
});

test('git runs with an allowlisted environment: a broken GIT_CONFIG_COUNT in the host does not reach it', async () => {
  const saved = { ...process.env };
  try {
    Object.assign(process.env, { GIT_CONFIG_COUNT: '2', GIT_CONFIG_KEY_0: 'user.name', GIT_CONFIG_VALUE_0: 'x' });
    const control = spawnSync('git', ['--version', '--no-build-options'], { encoding: 'utf8' });
    const broken = spawnSync('git', ['config', '--list'], { encoding: 'utf8' });
    assert.notEqual(broken.status, 0, 'control: plain git with this environment fails (' + control.stdout.trim() + ')');
    const root = newRoot(); const store = await open(root);
    await store.recordTurn('t1', [{ role: 'owner', text: 'commits anyway' }]); await store.flush();
    assert.equal((await store.search('commits')).status.git, 'ok');
    await store.close();
  } finally {
    for (const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key];
  }
});

test('permissions: directories 0700, every file 0600 or stricter; logs never carry memory text', async () => {
  const root = newRoot();
  const lines = [];
  const store = await open(root, { logger: (event, fields) => lines.push(event + ' ' + JSON.stringify(fields)) });
  const marker = 'UNIQUE-MARKER-7f3a';
  await store.recordTurn('t1', [{ role: 'owner', text: 'secret ' + marker }]);
  await store.note('note ' + marker);
  await store.forget(['t-t1-0'], { reason: 'r', by: 'owner' });
  await store.restore('t-t1-0');
  await store.flush();
  const walk = dir => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name), st = statSync(path);
      if (st.isDirectory()) { assert.equal(st.mode & 0o777, 0o700, path); walk(path); }
      else assert.equal(st.mode & 0o077, 0, `${path} is ${(st.mode & 0o777).toString(8)}`);
    }
  };
  assert.equal(statSync(root).mode & 0o777, 0o700);
  walk(root);
  assert.ok(lines.length > 3);
  assert.ok(!lines.some(l => l.includes(marker)), 'no log line carries the text');
  await store.close();
});

test('forget: gone from grep and BM25, a replay does not bring it back, restore does', async () => {
  const root = newRoot(); const store = await open(root);
  await store.recordTurn('t1', [{ role: 'owner', text: '我的银行是招商银行' }, { role: 'steward', text: '好' }]);
  assert.equal((await store.forget(['t-t1-0'], { by: 'owner' })).refused, 'reason_required');
  const f = await store.forget(['t-t1-0', 'nope'], { reason: 'owner asked', by: 'owner' });
  assert.deepEqual([f.forgotten, f.unknown], [['t-t1-0'], ['nope']]);
  assert.equal(f.remote_forget, 'local');
  const after = await store.search('招商银行');
  assert.deepEqual([after.grep.hits, after.ranked.hits], [[], []]);
  assert.doesNotMatch(logText(root), /招商银行/);
  assert.deepEqual((await store.recordTurn('t1', [{ role: 'owner', text: '我的银行是招商银行' }])), { written: 0 }, 'a forgotten id is not written again');
  assert.deepEqual(store.forgottenList().map(r => r.id), ['t-t1-0']);
  assert.deepEqual((await store.forget(['t-t1-0'], { reason: 'again', by: 'owner' })).already, ['t-t1-0']);
  assert.deepEqual(await store.restore('t-t1-0').then(r => r.restored), 't-t1-0');
  const back = await store.search('招商银行');
  assert.equal(back.grep.hits[0].id, 't-t1-0');
  assert.equal(back.ranked.hits[0].ref, 't-t1-0');
  assert.deepEqual(store.forgottenList(), []);
  await store.close();
  const reopened = await open(root);
  assert.equal((await reopened.search('招商银行')).grep.hits.length, 1, 'restore survives a restart');
  await reopened.close();
});

test('the lock: a second process is refused while the first holds it; a dead holder\'s lock is taken over', async () => {
  const root = newRoot(); const store = await open(root);
  const other = spawnSync(process.execPath, ['--input-type=module', '-e',
    `import { MemoryStore } from ${JSON.stringify(new URL('../lib/memory/store.mjs', import.meta.url).href)};
     MemoryStore.open({ root: ${JSON.stringify(root)}, userId: ${JSON.stringify(USER)} }).then(() => process.exit(0), e => { console.log(e.name + ': ' + e.message); process.exit(3); });`], { encoding: 'utf8' });
  assert.equal(other.status, 3);
  assert.match(other.stdout, new RegExp(`MemoryUnavailable: memory: locked by process ${process.pid}`));
  await store.close();
  const dead = spawnSync(process.execPath, ['-e', 'console.log(process.pid)'], { encoding: 'utf8' }).stdout.trim();
  writeFileSync(join(root, USER + '.lock'), dead);
  const again = await open(root);
  assert.equal(readFileSync(join(root, USER + '.lock'), 'utf8'), String(process.pid));
  await again.close();
  assert.ok(!existsSync(join(root, USER + '.lock')));
});

test('older months load in the background; coverage says so until they are in', async () => {
  const root = newRoot();
  let store = await open(root);
  await store.recordTurn('old', [{ role: 'owner', at: '2023-02-03T00:00:00Z', text: 'ancient ledger idea' }]);
  await store.recordTurn('new', [{ role: 'owner', at: '2026-11-03T00:00:00Z', text: 'recent ledger idea' }]);
  await store.flush(); await store.close();
  store = await open(root, { now: () => '2026-11-17T00:00:00Z' });
  const first = await store.search('ledger');
  assert.equal(first.ranked.coverage, 'since 2025-12');
  assert.equal(first.grep.hits.length, 2, 'grep reads the whole log at once');
  await new Promise(resolve => setImmediate(() => setImmediate(resolve)));
  const later = await store.search('ledger');
  assert.equal(later.ranked.coverage, 'all');
  assert.equal(later.ranked.hits.length, 2);
  await store.close();
});

test('the tools: exactly five, no purge; forget only on an owner message; an unavailable memory answers its error', async () => {
  const root = newRoot(); const store = await open(root);
  await store.recordTurn('t1', [{ role: 'owner', text: 'forget me' }]);
  const tools = new Map();
  const scope = { tools: { register: t => tools.set(t.name, t) } };
  const defineTool = t => t;
  registerMemoryTools(scope, { defineTool, jsonOutput: {}, memory: { store }, ownerMessage: async id => id === 'owner-msg-1' });
  assert.deepEqual([...tools.keys()], MEMORY_TOOLS);
  assert.ok(![...tools.keys()].some(n => /purge/.test(n)));
  const exec = { signal: AbortSignal.timeout(5000) };
  const forget = args => tools.get('memory_forget').execute({ ids: ['t-t1-0'], reason: 'asked', ...args }, exec);
  assert.equal((await forget({ owner_message_id: 'steward-msg' })).refused, 'not_an_owner_message');
  assert.equal((await forget({ owner_message_id: '' })).refused, 'not_an_owner_message');
  assert.deepEqual((await forget({ owner_message_id: 'owner-msg-1' })).forgotten, ['t-t1-0']);
  const noChannel = new Map();
  registerMemoryTools({ tools: { register: t => noChannel.set(t.name, t) } }, { defineTool, jsonOutput: {}, memory: { store } });
  assert.equal((await noChannel.get('memory_forget').execute({ ids: ['x'], reason: 'r', owner_message_id: 'owner-msg-1' }, exec)).refused, 'no_owner_channel');
  const note = await tools.get('memory_note').execute({ text: 'a note' }, exec);
  assert.match(note.id, /^n-/);
  assert.equal((await tools.get('memory_search').execute({ query: 'a note' }, exec)).grep.hits[0].id, note.id);
  const read = await tools.get('memory_read').execute({ ids: [note.id] }, exec);
  assert.deepEqual(read.entries.map(e => [e.id, e.text, e.source]), [[note.id, 'a note', 'note']]);
  assert.equal(read.entries[0].turn, undefined, 'a note is its own turn');
  await store.recordTurn('t2', [{ role: 'owner', text: 'q' }, { role: 'steward', text: 'a' }]);
  assert.equal((await tools.get('memory_read').execute({ ids: ['t-t2-0'], with_turn: 'yes' }, exec)).entries.length, 1, 'only a real true asks for the turn');
  assert.equal((await tools.get('memory_read').execute({ ids: ['t-t2-0'], with_turn: true }, exec)).entries.length, 2);
  const broken = new Map();
  registerMemoryTools({ tools: { register: t => broken.set(t.name, t) } }, { defineTool, jsonOutput: {}, memory: { error: 'memory: locked by process 1' } });
  for (const [name, tool] of broken) assert.deepEqual(await tool.execute({ query: 'x', text: 'x', ids: ['x'], reason: 'r', owner_message_id: 'o', expected_version: 'v' }, exec),
    { refused: 'memory_unavailable', reason: 'memory: locked by process 1' }, name);
  await store.close();
});

test('a repository directory that vanished while a pre-purge copy exists is never re-initialised empty', async () => {
  const root = newRoot(); const store = await open(root); await store.close();
  mkdirSync(join(root, USER + '.pre-purge-20260101000000000'));
  rmSync(repo(root), { recursive: true });
  await assert.rejects(open(root), MemoryUnavailable);
  assert.ok(!existsSync(repo(root)));
});

// ---- step 11 fixes (claude-dev-mcp), stacked on the purge-durability branch

test('f6: a live takeover between our judgement and our rename is put back, and we are refused', async () => {
  const root = newRoot();
  const sleeper = spawn('sleep', ['30']);
  try {
    const dead = spawnSync(process.execPath, ['-e', 'console.log(process.pid)'], { encoding: 'utf8' }).stdout.trim();
    mkdirSync(root, { recursive: true });
    const lock = join(root, USER + '.lock');
    writeFileSync(lock, dead);
    // Another process takes the stale lock over after we judged it stale and before we move it.
    const faults = { beforeLockTakeover: () => { unlinkSync(lock); writeFileSync(lock, String(sleeper.pid)); } };
    await assert.rejects(open(root, { faults }), new RegExp(`locked by process ${sleeper.pid}`));
    assert.equal(readFileSync(lock, 'utf8'), String(sleeper.pid), 'the live holder still has its lock');
    assert.deepEqual(readdirSync(root).filter(n => n.includes('.stale-')), []);
  } finally { sleeper.kill(); }
});

test('f6: the put-back never replaces a lock a third process made meanwhile', async () => {
  const root = newRoot();
  const second = spawn('sleep', ['30']), third = spawn('sleep', ['30']);
  try {
    const dead = spawnSync(process.execPath, ['-e', 'console.log(process.pid)'], { encoding: 'utf8' }).stdout.trim();
    mkdirSync(root, { recursive: true });
    const lock = join(root, USER + '.lock');
    writeFileSync(lock, dead);
    // A second process takes the stale lock over; after we moved it aside, a third makes its own.
    const faults = { beforeLockTakeover: () => { unlinkSync(lock); writeFileSync(lock, String(second.pid)); },
      afterLockAside: () => writeFileSync(lock, String(third.pid)) };
    await assert.rejects(open(root, { faults }), new RegExp(`locked by process ${second.pid}`));
    assert.equal(readFileSync(lock, 'utf8'), String(third.pid), 'the third process\'s lock was not replaced');
    assert.deepEqual(readdirSync(root).filter(n => n.includes('.stale-')), []);
  } finally { second.kill(); third.kill(); }
});

test('f6: close() leaves alone a lock that no longer names this process', async () => {
  const root = newRoot(); const store = await open(root);
  const sleeper = spawn('sleep', ['30']);
  try {
    writeFileSync(join(root, USER + '.lock'), String(sleeper.pid));
    await store.close();
    assert.equal(readFileSync(join(root, USER + '.lock'), 'utf8'), String(sleeper.pid));
  } finally { sleeper.kill(); }
});

test('write-f1: a profile change committed late keeps its author, and the version handed out still works', async () => {
  const root = newRoot(); const store = await open(root);
  const p = await store.profile();
  writeFileSync(join(repo(root), '.git', 'index.lock'), '');
  const r = await store.updateProfile('written while commits fail', p.version, 'learned a preference');
  assert.equal(r.updated, true);
  assert.match(r.version, /^uncommitted:/);
  rmSync(join(repo(root), '.git', 'index.lock'));
  await store.recordTurn('t1', [{ role: 'owner', text: 'any later write' }]);
  await store.flush();
  const log = subjects(root);
  assert.ok(log.includes('profile (steward): learned a preference'), log.join(' | '));
  assert.ok(!log.includes('profile (owner): edited on disk'), 'the steward\'s write is not blamed on the owner');
  assert.equal((await store.updateProfile('next', r.version, 'continue from the version I was given')).updated, true);
  // A real edit on disk is still the owner's.
  writeFileSync(join(repo(root), 'profile.md'), 'the owner typed this');
  await store.profile();
  assert.equal(subjects(root)[0], 'profile (owner): edited on disk');
  await store.close();
});

test('iss-a81f: a crossed budget line is logged once and stays on status.over_budget', async () => {
  const root = newRoot();
  const lines = [];
  let store = await open(root, { budget: { writeP95Ms: -1 }, logger: (event, fields) => lines.push([event, fields]) });
  // 40 writes: the p95 is evaluated from the 20th on, so 21 evaluations - "once" means once.
  for (let i = 0; i < 40; i++) { await store.recordTurn('t' + i, [{ role: 'owner', text: 'entry ' + i }]); await store.flush(); }
  assert.ok(store.statusLine().over_budget?.some(l => /^write p95 \d+ ms > -1 ms/.test(l)), JSON.stringify(store.statusLine()));
  assert.equal(lines.filter(([e, f]) => e === 'memory/over-budget' && f.kind === 'write_p95').length, 1, 'logged once, not per write');
  await store.close();
  store = await open(root, { budget: { entries: 5 }, logger: (event, fields) => lines.push([event, fields]) });
  assert.ok((await store.search('entry')).status.over_budget?.some(l => /^40 entries > 5/.test(l)));
  await store.close();
  store = await open(root);
  assert.equal(store.statusLine().over_budget, undefined, 'within the real budget nothing is reported');
  await store.close();
});
