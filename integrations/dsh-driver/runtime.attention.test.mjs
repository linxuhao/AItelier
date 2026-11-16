/** Plugin step 8 on the measured DSH pin: the director asks its owner with ask_owner on
 *  the built-in local host, the owner answers by dropping inbox/answer-<id>.json, and
 *  the director is woken by one event naming the question and carrying the answer. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutCallerMode } from './dsh-entry.mjs';

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.ATT_TEST_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-attention-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.ATT_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) });
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
export function apply(ctx) {
  const done = new Set();
  const once = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      let block;
      if (!options.tools?.length || options.sessionId !== 'att-root') block = { type: 'text', text: 'Fixture session' };
      else block = once('ask', () => tool('d-ask', 'ask_owner', { question_id: 'restart-q', question: 'May I restart the host?', context: 'two workers idle' }))
        ?? once('retry', () => tool('d-retry', 'ask_owner', { question_id: 'restart-q', question: 'May I restart the host?' }))
        ?? once('asked', () => ({ type: 'text', text: 'ASKED' }))
        ?? (text.includes('Owner answered') ? once('got', () => ({ type: 'text', text: 'GOT_ANSWER' })) : undefined)
        ?? { type: 'text', text: 'Director idle.' };
      yield { type: 'block-start', index: 0, blockType: block.type };
      if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text };
      else yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments };
      yield { type: 'block-end', index: 0, block };
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } };
      yield { type: 'finish', reason: { kind: block.type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
  ctx.on('session/event', (session, event) => {
    if (session.id === 'att-root' && ['tool/result', 'user/message', 'assistant/message'].includes(event.type))
      void post('/test/event', event).catch(() => {});
  });
}
`;

test('real pinned DSH: ask_owner asks once (a retry asks nothing new), the owner answers by file, and the director is woken naming the question', { timeout: 120000 }, async t => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-attention-'));
  const events = [];
  let output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    if (req.url === '/test/event') events.push(JSON.parse(Buffer.concat(chunks)));
    res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const result = id => events.find(e => e.type === 'tool/result' && e.data.message.toolCallId === id);
  const text = id => result(id)?.data.message.content.map(b => b.text ?? '').join('');
  const said = what => events.some(e => e.type === 'assistant/message' && JSON.stringify(e.data).includes(what));
  const until = async (what, met, ms = 60000) => {
    const deadline = Date.now() + ms;
    while (!met() && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(met(), what + '\n' + JSON.stringify(events.map(e => [e.type, e.data?.message?.toolCallId ?? e.data?.source?.kind ?? e.data?.message?.source?.kind ?? '', JSON.stringify(e.data).slice(0, 160)])) + '\n' + output.slice(-3000));
  };
  try {
    const root = join(directory, 'director');
    await mkdir(join(root, 'inbox'), { recursive: true });
    await writeFile(join(root, 'inbox', 'start.json'), JSON.stringify({ task: 'ask the owner' }));
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-attention-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-attention-provider.mjs') },
        { id: 'dsh-director', name: join(here, '../dsh-director/index.mjs') },
        { id: 'director-local', name: join(here, '../dsh-director/hosts/local.mjs'), config: { root, sessionId: 'att-root',
          provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100, idleReviewMs: 600000 } }] },
    ]));
    const environment = Object.fromEntries(Object.entries(withoutCallerMode(process.env)).filter(([k]) => !k.startsWith('AITELIER_')));
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...environment,
      DSH_HOME: join(directory, 'home'), DSH_PERMISSION_MODE: 'danger-full-access', ATT_TEST_DSH_ENTRY: entry,
      ATT_TEST_URL: `http://127.0.0.1:${server.address().port}` } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);

    await until('the director never asked', () => said('ASKED'));
    assert.match(text('d-ask'), /"asked":true/, text('d-ask'));
    assert.match(text('d-retry'), /"duplicate":true/, 'the retry asked again: ' + text('d-retry'));
    const asked = JSON.parse(await readFile(join(root, 'questions.json'), 'utf8'));
    assert.deepEqual(Object.keys(asked), ['restart-q']);
    assert.equal(asked['restart-q'].status, 'open');
    await writeFile(join(root, 'inbox', 'answer-restart-q.json'), JSON.stringify({ answer: 'yes, at 14:00Z' }));
    await until('the director was not woken by the answer', () => said('GOT_ANSWER'));
    const wake = events.filter(e => e.type === 'user/message' && JSON.stringify(e.data).includes('Owner answered'));
    assert.equal(wake.length, 1);
    assert.match(JSON.stringify(wake[0].data), /restart-q/); assert.match(JSON.stringify(wake[0].data), /yes, at 14:00Z/);
    assert.equal(JSON.parse(await readFile(join(root, 'questions.json'), 'utf8'))['restart-q'].status, 'answered');
    assert.equal(proc.exitCode, null, output.slice(-2000));
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
