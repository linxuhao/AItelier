import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DriverRuntime, IdleReview, endsDormancy, dormancyRefusal, watchRefusal, Continuation, continueSource, continuationQuestion, reviewInstruction, NOTHING_LEFT, MAX_AUTO_CONTINUE, processSession, processAlive, backgroundProcesses, StateInjection, stateInjectionSource, runtimeEventSource, eventCarriesSomething, ownEchoOnly, grepHistory, connectionErrorMessage, driverGoal, projectInboxDigest, listProjectInbox, INBOX_EXCERPT_CHARS, INBOX_SUPERSEDED_EXCERPT_CHARS, INBOX_CEILING_CHARS } from './runtime.mjs';
import { bindDriverGoal, driverGoalSection } from './goal.mjs';

test('an explicitly cleared native goal is not recreated on resident restart', () => {
  const agent={session:{snapshotEvents:()=>[{type:'goal/change',data:{operation:'clear'}}]}};
  assert.equal(bindDriverGoal({get:()=>undefined},agent,'bootstrap template'),undefined);
});

// An ACTIVE goal no longer keeps a differing objective: it follows goal.md (runtime.goalseed.test.mjs).
test('resident restart preserves stopped goals and their edited objectives', () => {
  for (const phase of ['paused', 'blocked', 'complete']) {
    const current = {id:'kept',revision:7,objective:'owner-edited objective',phase,activation:'armed'};
    const goals = {get: () => current};
    assert.equal(bindDriverGoal(goals, {}, 'bootstrap template'), current);
    assert.match(driverGoalSection(goals, {}), /owner-edited objective/);
    assert.doesNotMatch(driverGoalSection(goals, {}), /bootstrap template/);
  }
});

test('enabled resident re-arms the exact active native goal without resetting its budget', () => {
  const agent = {}, current = {id:'kept',revision:7,objective:'owner-edited objective',phase:'active',activation:'disarmed',roundsStarted:12,maxGoalRounds:256};
  const goals = {get: a => {assert.equal(a,agent);return current;}, resume: (a,ref) => {
    assert.equal(a,agent);assert.deepEqual(ref,{id:'kept',revision:7});return {...current,revision:8,activation:'armed'};
  }};
  const resumed = bindDriverGoal(goals, agent, 'owner-edited objective');
  assert.equal(resumed.roundsStarted,12);assert.equal(resumed.objective,current.objective);
  assert.equal(resumed.activation,'armed');
});

// Each start spends one counted round, so a resident goal's cap is raised at start instead of
// blocking a healthy director after its 256th restart (runtime.goalseed.test.mjs).
test('an exhausted active native budget is raised at restart, not turned into a blocker', () => {
  const current={id:'capped',revision:3,objective:'template',phase:'active',activation:'disarmed',roundsStarted:256,maxGoalRounds:256};
  let edited;
  const goals={get:()=>edited??current,block:()=>assert.fail('blocked'),
    edit:(_agent,ref,request)=>{assert.deepEqual(ref,{id:'capped',revision:3});assert.deepEqual(request,{maxGoalRounds:512});return edited={...current,revision:4,maxGoalRounds:512};},
    resume:(_agent,ref)=>{assert.deepEqual(ref,{id:'capped',revision:4});return {...edited,revision:5,activation:'armed'};}};
  const view=bindDriverGoal(goals,{},'template');
  assert.equal(view.phase,'active');assert.equal(view.activation,'armed');assert.equal(view.roundsStarted,256);assert.equal(view.maxGoalRounds,512);
});

test('State injection survives resume and ignores ordinary events, steps and failed compaction', async () => {
  const events = [], session = { snapshotEvents: () => events };
  let reads = 0, value = 'initial';
  const runtime = { refresh: async () => { reads++; }, render: () => value };
  let injection = new StateInjection(session, runtime, input => input);
  const append = (type, data) => {
    const event = { seq: events.length + 1, type, data }; events.push(event); injection.observe(event); return event;
  };
  session.append = append;
  const first = await injection.prepare();
  await injection.beforeRequest();
  assert.equal(events.length, 0, 'queued first-step input must be committed by the native loop after its system prompt');
  append('user/message', first);
  assert.equal(first.source.kind, stateInjectionSource);
  for (let i = 0; i < 4; i++) {
    value = 'State changed ' + i;
    assert.equal(await injection.prepare(), undefined);
    await injection.beforeRequest();
  }
  append('compaction/end', { error: 'summary failed' });
  assert.equal(await injection.prepare(), undefined);
  injection = new StateInjection(session, runtime, input => input);
  assert.equal(await injection.prepare(), undefined, 'process resume duplicated an existing injection');
  assert.equal(reads, 1);
  const compact = append('compaction/end', {});
  value = 'fresh after compact';
  const next = await injection.prepare(); append('user/message', next);
  assert.equal(next.source.compaction_seq, compact.seq);
  assert.match(next.content[0].text, /fresh after compact/);
  assert.equal(await injection.prepare(), undefined);
  assert.equal(reads, 2);
});

test('overflow retry injects once without another pre-step and restores pending postcompact after restart', async () => {
  const events = [{seq:1,type:'user/message',data:{source:{kind:stateInjectionSource,compaction_seq:0}}},
    {seq:2,type:'compaction/end',data:{}}];
  const session = { snapshotEvents: () => events };
  let reads = 0;
  const injection = new StateInjection(session, { refresh: async () => { reads++; }, render: () => 'overflow fresh' }, input=>input);
  session.append = (type, data, intent) => {
    assert.deepEqual(intent, {surfaceOp:'append'});
    const event = {seq:events.length+1,type,data}; events.push(event); injection.observe(event);
  };
  await injection.beforeRequest(); await injection.beforeRequest();
  assert.equal(reads, 1);
  assert.equal(events.length, 3);
  assert.equal(events.at(-1).data.source.compaction_seq, 2);
});

const snapshot = status => ({ event_seq: 10, nodes: [{ name: 'n', status }],
  project_note: { index: [{ index_line: 'project rule' }] },
  private_note: { index: [{ index_line: 'private rule' }] }, driver_skill: 'driver skill' });
