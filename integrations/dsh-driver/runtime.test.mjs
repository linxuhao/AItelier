import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DriverRuntime, IdleReview, endsDormancy, StateInjection, stateInjectionSource, runtimeEventSource, eventCarriesSomething, grepHistory, connectionErrorMessage, driverGoal } from './runtime.mjs';
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
