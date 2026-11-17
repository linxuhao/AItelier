/** Plugin step 12 (design-steward 4), the JS side: the steward role in the table, its
 *  closed tool set, the AItelier host started as the steward, the owner's conversation
 *  recorded into memory, the owner-message check, the handoff and the portfolio reads.
 *  The server-side limits (steward_read_only) are tests/unit's; the real-DSH boot is
 *  runtime.steward.test.mjs. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadRoles, validateRoles, roleOf } from './roles.mjs';
import { roles as aitelierRoles } from './aitelier-roles.mjs';
import { refusal, presentTools } from './tool-discovery.mjs';
import { aitelierHost } from './runtime.mjs';
import { conversationTurns, stewardOps, syncConversation } from './steward-host.mjs';
import { validateHost } from '../dsh-director/lib/host.mjs';
import { MemoryStore } from '../dsh-director/lib/memory/store.mjs';

const text = readFileSync(new URL('../dsh-director/roles.json', import.meta.url), 'utf8');
const table = loadRoles();
const variant = edit => { const copy = JSON.parse(text); edit(copy); return copy; };
const exec = name => ({ name, arguments: {} });

test('the steward is an optional role: loaded from roles.json, absent is fine, its keys are checked', () => {
  assert.equal(table.steward.prompt, 'steward.md');
  assert.ok(readFileSync(new URL('../dsh-director/steward.md', import.meta.url), 'utf8').length > 500);
  assert.equal(table.steward.presented_only, true);
  assert.doesNotThrow(() => validateRoles(variant(t => { delete t.steward; })), 'a table without a steward is the table before step 12');
  assert.throws(() => validateRoles(variant(t => { t.steward.models = []; })), /unknown key steward\.models/);
  assert.throws(() => validateRoles(variant(t => { delete t.steward.forbidden; })), /missing key steward\.forbidden/);
  assert.throws(() => validateRoles(variant(t => { t.steward.presented_only = 'yes'; })), /steward\.presented_only must be true or false/);
  assert.throws(() => validateRoles(variant(t => { delete t.director; })), /missing key table\.director/);
  assert.doesNotThrow(() => validateRoles(variant(t => { t.worker.presented_only = false; })), 'presented_only is open to every role');
  assert.equal(roleOf('root', 'root', 'steward'), 'steward');
  assert.equal(roleOf('child', 'root', 'steward'), 'worker');
  assert.equal(roleOf('root', 'root'), 'director', 'the default root role is unchanged');
});

test('presented_only: the steward has exactly its presented tools; everything else is refused, however it is called', () => {
  const steward = aitelierRoles(table).steward;
  assert.deepEqual(steward.presented, [...table.steward.presented, 'send_user_message'], 'the adapter adds only the owner message');
  for (const name of ['bash', 'read', 'write', 'edit', 'grep', 'subagent', 'call_tool', 'search_tools', 'state_read', 'todo_plan',
    'project_work', 'mcp__aitelier__state_graph_write', 'mcp__aitelier__state_graph_read', 'web_fetch', 'some_new_tool'])
    assert.equal(refusal(steward, exec(name)), `${name} is not available to this role`, name);
  for (const name of steward.presented) assert.equal(refusal(steward, exec(name)), undefined, name);
  const registered = ['bash', 'memory_search', 'memory_read', 'portfolio_projects', 'handoff', 'send_user_message', 'call_tool'].map(name => ({ name }));
  assert.deepEqual(presentTools(registered, steward).map(t => t.name), ['memory_search', 'memory_read', 'portfolio_projects', 'handoff', 'send_user_message']);
  // The director and the worker are untouched by the steward's closed set.
  const effective = aitelierRoles(table);
  assert.equal(refusal(effective.director, exec('some_new_tool')), undefined);
  assert.equal(refusal(effective.worker, exec('some_new_tool')), undefined);
});

const env = (extra = {}) => ({ AITELIER_DSH_PROJECT: 'aitelier-home', AITELIER_DSH_SESSION: 's-1', AITELIER_DSH_PROVIDER: 'aitelier',
  AITELIER_DSH_MODEL: 'm', AITELIER_DSH_STATE_DIR: '/tmp/x', AITELIER_DSH_ENTRY: '/tmp/dsh.mjs', AITELIER_DRIVER_ID: 'drv-home',
  AITELIER_DSH_EXECUTION: 'host', ...extra });

test('the AItelier host started as the steward: steward root, memory, portfolio and handoff, no full access; a director host is unchanged', () => {
  const steward = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward' }));
  assert.equal(steward.config.rootRole, 'steward');
  assert.deepEqual(steward.config.memory, { userId: 'owner' });
  assert.equal(steward.config.permissionPreset, undefined, 'the steward never runs with danger-full-access');
  assert.deepEqual(steward.capabilities.slice(-3), ['memory', 'portfolio', 'handoff']);
  assert.doesNotThrow(() => validateHost(steward));
  const director = aitelierHost(env());
  assert.equal(director.config.rootRole, undefined);
  assert.equal(director.config.memory, undefined);
  assert.equal(director.config.permissionPreset, 'danger-full-access');
  assert.deepEqual(director.capabilities, ['progress', 'workState', 'heldWork', 'roles', 'install', 'setup', 'texts', 'labels', 'attention']);
  for (const op of ['memory', 'portfolio', 'handoff']) assert.equal(director[op], undefined, op);
  assert.doesNotThrow(() => validateHost(director));
  // A root role the table does not have is refused when the host is loaded.
  const rolesFile = join(mkdtempSync(join(tmpdir(), 'roles-')), 'roles.json');
  writeFileSync(rolesFile, JSON.stringify(variant(t => { delete t.steward; })));
  assert.throws(() => validateHost({ ...steward, config: { ...steward.config, rolesFile } }), /rolesFile steward prompt undefined is not next to/);
  assert.throws(() => validateHost({ ...director, config: { ...director.config, rootRole: 'worker' } }), /rootRole must be one of director, steward/);
});

/** A fake State: one conversation (newest first, paged) and the writes it was sent. */
function fakeState(messages, { pageSize } = {}) {
  const sent = [], reads = [];
  const request = async (path, body) => {
    reads.push(path);
    if (path === '/api/state/query/list_user_conversation') {
      if (body.project_id !== 'aitelier-home') return { messages: [], next_cursor: null };
      const newest = [...messages].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
      const start = body.cursor ? Number(body.cursor) : 0, size = Math.min(body.limit, pageSize ?? body.limit);
      const page = newest.slice(start, start + size);
      return { messages: page, next_cursor: start + size < newest.length ? String(start + size) : null };
    }
    if (path === '/api/state/commands/send_director_message') {
      const prior = sent.find(s => s.request_key === body.request_key);
      if (prior && JSON.stringify(prior) !== JSON.stringify(body)) throw new Error('AItelier send_director_message: HTTP 409 {"code":"idempotency_conflict"}');
      sent.push(body); return { message_id: 'dm-1' };
    }
    throw new Error('unexpected ' + path);
  };
  return { request, sent, reads };
}
const owner = (id, at, body) => ({ message_id: id, created_at: at, sender_kind: 'user', sender_driver_id: null, body, segments: [], reply_to_message_id: null });
const mine = (id, at, body, reply) => ({ message_id: id, created_at: at, sender_kind: 'driver', sender_driver_id: 'drv-home', body, segments: [], reply_to_message_id: reply ?? null });

