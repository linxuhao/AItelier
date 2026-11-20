/** The witnesses for iss-6df19ff31a3b449b: one failed completion must not end a
 *  resident driver, and an exit that does happen must name the failure class.
 *
 *  Measured on this host: two resident drivers ended `exit code 1` within ten
 *  minutes of each other, each immediately after a completion-retry line that still
 *  had about 165 s of its budget left. The journal could not say whether the driver
 *  was killed by a 500, by an abort or by a socket reset, and a reader could not
 *  tell a deliberate exit from a crash.
 *
 *  Both halves are driven through the code that runs, never re-implemented here:
 *  the failure comes out of the REAL completion bridge (`installHostModel` with an
 *  injected request that answers 500 - only the socket is stubbed), the decision is
 *  the plugin's own `pumpFailure` / `startupFailure`, and the exit line is the
 *  runtime's own deferred diagnosis, installed by its own `apply()` in a child
 *  process so the 'exit' hook really runs.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DriverRuntime } from './runtime.mjs';
import { installHostModel, retryableClass, RETRYABLE_TRANSPORT_CODES } from './model.mjs';
import { failureClass, exitCause, pumpFailure, startupFailure } from '../dsh-director/lib/exit-cause.mjs';

const RUNTIME = new URL('./runtime.mjs', import.meta.url).href;
const EXIT_CAUSE = new URL('../dsh-director/lib/exit-cause.mjs', import.meta.url).href;

/** The failure the production request raises for an answered error status: the
 *  message the operator saw, and the status the classifier reads. */
function httpFailure(status, path = '/api/dsh-drivers/alpha/completion') {
  const error = new Error(`AItelier ${path}: HTTP ${status}`);
  error.status = status;
  return error;
}

/** A transport failure shaped like undici's: a TypeError whose cause carries the code. */
function transportFailure(code, message = 'fetch failed') {
  const error = new TypeError(message);
  const cause = new Error(code === 'UND_ERR_SOCKET' ? 'other side closed' : `connect ${code}`);
  cause.code = code;
  error.cause = cause;
  return error;
}

/** One completion request through the REAL bridge, with only the request stubbed, so
 *  a 500 travels the path it travels in production: the retry loop classifies it,
 *  refuses to retry a verdict, and throws it at the caller. */
function bridgeFor(request, retry = {}) {
  let adapter;
  installHostModel({ llm: { registerAdapter(_names, value) { adapter = value; } }, get() { return undefined; } },
    { LlmAdapter: class {} }, { project: 'alpha', session: 'root', request,
      retry: { budgetMs: 180000, delayMs: 1, maxDelayMs: 1, log: () => {}, ...retry } });
  return adapter;
}

async function drain(adapter) {
  const chunks = [];
  for await (const chunk of adapter.stream({ provider: 'aitelier', model: 'driver', sessionId: 'root',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'work' }] }] })) chunks.push(chunk);
  return chunks;
}

