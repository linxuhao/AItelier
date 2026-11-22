/** Step 24.1b (design-steward 7.8.9): request_confirmation and run_confirmed (lib/confirm.mjs)
 *  and the turn's requester (lib/catalog.mjs), without DSH: the requester is code's - the
 *  newest person's message of the turn's input - never the model's; run_confirmed runs the
 *  STORED name and arguments through the ordinary guards. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { StewardCatalog } from '../lib/catalog.mjs';
import { registerConfirmTools } from '../lib/confirm.mjs';

const SEND = 'mcp__sw__mailpub__send_message', SEARCH = 'mcp__sw__mailpub__search_messages';
const CACHE = {
  connections: [{ conn_id: 'mailpub', visibility: 'public', usable: true, tools: [
    { name: SEND, egress: true, outward: true }, { name: SEARCH, egress: false, outward: false }] }],
  effective_tools: [SEARCH, SEND], effective_hash: 'a'.repeat(64),
};
const wake = (...ids) => 'EVENT:\n' + JSON.stringify({ events: [], driver_inbox: ids.map(id => ({ message_id: id, sender_kind: 'user' })) });
const userMessage = text => ({ type: 'user/message', data: { message: { content: [{ type: 'text', text }] } } });

function fixture({ stateDir, turn = { kind: 'owner' }, questions, openTurn = () => '5' } = {}) {
  const calls = [];
  const dir = stateDir ?? mkdtempSync(join(tmpdir(), 'confirm-'));
  const host = {
    connections: async () => JSON.parse(JSON.stringify(CACHE)),
    resyncDue: async () => {},
    humanInput: text => text.startsWith('EVENT:'),
    requesterOf: text => { const inbox = JSON.parse(text.slice(text.indexOf('\n') + 1)).driver_inbox; return inbox.at(-1)?.message_id; },
  };
  const catalog = new StewardCatalog({ host, stateDir: dir, rootSession: 'root', registryNames: () => CACHE.effective_tools,
    audience: () => turn.audience ?? turn });
  const record = { status: 'confirmed', name: SEND, arguments: { to: 'a@example.com', body: 'stored' } };
  const confirm = {
    create: async (request) => { calls.push(['create', request]); return { code: 'K7Q2A', text: 'TEXT', reply_to_message_id: request.requesterMessageId, expires_at: 'e',
      ...(request.audience === 'group' ? { channel: 'wechat', group_ref: 'room1' } : { channel: 'slack' }) }; },
    deliver: async (message) => { calls.push(['deliver', message]); return delivery.answer; },
    get: async (code, _signal, options) => { calls.push(['get', code, options]); return { code, ...record }; },
    cancel: async code => { calls.push(['cancel', code]); return { code, status: 'cancelled' }; },
  };
  const delivery = { answer: { message_id: 'm-out' } };
  const tools = new Map(), executed = [];
  const scope = { tools: { register: tool => tools.set(tool.name, tool) } };
  const ctx = { tools: { execute: async request => { executed.push(request); return { value: { sent: true }, content: [{ type: 'text', text: 'sent' }] }; } } };
  class HarnessError extends Error { constructor(message, code) { super(message); this.code = code; } }
  registerConfirmTools(scope, { ctx, defineTool: t => t, jsonOutput: {}, HarnessError, confirm, catalog, groupQuestions: () => questions?.ids,
    currentTurn: openTurn });
  const run = (name, args, session = 'root') => tools.get(name).execute(args, { agent: { session: { id: session } }, signal: undefined,
    callId: 'c1', rootCallId: 'r1', token: 'tok-1' });
  return { catalog, calls, record, run, executed, dir, delivery, tools };
}

test('request_confirmation: no person\'s message in the turn is no_requester; the newest one is the requester', async () => {
  const f = fixture();
  await f.catalog.preStep();
  f.catalog.observe({ type: 'turn/start', seq: 1 });
  f.catalog.observe(userMessage('Pending project inbox:\n{"letters": []}'));
  assert.equal((await f.run('request_confirmation', { name: SEND, arguments: { to: 'x' } })).refused, 'no_requester');
  f.catalog.observe(userMessage(wake('m-1', 'm-2')));
  const answer = await f.run('request_confirmation', { name: SEND, arguments: { to: 'a@example.com' } });
  assert.deepEqual(answer, { code: 'K7Q2A', status: 'pending', expires_at: 'e' });
  assert.deepEqual(f.calls, [['create', { name: SEND, arguments: { to: 'a@example.com' }, requesterMessageId: 'm-2', audience: 'owner' }],
    ['deliver', { code: 'K7Q2A', text: 'TEXT', replyTo: 'm-2', channel: 'slack' }]]);
  // A new turn woken by something else has none again; the record survives a restart.
  const again = new StewardCatalog({ host: f.catalog.host, stateDir: f.dir, rootSession: 'root', registryNames: () => [] });
  assert.equal(again.requester, 'm-2');
  f.catalog.observe({ type: 'turn/start', seq: 2 });
  assert.equal(f.catalog.requester, undefined);
});

test('request_confirmation: the audience it names is step 23\'s open turn - a group turn, or one it cannot read, is not the owner\'s', async () => {
  const turn = { audience: { kind: 'group', channel: 'wechat', group_ref: 'room1' } };
  const questions = { ids: ['gq-1'] };
  const f = fixture({ turn, questions });
  await f.catalog.preStep();
  f.catalog.observe({ type: 'turn/start', seq: 1 });
  f.catalog.observe(userMessage(wake('m-1')));
  await f.run('request_confirmation', { name: SEND, arguments: { to: 'a@example.com' } });
  turn.audience = undefined;
  f.catalog.audience = () => { throw new Error('not yet'); };
  await f.run('request_confirmation', { name: SEND, arguments: { to: 'a@example.com' } });
  assert.deepEqual(f.calls.filter(c => c[0] === 'create').map(c => c[1].audience), ['group', 'group']);
});

test('request_confirmation in a group turn (7.8.9): the requester is the turn\'s newest question, never an owner message; the text goes to the group', async () => {
  const turn = { audience: { kind: 'group', channel: 'wechat', group_ref: 'room1' } };
  const questions = { ids: undefined };
  const f = fixture({ turn, questions });
  await f.catalog.preStep();
  f.catalog.observe({ type: 'turn/start', seq: 1 });
  // An owner message id in the input names no group requester; no question either: no_requester.
  f.catalog.observe(userMessage(wake('m-1')));
  assert.equal((await f.run('request_confirmation', { name: SEND, arguments: { to: 'a@example.com' } })).refused, 'no_requester');
  questions.ids = ['gq-1', 'gq-2'];
  const answer = await f.run('request_confirmation', { name: SEND, arguments: { to: 'anyone@example.com' } });
  assert.deepEqual(answer, { code: 'K7Q2A', status: 'pending', expires_at: 'e' });
  assert.deepEqual(f.calls, [
    ['create', { name: SEND, arguments: { to: 'anyone@example.com' }, requesterMessageId: 'gq-2', audience: 'group', questionIds: ['gq-1', 'gq-2'] }],
    ['deliver', { code: 'K7Q2A', text: 'TEXT', replyTo: 'gq-2', channel: 'wechat', groupRef: 'room1' }]]);
  // The host's refusals (ambiguous_requester, withheld, group_send_budget) reach the model as
  // they are, and nothing is delivered.
  for (const refused of ['ambiguous_requester', 'withheld', 'group_send_budget']) {
    const seen = [], tools = new Map();
    registerConfirmTools({ tools: { register: t => tools.set(t.name, t) } }, { ctx: {}, defineTool: t => t, jsonOutput: {}, HarnessError: Error,
      confirm: { create: async () => { seen.push('create'); return { refused }; }, deliver: async () => { seen.push('deliver'); return { sent: true }; } },
      catalog: f.catalog, groupQuestions: () => ['gq-3'] });
    const out = await tools.get('request_confirmation').execute({ name: SEND, arguments: { to: 'x' } }, { agent: { session: { id: 'root' } } });
    assert.deepEqual(out, { refused });
    assert.deepEqual(seen, ['create'], refused);
  }
});

test('25.3 calendar (7.9.6): a calendar write marked outward goes through request_confirmation in owner and group turns; a private calendar is not in a group turn\'s catalog', async () => {
  const CREATE = 'mcp__sw__calfam__create_event', LIST = 'mcp__sw__calfam__list_events', MINE = 'mcp__sw__calme__create_event';
  const cache = { connections: [
    { conn_id: 'calfam', visibility: 'public', usable: true, tools: [{ name: CREATE, egress: true, outward: true }, { name: LIST, egress: false, outward: false }] },
    { conn_id: 'calme', visibility: 'private', usable: true, tools: [{ name: MINE, egress: true, outward: true }] }],
    effective_tools: [CREATE, LIST, MINE], effective_hash: 'c'.repeat(64) };
  for (const kind of ['owner', 'group']) {
    const calls = [];
    const turn = { kind };
    const catalog = new StewardCatalog({ host: { connections: async () => cache, resyncDue: async () => {}, humanInput: t => t.startsWith('EVENT:'),
      requesterOf: () => 'm-1' }, stateDir: mkdtempSync(join(tmpdir(), 'cal-')), rootSession: 'root', registryNames: () => cache.effective_tools,
    audience: () => turn });
    await catalog.preStep();
    catalog.observe({ type: 'turn/start', seq: 1 });
    catalog.observe(userMessage(wake('m-1')));
    const tools = new Map();
    registerConfirmTools({ tools: { register: t => tools.set(t.name, t) } }, { ctx: {}, defineTool: t => t, jsonOutput: {}, HarnessError: Error,
      confirm: { create: async r => { calls.push(r); return { code: 'K7Q2A', text: 'T', channel: 'wechat', reply_to_message_id: r.requesterMessageId, expires_at: 'e' }; },
        deliver: async () => ({ sent: true }) }, catalog, groupQuestions: () => ['gq-1'] });
    const ask = name => tools.get('request_confirmation').execute({ name, arguments: { title: '周六聚餐', start: '2026-11-28T18:00' } },
      { agent: { session: { id: 'root' } } });
    assert.equal((await ask(CREATE)).status, 'pending', kind);
    assert.equal((await ask(LIST)).refused, 'not_outward', 'reading the calendar needs no confirmation');
    assert.equal((await ask(MINE)).refused ?? 'pending', kind === 'owner' ? 'pending' : 'not_visible_in_this_turn', kind);
    assert.deepEqual(calls.map(c => [c.name, c.audience]), kind === 'owner' ? [[CREATE, 'owner'], [MINE, 'owner']] : [[CREATE, 'group']]);
    assert.equal(catalog.catalog('root').has(MINE), kind === 'owner');
  }
});

test('request_confirmation: only an outward tool of this turn\'s catalog, only in the steward\'s own session', async () => {
  const f = fixture();
  await f.catalog.preStep();
  f.catalog.observe({ type: 'turn/start', seq: 1 });
  f.catalog.observe(userMessage(wake('m-1')));
  assert.equal((await f.run('request_confirmation', { name: SEARCH, arguments: {} })).refused, 'not_outward');
  assert.equal((await f.run('request_confirmation', { name: 'mcp__sw__other__send', arguments: {} })).refused, 'not_visible_in_this_turn');
  assert.equal((await f.run('request_confirmation', { name: 'bash', arguments: {} })).refused, 'not_visible_in_this_turn');
  assert.equal((await f.run('request_confirmation', { name: SEND, arguments: {} }, 'child')).refused, 'not_available');
  assert.equal((await f.run('run_confirmed', { code: 'K7Q2A' }, 'child')).refused, 'not_available');
  assert.equal(f.calls.length, 0);
});

test('run_confirmed runs the stored name and arguments, never the model\'s, and only a confirmed record', async () => {
  const f = fixture();
  const value = await f.run('run_confirmed', { code: 'K7Q2A', arguments: { to: 'evil@example.com' } });
  assert.deepEqual(value, { sent: true });
  assert.equal(f.executed.length, 1);
  assert.equal(f.executed[0].name, SEND);
  assert.deepEqual(f.executed[0].arguments, { to: 'a@example.com', body: 'stored' });
  assert.equal(f.executed[0].parent, 'tok-1', 'a nested call: the ordinary guards and the group-turn count apply');
  assert.deepEqual(f.executed[0].agent, { session: { id: 'root' } });
  for (const status of ['pending', 'expired', 'consumed', 'cancelled']) {
    f.record.status = status;
    assert.deepEqual(await f.run('run_confirmed', { code: 'K7Q2A' }), { refused: 'not_confirmed', status });
  }
  assert.equal(f.executed.length, 1);
});

test('request_confirmation: a text that did not reach the person gives no code, and its record is cancelled', async () => {
  const f = fixture();
  await f.catalog.preStep();
  f.catalog.observe({ type: 'turn/start', seq: 1 });
  f.catalog.observe(userMessage(wake('m-1')));
  for (const answer of [{ refused: 'credential_in_body' }, { refused: 'not_delivered', reason: 'not_bound', message_id: 'm-out' }, undefined]) {
    f.calls.length = 0;
    f.delivery.answer = answer;
    const out = await f.run('request_confirmation', { name: SEND, arguments: { to: 'a@example.com' } });
    assert.equal(out.refused, 'confirmation_not_delivered');
    assert.equal(out.code, undefined, 'no code the model could pass on');
    assert.ok(!JSON.stringify(out).includes('K7Q2A'), JSON.stringify(out));
    assert.deepEqual(f.calls.at(-1), ['cancel', 'K7Q2A']);
  }
});

test('run_confirmed (step 23.1, 7.8.9 step 5): the read names the open turn; an owner turn running a group\'s record is told which group and who confirmed it', async () => {
  const f = fixture({ openTurn: () => 12 });
  await f.catalog.preStep();
  f.catalog.observe({ type: 'turn/start', seq: 1 });
  f.catalog.observe(userMessage(wake('m-1')));
  Object.assign(f.record, { group: '家庭群', confirmed_by: '妈妈' });
  const ran = await f.run('run_confirmed', { code: 'K7Q2A' });
  assert.deepEqual(f.calls.filter(c => c[0] === 'get'), [['get', 'K7Q2A', { turn: '12' }]]);
  assert.deepEqual(ran, { sent: true });
  // What the model reads of the result: the line first, then the call's own content.
  const shown = f.tools.get('run_confirmed').projectContent({ token: 'tok-1' });
  assert.match(shown[0].text, /confirmed in the family group 「家庭群」 by 妈妈/);
  assert.deepEqual(shown.slice(1), [{ type: 'text', text: 'sent' }]);
  // A record of the turn's own conversation carries no group: nothing is added.
  delete f.record.group; delete f.record.confirmed_by;
  assert.deepEqual(await f.run('run_confirmed', { code: 'K7Q2A' }), { sent: true });
  assert.deepEqual(f.tools.get('run_confirmed').projectContent({ token: 'tok-1' }), [{ type: 'text', text: 'sent' }]);
});

test('run_confirmed (step 23.1): the backend\'s not_this_conversation reaches the model as it is and nothing runs', async () => {
  const f = fixture();
  f.record.status = undefined;
  const tools = new Map(), executed = [];
  registerConfirmTools({ tools: { register: t => tools.set(t.name, t) } }, { ctx: { tools: { execute: async r => { executed.push(r); return {}; } } },
    defineTool: t => t, jsonOutput: {}, HarnessError: Error, catalog: f.catalog, currentTurn: () => 3,
    confirm: { get: async () => ({ refused: 'not_this_conversation', reason: 'a confirmation runs only in the conversation it was asked for' }) } });
  const out = await tools.get('run_confirmed').execute({ code: 'K7Q2A' }, { agent: { session: { id: 'root' } }, token: 't' });
  assert.equal(out.refused, 'not_this_conversation');
  assert.deepEqual(executed, []);
});
