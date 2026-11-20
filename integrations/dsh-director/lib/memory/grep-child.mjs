/** The resident grep helper process (grep-helper.mjs is its parent-side client; design 3.8.2:
 *  during a search the event loop blocks at most 20 ms).
 *
 *  It exists to move ONE fork off the store's process: `git grep` on the search path used to be
 *  a fork+exec of the store itself, ~24 ms per GiB of its RSS, and the store is the big process.
 *  Here the fork happens in this bare node process, which stays at 40-50 MB, so it costs 2-3 ms.
 *
 *  Requests arrive as one JSON line on stdin: {id, spec, timeoutMs}, where spec is git.mjs's
 *  gitSpawnSpec - the very command the store's process would have spawned (same argv, cwd,
 *  allowlisted environment and umask 077 sh wrapper), built there and sent here. git's stdout
 *  goes straight to fd 1 as it comes (paused while the pipe is full), and control frames go on
 *  fd 3, one JSON line each: {id, event: 'spawn', pid} when git is forked and
 *  {id, event: 'end', code, killed, stderr, bytes} when it is done. `bytes` is how much stdout
 *  this request produced, so the parent knows when it has all of it even though the two pipes
 *  are independent. Requests are served one at a time, in order.
 *
 *  `timeoutMs` is the request's own: past it the git child is SIGKILLed and the answer says
 *  killed - this process is untouched and the next request is served as usual. Between
 *  requests it waits on stdin (one epoll wait: no timer, no loop, no CPU). stdin closing means
 *  the store's process is gone: kill a running git child and exit, so nothing is orphaned.
 *
 *  `DSH_GREP_CHILD_IDLE_MS` (grep-helper.mjs's IDLE_ENV, same literal) bounds that wait: with
 *  a positive value, a helper that has answered and then heard no request for that long leaves
 *  by itself, exactly as it would on stdin's EOF. Without it a resident helper lives as long as
 *  its store does no matter how long ago it was last asked - a live driver held one for 15 h
 *  with 1 tick of CPU (iss-ff86509e0aa34e3b). 0 (the default) keeps the unbounded wait, which
 *  is what the test fixtures that stand in for this script want. */
import { spawn } from 'node:child_process';
import { writeSync } from 'node:fs';

const CONTROL = 3;
/** Must match grep-helper.mjs's IDLE_ENV: this script is a child process and cannot import
 *  the client module (importing it would run the client here). */
const IDLE_ENV = 'DSH_GREP_CHILD_IDLE_MS';
const rawIdle = Number(process.env[IDLE_ENV] ?? 0);
const IDLE_MS = Number.isFinite(rawIdle) && rawIdle > 0 ? Math.min(rawIdle, 2 ** 31 - 1) : 0;
const queued = [];
let running, current, idleTimer;

function control(frame) {
  try { writeSync(CONTROL, JSON.stringify(frame) + '\n'); } catch { /* the parent is gone: stdin's end ends us */ }
}

function stop() {
  try { current?.kill('SIGKILL'); } catch { /* already gone */ }
  process.exit(0);
}

/** Nothing to do: start the clock that ends this process, so its life is bounded by its last
 *  use rather than by its store's. Cleared the moment a request arrives (pump). */
function armIdle() {
  if (!IDLE_MS) return;
  clearTimeout(idleTimer);
  idleTimer = setTimeout(stop, IDLE_MS);
}

function run(request) {
  return new Promise(resolve => {
    const { id, spec, timeoutMs } = request;
    let child;
    try {
      // detached: git leads a process group of its own (its pid), so the store's process can
      // still kill it if this helper dies under it (grep-helper.mjs #fail) - the helper's timer
      // was the only thing bounding it. The command, cwd and environment are untouched.
      child = spawn(spec.command, spec.argv, { cwd: spec.cwd, env: spec.env, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
    } catch (error) {
      // A git that cannot even be spawned answers the way a shell would: 127, with the reason.
      control({ id, event: 'end', code: 127, killed: false, stderr: String(error?.message ?? error).slice(0, 500), bytes: 0 });
      resolve();
      return;
    }
    current = child;
    control({ id, event: 'spawn', pid: child.pid });
    let bytes = 0, killed = false, paused = false, finished = false;
    const stderr = [];
    const timer = timeoutMs > 0 ? setTimeout(() => { killed = true; child.kill('SIGKILL'); }, timeoutMs) : null;
    const done = (code, note) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      current = undefined;
      const text = note ?? Buffer.concat(stderr).toString('utf8');
      control({ id, event: 'end', code, killed, stderr: text.slice(0, 1000), bytes });
      resolve();
    };
    child.stdout.on('data', chunk => {
      bytes += chunk.length;
      if (!process.stdout.write(chunk) && !paused) {
        paused = true;
        child.stdout.pause();
        process.stdout.once('drain', () => { paused = false; child.stdout.resume(); });
      }
    });
    child.stderr.on('data', chunk => { if (stderr.length < 8) stderr.push(chunk); });
    // A git that fails after the fork is an ordinary non-zero exit, with its own words.
    child.on('error', error => done(127, String(error?.message ?? error)));
    child.on('close', code => done(code));
  });
}

function pump() {
  if (running) return;
  const request = queued.shift();
  if (!request) { armIdle(); return; }
  clearTimeout(idleTimer);            // a request in hand: not idle, however long it runs
  running = run(request).finally(() => { running = undefined; pump(); });
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {
  input += chunk;
  for (let nl = input.indexOf('\n'); nl >= 0; nl = input.indexOf('\n')) {
    const line = input.slice(0, nl);
    input = input.slice(nl + 1);
    if (!line.trim()) continue;
    let request;
    try { request = JSON.parse(line); } catch { continue; }
    queued.push(request);
    pump();
  }
});
process.stdin.on('end', stop);
process.stdin.on('close', stop);
process.stdin.on('error', stop);
process.stdout.on('error', stop);
armIdle();                            // spawned and not yet asked: the same bounded wait applies
