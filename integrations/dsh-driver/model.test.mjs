import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { openaiMessages, installHostModel, modelRetryBudgetMs, rootReasoningEffort } from './model.mjs';

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
