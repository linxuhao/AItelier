/** Design 3.8.2's 20 ms on the search path: a `git grep` is forked by a resident helper process
 *  (lib/memory/grep-helper.mjs, lib/memory/grep-child.mjs) instead of by the store's own
 *  process, where fork+exec costs ~24 ms per GiB of RSS. What each arm proves:
 *
 *  1  the helper runs the direct spawn's command - same argv, cwd, environment and umask
 *     wrapper - so what matches is unchanged: a profile.md line, a -F literal, -i's case, -I's
 *     binary skip, -z/-n's fields, --untracked's files; and the fork is the helper's, not this
 *     process's.
 *  2  the grep timeout is the request's: complete: false with the partial hits and the same
 *     reason, the helper survives it and serves the next search.
 *  3  a helper that cannot start, dies mid-request or never answers falls back to the direct
 *     spawn, logged as memory/grep-spawn-fallback; no search fails and no answer changes.
 *  4  close() takes the helper with it, and a store whose process simply exits leaves no orphan.
 *  5  an idle helper sleeps: it uses no CPU between searches and still answers afterwards.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { MemoryStore } from '../lib/memory/store.mjs';
import { git, gitEnv } from '../lib/memory/git.mjs';

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
    console.log(store.grepHelper.pid);
    process.exit(0);`;
  const child = spawn(process.execPath, ['--input-type=module', '-e', script], { stdio: ['ignore', 'pipe', 'inherit'] });
  let printed = '';
  child.stdout.on('data', chunk => { printed += chunk; });
  await new Promise(resolve => child.on('exit', resolve));
  const helper = Number(printed.trim());
  assert.ok(Number.isInteger(helper) && helper > 0, printed);
  assert.equal(running(helper), true, 'it was running while the store was open');
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
