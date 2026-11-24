/** Step 23 (design-steward 7.7.3, 7.7.5, 7.7.6, 7.7.6a, 7.7.10, 7.7.11): the steward's family
 *  group turns - audience and put-back, the guard, the pause, no snapshot in a group turn, the
 *  fold, the compaction filter, the grep filter, the plugin's pinned bounds and the channel
 *  service's group path. The real-DSH witness of the same is
 *  integrations/dsh-driver/runtime.group.test.mjs. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, existsSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { GroupTurns, GROUP_SOURCE, FOLD_SOURCE, FOLD_LINE, GROUP_TOOLS, FAMILY_TOOLS, PAUSED_TOOLS, groupTurnRefusal, familyRefusal,
  groupReminderRefusal, pausedRefusal, answerNow, foldGroupTurns, filterCompactionRequest, withoutGroupTurns, turnSpans, putBack, createGroupSetup, groupReplies, groupWakeName } from '../lib/group-turns.mjs';
import { GROUP_LIMITS, CHANNEL_LIMITS, createOwnerChannels, channelReplies } from '../lib/channels.mjs';
import { ContextInjection, clockLine } from '../lib/core.mjs';
import { boot } from '../index.mjs';

const here = dirname(fileURLToPath(import.meta.url));
let ids = 0;
const message = (text, source = { kind: 'dsh-director-runtime-event' }) => ({ id: 'm' + ++ids, role: 'user', content: [{ type: 'text', text }], source });
const group = (text, ref = 'r1', qid = 'q' + ids) => message(text, { kind: GROUP_SOURCE, channel: 'wechat', group_ref: ref, group_label: '家庭群', question_ids: [qid], sender_refs: ['wxid_m'] });
const turns = () => new GroupTurns({ stateDir: mkdtempSync(join(tmpdir(), 'group-turns-')), limits: GROUP_LIMITS });
// The steward's tools in roles.json (a host's own tools are refused the same way: the guard is a
// whitelist; integrations/dsh-driver/steward.groups.test.mjs checks the adapter's by name).
const STEWARD_TOOLS = JSON.parse(readFileSync(join(here, '../roles.json'), 'utf8')).steward.presented;

test('the plugin keeps its group bounds as code constants, none of them a backend or bridge key', () => {
  // keptChars, keptNameChars (design/steward-group-media.md section 7): what the kept media entries
  // use before the answered pairs give way, and how short a long sender name is cut past both.
  assert.deepEqual({ ...GROUP_LIMITS }, { mergeMax: 5, turnToolCalls: 6, recentChars: 1500, keptChars: 4000, keptNameChars: 8 });
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

test('audience: the host\'s system clock line is nobody\'s input - a group turn keeps it, puts nothing back, still merges', () => {
  const g = new GroupTurns({ stateDir: mkdtempSync(join(tmpdir(), 'group-turns-')), limits: GROUP_LIMITS, neutralKinds: ['host-clock'] });
  const clock = message('[system clock] now', { kind: 'host-clock' }), q1 = group('q1');
  const first = g.split([clock, q1], 1);
  assert.deepEqual(first, { keep: [clock, q1], back: [] });
  assert.equal(g.audience().kind, 'group');
  assert.equal(g.waiting.size, 0, 'the clock line waits on no turn');
  // No owner input is waiting, so the next question of the same group still merges.
  const q2 = group('q2');
  assert.deepEqual(g.split([message('[system clock] later', { kind: 'host-clock' }), q2], 2).keep.at(-1), q2);
});

test('audience: a group turn merges at most five questions of its group', () => {
  const g = turns();
  const qs = Array.from({ length: 7 }, (_, i) => group('q' + i));
  const { keep, back } = g.split(qs, 1);
  assert.equal(keep.length, 5);
  assert.equal(back.length, 2);
  assert.equal(g.current.questions.length, 5);
});

test('guard: in a group turn only the group tools, at most six calls; anything new is refused', () => {
  // Each name as a turn's first call (every call counts, so a 7th refused call would read answerNow).
  // Step 24 (iss-7e0949291b584b42, 7.8.8): search_tools and call_tool are group tools now - what
  // they reach is the turn's catalog, public connections only (the catalog and the gateway's
  // audience check); the connection-configuration tools stay refused, and so does anything new.
  // Step 24.2 (7.8.8): media_call and send_media are group tools now, so the derived default-deny
  // list no longer holds them - and nothing else left it (the exact set below is the other side).
  const denied = STEWARD_TOOLS.filter(n => !GROUP_TOOLS.has(n));
  assert.ok(!denied.includes('media_call') && !denied.includes('send_media'));
  assert.ok(STEWARD_TOOLS.includes('media_call') && STEWARD_TOOLS.includes('send_media'), 'roles.json presents both to the steward');
  for (const name of [...denied, 'connections_list', 'connection_tools',
    'connection_refresh', 'propose_connection_change', 'a_tool_added_next_year']) {
    const g = turns();
    g.split([group('q')], 1);
    assert.equal(g.guard(name), groupTurnRefusal, name);
  }
  const g2 = turns();
  g2.split([group('q')], 1);
  for (const name of ['portfolio_projects', 'family_memory_note', 'call_tool', 'request_confirmation', 'run_confirmed', 'media_call'])
    assert.equal(g2.guard(name), undefined, name);
  // Step 24.2c (7.8.12b "五"): read_document is a group tool, presented to the steward, and the
  // refusal sentence names it.
  assert.ok(GROUP_TOOLS.has('read_document') && STEWARD_TOOLS.includes('read_document'));
  assert.match(groupTurnRefusal, /read_document/);
  const g3 = turns();
  g3.split([group('q')], 1);
  assert.equal(g3.guard('read_document'), undefined);
  assert.equal(g2.guard('search_tools'), answerNow, 'the discovery tools count toward the six like any call');
});

test('guard (step 24, 7.8.8): a group turn calls its PUBLIC connection tools - direct or nested - within the six top-level calls', () => {
  const PUBLIC = 'mcp__sw__calfam__create_event', PRIVATE = 'mcp__sw__calme__create_event';
  let catalog = new Set([PUBLIC, 'mcp__sw__calfam__list_events']);
  const g = new GroupTurns({ stateDir: mkdtempSync(join(tmpdir(), 'group-turns-')), limits: GROUP_LIMITS, connectionTools: () => catalog });
  g.split([group('q')], 1);
  // The private connection's name is not in a group turn's catalog: refused by name, counted.
  assert.equal(g.guard(PRIVATE), groupTurnRefusal);
  assert.equal(g.guard(PRIVATE, { nested: true }), groupTurnRefusal, 'nor through call_tool');
  assert.equal(g.guard('handoff', { nested: true }), groupTurnRefusal, 'call_tool reaches no tool the group may not call');
  assert.equal(g.guard('mcp__sw__calfam__list_events'), undefined, 'a direct call of a public one');
  assert.equal(g.guard('call_tool'), undefined);
  assert.equal(g.guard(PUBLIC, { nested: true }), undefined, 'the call_tool target itself');
  assert.equal(g.current.calls, 3, 'the nested calls are not counted again');
  for (let n = 0; n < 3; n += 1) assert.equal(g.guard('run_confirmed'), undefined);
  assert.equal(g.guard(PUBLIC, { nested: true }), undefined, 'the sixth call\'s own nested call still runs');
  assert.equal(g.guard('call_tool'), answerNow, 'the seventh top-level call');
  assert.equal(g.current.calls, 7);
  // A catalog that cannot be read gives nothing beyond the group tools.
  catalog = undefined;
  const h = new GroupTurns({ stateDir: mkdtempSync(join(tmpdir(), 'group-turns-')), limits: GROUP_LIMITS, connectionTools: () => { throw new Error('x'); } });
  h.split([group('q')], 1);
  assert.equal(h.guard(PUBLIC, { nested: true }), groupTurnRefusal);
  // An owner turn: the group whitelist does not apply (the catalog's own guard does), nothing counted.
  h.split([message('owner')], 1);
  assert.equal(h.guard(PRIVATE), undefined);
  assert.equal(h.current.calls, 0);
});

test('guard (step 25 C4, 7.7.16 table + 7.9.5): list_show, list_add, list_check in a group turn; list_remove only in an owner turn, and not paused', () => {
  assert.deepEqual([...GROUP_TOOLS].sort(), ['call_tool', 'family_memory_note', 'family_memory_show', 'group_reminder_set', 'list_add',
    'list_check', 'list_show', 'make_document', 'media_call', 'portfolio_project', 'portfolio_projects', 'read_document', 'request_confirmation', 'run_confirmed',
    'search_tools', 'send_media']);
  for (const name of ['list_show', 'list_add', 'list_check']) {
    const g = turns();
    g.split([group('q')], 1);
    assert.equal(g.guard(name), undefined, name);
  }
  const g = turns();
  g.split([group('q')], 1);
  assert.equal(g.guard('list_remove'), groupTurnRefusal);
  assert.match(groupTurnRefusal, /list_show, list_add, list_check/);
  // After a group turn, before the owner writes: the owner's turn still changes the lists.
  const o = turns();
  o.pause();
  o.split([message('a director letter')], 1);
  for (const name of ['list_show', 'list_add', 'list_check', 'list_remove']) assert.equal(o.guard(name), undefined, name);
  assert.equal(o.guard('propose'), pausedRefusal);
});

test('director 505: list_show states its bound on checked items where the model reads it, and how the rest is counted', () => {
  const source = readFileSync(new URL('../lib/group-tools.mjs', import.meta.url), 'utf8');
  const description = source.match(/name: 'list_show',\s*description: '((?:[^'\\]|\\.)*)'/)?.[1] ?? '';
  for (const bound of ['100 checked items', 'newest first', 'done_total', 'done_more', 'done_not_shown']) assert.ok(description.includes(bound), bound);
});

test('guard: an unknown audience reads as a group; an owner turn never gets the family memory tools', () => {
  const g = turns();
  assert.equal(g.guard('memory_note'), groupTurnRefusal);
  g.split([message('owner')], 1);
  for (const name of STEWARD_TOOLS.filter(n => !FAMILY_TOOLS.has(n))) assert.equal(g.guard(name), undefined, name);
  assert.equal(g.guard('family_memory_note'), familyRefusal);
  assert.equal(g.guard('family_memory_show'), familyRefusal);
  assert.equal(g.guard('group_reminder_set'), groupReminderRefusal);
});

test('pause: after a group turn the paused writes wait for the owner; a broken pause file is in effect', () => {
  const g = turns();
  g.pause();
  g.split([message('a director letter')], 1);
  for (const name of PAUSED_TOOLS) assert.equal(g.guard(name), pausedRefusal, name);
  // 7.8.8: a turn nobody's message opened (a letter, a reminder) proposes no connection change.
  assert.equal(g.guard('propose_connection_change'), pausedRefusal);
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

test('snapshot: a new turn reads no audience until its first step is set up - never the previous owner turn\'s (7.7.3 hook order)', async () => {
  const g = turns();
  const injection = new ContextInjection({ snapshotEvents: () => [], append: () => {} }, { refresh: async () => {}, render: () => 'PROFILE' },
    input => ({ ...input, id: 'snap' }), { audience: () => g.audience().kind });
  g.turnOpened(1);
  g.split([message('OWNER')], 1, 1);
  injection.epoch = 1; // a compaction after the owner turn: the snapshot is owed again
  // Turn 2 opens (DSH's turn/start); its first step is a group question. Were the snapshot hook
  // to run before the audience is fixed, it would still read nothing it may inject into:
  g.turnOpened(2);
  assert.equal(await injection.prepare(), undefined, 'no snapshot before the audience is fixed');
  g.split([group('Q')], 1, 2);
  assert.equal(await injection.prepare(), undefined, 'none in the group turn');
  g.turnOpened(3);
  g.split([message('OWNER_AGAIN')], 1, 3);
  assert.match(JSON.stringify(await injection.prepare()), /PROFILE/, 'still owed to the next owner turn');
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
  const events = script([ev('turn/start', { turn: 7 }), ev('user/message', group('MID_TURN', 'r1', 'q-mid')),
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

test('audience: a later step whose open turn is another turn reads as an unknown group, never the owner\'s', async () => {
  // Guards a future DSH event-order change, not a defect reachable today: createGroupSetup runs
  // on every turn's first step, so a step-2 payload finds this object's bookkeeping for its turn,
  // or reads it back from the session log. Were DSH to claim a later step of a turn whose first
  // step this object never saw, or the log's open turn to be a later one, the audience must fail
  // closed - {kind: 'group', unknown: true}, the narrower rules (7.7.6a two) - never the
  // owner's; and a reader that does not recognise `unknown` must leave it where it is.
  const events = script([ev('turn/start', { turn: 7 }), ev('user/message', group('Q7')), ev('turn/end', { turn: 7 }),
    ev('turn/start', { turn: 8 })]);
  const g = turns();
  g.turnOpened(8);
  const setup = createGroupSetup({ turns: g, groups: {}, createUserMessage: m => m, fold: () => 0 });
  const payload = { step: 2, turn: 7, agent: { inbox: fakeInbox(), session: { snapshotEvents: () => events } } };
  const decision = await setup.step(payload, { kind: 'enter', messages: [message('OWNER_ON_7')] });
  // (a) The audience of the turn, from split(): the log's open turn 8 is no resume of turn 7, so
  // what this turn is nobody's this object can name - the owner's input is not this turn's.
  assert.deepEqual(g.current.audience, { kind: 'group', unknown: true });
  assert.deepEqual(decision.messages, []);
  assert.deepEqual(texts(payload.agent.inbox.list), ['OWNER_ON_7'], 'it goes back for the next turn');
  assert.equal(g.current.questions.length, 0);
  // (b) A reader that does not recognise `unknown` - one written against the two kinds alone, as
  // the steward's own audience readers are (they ask only whether `kind` is 'owner': lib/catalog.mjs,
  // lib/connections.mjs) - leaves the audience in place instead of replacing it with the owner's.
  const reader = audience => audience.kind === 'group' ? audience : { kind: 'owner' };
  assert.deepEqual(reader(g.current.audience), { kind: 'group', unknown: true });
  // The condition itself: a fresh object reads no resume out of a log whose open turn is 8.
  assert.equal(turns().resume(events, 7), false);
});

// The first step's setup (7.7.3 item 6): a fake host and agent around createGroupSetup.
function setupWorld({ fold = () => 0, audience = async () => ({ recorded: true }), ownerSpokeSince } = {}) {
  const stateDir = mkdtempSync(join(tmpdir(), 'group-setup-'));
  const g = new GroupTurns({ stateDir, limits: GROUP_LIMITS });
  const calls = { audience: [], steered: [], errors: [], cleared: 0 };
  const host = { audience: async (a, s) => { calls.audience.push(a); return audience(a, s); },
    ...(ownerSpokeSince ? { ownerSpokeSince } : {}) };
  const pendingFile = join(stateDir, 'group-setup-pending.json');
  const setup = createGroupSetup({ turns: g, groups: host, pendingFile, retryMs: 30, recordRetryMs: 1, fold: (...a) => fold(...a),
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

test('setup (director 602 defect 1): a failed audience record is tried again at once - a lost request does not cost the turn', async () => {
  let failures = 2;
  const w = setupWorld({ audience: async () => { if (failures-- > 0) throw new Error('HTTP 503'); return { recorded: true }; } });
  const q = group('Q', 'r1', 'gq1');
  const decision = await w.setup.step(w.payload(1), { kind: 'enter', messages: [q] });
  assert.equal(decision.kind, 'enter');
  assert.deepEqual(decision.messages, [q]);
  assert.equal(w.calls.audience.length, 3, 'two failures, then recorded');
  assert.deepEqual(w.calls.audience.map(a => a.questionIds), [['gq1'], ['gq1'], ['gq1']], 'the same record each time');
  assert.ok(!existsSync(w.pendingFile) && w.calls.errors.length === 0);
  // Three in a row: the step is rejected as before (kept, delivered again later).
  const down = setupWorld({ audience: async () => { throw new Error('HTTP 503'); } });
  assert.deepEqual(await down.setup.step(down.payload(1), { kind: 'enter', messages: [group('Q')] }), { kind: 'reject' });
  assert.equal(down.calls.audience.length, 3);
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

// --- director 477 S1: a group turn is bounded at six calls, refused calls included ---

test('guard: every call of a group turn counts - thirty refused calls, then a whitelisted one is answerNow', () => {
  const g = turns();
  g.turnOpened(7);
  g.split([group('Q')], 1, 7);
  const verdicts = Array.from({ length: 30 }, () => g.guard('bash'));
  assert.deepEqual(verdicts.slice(0, 6), Array(6).fill(groupTurnRefusal), 'the first six: the whitelist refusal');
  assert.deepEqual(verdicts.slice(6), Array(24).fill(answerNow), 'from the 7th on: answer now, whatever the name');
  assert.equal(g.guard('portfolio_project'), answerNow);
  assert.equal(g.current.calls, 31);
});

test('guard: a refused call in the middle of six permitted ones - the 7th call overall is answerNow', () => {
  const g = turns();
  g.turnOpened(8);
  g.split([group('Q')], 1, 8);
  const order = ['portfolio_projects', 'portfolio_project', 'portfolio_project', 'memory_note', 'family_memory_show', 'portfolio_project', 'portfolio_project'];
  assert.deepEqual(order.map(name => g.guard(name)), [undefined, undefined, undefined, groupTurnRefusal, undefined, undefined, answerNow]);
  assert.equal(g.current.calls, 7);
  // An owner turn is unaffected: refused and permitted calls alike, nothing counted, never answerNow.
  g.turnOpened(9);
  g.split([message('OWNER')], 1, 9);
  const owner = Array.from({ length: 10 }, (_, i) => g.guard(i % 2 ? 'family_memory_note' : 'portfolio_project'));
  assert.deepEqual(owner, Array.from({ length: 10 }, (_, i) => i % 2 ? familyRefusal : undefined));
  assert.equal(g.current.calls, 0);
});

// --- r44n 7.7.3 item 3, 7.7.5, 7.7.16 (platform director 466 A): the counts are this turn's ---

test('guard: the call count is this group turn\'s - a second group turn\'s first call works, an owner turn\'s seventh too', () => {
  const g = turns();
  // DSH opens turn 1 (turn/start), its first step is a group question; seven calls, the 7th refused.
  g.turnOpened(1);
  g.split([group('Q1')], 1, 1);
  for (let call = 1; call <= 6; call += 1) assert.equal(g.guard('portfolio_project'), undefined, 'call ' + call);
  assert.equal(g.guard('portfolio_project'), answerNow);
  assert.equal(g.current.turn, '1');
  // Turn 2, another group turn: its count starts at 0.
  g.turnOpened(2);
  g.split([group('Q2')], 1, 2);
  assert.equal(g.guard('portfolio_projects'), undefined, 'the second group turn\'s first call');
  assert.equal(g.current.calls, 1);
  // Turn 3, an owner turn: no count at all - its 7th (and 8th) call goes through.
  g.turnOpened(3);
  g.split([message('OWNER')], 1, 3);
  for (let call = 1; call <= 8; call += 1) assert.equal(g.guard('portfolio_project'), undefined, 'owner call ' + call);
  assert.equal(g.current.calls, 0, 'an owner turn counts nothing');
  // The audience before the count: an owner turn's refusal is the family one, never "answer now".
  assert.equal(g.guard('family_memory_note'), familyRefusal);
  // Turn 4 opened but its first step not set up yet (or rejected): another turn's bookkeeping is
  // never borrowed - fail closed as a group of unknown audience, with no count to spend.
  g.turnOpened(4);
  assert.equal(g.audience().kind, 'group');
  assert.equal(g.audience().unknown, true);
  assert.equal(g.guard('memory_note'), groupTurnRefusal);
  assert.equal(g.guard('portfolio_project'), answerNow);
});

test('audience: the merge cap is this group turn\'s - a second group turn merges five again', () => {
  const g = turns();
  const batch = tag => Array.from({ length: 7 }, (_, i) => group(tag + i));
  g.turnOpened(10);
  assert.equal(g.split(batch('A'), 1, 10).keep.length, 5);
  assert.equal(g.split([group('A-later')], 2, 10).keep.length, 0, 'the cap holds within the turn');
  g.turnOpened(11);
  const second = g.split(batch('B'), 1, 11);
  assert.equal(second.keep.length, 5, 'the second group turn starts from 0');
  assert.equal(g.current.merged, 5);
  // A later step of a turn whose first step this object never saw (a restart) reads its count
  // from that turn's log, not from the previous turn's bookkeeping.
  const events = script([ev('turn/start', { turn: 12 }), ev('user/message', group('LOGGED', 'r1', 'q-logged'))]);
  const setup = createGroupSetup({ turns: g, groups: {}, createUserMessage: m => m, fold: () => 0 });
  g.turnOpened(12);
  return setup.step({ step: 2, turn: 12, agent: { inbox: fakeInbox(), session: { snapshotEvents: () => events } } },
    { kind: 'enter', messages: [group('C1'), group('C2')] }).then(decision => {
    assert.equal(decision.messages.length, 2);
    assert.equal(g.current.turn, '12');
    assert.equal(g.current.merged, 3);
  });
});

test('setup: writing group-pause.json failing rejects the step like a failed fold - kept, delivered again in 30 s, then group_turn_setup_failed', async () => {
  const w = setupWorld();
  // A directory where the pause file's temporary goes: the write throws (EISDIR).
  mkdirSync(w.g.pauseFile + '.tmp');
  const q = group('PAUSE_WRITE_FAILS');
  assert.deepEqual(await w.setup.step(w.payload(1), { kind: 'enter', messages: [q] }), { kind: 'reject' });
  assert.deepEqual(JSON.parse(readFileSync(w.pendingFile, 'utf8')).map(m => m.content[0].text), ['PAUSE_WRITE_FAILS'],
    'recorded before it is delivered again');
  assert.equal(w.g.current, undefined);
  await sleep(80);
  assert.deepEqual(texts(w.calls.steered), ['PAUSE_WRITE_FAILS']);
  assert.deepEqual(await w.setup.step(w.payload(2), { kind: 'enter', messages: [w.calls.steered[0]] }), { kind: 'reject' });
  assert.match(w.calls.errors.join(), /^group_turn_setup_failed: .*EISDIR/);
  await sleep(80);
  assert.equal(w.calls.steered.length, 1, 'no third try');
  // Once the pause can be written, the next group turn runs and the pause is in effect.
  rmSync(w.g.pauseFile + '.tmp', { recursive: true });
  assert.equal((await w.setup.step(w.payload(3), { kind: 'enter', messages: [group('NEXT')] })).kind, 'enter');
  assert.ok(w.g.paused());
  assert.equal(w.calls.cleared, 1);
});

// Every tool call in a request has its result and every result its call.
function wellFormed(messages) {
  const calls = messages.filter(m => m.role === 'assistant').flatMap(m => (m.content ?? []).filter(b => b.type === 'tool-call').map(b => b.id));
  const results = messages.filter(m => m.role === 'tool').map(m => m.toolCallId);
  return JSON.stringify([...calls].sort()) === JSON.stringify([...results].sort());
}

test('compaction filter: turns first, then audience - one readable and one unreadable message in a turn removes the whole turn, the request stays well formed', () => {
  const make = input => ({ ...input, id: 'fold-' + ++ids, role: 'user' });
  const owner = id => ({ ...message('OWNER_' + id), id });
  const call = (id, callId) => ({ id, role: 'assistant', content: [{ type: 'tool-call', id: callId, name: 'portfolio_project' }] });
  const result = (id, callId, text) => ({ id, role: 'tool', toolCallId: callId, content: [{ type: 'text', text }] });
  const logged = list => list.map(m => m.role === 'user' ? ev('user/message', m) : ev(m.role === 'tool' ? 'tool/result' : 'assistant/message', { message: m }));
  // (1) The log: turn 2 has an owner message that reads and one whose source does not
  // (a group tag without its group_ref): the whole turn is a group's of unknown group.
  const t1 = [owner('u1'), call('a1', 'c1'), result('r1', 'c1', 'OWNER_RESULT')];
  const unreadable = { id: 'u2b', role: 'user', content: [{ type: 'text', text: 'HALF_TURN_TEXT' }], source: { kind: GROUP_SOURCE, channel: 'wechat' } };
  const t2 = [owner('u2'), call('a2', 'c2'), result('r2', 'c2', 'TURN_TWO_RESULT'), unreadable, call('a3', 'c3'), result('r3', 'c3', 'TURN_TWO_LATER')];
  const events = script([ev('turn/start', { turn: 1 }), ...logged(t1), ev('turn/end', { turn: 1 }),
    ev('turn/start', { turn: 2 }), ...logged(t2), ev('turn/end', { turn: 2 })]);
  assert.deepEqual(turnSpans(events).map(s => s.audience), [{ kind: 'owner' }, { kind: 'group', unknown: true }]);
  const system = { id: 'sys', role: 'system', content: [] }, instruction = { role: 'user', content: [{ type: 'text', text: 'compaction instruction' }] };
  const request = { purpose: 'compaction', sessionId: 's', messages: [system, ...t1, ...t2, instruction] };
  const replay = filterCompactionRequest(request, events, make);
  const body = JSON.stringify(replay.messages);
  for (const gone of ['OWNER_u2', 'TURN_TWO_RESULT', 'HALF_TURN_TEXT', 'TURN_TWO_LATER', '"c2"', '"c3"']) assert.ok(!body.includes(gone), gone);
  assert.deepEqual(replay.messages.map(m => m.id ?? m.content[0].text), ['sys', 'u1', 'a1', 'r1', replay.messages[4].id, 'compaction instruction']);
  assert.equal(replay.messages[4].source.kind, FOLD_SOURCE);
  assert.ok(wellFormed(replay.messages));
  // (2) The request: a message that cannot be matched to the log (its id is not there) follows
  // two of an owner turn's - it is that turn's, so the whole turn goes, not just the matched half.
  const logOnly = script([ev('turn/start', { turn: 1 }), ...logged(t1), ev('turn/end', { turn: 1 }),
    ev('turn/start', { turn: 2 }), ...logged([owner('u2'), call('a2', 'c2')]), ev('turn/end', { turn: 2 })]);
  const stray = result('not-in-log', 'c2', 'UNMATCHED_RESULT');
  const half = filterCompactionRequest({ purpose: 'compaction', messages: [system, ...t1, owner('u2'), call('a2', 'c2'), stray, instruction] }, logOnly, make);
  const halfBody = JSON.stringify(half.messages);
  assert.ok(!halfBody.includes('UNMATCHED_RESULT') && !halfBody.includes('"c2"') && !halfBody.includes('OWNER_u2'));
  assert.ok(wellFormed(half.messages));
  // Before this rule the matched half would go and the stray result stay: a result without its call.
  assert.ok(!wellFormed([system, ...t1, stray, instruction]));
  // The compaction instruction (last, no id) is no turn's and stays; readable owner turns pass as is.
  // An unmatched message with no turn before it is the next turn's: here the owner turn after it goes too.
  const leading = filterCompactionRequest({ purpose: 'compaction', messages: [system, stray, ...t1, instruction] }, logOnly, make);
  assert.deepEqual(leading.messages.map(m => m.id ?? m.content[0].text), ['sys', leading.messages[1].id, 'compaction instruction']);
  assert.ok(wellFormed(leading.messages));
  assert.equal(filterCompactionRequest({ purpose: 'compaction', messages: [system, ...t1, instruction] }, logOnly, make), undefined);
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

test('channels (step 24, 7.8.9): a reply that decided a confirmation sends the backend\'s one line into the group - accepted, or a cancel over quota', async () => {
  const line = '已确认 K7Q2A，正在执行。';
  const w = groupWorld({ relay: { message_id: 'g2', sender_role: 'member', sender_label: '成员甲', duplicate: false,
    confirmation: { code: 'K7Q2A', action: 'confirm', outcome: 'confirmed' }, confirmation_line: line, confirmation_line_message_id: 'gout' } });
  assert.deepEqual(await inbound(w, { text: '确认 K7Q2A' }), { accepted: true });
  assert.deepEqual(w.sent, [['group', 'r1', line]]);
  assert.equal(w.woken.length, 1, 'the reply is still a question: it wakes the steward, which runs it');
  // Over quota the backend refuses only a reply that did not confirm (a confirming one is a
  // question past the quotas): its line is sent and nothing wakes.
  const cancelled = '已取消 K7Q2A，不会执行。';
  const over = groupWorld({ relay: { refused: 'member_quota', confirmation: { code: 'K7Q2A', outcome: 'cancelled' }, confirmation_line: cancelled } });
  assert.deepEqual(await inbound(over, { text: '取消 K7Q2A' }), { accepted: false, reason: 'member_quota' });
  assert.deepEqual(over.sent, [['group', 'r1', cancelled]]);
  assert.equal(over.woken.length, 0);
  // No line from the backend (the group's send budget is spent): nothing extra is sent.
  const spent = groupWorld({ relay: { message_id: 'g3', confirmation: { code: 'K7Q2A', outcome: 'confirmed' } } });
  await inbound(spent, { text: '确认 K7Q2A' });
  assert.deepEqual(spent.sent, []);
});

// The group reply path strips the system clock lines the steward copied to the start of its
// last words, with the owner's automatic reply's rule (lib/core.mjs withoutLeadingClockLines).
function groupReply(lastWords) {
  const g = turns(), replied = [], sent = [], warned = [];
  g.split([group('Q1', 'r1', 'gq1'), group('Q2', 'r1', 'gq2')], 1, 7);
  const watch = groupReplies({ turns: g, warn: line => warned.push(line),
    reply: async args => { replied.push(args); return { channel: 'wechat', group_ref: 'r1', text: '「Q2」\n' + args.body }; },
    sendGroup: async (channel, groupRef, text) => { sent.push([channel, groupRef, text]); } });
  watch({ type: 'turn/start', data: { turn: 7 } });
  watch({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text: lastWords }] } } });
  return { done: watch({ type: 'turn/end', data: { turn: 7, reason: { kind: 'completed' } } }), replied, sent, warned };
}

test('group reply: a system clock line the steward copied to the start of its words never reaches the group', async () => {
  const clock = clockLine({ now: Date.parse('2026-11-21T10:14:00Z'), tz: 'Europe/Paris' });
  const r = groupReply(`${clock}\n${clock} (after tool)\n\n周六去公园。`);
  await r.done;
  assert.deepEqual(r.replied, [{ questionId: 'gq2', alsoAnswers: ['gq1'], body: '周六去公园。', turn: '7' }]);
  assert.deepEqual(r.sent, [['wechat', 'r1', '「Q2」\n周六去公园。']]);
  assert.deepEqual(r.warned, []);
});

test('group reply: words that are only a system clock line send nothing, recorded as only_clock_text', async () => {
  const r = groupReply(clockLine({ now: Date.parse('2026-11-21T10:14:00Z') }));
  await r.done;
  assert.deepEqual(r.replied, []);
  assert.deepEqual(r.sent, []);
  assert.deepEqual(r.warned, ['group reply not sent {"reason":"only_clock_text","question_id":"gq2"}']);
});

test('group reply (r44w, 7.7.7): a withheld reply\'s owner line goes to the owner\'s mirror, the group gets only the fixed line', async () => {
  const g = turns(), sent = [], mirrored = [];
  g.split([group('Q1', 'r1', 'gq1')], 1, 7);
  const line = '家庭群里有一条回答没发出去：涉及私有项目。要问可以私聊我。';
  const answers = [{ channel: 'wechat', group_ref: 'r1', text: '「Q1」\n这个我在群里答不了。', withheld: true,
    owner_notice: { message_id: 'm-owner-1', body: line } },
  { channel: 'wechat', group_ref: 'r1', text: '「Q1」\n这个我在群里答不了。', withheld: true }];
  const watch = groupReplies({ turns: g, warn: () => {}, reply: async () => answers.shift(),
    sendGroup: async (channel, groupRef, text) => { sent.push([channel, groupRef, text]); },
    mirror: async (id, body) => { mirrored.push([id, body]); } });
  const end = () => {
    watch({ type: 'turn/start', data: { turn: 7 } });
    watch({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'secret-plan 在 /home/x' }] } } });
    return watch({ type: 'turn/end', data: { turn: 7, reason: { kind: 'completed' } } });
  };
  await end();
  assert.deepEqual(sent, [['wechat', 'r1', '「Q1」\n这个我在群里答不了。']]);
  assert.deepEqual(mirrored, [['m-owner-1', line]]);
  // No owner line in the answer (the group's hourly lines are spent): nothing is mirrored.
  g.split([group('Q1', 'r1', 'gq1')], 1, 7);
  await end();
  assert.equal(sent.length, 2);
  assert.deepEqual(mirrored, [['m-owner-1', line]]);
});


// --- step 23.1: several groups (design-steward 7.7.24) ----------------------------------------

test('guard (step 23.1, 7.9.4): group_reminder_set in a group turn only - the owner\'s turn is told to use the host\'s reminder tool', () => {
  assert.ok(FAMILY_TOOLS.has('group_reminder_set') && GROUP_TOOLS.has('group_reminder_set'));
  assert.match(groupTurnRefusal, /group_reminder_set/);
  const g = turns();
  g.split([group('明早提醒带伞')], 1);
  assert.equal(g.guard('group_reminder_set'), undefined);
  const o = turns();
  o.split([message('owner')], 1);
  assert.equal(o.guard('group_reminder_set'), groupReminderRefusal);
  assert.match(groupReminderRefusal, /reminder tool/);
  assert.equal(o.guard('family_memory_note'), familyRefusal);
  // ... and every step-23 group tool is still counted toward the six in a group turn.
  for (let i = 0; i < 5; i += 1) g.guard('list_show');
  assert.equal(g.guard('group_reminder_set'), answerNow);
});

const groupB = (text, qid = 'qb' + ids) => message(text, { kind: GROUP_SOURCE, channel: 'wechat', group_ref: 'r2', group_label: '老家群',
  question_ids: [qid], sender_refs: ['wxid_b'] });

test('fold (step 23.1, 7.7.24 item 3): group A\'s turn, then group B\'s - B\'s first step folds A to the one line that names no group, time or count', () => {
  const session = fakeSession(script([
    ev('turn/start', { turn: 1 }), ev('user/message', group('A_QUESTION_MARKER')),
    ev('assistant/message', { message: { id: 'a1', content: [{ type: 'text', text: 'A_ANSWER_MARKER 家庭群' }] } }), ev('turn/end', { turn: 1 }),
    ev('turn/start', { turn: 2 }), ev('user/message', groupB('B_QUESTION')),
  ]));
  assert.equal(foldGroupTurns(session, input => ({ ...input, id: 'f' + ++ids }), { before: 5 }), 1);
  const visible = session.surface.nodes.map(seq => JSON.stringify(session.log[seq].data));
  assert.ok(!visible.some(t => t.includes('A_QUESTION_MARKER') || t.includes('A_ANSWER_MARKER') || t.includes('家庭群')));
  const line = session.log.find(e => e.data?.source?.kind === FOLD_SOURCE).data.content[0].text;
  assert.equal(line, FOLD_LINE);
  assert.doesNotMatch(line, /group [^ ]+,|question\(s\)|\d{4}-\d\d-\d\d/);
  assert.ok(visible.some(t => t.includes('B_QUESTION')), 'B\'s own question is not folded');
});

test('compaction filter (step 23.1): a compaction over two groups\' turns removes both', () => {
  const events = script([
    ev('turn/start'), ev('user/message', { ...group('A_Q'), id: 'ua' }), ev('assistant/message', { message: { id: 'aa', content: [] } }), ev('turn/end'),
    ev('turn/start'), ev('user/message', { ...groupB('B_Q'), id: 'ub' }), ev('assistant/message', { message: { id: 'ab', content: [] } }), ev('turn/end'),
    ev('turn/start'), ev('user/message', { ...message('OWNER'), id: 'uo' }),
  ]);
  const request = { purpose: 'compaction', messages: [{ id: 'sys', role: 'system' }, { id: 'ua', role: 'user', content: 'A_Q' },
    { id: 'aa', role: 'assistant', content: 'A_A' }, { id: 'ub', role: 'user', content: 'B_Q' }, { id: 'ab', role: 'assistant', content: 'B_A' },
    { id: 'uo', role: 'user', content: 'OWNER' }] };
  const out = filterCompactionRequest(request, events, input => ({ ...input, id: 'x' + ++ids }));
  const text = JSON.stringify(out.messages);
  assert.ok(!/A_Q|A_A|B_Q|B_A/.test(text) && text.includes('OWNER'));
});

test('audience (step 23.1): two groups\' questions never share a turn - B\'s goes back and opens a later turn', () => {
  const g = turns();
  const a = group('A'), b = groupB('B');
  const first = g.split([a, b], 1, 1);
  assert.deepEqual(first.keep, [a]);
  assert.deepEqual(first.back, [b]);
  assert.deepEqual(g.split([groupB('B2')], 2, 1).back.length, 1, 'nor merged in a later step');
  g.split([b], 1, 2);
  assert.equal(g.audience().group_ref, 'r2');
  assert.equal(g.current.merged, 1, 'B\'s merge count starts from zero');
});

test('setup (step 23.1, 7.7.7): the audience record carries the questions the turn took; a later merge records the whole turn again', async () => {
  const w = setupWorld();
  const q1 = group('Q1', 'r1', 'gq1'), q2 = group('Q2', 'r1', 'gq2');
  await w.setup.step(w.payload(4), { kind: 'enter', messages: [q1] });
  assert.deepEqual(w.calls.audience.map(a => [a.turn, a.audience, a.group, a.questionIds, a.senderRefs]),
    [['4', 'group', { channel: 'wechat', group_ref: 'r1' }, ['gq1'], ['wxid_m']]]);
  const later = await w.setup.step(w.payload(4, 2), { kind: 'enter', messages: [q2] });
  assert.deepEqual(later.messages, [q2]);
  assert.deepEqual(w.calls.audience.at(-1).questionIds, ['gq1', 'gq2']);
  assert.deepEqual(w.g.current.questions, ['gq1', 'gq2']);
  // An owner turn records no questions.
  await w.setup.step(w.payload(5), { kind: 'enter', messages: [message('OWNER')] });
  assert.equal(w.calls.audience.at(-1).questionIds, undefined);
});

test('setup (step 23.1): a merge that cannot be recorded goes back for a later turn and is not this turn\'s question', async () => {
  let failing = false;
  const w = setupWorld({ audience: async () => { if (failing) throw new Error('HTTP 503'); return { recorded: true }; } });
  await w.setup.step(w.payload(4), { kind: 'enter', messages: [group('Q1', 'r1', 'gq1')] });
  failing = true;
  const payload = w.payload(4, 2);
  const q2 = group('Q2', 'r1', 'gq2');
  const later = await w.setup.step(payload, { kind: 'enter', messages: [q2] });
  assert.deepEqual(later.messages, []);
  assert.deepEqual(texts(payload.agent.inbox.list), ['Q2']);
  assert.deepEqual(w.g.current.questions, ['gq1'], 'the reply answers only what the turn took on record');
  assert.equal(w.g.current.merged, 1);
});

/** A whole boot with a fake host and a fake runtime: the packages it loads through its
 *  declared entry are stubs written into a temporary directory, so no DSH is involved. */
