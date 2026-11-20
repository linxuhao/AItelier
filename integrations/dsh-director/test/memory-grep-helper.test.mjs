/** Design 3.8.2's 20 ms on the search path: a `git grep` is forked by a resident helper process
 *  (lib/memory/grep-helper.mjs, lib/memory/grep-child.mjs) instead of by the store's own
 *  process, where fork+exec costs ~24 ms per GiB of RSS. What each arm proves:
 *
 *  1  the helper runs the direct spawn's command - same argv, cwd, environment and umask
 *     wrapper - so what matches is unchanged: a profile.md line, a -F literal, -i's case, -I's
 *     binary skip, -z/-n's fields, --untracked's files; and the fork is the helper's, not this
 *     process's. On a seeded random corpus (ASCII, non-ASCII, mixed case, regex metacharacters)
 *     the helper's git output is the direct spawn's byte for byte (1d).
 *  2  the grep timeout is the request's: complete: false with the partial hits and the same
 *     reason, the helper survives it and serves the next search.
 *  3  a helper that cannot start, dies mid-request or never answers falls back to the direct
 *     spawn, logged as memory/grep-spawn-fallback; no search fails and no answer changes. A
 *     replaced helper's late exit is its own and does not take down its successor (3b).
 *     A helper that dies while its git runs takes that git with it (3c), and one that keeps
 *     failing is restarted once, then only after a backoff, doubling to a cap (3d).
 *  4  close() takes the helper with it, and a store whose process simply exits, or is SIGKILLed
 *     mid-grep, leaves no orphan: neither the helper nor its git child (4b).
 *  5  an idle helper sleeps: it uses no CPU between searches and still answers afterwards.
 *  6  close() during a search forks nothing more; 7  a direct spawn that cannot start answers
 *     an incomplete grep, never a thrown search; 8  the helper gets PATH alone, not the host's
 *     environment (git gets the spec's own either way).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { closeSync, constants, mkdtempSync, openSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { MemoryStore } from '../lib/memory/store.mjs';
import { git, gitEnv } from '../lib/memory/git.mjs';
import { GrepHelper } from '../lib/memory/grep-helper.mjs';

const USER = 'owner@example.com';
const newRoot = () => mkdtempSync(join(tmpdir(), 'dsh-memgrep-'));
const open = (root, extra = {}) => MemoryStore.open({ root, userId: USER, ...extra });
const repo = root => join(root, USER);
const gitIn = (dir, ...args) => spawnSync('git', ['-c', 'safe.directory=' + dir, '-c', 'core.quotePath=false', ...args],
  { cwd: dir, env: gitEnv(dir), encoding: 'utf8', maxBuffer: 1 << 28 });
// A broken helper on purpose: 'die' answers with a streamed record and a torn one, then exits
// mid-request; 'silent' holds the request and answers nothing.
const FAULTY = fileURLToPath(new URL('./fixtures/grep-helper-faulty.mjs', import.meta.url));
const FALLBACK = 'memory/grep-spawn-fallback';
const CHILD = fileURLToPath(new URL('../lib/memory/grep-child.mjs', import.meta.url));

/** grep's hits the way the direct spawn gives them: the argv is written out here, not taken from
 *  the code under test, so a flag the helper dropped shows up as a different answer. */
function directHits(dir, query, k) {
  const needle = JSON.stringify(query).slice(1, -1);
  const out = gitIn(dir, 'grep', '-F', '-i', '-I', '-n', '-z', '--no-color', '--untracked', '-e', needle, '--', 'log', 'profile.md').stdout;
  const items = [], lower = query.toLowerCase();
  for (const record of out.split('\n')) {
    const [file, line, ...rest] = record.split('\0');
    if (!file || rest.length === 0) continue;
    const content = rest.join('\0');
    if (file === 'profile.md') { items.push({ at: '', hit: { id: 'profile', line: Number(line) } }); continue; }
    let e;
    try { e = JSON.parse(content); } catch { continue; }
    if (!e?.text || !e.text.toLowerCase().includes(lower)) continue;
    items.push({ at: e.at, hit: { id: e.id, at: e.at, role: e.role } });
  }
  items.sort((a, b) => (b.at > a.at ? 1 : b.at < a.at ? -1 : 0));
  return items.slice(0, k);
}
const hitsOf = answer => answer.grep.hits.map(h => [h.id, h.line]);
const reference = (dir, query, k) => directHits(dir, query, k).map(i => [i.hit.id, i.hit.line]);

