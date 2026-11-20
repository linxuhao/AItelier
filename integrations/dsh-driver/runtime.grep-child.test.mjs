/** The memory store's resident grep helper as a PROCESS, not as code that reads as if it exits
 *  (plugin step 11's helper: integrations/dsh-director/lib/memory/grep-{helper,child}.mjs).
 *
 *  Why this file exists (iss-ff86509e0aa34e3b): on a live host a helper child had answered long
 *  ago and was still there 15 h 40 m later - 1 tick of CPU, state S, 22 fds - and the driver's
 *  `idle_dormant` was refused with
 *    refused: 'background_processes', reason: 'jobs you started are still running; watch_process
 *    them or wait', pids: [<the helper>]
 *  for hours, naming a process the agent had no tool to watch and no shell to kill. Two separate
 *  facts had to be measured rather than inferred, so each test below states one:
 *
 *  1  a helper leaves by itself once it has answered and no request follows within its idle
 *     window (`idleExitMs`), and the next search transparently starts a fresh one - so its life
 *     is bounded by its last use, not by its store's;
 *  2  while it lives, what keeps it alive is the PARENT's open stdin: /proc/<pid>/fd/0 is an
 *     open pipe/socket, the process sleeps in the event-loop wait that watches it (state S, no
 *     CPU) and it never leaves on its own. That is the leak side a fix has to name;
 *  3  the child honours EOF: the moment its stdin is closed it exits 0 after answering, timed
 *     here over the wire protocol itself (one request, its end frame, then stdin.end()) - so
 *     "the child ignores EOF" is refuted by measurement, not by reading the source;
 *  4  the runtime marks the processes it starts for its own machinery (core.mjs's INTERNAL_ENV),
 *     and the dormancy gate counts only jobs the agent itself started: with a real helper and a
 *     real shell-style job both carrying the driver session tag, `backgroundProcesses` reports
 *     the job alone and `dormancyRefusal` names only it.
 *
 *  Everything is observed in /proc and over real pipes: no fixture stands in for the helper, and
 *  no assertion is satisfied by the shape of the code.
 *
 *  Boundaries recorded, not fixed: the marker is forgeable - a shell that exports
 *  DSH_INTERNAL_HELPER=1 hides its own job from this gate (measured: such a job is not reported,
 *  an identical unmarked one is). That is accepted because it can only hide work from the agent
 *  that started it, and because the runtime's own environment never carries the marker (asserted
 *  in test 4), so no driver process inherits it. And a non-numeric DSH_GREP_CHILD_IDLE_MS means
 *  "never" rather than "at once": grep-child.mjs guards it with Number.isFinite, so a typo cannot
 *  turn into an immediate-exit loop. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, readlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { MemoryStore } from '../dsh-director/lib/memory/store.mjs';
import { gitSpawnSpec } from '../dsh-director/lib/memory/git.mjs';
import { backgroundProcesses, dormancyRefusal, SESSION_ENV } from './runtime.mjs';

const USER = 'owner@example.com';
/** The helper script itself, run directly in test 3 (the protocol, with nothing in between). */
const CHILD = fileURLToPath(new URL('../dsh-director/lib/memory/grep-child.mjs', import.meta.url));
/** grep-helper.mjs's IDLE_ENV literal, written out here so the pair is pinned behaviourally. */
const IDLE_ENV = 'DSH_GREP_CHILD_IDLE_MS';
/** The runtime's internal-process marker, pinned from the outside as a literal for the same
 *  reason: what a gate and a spawner agree on IS the string in /proc, and reading it from the
 *  module under test would let a rename move the goalposts. */
const INTERNAL = 'DSH_INTERNAL_HELPER=1';
const FALLBACK = 'memory/grep-spawn-fallback';
const newRoot = () => mkdtempSync(join(tmpdir(), 'dsh-grepchild-'));
const open = (extra = {}) => MemoryStore.open({ root: newRoot(), userId: USER, ...extra });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/** Whether a process is running, from /proc: a zombie is not a process still at work, and a
 *  pid whose /proc entry is gone is not either. */
