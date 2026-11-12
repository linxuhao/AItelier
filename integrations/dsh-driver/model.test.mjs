import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { openaiMessages, installHostModel, modelRetryBudgetMs, rootReasoningEffort, retryableClass,
  RETRYABLE_TRANSPORT_CODES, RETRYABLE_TRANSPORT_SHAPES, RETRY_JITTER } from './model.mjs';

test('host bridge preserves tools, tool results, reasoning, session identity and usage', async () => {
  let adapter, payload;
  installHostModel({ llm: { registerAdapter(names, value) { assert.deepEqual(names, ['aitelier']); adapter = value; } } },
    { LlmAdapter: class {} }, { project: 'alpha', session: 'root', async request(path, body, signal) {
      assert.equal(path, '/api/dsh-drivers/alpha/completion'); assert.ok(signal); payload = body;
      return { text: '', reasoning_content: 'check', tool_calls: [{ id: 'next', function: { name: 'wait', arguments: '{}' } }],
        truncated: false, usage: { prompt_tokens: 300, cache_hit_tokens: 200, completion_tokens: 20 } };
    } });
  const options = { provider: 'aitelier', model: 'driver', sessionId: 'native-child', system: 'rules',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'work' }] },
      { role: 'assistant', content: [{ type: 'reasoning', text: 'prior thought' }, { type: 'tool-call', id: 'call', name: 'bash', arguments: '{}' }] },
      { role: 'tool', toolCallId: 'call', content: [{ type: 'text', text: 'done' }] }],
    tools: [{ name: 'wait', description: 'wait', parameters: { type: 'object' } }] };
  const chunks = []; for await (const chunk of adapter.stream(options)) chunks.push(chunk);
  assert.equal(payload.session_id, 'native-child');
  assert.equal(payload.reasoning_effort, 'high', 'a native child keeps high');
  assert.deepEqual(payload.messages[0], { role: 'system', content: 'rules' });
  assert.equal(payload.messages[2].reasoning_content, 'prior thought');
  assert.equal(payload.messages[2].tool_calls[0].id, 'call');
  assert.equal(payload.messages[3].tool_call_id, 'call');
  assert.equal(payload.tools[0].function.name, 'wait');
  assert.equal(chunks.find(c => c.type === 'usage').usage.inputTokens, 100);
  assert.ok(chunks.some(c => c.type === 'tool-call-delta' && c.id === 'next'));
  assert.equal(chunks.at(-1).reason.kind, 'tool-calls');
});

test('the root director defaults to max effort, children to high, and an explicit effort always wins', async () => {
  const sent = [];
  const install = rootEffort => {
    let adapter;
    installHostModel({ llm: { registerAdapter(_names, value) { adapter = value; } } }, { LlmAdapter: class {} },
      { project: 'alpha', session: 'root', ...(rootEffort ? { rootEffort } : {}), async request(_path, body) { sent.push(body); return { text: 'ok', tool_calls: [], usage: {} }; } });
    return adapter;
  };
  const run = async (adapter, extra) => { for await (const _ of adapter.stream({ provider: 'aitelier', model: 'driver', system: 's',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'x' }] }], ...extra })); return sent.at(-1).reasoning_effort; };
  const fallback = install();
  assert.equal(await run(fallback, { sessionId: 'root' }), 'max');
  assert.equal(await run(fallback, {}), 'max', 'no session id means the root session');
  assert.equal(await run(fallback, { sessionId: 'child-1' }), 'high');
  assert.equal(await run(fallback, { sessionId: 'root', reasoningEffort: 'low' }), 'low');
  assert.equal(await run(fallback, { sessionId: 'child-1', reasoningEffort: 'max' }), 'max');
  assert.equal(await run(install('high'), { sessionId: 'root' }), 'high', 'configured root effort');
  assert.deepEqual(['off', 'low', 'high', 'max', undefined, '', 'MAX', 'xhigh'].map(rootReasoningEffort), ['off', 'low', 'high', 'max', 'max', 'max', 'max', 'max']);
});

test('unsupported dynamic tool blocks fail explicitly', () => {
  assert.throws(() => openaiMessages({ messages: [{ role: 'developer', content: [{ type: 'tool-addition' }] }] }), /Unsupported/);
});