async function bootWorld() {
  const dir = mkdtempSync(join(tmpdir(), 'group-boot-'));
  const stub = (name, source) => {
    const base = join(dir, 'node_modules', '@deepseek-ai', name);
    mkdirSync(base, { recursive: true });
    writeFileSync(join(base, 'package.json'), JSON.stringify({ name: '@deepseek-ai/' + name, version: '0.0.0', type: 'module', exports: './index.mjs' }));
    writeFileSync(join(base, 'index.mjs'), source);
  };
  stub('dsh-tools', 'export const defineTool = tool => tool;\n');
  stub('dsh-llm', 'export class HarnessError extends Error {}\nexport const createUserMessage = input => ({ ...input, id: "msg-1", role: "user" });\n');
  const installed = [], steered = [], images = [];
  let ready = 0;
  const channels = {
    setGroupWake: fn => installed.push(fn), ready: () => { ready += 1; },
    ownerMessageSent: async () => ({}), sendGroup: async () => true, login: async () => ({}),
  };
  const group = { channel: 'wechat', group_ref: 'r1', label: '老家群', mention_prefix: '@管家', media_keep: 'on' };
  const host = {
    version: '1.0.0', capabilities: ['channels', 'groups'],
    config: { sessionId: 'root-1', provider: 'test', model: 'test', stateDir: dir,
      dshEntry: join(dir, 'entry.mjs'), rolesFile: join(here, '../roles.json'), rootRole: 'steward',
      todo: { module: 'dsh_todo', stateDirEnv: 'DSH_TODO_STATE_DIR' }, resumeWorkers: false },
    context: async () => '', events: async () => ({}),
    channels: { relay: async () => ({}), bind: async () => ({}), sourceOf: async () => null,
      recordDelivery: async () => ({}), list: async () => ({ channels: [] }) },
    groups: { list: async () => ({ groups: [group] }), recent: async () => ({ recent: [] }),
      bind: async () => ({}), relay: async () => ({}), reply: async () => ({}), audience: async () => ({}),
      ownerSpokeSince: async () => false, familyNote: async () => ({}), familyShow: async () => ({}) },
    wakeMedia: async () => ({ images, line: async (item, { attach } = {}) => {
      if (!attach) return `[图片 ${item.ref}；media_call]`;
      images.push({ attachmentId: 'att-1', mediaType: 'image/png' });
      return `[图片 ${item.ref}，原图已附]`;
    } }),
  };
  const agent = { session: { id: 'root-1' }, steer: message => steered.push(message) };
  const ctx = {
    logger: { info() {}, warn() {} },
    get: name => name === 'loader' ? { await: async () => {} } : undefined,
    tools: { guard() {}, register() {}, schemas: () => [] },
    systemPrompt: { section() {} }, on() {},
    agents: { create: async () => ({ agent, dispose: async () => {} }), get: () => agent, withoutInitiator: fn => fn() },
    sessionPersistence: { stat: async () => undefined, flush: async () => {} },
    goals: { disarm() {}, get: () => ({ phase: 'active' }) },
    tokenMeter: { measure: () => ({ totalTokens: 0 }) }, compaction: { compactNow: async () => false },
  };
  const handle = await boot(ctx, { host, channels });
  return { handle, installed, steered, ready };
}