/** The parent of a process, from /proc (the arm that proves WHERE the fork happened needs it,
 *  and it must be read while the child is alive - inside onSpawn). */
function parentOf(pid) {
  const stat = readFileSync(`/proc/${pid}/stat`, 'utf8');
  return Number(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[1]);
}

/** A process's own CPU (utime+stime) in ms: /proc is the only place node leaves it. */
function cpuMs(pid) {
  const stat = readFileSync(`/proc/${pid}/stat`, 'utf8');
  const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
  return (Number(fields[11]) + Number(fields[12])) * 10; // USER_HZ is 100 on every Linux we run on
}

const alive = pid => { try { process.kill(pid, 0); return true; } catch (error) { return error.code === 'EPERM'; } };

/** Whether a process is still running, from /proc: a re-parented helper that has exited is a
 *  zombie until someone reaps it, and in a container nothing does (PID 1 is the test runner),
 *  while kill(pid, 0) still succeeds for a zombie. A zombie is not a process still at work. */
function running(pid) {
  try {
    const stat = readFileSync(`/proc/${pid}/stat`, 'utf8');
    return stat.slice(stat.lastIndexOf(')') + 2)[0] !== 'Z';
  } catch { return false; }
}

test('1: the helper greps the direct spawn\'s command: a profile.md line, -F literal, -i case, -z -n fields', async () => {
  const root = newRoot();
  const events = [];
  const store = await open(root, { logger: (event, fields) => events.push([event, fields]) });
  await store.recordTurn('t1', [{ role: 'owner', text: 'She said "ship it" on Friday' }, { role: 'steward', text: '记账 完成' }]);
  const p = await store.profile();
  await store.updateProfile('- Friday releases only\n- nothing else', p.version, 'test', 'owner');
  const dir = repo(root);
  const helperPid = store.grepHelper.pid;
  assert.ok(Number.isInteger(helperPid), 'the helper is started at open, not on the first search');
  for (const [query, k] of [['friday', 10], ['FRIDAY', 10], ['"SHIP IT"', 5], ['SHIP.*IT', 5], ['记账', 5], ['else', 5], ['nothing', 2]]) {
    const r = await store.search(query, k);
    assert.equal(r.grep.complete, true, query);
    assert.deepEqual(hitsOf(r), reference(dir, query, k), `the helper's hits are the direct spawn's: ${query}`);
  }
  // What each flag does, so the equality above is not two implementations agreeing on nothing:
  assert.equal(gitIn(dir, 'grep', '-F', '-n', '-e', 'friday', '--', 'log', 'profile.md').status, 1, '-F without -i finds no Friday');
  assert.equal(gitIn(dir, 'grep', '-n', '-e', 'ship.*it', '--', 'log', 'profile.md').status, 0, 'as a regex, ship.*it matches');
  assert.deepEqual((await store.search('else')).grep.hits.map(h => [h.id, h.line]), [['profile', 2]],
    '-z separates file, line and content and -n numbers the profile line');
  assert.equal(store.grepHelper.pid, helperPid, 'one helper served every search');
  assert.deepEqual(events.filter(([event]) => event === FALLBACK), [], 'none of it was a fallback');
  await store.close();
  assert.equal(alive(helperPid), false, 'and it went with the store');
});

test('1b: the grep child is forked by the helper, not by this process', async () => {
  const root = newRoot();
  const store = await open(root);
  await store.recordTurn('big', Array.from({ length: 2000 }, (_, i) => ({ role: 'owner', text: 'needle line ' + i })));
  const args = ['grep', '-F', '-i', '-I', '-n', '-z', '--no-color', '--untracked', '-e', 'needle', '--', 'log', 'profile.md'];
  let throughHelper, directly;
  const viaHelper = await store.grepHelper.grep(store.dir, args, { timeoutMs: 5000, onSpawn: pid => { throughHelper = parentOf(pid); } });
  assert.equal(viaHelper.code, 0);
  assert.equal(viaHelper.fallback, undefined);
  const viaSpawn = await git(store.dir, args, { timeoutMs: 5000, onSpawn: pid => { directly = parentOf(pid); } });
  assert.equal(viaSpawn.code, 0);
  assert.equal(throughHelper, store.grepHelper.pid, 'the helper forked it');
  assert.equal(directly, process.pid, 'the direct spawn forks it here - which is what the helper spares this process');
  assert.notEqual(store.grepHelper.pid, process.pid);
  await store.close();
});

