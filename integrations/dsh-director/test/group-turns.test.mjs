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
  answerNow, foldGroupTurns, filterCompactionRequest, withoutGroupTurns, turnSpans, putBack, createGroupSetup } from '../lib/group-turns.mjs';
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

const ev = (type, data = {}) => ({ type, data });
const script = list => list.map((e, seq) => ({ ...e, seq, time: 0 }));

// DSH's inbox as the plugin uses it: `next-turn` holds one queued turn per message.
function fakeInbox(queued = []) {
  const nextTurn = [...queued];
  return { get nextTurn() { return [...nextTurn]; }, list: nextTurn,
    append: (target, m) => { assert.equal(target, 'next-turn'); nextTurn.push(m); },
    prepend: (target, m) => { assert.equal(target, 'next-turn'); nextTurn.unshift(m); },
    splice: (target, at, count, inserted) => { assert.equal(target, 'next-turn'); return nextTurn.splice(at, count, ...inserted); },
    claimTurn: () => nextTurn.shift() };
}
const texts = list => list.map(m => m.content[0].text);

test('put back: owner input a group turn put back opens the very next turn, whatever the family keeps asking (7.7.3 item 3)', () => {
  const g = turns(), inbox = fakeInbox();
  g.split([group('Q1')], 1);
  // Step 2: the owner writes; step 3: a family member follows up; step 4: the owner again.
  for (const [step, input] of [[2, message('OWNER_A')], [3, group('FOLLOW_UP')], [4, message('OWNER_B')]])
    putBack(inbox, g.split([input], step).back, g);
  assert.deepEqual(texts(inbox.list), ['OWNER_A', 'OWNER_B', 'FOLLOW_UP']);
  // The next turn claims the queued head - and a new question that arrived meanwhile: the owner's turn.
  const next = g.split([group('NEW_Q'), inbox.claimTurn()], 1);
  assert.equal(g.audience().kind, 'owner');
  assert.deepEqual(texts(next.keep), ['OWNER_A']);
  putBack(inbox, next.back, g);
  assert.deepEqual(texts(inbox.list), ['OWNER_B', 'FOLLOW_UP', 'NEW_Q']);
  // Kept across a restart (a new object over the same directory), and cleared once served.
  const again = new GroupTurns({ stateDir: dirname(g.waitingFile), limits: GROUP_LIMITS });
  again.split([group('Q9'), inbox.claimTurn()], 1);
  assert.equal(again.audience().kind, 'owner');
  assert.deepEqual([...again.waiting], []);
  // Fresh owner input that meets a group question on a first step still waits one group turn only.
  const fresh = message('OWNER_C');
  putBack(inbox, again.split([group('Q10'), fresh], 1).back, again);
  assert.equal(inbox.list[0], fresh);
});

test('restart: a group question still in the inbox opens a group turn; the owner\'s message is the next turn', () => {
  const stateDir = mkdtempSync(join(tmpdir(), 'group-restart-'));
  const persisted = group('LEFT_IN_INBOX'), owner = message('OWNER_AFTER_RESTART'), inbox = fakeInbox();
  const g = new GroupTurns({ stateDir, limits: GROUP_LIMITS });
  putBack(inbox, g.split([persisted, owner], 1).back, g);
  assert.equal(g.audience().kind, 'group');
  assert.deepEqual(texts(inbox.list), ['OWNER_AFTER_RESTART']);
  g.split([inbox.claimTurn()], 1);
  assert.equal(g.audience().kind, 'owner');
});

test('restart in the middle of a group turn: the guard still reads the group from the session log', async () => {
  const events = script([ev('turn/start'), ev('user/message', group('MID_TURN', 'r1', 'q-mid')),
    ev('assistant/message', { message: { id: 'a', content: [{ type: 'tool-call', id: 't', name: 'portfolio_projects' }] } }),
    ev('tool/result', { message: { id: 't' } })]);
  const g = turns();
  const setup = createGroupSetup({ turns: g, groups: {}, createUserMessage: m => m, fold: () => 0 });
  const payload = { step: 2, turn: 7, agent: { inbox: fakeInbox(), session: { snapshotEvents: () => events } } };
  // The step after the restart claims only an owner message: it goes back, the turn stays the group's.
  const decision = await setup.step(payload, { kind: 'enter', messages: [message('OWNER_MID')] });
  assert.deepEqual(decision.messages, []);
  assert.equal(g.audience().kind, 'group');
  assert.deepEqual(g.current.questions, ['q-mid']);
  assert.equal(g.current.calls, 1);
  assert.equal(g.guard('memory_note'), groupTurnRefusal);
  assert.deepEqual(texts(payload.agent.inbox.list), ['OWNER_MID']);
});