function running(pid) {
  try {
    return readFileSync(`/proc/${pid}/stat`, 'latin1').replace(/^.*\) /s, '')[0] !== 'Z';
  } catch { return false; }
}

/** One process's state and its own CPU time in ms (USER_HZ is 100), from /proc. */
function procState(pid) {
  try {
    const fields = readFileSync(`/proc/${pid}/stat`, 'latin1').replace(/^.*\) /s, '').split(' ');
    return { state: fields[0], cpu_ms: (Number(fields[11]) + Number(fields[12])) * 10 };
  } catch { return null; }
}

function environOf(pid) {
  try { return readFileSync(`/proc/${pid}/environ`, 'latin1').split('\0'); } catch { return []; }
}

const fdTarget = (pid, fd) => { try { return readlinkSync(`/proc/${pid}/fd/${fd}`); } catch { return undefined; } };
/** A descriptor's fdinfo: an epoll set lists the descriptors it watches as `tfd: <fd> ...`. */
const readFdinfo = (pid, fd) => { try { return readFileSync(`/proc/${pid}/fdinfo/${fd}`, 'latin1'); } catch { return ''; } };
const watchesFd = (info, fd) => new RegExp('^tfd:\\s+' + fd + '\\s', 'm').test(info);

/** Block until `predicate` holds, returning how long that took; undefined on timeout. The
 *  helper leaves on its own, so every wait here is a real observation, not a poll for a flag. */
async function until(predicate, ms) {
  const started = Date.now();
  while (Date.now() - started < ms) {
    if (predicate()) return Date.now() - started;
    await sleep(10);
  }
  return undefined;
}

test('1: a helper exits by itself after answering (its idle window), and the next search starts a fresh one', async () => {
  const events = [];
  const store = await open({ logger: (event, fields) => events.push([event, fields]), grepHelper: { idleExitMs: 1200 } });
  try {
    await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
    const answer = await store.search('needle');
    assert.equal(answer.grep.complete, true, 'the helper answered');
    const pid = store.grepHelper.pid;
    assert.ok(Number.isInteger(pid) && pid > 1, 'the store boots one helper');
    assert.equal(running(pid), true, 'it answered and is still running, one idle window short of leaving');
    const idle = procState(pid);
    assert.equal(idle.state, 'S', 'it is asleep in its wait for the next request, not busy');
    // Nothing closes its store, nothing kills its pid: it has to leave on its own, and this is
    // the assertion the issue asked for - the process is gone, read from /proc, not inferred.
    const exitMs = await until(() => !running(pid), 15000);
    assert.notEqual(exitMs, undefined, `the helper never left after answering (pid ${pid}); an idle helper that stays is the leak`);
    console.log(`IDLE_EXIT pid=${pid} idle_exit_ms=${exitMs} exit_reason=${JSON.stringify(store.grepHelper.reason)}`);
    assert.match(String(store.grepHelper.reason), /helper-died \(exit code 0\)/, 'it left by itself, exit 0 - not killed, not wedged');
    assert.equal(fdTarget(pid, 0), undefined, 'and its stdin is gone with it');
    // The store is not left without an answer: the next search starts a fresh helper.
    const again = await store.search('needle');
    assert.equal(again.grep.complete, true, 'the search after an idle exit still answers');
    assert.ok(Number.isInteger(store.grepHelper.pid) && store.grepHelper.pid !== pid, 'a fresh helper served it');
    assert.equal(store.grepHelper.reason, undefined, 'and the store holds no fault from the idle exit');
    assert.deepEqual(events.filter(([event]) => event === FALLBACK), [], 'an idle exit is not a fallback: no search paid for it');
  } finally { await store.close(); }
});