async function fixture(fn) {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-driver-'));
  try { await fn(directory); } finally { await rm(directory, { recursive: true }); }
}
test('fresh context replaces names/statuses and both note indexes after compact', async () => fixture(async directory => {
  let current = snapshot('PENDING');
  const r = new DriverRuntime({ project: 'alpha', driver: 'driver-a', directory,
    request: async path => path.endsWith('/me') ? { driver_id: 'driver-a' } : current });
  await r.init();
  assert.equal(r.after, 10);
  current = { ...snapshot('VERIFIED'), private_note: { index: [{ index_line: 'new private rule' }] } };
  r.dirty = true; await r.refresh();
  const rendered = JSON.parse(r.render());
  assert.deepEqual(rendered.nodes, [{ name: 'n', status: 'VERIFIED' }]);
  assert.equal(rendered.private_note.index[0].index_line, 'new private rule');
  assert.equal(rendered.project_note.index[0].index_line, 'project rule');
  assert.deepEqual(Object.keys(rendered).sort(), ['driver_id','nodes','private_note','project_id','project_note']);
  assert.match(driverGoal('alpha'), /你是 alpha 项目 State DAG 的常驻导演/);
  assert.match(driverGoal('alpha'), /critical issues/);
  assert.match(driverGoal('alpha'), /Claude Sonnet 5\.5 和 GPT-6\.1 Sol/);
  assert.match(driverGoal('alpha'), /Claude Opus 5\.5/);
  assert.doesNotMatch(driverGoal('alpha'), /\{\{project_id\}\}/);
}));
test('wrong identity refuses bootstrap before private context is read', async () => fixture(async directory => {
  let reads = 0;
  const r = new DriverRuntime({ project: 'alpha', driver: 'driver-a', directory,
    request: async () => { reads++; return { driver_id: 'driver-b' }; } });
  await assert.rejects(r.init(), /identity mismatch/);
  assert.equal(reads, 1);
}));
test('timeouts stay inside runtime; a combined event advances both durable cursors', async () => fixture(async directory => {
  let calls = 0, delivered = [];
  const r = new DriverRuntime({ project: 'alpha', driver: 'driver-a', directory,
    request: async (path, args) => {
      if (path.endsWith('/me')) return { driver_id: 'driver-a' };
      if (path.endsWith('/context')) return snapshot('PENDING');
      if (path.endsWith('/heartbeat')) return {};
      assert.equal(args.include_driver_inbox, true);
      assert.equal(args.return_when_idle, false);
      assert.equal(args.timeout_seconds, 240);
      if (++calls < 3) return { timed_out: true, next_after: 10, next_inbox_after: 0 };
      if (calls === 3) return { timed_out: false, next_after: 12, next_inbox_after: 7, events: [{ type: 'changed' }] };
      r.close(); throw new Error('closed');
    }, onEvent: async value => { delivered.push(value); } });
  await r.init();
  await assert.rejects(r.pump(), /closed/);
  assert.equal(delivered.length, 1);
  assert.deepEqual(JSON.parse(await readFile(join(directory, 'cursor.json'))), { after: 12, inboxAfter: 7 });
  const restored = new DriverRuntime({ project: 'alpha', driver: 'driver-a', directory,
    request: async path => path.endsWith('/me') ? { driver_id: 'driver-a' } : snapshot('PENDING') });
  await restored.init(); assert.equal(restored.after, 12); assert.equal(restored.inboxAfter, 7);
}));
test('runtime parks native continuation, reviews idle active goals, and cancels reviews during work or shutdown', async t => {
  t.mock.timers.enable({apis:['setTimeout']});
  const agent={status:'idle'},goal={id:'kept',phase:'active',activation:'armed',roundsStarted:4};
  let reviews=0;
  const review=new IdleReview({agent,goals:{get:()=>goal,disarm:()=>{goal.activation='disarmed';}},intervalMs:100,
    notify:()=>{reviews++;agent.status='running';review.statusChanged('running');},onError:error=>{throw error;}});
  review.statusChanged('idle');
  assert.equal(goal.phase,'active');assert.equal(goal.activation,'disarmed');assert.equal(goal.roundsStarted,4);
  t.mock.timers.tick(99);await Promise.resolve();assert.equal(reviews,0);
  agent.status='running';review.statusChanged('running');
  t.mock.timers.tick(500);await Promise.resolve();assert.equal(reviews,0);
  agent.status='idle';review.statusChanged('idle');
  t.mock.timers.tick(100);await Promise.resolve();assert.equal(reviews,1);
  agent.status='idle';review.statusChanged('idle');review.close();
  t.mock.timers.tick(500);await Promise.resolve();assert.equal(reviews,1);
});
test('dormancy parks the timed review, compacts once on the first idle, and wake restores the review', async t => {
  t.mock.timers.enable({apis:['setTimeout']});
  const agent={status:'idle'},goal={id:'kept',phase:'active',activation:'armed',roundsStarted:4};
  let reviews=0,compactions=0;
  const review=new IdleReview({agent,goals:{get:()=>goal,disarm:()=>{goal.activation='disarmed';}},intervalMs:100,
    notify:()=>{reviews++;},compact:()=>{compactions++;},onError:error=>{throw error;}});
  // Entered during the tool's turn (running), so the review timer is not armed yet.
  agent.status='running';review.statusChanged('running');
  const entered=review.enterDormancy('nothing authorized');
  assert.equal(entered.reason,'nothing authorized');assert.ok(Date.parse(entered.since));
  agent.status='idle';review.statusChanged('idle');
  await Promise.resolve();await Promise.resolve();
  assert.equal(compactions,1,'first idle after entering compacts once');
  assert.equal(goal.activation,'disarmed','dormancy still parks native continuation');
  t.mock.timers.tick(10000);await Promise.resolve();
  assert.equal(reviews,0,'no timed review while dormant');
  // Further idles while still dormant neither review nor compact again.
  review.statusChanged('running');review.statusChanged('idle');await Promise.resolve();
  t.mock.timers.tick(10000);await Promise.resolve();
  assert.equal(compactions,1);assert.equal(reviews,0);
  // A real message wakes it: the turn runs, the next idle arms the normal review.
  assert.equal(review.wake().reason,'nothing authorized');
  assert.equal(review.wake(),undefined,'waking twice is a no-op');
  agent.status='running';review.statusChanged('running');
  agent.status='idle';review.statusChanged('idle');
  t.mock.timers.tick(100);await Promise.resolve();
  assert.equal(reviews,1);assert.equal(compactions,1,'waking does not compact');
  review.close();
});
test('only real input ends dormancy: owner, runtime events and child messages, not DSH bookkeeping', () => {
  const message = kind => ({ type: 'user/message', data: { content: [], ...(kind === undefined ? {} : { source: { kind } }) } });
  for (const kind of [undefined, 'user', 'aitelier-runtime-event', 'agent-message', 'subagent-settled', 'child'])
    assert.equal(endsDormancy(message(kind)), true, String(kind));
  for (const kind of ['runtime-context', 'compact-checkpoint', stateInjectionSource, 'goal', 'something-new'])
    assert.equal(endsDormancy(message(kind)), false, kind);
  for (const type of ['assistant/message', 'tool/result', 'compaction/end'])
    assert.equal(endsDormancy({ type, data: {} }), false, type);
});
test('dormancy is refused for each kind of live work, in a fixed order, and allowed only with none', () => {
  const claim = { node_key: 'n', status: 'live', driver_id: 'me' }, plan = { attempt_id: 'attempt-x', remaining: ['test'] };
  assert.equal(dormancyRefusal({ reason: '' }).refused, 'reason_required');
  const children = dormancyRefusal({ reason: 'quiet', liveChildren: 2 });
  assert.equal(children.refused, 'live_children'); assert.match(children.reason, /^2 native/);
  assert.deepEqual(dormancyRefusal({ reason: 'quiet', claims: [claim] }), { refused: 'live_claims', reason: 'you hold live State claims', node_keys: ['n'] });
  assert.deepEqual(dormancyRefusal({ reason: 'quiet', plans: [plan] }),
    { refused: 'unfinished_plans', reason: 'finish, report or hand back these plans first', attempts: ['attempt-x'] });
  // A child outranks claims and plans; a claim outranks plans.
  assert.equal(dormancyRefusal({ reason: 'quiet', liveChildren: 1, claims: [claim], plans: [plan] }).refused, 'live_children');
  assert.equal(dormancyRefusal({ reason: 'quiet', claims: [claim], plans: [plan] }).refused, 'live_claims');
  assert.equal(dormancyRefusal({ reason: 'quiet', liveChildren: 0, claims: [], plans: [] }), null);
});
const toolCall = () => ({ type: 'tool/call', data: {} });
const said = text => ({ type: 'assistant/message', data: { message: { content: [{ type: 'reasoning', text: 'NOTHING_LEFT in reasoning does not count' }, { type: 'text', text }] } } });
const input = kind => ({ type: 'user/message', data: { content: [], source: { kind } } });
test('continuation: a turn that called a tool is asked once more; no tool or NOTHING_LEFT is the second stop', () => {
  const c = new Continuation();
  c.observe(input('aitelier-runtime-event')); c.observe(toolCall()); c.observe(said('done one step'));
  assert.equal(c.turnEnded(), true, 'stopped after work: ask');
  c.observe(input(continueSource)); c.observe(said('nothing more'));
  assert.equal(c.turnEnded(), false, 'answered without a tool call: idle');
  c.observe(input('aitelier-runtime-event')); c.observe(toolCall()); c.observe(said(NOTHING_LEFT + ': all blocked on the owner'));
  assert.equal(c.turnEnded(), false, 'NOTHING_LEFT after checking: idle');
  // Only the visible text counts, not reasoning.
  c.observe(toolCall()); c.observe(said('checked'));
  assert.equal(c.turnEnded(), true);
  // A fresh turn record each time: the counters do not leak into the next turn.
  assert.equal(c.turnEnded(), false);
});
test('continuation is bounded: at most MAX_AUTO_CONTINUE questions until real input, and its own question is not input', () => {
  assert.equal(MAX_AUTO_CONTINUE, 5);
  const c = new Continuation();
  const asked = [];
  for (let i = 0; i < 8; i++) { c.observe(input(continueSource)); c.observe(toolCall()); c.observe(said('polled')); asked.push(c.turnEnded()); }
  assert.deepEqual(asked, [true, true, true, true, true, false, false, false]);
  c.observe(input('user')); c.observe(toolCall()); c.observe(said('owner asked'));
  assert.equal(c.turnEnded(), true, 'real input restores the budget');
});
test('continuation while waiting: one NOTHING_LEFT holds for the wait, a changed wait is asked again', () => {
  let key = 'child-a';
  const c = new Continuation({ waitKey: () => key });
  const turn = text => { c.observe(input('aitelier-runtime-event')); c.observe(toolCall()); c.observe(said(text)); return c.turnEnded(); };
  assert.equal(turn('dispatched a worker'), true, 'a fresh wait is asked once');
  assert.equal(turn(NOTHING_LEFT + ': the worker owns the only free file'), false);
  assert.equal(turn('checked the worker'), false, 'same wait, already answered: not asked');
  assert.equal(turn('checked again'), false);
  key = 'child-a,pid:7';
  assert.equal(turn('started a job too'), true, 'the wait changed: asked again');
  assert.equal(turn(NOTHING_LEFT + ': waiting'), false);
  assert.equal(turn('looked'), false);
  key = '';
  assert.equal(turn('the job ended, collected it'), true, 'no wait: the ordinary rule');
  assert.equal(turn(NOTHING_LEFT + ': done'), false);
  assert.equal(turn('a new issue came in, triaged it'), true, 'outside a wait an earlier NOTHING_LEFT does not silence the check');
  key = 'child-b';
  assert.equal(turn('dispatched the next worker'), true);
});
test('own echo: a tick of only this driver\'s writes is not input; any other author, no author or inbox is', () => {
  const me = 'dsh-x', ev = payload => ({ event_type: 'e', payload });
  const own = ev({ actor: 'driver:dsh-x' }), claim = ev({ actor: 'driver:dsh-x', driver_id: 'dsh-x' });
  const foreign = ev({ actor: 'driver:grok' }), authorless = ev({});
  // The acker's own ack/resolve: core/director_messaging.py stamps the actor it
  // acts as next to the acker's driver_id, so it is own like any other own write.
  const acked = ev({ delivery_id: 'd-1', driver_id: 'dsh-x', actor: 'driver:dsh-x', acks: [] });
  // The shape core/state_claims.py emits for an expired lease: no actor at all,
  // the holder in driver_id. The holder must be woken by it, so it is not own.
  const leaseExpired = ev({ subject: 'node:alpha/B', lease_id: 'lease-1', driver_id: 'dsh-x', phase: 'expired', fence: 4 });
  // An acked event that names me in driver_id but carries no actor (a foreign or
  // older payload): the identity is not stated, so it is input, not an echo.
  const ackedNoActor = ev({ delivery_id: 'd-1', driver_id: 'dsh-x', acks: [] });
  const table = [
    [{ events: [own, claim] }, true, 'all own'],
    [{ events: [acked] }, true, 'my own ack/resolve (actor stamped at the emitter) is an echo'],
    [{ events: [acked, own] }, true, 'own acks mixed with own writes is an echo'],
    [{ events: [ackedNoActor] }, false, 'an acked event naming me with no actor is input'],
    [{ events: [acked, ackedNoActor] }, false, 'one actorless ack in the tick makes it input'],
    [{ events: [leaseExpired] }, false, 'an authorless lease_expired naming me is not my own write'],
    [{ events: [own, leaseExpired] }, false, 'one authorless event in the tick makes it input'],
    [{ events: [ev({ actor: null, driver_id: 'dsh-x' })] }, false, 'an authorless event is not my own write'],
    [{ events: [ev({ driver_id: 'dsh-x' })] }, false, 'driver_id alone never makes an event own'],
    [{ events: [own, foreign] }, false, 'one foreign event makes it input'],
    [{ events: [own, authorless] }, false, 'an event naming no author is not own'],
    [{ events: [{ event_type: 'e' }] }, false, 'no payload at all is not own'],
    [{ events: [ev({ actor: 'driver:grok', driver_id: 'dsh-x' })] }, false, 'actor decides over driver_id'],
    [{ events: [ev({ actor: 'driver:dsh-x-2' })] }, false, 'a prefix is another driver'],
    [{ events: [own], driver_inbox: [{ seq: 1 }] }, false, 'driver inbox is input'],
    [{ events: [own], project_inbox: { items: [{ id: 1 }] } }, false, 'project inbox is input'],
    [{ events: [] }, false, 'nothing is not an echo (eventCarriesSomething handles it)'],
  ];
  for (const [result, expected, why] of table) assert.equal(ownEchoOnly(result, me), expected, why);
  assert.equal(ownEchoOnly({ events: [leaseExpired] }, 'dsh-y'), false, 'someone else\'s lease expiry is input too');
  assert.equal(ownEchoOnly({ events: [own] }, undefined), false, 'no identity: never filter');
  assert.equal(ownEchoOnly({ events: [own] }, ''), false);
});
test('idle review: continuation asks instead of arming the review; dormancy, inactive goals and failures do not ask', async t => {
  t.mock.timers.enable({apis:['setTimeout']});
  const agent = { status: 'idle' }, goal = { id: 'g', phase: 'active', activation: 'disarmed' };
  let asks = 0, reviews = 0, ok = true;
  const continuation = new Continuation();
  const review = new IdleReview({ agent, goals: { get: () => goal, disarm: () => {} }, intervalMs: 100, continuation,
    canContinue: () => ok, ask: () => { asks++; }, notify: () => { reviews++; }, onError: error => { throw error; } });
  continuation.observe(toolCall()); review.statusChanged('idle'); await Promise.resolve();
  t.mock.timers.tick(1000); await Promise.resolve();
  assert.equal(asks, 1); assert.equal(reviews, 0, 'asked, so no review timer');
  review.statusChanged('running'); review.statusChanged('idle'); t.mock.timers.tick(100); await Promise.resolve();
  assert.equal(asks, 1); assert.equal(reviews, 1, 'empty answer: the normal review');
  ok = false; continuation.observe(toolCall()); review.statusChanged('idle'); t.mock.timers.tick(100); await Promise.resolve();
  assert.equal(asks, 1, 'a failed completion is not asked again'); assert.equal(reviews, 2);
  ok = true; review.enterDormancy('quiet'); continuation.observe(toolCall()); review.statusChanged('idle'); await Promise.resolve();
  t.mock.timers.tick(1000); await Promise.resolve();
  assert.equal(asks, 1, 'dormant: never asked'); assert.equal(reviews, 2);
  review.wake(); goal.phase = 'paused'; continuation.observe(toolCall()); review.statusChanged('idle'); await Promise.resolve();
  assert.equal(asks, 1, 'stopped goal: never asked');
  review.close();
});
test('background jobs are found by this driver session in the environment; zombies and the driver itself are not jobs', async () => {
  const root = await mkdtemp(join(tmpdir(), 'fake-proc-'));
  const proc = async (pid, session, state = 'S') => {
    await mkdir(join(root, String(pid)));
    await writeFile(join(root, String(pid), 'environ'), ['PATH=/bin', ...(session ? ['AITELIER_DSH_SESSION=' + session] : []), 'X=1'].join('\0') + '\0');
    await writeFile(join(root, String(pid), 'stat'), `${pid} (sh (weird) name) ${state} 1 2 3`);
  };
  try {
    await proc(10, 'root-a'); await proc(11, 'root-a'); await proc(12, 'root-b'); await proc(13, undefined);
    await proc(14, 'root-a', 'Z'); await proc(15, 'root-a'); await mkdir(join(root, 'self'));
    assert.equal(processSession(10, root), 'root-a'); assert.equal(processSession(13, root), undefined); assert.equal(processSession(99, root), undefined);
    assert.equal(processAlive(10, root), true); assert.equal(processAlive(14, root), false, 'zombie'); assert.equal(processAlive(99, root), false);
    assert.deepEqual(backgroundProcesses('root-a', 15, root), [10, 11], 'not the driver itself, not a zombie, not another session');
    assert.deepEqual(backgroundProcesses(undefined, 15, root), []);
    assert.deepEqual(backgroundProcesses('root-a', 15, join(root, 'missing')), []);
  } finally { await rm(root, { recursive: true, force: true }); }
});
test('dormancy refuses unwatched background jobs last, after every other kind of live work', () => {
  assert.deepEqual(dormancyRefusal({ reason: 'quiet', background: [41, 42] }),
    { refused: 'background_processes', reason: 'jobs you started are still running; watch_process them or wait', pids: [41, 42] });
  assert.equal(dormancyRefusal({ reason: 'quiet', plans: [{ attempt_id: 'a' }], background: [41] }).refused, 'unfinished_plans');
  assert.equal(dormancyRefusal({ reason: 'quiet', background: [] }), null);
});
test('watch_process refuses what it cannot or must not watch: bad args, a dead pid, another driver\'s job, a 17th watch', () => {
  const ok = { pid: 4242, note: 'pytest', alive: true, session: 'me', mySession: 'me', watched: false, count: 0 };
  assert.equal(watchRefusal(ok), null);
  for (const bad of [{ pid: 1 }, { pid: 0 }, { pid: 1.5 }, { pid: NaN }, { note: '' }])
    assert.equal(watchRefusal({ ...ok, ...bad }).refused, 'invalid_arguments', JSON.stringify(bad));
  assert.deepEqual(watchRefusal({ ...ok, alive: false }), { refused: 'not_running', reason: 'process 4242 is not running' });
  assert.deepEqual(watchRefusal({ ...ok, session: 'someone-else' }), { refused: 'not_this_driver', reason: 'process 4242 was not started from this driver' });
  assert.deepEqual(watchRefusal({ ...ok, session: undefined }).refused, 'not_this_driver', 'no session in the environment is not ours');
  assert.equal(watchRefusal({ ...ok, count: 15 }), null, 'the 16th watch is allowed');
  assert.deepEqual(watchRefusal({ ...ok, count: 16 }), { refused: 'too_many_watches', reason: 'at most 16 watched jobs' });
  assert.equal(watchRefusal({ ...ok, count: 16, watched: true }), null, 're-watching a watched pid is not a new watch');
  assert.equal(watchRefusal({ ...ok, alive: false, session: 'someone-else' }).refused, 'not_running', 'order: running first');
});
test('prompts ask for concrete candidates and a NOTHING_LEFT line, and name the tools', async () => {
  for (const text of [continuationQuestion, reviewInstruction]) {
    assert.match(text, /NOTHING_LEFT:/); assert.match(text, /blocked/); assert.match(text, /open issues/);
  }
  assert.match(continuationQuestion, /ready frontier nodes/); assert.match(continuationQuestion, /promised/); assert.match(continuationQuestion, /watch_process/);
  assert.match(reviewInstruction, /idle_dormant/);
  for (const text of [continuationQuestion, reviewInstruction])
    for (const candidate of [/ready/, /open issues/, /next steps you promised yourself/, /results you can collect/]) assert.match(text, candidate);
  const guide = await readFile(new URL('./driver.md', import.meta.url), 'utf8');
  for (const word of ['continuation check', 'NOTHING_LEFT:', 'watch_process', 'idle_dormant']) assert.ok(guide.includes(word), word);
});
test('a failing dormancy compaction is reported, never thrown, and dormancy holds', async t => {
  t.mock.timers.enable({apis:['setTimeout']});
  const errors=[],agent={status:'idle'},goal={id:'g',phase:'active',activation:'disarmed'};
  const review=new IdleReview({agent,goals:{get:()=>goal,disarm:()=>{}},intervalMs:100,notify:()=>assert.fail('reviewed'),
    compact:async()=>{throw new Error('busy');},onError:error=>errors.push(String(error))});
  review.enterDormancy('quiet');review.statusChanged('idle');
  for(let i=0;i<5;i++) await Promise.resolve();
  assert.deepEqual(errors,['Error: busy']);
  t.mock.timers.tick(1000);await Promise.resolve();
  assert.ok(review.dormant,'a compaction failure does not end dormancy');
  review.close();
});
test('idle review respects explicit stopped and cleared goal states', async t => {
  t.mock.timers.enable({apis:['setTimeout']});
  let reviews=0;
  for(const phase of ['paused','blocked','complete',undefined]){
    const review=new IdleReview({agent:{status:'idle'},goals:{get:()=>phase?{phase}:undefined,disarm:()=>{}},
      intervalMs:10,notify:()=>{reviews++;},onError:error=>{throw error;}});
    review.statusChanged('idle');t.mock.timers.tick(100);await Promise.resolve();review.close();
  }
  assert.equal(reviews,0);
});
test('subscription reconnects without a model turn and never retries a revoked token', async () => fixture(async directory => {
  let waits = 0, failures = 0, delivered = 0, restored = 0;
  const r = new DriverRuntime({ project:'alpha', driver:'driver-a', directory, retryDelayMs:1,
    request: async path => {
      if (path.endsWith('/heartbeat')) return {};
      if (++waits === 1) throw new TypeError('network lost');
      return {timed_out:false,next_after:12,next_inbox_after:3,events:[{event_type:'node_changed'}]};
    }, onConnectionError: async () => {failures++;}, onConnectionRestored: async () => {restored++;}, onEvent: async () => {delivered++;r.close();} });
  await r.pump();
  assert.equal(failures,1);assert.equal(restored,1);assert.equal(delivered,1);assert.equal(r.after,12);
  const denied = new DriverRuntime({project:'alpha',directory,request:async()=>{
    const error = new Error('revoked');error.status=403;throw error;
  }});
  await assert.rejects(denied.pump(), /revoked/);
}));
test('successful empty subscription response clears a transport error without waking the model', async () => fixture(async directory => {
  let calls = 0, delivered = 0;
  const status = [];
  const r = new DriverRuntime({directory, project:'alpha', retryDelayMs:1,
    request: async path => {
      if (path.endsWith('/heartbeat')) return {};
      if (++calls === 1) throw new TypeError('fetch failed', {cause:{code:'UND_ERR_HEADERS_TIMEOUT', message:'Headers Timeout Error'}});
      if (calls === 2) return {timed_out:true,next_after:12,next_inbox_after:3};
      r.close(); throw new Error('done');
    }, onConnectionError: error => {status.push(connectionErrorMessage(error));},
    onConnectionRestored: () => {status.push('');}, onEvent: () => {delivered++;} });
  await assert.rejects(r.pump(), /done/);
  assert.deepEqual(status, ['AItelier event subscription: fetch failed (UND_ERR_HEADERS_TIMEOUT: Headers Timeout Error)', '']);
  assert.equal(delivered,0); assert.equal(r.after,12); assert.equal(r.inboxAfter,3);
  assert.equal(r.connectionLost,false);
}));
test('history grep includes precompact events, exact citations, regex and pagination', () => {
  const events = [
    { seq: 0, type: 'user/message', data: { message: { content: [{ type: 'text', text: 'OLD decision 42' }] } } },
    { seq: 1, type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'decision 43' }] } } },
    { seq: 2, type: 'surface/replace', data: { shadowedSeqs: [0, 1] } },
    { seq: 3, type: 'user/message', data: { message: { content: [{ type: 'text', text: 'new turn' }] } } },
  ];
  const first = grepHistory(events, 'decision', { limit: 1 });
  assert.equal(first.matches[0].seq, 0); assert.equal(first.has_more, true);
  assert.match(first.matches[0].matches[0].text, /OLD decision 42/);
  assert.ok(first.matches[0].matches[0].line > 0);
  const second = grepHistory(events, 'decision', { after_seq: first.next_after_seq });
  assert.deepEqual(second.matches.map(m => m.seq), [1]);
  assert.equal(second.has_more, false);
  assert.equal(grepHistory(events, 'old.*42', { regex: true, ignore_case: true }).matches[0].seq, 0);
  assert.equal(grepHistory(events, 'old.*42').matches.length, 0);
  assert.throws(() => grepHistory(events, '[' , { regex: true }), SyntaxError);
  assert.throws(() => grepHistory(events, '', {}), /empty/);
  const long = grepHistory([{seq:4,type:'user/message',data:{text:'x'.repeat(5000)+'find-me'+'y'.repeat(5000)}}], 'find-me');
  assert.match(long.matches[0].matches[0].text, /find-me/);
  assert.equal(long.matches[0].matches[0].truncated, true);
});