test('conversation turns: an owner message opens a turn, the steward\'s replies join it, other senders stay out', () => {
  const turns = conversationTurns([
    mine('s2', '2026-11-17T10:03Z', 'second reply'),
    owner('o1', '2026-11-17T10:00Z', '想做个记账工具'),
    mine('s1', '2026-11-17T10:01Z', '记下了', 'o1'),
    { ...mine('x1', '2026-11-17T10:02Z', 'someone else'), sender_driver_id: 'other' },
    mine('s0', '2026-11-17T09:00Z', 'before any owner message'),
    owner('o2', '2026-11-17T11:00Z', 'next'),
    mine('s3', '2026-11-17T11:05Z', 'late reply to the first', 'o1'),
  ], 'drv-home');
  assert.deepEqual([...turns].map(([turn, parts]) => [turn, parts.map(p => `${p.role}:${p.part}`)]), [
    ['s0', ['steward:s0']],
    ['o1', ['owner:o', 'steward:s1', 'steward:s2', 'steward:s3']],
    ['o2', ['owner:o']],
  ]);
});

test('sync: the conversation lands in memory as turns, once; a reply recorded later joins its turn; forgotten stays forgotten', async () => {
  const store = await MemoryStore.open({ root: mkdtempSync(join(tmpdir(), 'steward-mem-')), userId: 'owner' });
  const conversation = [owner('o1', '2026-11-17T10:00:00Z', '想做个记账工具')];
  const state = fakeState(conversation);
  const sync = () => syncConversation({ request: state.request, project: 'aitelier-home', me: 'drv-home', memory: store });
  assert.deepEqual(await sync(), { written: 1 });
  conversation.push(mine('s1', '2026-11-17T10:01:00Z', '记下了，先列需求', 'o1'));
  assert.deepEqual(await sync(), { written: 1 }, 'only the reply is new');
  assert.deepEqual(await sync(), { written: 0 }, 'a replay writes nothing');
  const read = await store.read(['t-o1-o'], { withTurn: true });
  assert.deepEqual(read.entries.map(e => [e.id, e.role, e.text, e.context ?? false]),
    [['t-o1-o', 'owner', '想做个记账工具', false], ['t-o1-s1', 'steward', '记下了，先列需求', true]]);
  await store.forget(['t-o1-o'], { reason: 'asked', by: 'owner' });
  assert.deepEqual(await sync(), { written: 0 }, 'a forgotten message is not written back by the next sync');
  assert.equal((await store.search('记账')).grep.hits.length, 0);
  await store.close();
});