test('an answered 500 from the completion bridge is one failed turn: the pump keeps delivering and no exit is requested', async () => {
  // 1. The failure itself, from the real retry path: an answered 500 is a verdict,
  //    so it surfaces after exactly one request instead of being retried.
  let attempts = 0;
  const adapter = bridgeFor(async () => { attempts += 1; throw httpFailure(500); });
  let completionFailure;
  await assert.rejects(drain(adapter), error => { completionFailure = error; return true; });
  assert.equal(attempts, 1, 'a 500 is a verdict about the request, not a dead socket');
  assert.equal(completionFailure.status, 500, 'the failure keeps its status: the class an exit record needs');

  // 2. That failure thrown out of ONE delivery, on the real event pump. The director's
  //    own wiring is one line - `pump.catch(error => pumpFailure(ctx, error))` - and it
  //    is the same call here, so a rejection really would reach `appExit`.
  const directory = await mkdtemp(join(tmpdir(), 'dsh-exit-cause-'));
  const exits = [], logged = [], attemptsOn = [], reported = [];
  const ctx = { get: name => name === 'appExit' ? code => { exits.push(code); } : undefined,
    logger: { error: line => logged.push(line) } };
  let polls = 0;
  const runtime = new DriverRuntime({ project: 'alpha', driver: 'dsh-fixture', directory, retryDelayMs: 1,
    // The backend replays from the durable cursor: a tick that was not consumed comes
    // back with the SAME seq, which is the id the director recognises a redelivery by.
    request: async (path, args) => {
      if (path.endsWith('/heartbeat')) return {};
      polls += 1;
      if (polls > 4) { runtime.close(); return { timed_out: true, next_after: 9, next_inbox_after: 0 }; }
      const seq = (args.after ?? 0) + 1;
      return { timed_out: false, next_after: seq, next_inbox_after: 0,
        events: [{ seq, event_type: 'node_changed', payload: { seq } }] };
    },
    onDeliveryError: async error => { reported.push(error); },
    onEvent: async raw => {
      const seq = raw.events[0].seq;
      // The first attempt to deliver the first tick is the failed completion.
      attemptsOn.push(seq);
      if (seq === 1 && attemptsOn.filter(value => value === 1).length === 1) throw completionFailure;
    } });
  const outcome = runtime.pump().then(() => 'ended', error => { pumpFailure(ctx, error); return 'ended-failed'; });
  try {
    assert.equal(await outcome, 'ended', `the pump ended by itself: ${JSON.stringify(logged)}`);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
  assert.deepEqual(exits, [], 'a single failed completion must not request a process exit');
  assert.equal(exitCause(), undefined, 'nothing recorded a cause: this process never decided to end');
  assert.equal(reported.length, 1, 'the failed delivery is reported on the host channel, exactly once');
  assert.match(String(reported[0]?.message), /HTTP 500/, 'the report names the failure, status included');
  assert.deepEqual(attemptsOn, [1, 1, 2, 3], 'the failed tick came back under the same id, then the stream served on');
});

test('the exit record names the failure class, not only the exit code', async () => {
  // Each row is one child process: the runtime's own `apply()` installs the real exit
  // diagnosis, the plugin's own decision records the cause and requests the exit, and
  // the 'exit' hook writes the line an operator reads. The assertion is on stderr,
  // because that is the journal.
  const rows = [
    { what: 'an answered 500 at startup', call: 'startupFailure',
      make: `(() => { const error = new Error('AItelier /api/dsh-drivers/alpha/completion: HTTP 500'); error.status = 500; return error; })()`,
      named: /AItelier DSH exit: .*HTTP 500/ },
    { what: 'a reset socket on the event pump', call: 'pumpFailure',
      make: `(() => { const error = new TypeError('fetch failed'); const cause = new Error('other side closed'); cause.code = 'ECONNRESET'; error.cause = cause; return error; })()`,
      named: /AItelier DSH exit: .*connection-level ECONNRESET after_send=true/ },
    { what: 'a cancelled turn', call: 'pumpFailure',
      make: `(() => { const error = new Error('owner cancelled the turn'); error.name = 'AbortError'; return error; })()`,
      named: /AItelier DSH exit: .*abort/ },
  ];
  for (const row of rows) {
    const script = [
      `import { apply } from ${JSON.stringify(RUNTIME)};`,
      `import { pumpFailure, startupFailure } from ${JSON.stringify(EXIT_CAUSE)};`,
      'apply({ provide() {} });',
      `const error = ${row.make};`,
      'const exits = [];',
      `const ctx = { get: name => name === 'appExit' ? code => { exits.push(code); process.exitCode = code; } : undefined,`,
      '  logger: { error() {} } };',
      `${row.call}(ctx, error);`,
      `process.stderr.write('EXITS ' + JSON.stringify(exits) + '\\n');`,
    ].join('\n');
    const { code, stderr } = await runChild(script);
    assert.equal(code, 1, `${row.what}: the process ended with the requested code\n${stderr}`);
    assert.match(stderr, /EXITS \[1\]/, `${row.what}: the exit was requested once\n${stderr}`);
    assert.match(stderr, row.named, `${row.what}: the last record names the class\n${stderr}`);
    // The class is the point: a bare exit code is exactly what could not be read.
    assert.doesNotMatch(stderr, /^AItelier DSH exit: exit code \d+\s*$/m,
      `${row.what}: an exit line with no class is the defect\n${stderr}`);
    const lines = stderr.trim().split('\n');
    assert.match(lines.at(-1), row.named, `${row.what}: the CLASS is in the LAST record\n${stderr}`);
  }
});

test('a generic error names its class once, and a class its cause lacks is still named', async () => {
  // Measured on main (iss-489d473442794c3c, deterministic in 0.53 s): the runtime's
  // own exit line read `AItelier DSH exit: uncaught exception: Error: Error: kaboom`.
  // The class had been prepended to a cause whose first line already begins with it,
  // because `failureClass` falls back to the error's `name` and a stack's first line
  // is `<name>: <message>`. Both halves are witnessed here, because the cheap repair
  // for the first half - dropping the class from the line - makes the second silent:
  // a status, a transport code, an abort and an undecodable body are classes the
  // cause's first line does NOT carry, and they are the whole reason the class exists
  // (the reading two resident drivers could not make on 2026-11-19, iss-6df19ff31a3b449b).
  const rows = [
    // The measured rows, byte for byte.
    { what: 'an uncaught generic error',
      script: 'setTimeout(() => { throw new Error(\'kaboom\'); }, 0);',
      want: 'AItelier DSH exit: uncaught exception: Error: kaboom' },
    { what: 'an unhandled generic rejection',
      script: 'Promise.reject(new Error(\'the session log is corrupt\'));',
      want: 'AItelier DSH exit: unhandled rejection: Error: the session log is corrupt' },
    // Any other name is the same shape: the stack's first line carries it.
    { what: 'an uncaught error whose name is not `Error`',
      script: 'setTimeout(() => { throw new RangeError(\'boom\'); }, 0);',
      want: 'AItelier DSH exit: uncaught exception: RangeError: boom' },
    // The deliberate exit's own line is composed the same way and doubled the same way.
    { what: 'a deliberate exit recording a generic cause',
      script: 'recordExitCause(new Error(\'boom\'), \'events pump\'); process.exit(1);',
      want: 'AItelier DSH exit: exit code 1 (events pump): Error: boom' },
    // The classes that must survive: none of them is a stack line's first token, and
    // each of the four reasons this classifier exists is here at least once.
    { what: 'an uncaught answered 500',
      script: 'setTimeout(() => { throw Object.assign(new Error(\'verdict\'), { status: 500 }); }, 0);',
      want: 'AItelier DSH exit: uncaught exception: HTTP 500: Error: verdict' },
    { what: 'an uncaught reset socket',
      script: 'setTimeout(() => { const error = new TypeError(\'fetch failed\');'
        + ' const cause = new Error(\'other side closed\'); cause.code = \'ECONNRESET\';'
        + ' error.cause = cause; throw error; }, 0);',
      want: 'AItelier DSH exit: uncaught exception: connection-level ECONNRESET '
        + 'after_send=true: TypeError: fetch failed' },
    { what: 'an uncaught cancellation',
      script: 'setTimeout(() => { const error = new Error(\'owner cancelled the turn\');'
        + ' error.name = \'AbortError\'; throw error; }, 0);',
      want: 'AItelier DSH exit: uncaught exception: abort: AbortError: owner cancelled the turn' },
    { what: 'an uncaught undecodable body',
      script: 'setTimeout(() => { throw new SyntaxError(\'Unexpected token < in JSON\'); }, 0);',
      want: 'AItelier DSH exit: uncaught exception: malformed body: '
        + 'SyntaxError: Unexpected token < in JSON' },
  ];
  for (const row of rows) {
    const script = [
      `import { apply } from ${JSON.stringify(RUNTIME)};`,
      `import { recordExitCause } from ${JSON.stringify(EXIT_CAUSE)};`,
      'apply({ provide() {} });',
      row.script,
    ].join('\n');
    const { code, stderr } = await runChild(script);
    assert.equal(code, 1, `${row.what}: the ending is unchanged, only its line\n${stderr}`);
    // The exact line, not a substring: `Error: Error: kaboom` contains the right
    // class and is exactly the defect.
    const named = stderr.split('\n').filter(line => line.startsWith('AItelier DSH exit: '));
    assert.deepEqual(named, [row.want], `${row.what}: one line, the class named once\n${stderr}`);
  }
});

test('the exit class is the retry log\'s class, extended to the failures the retry refuses', () => {
  // Agreement first, so the two classifiers cannot drift: for everything the retry
  // layer retries, the exit record says exactly what the retry line says.
  for (const code of RETRYABLE_TRANSPORT_CODES) {
    assert.equal(failureClass(transportFailure(code)), retryableClass(transportFailure(code)), code);
  }
  for (const status of [502, 503, 504]) {
    assert.equal(failureClass(httpFailure(status)), retryableClass(httpFailure(status)), String(status));
  }
  // Then the verdicts the retry layer deliberately refuses - which are exactly the
  // ones an operator most needs to tell apart when a driver ends.
  assert.equal(failureClass(httpFailure(500)), 'HTTP 500');
  assert.equal(retryableClass(httpFailure(500)), null, 'a 500 is a verdict; the exit record still names it');
  assert.equal(failureClass(Object.assign(new Error('nope'), { status: 401 })), 'HTTP 401');
  assert.equal(failureClass(Object.assign(new Error('cancelled'), { name: 'AbortError' })), 'abort');
  assert.equal(failureClass(new SyntaxError('Unexpected token < in JSON')), 'malformed body');
  assert.equal(failureClass(transportFailure('ENOTFOUND')), 'connection-level ENOTFOUND',
    'a code outside the retried set keeps its own name, without a claim about where it was sent');
  assert.equal(failureClass(Object.assign(new Error('boom'), { name: 'RangeError' })), 'RangeError');
  assert.equal(failureClass(undefined), 'no cause');
});

/** Run one child process to its real end and hand back what it wrote to stderr. */
function runChild(script) {
  return new Promise((resolve, reject) => {
    const child = execFile(process.execPath, ['--input-type=module', '-e', script],
      { encoding: 'utf8', timeout: 30000 }, (error, stdout, stderr) => {
        // A non-zero code is the expected shape here, never a reason to reject.
        if (error && typeof error.code !== 'number') { reject(error); return; }
        resolve({ code: error ? error.code : 0, stdout, stderr });
      });
    child.stdin?.end();
  });
}