test('1c: -I skips a binary log file and --untracked finds a file git does not track - the helper matches', async () => {
  const root = newRoot();
  const events = [];
  const store = await open(root, { logger: (event, fields) => events.push([event, fields]) });
  await store.recordTurn('t1', [{ role: 'owner', text: 'needle in a log line' }]);
  await store.flush();
  const dir = repo(root);
  // A log file git reads as binary (a NUL byte in it). The matching text line really is there -
  // -a finds it - so the search's -I is what leaves it out: no hit, and no record made of a
  // "Binary file ... matches" line either.
  writeFileSync(join(dir, 'log', '2026-12.jsonl'), Buffer.concat([
    Buffer.from(JSON.stringify({ id: 't-bin-0', at: '2026-12-01T00:00:00Z', role: 'owner', text: 'needle in a binary file' }) + '\n'),
    Buffer.from([0x00, 0x01, 0x02, 0x00]),
  ]));
  assert.match(gitIn(dir, 'grep', '-F', '-i', '--untracked', '-n', '-e', 'needle', '--', 'log').stdout, /Binary file/,
    'without -I git prints no matching line for it');
  assert.match(gitIn(dir, 'grep', '-F', '-i', '--untracked', '-a', '-n', '-e', 'needle', '--', 'log').stdout, /t-bin-0/,
    'the line is there: -a finds it, so -I is what skips it');
  const binary = await store.search('needle');
  assert.equal(binary.grep.complete, true);
  assert.deepEqual(binary.grep.hits.map(h => h.id), ['t-t1-0'], 'the binary file contributed nothing');
  assert.deepEqual(hitsOf(binary), reference(dir, 'needle', 10));
  // --untracked: a log file written but not committed is searched; without the flag it is not.
  writeFileSync(join(dir, 'log', '2099-01.jsonl'),
    JSON.stringify({ id: 't-2099-0', at: '2099-01-01T00:00:00Z', role: 'owner', text: 'an untracked needle line' }) + '\n');
  assert.match(gitIn(dir, 'status', '--porcelain').stdout, /2099-01/, 'the file is untracked');
  assert.equal(gitIn(dir, 'grep', '-F', '-i', '-n', '-e', 'an untracked needle', '--', 'log').stdout.trim(), '',
    'git does not search an untracked file without --untracked');
  const untracked = await store.search('an untracked needle');
  assert.deepEqual(untracked.grep.hits.map(h => h.id), ['t-2099-0']);
  assert.deepEqual(hitsOf(untracked), reference(dir, 'an untracked needle', 10));
  assert.deepEqual(events.filter(([event]) => event === FALLBACK), []);
  await store.close();
});

test('2: the grep timeout is the request\'s: complete: false with the partial hits, and the helper serves the next search', async () => {
  const root = newRoot();
  const events = [];
  const store = await open(root, { logger: (event, fields) => events.push([event, fields]), grepTimeoutMs: 1 });
  await store.recordTurn('big', Array.from({ length: 3000 }, (_, i) => ({ role: 'owner', text: 'needle line ' + i })));
  const helperPid = store.grepHelper.pid;
  const timedOut = await store.search('needle');
  assert.equal(timedOut.grep.complete, false, 'a killed grep is an incomplete answer, as before');
  assert.match(timedOut.grep.reason, /grep killed after 1 ms; partial/);
  assert.throws(() => process.kill(store.lastGrepPid, 0), { code: 'ESRCH' }, 'the git grep child is the one killed');
  // The helper itself is untouched: the same process answers the next search, in full.
  store.grepTimeoutMs = 5000;
  const served = await store.search('needle');
  assert.equal(served.grep.complete, true);
  assert.equal(served.grep.hits.length, 10);
  assert.equal(store.grepHelper.pid, helperPid, 'a timeout never costs the helper');
  assert.deepEqual(events.filter(([event]) => event === FALLBACK), [], 'a slow grep is the request\'s timeout, not a helper fault');
  await store.close();
});

