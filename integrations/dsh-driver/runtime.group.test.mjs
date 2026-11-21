/** Step 23 press witness, plugin half (design-steward 7.7.16 end; platform director 418 end and
 *  420 Q3): the steward in a real pinned DSH with a fixture model whose every request body is
 *  recorded at the request boundary. One run:
 *    owner turn 1 -> group turn 1 (writes a unique MARKER into family memory; its step 2 crosses
 *    the compaction threshold, so DSH compacts IN the group turn) -> an idle compaction right
 *    after it (before any fold) -> group turn 2 -> owner turn 2.
 *  Arm one: no request of owner turn 2 carries a byte of the marker or of group turn 1's text.
 *  Arm two: neither compaction request, nor the checkpoint either leaves, carries them (the
 *  fixture's checkpoint echoes every user text it was given, so a leak would show).
 *  Snapshot: the rest of group turn 1 after its compaction, and group turn 2, are not given the
 *  snapshot; owner turn 2 is.
 *  Also: the guard refuses memory_note in a group turn and family_memory_show in an owner turn,
 *  and the host records the audience of every turn before its first step.
 *  Hook order (7.7.3, platform director 466): the audience of a turn is fixed before the
 *  snapshot is injected into it - observed, not inferred from registration order. */
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
      // The checkpoint echoes the markers it was given (every capitalised token and the family
      // marker): a marker a compaction request carried shows in it, and it stays smaller than
      // what it replaces.
      if (options.purpose === 'compaction') block = { type: 'text', text: 'Checkpoint: ' + [...new Set(options.messages.filter(m => m.role !== 'system')
        .map(m => JSON.stringify(m)).join(' ').match(/FAMILY-[0-9a-f-]{36}|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+/g) ?? [])].join(' ') };
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
      // Group turn 1's first step reports a prompt at the threshold (64000 x 0.5): its second step
      // starts with a pressure compaction, in the group turn.
      yield { type: 'usage', usage: { inputTokens: block.id === 'g1' ? 32000 : 100, outputTokens: 20 } };
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
  // The snapshot's source: called only when the snapshot is injected (ContextInjection.prepare).
  const context = host.context.bind(host);
  host.context = async (...args) => { await post('/test/context', {}); return context(...args); };
  const family = [];
  host.groups = {
    list: async () => ({ owner_refs: {}, groups: [{ channel: 'wechat', group_ref: 'r1', label: '家庭群', mention_prefix: '@管家' }] }),
    bind: async () => ({ refused: 'not_in_test' }), relay: async () => ({ refused: 'not_in_test' }),
    reply: async () => ({ refused: 'not_in_test' }), recent: async () => ({ recent: [] }),
    audience: async a => { await post('/test/audience', a); return { recorded: true }; },
    ownerSpokeSince: async () => false,
    familyNote: async (text, _signal, { turn } = {}) => { family.push(text); await post('/test/family', { text, turn }); return { noted: true }; },
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
  const events = [], requests = [], audiences = [], family = [], familyTurns = [], marks = {}, order = [];
  let output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    if (req.url === '/test/event') events.push(body);
    else if (req.url === '/test/request') requests.push(body);
    else if (req.url === '/test/audience') { audiences.push(body); order.push('audience:' + body.audience); }
    else if (req.url === '/test/context') order.push('snapshot');
    else if (req.url === '/test/family') { family.push(body.text); familyTurns.push(body.turn); }
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
      // The steward's own compaction policy shape (core/dsh_driver.py), retaining nothing; the
      // headroom fits the fixture's 64000-token window (the default 65536 leaves no pressure
      // budget, so no pressure compaction would ever run).
      { id: 'compaction-basic', config: { thresholdRatio: 0.5, retainTokens: 0, headroomTokens: 1024 } },
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
    // Arm two: the pressure compaction in the middle of group turn 1 (its step 2, the CURRENT
    // group turn's messages on the surface) and the idle compaction right after it (before any fold).
    const midTurn = root_.filter(r => r.purpose === 'compaction' && r.phase === 1);
    assert.ok(midTurn.length, 'no compaction ran inside group turn 1\n' + output.slice(-3000));
    assert.deepEqual(marks['/test/compacted'], { compacted: true });
    const compactions = root_.filter(r => r.purpose === 'compaction');
    assert.ok(compactions.some(r => r.phase === 2), 'the idle compaction made no request');
    for (const r of compactions) {
      assert.ok(!r.body.includes(MARKER) && !r.body.includes(escaped), 'the compaction request carries the family marker');
      assert.ok(!r.body.includes('GROUP_ONE_QUESTION'), 'the compaction request carries the group question');
    }
    assert.ok(midTurn[0].body.includes('A family-group turn ran here'), 'the mid-turn compaction request has no fold line in place of the group turn');
    assert.ok(midTurn[0].body.includes('RECENT_TURN_MARKER_99'), 'the fixture checkpoint echoes what it is given (owner turn 1 is in it)');
    const checkpoints = events.filter(e => e.type === 'compaction/summary' || (e.type === 'user/message' && /compact/.test(e.data?.source?.kind ?? '')));
    assert.ok(checkpoints.some(e => JSON.stringify(e).includes('Checkpoint: ')), 'no checkpoint was recorded');
    for (const e of checkpoints)
      assert.ok(!JSON.stringify(e).includes(MARKER) && !JSON.stringify(e).includes('GROUP_ONE_QUESTION'), 'the checkpoint carries group text');
    // Snapshot (the host's own injection, source dsh-director-context): not in the rest of group
    // turn 1 after its compaction, not in group turn 2 (after the idle one); in owner turn 2.
    const SNAPSHOT = '"kind":"dsh-director-context"';
    const afterMid = root_.slice(root_.indexOf(midTurn[0]) + 1).filter(r => r.phase === 1 && r.purpose !== 'compaction');
    assert.ok(afterMid.length, 'group turn 1 made no request after its compaction');
    for (const r of afterMid) assert.ok(!r.body.includes(SNAPSHOT), 'group turn 1 was given the snapshot after its compaction');
    assert.ok(turns(2).length, 'group turn 2 made requests');
    for (const r of turns(2)) assert.ok(!r.body.includes(SNAPSHOT), 'group turn 2 was given the snapshot');
    assert.ok(turns(3)[0].body.includes(SNAPSHOT) && turns(3)[0].body.includes('RECENT_TURN_MARKER_99'), 'owner turn 2 was not given the snapshot');
    // The guard, both ways.
    assert.match(resultOf('g2'), /group turn: only portfolio_projects/);
    assert.match(resultOf('o2'), /family memory is for group turns only/);
    // The audience of each turn, recorded before its first step.
    assert.deepEqual(audiences.map(a => a.audience), ['owner', 'group', 'group', 'owner']);
    assert.deepEqual(audiences[1].group, { channel: 'wechat', group_ref: 'r1' });
    // The family tool named the turn it ran in: the very turn the host recorded as the group's (477 N1).
    assert.ok(typeof familyTurns[0] === 'string' && familyTurns[0] === audiences[1].turn, JSON.stringify([familyTurns, audiences[1]]));
    assert.deepEqual(audiences[1].senderRefs, ['wxid_m1']);
    // Hook order: every snapshot was built after its turn's audience was recorded, and that
    // audience was the owner's (a snapshot before the record would follow the previous turn's).
    assert.ok(order.includes('snapshot'), 'no snapshot was injected: ' + order.join(' '));
    order.forEach((entry, at) => { if (entry === 'snapshot') assert.equal(order.slice(0, at).findLast(e => e !== 'snapshot'), 'audience:owner', order.join(' ')); });
    assert.equal(order.filter(e => e.startsWith('audience:'))[0], order[0], 'the first turn\'s audience came before any snapshot');
    assert.ok(order.lastIndexOf('snapshot') > order.lastIndexOf('audience:owner'), 'owner turn 2\'s snapshot was built after its audience: ' + order.join(' '));
    assert.doesNotMatch(output, /SessionPersistenceCorruptionError|stored log is corrupt/);
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});

