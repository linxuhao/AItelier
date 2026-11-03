import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openaiMessages, installHostModel } from './model.mjs';

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
  assert.deepEqual(payload.messages[0], { role: 'system', content: 'rules' });
  assert.equal(payload.messages[2].reasoning_content, 'prior thought');
  assert.equal(payload.messages[2].tool_calls[0].id, 'call');
  assert.equal(payload.messages[3].tool_call_id, 'call');
  assert.equal(payload.tools[0].function.name, 'wait');
  assert.equal(chunks.find(c => c.type === 'usage').usage.inputTokens, 100);
  assert.ok(chunks.some(c => c.type === 'tool-call-delta' && c.id === 'next'));
  assert.equal(chunks.at(-1).reason.kind, 'tool-calls');
});

test('unsupported dynamic tool blocks fail explicitly', () => {
  assert.throws(() => openaiMessages({ messages: [{ role: 'developer', content: [{ type: 'tool-addition' }] }] }), /Unsupported/);
});