// The first step's setup (7.7.3 item 6): a fake host and agent around createGroupSetup.
function setupWorld({ fold = () => 0, audience = async () => ({ recorded: true }), ownerSpokeSince } = {}) {
  const stateDir = mkdtempSync(join(tmpdir(), 'group-setup-'));
  const g = new GroupTurns({ stateDir, limits: GROUP_LIMITS });
  const calls = { audience: [], steered: [], errors: [], cleared: 0 };
  const host = { audience: async (a, s) => { calls.audience.push(a); return audience(a, s); },
    ...(ownerSpokeSince ? { ownerSpokeSince } : {}) };
  const pendingFile = join(stateDir, 'group-setup-pending.json');
  const setup = createGroupSetup({ turns: g, groups: host, pendingFile, retryMs: 30, fold: (...a) => fold(...a),
    createUserMessage: m => ({ ...m, id: 'redelivered-' + ++ids, role: 'user' }), steer: list => calls.steered.push(...list),
    setHostError: async e => { calls.errors.push(e); }, clearHostError: async () => { calls.cleared += 1; } });
  const payload = (turn, step = 1) => ({ step, turn, agent: { inbox: fakeInbox(), session: { snapshotEvents: () => [] } } });
  return { g, calls, setup, payload, pendingFile };
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

test('setup: a failing fold rejects the step, keeps its input, delivers it again; a second failure is group_turn_setup_failed', async () => {
  let failing = true;
  const w = setupWorld({ fold: () => { if (failing) throw new Error('replace refused'); return 0; } });
  const q = group('KEPT_QUESTION');
  assert.deepEqual(await w.setup.step(w.payload(1), { kind: 'enter', messages: [q] }), { kind: 'reject' });
  assert.deepEqual(JSON.parse(readFileSync(w.pendingFile, 'utf8')).map(m => m.content[0].text), ['KEPT_QUESTION']);
  assert.equal(w.g.current, undefined);
  assert.deepEqual(w.calls.audience, [], 'nothing reached the host before the fold');
  await sleep(80);
  assert.deepEqual(texts(w.calls.steered), ['KEPT_QUESTION']);
  assert.equal(w.calls.steered[0].source.group_ref, 'r1');
  assert.ok(!existsSync(w.pendingFile));
  // The redelivered question fails again: reported, not retried.
  assert.deepEqual(await w.setup.step(w.payload(2), { kind: 'enter', messages: [w.calls.steered[0]] }), { kind: 'reject' });
  assert.match(w.calls.errors.join(), /^group_turn_setup_failed: replace refused/);
  await sleep(80);
  assert.equal(w.calls.steered.length, 1);
  // The next first step that works clears the error and delivers what was kept.
  failing = false;
  const owner = message('OWNER_LATER');
  assert.deepEqual((await w.setup.step(w.payload(3), { kind: 'enter', messages: [owner] })).messages, [owner]);
  assert.equal(w.calls.cleared, 1);
  assert.deepEqual(texts(w.calls.steered), ['KEPT_QUESTION', 'KEPT_QUESTION']);
});

test('setup: a failed audience write is handled like a failed fold; a failed pause check never rejects the owner\'s turn', async () => {
  const w = setupWorld({ audience: async () => { throw new Error('HTTP 503'); } });
  assert.deepEqual(await w.setup.step(w.payload(1), { kind: 'enter', messages: [group('Q')] }), { kind: 'reject' });
  assert.ok(existsSync(w.pendingFile));
  assert.ok(!existsSync(w.g.pauseFile), 'no pause recorded for a turn that did not run');
  const spoke = setupWorld({ ownerSpokeSince: async () => { throw new Error('backend hiccup'); } });
  spoke.g.pause();
  const owner = message('OWNER');
  const decision = await spoke.setup.step(spoke.payload(1), { kind: 'enter', messages: [owner] });
  assert.equal(decision.kind, 'enter');
  assert.deepEqual(decision.messages, [owner]);
  assert.ok(spoke.g.paused(), 'the pause stays in effect');
  assert.deepEqual(spoke.calls.audience.map(a => a.audience), ['owner']);
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