test('project issue notification is delivered without a model wait and retains its durable cursor', async () => fixture(async directory => {
  let reads=0;const delivered=[];
  const issue={seq:12,project_id:'alpha',event_type:'issue_reported',payload:{issue_id:'iss-new',title:'A real defect',kind:'defect'}};
  const runtime=new DriverRuntime({directory,project:'alpha',
    request:async(path,args)=>{
      if(path.endsWith('/heartbeat'))return {};
      assert.equal(args.project_id,'alpha');
      if(++reads===1)return {timed_out:true,events:[],next_after:11,next_inbox_after:0};
      if(reads===2)return {timed_out:false,events:[issue],next_after:12,next_inbox_after:0};
      runtime.close();throw new Error('done');
    },onEvent:async value=>{delivered.push(value);}});
  await assert.rejects(runtime.pump(),/done/);
  assert.equal(delivered.length,1);assert.deepEqual(delivered[0].events,[issue]);
  assert.equal(JSON.parse(await readFile(join(directory,'cursor.json'),'utf8')).after,12);
}));

test('empty/resolved inbox replies advance cursors without model turns', async () => fixture(async directory => {
  let calls = 0, delivered = 0;
  const r = new DriverRuntime({directory, project:'alpha',
    request: async path => {
      if (path.endsWith('/heartbeat')) return {};
      if (++calls === 1) return {timed_out:false,events:[],driver_inbox:[],next_after:11,next_inbox_after:1};
      if (calls === 2) return {timed_out:false,events:[],driver_inbox:[{status:'resolved',body:'old'}],next_after:12,next_inbox_after:2};
      r.close(); throw new Error('done');
    }, onEvent:async()=>{delivered++;} });
  await assert.rejects(r.pump(), /done/);
  assert.equal(delivered,0); assert.equal(r.after,12); assert.equal(r.inboxAfter,2);
}));


