/** Overfit review 2026-11-24 (重-10, 重-11): a standalone steward remembers what its owner said.
 *  A real pinned DSH runs dsh-director and its built-in local host only (no adapter, no backend),
 *  with the owner's memory, over two boots of one session. The owner types into the steward's
 *  session as DSH's client does (a user message whose source is `user`), driven here by a probe
 *  plugin:
 *    boot 1: a wake, then the owner says SAY1 (a fact with a marker) and the steward answers;
 *    the owner says SAY2 and the steward's memory_search for the marker finds the owner's words,
 *    recorded by the plugin itself (the local host has no conversation source).
 *    boot 2: the owner says SAY3 and the search finds them again - each owner message is in
 *    memory once, nothing a wake brought is, and the steward's prompt never names a dashboard,
 *    a chat channel or a family group. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm, rename } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutHostWiring } from './dsh-entry.mjs';
import { MemoryStore } from '../dsh-director/lib/memory/store.mjs';
import { stewardPromptFor, stewardPromptFeatures } from '../dsh-director/lib/steward-prompt.mjs';
import { localHost } from '../dsh-director/hosts/local.mjs';

const ROOT = 'stm-root';
const SAY1 = 'SAY1 Remember HERON_4711: the boat is blue.';
const SAY2 = 'SAY2 What did I tell you about the heron?';
const SAY3 = 'SAY3 And once more, the heron?';

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.STM_TEST_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-stm-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.STM_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const say = text => ({ type: 'text', text });
const flat = m => typeof m.content === 'string' ? m.content : m.content.map(b => b.text ?? '').join('');
export function apply(ctx) {
  const done = new Set(), boot = process.env.STM_TEST_BOOT;
  const once = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages), last = options.messages.at(-1);
      await post('/test/request', { session: options.sessionId, purpose: options.purpose ?? null, tools: (options.tools ?? []).map(t => t.name),
        system: options.messages.filter(m => m.role === 'system').map(flat).join('\n') });
      let block;
      if (options.purpose === 'compaction') block = say('Checkpoint: nothing in flight.');
      else if (!options.tools?.length || options.sessionId !== '${ROOT}') block = say('Fixture session');
      else if (last?.role === 'tool' && ['s1', 's2'].includes(last.toolCallId)) block = once('f-' + last.toolCallId, () => say('FOUND it in memory.'));
      else if (boot === '1') block = once('d1', () => tool('d1', 'idle_dormant', { reason: 'nothing yet', wake_after_minutes: 60 }))
        ?? (text.includes('SAY1') ? once('r1', () => say('NOTED: the boat is blue.')) : undefined)
        ?? (text.includes('SAY2') ? once('s1', () => tool('s1', 'memory_search', { query: 'HERON_4711' })) : undefined);
      else block = text.includes('SAY3') ? once('s2', () => tool('s2', 'memory_search', { query: 'HERON_4711' })) : undefined;
      block ??= say('Steward idle.');
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
    if (session.id === '${ROOT}' && ['tool/result', 'user/message', 'assistant/message'].includes(event.type)) void post('/test/event', event);
  });
}
`;

// What DSH's client does with a line the owner typed (dsh-api-session-controller prompt): one
// user message whose source is {kind: 'user', rpcId}, queued as a follow-up. Lines come in as
// <dir>/<n>.say and are renamed to .done once sent.
const PROBE = String.raw`
import { readdir, readFile, rename } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { join } from 'node:path';
export const name = 'stm-owner-probe';
export const inject = ['agents'];
export function apply(ctx, { dir, session, entry }) {
  let busy = false, llm;
  const timer = setInterval(async () => {
    if (busy) return;
    busy = true;
    try {
      llm ??= await import(createRequire(entry).resolve('@deepseek-ai/dsh-llm'));
      for (const file of (await readdir(dir)).filter(n => n.endsWith('.say')).sort()) {
        const agent = ctx.agents.get(session);
        if (!agent) break;
        const text = await readFile(join(dir, file), 'utf8');
        agent.followup(llm.createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'user', rpcId: 'probe-' + file } }));
        await rename(join(dir, file), join(dir, file + '.done'));
      }
    } catch {} finally { busy = false; }
  }, 50);
  ctx.on('dispose', () => clearInterval(timer));
}
`;

test('real pinned DSH, two boots: a standalone steward records its owner\'s turns itself, and memory_search finds them', { timeout: 240000 }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-stm-'));
  const root = join(directory, 'steward'), memRoot = join(directory, 'memory'), sayDir = join(directory, 'say');
  const events = [], requests = [];
  let output = '', proc, n = 0;
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
  const said = text => events.some(e => e.type === 'assistant/message' && JSON.stringify(e.data).includes(text));
  const until = async (what, met, ms = 60000) => {
    const deadline = Date.now() + ms;
    while (!met() && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(met(), what + '\n' + JSON.stringify(events.map(e => [e.type, e.data?.message?.toolCallId ?? ''])) + '\n' + output.slice(-3000));
  };
  const ownerSays = async text => {
    const name = String(++n).padStart(4, '0');
    await writeFile(join(sayDir, name + '.tmp'), text);
    await rename(join(sayDir, name + '.tmp'), join(sayDir, name + '.say'));
  };
  // No ownerLanguage in this host's config (the plugin's default, English). Every language passage
  // sits in a section this host lacks, so its prompt reads the same in either language
  // (dsh-director/test/steward-prompt.test.mjs, the local host); the language shows only on a
  // host with those sections (runtime.group.test.mjs, 'en' and 'zh').
  const stewardMd = await readFile(join(here, '../dsh-director/steward.md'), 'utf8');
  const stewardFeatures = stewardPromptFeatures(localHost({ root: '/tmp/x', provider: 'p', model: 'm' }).capabilities, { memory: { userId: 'owner' } });
  const stewardSkill = stewardPromptFor(stewardMd, { language: undefined, features: stewardFeatures });
  assert.equal(stewardSkill, stewardPromptFor(stewardMd, { language: 'zh', features: stewardFeatures }));
  const rootRequests = () => requests.filter(r => r.session === ROOT && r.purpose !== 'compaction' && r.tools.length);
  try {
    await mkdir(join(root, 'inbox'), { recursive: true });
    await mkdir(sayDir, { recursive: true });
    await writeFile(join(root, 'inbox', 'start.json'), JSON.stringify({ task: 'begin' }));
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-stm-provider.mjs'), PROVIDER);
    await writeFile(join(directory, 'stm-owner-probe.mjs'), PROBE);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-stm-provider.mjs') },
        { id: 'director', name: join(here, '../dsh-director/index.mjs') },
        { id: 'director-local', name: join(here, '../dsh-director/hosts/local.mjs'), config: { root, sessionId: ROOT,
          provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100, idleReviewMs: 600000,
          dormantCompactMinTokens: 0, rootRole: 'steward', memory: { userId: 'owner', root: memRoot } } },
        { id: 'stm-probe', name: join(directory, 'stm-owner-probe.mjs'), config: { dir: sayDir, session: ROOT, entry } }] },
    ]));
    const environment = withoutHostWiring(process.env);
    const launch = boot => {
      const child = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...environment,
        DSH_HOME: join(directory, 'home'), STM_TEST_DSH_ENTRY: entry, STM_TEST_BOOT: boot, STM_TEST_URL: `http://127.0.0.1:${server.address().port}` } });
      child.stdout.on('data', chunk => output += chunk); child.stderr.on('data', chunk => output += chunk);
      return child;
    };

    // ---- boot 1
    proc = launch('1');
    await until('boot 1 never went dormant', () => result('d1'));
    await ownerSays(SAY1);
    await until('the steward never answered SAY1', () => said('NOTED: the boat is blue.'));
    await ownerSays(SAY2);
    await until('the steward never searched', () => result('s1') && said('FOUND it in memory.'));
    const found = resultText('s1');
    console.log('memory_search (boot 1):', found.slice(0, 1500));
    assert.ok(found.includes('HERON_4711: the boat is blue.'), 'the search finds the owner\'s own words: ' + found);
    assert.match(found, /t-[A-Za-z0-9._:-]+-o/, 'as the owner part of a recorded turn');
    for (const r of rootRequests()) {
      assert.ok(r.system.includes(stewardSkill.trim()), 'the steward\'s prompt is the one for its host');
      for (const word of ['dashboard', 'WeChat', 'Slack', 'family', 'handoff', 'Daily brief'])
        assert.ok(!r.system.includes(word), `the standalone steward is told about ${word}`);
    }
    proc.kill('SIGTERM'); await once(proc, 'exit');

    // ---- boot 2, same session: what boot 1 recorded is still found, and nothing is written twice
    proc = launch('2');
    await ownerSays(SAY3);
    await until('the steward never searched on boot 2', () => result('s2') && events.filter(e => e.type === 'assistant/message'
      && JSON.stringify(e.data).includes('FOUND it in memory.')).length >= 2);
    assert.ok(resultText('s2').includes('HERON_4711: the boat is blue.'), resultText('s2'));
    assert.doesNotMatch(output, /SessionPersistenceCorruptionError|stored log is corrupt|already registered|memory turn:/);
    proc.kill('SIGTERM'); await once(proc, 'exit');

    const store = await MemoryStore.open({ root: memRoot, userId: 'owner' });
    try {
      const conversation = [...store.entries.values()].filter(e => e.source === 'conversation');
      console.log('recorded:', JSON.stringify(conversation.map(e => [e.id, e.role, e.text])));
      assert.deepEqual(conversation.filter(e => e.role === 'owner').map(e => e.text), [SAY1, SAY2, SAY3], 'each owner message once');
      const turnOf = text => conversation.find(e => e.text === text).batch;
      assert.ok(conversation.some(e => e.role === 'steward' && e.text === 'NOTED: the boat is blue.' && e.batch === turnOf(SAY1)),
        'the reply is in the owner\'s turn');
      assert.ok(conversation.every(e => !/(Director|Steward) event/.test(e.text) && !e.text.includes('[system clock]')), 'no wake, no clock line');
    } finally { await store.close(); }
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
