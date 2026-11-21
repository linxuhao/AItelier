/** Step 23 press witness, plugin half (design-steward 7.7.16 end; platform director 418 end and
 *  420 Q3): the steward in a real pinned DSH with a fixture model whose every request body is
 *  recorded at the request boundary. One run:
 *    owner turn 1 -> group turn 1 (writes a unique MARKER into family memory) -> an idle
 *    compaction right after it (before any fold) -> group turn 2 -> owner turn 2.
 *  Arm one: no request of owner turn 2 carries a byte of the marker or of group turn 1's text.
 *  Arm two: the compaction request and the checkpoint it leaves carry neither.
 *  Snapshot: group turn 2 (after the compaction) is not given the snapshot; owner turn 2 is.
 *  Also: the guard refuses memory_note in a group turn and family_memory_show in an owner turn,
 *  and the host records the audience of every turn before its first step. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { randomUUID } from 'node:crypto';
import { measuredEntryForTest, withoutCallerMode } from './dsh-entry.mjs';
import { MemoryStore } from '../dsh-director/lib/memory/store.mjs';

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.STW_TEST_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-group-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.STW_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const textOf = m => typeof m.content === 'string' ? m.content : (m.content ?? []).map(b => b.text ?? '').join('');
export function apply(ctx) {
  const done = new Set();
  const first = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      await post('/test/request', { session: options.sessionId, purpose: options.purpose ?? null, phase: globalThis.__stwPhase ?? 0,
        body: JSON.stringify(options.messages) });
      // What this turn was given: the user messages after the last assistant message.
      const last = options.messages.at(-1), lastAssistant = options.messages.findLastIndex(m => m.role === 'assistant');
      const asked = options.messages.slice(lastAssistant + 1).filter(m => m.role === 'user').map(textOf).join('\n')
        || [...options.messages].reverse().filter(m => m.role === 'user').slice(0, 3).map(textOf).join('\n');
      let block;
      if (options.purpose === 'compaction') block = { type: 'text', text: 'Checkpoint: nothing in flight.' };
      else if (!options.tools?.length || options.sessionId !== 'stw-root') block = { type: 'text', text: 'Fixture session' };
      else if (last?.role === 'tool') block = { type: 'text', text: asked.includes('GROUP_ONE_QUESTION') ? 'GROUP_ONE_ANSWER' : 'Done.' };
      else if (asked.includes('GROUP_ONE_QUESTION')) block = first('g1', () => tool('g1', 'family_memory_note', { text: process.env.STW_TEST_MARKER }));
      else if (asked.includes('GROUP_TWO_QUESTION')) block = first('g2', () => tool('g2', 'memory_note', { text: 'family wants this kept' }));
      else if (asked.includes('wake2.json')) block = first('o2', () => tool('o2', 'family_memory_show', {}));
      block ??= { type: 'text', text: 'Owner answer.' };
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
    if (session.id === 'stw-root' && ['tool/result', 'user/message', 'compaction/summary', 'turn/end'].includes(event.type)) void post('/test/event', event);
  });
}
`;

// The local host with the `groups` capability: in-memory, recording every call.
const HOST = here => String.raw`
import { localHost } from ${JSON.stringify(join(here, '../dsh-director/hosts/local.mjs'))};
export const name = 'group-test-host';
export const inject = [];
const post = (path, body) => fetch(process.env.STW_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
export function apply(ctx, config) {
  const host = localHost(config);
  host.capabilities = [...host.capabilities, 'groups'];
  const family = [];
  host.groups = {
    list: async () => ({ owner_refs: {}, groups: [{ channel: 'wechat', group_ref: 'r1', label: '家庭群', mention_prefix: '@管家' }] }),
    bind: async () => ({ refused: 'not_in_test' }), relay: async () => ({ refused: 'not_in_test' }),
    reply: async () => ({ refused: 'not_in_test' }), recent: async () => ({ recent: [] }),
    audience: async a => { await post('/test/audience', a); return { recorded: true }; },
    ownerSpokeSince: async () => false,
    familyNote: async text => { family.push(text); await post('/test/family', { text }); return { noted: true }; },
    familyShow: async () => ({ entries: family.map(text => ({ text })) }),
  };
  ctx.provide('directorHost', host);
}
`;

// Drives the scenario from inside the process: steers each group question tagged the way the
// production wake is, and runs the idle compaction right after group turn 1.
const DRIVER = String.raw`
import { createRequire } from 'node:module';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
const require = createRequire(process.env.STW_TEST_DSH_ENTRY);
const { createUserMessage } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'group-test-driver';
export const inject = ['agents', 'compaction'];
const post = (path, body) => fetch(process.env.STW_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
const group = (id, text) => createUserMessage({ content: [{ type: 'text', text: JSON.stringify({ group_turn: { group: '家庭群',
  messages: [{ message_id: id, sender_role: 'member', sender: '成员甲', text }], recent: [] } }) }],
  source: { kind: 'dsh-director-group', channel: 'wechat', group_ref: 'r1', group_label: '家庭群', question_ids: [id], sender_refs: ['wxid_m1'] } });
export function apply(ctx) {
  globalThis.__stwPhase = 0;
  let busy = false;
  ctx.on('agent/status', ({ agent, status }) => {
    if (agent.session.id !== 'stw-root' || status !== 'idle' || busy) return;
    busy = true;
    setTimeout(async () => {
      try {
        const phase = globalThis.__stwPhase;
        if (phase === 0) { globalThis.__stwPhase = 1; ctx.agents.withoutInitiator(() => agent.steer(group('q1', 'GROUP_ONE_QUESTION 周六我们干嘛'))); }
        else if (phase === 1) {
          globalThis.__stwPhase = 2;
          const result = await ctx.compaction.compactNow(agent, AbortSignal.timeout(60000));
          await post('/test/compacted', { compacted: Boolean(result) });
          ctx.agents.withoutInitiator(() => agent.steer(group('q2', 'GROUP_TWO_QUESTION 记住这个')));
        } else if (phase === 2) {
          globalThis.__stwPhase = 3;
          await writeFile(join(process.env.STW_TEST_ROOT, 'inbox', 'wake2.json'), JSON.stringify({ wake: 2 }));
        } else if (phase === 3) { globalThis.__stwPhase = 4; await post('/test/done', {}); }
      } catch (error) { await post('/test/error', { error: String(error?.stack ?? error) }); }
      finally { busy = false; }
    }, 300);
  });
}
`;

test('real pinned DSH: a group turn never reaches the owner\'s next turn, a compaction or the group\'s snapshot', { timeout: 240000 }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-group-'));
  const root = join(directory, 'steward'), memRoot = join(directory, 'memory');
  const MARKER = 'FAMILY-' + randomUUID();
  const events = [], requests = [], audiences = [], family = [], marks = {};
  let output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    if (req.url === '/test/event') events.push(body);
    else if (req.url === '/test/request') requests.push(body);
    else if (req.url === '/test/audience') audiences.push(body);
    else if (req.url === '/test/family') family.push(body.text);
    else marks[req.url] = body;
    res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const resultOf = id => JSON.stringify(events.find(e => e.type === 'tool/result' && e.data.message.toolCallId === id)?.data.message ?? null);
  try {
    const seeded = await MemoryStore.open({ root: memRoot, userId: 'owner' });
    await seeded.recordTurn('seed', [{ role: 'owner', part: 'o', text: 'RECENT_TURN_MARKER_99 记账工具' }]);
    await seeded.close();
    await mkdir(join(root, 'inbox'), { recursive: true });
    await writeFile(join(root, 'inbox', 'start.json'), JSON.stringify({ task: 'begin' }));
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'provider.mjs'), PROVIDER);
    await writeFile(join(directory, 'host.mjs'), HOST(here));
    await writeFile(join(directory, 'driver.mjs'), DRIVER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true }, { id: 'tool-result-pruner', disabled: true },
      // The steward's own compaction policy shape (core/dsh_driver.py), retaining nothing.
      { id: 'compaction-basic', config: { thresholdRatio: 0.5, retainTokens: 0 } },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'provider.mjs') },
        { id: 'director', name: join(here, '../dsh-director/index.mjs') },
        { id: 'group-host', name: join(directory, 'host.mjs'), config: { root, sessionId: 'stw-root',
          provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100, idleReviewMs: 600000,
          rootRole: 'steward', memory: { userId: 'owner', root: memRoot } } },
        { id: 'group-driver', name: join(directory, 'driver.mjs') }] },
    ]));
    const environment = Object.fromEntries(Object.entries(withoutCallerMode(process.env)).filter(([k]) => !k.startsWith('AITELIER_')));
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...environment,
      DSH_HOME: join(directory, 'home'), STW_TEST_DSH_ENTRY: entry, STW_TEST_MARKER: MARKER, STW_TEST_ROOT: root,
      STW_TEST_URL: `http://127.0.0.1:${server.address().port}` } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);
    const deadline = Date.now() + 180000;
    while (!marks['/test/done'] && !marks['/test/error'] && Date.now() < deadline && proc.exitCode === null)
      await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(marks['/test/done'], 'the scenario did not finish: ' + JSON.stringify(marks['/test/error'] ?? null) + '\n' + output.slice(-3000));

    const root_ = requests.filter(r => r.session === 'stw-root');
    const turns = phase => root_.filter(r => r.phase === phase && r.purpose !== 'compaction');
    const escaped = JSON.stringify(MARKER).slice(1, -1);
    // The group turn ran and kept the marker in family memory (the host got it).
    assert.deepEqual(family, [MARKER]);
    assert.ok(turns(1).some(r => r.body.includes('GROUP_ONE_QUESTION')), 'group turn 1 saw its question');
    // Arm one: owner turn 2's every request body: no byte of the marker, nor of group turn 1.
    assert.ok(turns(3).length, 'owner turn 2 made requests');
    for (const r of turns(3)) {
      assert.ok(!r.body.includes(MARKER) && !r.body.includes(escaped), 'owner turn 2 carries the family marker');
      assert.ok(!r.body.includes('GROUP_ONE_QUESTION') && !r.body.includes('GROUP_ONE_ANSWER'), 'owner turn 2 carries group turn 1');
      assert.ok(!r.body.includes('GROUP_TWO_QUESTION'), 'owner turn 2 carries group turn 2');
    }
    assert.ok(turns(3)[0].body.includes('A family-group turn ran here'), 'the fold line stands where the group turns were');
    // Arm two: the idle compaction right after group turn 1 (before any fold).
    assert.deepEqual(marks['/test/compacted'], { compacted: true });
    const compactions = root_.filter(r => r.purpose === 'compaction');
    assert.ok(compactions.length, 'a compaction request was made');
    for (const r of compactions) {
      assert.ok(!r.body.includes(MARKER) && !r.body.includes(escaped), 'the compaction request carries the family marker');
      assert.ok(!r.body.includes('GROUP_ONE_QUESTION'), 'the compaction request carries the group question');
    }
    for (const e of events.filter(e => e.type === 'compaction/summary' || (e.type === 'user/message' && /compact/.test(e.data?.source?.kind ?? ''))))
      assert.ok(!JSON.stringify(e).includes(MARKER) && !JSON.stringify(e).includes('GROUP_ONE_QUESTION'), 'the checkpoint carries group text');
    // Snapshot: not given to group turn 2 (after the compaction), given to owner turn 2.
    assert.ok(turns(2).length, 'group turn 2 made requests');
    for (const r of turns(2)) assert.ok(!r.body.includes('RECENT_TURN_MARKER_99'), 'group turn 2 was given the snapshot: '
      + r.body.slice(Math.max(0, r.body.indexOf('RECENT_TURN_MARKER_99') - 1500), r.body.indexOf('RECENT_TURN_MARKER_99') + 200));
    assert.ok(turns(3)[0].body.includes('RECENT_TURN_MARKER_99'), 'owner turn 2 was not given the snapshot');
    // The guard, both ways.
    assert.match(resultOf('g2'), /group turn: only portfolio_projects/);
    assert.match(resultOf('o2'), /family memory is for group turns only/);
    // The audience of each turn, recorded before its first step.
    assert.deepEqual(audiences.map(a => a.audience), ['owner', 'group', 'group', 'owner']);
    assert.deepEqual(audiences[1].group, { channel: 'wechat', group_ref: 'r1' });
    assert.deepEqual(audiences[1].senderRefs, ['wxid_m1']);
    assert.doesNotMatch(output, /SessionPersistenceCorruptionError|stored log is corrupt/);
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