// ── A State tick is not the owner speaking ──────────────────────────────────
// Every injected state event used to be labelled `source.kind: 'user'`, so a
// driver could not tell "State changed" from "the owner wrote to me" - and a
// routine tick got mistaken for an unanswered message. The kinds are now
// distinct, and a tick carrying nothing no longer steers at all.
test('a State event is not labelled as the user, and an empty tick does not wake the driver', () => {
  assert.notEqual(runtimeEventSource, 'user');
  assert.notEqual(runtimeEventSource, stateInjectionSource);

  // An empty heartbeat: no events, no driver inbox, no project inbox.
  assert.equal(eventCarriesSomething({ timed_out: true, events: [] }), false);
  assert.equal(eventCarriesSomething({ timed_out: true }), false);
  assert.equal(eventCarriesSomething({}), false);

  // Anything readable is worth the turn.
  assert.equal(eventCarriesSomething({ events: [{ seq: 1 }] }), true);
  assert.equal(eventCarriesSomething({ driver_inbox: [{ message_id: 'm' }] }), true);
  assert.equal(eventCarriesSomething({ project_inbox: { items: [{ message: {} }] } }), true);
});

// ── The director's project inbox injection is bounded and honest ──────────────
// What a `director_message_received` wake injects, and what it must never inject.
//
// Measured on the real page 2026-11-05: 20 items, matched_total 91, has_more true,
// 42,304 characters of project_inbox per wake - and only 24,894 of those were
// bodies. The projection below is the guard: identity for every item, at most a
// few hundred characters of body, the page's own numbers stated, and a ceiling on
// the whole payload. The fixtures are synthetic; no real message text is in this
// repository.