test('group wake (step 23.1, 7.7.4): boot installs the handler that wakes with the group\'s own name', async t => {
  const w = await bootWorld();
  t.after(() => w.handle.close());
  // The handler under test is the one the plugin's boot handed to a channel, not one
  // imported separately: a boot that stopped installing it leaves nothing to invoke.
  assert.equal(w.ready, 1, 'boot reached the channel install and opened the channels');
  assert.equal(w.installed.length, 1);
  assert.equal(typeof w.installed[0], 'function');
  await w.installed[0]({ channel: 'wechat', groupRef: 'r1', senderRef: 'wxid_m', text: '@管家 看看这几张',
    at: '2026-11-23T10:00:00+00:00', message_id: 'g1', sender_role: 'member', sender_label: '成员甲',
    attachments: [{ kind: 'image', ref: 'm_1111111111111111', mime: 'image/png' }] });
  assert.equal(w.steered.length, 1);
  assert.deepEqual(w.steered[0].content.map(block => block.type), ['text', 'image']);
  const wake = JSON.parse(w.steered[0].content[0].text).group_turn;
  assert.equal(wake.group, '老家群', 'the group is named by its own name, never a shared one');
  assert.match(wake.messages[0].attachments[0], /原图已附/);
  assert.ok(!w.steered[0].content[0].text.includes('家庭群'), 'no shared-name fallback in the wake');
});