test('sync pages back only until it meets an owner message memory already has', async () => {
  const store = await MemoryStore.open({ root: mkdtempSync(join(tmpdir(), 'steward-mem-')), userId: 'owner' });
  const conversation = Array.from({ length: 5 }, (_, i) => owner('o' + i, `2026-11-17T10:0${i}:00Z`, 'msg ' + i));
  const state = fakeState(conversation, { pageSize: 2 });
  const sync = () => syncConversation({ request: state.request, project: 'aitelier-home', me: 'drv-home', memory: store });
  assert.deepEqual(await sync(), { written: 5 }, 'a first sync walks every page');
  conversation.push(owner('o5', '2026-11-17T10:05:00Z', 'msg 5'));
  state.reads.length = 0;
  assert.deepEqual(await sync(), { written: 1 });
  assert.equal(state.reads.length, 1, 'the newest page already holds a known owner message: one read');
  await store.close();
});

test('owner-message check and handoff: only the owner\'s own message, never to the steward\'s project, one handoff per message and project', async () => {
  const state = fakeState([owner('o1', '2026-11-17T10:00:00Z', 'tell wuxia to pause the save rows'), mine('s1', '2026-11-17T10:01:00Z', 'ok', 'o1')]);
  const ops = stewardOps({ request: state.request, project: 'aitelier-home', me: 'drv-home' });
  assert.equal(await ops.memory.ownerMessage('o1'), true);
  assert.equal(await ops.memory.ownerMessage('s1'), false, 'the steward\'s own message is not the owner\'s');
  assert.equal(await ops.memory.ownerMessage('nope'), false);
  assert.equal((await ops.handoff({ projectId: 'wuxia', text: 'pause', citesUserMessageId: 's1' })).refused, 'not_an_owner_message');
  assert.equal((await ops.handoff({ projectId: 'aitelier-home', text: 'x', citesUserMessageId: 'o1' })).refused, 'invalid_project');
  assert.equal(state.sent.length, 0, 'a refused handoff sends nothing');
  const done = await ops.handoff({ projectId: 'wuxia', text: 'Your owner asks you to pause the save rows.', citesUserMessageId: 'o1' });
  assert.equal(done.handed_off, true);
  assert.deepEqual({ ...state.sent[0], body: undefined }, { sender_project_id: 'aitelier-home', director_identity: 'drv-home',
    target_project_id: 'wuxia', request_key: 'handoff:o1:wuxia', subject: 'From your owner, via the steward', body: undefined });
  assert.match(state.sent[0].body, /^Your owner asks you to pause the save rows\.\n\nYour owner wrote \(message o1, .*\):\n> tell wuxia to pause the save rows$/);
  assert.equal((await ops.handoff({ projectId: 'wuxia', text: 'Your owner asks you to pause the save rows.', citesUserMessageId: 'o1' })).handed_off, true, 'a retry is a replay');
  assert.equal((await ops.handoff({ projectId: 'wuxia', text: 'other words', citesUserMessageId: 'o1' })).refused, 'already_handed_off');
});

test('portfolio: every other project with its director and open questions; one project; one conversation page', async () => {
  const calls = [];
  const request = async (path, body) => {
    calls.push([path, body]);
    if (path === '/api/state/query/list_projects') return { result: [{ project_id: 'aitelier-home', title: 'Home' }, { project_id: 'wuxia', title: 'Wuxia' }, { project_id: 'idle', title: 'Idle' }] };
    if (path === '/api/dsh-drivers') return { projects: { wuxia: { online: 1, managed: { phase: 'dormant' } } } };
    if (path === '/api/state/query/list_user_questions') return body.project_id === 'wuxia'
      ? { questions: [{ segment_id: 'q1' }], has_more: false } : { questions: [{}, {}], has_more: true };
    return { result: { path, body } };
  };
  const ops = stewardOps({ request, project: 'aitelier-home', me: 'drv-home' });
  assert.deepEqual(await ops.portfolio.projects(), { projects: [
    { project_id: 'wuxia', title: 'Wuxia', director: { phase: 'dormant', online: 1 }, open_questions: 1 },
    { project_id: 'idle', title: 'Idle', director: null, open_questions: '2+' }] });
  const one = await ops.portfolio.project('wuxia');
  assert.deepEqual(Object.keys(one), ['project_id', 'overview', 'open_issues', 'open_questions']);
  assert.deepEqual((await ops.portfolio.conversation('wuxia', 'c-1')).body, { project_id: 'wuxia', limit: 30, cursor: 'c-1' });
  assert.ok(calls.every(([path]) => path.startsWith('/api/state/query/') || path === '/api/dsh-drivers'), 'reads only');
});
