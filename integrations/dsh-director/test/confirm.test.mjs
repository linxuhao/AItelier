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

function fixture({ stateDir, turn = { kind: 'owner' } } = {}) {
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
    create: async (request) => { calls.push(['create', request]); return { code: 'K7Q2A', text: 'TEXT', reply_to_message_id: request.requesterMessageId, expires_at: 'e' }; },
    deliver: async (message) => { calls.push(['deliver', message]); return { message_id: 'm-out' }; },
    get: async code => { calls.push(['get', code]); return { code, ...record }; },
  };
  const tools = new Map(), executed = [];
  const scope = { tools: { register: tool => tools.set(tool.name, tool) } };
  const ctx = { tools: { execute: async request => { executed.push(request); return { value: { sent: true }, content: [{ type: 'text', text: 'sent' }] }; } } };
  class HarnessError extends Error { constructor(message, code) { super(message); this.code = code; } }
  registerConfirmTools(scope, { ctx, defineTool: t => t, jsonOutput: {}, HarnessError, confirm, catalog });
  const run = (name, args, session = 'root') => tools.get(name).execute(args, { agent: { session: { id: session } }, signal: undefined,
    callId: 'c1', rootCallId: 'r1', token: 'tok-1' });
  return { catalog, calls, record, run, executed, dir };
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
    ['deliver', { code: 'K7Q2A', text: 'TEXT', replyTo: 'm-2' }]]);
  // A new turn woken by something else has none again; the record survives a restart.
  const again = new StewardCatalog({ host: f.catalog.host, stateDir: f.dir, rootSession: 'root', registryNames: () => [] });
  assert.equal(again.requester, 'm-2');
  f.catalog.observe({ type: 'turn/start', seq: 2 });
  assert.equal(f.catalog.requester, undefined);
});

test('request_confirmation: the audience it names is step 23\'s open turn - a group turn, or one it cannot read, is not the owner\'s', async () => {
  const turn = { audience: { kind: 'group', channel: 'wechat', group_ref: 'room1' } };
  const f = fixture({ turn });
  await f.catalog.preStep();
  f.catalog.observe({ type: 'turn/start', seq: 1 });
  f.catalog.observe(userMessage(wake('m-1')));
  await f.run('request_confirmation', { name: SEND, arguments: { to: 'a@example.com' } });
  turn.audience = undefined;
  f.catalog.audience = () => { throw new Error('not yet'); };
  await f.run('request_confirmation', { name: SEND, arguments: { to: 'a@example.com' } });
  assert.deepEqual(f.calls.filter(c => c[0] === 'create').map(c => c[1].audience), ['group', 'group']);
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
