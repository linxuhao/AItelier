import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openaiMessages, installHostModel, rootReasoningEffort } from './model.mjs';

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
