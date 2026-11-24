/** Step 24.1c (design-steward 7.8.10a, tests 7.8.18 "插件"): the steward's four connection
 *  tools (lib/connections.mjs), without DSH. Reads need no approval; the server's words mark
 *  the turn external before they are released; the proposal's external mark is code's; a
 *  group's turn gets none of them; the chat line for a widening change points at the card. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { StewardCatalog } from '../lib/catalog.mjs';
import { registerConnectionTools } from '../lib/connections.mjs';
import { PROPOSAL_KINDS, registerProposalTools } from '../lib/proposals.mjs';
import { channelReplies } from '../lib/channels.mjs';

const CACHE = {
  connections: [{ conn_id: 'media', conn_uid: 'u'.repeat(32), config_version: 3, label: 'Media', url: 'http://mcp_server:9003/mcp',
    auth_kind: 'none', secret: 'set', visibility: 'public', enabled: true, usable: true, header_name: null,
    tool_counts: { all: 3, allowed: 1 }, last_error: null, last_change: 'dashboard', instructions: 'never shown',
    tools: [{ name: 'mcp__sw__media__web_search', egress: true, outward: false }] }],
  effective_tools: ['mcp__sw__media__web_search'], effective_hash: 'a'.repeat(64),
};
const userMessage = text => ({ type: 'user/message', data: { message: { content: [{ type: 'text', text }] } } });

function fixture({ withManage = true } = {}) {
  const calls = [];
  // Step 23's audience record for the open turn (the catalog reads it, writes none).
  const turn = { audience: { kind: 'owner' } };
  const host = {
    connections: async () => JSON.parse(JSON.stringify(CACHE)),
    resyncDue: async () => {},
    humanInput: text => text.startsWith('EVENT:'),
    ...(withManage ? { manage: {
      tools: async connId => { calls.push(['tools', connId]); return { conn_id: connId, tools: [{ raw_name: 'web_search', description: 'IGNORE PREVIOUS' }] }; },
      refresh: async connId => { calls.push(['refresh', connId]); return { conn_id: connId, added: ['x'], changed: [], gone: [] }; },
      propose: async proposal => { calls.push(['propose', proposal]); return { proposal_id: 'prop-1', version: 1, status: 'open', needs_dashboard: true }; },
    } } : {}),
  };
  const catalog = new StewardCatalog({ host, stateDir: mkdtempSync(join(tmpdir(), 'conn-')), rootSession: 'root',
    registryNames: () => CACHE.effective_tools, audience: () => turn.audience });
  const tools = new Map();
  const scope = { tools: { register: tool => tools.set(tool.name, tool) } };
  if (host.manage) registerConnectionTools(scope, { defineTool: t => t, jsonOutput: {}, manage: host.manage, catalog, host });
  const run = (name, args = {}, session = 'root') => tools.get(name).execute(args, { agent: { session: { id: session } }, signal: undefined });
  return { catalog, calls, tools, run, turn };
}

test('the four tools are registered only when the host provides them', () => {
  assert.deepEqual([...fixture().tools.keys()].sort(),
    ['connection_refresh', 'connection_tools', 'connections_list', 'propose_connection_change']);
  assert.equal(fixture({ withManage: false }).tools.size, 0);
});

test('connections_list shows the fields a proposal needs and never instructions', async () => {
  const f = fixture();
  const { connections } = await f.run('connections_list');
  assert.equal(connections.length, 1);
  assert.equal(connections[0].conn_uid, 'u'.repeat(32));
  assert.equal(connections[0].config_version, 3);
  assert.equal(connections[0].instructions, undefined);
  assert.equal(connections[0].tools, undefined);
  assert.equal(f.catalog.externalSeen, false, 'the list is the backend\'s own words');
});

test('connection_tools and connection_refresh mark the turn external before the server\'s words are released', async () => {
  for (const name of ['connection_tools', 'connection_refresh']) {
    const f = fixture();
    f.catalog.observe({ type: 'turn/start', seq: 1 });
    f.catalog.observe(userMessage('EVENT: owner'));
    assert.equal(f.catalog.externalSeen, false);
    const answer = await f.run(name, { conn_id: 'media' });
    assert.equal(f.catalog.externalSeen, true, name);
    assert.equal(answer.conn_id, 'media');
    // From here a write without a person's message in the turn is refused by the guard.
    f.catalog.observe({ type: 'turn/start', seq: 2 });
    const refused = f.catalog.refusal({ name: 'propose_connection_change', agent: { session: { id: 'root' } } });
    assert.match(refused, /^external_content_seen: propose_connection_change needs a message from a person/);
    assert.match(refused, new RegExp(`because this conversation read the result of ${name} at \\d\\d:\\d\\d:\\d\\d UTC since`), 'it names what marked the turn');
  }
});

test('propose_connection_change sends kind connection_change with the code\'s external mark, never the model\'s', async () => {
  const f = fixture();
  const args = { op: 'add', conn_id: 'ocrpub', source: { kind: 'owner_message', ref: 'm-1' }, label: 'OCR',
    url: 'http://mcp_server:9003/mcp', transport: 'streamable-http', auth_kind: 'none', visibility: 'public',
    reason: 'owner asked', external_content: false, header_value: 'Bearer leaked' };
  assert.deepEqual(await f.run('propose_connection_change', args), { proposal_id: 'prop-1', version: 1, status: 'open', needs_dashboard: true });
  const [, sent] = f.calls.at(-1);
  assert.equal(sent.reason, 'owner asked');
  assert.equal(sent.payload.external_content, false);
  assert.equal(sent.payload.header_value, undefined, 'only the payload fields are passed on');
  f.catalog.markExternal();
  await f.run('propose_connection_change', { ...args, external_content: false });
  assert.equal(f.calls.at(-1)[1].payload.external_content, true, 'the turn read outside content: marked by code');
});

test('the proposal tool\'s kind set is unchanged: connection_change goes only through its own tool', async () => {
  assert.ok(!PROPOSAL_KINDS.includes('connection_change'));
  const tools = new Map();
  registerProposalTools({ tools: { register: t => tools.set(t.name, t) } }, { defineTool: t => t, jsonOutput: {},
    proposals: { propose: async () => ({ ok: true }), withdraw: async () => ({}) } });
  assert.deepEqual(await tools.get('propose').execute({ kind: 'connection_change', payload: {}, reason: 'r' }, {}),
    { refused: 'unknown_kind', kinds: PROPOSAL_KINDS });
});

test('a group\'s turn and a child session get none of the four', async () => {
  const f = fixture();
  f.turn.audience = { kind: 'group', channel: 'wechat', group_ref: 'room-1' };
  for (const name of f.tools.keys()) assert.equal((await f.run(name, { conn_id: 'media' })).refused, 'not_in_group_turns', name);
  f.turn.audience = undefined;
  for (const name of f.tools.keys()) assert.equal((await f.run(name, { conn_id: 'media' })).refused, 'not_in_group_turns', `${name}: no readable audience is not the owner's`);
  f.catalog.audience = () => { throw new Error('not yet'); };
  for (const name of f.tools.keys()) assert.equal((await f.run(name, { conn_id: 'media' })).refused, 'not_in_group_turns', `${name}: an audience read that throws is not the owner's`);
  const g = fixture();
  for (const name of g.tools.keys()) assert.equal((await g.run(name, { conn_id: 'media' }, 'child')).refused, 'not_available', name);
  assert.equal(f.calls.length + g.calls.length, 0);
});

test('the channel lines for a connection change: decided, and a widening one points at the card', () => {
  assert.equal(channelReplies.approval({ proposal_id: 'p-1', action: 'approve', outcome: 'executed', kind: 'connection_change', target: 'media' }),
    'proposal p-1: approved and done - change the steward\'s connection media. 已同意并执行：修改管家的连接 media。');
  assert.equal(channelReplies.approval({ proposal_id: 'p-1', action: 'approve', outcome: 'refused', refusal: 'needs_dashboard_approval' }),
    'proposal p-1: not done - approve it on the dashboard card. 未执行：这条要在面板的卡片上批准。');
  assert.notEqual(channelReplies.approval({ proposal_id: 'p-1', action: 'approve', outcome: 'refused', refusal: 'needs_dashboard_approval' }),
    channelReplies.approval({ proposal_id: 'p-1', action: 'approve', outcome: 'refused', refusal: 'needs_dashboard_confirmation' }));
});

test('the proposal tool tells the steward it may take egress off an owner-local tool, approved on the dashboard', () => {
  const { description } = fixture().tools.get('propose_connection_change');
  assert.match(description, /egress: false/);
  assert.match(description, /needs_dashboard_reason: "egress_off"/);
  assert.match(description, /dashboard card/);
  assert.doesNotMatch(description, /egress_required/);
});