test('2: while its store lives, what keeps the child alive is the parent\'s open stdin: fd 0 open, state S, no CPU', async () => {
  const store = await open();                          // the product's default idle window
  try {
    await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
    assert.equal((await store.search('needle')).grep.complete, true);
    const pid = store.grepHelper.pid;
    const stdin = fdTarget(pid, 0);
    assert.match(String(stdin), /^(pipe|socket):\[\d+\]$/, `the child's stdin is an open descriptor, held by its parent: ${stdin}`);
    assert.equal(running(pid), true);
    // Parked, not spinning, and parked ON fd 0: node's libuv watches its stdin in an epoll set,
    // and /proc/<pid>/fdinfo/<epoll fd> names the descriptors in it. A child whose stdin had been
    // closed could not be waiting on it - that is the parent side of this leak, measured.
    const epoll = readdirSync(`/proc/${pid}/fd`).filter(fd => fdTarget(pid, fd) === 'anon_inode:[eventpoll]'
      && watchesFd(readFdinfo(pid, fd), 0));
    assert.equal(epoll.length, 1, `nothing waits on fd 0 any more: ${JSON.stringify(readdirSync(`/proc/${pid}/fd`).map(fd => [fd, fdTarget(pid, fd)]))}`);
    const before = procState(pid);
    assert.equal(before.state, 'S', 'asleep in that wait');
    await sleep(1200);
    const after = procState(pid);
    assert.equal(running(pid), true, 'it does not leave while its stdin is open - which is the side of the leak the fix must bound');
    assert.equal(after.cpu_ms - before.cpu_ms, 0, `an idle helper burned ${after.cpu_ms - before.cpu_ms} ms of CPU in 1.2 s`);
    assert.equal(fdTarget(pid, 0), stdin, 'the descriptor is still there, unchanged, after 1.2 s of idling');
    // Evidence for the record, not a gate: /proc/<pid>/syscall needs ptrace permission, and which
    // syscall number means "wait for events" is the architecture's business.
    try { console.log(`IDLE_WAIT pid=${pid} epoll_fd=${epoll[0]} syscall=${readFileSync(`/proc/${pid}/syscall`, 'latin1').trim()}`); }
    catch { console.log(`IDLE_WAIT pid=${pid} epoll_fd=${epoll[0]} syscall=unreadable`); }
  } finally { await store.close(); }
});

test('3: the child honours EOF - it answers, then exits 0 the moment its stdin is closed (timed over its own protocol)', async () => {
  // Nothing of the client here: the protocol is the request line on stdin, git's stdout on fd 1
  // and the end frame on fd 3, so the exit that follows stdin.end() is the child's own doing.
  const child = spawn(process.execPath, [CHILD], { stdio: ['pipe', 'pipe', 'pipe', 'pipe'],
    env: { ...process.env, [IDLE_ENV]: '0' } });      // no idle window: only EOF can end this one
  const pid = child.pid;
  let control = '', answered;
  const frames = [];
  child.stdio[3].on('data', chunk => {
    control += chunk.toString('utf8');
    for (let nl = control.indexOf('\n'); nl >= 0; nl = control.indexOf('\n')) {
      const line = control.slice(0, nl);
      control = control.slice(nl + 1);
      if (!line.trim()) continue;
      try { frames.push(JSON.parse(line)); } catch { /* a torn frame: the next test would be the one to notice */ }
    }
  });
  child.stdout.resume();                                // the answer's bytes; not what this test reads
  child.stderr.resume();
  try {
    assert.equal(running(pid), true, 'the raw child is up');
    const spec = gitSpawnSpec(tmpdir(), ['--version'], tmpdir());
    child.stdin.write(JSON.stringify({ id: 7, spec, timeoutMs: 5000 }) + '\n');
    answered = await until(() => frames.some(frame => frame.event === 'end'), 15000);
    assert.notEqual(answered, undefined, 'the request was never answered: ' + JSON.stringify(frames));
    const end = frames.find(frame => frame.event === 'end');
    assert.equal(end.id, 7);
    assert.equal(end.code, 0);
    assert.ok(end.bytes > 0, 'git\'s stdout went to fd 1 as the protocol says');
    // Answered. Now close stdin, exactly as MemoryStore#close does, and time the exit.
    const started = performance.now();
    const exit = await new Promise(resolve => {
      child.once('exit', (code, signal) => resolve({ code, signal, ms: Math.round(performance.now() - started) }));
      child.stdin.end();
    });
    console.log(`EOF_EXIT pid=${pid} ${JSON.stringify(exit)}`);
    assert.equal(exit.signal, null, 'EOF is not a kill: it leaves on its own');
    assert.equal(exit.code, 0, 'and says so');
    assert.ok(exit.ms < 2000, `the child took ${exit.ms} ms to leave on EOF; a child that ignores EOF is the other candidate leak side`);
    assert.equal(running(pid), false, 'the process is gone, read from /proc');
  } finally {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
  }
});

