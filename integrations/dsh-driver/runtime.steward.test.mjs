/** Plugin step 12 (design-steward 4 验收): the steward in a real pinned DSH, with the
 *  plugin's built-in local host (rootRole steward, owner memory) and a fixture model, over
 *  two boots of one session. On both boots the steward's request carries exactly the
 *  steward's presented tools that are registered, its prompt is steward.md (never
 *  director.md), and a tool it does not have - bash, called by name - is refused. */
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
const require = createRequire(process.env.STW_TEST_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-steward-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.STW_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const systemText = messages => messages.filter(m => m.role === 'system')
  .map(m => typeof m.content === 'string' ? m.content : m.content.map(b => b.text ?? '').join('')).join('\n');
export function apply(ctx) {
  const done = new Set(), boot = process.env.STW_TEST_BOOT;
  const once = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      await post('/test/request', { session: options.sessionId, purpose: options.purpose ?? null,
        tools: (options.tools ?? []).map(t => t.name), descriptions: (options.tools ?? []).map(t => [t.name, t.description ?? '']),
        system: systemText(options.messages) });
      let block;
      if (options.purpose === 'compaction') block = { type: 'text', text: 'Checkpoint: nothing in flight.' };
      else if (!options.tools?.length || options.sessionId !== 'stw-root') block = { type: 'text', text: 'Fixture session' };
      else if (boot === '1') block = once('b1', () => tool('b1', 'bash', { command: 'echo STEWARD_HAS_A_SHELL' }))
        ?? once('n1', () => tool('n1', 'memory_note', { text: 'STEWARD_NOTE_7 the owner likes short replies' }))
        ?? once('d1', () => tool('d1', 'idle_dormant', { reason: 'quiet', wake_after_minutes: 15 }))
        ?? { type: 'text', text: 'Steward idle.' };
      else block = (text.includes('wake2.json') ? once('s2', () => tool('s2', 'memory_search', { query: 'STEWARD_NOTE_7' })) : undefined)
        ?? (done.has('s2') ? once('b2', () => tool('b2', 'bash', { command: 'echo STEWARD_HAS_A_SHELL' })) : undefined)
        ?? (done.has('b2') ? once('d2', () => tool('d2', 'idle_dormant', { reason: 'quiet', wake_after_minutes: 15 })) : undefined)
        ?? { type: 'text', text: 'Steward idle.' };
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
    if (session.id === 'stw-root' && ['tool/result', 'user/message', 'assistant/message'].includes(event.type)) void post('/test/event', event);
  });
}
`;

test('real pinned DSH, two boots: the steward sees exactly its tools and its own prompt, and has no shell', { timeout: 180000 }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const here = dirname(fileURLToPath(import.meta.url));
  const stewardSkill = await readFile(join(here, '../dsh-director/steward.md'), 'utf8');
  const directorSkill = await readFile(join(here, '../dsh-director/director.md'), 'utf8');
  const directory = await mkdtemp(join(homedir(), 'dsh-steward-'));
  const root = join(directory, 'steward'), memRoot = join(directory, 'memory');
  const events = [], requests = [];
  let output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    if (req.url === '/test/event') events.push(body);
    else if (req.url === '/test/request') requests.push(body);
    res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const result = id => events.find(e => e.type === 'tool/result' && e.data.message.toolCallId === id);
  const resultText = id => JSON.stringify(result(id).data.message);
  const until = async (what, met, ms = 60000) => {
    const deadline = Date.now() + ms;
    while (!met() && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(met(), what + '\n' + JSON.stringify(events.map(e => [e.type, e.data?.message?.toolCallId ?? ''])) + '\n' + output.slice(-3000));
  };
  const expected = ['ask_owner', 'close_question', 'grep_chat_history', 'idle_dormant', 'memory_forget', 'memory_note', 'memory_profile_update', 'memory_read', 'memory_search'];
  const rootRequests = () => requests.filter(r => r.session === 'stw-root' && r.purpose !== 'compaction' && r.tools.length);
  try {
    await mkdir(join(root, 'inbox'), { recursive: true });
    await writeFile(join(root, 'inbox', 'start.json'), JSON.stringify({ task: 'begin' }));
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-steward-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-steward-provider.mjs') },
        { id: 'director', name: join(here, '../dsh-director/index.mjs') },
        { id: 'director-local', name: join(here, '../dsh-director/hosts/local.mjs'), config: { root, sessionId: 'stw-root',
          provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100, idleReviewMs: 600000,
          dormantCompactMinTokens: 0, rootRole: 'steward', memory: { userId: 'owner', root: memRoot } } }] },
    ]));
    const environment = Object.fromEntries(Object.entries(withoutCallerMode(process.env)).filter(([k]) => !k.startsWith('AITELIER_')));
    const launch = boot => {
      const child = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...environment,
        DSH_HOME: join(directory, 'home'), STW_TEST_DSH_ENTRY: entry, STW_TEST_BOOT: boot, STW_TEST_URL: `http://127.0.0.1:${server.address().port}` } });
      child.stdout.on('data', chunk => output += chunk); child.stderr.on('data', chunk => output += chunk);
      return child;
    };
    const checkRequests = (boot, from) => {
      const mine = rootRequests().slice(from);
      assert.ok(mine.length, `boot ${boot}: no steward request`);
      for (const r of mine) {
        assert.deepEqual([...r.tools].sort(), expected, `boot ${boot}: the steward's tool list`);
        assert.ok(r.system.includes(stewardSkill.trim()), `boot ${boot}: the steward's system prompt lacks steward.md`);
        assert.ok(!r.system.includes(directorSkill.split('\n')[0]), `boot ${boot}: the steward's system prompt carries director.md`);
        for (const [name, description] of r.descriptions)
          assert.ok(!description.startsWith('Director only'), `boot ${boot}: the steward is shown ${name} as "Director only"`);
      }
    };

    // ---- boot 1
    proc = launch('1');
    await until('boot 1 never went dormant', () => result('d1'));
    assert.match(resultText('b1'), /bash is not available to this role/);
    assert.ok(!output.includes('STEWARD_HAS_A_SHELL'), 'bash never ran');
    assert.match(resultText("n1"), /\\"id\\":\\"n-[0-9a-f]{20}/);
    checkRequests(1, 0);
    const before = rootRequests().length;
    proc.kill('SIGTERM'); await once(proc, 'exit');

    // ---- boot 2, same session
    proc = launch('2');
    await writeFile(join(root, 'inbox', 'wake2.json'), JSON.stringify({ wake: 2 }));
    await until('boot 2 never went dormant', () => result('d2'));
    assert.match(resultText('s2'), /STEWARD_NOTE_7/, 'the note survived the restart');
    assert.match(resultText('b2'), /bash is not available to this role/);
    checkRequests(2, before);
    assert.doesNotMatch(output, /SessionPersistenceCorruptionError|stored log is corrupt/);
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