test('group wake (director 602 defect 3): a group the host does not name wakes nothing - refused, never group: null', () => {
  assert.equal(groupWakeName({ channel: 'wechat', group_ref: 'r1', label: '老家群' }), '老家群');
  for (const group of [undefined, null, {}, { label: null }, { label: '' }, { label: '  ' }, { label: 7 }])
    assert.throws(() => groupWakeName(group), /^Error: group_wake_unnamed/, JSON.stringify(group));
});

// --- step 23, Slack private channels (design-steward 7.7.19): only the platform layer changes ----

test('audience (7.7.19): a Slack group\'s turns follow the WeChat group\'s rules - audience, merge, put-back, guard, pause', () => {
  const tagged = channel => (text, ref = 'r1', qid = 'q' + ids) => message(text, { kind: GROUP_SOURCE, channel, group_ref: ref,
    group_label: '家庭群', question_ids: [qid], sender_refs: ['m1'] });
  const run = channel => {
    const ask = tagged(channel), g = turns(), seen = [];
    const owner = message('owner wrote');
    const first = g.split([ask('q1'), owner, ask('x', 'r2'), ...Array.from({ length: 6 }, (_, i) => ask('m' + i))], 1);
    seen.push([first.keep.length, first.back.length, g.audience()]);
    seen.push(['portfolio_projects', 'family_memory_note', 'request_confirmation', 'shell', 'memory_write', 'connections_list']
      .map(name => g.guard(name)));
    seen.push(g.split([owner], 1).keep.length, g.audience());
    seen.push(g.guard('family_memory_note'));
    return JSON.parse(JSON.stringify(seen).replaceAll(`"${channel}"`, '"CHANNEL"'));
  };
  const wechat = run('wechat'), slack = run('slack');
  assert.deepEqual(slack, wechat);
  assert.deepEqual(wechat[0][2], { kind: 'group', channel: 'CHANNEL', group_ref: 'r1', label: '家庭群' });
});