const inboxPage = (items, extra = {}) => ({ schema: 'aitelier.director-messaging.v3',
  result: { project_id: 'aitelier', items, matched_total: items.length, has_more: false, next_after: 0, ...extra } });

const inboxItem = ({ seq, body = '', subject = `Synthetic subject ${seq}`, thread = `thr_${seq}`,
  status = 'unread', acked_by_me = false }) => ({
  message: { message_id: `dm_${String(seq).padStart(4, '0')}`, thread_id: thread, sender_project_id: 'other-project',
    director_identity: 'other-project/dsh-other', actor: 'driver:dsh-other', subject, body,
    created_at: '2026-11-0' + (1 + (seq % 5)) + 'T10:00:00+00:00', reply_to_delivery_id: null,
    delivery_mode: 'transient', sender_driver_id: 'dsh-other', ack_mode: 'at_least_n', ack_quorum: 1 },
  delivery: { delivery_id: `dlv_${String(seq).padStart(4, '0')}`, message_id: `dm_${String(seq).padStart(4, '0')}`,
    target_project_id: 'aitelier', delivery_seq: seq, status, version: 1 },
  ack_mode: 'at_least_n', ack_quorum: 1, acks: [], acked_count: 0, pending_drivers: ['dsh-me'],
  acked_by_me, legacy_ack: false, quorum_met: status === 'acknowledged', ack_quorum_unreachable: false,
});

