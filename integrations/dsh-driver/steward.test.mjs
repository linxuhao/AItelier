/** Plugin step 12 (design-steward 4), the JS side: the steward role in the table, its
 *  closed tool set, the AItelier host started as the steward, the owner's conversation
 *  recorded into memory, the owner-message check, the handoff, the portfolio reads and
 *  the reader for the inbox the wake only injects an excerpt of.
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
import { aitelierHost, INBOX_EXCERPT_CHARS } from './runtime.mjs';
import { conversationTurns, stewardOps, syncConversation, replyToLetter, unansweredOwnerMessages,
  turnWatcher, forwardFinalText, stewardTexts } from './steward-host.mjs';
import { PORTFOLIO_TOOLS, registerPortfolioTools } from '../dsh-director/lib/portfolio.mjs';
import { validateHost } from '../dsh-director/lib/host.mjs';
import { MemoryStore } from '../dsh-director/lib/memory/store.mjs';

const text = readFileSync(new URL('../dsh-director/roles.json', import.meta.url), 'utf8');
const table = loadRoles();
const variant = edit => { const copy = JSON.parse(text); edit(copy); return copy; };
const exec = name => ({ name, arguments: {} });
/** Register what the plugin registers for a host, without DSH. */
const collectPortfolio = options => {
  const tools = new Map();
  registerPortfolioTools({ tools: { register: t => tools.set(t.name, t) } },
    { defineTool: t => t, jsonOutput: {}, ...options });
  return tools;
};

test('the steward is an optional role: loaded from roles.json, absent is fine, its keys are checked', () => {
  assert.equal(table.steward.prompt, 'steward.md');
  assert.ok(readFileSync(new URL('../dsh-director/steward.md', import.meta.url), 'utf8').length > 500);
  assert.equal(table.steward.presented_only, true);
  assert.doesNotThrow(() => validateRoles(variant(t => { delete t.steward; })), 'a table without a steward is the table before step 12');
  assert.throws(() => validateRoles(variant(t => { t.steward.models = []; })), /unknown key steward\.models/);
  assert.throws(() => validateRoles(variant(t => { delete t.steward.forbidden; })), /missing key steward\.forbidden/);
  assert.throws(() => validateRoles(variant(t => { t.steward.presented_only = 'yes'; })), /steward\.presented_only must be a boolean/);
  assert.throws(() => validateRoles(variant(t => { delete t.director; })), /missing key table\.director/);
  assert.doesNotThrow(() => validateRoles(variant(t => { t.worker.presented_only = false; })), 'presented_only is open to every role');
  assert.equal(roleOf('root', 'root', 'steward'), 'steward');
  assert.equal(roleOf('child', 'root', 'steward'), 'worker');
  assert.equal(roleOf('root', 'root'), 'director', 'the default root role is unchanged');
});

test('presented_only: the steward has exactly its presented tools; everything else is refused, however it is called', () => {
  const steward = aitelierRoles(table).steward;
  assert.deepEqual(steward.presented, [...table.steward.presented, 'send_user_message', 'reply_to_director'],
    'the adapter adds only the owner message and the letter reply');
  assert.ok(table.steward.presented.includes('grep_chat_history'), 'the steward recovers exact words after compaction too');
  // The inbox reader is the steward's own and nobody else's.
  assert.ok(table.steward.presented.includes('portfolio_inbox'), 'roles.json presents the inbox reader to the steward');
  assert.equal(refusal(steward, exec('portfolio_inbox')), undefined);
  for (const name of ['bash', 'read', 'write', 'edit', 'grep', 'subagent', 'call_tool', 'search_tools', 'state_read', 'todo_plan',
    'project_work', 'mcp__aitelier__state_graph_write', 'mcp__aitelier__state_graph_read', 'web_fetch', 'some_new_tool'])
    assert.equal(refusal(steward, exec(name)), `${name} is not available to this role`, name);
  for (const name of steward.presented) assert.equal(refusal(steward, exec(name)), undefined, name);
  const registered = ['bash', 'memory_search', 'memory_read', 'portfolio_projects', 'portfolio_inbox', 'handoff', 'send_user_message', 'call_tool'].map(name => ({ name }));
  assert.deepEqual(presentTools(registered, steward).map(t => t.name), ['memory_search', 'memory_read', 'portfolio_projects', 'portfolio_inbox', 'handoff', 'send_user_message']);
  // The director and the worker are untouched by the steward's closed set.
  const effective = aitelierRoles(table);
  assert.equal(refusal(effective.director, exec('some_new_tool')), undefined);
  assert.equal(refusal(effective.worker, exec('some_new_tool')), undefined);
  for (const role of ['director', 'worker']) assert.ok(!effective[role].presented.includes('portfolio_inbox'), role);
});

