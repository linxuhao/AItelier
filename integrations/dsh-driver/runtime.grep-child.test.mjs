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
 *     real shell-style job carrying the driver session tag, `backgroundProcesses` reports
 *     the job alone and `dormancyRefusal` names only it.
 *
 *  Test 1's two samples have a store each, for a measured reason (iss-44844192be184fbb): the
 *  "still running" sample was first taken after store.search() returned, and search() joins the
 *  grep answer with the ranked half (store.mjs:1031), so the distance from the answer to the
 *  /proc sample was that half's duration - work of unbounded duration. Under the load of the
 *  full suite it once ran past the 1200 ms window and the arm reddened (tests 549 / pass 548 /
 *  fail 1, 2026-11-21) while the helper was correct: the sample observed a moment already past
 *  the deadline it meant to be before. Now "still there, asleep" is witnessed on the product's
 *  wide default window, with the distance from the request (which the answer cannot precede)
 *  measured and printed, so no stall this test can meet carries the sample past the deadline;
 *  "it leaves by itself" gets a store with the short window, where the exit is also required to
 *  come no earlier than one whole window after the request - so a helper that left at once
 *  cannot pass as one that waited.
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
/** Test 1's two idle windows. The first is the product's own default (grep-helper.mjs's
 *  idleExitMs 60000), wide enough that no stall between the helper's answer and a /proc sample
 *  can reach it; the second is short enough to watch a helper leave inside the test. They are
 *  two stores on purpose: the "alive" sample must not wait out a window, and the "gone" sample
 *  has to be watched from inside a short one. */
const WIDE_IDLE_MS = 60000;
const SHORT_IDLE_MS = 1200;
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
  const log = (event, fields) => events.push([event, fields]);
  // (a) An answered helper that has not reached its idle deadline is still there, and asleep.
  // What binds this sample to the window is measurement, not luck: the window is the product's
  // wide default and `since_request_ms` - printed, and asserted below to be inside that window -
  // bounds the answer-to-sample distance from above, because the request (and so the answer) went
  // out after `started`. So no work between the two - search()'s ranked half, a slow host, other
  // containers sharing two CPUs - can carry the sample past the deadline without this test
  // saying so first, instead of blaming the helper.
  const store = await open({ logger: log, grepHelper: { idleExitMs: WIDE_IDLE_MS } });
  try {
    await store.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
    const started = Date.now();
    const answer = await store.search('needle');
    assert.equal(answer.grep.complete, true, 'the helper answered');
    const pid = store.grepHelper.pid;
    assert.ok(Number.isInteger(pid) && pid > 1, 'the store boots one helper');
    const sinceRequestMs = Date.now() - started;
    console.log(`IDLE_ALIVE pid=${pid} window_ms=${WIDE_IDLE_MS} since_request_ms=${sinceRequestMs}`);
    assert.ok(sinceRequestMs < WIDE_IDLE_MS, `the sample came ${sinceRequestMs} ms after the request, past the helper's ${WIDE_IDLE_MS} ms window: it witnesses nothing about a helper still inside one`);
    assert.equal(running(pid), true, 'it answered and is still running, inside the window measured above');
    const idle = procState(pid);
    assert.equal(idle.state, 'S', 'it is asleep in its wait for the next request, not busy');
  } finally { await store.close(); }
  // (b) The same helper with a window short enough to watch it leave: nothing closes this store
  // and nothing kills its pid, so the exit has to be its own - and this is the assertion the
  // issue asked for, the process gone, read from /proc, not inferred. A store of its own, both
  // because (a) must not wait out a wide window and because "it waited its window, it did not
  // leave at once" can only be measured against a short one.
  const dying = await open({ logger: log, grepHelper: { idleExitMs: SHORT_IDLE_MS } });
  try {
    await dying.recordTurn('t1', [{ role: 'owner', text: 'needle here' }]);
    const started = Date.now();
    assert.equal((await dying.search('needle')).grep.complete, true, 'the short-window helper answered');
    const dyingPid = dying.grepHelper.pid;
    // Wait on the exit the store records, not on `running()`: that helper calls a zombie "not at
    // work", so polling it can return between the kernel's exit and node's dispatch of 'exit'
    // (measured red in the full suite 2026-11-20: pid already 'Z', reason still undefined).
    const diedMs = await until(() => dying.grepHelper.reason !== undefined, 15000);
    assert.notEqual(diedMs, undefined, `the helper never left after answering (pid ${dyingPid}); an idle helper that stays is the leak`);
    const sinceRequestMs = Date.now() - started;
    console.log(`IDLE_EXIT pid=${dyingPid} idle_exit_ms=${diedMs} since_request_ms=${sinceRequestMs} exit_reason=${JSON.stringify(dying.grepHelper.reason)}`);
    // The answer cannot precede the request either, so an exit this long after the request is an
    // exit no earlier than one whole window after the answer: it kept its window, not left at once.
    assert.ok(sinceRequestMs >= SHORT_IDLE_MS, `it left ${sinceRequestMs} ms after the request, less than its ${SHORT_IDLE_MS} ms window after the answer: it did not leave on its idle clock`);
    assert.match(String(dying.grepHelper.reason), /helper-died \(exit code 0\)/, 'it left by itself, exit 0 - not killed, not wedged');
    assert.equal(procState(dyingPid), null, 'and no /proc entry is left for it: not a live process, not a zombie');
    assert.equal(fdTarget(dyingPid, 0), undefined, 'and its stdin is gone with it');
    // The store is not left without an answer: the next search starts a fresh helper.
    const again = await dying.search('needle');
    assert.equal(again.grep.complete, true, 'the search after an idle exit still answers');
    assert.ok(Number.isInteger(dying.grepHelper.pid) && dying.grepHelper.pid !== dyingPid, 'a fresh helper served it');
    assert.equal(dying.grepHelper.reason, undefined, 'and the store holds no fault from the idle exit');
    assert.deepEqual(events.filter(([event]) => event === FALLBACK), [], 'an idle exit is not a fallback: no search paid for it');
  } finally { await dying.close(); }
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
    // The environment fact behind it: the helper carries the runtime's marker (which is how the
    // gate can tell). Its session tag is no longer asserted: since s21-followups the helper's
    // environment is PATH alone by default (grep-helper.mjs), so it no longer inherits the driver
    // session tag - the old gate's reason to read it as a job is gone at the source, and the
    // marker remains the second, independent guard measured here.
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

