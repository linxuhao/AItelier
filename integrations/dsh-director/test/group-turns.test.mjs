/** Step 23 (design-steward 7.7.3, 7.7.5, 7.7.6, 7.7.6a, 7.7.10, 7.7.11): the steward's family
 *  group turns - audience and put-back, the guard, the pause, no snapshot in a group turn, the
 *  fold, the compaction filter, the grep filter, the plugin's pinned bounds and the channel
 *  service's group path. The real-DSH witness of the same is
 *  integrations/dsh-driver/runtime.group.test.mjs. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { GroupTurns, GROUP_SOURCE, FOLD_SOURCE, GROUP_TOOLS, PAUSED_TOOLS, groupTurnRefusal, familyRefusal, pausedRefusal,
  answerNow, foldGroupTurns, filterCompactionRequest, withoutGroupTurns, turnSpans } from '../lib/group-turns.mjs';
import { GROUP_LIMITS, CHANNEL_LIMITS, createOwnerChannels, channelReplies } from '../lib/channels.mjs';
import { ContextInjection } from '../lib/core.mjs';

const here = dirname(fileURLToPath(import.meta.url));
let ids = 0;
const message = (text, source = { kind: 'dsh-director-runtime-event' }) => ({ id: 'm' + ++ids, role: 'user', content: [{ type: 'text', text }], source });
const group = (text, ref = 'r1', qid = 'q' + ids) => message(text, { kind: GROUP_SOURCE, channel: 'wechat', group_ref: ref, group_label: '家庭群', question_ids: [qid], sender_refs: ['wxid_m'] });
const turns = () => new GroupTurns({ stateDir: mkdtempSync(join(tmpdir(), 'group-turns-')), limits: GROUP_LIMITS });
// The steward's tools in roles.json (a host's own tools are refused the same way: the guard is a
// whitelist; integrations/dsh-driver/steward.groups.test.mjs checks the adapter's by name).
const STEWARD_TOOLS = JSON.parse(readFileSync(join(here, '../roles.json'), 'utf8')).steward.presented;

test('the plugin keeps its group bounds as code constants, none of them a backend or bridge key', () => {
  assert.deepEqual({ ...GROUP_LIMITS }, { mergeMax: 5, turnToolCalls: 6, recentChars: 1500 });
  assert.ok(Object.isFrozen(GROUP_LIMITS));
  const backend = readFileSync(join(here, '../../../core/state_groups.py'), 'utf8');
  const bridge = readFileSync(join(here, '../../wechat-bridge/bridge.mjs'), 'utf8');
  for (const key of Object.keys(GROUP_LIMITS)) {
    assert.ok(!backend.includes(`"${key}"`), `${key} is also a backend limit`);
    assert.ok(!/GROUP_SEND_LIMITS = Object.freeze\(\{[^}]*\b/.exec(bridge)?.[0].includes(key), `${key} is also a bridge limit`);
    assert.ok(!(key in CHANNEL_LIMITS));
  }
});

test('audience: a turn whose first step has a group message is that group\'s; other input goes back, in order', () => {
  const g = turns();
  const owner = message('owner wrote'), letter = message('director letter'), q1 = group('q1'), other = group('x', 'r2');
  const context = message('cwd', { kind: 'runtime-context' });
  const first = g.split([q1, owner, other, context, letter], 1);
  assert.deepEqual(first.keep, [q1, context]);
  assert.deepEqual(first.back, [owner, other, letter]);
  assert.equal(g.audience().kind, 'group');
  // Owner input is waiting: a new question of the same group is not merged any more.
  assert.deepEqual(g.split([group('q2')], 2).keep, []);
  // Mixed input on a first step: the group's turn (the tag decides); the owner's goes back.
  assert.deepEqual(g.split([owner, group('q3')], 1).back, [owner]);
  g.split([owner], 1);
  assert.equal(g.audience().kind, 'owner');
  assert.deepEqual(g.split([group('mid')], 2).back.map(m => m.content[0].text), ['mid']);
});

test('audience: a group turn merges at most five questions of its group', () => {
  const g = turns();
  const qs = Array.from({ length: 7 }, (_, i) => group('q' + i));
  const { keep, back } = g.split(qs, 1);
  assert.equal(keep.length, 5);
  assert.equal(back.length, 2);
  assert.equal(g.current.questions.length, 5);
});

test('guard: in a group turn only the four tools, at most six calls; anything new is refused', () => {
  const g = turns();
  g.split([group('q')], 1);
  for (const name of STEWARD_TOOLS.filter(n => !GROUP_TOOLS.has(n))) assert.equal(g.guard(name), groupTurnRefusal, name);
  assert.equal(g.guard('a_tool_added_next_year'), groupTurnRefusal);
  const g2 = turns();
  g2.split([group('q')], 1);
  for (const name of ['portfolio_projects', 'portfolio_project', 'family_memory_note', 'family_memory_show', 'portfolio_project', 'portfolio_project'])
    assert.equal(g2.guard(name), undefined, name);
  assert.equal(g2.guard('portfolio_project'), answerNow);
});

test('guard: an unknown audience reads as a group; an owner turn never gets the family memory tools', () => {
  const g = turns();
  assert.equal(g.guard('memory_note'), groupTurnRefusal);
  g.split([message('owner')], 1);
  for (const name of STEWARD_TOOLS.filter(n => !n.startsWith('family_'))) assert.equal(g.guard(name), undefined, name);
  assert.equal(g.guard('family_memory_note'), familyRefusal);
  assert.equal(g.guard('family_memory_show'), familyRefusal);
});

test('pause: after a group turn the five writes wait for the owner; a broken pause file is in effect', () => {
  const g = turns();
  g.pause();
  g.split([message('a director letter')], 1);
  for (const name of PAUSED_TOOLS) assert.equal(g.guard(name), pausedRefusal, name);
  for (const name of ['ask_owner', 'portfolio_inbox', 'memory_search']) assert.equal(g.guard(name), undefined, name);
  g.unpause();
  assert.equal(g.guard('memory_note'), undefined);
  writeFileSync(g.pauseFile, '{broken');
  assert.equal(g.guard('propose'), pausedRefusal);
  // A restart (a new object over the same directory) keeps it.
  const again = new GroupTurns({ stateDir: dirname(g.pauseFile), limits: GROUP_LIMITS });
  again.split([message('x')], 1);
  assert.equal(again.guard('propose'), pausedRefusal);
  // A host's own writes join the pause.
  const hosted = new GroupTurns({ stateDir: dirname(g.pauseFile), limits: GROUP_LIMITS, pausedTools: ['host_write'] });
  hosted.split([message('x')], 1);
  assert.equal(hosted.guard('host_write'), pausedRefusal);
});

test('snapshot: no injection in a group turn, and it is still owed to the next owner turn', async () => {
  let audience = 'group';
  const appended = [];
  const session = { snapshotEvents: () => [], append: (...args) => appended.push(args) };
  const injection = new ContextInjection(session, { refresh: async () => {}, render: () => 'PROFILE' },
    input => ({ ...input, id: 'snap' }), { audience: () => audience });
  assert.equal(await injection.prepare(), undefined);
  await injection.beforeRequest();
  assert.equal(appended.length, 0);
  audience = 'owner';
  assert.match(JSON.stringify(await injection.prepare()), /PROFILE/);
});

// A session double with the parts the fold uses: the log, the surface, and append with a
// positional replace that checks what DSH checks (earlier, present, every node cited).
function fakeSession(events) {
  const log = [...events];
  let nodes = log.filter(e => ['user/message', 'assistant/message', 'tool/result', 'system/message'].includes(e.type)).map(e => e.seq);
  return {
    snapshotEvents: () => [...log], get surface() { return { nodes }; }, log,
    append(type, data, { surfaceOp, sourceEventSeqs }) {
      const seq = log.length;
      const start = nodes.indexOf(surfaceOp.startSeq), end = nodes.indexOf(surfaceOp.endSeq);
      assert.ok(start >= 0 && end >= start, 'replace range on the surface');
      assert.deepEqual(sourceEventSeqs, nodes.slice(start, end + 1));
      log.push({ seq, type, data });
      nodes = [...nodes.slice(0, start), seq, ...nodes.slice(end + 1)];
    },
  };
}
const ev = (type, data = {}) => ({ type, data });
const script = list => list.map((e, seq) => ({ ...e, seq, time: 0 }));

test('fold: each finished group turn becomes one line; the system prompt, checkpoints and owner turns stay', () => {
  const session = fakeSession(script([
    ev('system/message', { message: { content: [] } }),
    ev('turn/start', { turn: 1 }), ev('user/message', message('OWNER_ONE')), ev('assistant/message', { message: { id: 'a1', content: [{ type: 'text', text: 'hi' }] } }), ev('turn/end', { turn: 1 }),
    ev('turn/start', { turn: 2 }), ev('user/message', group('GROUP_Q')), ev('assistant/message', { message: { id: 'a2', content: [] } }),
    ev('tool/result', { message: { id: 't2', content: [{ type: 'text', text: 'MARKER' }] } }),
    ev('user/message', message('CHECKPOINT', { kind: 'compaction-checkpoint' })),
    ev('assistant/message', { message: { id: 'a3', content: [{ type: 'text', text: 'GROUP_A' }] } }), ev('turn/end', { turn: 2 }),
    ev('turn/start', { turn: 3 }),
  ]));
  assert.equal(foldGroupTurns(session, input => ({ ...input, id: 'f' + ++ids })), 2);
  const visible = session.surface.nodes.map(seq => JSON.stringify(session.log[seq].data));
  assert.ok(visible.some(t => t.includes('OWNER_ONE')) && visible.some(t => t.includes('CHECKPOINT')));
  assert.ok(!visible.some(t => t.includes('GROUP_Q') || t.includes('MARKER') || t.includes('GROUP_A')));
  assert.equal(visible.filter(t => t.includes(FOLD_SOURCE)).length, 2);
  // Nothing left to fold the second time; a fold line in a later group turn is kept.
  assert.equal(foldGroupTurns(session, input => ({ ...input, id: 'f' + ++ids })), 0);
});

test('compaction filter: a request without group messages passes as is; one with them loses them', () => {
  const events = script([
    ev('turn/start'), ev('user/message', { ...message('OWNER'), id: 'u1' }), ev('turn/end'),
    ev('turn/start'), ev('user/message', { ...group('GROUP_Q'), id: 'u2' }),
    ev('assistant/message', { message: { id: 'a2', role: 'assistant', content: [{ type: 'text', text: 'MARKER' }] } }),
  ]);
  const make = input => ({ ...input, id: 'x' });
  const ownerOnly = { purpose: 'compaction', messages: [{ id: 'sys', role: 'system' }, { id: 'u1', role: 'user' }] };
  assert.equal(filterCompactionRequest(ownerOnly, events, make), undefined);
  const mixed = { purpose: 'compaction', sessionId: 's', messages: [{ id: 'sys', role: 'system' }, { id: 'u1', role: 'user' }, { id: 'u2', role: 'user', content: 'GROUP_Q' },
    { id: 'a2', role: 'assistant', content: 'MARKER' }, { role: 'user', content: 'compaction instruction' }] };
  const replay = filterCompactionRequest(mixed, events, make);
  assert.equal(replay.sessionId, 's');
  assert.ok(!JSON.stringify(replay.messages).includes('MARKER') && !JSON.stringify(replay.messages).includes('GROUP_Q'));
  assert.equal(replay.messages.length, 4);
  assert.equal(replay.messages[2].source.kind, FOLD_SOURCE);
  assert.equal(turnSpans(events)[1].audience.kind, 'group');
});

test('grep_chat_history never finds what a group turn said', () => {
  const events = script([ev('turn/start'), ev('user/message', message('OWNER_WORD')), ev('turn/end'),
    ev('turn/start'), ev('user/message', group('FAMILY_WORD')), ev('tool/call', { arguments: 'FAMILY_WORD' }), ev('turn/end')]);
  const kept = JSON.stringify(withoutGroupTurns(events));
  assert.ok(kept.includes('OWNER_WORD') && !kept.includes('FAMILY_WORD'));
});

// --- the channel service's group path (7.7.11) -------------------------------------------

function groupWorld(answers = {}) {
  const calls = [], sent = [], rooms = [];
  const host = { relay: async m => { calls.push(['owner-relay', m]); return { message_id: 'x' }; }, bind: async m => { calls.push(['owner-bind', m]); return { bound: true }; },
    sourceOf: async () => null, recordDelivery: async () => ({}), list: async () => ({ channels: [] }) };
  const groups = { list: async () => ({ owner_refs: { wechat: 'wxid_owner' }, groups: [{ channel: 'wechat', group_ref: 'r1', mention_prefix: '@管家' }] }),
    relay: async m => { calls.push(['relay', m]); return answers.relay ?? { message_id: 'g1', sender_role: 'member', sender_label: '成员甲' }; },
    bind: async m => { calls.push(['bind', m]); return answers.bind ?? { bound: true, notice: 'NOTICE' }; } };
  const dir = mkdtempSync(join(tmpdir(), 'group-channels-'));
  const channels = createOwnerChannels({ host, groups, owedFile: join(dir, 'owed.json') });
  channels.service.register({ name: 'wechat', send: async (text, o) => { sent.push(['1:1', text, o]); return {}; }, status: () => ({ connected: true }),
    sendGroup: async (room, text) => { sent.push(['group', room, text]); return {}; }, setRooms: async r => { rooms.push(r); } });
  const woken = [];
  channels.setGroupWake(async q => { woken.push(q); });
  channels.ready();
  return { channels, calls, sent, rooms, woken, owed: join(dir, 'owed.json') };
}
const inbound = (w, extra) => w.channels.service.inbound({ channel: 'wechat', senderRef: 'wxid_m', messageRef: 'm' + ++ids, text: '123456', at: new Date().toISOString(), conversationRef: 'r1', ...extra });

test('channels: a group message never writes the owed store, is never a binding attempt and is never answered bindRefused', async () => {
  const w = groupWorld();
  assert.deepEqual(await inbound(w), { accepted: true });
  assert.deepEqual(w.calls.map(c => c[0]), ['relay']);
  assert.ok(!existsSync(w.owed) || !readFileSync(w.owed, 'utf8').includes('123456'));
  assert.ok(!w.sent.some(s => s[1] === channelReplies.bindRefused || s[2] === channelReplies.bindRefused));
  assert.equal(w.woken.length, 1);
  assert.equal(w.woken[0].message_id, 'g1');
  assert.deepEqual(w.rooms.at(-1), { ownerRef: 'wxid_owner', rooms: [{ room_ref: 'r1', prefix: '@管家' }] });
});

test('channels: a refused group message is final, its notice goes to the group, and a bound group code sends the notice', async () => {
  const w = groupWorld({ relay: { refused: 'member_quota', notice: '问得太多了' } });
  assert.deepEqual(await inbound(w, { text: 'hi' }), { accepted: false, reason: 'member_quota' });
  assert.deepEqual(w.sent, [['group', 'r1', '问得太多了']]);
  assert.equal(w.woken.length, 0);
  assert.deepEqual(await inbound(w, { text: 'G-7K2M-Q9XD', conversationRef: 'r9', groupBind: { prefix: '@管家', code: 'G-7K2M-Q9XD' } }), { accepted: true });
  assert.deepEqual(w.sent.at(-1), ['group', 'r9', 'NOTICE']);
  const failing = createOwnerChannels({ host: { relay() {}, bind() {}, sourceOf() {}, recordDelivery() {}, list: async () => ({}) },
    groups: { relay: async () => { throw new Error('ECONNREFUSED'); } }, owedFile: undefined });
  failing.ready();
  assert.deepEqual(await failing.service.inbound({ channel: 'wechat', senderRef: 's', messageRef: 'r', text: 't', conversationRef: 'r1' }),
    { accepted: false, reason: 'unavailable' });
});