function slackGroupWorld(answers = {}) {
  const calls = [], sent = [], rooms = [];
  const host = { relay: async m => { calls.push(['owner-relay', m]); return { message_id: 'x' }; }, bind: async m => { calls.push(['owner-bind', m]); return { bound: true }; },
    sourceOf: async () => null, recordDelivery: async () => ({}), list: async () => ({ channels: [] }) };
  const groups = { list: async () => ({ owner_refs: { slack: 'U1' }, groups: [{ channel: 'slack', group_ref: 'C1', mention_prefix: '<@UBOT>' }] }),
    relay: async m => { calls.push(['relay', m]); return answers.relay ?? { message_id: 'g1', sender_role: 'member', sender_label: '成员甲' }; },
    bind: async m => { calls.push(['bind', m]); return answers.bind ?? { bound: true, notice: 'NOTICE' }; } };
  const dir = mkdtempSync(join(tmpdir(), 'slack-group-channels-'));
  const channels = createOwnerChannels({ host, groups, owedFile: join(dir, 'owed.json') });
  channels.service.register({ name: 'slack', send: async (text, o) => { sent.push(['1:1', text, o]); return {}; }, status: () => ({ connected: true }),
    sendGroup: async (room, text, options) => { sent.push(['group', room, text, options]); return {}; }, setRooms: async r => { rooms.push(r); } });
  const woken = [];
  channels.setGroupWake(async q => { woken.push(q); });
  channels.ready();
  return { channels, calls, sent, rooms, woken, owed: join(dir, 'owed.json') };
}
const slackInbound = (w, extra) => w.channels.service.inbound({ channel: 'slack', senderRef: 'U2', messageRef: 'C1:1763481600.0003',
  text: '123456', at: new Date().toISOString(), conversationRef: 'C1', asked: true, ...extra });