// 4 KB bodies with a unique tail past the excerpt cap, so "the tail is absent" is a
// real assertion rather than a search for repeated filler.
const fatBody = seq => {
  const head = `Synthetic body ${seq}: `, tail = ` TAIL-${seq}`;
  return head + 'x'.repeat(4096 - head.length - tail.length) + tail;
};

test('twenty four-kilobyte bodies cost one stated, enforced ceiling', () => {
  const items = Array.from({ length: 20 }, (_, i) => inboxItem({ seq: 72 + i, body: fatBody(i + 1) }));
  // What listProjectInbox hands over for a 91-match inbox: it scanned all 91 and
  // kept the newest 20 (delivery_seq 72..91).
  const digest = projectInboxDigest(inboxPage(items, {
    matched_total: 91, has_more: true, next_after: 91, scanned: 91, scan_complete: true }));
  const text = JSON.stringify(digest);

  // The bound, measured: 20 items x 4 KB bodies with full excerpts would be tens of
  // kilobytes (this fixture's raw page is 87,720 characters of project_inbox).
  // Assert the real number, not just "smaller": 10,520 against the 12,000 ceiling.
  assert.ok(text.length <= INBOX_CEILING_CHARS,
    `project_inbox is ${text.length} chars, over its ${INBOX_CEILING_CHARS} ceiling`);

  // The page states itself: a director can tell "I am seeing 20 of 91" from "there are 20".
  assert.equal(digest.page.window, 'newest 20 of 91');
  assert.equal(digest.page.items, 20);
  assert.equal(digest.page.matched_total, 91);
  assert.equal(digest.page.scanned, 91);
  assert.equal(digest.page.scan_complete, true);
  assert.equal(digest.page.has_more, true);
  assert.equal(digest.page.next_after, 91);
  assert.equal(digest.page.items_omitted, 0, 'nothing was dropped at this size, so nothing may be claimed as dropped');
  assert.equal(digest.page.bodies_withheld, 0);
  assert.equal(digest.page.excerpt_chars, INBOX_EXCERPT_CHARS);
  assert.equal(digest.page.ceiling_chars, INBOX_CEILING_CHARS);

  // Everything acknowledge_director_message(project_id, delivery_id,
  // expected_version, request_key) needs is on the payload it was given: without
  // these two the director cannot settle a delivery, and a delivery that is never
  // settled keeps costing context on every later wake.
  assert.equal(digest.page.project_id, 'aitelier');
  for (const [i, entry] of digest.items.entries()) {
    const seq = 91 - i;   // newest first
    assert.equal(entry.message_id, `dm_${String(seq).padStart(4, '0')}`);
    assert.equal(entry.delivery_id, `dlv_${String(seq).padStart(4, '0')}`);
    assert.equal(entry.version, 1);
    assert.equal(entry.subject, `Synthetic subject ${seq}`);
    assert.equal(entry.sender_project_id, 'other-project');
    assert.match(entry.created_at, /^2026-11-0/);
    assert.equal(entry.status, 'unread');
    assert.equal(entry.acked_by_me, false);
    assert.equal(entry.body_chars, 4096);
    assert.equal(entry.body_excerpt.length, INBOX_EXCERPT_CHARS, 'the excerpt is capped, not the body');
    assert.equal(entry.body_excerpt, fatBody(seq - 71).slice(0, INBOX_EXCERPT_CHARS), 'the excerpt is the body\'s own opening');
    assert.equal(entry.excerpt_truncated, true);
  }
  for (let seq = 1; seq <= 20; seq++) assert.ok(!text.includes(`TAIL-${seq}`), `body text past the cap leaked for ${seq}`);

  // The event path still sees an injection: these two functions decide whether a
  // tick is worth a turn, and the projection must not turn input into "nothing".
  assert.equal(eventCarriesSomething({ project_inbox: digest }), true);
  assert.equal(ownEchoOnly({ events: [{ payload: { actor: 'driver:dsh-me' } }], project_inbox: digest }, 'dsh-me'), false);
});