test('5: a helper that left on its spawn-anchored idle clock before the first request is restarted, not faulted', async () => {
  // The failure this forces (iss-5790276bf12140ef) is an ORDERING, not a duration. grep-child.mjs
  // arms its idle clock at process start (:127) and grep-helper.mjs starts the helper as the last
  // act of boot (store.mjs:345); if the loop is held at the deadline the child leaves while its
  // parent cannot dispatch the exit, so a subsequent request writes into a dead pipe first and
  // learns of the exit later. On the fault path that is `helper-gone: write EPIPE` (or
  // `helper-died (exit code 0)`) as grepHelper.reason, plus a direct-spawn fallback. The idle
  // path restarts one helper, retries that write, and records neither a reason nor a fallback.
  const events = [];
  const log = (event, fields) => events.push([event, fields]);
  const WINDOW = 400;                     // short against the product's 60000, longer than boot
  const store = await open({ logger: log, grepHelper: { idleExitMs: WINDOW } });
  try {
    const pid = store.grepHelper.pid;
    assert.ok(Number.isInteger(pid) && pid > 1, 'the store boots one helper');
    assert.equal(store.grepHelper.reason, undefined, 'no fault before the race');
    // Hold THIS loop across the child's own deadline: no exit callback can run while it is busy.
    // Wait for the child's actual state in /proc (zombie or reaped), not a timer approximation,
    // so a slow host cannot turn this into a guess about when the child's clock fired.
    const heldFrom = Date.now();
    const ceiling = heldFrom + WINDOW + 5000;
    while (Date.now() < ceiling) {
      const state = procState(pid)?.state;
      if (state === undefined || state === 'Z') break;
    }
    const heldMs = Date.now() - heldFrom;
    console.log(`IDLE_RACE pid=${pid} window_ms=${WINDOW} held_ms=${heldMs}`);
    assert.ok(heldMs >= WINDOW, `the loop was held only ${heldMs} ms, short of the ${WINDOW} ms window the child started with`);
    assert.equal(store.grepHelper.reason, undefined, 'the exit was not dispatched during the hold');
    const args = ['grep', '-F', '-i', '-I', '-n', '-z', '--no-color', '--untracked', '-e', 'needle', '--', 'log', 'profile.md'];
    const reasons = [];
    // grep()'s request runs in a microtask, so this write lands before libuv can dispatch the
    // child's pending exit. That is the ordering no amount of host load could force reliably.
    const pending = store.grepHelper.grep(store.dir, args, { timeoutMs: 5000,
      onSpawn: () => reasons.push(store.grepHelper.reason),
      onStdout: () => reasons.push(store.grepHelper.reason) });
    const answered = await pending;
    console.log(`IDLE_RACE_ANSWER pid=${pid} new_pid=${store.grepHelper.pid} fallback=${JSON.stringify(answered.fallback)} reason=${JSON.stringify(store.grepHelper.reason)} reasons=${JSON.stringify(reasons)}`);
    assert.equal(answered.fallback, undefined, 'the helper answered; no direct-spawn fallback was paid');
    assert.equal(store.grepHelper.reason, undefined, 'a clean self-exit is idle, not a fault');
    assert.deepEqual(reasons, reasons.map(() => undefined), 'no fault reason was recorded on the way to the answer');
    assert.deepEqual(events.filter(([event]) => event === FALLBACK), [], 'no fallback event was recorded');
    assert.ok(Number.isInteger(store.grepHelper.pid) && store.grepHelper.pid !== pid,
      'exactly one fresh helper served the request that reached the dead one');
  } finally { await store.close(); }
});