test('channels (7.7.19): a Slack channel\'s question takes the group path - relayed, woken, never the owner\'s lane or a binding', async () => {
  const w = slackGroupWorld();
  assert.deepEqual(await slackInbound(w), { accepted: true });
  assert.deepEqual(w.calls.map(c => c[0]), ['relay']);
  assert.deepEqual({ ...w.calls[0][1], at: undefined }, { channel: 'slack', groupRef: 'C1', senderRef: 'U2', messageRef: 'C1:1763481600.0003',
    text: '123456', at: undefined });
  assert.ok(!existsSync(w.owed) || !readFileSync(w.owed, 'utf8').includes('123456'));
  assert.equal(w.woken.length, 1);
  assert.equal(w.woken[0].channel, 'slack');
  assert.deepEqual(w.rooms.at(-1), { ownerRef: 'U1', rooms: [{ room_ref: 'C1', prefix: '<@UBOT>' }] });
  assert.deepEqual(w.sent, [], 'nothing is said for an accepted question until the turn ends');
});

test('channels (D8): the lines that answer one Slack message - a quota notice, a confirmation line, the bind notice - go into its thread', async () => {
  const over = slackGroupWorld({ relay: { refused: 'member_quota', notice: '问得太多了' } });
  assert.deepEqual(await slackInbound(over, { text: 'hi' }), { accepted: false, reason: 'member_quota' });
  assert.deepEqual(over.sent, [['group', 'C1', '问得太多了', { replyToRef: 'C1:1763481600.0003' }]]);
  const line = '已确认 K7Q2A，正在执行。';
  const confirmed = slackGroupWorld({ relay: { message_id: 'g2', confirmation: { code: 'K7Q2A', outcome: 'confirmed' }, confirmation_line: line } });
  await slackInbound(confirmed, { text: '确认 K7Q2A' });
  assert.deepEqual(confirmed.sent, [['group', 'C1', line, { replyToRef: 'C1:1763481600.0003' }]]);
  const bound = slackGroupWorld();
  assert.deepEqual(await slackInbound(bound, { text: 'G-7K2M-Q9XD', conversationRef: 'C9', messageRef: 'C9:1.2', asked: undefined,
    senderRef: 'U1', groupBind: { prefix: '<@UBOT>', code: 'G-7K2M-Q9XD' } }), { accepted: true });
  assert.deepEqual(bound.calls.at(-1), ['bind', { channel: 'slack', groupRef: 'C9', senderRef: 'U1', messageRef: 'C9:1.2', prefix: '<@UBOT>',
    code: 'G-7K2M-Q9XD' }]);
  assert.deepEqual(bound.sent.at(-1), ['group', 'C9', 'NOTICE', { replyToRef: 'C9:1.2' }]);
});