test('an acknowledged delivery carries identity only and a superseded one a short excerpt', () => {
  const settled = inboxItem({ seq: 1, body: 'S'.repeat(2000), status: 'acknowledged', acked_by_me: true, thread: 'thr_a' });
  // Quorum-acknowledged by another member: settled for the project, not by me.
  const quorum = inboxItem({ seq: 2, body: 'Q'.repeat(2000), status: 'acknowledged', acked_by_me: false, thread: 'thr_b' });
  // One thread, three messages: 4 and 5 are superseded by 6, which is on the page too.
  const thread = [4, 5, 6].map(seq => inboxItem({ seq, body: `T${seq}: ` + 'y'.repeat(1000), thread: 'thr_t' }));
  const digest = projectInboxDigest(inboxPage([settled, quorum, ...thread]));
  const text = JSON.stringify(digest);

  const byId = Object.fromEntries(digest.items.map(entry => [entry.message_id, entry]));
  for (const id of ['dm_0001', 'dm_0002']) {
    const entry = byId[id];
    assert.equal(entry.body_excerpt, undefined, `acknowledged ${id} must not re-inject a body`);
    assert.equal(entry.body_withheld, 'acknowledged');
    assert.equal(entry.body_chars, 2000);
    assert.equal(entry.status, 'acknowledged');
  }
  assert.equal(byId.dm_0001.acked_by_me, true);
  assert.equal(byId.dm_0002.acked_by_me, false, 'the cheap ack truth still says who settled it');

  for (const id of ['dm_0004', 'dm_0005']) {
    const entry = byId[id];
    assert.ok(entry.body_excerpt.length <= INBOX_SUPERSEDED_EXCERPT_CHARS,
      `${id} is superseded by a later message on the page and must not arrive at the full excerpt`);
    assert.equal(entry.superseded_by, 'dm_0006');
    assert.equal(entry.thread_id, 'thr_t');
  }
  assert.equal(byId.dm_0006.body_excerpt.length, INBOX_EXCERPT_CHARS);
  assert.equal(byId.dm_0006.superseded_by, undefined);
  assert.ok(text.length <= INBOX_CEILING_CHARS);

  // Everything thinned out is still counted, and nothing vanished.
  assert.equal(digest.page.items, 5);
  assert.equal(digest.page.matched_total, 5);
  assert.equal(digest.page.items_omitted, 0);
  assert.equal(digest.page.bodies_withheld, 2);
});

