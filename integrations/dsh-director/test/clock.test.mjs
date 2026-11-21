/** Message time (design-message-time §12, 2-7): the root session's system clock line.
 *  Pure logic with fake `next`s; the real pinned DSH arm is in
 *  integrations/dsh-driver/runtime.steward.test.mjs. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Clock, CLOCK_TAG, CLOCK_TOOL_GAP_MS, clockLine, ContextInjection, Continuation, defaultLabels, endsDormancyFor,
  grepHistory, wakeSourcesFor } from '../lib/core.mjs';

const T = Date.parse('2026-11-20T10:14:59.900Z');
const session = { id: 'root' }, child = { id: 'child' };
const createMessage = message => ({ role: 'user', ...message });
const event = (text, kind) => createMessage({ content: [{ type: 'text', text }], ...(kind ? { source: { kind } } : {}) });
const runtimeContext = event('runtime context', 'dsh-runtime-context');
const enter = claimed => async () => ({ kind: 'enter', messages: [...claimed, runtimeContext] });
const step = (messages, over = {}) => ({ agent: { session }, messages, signal: new AbortController().signal, ...over });
const clockOf = (over = {}) => {
  const logs = [];
  let now = T;
  const clock = new Clock({ session, createMessage, now: () => now, log: line => logs.push(line), ...over });
  return { clock, logs, at: ms => { now = ms; } };
};
const texts = decision => decision.messages.map(m => m.content[0].text);
const throwing = () => { throw new Error('clock broke'); };

// ---- 2. clockLine
test('clockLine: UTC to the minute for a director; the owner\'s local time in winter and summer for a steward', () => {
  assert.equal(clockLine({ now: T }), '[system clock] now 2026-11-20T10:14Z');
  assert.equal(clockLine({ now: T, tz: 'Europe/Paris' }),
    '[system clock] now 2026-11-20T10:14Z · owner\'s time Europe/Paris 2026-11-20 周五 11:14+01:00');
  assert.equal(clockLine({ now: Date.parse('2027-07-01T10:14:00Z'), tz: 'Europe/Paris' }),
    '[system clock] now 2027-07-01T10:14Z · owner\'s time Europe/Paris 2027-07-01 周四 12:14+02:00');
  assert.equal(clockLine({ now: T, where: 'snapshot' }), '[system clock] now 2026-11-20T10:14Z (context snapshot)');
  assert.equal(clockLine({ now: T, where: 'tool' }), '[system clock] now 2026-11-20T10:14Z (after tool)');
  assert.ok(clockLine({ now: T }).startsWith(CLOCK_TAG));
});

test('Clock: a zone that is not one, or none, leaves the clock in UTC (one log line for the bad one)', async () => {
  const bad = clockOf({ tz: 'Mars/Olympus' });
  assert.equal(bad.clock.tz, undefined);
  assert.equal(bad.logs.length, 1);
  assert.match(bad.logs[0], /^clock: memory\.timeZone "Mars\/Olympus" is not a time zone .*; UTC only$/);
  const decision = await bad.clock.preStep(step([event('hello')]), enter([event('hello')]));
  assert.equal(texts(decision)[0], '[system clock] now 2026-11-20T10:14Z');
  const none = clockOf();
  assert.equal(none.clock.tz, undefined);
  assert.deepEqual(none.logs, []);
  const paris = clockOf({ tz: 'Europe/Paris' });
  assert.equal(texts(await paris.clock.preStep(step([event('hello')]), enter([event('hello')])))[0],
    '[system clock] now 2026-11-20T10:14Z · owner\'s time Europe/Paris 2026-11-20 周五 11:14+01:00');
});

// ---- 3. the pre-step hook
test('pre-step: one clock line FIRST among the step\'s new messages, as a notice of its own kind; input untouched', async () => {
  const { clock } = clockOf();
  const owner = event('what time is it?'), wake = event('runtime event', defaultLabels.runtimeEvent);
  const decision = await clock.preStep(step([owner]), enter([owner]));
  assert.equal(decision.kind, 'enter');
  assert.equal(decision.messages.length, 3);
  const [line] = decision.messages;
  assert.equal(line.content[0].text, '[system clock] now 2026-11-20T10:14Z');
  assert.deepEqual(line.source, { kind: defaultLabels.clock, form: 'notice', summary: '[system clock] now 2026-11-20T10:14Z',
    clock_at: Clock.sourceAt(T) }, 'the source carries the exact instant the line reads back from the log');
  assert.equal(decision.messages[1], owner, 'the input itself is the same object, its text unchanged');
  assert.equal(decision.messages[2], runtimeContext, 'the runtime context stays last');
  const three = [wake, owner, event('child', 'agent-message')];
  const batch = await clock.preStep(step(three), enter(three));
  assert.equal(batch.messages.filter(m => m.source?.kind === defaultLabels.clock).length, 1, 'three claimed, one line');
  assert.equal(batch.messages[0].source.kind, defaultLabels.clock);
});

test('pre-step TEETH: the clock is the minute the step runs, never the queued input\'s created_at', async () => {
  // A letter written 30 minutes before the step that claims it (it waited behind a long turn).
  const { clock } = clockOf();
  const queued = new Date(T - 30 * 60 * 1000).toISOString();
  const letter = event('runtime event: ' + JSON.stringify({ driver_inbox: [{ message_id: 'm-1', created_at: queued }] }),
    defaultLabels.runtimeEvent);
  const [line] = texts(await clock.preStep(step([letter]), enter([letter])));
  assert.equal(line, '[system clock] now 2026-11-20T10:14Z', 'the minute of T');
  assert.ok(!line.includes(queued.slice(0, 16)), 'a queue-time stamp or a copy of created_at writes 09:44');
});

test('pre-step: no clock without real input, on an aborted step, on a refused step, or on a child\'s step', async () => {
  const { clock } = clockOf();
  const owner = event('hello');
  const none = await clock.preStep(step([]), enter([]));
  assert.deepEqual(texts(none), ['runtime context'], 'nothing claimed');
  const oldClock = event('[system clock] now 2026-11-20T09:00Z', defaultLabels.clock);
  assert.deepEqual(texts(await clock.preStep(step([oldClock]), enter([oldClock]))), [oldClock.content[0].text, 'runtime context'],
    'only a clock line put back (a refused goal round): no second line');
  const snapshot = event('snapshot', defaultLabels.context);
  assert.equal(texts(await clock.preStep(step([snapshot]), enter([snapshot]))).length, 2, 'only a snapshot: no line');
  const aborted = new AbortController(); aborted.abort();
  assert.equal(texts(await clock.preStep(step([owner], { signal: aborted.signal }), enter([owner]))).length, 2);
  const refused = await clock.preStep(step([owner]), async () => ({ kind: 'reject', reason: 'x' }));
  assert.deepEqual(refused, { kind: 'reject', reason: 'x' });
  assert.equal(texts(await clock.preStep(step([owner], { agent: { session: child } }), enter([owner]))).length, 2,
    'a child session gets no clock');
  assert.equal(clock.lastAt, -Infinity, 'no line written, none recorded');
});

test('pre-step (E2): a clock that throws leaves the step entering, without a line, with exactly one log line', async () => {
  const { clock, logs } = clockOf({ line: throwing });
  const owner = event('hello');
  for (let i = 0; i < 2; i++) {
    const decision = await clock.preStep(step([owner]), enter([owner]));
    assert.equal(decision.kind, 'enter');
    assert.deepEqual(decision.messages, [owner, runtimeContext]);
  }
  assert.deepEqual(logs, ['clock: pre-step failed: clock broke'], 'logged once, not once per step');
  assert.equal(clock.lastAt, -Infinity);
});

// ---- 4. not input
test('a clock line is not input: it ends no dormancy and restores no continuation budget', () => {
  for (const labels of [defaultLabels, { ...defaultLabels, clock: 'host-clock', runtimeEvent: 'host-event' }]) {
    const message = { type: 'user/message', data: { source: { kind: labels.clock } } };
    const ends = endsDormancyFor(wakeSourcesFor(labels));
    assert.equal(ends(message), false, labels.clock);
    assert.equal(ends({ type: 'user/message', data: { source: { kind: labels.runtimeEvent } } }), true, 'a real event still does');
    const continuation = new Continuation({ endsDormancy: ends });
    continuation.auto = 3;
    continuation.observe(message);
    assert.equal(continuation.auto, 3, 'the clock does not reset Continuation.auto');
  }
});

// ---- 5. tool results
const tool = (over = {}) => ({ agent: { session }, name: 'bash', ...over });
const result = { content: [{ type: 'text', text: 'ok' }] };
const accept = async () => ({ kind: 'accept' });

test('tool result: a clock block at the end once the last line is ten minutes old, then not again', async () => {
  const { clock, at } = clockOf();
  const owner = event('hello');
  const logged = await clock.preStep(step([owner]), enter([owner]));
  clock.observe({ type: 'user/message', data: logged.messages[0] });   // that line is in the log
  at(T + CLOCK_TOOL_GAP_MS - 1);
  assert.deepEqual(await clock.postExecute(tool(), result, accept), { kind: 'accept' }, 'under ten minutes: unchanged');
  at(T + CLOCK_TOOL_GAP_MS);
  const late = await clock.postExecute(tool(), result, accept);
  assert.deepEqual(late, { kind: 'accept',
    content: [{ type: 'text', text: 'ok' }, { type: 'text', text: '[system clock] now 2026-11-20T10:24Z (after tool)' }] });
  assert.deepEqual(result.content, [{ type: 'text', text: 'ok' }], 'the result itself is not mutated');
  assert.deepEqual(await clock.postExecute(tool(), result, accept), { kind: 'accept' }, 'once, not on every result after');
  // A decision that already replaced the content keeps that content, with the line after it.
  at(T + 3 * CLOCK_TOOL_GAP_MS);
  const replaced = await clock.postExecute(tool(), result, async () => ({ kind: 'accept', content: [{ type: 'text', text: 'policy' }] }));
  assert.deepEqual(replaced.content.map(b => b.text), ['policy', '[system clock] now 2026-11-20T10:44Z (after tool)']);
});

test('tool result: no clock on a block, a value, an error or a child\'s result', async () => {
  const { clock } = clockOf();
  const block = { kind: 'block', feedback: [{ type: 'text', text: 'no' }] };
  assert.equal(await clock.postExecute(tool(), result, async () => block), block);
  const value = { kind: 'accept', value: { x: 1 } };
  assert.equal(await clock.postExecute(tool(), result, async () => value), value);
  assert.deepEqual(await clock.postExecute(tool(), { ...result, isError: true }, accept), { kind: 'accept' });
  assert.deepEqual(await clock.postExecute(tool({ agent: { session: child } }), result, accept), { kind: 'accept' });
  assert.equal(clock.lastAt, -Infinity);
});

test('tool result (E2): a clock that throws leaves the result accepted and unchanged, with exactly one log line', async () => {
  const { clock, logs } = clockOf({ line: throwing });
  for (let i = 0; i < 2; i++) {
    const decision = await clock.postExecute(tool(), result, accept);
    assert.deepEqual(decision, { kind: 'accept' }, 'accepted, content unchanged, not an error');
  }
  assert.deepEqual(logs, ['clock: post-execute failed: clock broke']);
});

test('a failure of the step or the tool itself is not the clock\'s: it propagates, and nothing is logged', async () => {
  const { clock, logs } = clockOf();
  const inner = async () => { throw new Error('inner'); };
  await assert.rejects(clock.preStep(step([event('hello')]), inner), /inner/);
  await assert.rejects(clock.postExecute(tool(), result, inner), /inner/);
  assert.deepEqual(logs, []);
});

test('tool result: gapMs (tests only) replaces the ten-minute gap', async () => {
  const { clock, at } = clockOf({ gapMs: 5 });
  const logged = await clock.preStep(step([event('hello')]), enter([event('hello')]));
  clock.observe({ type: 'user/message', data: logged.messages[0] });
  at(T + 4);
  assert.deepEqual(await clock.postExecute(tool(), result, accept), { kind: 'accept' });
  at(T + 5);
  assert.equal((await clock.postExecute(tool(), result, accept)).content.at(-1).text, '[system clock] now 2026-11-20T10:14Z (after tool)');
});

// ---- 7. the log is the truth
test('message time TEETH: the clock advances on the log, not on the proposal', async () => {
  const { clock, at } = clockOf();
  const owner = event('hello');
  const decision = await clock.preStep(step([owner]), enter([owner]));
  assert.equal(clock.lastAt, -Infinity, 'a proposed line has written nothing');
  // An outer hook replaced the step's messages: the line never entered the log, so the ten
  // minutes were not paid and the next tool result still carries its own line.
  at(T + 1);
  const content = (await clock.postExecute(tool(), result, accept)).content;
  assert.equal(content.at(-1).text, '[system clock] now 2026-11-20T10:14Z (after tool)');
  // The line that DID enter the log pays the gap for the tool result.
  clock.observe({ type: 'user/message', data: decision.messages[0] });
  assert.equal(clock.lastAt, T);
  at(T + 2);
  assert.deepEqual(await clock.postExecute(tool(), result, accept), { kind: 'accept' }, 'the logged line is the last clock');
  // A clock_at that is not a time leaves lastAt where it was, and never throws.
  clock.observe({ type: 'user/message', data: { source: { kind: defaultLabels.clock, clock_at: 'not a time' } } });
  clock.observe({ type: 'tool/result', data: { source: { clock_at: Clock.sourceAt(T + 10) } } });
  assert.equal(clock.lastAt, T, 'neither a bad value nor a non-message moves it');
});

// ---- 7. snapshot and history
const fakeSession = () => {
  const appended = [];
  return { appended, snapshotEvents: () => [], append: (type, message) => appended.push([type, message]) };
};
const source = { refresh: async () => {}, render: () => 'the state' };

test('snapshot: the context snapshot carries its own clock line after the header', async () => {
  const { clock } = clockOf();
  const injection = new ContextInjection(fakeSession(), source, createMessage, { header: 'Snapshot:\n', clock });
  const message = await injection.prepare();
  assert.equal(message.content[0].text, 'Snapshot:\n[system clock] now 2026-11-20T10:14Z (context snapshot)\nthe state');
  assert.equal(clock.lastAt, -Infinity, 'prepared is not appended: only the log counts');
  clock.observe({ type: 'user/message', data: message });
  assert.equal(clock.lastAt, T, 'the snapshot line counts as the last clock once its message is in the session');
  const bare = await new ContextInjection(fakeSession(), source, createMessage, { header: 'Snapshot:\n' }).prepare();
  assert.equal(bare.content[0].text, 'Snapshot:\nthe state', 'no clock, no line');
});

test('snapshot (E2): a clock that throws leaves the snapshot injected without its line; neither hook throws', async () => {
  const { clock, logs } = clockOf({ line: throwing });
  const injection = new ContextInjection(fakeSession(), source, createMessage, { header: 'Snapshot:\n', clock });
  const message = await injection.prepare();
  assert.equal(message.content[0].text, 'Snapshot:\nthe state');
  const session = fakeSession();
  const retry = new ContextInjection(session, source, createMessage, { header: 'Snapshot:\n', clock });
  await retry.beforeRequest();
  assert.equal(session.appended.length, 1, 'the overflow-retry path appends the snapshot');
  assert.equal(session.appended[0][1].content[0].text, 'Snapshot:\nthe state');
  assert.deepEqual(logs, ['clock: snapshot failed: clock broke'], 'one line for the session, whichever site');
  assert.equal(clock.lastAt, -Infinity);
});

test('grep_chat_history: hits carry their time, and clock lines never fill a date search', () => {
  const events = [];
  for (let i = 0; i < 20; i++) events.push({ seq: i, type: 'user/message', time: T + i * 60000,
    data: event(`[system clock] now 2026-11-19T${String(i).padStart(2, '0')}:00Z`, defaultLabels.clock) });
  events.push({ seq: 20, type: 'tool/result', time: T, data: { content: [{ type: 'text', text: 'listing' },
    { type: 'text', text: '[system clock] now 2026-11-19T21:00Z (after tool)' }] } });
  events.push({ seq: 21, type: 'user/message', time: T, data: event('Snapshot:\n[system clock] now 2026-11-19T22:00Z (context snapshot)\nthe state', defaultLabels.context) });
  events.push({ seq: 22, type: 'user/message', time: Date.parse('2026-11-19T18:49:05.120Z'), data: event('the owner on 2026-11-19: ship it') });
  const found = grepHistory(events, '2026-11-19');
  assert.deepEqual(found.matches.map(m => m.seq), [22], 'only the owner message');
  assert.equal(found.matches[0].time, '2026-11-19T18:49:05.120Z');
  assert.equal(found.has_more, false);
  assert.deepEqual(grepHistory(events, 'the state').matches.map(m => m.seq), [21], 'the rest of a snapshot is still searched');
  assert.equal(grepHistory([{ seq: 0, type: 'user/message', data: event('timeless') }], 'timeless').matches[0].time, null);
  // A host that renamed the clock's kind names it, and its lines are skipped the same way.
  const renamed = [{ seq: 0, type: 'user/message', data: event('no tag here, 2026-11-19', 'host-clock') }];
  assert.equal(grepHistory(renamed, '2026-11-19', {}, { clockKind: 'host-clock' }).matches.length, 0);
});