test('image blocks are forwarded as image_url parts, never silently downgraded', async () => {
  let adapter, payload;
  const attachments = { async readImageRequest(ref, target, signal) {
    assert.equal(ref.attachmentId, 'img-1'); assert.ok(target.maxBytes > 0);
    return { mediaType: 'image/png', data: new Uint8Array([1, 2, 3]) };
  } };
  installHostModel({ llm: { registerAdapter(_names, value) { adapter = value; } }, get(name) {
    assert.equal(name, 'attachments'); return attachments;
  } }, { LlmAdapter: class {} }, { project: 'alpha', session: 'root', async request(_path, body) {
    payload = body; return { text: 'ok', tool_calls: [], truncated: false, usage: { prompt_tokens: 1, completion_tokens: 1 } };
  } });
  const options = { provider: 'aitelier', model: 'driver', sessionId: 's',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'what is this' },
      { type: 'image', attachment: { attachmentId: 'img-1', name: 'shot.png' } }] }] };
  const chunks = []; for await (const chunk of adapter.stream(options)) chunks.push(chunk);
  const content = payload.messages[0].content;
  assert.equal(content[0].text, 'what is this');
  assert.equal(content[1].type, 'image_url');
  assert.equal(content[1].image_url.url, 'data:image/png;base64,AQID');
});

test('an image without the attachment service fails loudly', async () => {
  let adapter;
  installHostModel({ llm: { registerAdapter(_names, value) { adapter = value; } }, get() { return undefined; } },
    { LlmAdapter: class {} }, { project: 'alpha', session: 'root', async request() { throw new Error('must not be called'); } });
  const options = { provider: 'aitelier', model: 'driver', sessionId: 's',
    messages: [{ role: 'user', content: [{ type: 'image', attachment: { attachmentId: 'img-1' } }] }] };
  await assert.rejects(async () => { for await (const _ of adapter.stream(options)) { /* drain */ } }, /attachment service is not mounted/);
});

test('the route declares image input', async () => {
  let adapter;
  installHostModel({ llm: { registerAdapter(_names, value) { adapter = value; } }, get() { return {}; } },
    { LlmAdapter: class {} }, { project: 'alpha', session: 'root', async request() { return {}; } });
  const info = await adapter.resolveModel('aitelier', 'driver');
  assert.deepEqual(info.inputModalities, ['text', 'image']);
});

// The witness for the retry is a REAL loopback HTTP server reached through a
// real `fetch`, so what the adapter retries is the same failure a recreated
// backend container produces: the port refuses, or the socket dies before any
// response. Only the delays are shortened.
const TURN = { reasoning_content: 'think', text: 'answer',
  tool_calls: [{ id: 'call-1', function: { name: 'bash', arguments: '{"command":"ls"}' } }],
  truncated: false, usage: { prompt_tokens: 40, cache_hit_tokens: 10, completion_tokens: 6 } };

function turnOptions(extra = {}) {
  return { provider: 'aitelier', model: 'driver', sessionId: 'child-1',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'work' }] }], ...extra };
}

async function collect(adapter, options) {
  const chunks = [];
  for await (const chunk of adapter.stream(options)) chunks.push(chunk);
  return chunks;
}