test('3: a helper that cannot start falls back to the direct spawn, logged once, and the search answers', async () => {
  const root = newRoot();
  const events = [];
  const store = await open(root, { logger: (event, fields) => events.push([event, fields]),
    grepHelper: { command: '/nonexistent/definitely-not-node' } });
  await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
  const r = await store.search('needle');
  assert.equal(r.grep.complete, true);
  assert.deepEqual(hitsOf(r), reference(repo(root), 'needle', 10));
  const fallbacks = events.filter(([event]) => event === FALLBACK);
  assert.equal(fallbacks.length, 1, JSON.stringify(events));
  assert.match(fallbacks[0][1].reason, /helper-unavailable/);
  assert.equal(store.grepHelper.pid, undefined, 'there never was a helper process');
  await store.close();
});

test('3: a helper that dies mid-request: the direct spawn answers, with no forged hit and none lost', async () => {
  const root = newRoot();
  const events = [];
  const store = await open(root, { logger: (event, fields) => events.push([event, fields]),
    grepHelper: { script: FAULTY, args: ['die'] } });
  await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
  const r = await store.search('needle');
  assert.equal(r.grep.complete, true);
  assert.deepEqual(hitsOf(r), reference(repo(root), 'needle', 10), 'the half-streamed attempt was dropped, not merged');
  assert.deepEqual(r.grep.hits.map(h => h.id), ['t-t1-0'], 'nothing the broken helper streamed survived');
  const fallbacks = events.filter(([event]) => event === FALLBACK);
  assert.equal(fallbacks.length, 1, JSON.stringify(events));
  assert.match(fallbacks[0][1].reason, /helper-died/);
  assert.equal(alive(store.grepHelper.pid), false, 'the dead helper is not left behind');
  await store.close();
});

test('3: a helper that never answers: the request deadline falls back, and the wedged helper is gone', async () => {
  const root = newRoot();
  const events = [];
  const store = await open(root, { logger: (event, fields) => events.push([event, fields]), grepTimeoutMs: 100,
    grepHelper: { script: FAULTY, args: ['silent'], answerGraceMs: 150 } });
  await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
  const wedged = store.grepHelper.pid;
  assert.ok(Number.isInteger(wedged));
  const r = await store.search('needle');
  assert.equal(r.grep.complete, true, 'a helper that answers nothing never fails the search');
  assert.deepEqual(hitsOf(r), reference(repo(root), 'needle', 10));
  const fallbacks = events.filter(([event]) => event === FALLBACK);
  assert.equal(fallbacks.length, 1, JSON.stringify(events));
  assert.match(fallbacks[0][1].reason, /helper-no-answer/);
  assert.equal(alive(wedged), false, 'a wedged helper is killed, so it cannot answer a later request out of turn');
  await store.close();
});

test('4: close() takes the helper with it, and a store whose process just exits leaves no orphan', async () => {
  const root = newRoot();
  const store = await open(root);
  await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
  await store.search('needle');
  const pid = store.grepHelper.pid;
  assert.ok(Number.isInteger(pid));
  assert.equal(alive(pid), true);
  await store.close();
  assert.equal(alive(pid), false, 'the helper died with the store');
  // A store whose process exits without close() (a killed or exiting host): the helper leaves on
  // stdin's EOF, so nothing outlives the store either way.
  const script = `import { MemoryStore } from ${JSON.stringify(new URL('../lib/memory/store.mjs', import.meta.url).href)};
    const store = await MemoryStore.open({ root: ${JSON.stringify(newRoot())}, userId: ${JSON.stringify(USER)} });
    const pid = store.grepHelper.pid;
    let state = 'gone';
    try { const stat = (await import('node:fs')).readFileSync('/proc/' + pid + '/stat', 'utf8'); state = stat.slice(stat.lastIndexOf(')') + 2)[0]; } catch {}
    console.log(pid, state);
    process.exit(0);`;
  const child = spawn(process.execPath, ['--input-type=module', '-e', script], { stdio: ['ignore', 'pipe', 'inherit'] });
  let printed = '';
  child.stdout.on('data', chunk => { printed += chunk; });
  await new Promise(resolve => child.on('exit', resolve));
  const [pidText, state] = printed.trim().split(' ');
  const helper = Number(pidText);
  assert.ok(Number.isInteger(helper) && helper > 0, printed);
  // Read by the store's process itself, before it exits: from here the helper may already be
  // leaving on stdin's EOF, so a look from this side would race it.
  assert.ok(state && state !== 'Z' && state !== 'gone', 'it was running while the store was open: ' + printed);
  const end = Date.now() + 3000;
  while (running(helper) && Date.now() < end) await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(running(helper), false, 'no orphan: the helper leaves when the store\'s process does');
});