test('when the ceiling cannot be met by withholding bodies, the oldest items are omitted and counted', () => {
  const items = Array.from({ length: 20 }, (_, i) => inboxItem({ seq: i + 1, body: fatBody(i + 1), subject: 's'.repeat(160) }));
  const digest = projectInboxDigest(inboxPage(items, { matched_total: 91, has_more: true }), { ceiling: 4000 });
  const text = JSON.stringify(digest);

  assert.ok(text.length <= 4000, `project_inbox is ${text.length} chars against a 4000 ceiling`);
  assert.ok(digest.page.items < 20, 'this ceiling cannot carry twenty identity lines');
  assert.equal(digest.page.items + digest.page.items_omitted, 20, 'an omitted item is counted, never dropped');
  assert.equal(digest.page.matched_total, 91, 'omission never rewrites what matched');
  assert.equal(digest.page.excerpt_chars, 0);
  assert.equal(digest.items.filter(entry => entry.body_excerpt).length, 0);
  for (const entry of digest.items) {
    assert.ok(entry.message_id && entry.subject && entry.sender_project_id && entry.created_at);
    assert.equal(entry.body_withheld, 'ceiling');
  }
  // Newest survive: the omitted tail is the oldest end of the page.
  assert.equal(digest.items[0].message_id, 'dm_0020');
});

test('an injected item carries everything acknowledging its delivery needs', () => {
  // acknowledge_director_message(project_id, delivery_id, expected_version, request_key):
  // a director must be able to settle a delivery from the payload it was handed.
  // Acknowledging is what stops a delivery costing context on every later wake, so
  // these two fields are part of the bound, not decoration - do not trim them.
  const digest = projectInboxDigest(inboxPage([inboxItem({ seq: 7, body: 'x'.repeat(1000) })]));
  const [entry] = digest.items;

  assert.equal(digest.page.project_id, 'aitelier');
  assert.equal(entry.delivery_id, 'dlv_0007');
  assert.ok(Number.isInteger(entry.version), 'version is the expected_version the call takes');
  assert.equal(entry.version, 1);
  assert.equal(entry.message_id, 'dm_0007');
  // A settled (already acknowledged) item keeps them too: it still has to be resolvable.
  const settled = projectInboxDigest(inboxPage([inboxItem({ seq: 8, status: 'acknowledged', acked_by_me: true })]));
  assert.equal(settled.items[0].delivery_id, 'dlv_0008');
  assert.equal(settled.items[0].version, 1);
});

// The API pages by delivery_seq ascending and has no newest-first option, so the
// page a `limit: 20` request returned was the OLDEST 20 of 91 - a message that
// arrives today sits on page five and would never be injected. The collector walks
// the cursor to the end of the matches and keeps the newest page.
const inboxServer = store => {
  const asked = [];
  const request = async (_path, body) => {
    const after = body.after ?? 0;
    asked.push(after);
    const matches = store.filter(entry => entry.delivery.delivery_seq > after);
    const batch = matches.slice(0, body.limit);
    return { result: { project_id: 'alpha', items: batch, matched_total: store.length,
      has_more: matches.length > batch.length,
      next_after: batch.at(-1)?.delivery.delivery_seq ?? after } };
  };
  return { request, asked };
};

test('the message that just arrived is on the injected page, wherever the cursor put it', async () => {
  const store = Array.from({ length: 40 }, (_, i) => inboxItem({ seq: i + 1, body: fatBody(i + 1) }));
  const { request, asked } = inboxServer(store);
  const digest = projectInboxDigest(await listProjectInbox(request, 'alpha', { limit: 10 }));

  assert.deepEqual(asked, [0, 10, 20, 30], 'the collector followed next_after to the end of the matches');
  assert.equal(digest.page.window, 'newest 20 of 40');
  assert.equal(digest.page.scanned, 40);
  assert.equal(digest.page.scan_complete, true);
  assert.equal(digest.items.length, 20);
  assert.equal(digest.items[0].delivery_id, 'dlv_0040', 'the newest delivery is the first thing read');
  assert.equal(digest.items[0].message_id, 'dm_0040');
  assert.equal(digest.items[0].body_excerpt, fatBody(40).slice(0, INBOX_EXCERPT_CHARS));
  assert.equal(digest.items[0].excerpt_truncated, true);
  assert.ok(!digest.items.some(entry => entry.message_id === 'dm_0001'),
    'returning the first page as the API served it is the bug: the oldest 20 are not the page');
});

test('a scan that cannot reach the end of the matches says so instead of guessing the window', async () => {
  const store = Array.from({ length: 600 }, (_, i) => inboxItem({ seq: i + 1 }));
  const { request, asked } = inboxServer(store);
  const digest = projectInboxDigest(await listProjectInbox(request, 'alpha', { pages: 1 }));

  assert.deepEqual(asked, [0], 'one page of the scan, exactly as configured');
  assert.equal(digest.page.matched_total, 600);
  assert.equal(digest.page.scanned, 100);
  assert.equal(digest.page.scan_complete, false, 'a truncated scan is not the whole match set');
  assert.equal(digest.page.window, 'newest 20 of 600 (scanned 100)');
  assert.equal(digest.page.has_more, true);
  assert.equal(digest.items[0].delivery_id, 'dlv_0100', 'newest of what the scan could see');
});