test('group reply (D8): the question\'s channel ref the backend answers goes to the send, so Slack threads the reply', async () => {
  const g = turns(), sent = [];
  const ask = (text, qid) => message(text, { kind: GROUP_SOURCE, channel: 'slack', group_ref: 'C1', group_label: '家庭群',
    question_ids: [qid], sender_refs: ['U2'] });
  g.split([ask('Q1', 'gq1')], 1, 7);
  const watch = groupReplies({ turns: g, warn: () => {},
    reply: async args => ({ channel: 'slack', group_ref: 'C1', text: args.body, reply_to_ref: 'C1:1763481600.0003' }),
    sendGroup: async (...args) => { sent.push(args); } });
  watch({ type: 'turn/start', data: { turn: 7 } });
  watch({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '周六去公园。' }] } } });
  await watch({ type: 'turn/end', data: { turn: 7, reason: { kind: 'completed' } } });
  assert.deepEqual(sent, [['slack', 'C1', '周六去公园。', { replyToRef: 'C1:1763481600.0003' }]]);
  // A backend answer without the ref (a reply recorded before it carried one) sends as before.
  const r = groupReply('答。');
  await r.done;
  assert.deepEqual(r.sent, [['wechat', 'r1', '「Q2」\n答。']]);
});

test('channels (D5 a): the whitelist carries each group\'s dashboard name, so a channel can name a paused group', async () => {
  const rooms = [];
  const groups = { list: async () => ({ owner_refs: { slack: 'U1' }, groups: [
    { channel: 'slack', group_ref: 'C1', mention_prefix: '<@UBOT>', label: '家人' },
    { channel: 'slack', group_ref: 'C2', mention_prefix: '<@UBOT>', label: null }] }) };
  const host = { sourceOf: async () => null, recordDelivery: async () => ({}), list: async () => ({ channels: [] }) };
  const channels = createOwnerChannels({ host, groups, owedFile: join(mkdtempSync(join(tmpdir(), 'rooms-label-')), 'owed.json') });
  channels.service.register({ name: 'slack', send: async () => ({}), status: () => ({ connected: true }),
    sendGroup: async () => ({}), setRooms: async r => { rooms.push(r); } });
  channels.ready();
  await channels.refreshRooms();
  assert.deepEqual(rooms.at(-1), { ownerRef: 'U1', rooms: [{ room_ref: 'C1', prefix: '<@UBOT>', label: '家人' },
    { room_ref: 'C2', prefix: '<@UBOT>' }] });
});