test('5: an idle helper sleeps: no CPU between searches, and it still answers', async () => {
  const root = newRoot();
  const events = [];
  const store = await open(root, { logger: (event, fields) => events.push([event, fields]) });
  await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
  assert.equal((await store.search('needle')).grep.complete, true);
  const pid = store.grepHelper.pid;
  const before = cpuMs(pid);
  const started = performance.now();
  await new Promise(resolve => setTimeout(resolve, 400));
  const idle = cpuMs(pid) - before;
  assert.ok(idle < 60, `an idle helper used ${Math.round(idle)} ms of CPU in ${Math.round(performance.now() - started)} ms; a spin loop would use all of it`);
  // Asleep, not dead: the same process answers the next search.
  const again = await store.search('needle');
  assert.deepEqual(again.grep.hits.map(h => h.id), ['t-t1-0']);
  assert.equal(store.grepHelper.pid, pid);
  assert.deepEqual(events.filter(([event]) => event === FALLBACK), []);
  await store.close();
});

/** A seeded generator (mulberry32), so a failing corpus can be rebuilt from the seed it names. */
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

test('1d: on a random corpus - ASCII, non-ASCII, mixed case, regex metacharacters - the helper\'s grep is the direct spawn\'s, byte for byte', async () => {
  const seed = Number(process.env.GREP_HELPER_SEED ?? Date.now() % 1e9);
  const random = rng(seed);
  const pick = list => list[Math.floor(random() * list.length)];
  const words = ['needle', 'Needle', 'NEEDLE', 'ship', 'Friday', 'straße', 'STRASSE', 'École', 'école', 'ÉCOLE', 'naïve',
    'Ωmega', 'ωmega', 'Россия', 'россия', '记账', '完成', '日记', '🙂', 'a.b*c', '[x]', '(y|z)', 'q?', '$100', '^start',
    'back\\slash', '"quoted"', 'tab\there', 'x', 'İstanbul', 'ǅemal', 'ﬁle'];
  const root = newRoot();
  const events = [];
  const store = await open(root, { logger: (event, fields) => events.push([event, fields]) });
  const texts = [];
  for (let t = 0; t < 12; t++) {
    const turn = Array.from({ length: 1 + Math.floor(random() * 6) }, () => {
      const text = Array.from({ length: 1 + Math.floor(random() * 8) }, () => pick(words)).join(pick([' ', '', '-', '\n']));
      texts.push(text);
      return { role: pick(['owner', 'steward']), text };
    });
    await store.recordTurn('r' + t, turn);
  }
  const p = await store.profile();
  await store.updateProfile(Array.from({ length: 6 }, () => '- ' + pick(words) + ' ' + pick(words)).join('\n'), p.version, 'test', 'owner');
  await store.flush();
  // A log file git reads as binary (-I skips it; without -I git prints "Binary file … matches")
  // and one git does not track (--untracked searches it), both holding words of the corpus.
  const line = (id, at, text) => JSON.stringify({ id, at, role: 'owner', text }) + '\n';
  writeFileSync(join(repo(root), 'log', '2026-12.jsonl'), Buffer.concat([
    Buffer.from(line('t-bin-0', '2026-12-01T00:00:00Z', words.join(' '))), Buffer.from([0x00, 0x01, 0x00])]));
  writeFileSync(join(repo(root), 'log', '2099-01.jsonl'), line('t-2099-0', '2099-01-01T00:00:00Z', words.join(' ')));
  // Queries: pieces of what was written (by code point), their case flipped at random, and
  // strings that are not there at all.
  const flip = s => [...s].map(c => (random() < 0.5 ? c.toUpperCase() : c.toLowerCase())).join('');
  const queries = ['needle', 'ÉCOLE', 'Ωmega', '记账', 'a.b*c', '"quoted"', 'back\\slash', 'not-in-the-corpus-Ж'];
  texts.push(words.join(' '));
  for (let i = 0; i < 40; i++) {
    const chars = [...pick(texts)];
    const from = Math.floor(random() * chars.length);
    const piece = chars.slice(from, from + 1 + Math.floor(random() * 10)).join('').trim();
    if (piece) queries.push(random() < 0.5 ? flip(piece) : piece);
  }
  const dir = repo(root);
  for (const query of queries) {
    const needle = JSON.stringify(query).slice(1, -1);
    const args = ['grep', '-F', '-i', '-I', '-n', '-z', '--no-color', '--untracked', '-e', needle, '--', 'log', 'profile.md'];
    const viaHelper = await store.grepHelper.grep(dir, args, { timeoutMs: 5000 });
    const viaSpawn = await git(dir, args, { timeoutMs: 5000 });
    const label = `seed ${seed}, query ${JSON.stringify(query)}`;
    assert.equal(viaHelper.fallback, undefined, label);
    assert.equal(viaHelper.code, viaSpawn.code, label);
    assert.ok(viaHelper.stdout.equals(viaSpawn.stdout), `the same bytes: ${label}`);
    assert.equal(viaHelper.killed, false, label);
    const k = 1 + Math.floor(random() * 20);
    const r = await store.search(query, k);
    assert.equal(r.grep.complete, true, label);
    assert.deepEqual(hitsOf(r), reference(dir, query, k), `the search's hits: ${label}`);
  }
  // The corpus did exercise both answers: some queries matched and some did not.
  const codes = new Set();
  for (const query of queries.slice(0, 8)) codes.add((await git(dir, ['grep', '-F', '-i', '-q', '-e', JSON.stringify(query).slice(1, -1), '--', 'log', 'profile.md'])).code);
  assert.deepEqual([...codes].sort(), [0, 1], `seed ${seed}`);
  assert.deepEqual(events.filter(([event]) => event === FALLBACK), [], `seed ${seed}`);
  await store.close();
});

