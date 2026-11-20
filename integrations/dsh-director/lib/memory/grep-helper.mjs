/** The resident git-grep helper a memory store searches through (design 3.8.2: during a search
 *  the event loop blocks at most 20 ms at the design's 100k entries).
 *
 *  Why it exists: `git grep` on the search path was a fork+exec of the STORE's process, and
 *  fork+exec costs ~24 ms per GiB of the forking process's RSS (12-16 ms at 550 MB, measured in
 *  the step 21 follow-up) - synchronous in libuv, straight into the event loop. The store is
 *  the big process by design (150 MB of entries, 120 MB of vectors, plus whatever the host
 *  holds). So the fork happens in a bare node process of 40-50 MB instead: one per store,
 *  started by MemoryStore#boot while the store is still small, asked over pipes for each grep.
 *  There a fork+exec costs 2-3 ms whatever the store has grown to.
 *
 *  The request is git.mjs's gitSpawnSpec - the very command the store's process would have
 *  spawned, argv, cwd, allowlisted environment and umask 077 wrapper included - so what the
 *  grep matches is unchanged; only where it is forked from changed. The protocol is
 *  grep-child.mjs's: one JSON request line on stdin, git's stdout on fd 1, control frames on
 *  fd 3.
 *
 *  A search never depends on the helper. `grep()` resolves {fallback: reason} instead of a
 *  result when the helper cannot be started, is gone, or does not answer in time; the caller
 *  (MemoryStore#grep) then runs the direct spawn for that query, and every fallback is logged
 *  as `memory/grep-spawn-fallback`. The grep's own `timeoutMs` rides on the request: the helper
 *  kills its git child and answers killed: true, and the next request is served as usual - a
 *  slow grep never costs the helper. Between requests it sleeps in a read on stdin. `close()`
 *  ends it: no process outlives its store, and one whose process dies ends on stdin's EOF. */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { gitSpawnSpec } from './git.mjs';

const SCRIPT = fileURLToPath(new URL('./grep-child.mjs', import.meta.url));
const say = error => String(error?.message ?? error);

export class GrepHelper {
  #child; #pid; #reason; #closed = false; #active; #queue = Promise.resolve();
  #frames = ''; #stderr = []; #seq = 0;

  /** @param {object} o log(event, fields): the store's logger; answerGraceMs (5000): how long
   *  past the request's own timeoutMs the helper may take to answer before it is treated as
   *  wedged - the query then falls back and the helper is killed; command/script/args/env: what
   *  to run as the helper (process.execPath and grep-child.mjs; tests hand it a broken one). */
  constructor({ log = () => {}, answerGraceMs = 5000, command = process.execPath, script = SCRIPT, args = [], env } = {}) {
    this.log = log; this.answerGraceMs = answerGraceMs;
    this.command = command; this.script = script; this.args = args; this.env = env;
  }