test('4: an internal helper is not a background job - dormancy is refused only by jobs the agent itself can watch and kill', async () => {
  // A session tag unique to this test: background-job detection matches by it, and other files
  // in the suite run their own drivers.
  const session = 'grepchild-' + process.pid + '-' + Math.random().toString(36).slice(2, 8);
  const previous = process.env[SESSION_ENV];
  process.env[SESSION_ENV] = session;                  // what core/dsh_driver.py sets on a driver
  let store, job;
  try {
    store = await open();
    await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
    assert.equal((await store.search('needle')).grep.complete, true);
    const helper = store.grepHelper.pid;
    const helperEnv = environOf(helper);
    // A job an agent's shell would start: the same session tag, no marker.
    job = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 30000)'], { stdio: ['ignore', 'ignore', 'ignore'],
      env: { ...process.env } });
    assert.notEqual(await until(() => environOf(job.pid).includes(SESSION_ENV + '=' + session), 5000), undefined, 'the job carries the session tag too');
    assert.equal(environOf(job.pid).some(entry => entry.startsWith(INTERNAL.split('=')[0] + '=')), false, 'and no marker');

    const background = backgroundProcesses(session, process.pid, '/proc', SESSION_ENV);
    console.log(`BACKGROUND session=${session} helper=${helper} job=${job.pid} reported=${JSON.stringify(background)}`);
    assert.ok(background.includes(job.pid), 'the job the agent started is reported, so the filter is not hiding everything');
    // This is the verdict the issue measured: a live helper, and idle_dormant refused over it.
    assert.equal(background.includes(helper), false, 'the runtime\'s own helper is not a job; counting it refused dormancy for as long as it lived');
    // The two environment facts behind it: the helper carries the driver's session tag (which is
    // why the old gate read it as a job) and the runtime's marker (which is how the gate can tell).
    assert.ok(helperEnv.includes(SESSION_ENV + '=' + session), 'the helper inherits the driver session tag');
    assert.ok(helperEnv.includes(INTERNAL), `the helper carries ${INTERNAL}`);

    // The case that used to refuse for as long as the driver lived: a live helper, no job.
    const helperOnly = background.filter(pid => pid !== job.pid);
    assert.deepEqual(helperOnly, [], 'the helper was the only other process with this session tag');
    assert.equal(dormancyRefusal({ reason: 'nothing authorized is available', liveChildren: 0, childSessions: [], background: helperOnly }), null,
      'a live internal helper must not refuse dormancy');
    // And the gate still refuses real work, naming only the job.
    const refused = dormancyRefusal({ reason: 'nothing authorized is available', liveChildren: 0, childSessions: [], background });
    assert.equal(refused.refused, 'background_processes', JSON.stringify(refused));
    assert.deepEqual(refused.pids, [job.pid], 'the refusal names only the job the agent can watch or kill');
    // The marker reaches the spawned child and NOT the process that spawned it: the helper's
    // environment is a new object built for the child, and nothing ever assigns process.env - so
    // no driver process can inherit this marker by accident (the spawn-time environment is what
    // /proc/<pid>/environ holds, which is exactly the claim).
    assert.equal(environOf(process.pid).some(entry => entry.startsWith(INTERNAL.split('=')[0] + '=')), false,
      'the runtime\'s own environment is unmarked: only the child it spawns for itself is marked');
  } finally {
    job?.kill('SIGKILL');
    await store?.close();
    if (previous === undefined) delete process.env[SESSION_ENV]; else process.env[SESSION_ENV] = previous;
  }
});