test('3b: a replaced helper\'s late exit does not take down the helper that replaced it', async () => {
  const root = newRoot();
  const store = await open(root);
  await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
  const dir = store.dir;
  await store.close();
  const events = [];
  const helper = new GrepHelper({ log: (event, fields) => events.push([event, fields]), script: FAULTY, args: ['silent'], answerGraceMs: 100 });
  helper.start();
  const wedged = helper.pid;
  const args = ['grep', '-F', '-i', '-n', '-z', '-e', 'needle', '--', 'log'];
  const first = await helper.grep(dir, args, { timeoutMs: 50 });
  assert.match(first.fallback, /helper-no-answer/);
  // Asked again at once - before the SIGKILLed helper's exit has been heard - a fresh, working
  // helper is started. The old one's exit, heard after that, belongs to the old one.
  helper.script = CHILD;
  helper.args = [];
  const second = await helper.grep(dir, args, { timeoutMs: 5000 });
  assert.equal(second.fallback, undefined, JSON.stringify(second));
  assert.equal(second.code, 0);
  assert.match(second.stdout.toString('utf8'), /needle here/);
  assert.notEqual(helper.pid, wedged);
  assert.equal(running(helper.pid), true, 'the new helper is still there');
  assert.equal(events.filter(([event]) => event === FALLBACK).length, 1, JSON.stringify(events));
  await helper.close();
});