const env = (extra = {}) => ({ AITELIER_DSH_PROJECT: 'aitelier-home', AITELIER_DSH_SESSION: 's-1', AITELIER_DSH_PROVIDER: 'aitelier',
  AITELIER_DSH_MODEL: 'm', AITELIER_DSH_STATE_DIR: '/tmp/x', AITELIER_DSH_ENTRY: '/tmp/dsh.mjs', AITELIER_DRIVER_ID: 'drv-home',
  AITELIER_DSH_EXECUTION: 'host', ...extra });

test('the AItelier host started as the steward: steward root, memory, portfolio and handoff, no full access; a director host is unchanged', () => {
  const steward = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward' }));
  assert.equal(steward.config.rootRole, 'steward');
  assert.deepEqual(steward.config.memory, { userId: 'owner' });
  assert.equal(steward.config.permissionPreset, undefined, 'the steward never runs with danger-full-access');
  assert.deepEqual(steward.capabilities.slice(-4), ['memory', 'portfolio', 'handoff', 'proposals']);
  assert.deepEqual(Object.keys(steward.portfolio).sort(), ['acknowledge', 'conversation', 'inbox', 'project', 'projects'],
    'the steward\'s own inbox page is one of the reads this host provides, and acknowledging a delivery is its one write');
  assert.doesNotThrow(() => validateHost(steward));
  const director = aitelierHost(env());
  assert.equal(director.config.rootRole, undefined);
  assert.equal(director.config.memory, undefined);
  assert.equal(director.config.permissionPreset, 'danger-full-access');
  assert.deepEqual(director.capabilities, ['progress', 'workState', 'heldWork', 'roles', 'install', 'setup', 'texts', 'labels', 'attention']);
  for (const op of ['memory', 'portfolio', 'handoff', 'proposals']) assert.equal(director[op], undefined, op);
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

/** A fake State for this steward's own inbox: deliveries stored whole, paged by
 *  delivery_seq the way the inbox query pages them, and every call remembered. */
function fakeInbox(bodies) {
  const calls = [];
  const deliveries = bodies.map((body, index) => ({
    message: { message_id: `m-${index + 1}`, thread_id: `thr-${index + 1}`, sender_project_id: 'wuxia',
      subject: `subject ${index + 1}`, body, created_at: `2026-11-17T10:${String(index).padStart(2, '0')}:00Z` },
    delivery: { delivery_id: `dlv-${index + 1}`, delivery_seq: index + 1, status: 'unread', version: 1 },
    acked_by_me: false }));
  const request = async (path, body) => {
    calls.push([path, body]);
    if (path !== '/api/state/query/list_director_messages') throw new Error('the inbox reader called ' + path);
    if (body.project_id !== 'aitelier-home') throw new Error('the inbox reader read another project: ' + body.project_id);
    const left = deliveries.filter(d => d.delivery.delivery_seq > body.after), page = left.slice(0, body.limit);
    return { result: { project_id: body.project_id, items: page, matched_total: left.length,
      has_more: left.length > page.length, next_after: page.length ? page.at(-1).delivery.delivery_seq : body.after } };
  };
  return { request, calls, deliveries };
}

/** The inbox reader exactly as the steward is given it: the delivered registered tool
 *  over the delivered adapter operations. */
function stewardInboxTool(request) {
  const tools = new Map();
  registerPortfolioTools({ tools: { register: tool => tools.set(tool.name, tool) } },
    { defineTool: tool => tool, jsonOutput: {}, portfolio: stewardOps({ request, project: 'aitelier-home', me: 'drv-home' }).portfolio });
  assert.deepEqual([...tools.keys()], [...PORTFOLIO_TOOLS, 'portfolio_inbox_acknowledge'],
    'the steward host registers the four portfolio reads and its one inbox write');
  return tools.get('portfolio_inbox');
}
const toolCall = { signal: AbortSignal.timeout(5000) };

test('the inbox reader (step 12 follow-up): a body longer than the injected excerpt comes back whole, equal to the stored body', async () => {
  const stored = '0123456789abcdef'.repeat(117).slice(0, 1863);
  assert.equal(stored.length, 1863);
  assert.ok(stored.length > INBOX_EXCERPT_CHARS, 'longer than the excerpt the wake injects');
  const state = fakeInbox([stored, 'tiny', 'also tiny']);
  const tool = stewardInboxTool(state.request);
  const answer = await tool.execute({ after: 0, limit: 50 }, toolCall);
  assert.equal(answer.project_id, 'aitelier-home');
  assert.deepEqual(answer.items.map(item => item.delivery_seq), [1, 2, 3]);
  const [first] = answer.items;
  assert.deepEqual({ delivery_id: first.delivery_id, delivery_seq: first.delivery_seq, message_id: first.message_id,
    body_chars: first.body_chars }, { delivery_id: 'dlv-1', delivery_seq: 1, message_id: 'm-1', body_chars: 1863 });
  assert.equal(first.body, stored, 'the whole stored body, not an excerpt');
  assert.equal(first.body_truncated, undefined);
  assert.equal(answer.matched_total, 3);
  assert.equal(answer.has_more, false);
  assert.equal(answer.next_after, null);
  // The bounds the description states are the bounds the answer reports, and the request
  // carried the per-page bound, not the 50 the caller asked for.
  const stated = tool.description.match(/at most (\d+) deliveries, one body at most (\d+) characters, and the whole answer is capped at (\d+) characters/);
  assert.ok(stated, tool.description);
  assert.deepEqual(answer.limits, { items_per_page: Number(stated[1]), body_chars: Number(stated[2]), answer_chars: Number(stated[3]) });
  assert.deepEqual(state.calls, [['/api/state/query/list_director_messages', { project_id: 'aitelier-home',
    protocol_version: 'v3', statuses: ['unread', 'acknowledged'], after: 0, limit: answer.limits.items_per_page }]]);
});

test('inbox bounds: a body past the per-body bound carries its true body_chars and a truncation flag; a page past the item bound carries matched_total, has_more and the next cursor', async () => {
  const enormous = 'y'.repeat(9000);
  const state = fakeInbox([enormous, ...Array.from({ length: 11 }, (_, i) => 'small ' + i)]);
  const tool = stewardInboxTool(state.request);
  const first = await tool.execute({ after: 0, limit: 50 }, toolCall);
  assert.equal(first.items.length, 5, 'the item bound holds, though 12 are waiting');
  const [cut] = first.items;
  assert.equal(cut.body_chars, 9000, 'the true size of the stored body');
  assert.equal(cut.body_truncated, true);
  assert.equal(cut.body, enormous.slice(0, first.limits.body_chars));
  assert.equal(first.matched_total, 12);
  assert.equal(first.has_more, true);
  assert.equal(first.next_after, 5);
  const second = await tool.execute({ after: first.next_after, limit: 50 }, toolCall);
  assert.deepEqual(second.items.map(item => item.delivery_seq), [6, 7, 8, 9, 10]);
  assert.equal(second.matched_total, 7);
  assert.equal(second.has_more, true);
  assert.equal(second.next_after, 10);
  const last = await tool.execute({ after: second.next_after, limit: 50 }, toolCall);
  assert.deepEqual(last.items.map(item => item.delivery_seq), [11, 12]);
  assert.equal(last.has_more, false);
  assert.equal(last.next_after, null);
  assert.ok(state.calls.every(([path]) => path === '/api/state/query/list_director_messages'), 'read-only: one query, no acknowledgement, no write');
});

test('the inbox answer itself is bounded: a page of long bodies is cut and counted, and its cursor still moves', async () => {
  const state = fakeInbox(Array.from({ length: 5 }, () => 'z'.repeat(9000)));
  const tool = stewardInboxTool(state.request);
  const answer = await tool.execute({ after: 0, limit: 5 }, toolCall);
  assert.ok(JSON.stringify(answer).length <= answer.limits.answer_chars, 'one call cannot flood the steward\'s context');
  assert.ok(answer.items.length >= 1 && answer.items.length < 5, 'fewer than the page asked for, not more than the cap');
  assert.equal(answer.items_omitted, 5 - answer.items.length);
  assert.equal(answer.has_more, true);
  assert.equal(answer.next_after, answer.items.at(-1).delivery_seq, 'the cursor resumes after the last delivery it showed');
});

test('proposals (step 13): propose records one open proposal under a digest key; withdraw names the proposal; a refusal is an answer', async () => {
  const calls = [];
  const request = async (path, body) => {
    calls.push([path, body]);
    if (body.proposal_id === 'p-gone') throw new Error('AItelier withdraw_steward_proposal: HTTP 409 {"code":"proposal_not_open"}');
    return path.endsWith('create_steward_proposal') ? { result: { proposal_id: 'p-1', version: 1, status: 'open' } } : { result: { withdrawn: true } };
  };
  const { proposals } = stewardOps({ request, project: 'aitelier-home', me: 'drv-home' });
  const proposal = { kind: 'start_driver', payload: { project_id: 'wuxia' }, reason: 'your owner wants wuxia running again' };
  assert.deepEqual(await proposals.propose(proposal), { proposal_id: 'p-1', version: 1, status: 'open' });
  await proposals.propose(proposal);
  assert.equal(calls[0][1].request_key, calls[1][1].request_key, 'the same proposal is the same request: a replay, not a second card');
  assert.match(calls[0][1].request_key, /^proposal:[0-9a-f]{40}$/);
  assert.deepEqual({ ...calls[0][1], request_key: undefined }, { request_key: undefined, ...proposal });
  await proposals.propose({ ...proposal, reason: 'other words' });
  assert.notEqual(calls[2][1].request_key, calls[0][1].request_key);
  assert.deepEqual(await proposals.withdraw({ id: 'p-1', reason: 'owner changed their mind' }), { withdrawn: true });
  assert.deepEqual(calls[3], ['/api/state/commands/withdraw_steward_proposal', { request_key: 'withdraw-proposal:p-1', proposal_id: 'p-1', reason: 'owner changed their mind' }]);
  assert.equal((await proposals.withdraw({ id: 'p-gone', reason: 'x' })).refused, 'state_refused');
  assert.ok(calls.every(([path]) => /\/(create|withdraw)_steward_proposal$/.test(path)), 'nothing but the two steward writes');
});

test('owner memory requests (step 14): each kind runs against the store as the owner; results go back; an unwritten result runs again', async () => {
  const { runOwnerMemoryRequest, processMemoryRequests, MEMORY_REQUEST_EVENTS } = await import('./memory-requests.mjs');
  const store = await MemoryStore.open({ root: mkdtempSync(join(tmpdir(), 'steward-mem-')), userId: 'owner' });
  await store.recordTurn('o1', [{ role: 'owner', part: 'o', text: 'secret account 4242' }, { role: 'owner', part: 'x', text: 'keep this' }]);
  const run = (kind, payload) => runOwnerMemoryRequest(store, { kind, payload });
  const profile = await run('profile_read');
  assert.equal(profile.text, '');
  const updated = await run('profile_update', { text: 'likes short replies', expected_version: profile.version });
  assert.equal(updated.updated, true);
  assert.equal((await run('profile_update', { text: 'again', expected_version: profile.version })).refused, 'version_conflict', 'a repeat with the old version is refused');
  assert.deepEqual((await run('forget', { ids: ['t-o1-o'] })).forgotten, ['t-o1-o']);
  assert.deepEqual((await run('forgotten_list')).forgotten.map(r => [r.id, r.by]), [['t-o1-o', 'owner']]);
  assert.deepEqual((await run('restore', { id: 't-o1-o' })).restored, 't-o1-o');
  assert.equal((await run('purge', { ids: ['t-o1-o'] })).purged, true);
  assert.deepEqual((await store.read(['t-o1-o'])).not_found, ['t-o1-o']);
  assert.equal((await run('deploy')).refused, 'unknown_kind');
  assert.deepEqual(await runOwnerMemoryRequest(undefined, { kind: 'profile_read' }), { refused: 'memory_unavailable' });
  // The loop: every pending row runs and is completed; a completion that fails stays pending.
  const completed = [];
  let failOnce = true;
  const request = async (path, body) => {
    if (path.endsWith('list_steward_memory_requests')) return { result: { requests: [{ request_id: 'r1', kind: 'forgotten_list' }, { request_id: 'r2', kind: 'profile_read' }] } };
    if (body.request_id === 'r2' && failOnce) { failOnce = false; throw new Error('HTTP 503'); }
    completed.push(body); return { result: { completed: true } };
  };
  const logged = [];
  assert.deepEqual(await processMemoryRequests({ request, memory: store, log: line => logged.push(line) }), { pending: 2, done: 1 });
  assert.match(logged[0], /memory request r2: result not written/);
  assert.deepEqual(await processMemoryRequests({ request, memory: store }), { pending: 2, done: 2 });
  assert.deepEqual(completed.map(c => [c.request_key, c.request_id]), [['memory-request:r1', 'r1'], ['memory-request:r1', 'r1'], ['memory-request:r2', 'r2']]);
  assert.equal(completed.at(-1).result.text, 'likes short replies');
  assert.ok(MEMORY_REQUEST_EVENTS.has('steward_memory_request_created'));
  await store.close();
});

test('inbox acknowledgement: the registered tool writes ONE acknowledge for the delivery the wake showed, and a retry is the same request', async () => {
  const calls = [];
  const request = async (path, body) => {
    calls.push([path, body]);
    if (path !== '/api/state/commands/acknowledge_director_message') throw new Error('unexpected ' + path);
    if (body.delivery_id === 'd-gone') {
      // The director-messaging contract's refusal envelope, not an HTTP error.
      return { schema: 'aitelier.director-messaging.v3', code: 'unknown_delivery',
        detail: { message: 'unknown_delivery' } };
    }
    return { schema: 'aitelier.director-messaging.v3', result: {
      delivery: { delivery_id: body.delivery_id, status: 'resolved', version: body.expected_version + 1 },
      replayed: calls.length > 1 } };
  };
  const ops = stewardOps({ request, project: 'aitelier-home', me: 'drv-home' });
  const tools = collectPortfolio({ portfolio: ops.portfolio });
  assert.ok(tools.has('portfolio_inbox_acknowledge'), 'registered when the host acknowledges');
  const tool = tools.get('portfolio_inbox_acknowledge');
  const first = await tool.execute({ delivery_id: 'd-1', version: 1 }, exec);
  assert.deepEqual(first.delivery, { delivery_id: 'd-1', status: 'resolved', version: 2 });
  assert.equal(calls.length, 1, 'one acknowledgement is one write');
  assert.deepEqual(calls[0], ['/api/state/commands/acknowledge_director_message', {
    project_id: 'aitelier-home', protocol_version: 'v3', delivery_id: 'd-1', expected_version: 1,
    request_key: 'steward-ack:d-1:1' }], 'the wake\'s delivery identity and version, its own project only');
  // Same delivery and version = same request key: State replays it, no second write.
  assert.equal((await tool.execute({ delivery_id: 'd-1', version: 1 }, exec)).replayed, true);
  assert.equal(calls[1][1].request_key, calls[0][1].request_key);
  assert.ok(calls.every(([path]) => path === '/api/state/commands/acknowledge_director_message'), 'nothing else was written');
  // An unknown delivery is a named refusal, never a silent success.
  const unknown = await tool.execute({ delivery_id: 'd-gone', version: 1 }, exec);
  assert.deepEqual({ refused: unknown.refused, code: unknown.code }, { refused: 'state_refused', code: 'unknown_delivery' });
  // What it cannot identify writes nothing at all.
  const before = calls.length;
  assert.equal((await tool.execute({ delivery_id: '', version: 1 }, exec)).refused, 'invalid_delivery');
  assert.equal((await tool.execute({ delivery_id: 'd-1', version: 0 }, exec)).refused, 'invalid_version');
  assert.equal(calls.length, before, 'a refused call writes nothing');
  // A host that does not offer the acknowledgement registers no such tool.
  const readOnly = collectPortfolio({ portfolio: { projects: async () => ({}), project: async () => ({}),
    conversation: async () => ({}) } });
  assert.equal(readOnly.has('portfolio_inbox_acknowledge'), false);
});

/** A fake State holding one letter in each inbox and recording every write.
 *  `refuse` makes the write answer the way the real server answers a refused request: a
 *  200 whose inner result carries a code and a message (never a thrown error). */
function letters({ refuse = null } = {}) {
  const writes = [];
  const request = async (path, body) => {
    if (path.endsWith('/list_director_messages')) return { result: { items: [{ message: { message_id: 'm1', subject: 'Steward boot',
      sender_project_id: 'aitelier' }, delivery: { delivery_id: 'd-proj', version: 2, status: 'unread', delivery_seq: 1 } }], has_more: false } };
    if (path.endsWith('/list_driver_messages')) return { driver_id: 'drv-home', truncated: false, next_after: 1,
      messages: [{ delivery_id: 'd-priv', version: 1, status: 'unread', subject: 'ping', sender_driver_id: 'drv-plat' }] };
    writes.push([path.split('/').at(-1), body]);
    if (refuse) return { result: { schema: 'aitelier.director-messaging.v3', code: refuse,
      detail: { message: 'target_project_id / broadcast / reply_to_delivery_id: choose exactly one destination; replies omit target_project_id' } } };
    return { result: { ok: true } };
  };
  return { request, writes, base: { request, project: 'aitelier-home', me: 'drv-home' } };
}

test('letters (owner 2026-11-17): a reply goes back to the sender in either inbox, threaded on the cited delivery', async () => {
  const { writes, base } = letters();
  assert.deepEqual(await replyToLetter({ ...base, inbox: 'project', deliveryId: 'd-proj', text: '收到，已读完整封信' }),
    { replied: true, inbox: 'project', to: 'aitelier', ok: true });
  assert.deepEqual(writes[0][0], 'send_director_message');
  // ONE destination: the cited delivery. Sending target_project_id as well is refused by
  // the real server with invalid_request (measured 2026-11-17, iss-8ba3f8ce4b254704), so
  // the assertion is that the key is absent, not that it names the same sender.
  assert.equal('target_project_id' in writes[0][1], false, 'a reply omits target_project_id');
  assert.deepEqual({ ...writes[0][1], request_key: undefined }, { protocol_version: 'v3', sender_project_id: 'aitelier-home',
    director_identity: 'drv-home', request_key: undefined, subject: 'Re: Steward boot', body: '收到，已读完整封信',
    reply_to_delivery_id: 'd-proj' });
  await replyToLetter({ ...base, inbox: 'private', deliveryId: 'd-priv', text: 'noted' });
  assert.deepEqual(writes[1], ['send_driver_message', { request_key: writes[1][1].request_key, subject: 'Re: ping', body: 'noted',
    target_driver_id: 'drv-plat', reply_to_delivery_id: 'd-priv' }]);
  assert.match(writes[1][1].request_key, /^reply:d-priv:[0-9a-f]{16}$/);
  // Refused before anything is written: an unknown letter, another inbox name, no text.
  assert.equal((await replyToLetter({ ...base, inbox: 'private', deliveryId: 'nope', text: 'x' })).refused, 'unknown_delivery');
  assert.equal((await replyToLetter({ ...base, inbox: 'email', deliveryId: 'd-proj', text: 'x' })).refused, 'unknown_inbox');
  assert.equal((await replyToLetter({ ...base, inbox: 'project', deliveryId: 'd-proj', text: ' ' })).refused, 'empty_text');
  assert.equal(writes.length, 2);
});

test('a refused reply is not reported as sent: a 200 with a code inside the result is a refusal in either inbox', async () => {
  for (const [inbox, deliveryId, command] of [['project', 'd-proj', 'send_director_message'],
                                             ['private', 'd-priv', 'send_driver_message']]) {
    const { writes, base } = letters({ refuse: 'invalid_request' });
    const answer = await replyToLetter({ ...base, inbox, deliveryId, text: '收到' });
    assert.equal(writes.length, 1, `${inbox}: the write was attempted`);
    assert.equal(writes[0][0], command);
    assert.equal(answer.replied, undefined, `${inbox}: a refusal must not claim a sent reply`);
    assert.equal(answer.refused, 'invalid_request', `${inbox}: the refusal names the server's code`);
    assert.match(String(answer.reason), /choose exactly one destination|one destination/,
      `${inbox}: the server's own message is carried, so the steward can act on it`);
  }
});

test('every owner message gets an answer: the unanswered ones; the last words of a finished turn go to the owner; dormancy is held', async () => {
  const conversation = [owner('o1', '2026-11-17T10:00:00Z', 'first'), mine('s1', '2026-11-17T10:01:00Z', 'answer'),
    owner('o2', '2026-11-17T10:02:00Z', 'second'), owner('o3', '2026-11-17T10:03:00Z', 'third')];
  const sent = [];
  let conflict = false;
  const request = async (path, body) => {
    if (path.endsWith('/list_user_conversation')) return { messages: conversation, next_cursor: null };
    if (conflict) throw new Error('AItelier send_user_message: HTTP 409 {"code":"idempotency_conflict"}');
    sent.push(body); return { result: { message_id: 'r1' } };
  };
  const base = { request, project: 'aitelier-home', me: 'drv-home' };
  assert.deepEqual((await unansweredOwnerMessages(base)).map(m => m.message_id), ['o2', 'o3']);
  // A turn: what it was given, tool-call text first, the final words last; only a completed turn forwards.
  const said = [];
  const watch = turnWatcher((text, input) => said.push([text, input]));
  const assistant = text => ({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text }] } } });
  const given = text => ({ type: 'user/message', data: { message: { content: [{ type: 'text', text }] } } });
  for (const e of [{ type: 'turn/start' }, given('AItelier runtime event: {"driver_inbox":[{"message_id":"o3"}]}'), assistant('I will look first'),
    assistant('Both done: the ledger is set up.'), { type: 'turn/end', data: { reason: { kind: 'completed' } } }]) watch(e);
  for (const e of [{ type: 'turn/start' }, assistant('half a thought'), { type: 'turn/end', data: { reason: { kind: 'interrupted' } } }]) watch(e);
  assert.equal(said.length, 1);
  assert.equal(said[0][0], 'Both done: the ledger is set up.');
  // F3a: a turn that was not given the waiting owner message (a director's letter woke it) sends nothing.
  assert.deepEqual(await forwardFinalText({ ...base, text: 'about the director letter', input: 'Pending project inbox: d-1' }), { forwarded: false });
  assert.equal(sent.length, 0);
  const forwarded = await forwardFinalText({ ...base, text: said[0][0], input: said[0][1] });
  assert.deepEqual(forwarded, { forwarded: true, reply_to: 'o3', message_id: 'r1' });
  assert.deepEqual(sent[0], { request_key: 'auto-reply:o3', project_id: 'aitelier-home', subject: 'Re: ',
    body: 'Both done: the ledger is set up.', reply_to_message_id: 'o3' }, 'one key per owner message, whatever the text');
  // F3c: a second automatic reply to the same message (other words, after a lost answer) is refused, not sent twice.
  conflict = true;
  assert.deepEqual(await forwardFinalText({ ...base, text: 'other words', input: 'o3' }), { forwarded: false, already_replied: 'o3' });
  conflict = false;
  conversation.push(mine('s2', '2026-11-17T10:04:00Z', 'Both done', 'o3'));
  assert.deepEqual(await forwardFinalText({ ...base, text: 'more', input: 'o2 o3' }), { forwarded: false }, 'nobody waiting: nothing is sent');
  assert.equal(sent.length, 1);
  // The steward host's held work is exactly the unanswered owner messages (idle_dormant refuses on it).
  conversation.push(owner('o4', '2026-11-17T10:05:00Z', 'fourth'));
  const host = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward' }), { request });
  assert.deepEqual(await host.heldWork(), [{ node_key: 'owner message o4' }]);
  assert.equal(host.texts.heldWork, stewardTexts.heldWork);
  assert.equal(host.texts.tools.idle_dormant, stewardTexts.idleDormant);
  assert.notEqual(aitelierHost(env(), { request }).texts.heldWork, stewardTexts.heldWork, 'a director keeps its wording');
});