async function loopback(handler) {
  const hits = { count: 0, paths: [] };
  const server = createServer((req, res) => {
    hits.count += 1;
    hits.paths.push(req.url);
    handler(req, res, hits.count);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { hits, base: `http://127.0.0.1:${server.address().port}/`,
    async close() { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}

function answer(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(text) });
  res.end(text);
}

function adapterFor(base, retry = {}) {
  let adapter;
  installHostModel({ llm: { registerAdapter(_names, value) { adapter = value; } }, get() { return undefined; } },
    { LlmAdapter: class {} }, { project: 'alpha', session: 'root',
      async request(path, body, signal) {
        const response = await fetch(new URL(path, base), { method: 'POST',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });
        if (!response.ok) {
          const error = new Error(`AItelier ${path}: HTTP ${response.status}`);
          error.status = response.status;
          throw error;
        }
        return response.json();
      },
      retry: { delayMs: 5, maxDelayMs: 20, log: () => {}, ...retry } });
  return adapter;
}

test('a completion whose socket dies before any response is retried and delivered', async () => {
  const server = await loopback((req, res, count) => {
    if (count <= 2) { req.socket.destroy(); return; }
    answer(res, 200, TURN);
  });
  try {
    const chunks = await collect(adapterFor(server.base), turnOptions());
    assert.equal(server.hits.count, 3, 'the first two dead sockets plus the delivered answer');
    assert.deepEqual(new Set(server.hits.paths), new Set(['/api/dsh-drivers/alpha/completion']),
      'every attempt asks for the same completion, so a retry cannot duplicate a tool call');
    assert.ok(chunks.some(chunk => chunk.type === 'finish'));
  } finally { await server.close(); }
});

test('a zero budget surfaces the connection failure after exactly one request', async () => {
  const server = await loopback((req, res, count) => {
    if (count <= 2) { req.socket.destroy(); return; }
    answer(res, 200, TURN);
  });
  try {
    await assert.rejects(collect(adapterFor(server.base, { budgetMs: 0 }), turnOptions()), error => {
      assert.equal(error.name, 'TypeError', 'the connection-level failure itself, not a wrapper');
      assert.equal(error.cause?.code, 'UND_ERR_SOCKET');
      return true;
    });
    assert.equal(server.hits.count, 1);
  } finally { await server.close(); }
});

test('an answered HTTP 400 or 401 is never retried and keeps its status and name', async () => {
  for (const status of [400, 401]) {
    const server = await loopback((req, res) => answer(res, status, { detail: 'no' }));
    const logged = [];
    try {
      await assert.rejects(collect(adapterFor(server.base, { budgetMs: 60000, log: line => logged.push(line) }), turnOptions()), error => {
        assert.equal(error.status, status);
        assert.equal(error.name, 'Error');
        return true;
      });
      assert.equal(server.hits.count, 1, `HTTP ${status} is a verdict, not a dead connection`);
      assert.deepEqual(logged, [], 'only a retry is logged');
    } finally { await server.close(); }
  }
});

test('an abort while waiting to retry ends the call without another request', async () => {
  const server = await loopback(req => req.socket.destroy());
  try {
    const controller = new AbortController();
    const adapter = adapterFor(server.base, { budgetMs: 60000, delayMs: 300, maxDelayMs: 300 });
    setTimeout(() => controller.abort(), 60);
    await assert.rejects(collect(adapter, turnOptions({ signal: controller.signal })),
      error => { assert.equal(error.name, 'AbortError'); return true; });
    assert.equal(server.hits.count, 1, 'the pending retry never reached the server');
  } finally { await server.close(); }
});

test('a gateway 502 is retried with a logged attempt and the third answer is delivered', async () => {
  const server = await loopback((req, res, count) => {
    if (count <= 2) { answer(res, 502, { detail: 'bad gateway' }); return; }
    answer(res, 200, TURN);
  });
  const logged = [];
  try {
    const chunks = await collect(adapterFor(server.base, { budgetMs: 60000, log: line => logged.push(line) }), turnOptions());
    assert.equal(server.hits.count, 3);
    assert.ok(chunks.some(chunk => chunk.type === 'finish'));
    assert.equal(logged.length, 2, 'one line per retry, no other logging');
    assert.match(logged[0], /HTTP 502/);
    assert.match(logged[0], /attempt 1/);
    assert.match(logged[0], /retry budget/);
    assert.match(logged[1], /attempt 2/);
  } finally { await server.close(); }
});

test('a turn that never gets an answer inside its budget delivers zero chunks', async () => {
  const server = await loopback(req => req.socket.destroy());
  try {
    const chunks = [];
    await assert.rejects(async () => {
      for await (const chunk of adapterFor(server.base, { budgetMs: 200 }).stream(turnOptions())) chunks.push(chunk);
    }, error => { assert.equal(error.name, 'TypeError'); return true; });
    assert.deepEqual(chunks, []);
    assert.ok(server.hits.count > 1, 'it did retry before giving up');
  } finally { await server.close(); }
});

test('a turn delivered after retries yields exactly the chunks of a first-attempt turn', async () => {
  const clean = await loopback((req, res) => answer(res, 200, TURN));
  const flaky = await loopback((req, res, count) => {
    if (count === 1) { req.socket.destroy(); return; }
    answer(res, 200, TURN);
  });
  try {
    const first = await collect(adapterFor(clean.base), turnOptions());
    const retried = await collect(adapterFor(flaky.base, { budgetMs: 60000 }), turnOptions());
    assert.equal(flaky.hits.count, 2);
    assert.deepEqual(retried, first);
  } finally { await clean.close(); await flaky.close(); }
});

test('the retry budget helper reads AITELIER_DSH_MODEL_RETRY_SEC', () => {
  const saved = process.env.AITELIER_DSH_MODEL_RETRY_SEC;
  try {
    delete process.env.AITELIER_DSH_MODEL_RETRY_SEC;
    assert.equal(modelRetryBudgetMs(), 180000, 'the default budget is 180 s');
    assert.equal(modelRetryBudgetMs('2.5'), 2500);
    assert.equal(modelRetryBudgetMs('0'), 0);
    assert.equal(modelRetryBudgetMs('abc'), 180000);
    assert.equal(modelRetryBudgetMs('-1'), 180000);
    assert.equal(modelRetryBudgetMs(''), 180000);
    process.env.AITELIER_DSH_MODEL_RETRY_SEC = '4.5';
    assert.equal(modelRetryBudgetMs(), 4500, 'the default argument is the environment');
  } finally {
    if (saved === undefined) delete process.env.AITELIER_DSH_MODEL_RETRY_SEC;
    else process.env.AITELIER_DSH_MODEL_RETRY_SEC = saved;
  }
});

test('a 300 ms budget stops the attempts within a second of wall clock', async () => {
  const server = await loopback(req => req.socket.destroy());
  try {
    const started = Date.now();
    await assert.rejects(collect(adapterFor(server.base, { budgetMs: 300 }), turnOptions()));
    const elapsed = Date.now() - started;
    assert.ok(elapsed < 1000, `gave up after ${elapsed}ms`);
    assert.ok(server.hits.count >= 1);
  } finally { await server.close(); }
});

test('the documented retry budget is the code default, read out of docs/dsh-driver.md', () => {
  // A number typed into this file twice proves nothing: an edit that changed
  // only the doc would stay green. The row is the document's own claim, so it
  // is read from there and compared with the helper's built-in default.
  const doc = readFileSync(new URL('../../docs/dsh-driver.md', import.meta.url), 'utf8');
  const rows = doc.split('\n')
    .map(line => line.trim())
    .filter(line => line.startsWith('|') && line.endsWith('|'))
    .map(line => line.slice(1, -1).split('|').map(cell => cell.trim()))
    .filter(cells => cells.length >= 3 && cells[0].replace(/`/g, '') === 'AITELIER_DSH_MODEL_RETRY_SEC');
  assert.equal(rows.length, 1, 'exactly one documented row names the retry budget');
  const documented = /default\s+(\d+(?:\.\d+)?)/.exec(rows[0][2]);
  assert.ok(documented, `the row states a default: ${rows[0][2]}`);
  const saved = process.env.AITELIER_DSH_MODEL_RETRY_SEC;
  try {
    delete process.env.AITELIER_DSH_MODEL_RETRY_SEC;
    assert.equal(modelRetryBudgetMs(undefined), Number(documented[1]) * 1000,
      'the code default is the documented one');
    assert.equal(modelRetryBudgetMs('0'), 0, 'the same row says 0 disables the retry');
  } finally {
    if (saved === undefined) delete process.env.AITELIER_DSH_MODEL_RETRY_SEC;
    else process.env.AITELIER_DSH_MODEL_RETRY_SEC = saved;
  }
});

// ---------------------------------------------------------------------------
// Witnesses for the seven retry gaps (issue iss-429a766b26074bd0). Each
// property has its own test, and each one is written so that the mutant it
// exists for turns it red: a logger that is not passed, a deadline taken at the
// call, a pause slept after the last failure, a classifier that retries every
// TypeError, an unlabelled after-send reset, a fixed backoff, and an abort
// replaced by the transport error it raced.
// ---------------------------------------------------------------------------

/** A transport failure shaped like undici's: a TypeError whose cause carries
 *  the code (and, for a reset, the half-close message). */
function transportError(code, message = 'fetch failed') {
  const error = new TypeError(message);
  const cause = new Error(code === 'UND_ERR_SOCKET' ? 'other side closed' : `connect ${code}`);
  cause.code = code;
  error.cause = cause;
  return error;
}

/** An adapter over an injected `request`, so a failure can be produced without
 *  a socket and the clock, the sleep and the randomness can be pinned. */
function injectedAdapter(request, retry = {}) {
  let adapter;
  installHostModel({ llm: { registerAdapter(_names, value) { adapter = value; } }, get() { return undefined; } },
    { LlmAdapter: class {} }, { project: 'alpha', session: 'root', request,
      retry: { delayMs: 5, maxDelayMs: 20, log: () => {}, ...retry } });
  return adapter;
}

/** A clock the retry loop cannot advance behind the witnesses' back: `sleep`
 *  records the pause it was given and only then moves time. */
function fakeClock() {
  const clock = { t: 0, slept: [], now: () => clock.t };
  clock.sleep = async ms => { clock.slept.push(ms); clock.t += ms; };
  return clock;
}

const RETRY_LINE = /^dsh completion attempt (\d+) failed \((.+)\); retrying in (\d+)ms with (-?\d+)ms of the retry budget left$/;

/** Read one retry line as the four facts an operator (and this suite) needs. */
function parseRetryLine(line) {
  const match = RETRY_LINE.exec(line);
  assert.ok(match, `a retry line states attempt, class, slept ms and remaining budget: ${line}`);
  return { attempt: Number(match[1]), class: match[2], slept: Number(match[3]), remaining: Number(match[4]) };
}

/** Collect what a run writes to stderr; nothing else in the process may leak
 *  into the counts, so callers filter by their own prefix. */
async function captureStderr(run) {
  const written = [];
  const original = process.stderr.write;
  process.stderr.write = (chunk, ...rest) => {
    written.push(String(chunk));
    const callback = rest.find(value => typeof value === 'function');
    if (callback) callback();
    return true;
  };
  try { await run(); } finally { process.stderr.write = original; }
  return written;
}

/** The argument text of the first `callee(` call, read structurally (balanced
 *  brackets, strings skipped) out of the real source. Not a comment scan: what
 *  it returns is the code the file actually runs. */
function callArguments(source, callee) {
  const at = source.indexOf(`${callee}(`);
  assert.notEqual(at, -1, `${callee}( is called in the read source`);
  return balancedSlice(source, at + callee.length, '(', ')');
}

function balancedSlice(source, start, open, close) {
  assert.equal(source[start], open, `the read starts on a ${open}`);
  let depth = 0;
  for (let i = start; i < source.length; i += 1) {
    const char = source[i];
    if (char === '"' || char === "'" || char === '`') { i = endOfString(source, i); continue; }
    if (char === open) depth += 1;
    else if (char === close) { depth -= 1; if (depth === 0) return source.slice(start + 1, i); }
  }
  throw new Error(`unbalanced ${open}${close} in the read source`);
}

function endOfString(source, start) {
  const quote = source[start];
  for (let i = start + 1; i < source.length; i += 1) {
    if (source[i] === '\\') { i += 1; continue; }
    if (source[i] === quote) return i;
  }
  throw new Error('unterminated string in the read source');
}

/** The value expression of `key:` inside a slice of source: an object literal
 *  in full, or whatever runs up to the enclosing comma/bracket. */
function propertyValue(text, key) {
  const match = new RegExp(`(?:^|[\\s{,])${key}:\\s*`).exec(text);
  assert.ok(match, `${key}: is present in the read source`);
  const start = match.index + match[0].length;
  if (text[start] === '{') return balancedSlice(text, start, '{', '}');
  let i = start, depth = 0;
  for (; i < text.length; i += 1) {
    const char = text[i];
    if (char === '"' || char === "'" || char === '`') { i = endOfString(text, i); continue; }
    if (char === '(' || char === '[' || char === '{') depth += 1;
    else if (char === ')' || char === ']' || char === '}') { if (depth === 0) break; depth -= 1; }
    else if (char === ',' && depth === 0) break;
  }
  return text.slice(start, i).trim();
}

/** The body of the first `function name(` declaration in the source. */
function functionBody(source, name) {
  const at = source.indexOf(`function ${name}(`);
  assert.notEqual(at, -1, `function ${name} is declared in the read source`);
  const parameters = balancedSlice(source, at + `function ${name}`.length, '(', ')');
  const close = at + `function ${name}`.length + parameters.length + 1;
  const open = source.indexOf('{', close);
  return balancedSlice(source, open, '{', '}');
}

/** Every backticked token the source's own prose carries after `phrase`, up to
 *  the end of that comment paragraph. The document is the authority the code
 *  has to match, so the witness reads the document. */
function backtickedAfter(source, phrase) {
  const at = source.indexOf(phrase);
  assert.notEqual(at, -1, `the source documents "${phrase}"`);
  const found = [];
  for (const line of source.slice(at).split('\n')) {
    const ticks = [...line.matchAll(/`([^`\n]+)`/g)].map(match => match[1]);
    if (ticks.length === 0) { if (found.length) break; else continue; }
    found.push(...ticks);
  }
  return found;
}

/** A port nothing listens on any more, so a connection is refused rather than
 *  reset: the one retried failure that provably never reached a backend. */
async function closedPort() {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  await new Promise(resolve => server.close(resolve));
  return port;
}

test('a transport failing N times logs exactly N retry lines, each with the attempt, class, slept ms and budget left', async () => {
  const server = await loopback((req, res, count) => {
    if (count <= 3) { req.socket.destroy(); return; }
    answer(res, 200, TURN);
  });
  const logged = [];
  try {
    const chunks = await collect(adapterFor(server.base, { budgetMs: 60000, log: line => logged.push(line) }), turnOptions());
    assert.equal(server.hits.count, 4, 'three dead sockets and the delivered answer');
    assert.equal(logged.length, 3, 'exactly one line per retry, none for the delivered attempt');
    const lines = logged.map(parseRetryLine);
    assert.deepEqual(lines.map(line => line.attempt), [1, 2, 3], 'the attempt number counts up from one');
    for (const line of lines) assert.match(line.class, /^connection-level UND_ERR_SOCKET after_send=true$/);
    // The logged pause is the one that was slept, inside the declared bounds of
    // the exponential base the loop had reached (5, 10, 20 ms here).
    const bases = [5, 10, 20];
    lines.forEach((line, index) => {
      assert.ok(line.slept >= bases[index] * RETRY_JITTER.min && line.slept <= bases[index] * RETRY_JITTER.max,
        `pause ${line.slept}ms fits the ${bases[index]}ms base`);
    });
    // The budget left shrinks and never leaves the declared window.
    assert.ok(lines[0].remaining <= 60000 && lines[0].remaining > 59000, `first line states the budget: ${lines[0].remaining}`);
    assert.ok(lines[1].remaining < lines[0].remaining && lines[2].remaining < lines[1].remaining);
    assert.ok(chunks.some(chunk => chunk.type === 'finish'));
  } finally { await server.close(); }
});

test('the real runtime boot hands installHostModel a logger that writes to stderr, not the silent default', async () => {
  // The call site itself: the retry options `boot` passes must come from the
  // driver's own retry options, and those must name a logger function. Every
  // hop is read out of the code that runs, so an omitted `log`, an inline no-op
  // or a comment claiming one all fail here.
  const source = readFileSync(new URL('./runtime.mjs', import.meta.url), 'utf8');
  const call = callArguments(source, 'installHostModel');
  const retryValue = propertyValue(call, 'retry');
  assert.match(retryValue, /^modelRetryOptions\(env\)$/, `boot installs the driver's retry options: retry: ${retryValue}`);
  const named = /(?:^|[\s{,])log:\s*([A-Za-z_$][\w$]*)/.exec(functionBody(source, 'modelRetryOptions'));
  assert.ok(named, 'the retry options name a logger function, not an inline no-op');

  const runtime = await import('./runtime.mjs');
  assert.equal(typeof runtime[named[1]], 'function', `runtime.mjs exports ${named[1]}`);
  // The object boot installs carries that very function — not the module's
  // fallback logger, and not a fresh closure invented per call.
  const options = runtime.modelRetryOptions({ AITELIER_DSH_MODEL_RETRY_SEC: '2.5' });
  assert.equal(options.budgetMs, 2500, 'the documented budget still comes from the environment');
  assert.equal(options.log, runtime[named[1]], 'the installed retry options use the call site logger');

  // ...and it really writes one line per call to stderr.
  const direct = await captureStderr(() => { options.log('probe line'); });
  assert.equal(direct.length, 1);
  assert.match(direct[0], /probe line/);
  assert.ok(direct[0].endsWith('\n'), 'one line, terminated');

  // End to end: the retry loop, installed with exactly the options boot builds,
  // puts one line per retry on stderr (shortened delays only).
  const attempts = { count: 0 };
  const adapter = injectedAdapter(async () => {
    attempts.count += 1;
    if (attempts.count <= 2) throw transportError('ECONNRESET');
    return TURN;
  }, { budgetMs: 60000, delayMs: 1, maxDelayMs: 2, ...runtime.modelRetryOptions({}) });
  const written = await captureStderr(() => collect(adapter, turnOptions()));
  assert.equal(attempts.count, 3);
  const retryLines = written.filter(line => line.includes('dsh completion attempt'));
  assert.equal(retryLines.length, 2, `one line per retry, from the runtime's own logger: ${JSON.stringify(written)}`);
  for (const line of retryLines) assert.ok(line.startsWith('AItelier DSH model retry: '), 'the line is identifiable on the driver stderr');
});

test('an install that names no logger still reports every retry on stderr', async () => {
  // Defence in depth for the second half of the gap: the module's own default
  // was a no-op, so nothing about a retry could ever reach an operator. The
  // default is now stderr, which is what a caller that does not build its own
  // options gets.
  let attempts = 0;
  const adapter = injectedAdapter(async () => {
    attempts += 1;
    if (attempts <= 2) throw transportError('ECONNRESET');
    return TURN;
  }, { budgetMs: 60000, delayMs: 1, maxDelayMs: 2, log: undefined });
  const written = await captureStderr(() => collect(adapter, turnOptions()));
  assert.equal(attempts, 3);
  const retryLines = written.filter(line => line.includes('dsh completion attempt'));
  assert.equal(retryLines.length, 2, `the default logger is stderr, not silence: ${JSON.stringify(written)}`);
  for (const line of retryLines) assert.ok(line.endsWith('\n') && !line.includes('AItelier DSH model retry:'),
    'the module default writes its own line, not the driver prefix');
});

test('the retry budget starts at the first failure, so a slow successful attempt leaves it whole', async () => {
  const clock = fakeClock();
  const logged = [];
  let attempts = 0;
  const adapter = injectedAdapter(async () => {
    attempts += 1;
    if (attempts === 1) clock.t += 4000; // a slow first attempt, far past the 1000 ms budget
    if (attempts <= 2) throw transportError('ECONNRESET');
    return TURN;
  }, { budgetMs: 1000, delayMs: 100, maxDelayMs: 100, random: () => 1,
    now: clock.now, sleep: clock.sleep, log: line => logged.push(line) });
  const chunks = await collect(adapter, turnOptions());
  assert.equal(attempts, 3, 'both retries still happened: the 4 s attempt did not spend the budget');
  assert.deepEqual(logged.map(line => parseRetryLine(line).remaining), [1000, 900],
    'the first failure opens the budget with all of it; the next has slept exactly one 100 ms pause');
  assert.deepEqual(clock.slept, [100, 100]);
  assert.ok(chunks.some(chunk => chunk.type === 'finish'));
});

test('no pause is slept after the final failed attempt', async () => {
  const clock = fakeClock();
  const logged = [];
  let attempts = 0;
  const adapter = injectedAdapter(async () => { attempts += 1; throw transportError('ECONNRESET'); },
    { budgetMs: 1000, delayMs: 100, maxDelayMs: 100, random: () => 1,
      now: clock.now, sleep: clock.sleep, log: line => logged.push(line) });
  await assert.rejects(collect(adapter, turnOptions()), error => {
    assert.equal(error.name, 'TypeError');
    assert.equal(error.cause?.code, 'ECONNRESET');
    return true;
  });
  assert.equal(attempts, 10, 'nine retries fit a 1000 ms budget in 100 ms pauses');
  assert.equal(logged.length, 9, 'announced retries equal attempts that followed a pause');
  assert.deepEqual(clock.slept, Array(9).fill(100), 'nine pauses, none after the last failure');
  assert.equal(clock.t, 900, 'the tenth failure surfaced without sleeping the last 100 ms');
});

test('jitter inside the declared bounds: the mapping is exact at both ends and never exceeds the ceiling', async () => {
  assert.ok(RETRY_JITTER.min > 0, 'a jittered pause is never zero');
  assert.ok(RETRY_JITTER.min <= RETRY_JITTER.max && RETRY_JITTER.max <= 1, 'the declared bounds are ordered and bounded by the base');
  for (const [sample, expected] of [[0, 50], [0.5, 75], [1, 100]]) {
    const clock = fakeClock();
    const logged = [];
    let attempts = 0;
    const adapter = injectedAdapter(async () => {
      attempts += 1;
      if (attempts <= 2) throw transportError('ECONNRESET');
      return TURN;
    }, { budgetMs: 60000, delayMs: 100, maxDelayMs: 100, random: () => sample,
      now: clock.now, sleep: clock.sleep, log: line => logged.push(line) });
    await collect(adapter, turnOptions());
    assert.deepEqual(logged.map(line => parseRetryLine(line).slept), [expected, expected],
      `random()=${sample} sleeps ${expected}ms of the 100ms base`);
    assert.deepEqual(clock.slept, [expected, expected], 'the logged figure is the pause that happened');
  }
  // maxDelayMs stays the ceiling: the base is capped before jitter, never after.
  const clock = fakeClock();
  const logged = [];
  let attempts = 0;
  const adapter = injectedAdapter(async () => {
    attempts += 1;
    if (attempts <= 4) throw transportError('ECONNRESET');
    return TURN;
  }, { budgetMs: 60000, delayMs: 100, maxDelayMs: 150, random: () => 1,
    now: clock.now, sleep: clock.sleep, log: line => logged.push(line) });
  await collect(adapter, turnOptions());
  assert.deepEqual(logged.map(line => parseRetryLine(line).slept), [100, 150, 150, 150],
    'the pause doubles into the declared ceiling and stops there');
});

test('real jitter varies and every sampled pause stays inside the declared bounds', async () => {
  const clock = fakeClock();
  const logged = [];
  let attempts = 0;
  const adapter = injectedAdapter(async () => {
    attempts += 1;
    if (attempts <= 200) throw transportError('ECONNRESET');
    return TURN;
  }, { budgetMs: 1e9, delayMs: 100, maxDelayMs: 400, now: clock.now, sleep: clock.sleep, log: line => logged.push(line) });
  await collect(adapter, turnOptions());
  const slept = logged.map(line => parseRetryLine(line).slept);
  assert.equal(slept.length, 200);
  let base = 100;
  for (const [index, ms] of slept.entries()) {
    assert.ok(ms >= base * RETRY_JITTER.min && ms <= base * RETRY_JITTER.max,
      `pause ${index} = ${ms}ms is inside [${base * RETRY_JITTER.min}, ${base * RETRY_JITTER.max}]`);
    base = Math.min(base * 2, 400);
  }
  const saturated = slept.slice(3);
  assert.ok(new Set(slept).size > 3, 'a fixed backoff would repeat one value');
  assert.ok(Math.min(...saturated) >= 200 && Math.min(...saturated) < 400, 'the low end of the bound is reachable');
  assert.ok(Math.max(...saturated) > 200 && Math.max(...saturated) <= 400, 'the high end of the bound is reachable');
  assert.deepEqual(slept, clock.slept, 'every logged pause is the pause that was taken');
});

test('an abort that coincides with a connection error surfaces the abort, not the socket TypeError', async () => {
  // Two race outcomes a real fetch produces, both of which used to surface as
  // something other than an abort: the socket error winning the race, and the
  // caller's own plain-Error reason being thrown verbatim.
  const racers = {
    'the socket TypeError': () => transportError('UND_ERR_SOCKET'),
    "the caller's plain reason": () => new Error('owner cancelled the turn'),
  };
  for (const [what, thrown] of Object.entries(racers)) {
    const controller = new AbortController();
    const logged = [];
    let attempts = 0;
    const adapter = injectedAdapter(async () => {
      attempts += 1;
      controller.abort(new Error('owner cancelled the turn'));
      throw thrown();
    }, { budgetMs: 60000, delayMs: 5, maxDelayMs: 5, log: line => logged.push(line) });
    await assert.rejects(collect(adapter, turnOptions({ signal: controller.signal })), error => {
      assert.equal(error.name, 'AbortError', `${what} must not become the surfaced error`);
      assert.equal(error.cause?.message, 'owner cancelled the turn', 'the reason is kept as the cause');
      return true;
    });
    assert.equal(attempts, 1, `${what}: an abort is not retried`);
    assert.deepEqual(logged, [], 'an abort is a decision, never a retry');
  }
});

test('a reset after the request was sent is still retried, and its line carries the after-send label', async () => {
  const server = await loopback((req, res, count) => {
    if (count === 1) { req.socket.destroy(); return; } // the request arrived; the answer never did
    answer(res, 200, TURN);
  });
  const logged = [];
  try {
    const chunks = await collect(adapterFor(server.base, { budgetMs: 60000, log: line => logged.push(line) }), turnOptions());
    assert.equal(server.hits.count, 2, 'a reset that may already have run server-side is retried deliberately');
    assert.equal(logged.length, 1);
    const line = parseRetryLine(logged[0]);
    assert.equal(line.class, 'connection-level UND_ERR_SOCKET after_send=true',
      'the operator can see that a second server-side execution is possible');
    assert.ok(chunks.some(chunk => chunk.type === 'finish'));
  } finally { await server.close(); }
});

test('a refused connection is retried and labelled after_send=false: it never reached the backend', async () => {
  const port = await closedPort();
  const logged = [];
  const adapter = adapterFor(`http://127.0.0.1:${port}/`, { budgetMs: 200, delayMs: 20, maxDelayMs: 20, log: line => logged.push(line) });
  await assert.rejects(collect(adapter, turnOptions()), error => {
    assert.equal(error.name, 'TypeError');
    assert.equal(error.cause?.code, 'ECONNREFUSED');
    return true;
  });
  assert.ok(logged.length >= 2, `a refusal inside the budget is retried: ${logged.length}`);
  assert.deepEqual(new Set(logged.map(line => parseRetryLine(line).class)),
    new Set(['connection-level ECONNREFUSED after_send=false']),
    'nothing was established, so nothing can have executed server-side');
});

test('a TypeError whose cause is a malformed body is a verdict, never a retried transport failure', async () => {
  const logged = [];
  let attempts = 0;
  const adapter = injectedAdapter(async () => {
    attempts += 1;
    const error = new TypeError('fetch failed');
    error.cause = new TypeError('Malformed body: Unexpected token < in JSON at position 0');
    throw error;
  }, { budgetMs: 60, delayMs: 5, maxDelayMs: 10, log: line => logged.push(line) });
  await assert.rejects(collect(adapter, turnOptions()), error => {
    assert.equal(error.name, 'TypeError');
    assert.match(error.cause.message, /Malformed body/);
    return true;
  });
  assert.equal(attempts, 1, 'asking the same malformed question again cannot answer it');
  assert.deepEqual(logged, []);
});

test('the classifier retries exactly the documented transport set and nothing else', () => {
  const source = readFileSync(new URL('./model.mjs', import.meta.url), 'utf8');
  assert.deepEqual(backtickedAfter(source, 'the documented set'), [...RETRYABLE_TRANSPORT_CODES],
    'the classifier is the set its own documentation names');
  assert.deepEqual(backtickedAfter(source, 'documented shapes'), [...RETRYABLE_TRANSPORT_SHAPES],
    'the half-close shapes are the documented ones');
  for (const code of RETRYABLE_TRANSPORT_CODES) {
    assert.equal(retryableClass(transportError(code)),
      `connection-level ${code} after_send=${code !== 'ECONNREFUSED'}`, `${code} is retried`);
  }
  for (const code of ['ENOTFOUND', 'EAI_AGAIN', 'ECONNABORTED', 'ERR_STREAM_PREMATURE_CLOSE', 'UND_ERR_ABORTED', 'UND_ERR_HEADERS_TIMEOUT']) {
    assert.equal(retryableClass(transportError(code)), null, `${code} is not part of the documented set`);
  }
  assert.equal(retryableClass(new TypeError('socket hang up')), 'connection-level socket hang up after_send=true');
  assert.equal(retryableClass(new TypeError('terminated')), 'connection-level terminated after_send=true');
  assert.equal(retryableClass(new TypeError('terminated: other side closed')), 'connection-level terminated after_send=true');
  assert.equal(retryableClass(new TypeError('fetch failed')), null, 'being a TypeError is not a retry reason');
  assert.equal(retryableClass(new TypeError('Converting circular structure to JSON')), null);
  assert.equal(retryableClass(new TypeError('a body terminated by the caller is not a half-close')), null,
    'the documented shape is matched as the message or its prefix, never as prose');
  assert.equal(retryableClass(Object.assign(new Error('AItelier /completion: HTTP 400'), { status: 400 })), null);
  assert.equal(retryableClass(Object.assign(new Error('AItelier /completion: HTTP 503'), { status: 503 })), 'HTTP 503');
});