test('4b: a store\'s process SIGKILLed mid-grep leaves no orphan: the helper and its git child are gone', async () => {
  const root = newRoot();
  const fifo = join(root, 'patterns.fifo');
  assert.equal(spawnSync('mkfifo', [fifo]).status, 0);
  // git grep -f <fifo> blocks in open() until a writer comes: a grep that is still running when
  // its store's process is killed, with no grandchild of its own to confuse the count.
  const script = `import { MemoryStore } from ${JSON.stringify(new URL('../lib/memory/store.mjs', import.meta.url).href)};
    const store = await MemoryStore.open({ root: ${JSON.stringify(root)}, userId: ${JSON.stringify(USER)} });
    await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
    store.grepHelper.grep(store.dir, ['grep', '-f', ${JSON.stringify(fifo)}, '--', 'log'], { timeoutMs: 600000,
      onSpawn: pid => console.log(JSON.stringify({ helper: store.grepHelper.pid, git: pid })) });
    setInterval(() => {}, 1000);`;
  const child = spawn(process.execPath, ['--input-type=module', '-e', script], { stdio: ['ignore', 'pipe', 'inherit'] });
  const exited = new Promise(resolve => child.on('exit', (code, signal) => resolve(signal)));
  let printed = '';
  const pids = await new Promise((resolve, reject) => {
    child.stdout.on('data', chunk => {
      printed += chunk;
      if (printed.includes('\n')) resolve(JSON.parse(printed.split('\n')[0]));
    });
    child.on('exit', () => reject(new Error('the store process exited first: ' + printed)));
  });
  try {
    assert.ok(Number.isInteger(pids.helper) && Number.isInteger(pids.git), printed);
    assert.equal(parentOf(pids.git), pids.helper, 'the grep is the helper\'s child');
    assert.equal(parentOf(pids.helper), child.pid, 'the helper is the store process\'s child');
    assert.equal(running(pids.git), true, 'the grep is running');
    child.kill('SIGKILL');
    assert.equal(await exited, 'SIGKILL');
    const end = Date.now() + 3000;
    while ((running(pids.helper) || running(pids.git)) && Date.now() < end) await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(running(pids.helper), false, 'no orphan: the helper left with its store\'s process');
    assert.equal(running(pids.git), false, 'and took its running git child with it');
  } finally {
    // A grep left blocked by a failure above is released: a writer's open and close is its EOF.
    try { closeSync(openSync(fifo, constants.O_WRONLY | constants.O_NONBLOCK)); } catch { /* no reader left: as it should be */ }
  }
});

/** A grep that stays running: git grep -f <fifo> blocks in open() until a writer comes. Returns
 *  the fifo and its release (a writer's open and close is its EOF). */
function blockingFifo(root) {
  const fifo = join(root, 'patterns.fifo');
  assert.equal(spawnSync('mkfifo', [fifo]).status, 0);
  return { fifo, release: () => { try { closeSync(openSync(fifo, constants.O_WRONLY | constants.O_NONBLOCK)); } catch { /* no reader left */ } } };
}

test('3c: a helper that dies while its git runs takes that git with it - nothing is left running unbounded', async () => {
  const root = newRoot();
  const { fifo, release } = blockingFifo(root);
  const events = [];
  const store = await open(root, { logger: (event, fields) => events.push([event, fields]) });
  await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
  try {
    let gitPid;
    const pending = store.grepHelper.grep(store.dir, ['grep', '-f', fifo, '--', 'log'], { timeoutMs: 600000, onSpawn: pid => { gitPid = pid; } });
    while (!gitPid) await new Promise(resolve => setTimeout(resolve, 10));
    const helper = store.grepHelper.pid;
    assert.equal(parentOf(gitPid), helper);
    process.kill(helper, 'SIGKILL'); // a crash, an OOM kill - or #fail's own kill of a wedged helper
    assert.match((await pending).fallback, /helper-died \(SIGKILL\)/);
    const end = Date.now() + 2000;
    while (running(gitPid) && Date.now() < end) await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(running(gitPid), false, 'the git the dead helper was running is gone too');
    // and the store goes on: the next search starts a fresh helper
    const r = await store.search('needle');
    assert.equal(r.grep.complete, true);
    assert.notEqual(store.grepHelper.pid, helper);
  } finally { release(); await store.close(); }
});