  /** The helper's pid (the last one, once it is gone); undefined before the first start. */
  get pid() { return this.#pid; }

  /** Why the helper is unusable, or undefined while it is (or may be) used. */
  get reason() { return this.#reason; }

  /** Start it. MemoryStore#boot calls this once, at open, so the helper is forked while the
   *  store's process is still small. Never throws and never rejects: a helper that cannot start
   *  leaves each query to the direct spawn, which is where a search falls back anyway. */
  start() {
    if (this.#closed || this.#child) return this;
    this.#reason = undefined;
    this.#stderr = [];
    this.#frames = '';
    let child;
    try {
      child = spawn(this.command, [this.script, ...this.args], { stdio: ['pipe', 'pipe', 'pipe', 'pipe'],
        env: this.env ?? process.env });
    } catch (error) {
      this.#fail('helper-unavailable: ' + say(error));
      return this;
    }
    this.#child = child;
    this.#pid = child.pid;
    // Every event is this child's own: one heard after it was replaced (a SIGKILLed helper's
    // exit arrives a poll phase later, and the next search may have started a fresh one by
    // then) is about a process that is already gone and must not touch its successor.
    const own = handler => (...a) => { if (this.#child === child) handler(...a); };
    child.on('error', own(error => this.#fail('helper-unavailable: ' + say(error))));
    child.on('exit', (code, signal) => { child.exited = true; own(() => this.#fail(`helper-died (${signal ?? 'exit code ' + code})`))(); });
    child.stdin.on('error', own(error => this.#fail('helper-gone: ' + say(error))));
    child.stdout.on('data', own(chunk => this.#stdout(chunk)));
    child.stderr.on('data', own(chunk => { if (this.#stderr.length < 8) this.#stderr.push(chunk); }));
    child.stdio[3].on('data', own(chunk => this.#control(chunk)));
    child.stdio[3].on('error', () => {});
    this.#hold(false); // idle: see #hold - a helper awaiting a request must not pin this process
    return this;
  }

  /** Keep the loop alive only while a request is in flight: the four pipes and the child handle
   *  are unref'd between requests, so a store that is never closed does not keep its host's
   *  event loop (and so its process) alive - the helper leaves on stdin's EOF instead. While a
   *  request is pending they are ref'd, so the answer cannot be missed by an exiting process. */
  #hold(on, child = this.#child) {
    if (!child) return;
    for (const stream of [child.stdin, child.stdout, child.stderr, child.stdio[3]]) {
      try { if (on) stream?.ref?.(); else stream?.unref?.(); } catch { /* a closed stream */ }
    }
    try { if (on) child.ref(); else child.unref(); } catch { /* a closed child */ }
  }

  /** One grep through the helper: git()'s shape ({code, stdout, stderr, killed, pid}) when the
   *  helper answered, {fallback: reason} when this query has to run the direct spawn instead.
   *  Never rejects. A fallback is logged here, once, so every way of losing the helper is one
   *  event on the store's log. */
  async grep(dir, args, { timeoutMs = 60000, home = dir, onSpawn, onStdout } = {}) {
    const spec = gitSpawnSpec(dir, args, home);
    const queued = this.#queue.then(() => this.#request({ spec, timeoutMs, onSpawn, onStdout }));
    this.#queue = queued.catch(() => {});
    // Whatever happens here - a bug in this file included - the query must never fail: it is
    // answered by the direct spawn instead.
    const result = await queued.catch(error => ({ fallback: 'helper-error: ' + say(error) }));
    if (result.fallback) this.log('memory/grep-spawn-fallback', { reason: String(result.fallback).slice(0, 200) });
    return result;
  }

  /** Stop it, and be done: after this the pid is gone. A request still in flight is dropped
   *  (the store is unusable by then). Graceful first - stdin's end makes the helper kill its
   *  own git child and exit - with a SIGKILL as the ceiling. Never throws. */
  async close() {
    this.#closed = true;
    this.#settle({ fallback: 'helper-closed' });
    const child = this.#child;
    this.#child = undefined;
    if (!child) return;
    this.#hold(true, child); // long enough to hear the exit
    const gone = child.exited ? Promise.resolve() : new Promise(resolve => {
      child.once('exit', resolve);
      child.once('error', resolve);
    });
    try { child.stdin.end(); } catch { /* it is already gone */ }
    const timer = setTimeout(() => { try { child.kill('SIGKILL'); } catch { /* already gone */ } }, 2000);
    timer.unref?.();
    try { await gone; } finally {
      clearTimeout(timer);
      for (const stream of [child.stdin, child.stdout, child.stderr, child.stdio[3]]) {
        try { stream.destroy(); } catch { /* already closed */ }
      }
    }
  }

  #request({ spec, timeoutMs, onSpawn, onStdout }) {
    if (this.#closed) return Promise.resolve({ fallback: 'helper-closed' });
    this.start();
    const child = this.#child;
    if (!child) return Promise.resolve({ fallback: this.#reason ?? 'helper-unavailable' });
    this.#hold(true);
    return new Promise(resolve => {
      const active = { id: ++this.#seq, onSpawn, onStdout, out: [], got: 0, pid: undefined, end: undefined,
        timer: undefined, resolve, settled: false };
      this.#active = active;
      // The helper enforces `timeoutMs` itself and answers killed: true; this is the backstop
      // for a helper that answers nothing at all, which is a fault and not the grep's timeout.
      const deadline = timeoutMs > 0 ? timeoutMs + this.answerGraceMs : 0;
      if (deadline > 0) active.timer = setTimeout(() => {
        this.#fail(`helper-no-answer after ${deadline} ms`);
      }, deadline);
      try { child.stdin.write(JSON.stringify({ id: active.id, spec, timeoutMs }) + '\n'); }
      catch (error) { this.#fail('helper-gone: ' + say(error)); }
    });
  }

  #stdout(chunk) {
    const active = this.#active;
    if (!active || active.settled) return;
    active.got += chunk.length;
    if (active.onStdout) active.onStdout(chunk); else active.out.push(chunk);
    this.#maybeFinish(active);
  }

  #control(chunk) {
    this.#frames += chunk.toString('utf8');
    for (let nl = this.#frames.indexOf('\n'); nl >= 0; nl = this.#frames.indexOf('\n')) {
      const line = this.#frames.slice(0, nl);
      this.#frames = this.#frames.slice(nl + 1);
      if (!line.trim()) continue;
      let frame;
      try { frame = JSON.parse(line); } catch { continue; }
      const active = this.#active;
      if (!active || active.settled || frame.id !== active.id) continue;
      if (frame.event === 'spawn') {
        active.pid = frame.pid;
        if (Number.isInteger(frame.pid)) active.onSpawn?.(frame.pid);
      } else if (frame.event === 'end') {
        active.end = frame;
        this.#maybeFinish(active);
      }
    }
  }

  /** All of this request's stdout has arrived (the end frame says how much it is - the two
   *  pipes are independent, so the count is what orders them) and the answer is in: settle it. */
  #maybeFinish(active) {
    if (active.settled || !active.end || active.got < (active.end.bytes ?? 0)) return;
    this.#settle({
      code: active.end.code, stdout: Buffer.concat(active.out), stderr: active.end.stderr ?? '',
      killed: active.end.killed === true, pid: active.pid,
    }, active);
  }

  #settle(result, active = this.#active) {
    if (!active || active.settled) return;
    active.settled = true;
    clearTimeout(active.timer);
    if (this.#active === active) { this.#active = undefined; this.#hold(false); }
    active.resolve(result);
  }

  /** The helper is unusable: remember why, take the process down (a wedged one included) and
   *  let the request in flight fall back. The next request starts a fresh one, unless the
   *  store has been closed. */
  #fail(reason) {
    if (this.#reason) return;
    this.#reason = reason;
    const child = this.#child;
    this.#child = undefined;
    this.#settle({ fallback: reason });
    if (!child) return;
    for (const stream of [child.stdin, child.stdout, child.stderr, child.stdio[3]]) {
      try { stream.destroy(); } catch { /* already closed */ }
    }
    try { child.kill('SIGKILL'); } catch { /* already gone */ }
  }
}