test('iss-4a503609 (a)(b): the steward\'s texts name only what it has; the director\'s are byte-for-byte today\'s', async () => {
  const directorTexts = await import('./aitelier-texts.mjs');
  const steward = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward' })).texts, director = aitelierHost(env()).texts;
  // The director is unchanged: the module's own constants, byte for byte.
  assert.equal(director, directorTexts.texts);
  const effective = aitelierRoles(table);
  const presented = new Set(effective.steward.presented);
  const anyTool = new Set(['director', 'worker', 'steward'].flatMap(role => [...effective[role].presented, ...effective[role].forbidden])
    .concat(['state_read', 'list_user_questions', 'project_work', 'watch_process', 'todo_plan']));
  const lines = { continuationQuestion: steward.continuationQuestion, reviewInstruction: steward.reviewInstruction,
    askTheUser: steward.askTheUser, heldWork: steward.heldWork, ...Object.fromEntries(['idle_dormant', 'ask_owner', 'close_question']
      .map(name => ['tools.' + name, steward.tools[name]])) };
  for (const [key, text] of Object.entries(lines)) {
    assert.equal(typeof text, 'string', key);
    for (const phrase of ['ready work items', 'ready frontier nodes', 'open issues', 'worker capacity', 'watch_process', 'State claim',
      'list_user_questions', 'Director only', 'todo plan', 'live child'])
      assert.ok(!text.includes(phrase), `${key} names "${phrase}"`);
    const named = (text.match(/\b[a-z]+(?:_[a-z]+)+\b/g) ?? []).filter(word => anyTool.has(word));
    assert.deepEqual(named.filter(word => !presented.has(word)), [], `${key} names a tool the steward does not have`);
  }
  // It asks the steward for what it can do.
  for (const tool of ['send_user_message', 'reply_to_director', 'portfolio_inbox_acknowledge', 'handoff', 'ask_owner'])
    assert.ok(steward.continuationQuestion.includes(tool) || steward.askTheUser.includes(tool), tool);
  assert.match(steward.tools.idle_dormant, /Nothing is expected of you until your owner or a director writes/);
  assert.notEqual(director.tools.idle_dormant, steward.tools.idle_dormant);
});