test('3d: a helper that keeps failing is not restarted for every search - one restart, then a backoff (doubling to its cap); an answer resets it', async () => {
  const root = newRoot();
  const store = await open(root);
  await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
  const dir = store.dir;
  await store.close();
  // A helper that starts and dies at once; each start leaves one line, so the forks are counted.
  const starts = join(root, 'starts');
  const dying = join(root, 'dying.mjs');
  writeFileSync(dying, `import { appendFileSync } from 'node:fs'; appendFileSync(${JSON.stringify(starts)}, 'x'); process.exit(3);\n`);
  const forks = () => { try { return readFileSync(starts, 'utf8').length; } catch { return 0; } };
  let ms = Date.parse('2026-11-20T00:00:00Z');
  const events = [];
  const helper = new GrepHelper({ log: (event, fields) => events.push([event, fields]), script: dying,
    restartBackoffMs: { min: 1000, max: 3000 }, now: () => ms });
  const args = ['grep', '-F', '-i', '-n', '-z', '-e', 'needle', '--', 'log'];
  const ask = () => helper.grep(dir, args, { timeoutMs: 5000 });
  helper.start(); // the boot's start
  const until = async what => { const end = Date.now() + 5000; while (!what()) { if (Date.now() > end) throw new Error('timed out'); await new Promise(r => setTimeout(r, 10)); } };
  await until(() => events.some(([e]) => e === 'memory/grep-helper-down'));
  assert.equal(forks(), 1);
  // The first search after a failure restarts it once (a fault is restarted after, design r29) ...
  // (died, or gone: whether its exit or the EPIPE of the request's write is heard first)
  assert.match((await ask()).fallback, /helper-(died \(exit code 3\)|gone)/);
  assert.equal(forks(), 2);
  const backoffs = () => events.filter(([e]) => e === 'memory/grep-helper-backoff').map(([, f]) => f.retry_in_ms);
  assert.deepEqual(backoffs(), [1000]);
  // ... and then not again until the backoff runs out: each search pays the direct fork only.
  for (let i = 0; i < 3; i++) assert.match((await ask()).fallback, /^helper-backoff until 2026-11-20T00:00:01\.000Z: helper-(died|gone)/);
  assert.equal(forks(), 2);
  for (const [wait, next] of [[1000, 2000], [2000, 3000], [3000, 3000]]) {
    ms += wait - 1;
    assert.match((await ask()).fallback, /^helper-backoff/);
    ms += 1;
    assert.match((await ask()).fallback, /helper-(died|gone)/);
    assert.equal(backoffs().at(-1), next, 'doubling, capped');
  }
  assert.equal(forks(), 5);
  assert.equal(events.filter(([e]) => e === FALLBACK).length, 1 + 3 + 3 * 2, 'every query that fell back is logged');
  // A helper that works again answers once its backoff runs out, and that resets the count:
  // a later single failure is restarted at once.
  helper.script = CHILD;
  ms += 3000;
  const served = await ask();
  assert.equal(served.code, 0, JSON.stringify(served));
  process.kill(helper.pid, 'SIGKILL');
  await until(() => events.filter(([e]) => e === 'memory/grep-helper-down').length === 2);
  assert.equal((await ask()).code, 0, 'restarted at once: one failure after an answer is a first one');
  await helper.close();
});

test('6: close() while a search waits on the helper forks nothing more - the grep answers incomplete, "closed"', async () => {
  const root = newRoot();
  const events = [];
  const store = await open(root, { logger: (event, fields) => events.push([event, fields]), grepTimeoutMs: 60000,
    grepHelper: { script: FAULTY, args: ['silent'], answerGraceMs: 60000 } });
  await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
  const searching = store.search('needle');
  await new Promise(resolve => setTimeout(resolve, 100));
  await store.close();
  const r = await searching;
  assert.equal(r.grep.complete, false);
  assert.match(r.grep.reason, /closed/);
  assert.equal(store.lastGrepPid, undefined, 'no direct spawn after close');
  assert.deepEqual(events.filter(([event]) => event === FALLBACK), []);
});

test('7: a direct spawn that cannot even start answers an incomplete grep with its reason - the search never throws', async () => {
  const root = newRoot();
  const store = await open(root, { grepHelper: { command: '/nonexistent/definitely-not-node' } });
  await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
  const aside = store.dir + '.aside';
  renameSync(store.dir, aside); // the window a purge's renames leave: no working directory to spawn in
  try {
    const r = await store.search('needle');
    assert.equal(r.grep.complete, false);
    assert.match(r.grep.reason, /ENOENT/);
  } finally { renameSync(aside, store.dir); }
  await store.close();
});

test('8: the helper\'s own environment is PATH alone - the host\'s NODE_OPTIONS and the like do not reach it', async () => {
  const root = newRoot();
  const store = await open(root);
  const environ = readFileSync(`/proc/${store.grepHelper.pid}/environ`, 'utf8').split('\0').filter(Boolean);
  assert.deepEqual(environ.map(v => v.split('=')[0]), ['PATH']);
  await store.close();
});