// ---- the same in the AItelier adapter (runtime.mjs) over a recording State server ----------
// Design-steward 7.7.16 (plugin and adapter: "every State request a tool makes in a group turn
// carries the public-read header"; "a group turn's last words go only through post_group_reply,
// never forwardFinalText"; "grep_chat_history finds nothing a group turn said"); director 418
// item 4 ("one real group-turn request"). The State is a fake HTTP server that records each
// request's path, body and X-AItelier-Read-As, and answers a read that carries the header the
// way the backend answers an anonymous reader for a private project (403, the PUBLIC_READS
// refusal). A group message reaches the steward through a WeChat-shaped channel plugin.
const ADAPTER_PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-group-adapter-provider';
export const inject = ['llm'];
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
export function apply(ctx) {
  const done = new Set();
  const once = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const all = JSON.stringify(options.messages);
      await fetch(process.env.AITELIER_URL + '/test/model', { method: 'POST', body: JSON.stringify({ session: options.sessionId,
        purpose: options.purpose ?? null, body: all }) }).catch(() => {});
      const last = options.messages.at(-1);
      let block;
      if (options.purpose === 'compaction' || !options.tools?.length || options.sessionId !== 'grp-root') block = { type: 'text', text: 'Fixture.' };
      else if (last?.role === 'tool' && last.toolCallId === 'g1') block = { type: 'text', text: 'GROUP_ADAPTER_ANSWER' };
      else if (last?.role === 'tool' && last.toolCallId === 'o1') block = once('o2', () => tool('o2', 'grep_chat_history', { pattern: 'GROUP_Q_ADAPT[E]R', regex: true }));
      else if (last?.role === 'tool' && last.toolCallId === 'o2') block = once('o3', () => tool('o3', 'grep_chat_history', { pattern: 'm-owner-[2]', regex: true }));
      else if (last?.role === 'tool' && last.toolCallId === 'o3') block = { type: 'text', text: 'OWNER_ADAPTER_ANSWER' };
      else if (all.includes('GROUP_Q_ADAPTER') && !done.has('g1')) block = once('g1', () => tool('g1', 'portfolio_project', { project_id: 'family-ledger' }));
      else if (all.includes('m-owner-2') && !done.has('o1')) block = once('o1', () => tool('o1', 'portfolio_project', { project_id: 'family-ledger' }));
      block ??= { type: 'text', text: 'Done.' };
      yield { type: 'block-start', index: 0, blockType: block.type };
      if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text };
      else yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments };
      yield { type: 'block-end', index: 0, block };
      yield { type: 'usage', usage: { inputTokens: 10, outputTokens: 10 } };
      yield { type: 'finish', reason: { kind: block.type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
  ctx.on('session/event', (session, event) => {
    if (session.id === 'grp-root' && ['tool/result', 'turn/end'].includes(event.type))
      void fetch(process.env.AITELIER_URL + '/test/event', { method: 'POST', body: JSON.stringify(event) }).catch(() => {});
  });
}
`;

// A WeChat-shaped channel plugin: one family member's @-message in the bound group.
const GROUP_CHANNEL = String.raw`
export const name = 'fixture-group-channel';
export const inject = ['ownerChannels'];
const post = (path, body) => fetch(process.env.AITELIER_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
export function apply(ctx) {
  ctx.on('dispose', ctx.ownerChannels.register({ name: 'wechat', status: () => ({ connected: true }),
    async send(text, options) { await post('/test/send', { text, options }); return {}; },
    async sendGroup(room, text) { await post('/test/group-send', { room, text }); return { ref: 'g-out' }; },
    async setRooms(rooms) { await post('/test/rooms', rooms); } }));
  void (async () => {
    await post('/test/inbound', await ctx.ownerChannels.inbound({ channel: 'wechat', senderRef: 'wxid_m1', messageRef: 'gm-1',
      text: '@管家 GROUP_Q_ADAPTER m-owner-1 武侠项目怎样', at: new Date().toISOString(), conversationRef: 'r1@chatroom' }));
  })();
}
`;

test('real pinned DSH, the AItelier adapter: a group turn\'s tool reads carry the public-read header and its words go only to the group; the owner turn\'s do not', { timeout: 180000 }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-group-adapter-'));
  const seen = { state: [], model: [], events: [], groupSends: [], inbound: [] };
  let output = '', proc, parked, woke = false, ownerAt;
  const PRIVATE = '{"detail":"This State DAG record is not available."}';
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = (value, status = 200) => { res.statusCode = status; res.setHeader('content-type', 'application/json'); res.end(typeof value === 'string' ? value : JSON.stringify(value)); };
    const url = req.url, readAs = req.headers['x-aitelier-read-as'];
    if (url === '/test/model') { seen.model.push(body); return send({}); }
    if (url === '/test/event') { seen.events.push(body); return send({}); }
    if (url === '/test/group-send') { seen.groupSends.push(body); return send({}); }
    if (url === '/test/inbound') { seen.inbound.push(body); return send({}); }
    if (url.startsWith('/test/')) return send({});
    seen.state.push({ path: url, body, readAs });
    if (url === '/api/drivers/me') return send({ driver_id: 'drv-grp' });
    if (url === '/api/state/schema') return send({ operations: {} });
    if (url.endsWith('/context')) return send({ project_id: 'aitelier-home', driver_id: 'drv-grp', event_seq: 1, nodes: [],
      project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'fixture', runtime_skill: 'fixture' });
    if (url.endsWith('/activity') || url.endsWith('/heartbeat')) return send({ ok: true });
    if (url === '/api/state/query/wait_for_state_change') {
      // The owner's message arrives once the group's reply went out; until then (and after) it parks.
      if (woke) return;
      parked = () => { woke = true; ownerAt = new Date().toISOString();
        send({ events: [{ seq: 2, event_type: 'user_message_created', payload: { message_id: 'm-owner-2' } }], next_after: 2 }); };
      if (seen.state.some(s => s.path.endsWith('/post_group_reply'))) parked();
      return;
    }
    // The backend's answer to a public-read request for a private project: the anonymous refusal.
    if (readAs !== undefined && ['/api/state/query/project_overview', '/api/state/query/list_issues'].includes(url)) return send(PRIVATE, 403);
    if (url.startsWith('/api/state/commands/')) {
      const name = url.slice('/api/state/commands/'.length);
      if (name === 'relay_group_message') return send({ result: { message_id: 'gq1', sender_role: 'member', sender_label: '成员甲' } });
      if (name === 'set_steward_turn_audience') return send({ result: { recorded: true } });
      if (name === 'post_group_reply') {
        setTimeout(() => parked?.(), 200);
        return send({ result: { message_id: 'go1', channel: 'wechat', group_ref: 'r1@chatroom', text: '「武侠项目怎样」\n' + body.body, withheld: false } });
      }
      if (name === 'send_user_message') return send({ message_id: 'm-out-' + seen.state.length, project_id: 'aitelier-home' });
      if (name === 'report_channel_status') return send({ recorded: true });
      if (name === 'record_channel_delivery') return send({ recorded: true });
      if (name === 'complete_steward_memory_request') return send({});
      return send({ detail: 'unexpected ' + name }, 422);
    }
    if (url.startsWith('/api/state/query/')) {
      const name = url.slice('/api/state/query/'.length);
      if (name === 'list_owner_groups') return send({ result: { owner_refs: { wechat: 'wxid_owner' },
        groups: [{ channel: 'wechat', group_ref: 'r1@chatroom', label: '家庭群', mention_prefix: '@管家' }] } });
      if (name === 'list_group_recent') return send({ result: { recent: [] } });
      if (name === 'list_owner_channels') return send({ default_channel: null, channels: [
        { channel: 'wechat', bound: true, sender_ref: 'wxid_owner', bound_at: 't', revoked_at: null, is_default: false }] });
      // m-owner-1 waits unanswered all along (its id is in the family member's text);
      // m-owner-2 is the owner's message that opens the owner turn.
      if (name === 'list_user_conversation') return send({ messages: [
        { message_id: 'm-owner-1', sender_kind: 'user', sender_driver_id: null, created_at: '2026-11-21T00:00:00Z', subject: 'first',
          body: 'an earlier question', segments: [], reply_to_message_id: null },
        ...(woke ? [{ message_id: 'm-owner-2', sender_kind: 'user', sender_driver_id: null, created_at: ownerAt, subject: 'second',
          body: 'OWNER_BODY', segments: [], reply_to_message_id: null }] : [])], next_cursor: null });
      if (name === 'project_overview') return send({ result: { project: { project_id: body.project_id }, counts: {}, nodes: [] } });
      if (name === 'list_issues') return send({ result: { issues: [] } });
      return send({ result: {} });
    }
    send({ detail: url }, 404);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const profile = join(directory, 'home/profiles/driver');
    await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'provider.mjs'), ADAPTER_PROVIDER);
    await writeFile(join(directory, 'channel.mjs'), GROUP_CHANNEL);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'provider.mjs') },
        { id: 'dsh-director', name: join(here, '../dsh-director/index.mjs') },
        { id: 'aitelier-driver', name: join(here, 'runtime.mjs') },
        { id: 'dsh-channel-wechat', name: join(directory, 'channel.mjs') }] },
    ]));
    const token = join(directory, 'token');
    await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutCallerMode(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'drv-grp', AITELIER_DSH_PROJECT: 'aitelier-home', AITELIER_DSH_SESSION: 'grp-root',
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: `http://127.0.0.1:${server.address().port}`,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'),
      AITELIER_DSH_ROLE: 'steward', AITELIER_DSH_MEMORY_USER: 'owner', AITELIER_DSH_MEMORY_ROOT: join(directory, 'memory') } });
    proc.stdout.on('data', chunk => output += chunk);
    proc.stderr.on('data', chunk => output += chunk);
    const until = async (what, met, ms = 90000) => {
      const deadline = Date.now() + ms;
      while (!met() && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
      assert.ok(met(), what + '\n' + JSON.stringify(seen.state.map(s => [s.path, s.readAs])).slice(0, 3000) + '\n' + output.slice(-3000));
    };
    const commands = name => seen.state.filter(s => s.path === '/api/state/commands/' + name);
    const resultOf = id => JSON.stringify(seen.events.find(e => e.type === 'tool/result' && e.data.message.toolCallId === id)?.data.message ?? null);
    await until('the owner turn never ended', () => resultOf('o3') !== 'null'
      && seen.events.filter(e => e.type === 'turn/end').length >= 2);
    await until('the owner turn\'s automatic reply was never sent', () => commands('send_user_message').some(s => s.body.request_key === 'auto-reply:m-owner-2'));
    assert.deepEqual(seen.inbound, [{ accepted: true }]);
    // The one real group-turn request: portfolio_project of a private project, with the header,
    // answered the anonymous refusal; the tool shows only "not found". The owner turn's read
    // of the same project carries no header.
    const overviews = seen.state.filter(s => s.path === '/api/state/query/project_overview' && s.body.project_id === 'family-ledger');
    assert.deepEqual(overviews.map(s => s.readAs), ['public', undefined]);
    assert.match(resultOf('g1'), /not_found/);
    assert.doesNotMatch(resultOf('o1'), /not_found/);
    // The host's own requests never carry it, in the group turn or out of it.
    for (const name of ['relay_group_message', 'set_steward_turn_audience', 'post_group_reply'])
      assert.ok(commands(name).length && commands(name).every(s => s.readAs === undefined), name);
    assert.ok(seen.state.filter(s => s.path.endsWith('/list_owner_groups')).every(s => s.readAs === undefined));
    assert.deepEqual(commands('set_steward_turn_audience').map(s => s.body.audience).slice(0, 2), ['group', 'owner']);
    // The group turn's last words went to its group once, through post_group_reply ...
    assert.deepEqual(commands('post_group_reply').map(s => [s.body.question_id, s.body.body]), [['gq1', 'GROUP_ADAPTER_ANSWER']]);
    assert.deepEqual(seen.groupSends, [{ room: 'r1@chatroom', text: '「武侠项目怎样」\nGROUP_ADAPTER_ANSWER' }]);
    // ... and never to the owner: m-owner-1 was in the group turn's input and unanswered, and no
    // automatic reply answered it. The owner turn's last words did answer m-owner-2.
    assert.deepEqual(commands('send_user_message').map(s => [s.body.request_key, s.body.body]), [['auto-reply:m-owner-2', 'OWNER_ADAPTER_ANSWER']]);
    assert.ok(!seen.groupSends.some(s => s.text.includes('OWNER_ADAPTER_ANSWER')));
    // grep_chat_history in the owner turn: nothing the group turn said; the owner turn's own words.
    // (A regex, so the search does not find its own call.)
    const grepped = id => JSON.parse(JSON.parse(resultOf(id)).content[0].text);
    assert.deepEqual(grepped('o2').matches, []);
    assert.ok(grepped('o3').matches.length > 0, resultOf('o3'));
    // The owner turn's requests carry no byte of the group turn.
    const ownerTurn = seen.model.filter(m => m.session === 'grp-root' && m.body.includes('m-owner-2'));
    assert.ok(ownerTurn.length && ownerTurn.every(m => !m.body.includes('GROUP_Q_ADAPTER') && !m.body.includes('GROUP_ADAPTER_ANSWER')));
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
