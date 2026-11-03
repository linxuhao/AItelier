import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DriverRuntime, StateInjection, stateInjectionSource, grepHistory, connectionErrorMessage } from './runtime.mjs';

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
  assert.equal(rendered.driver_skill, 'driver skill');
  assert.match(rendered.driver_goal, /你是 alpha 项目 State DAG 的常驻导演/);
  assert.match(rendered.driver_goal, /critical issues/);
  assert.match(rendered.driver_goal, /Claude Sonnet 5\.5 和 GPT-6\.1 Sol/);
  assert.match(rendered.driver_goal, /Claude Opus 5\.5/);
  assert.doesNotMatch(rendered.driver_goal, /\{\{project_id\}\}/);
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
test('wait wakes on native child messages and is cancellation safe', async () => fixture(async directory => {
  let pending = [];
  const r = new DriverRuntime({ directory, pendingInputs: () => pending });
  const waiting = r.wait();
  pending = [{ reason: 'native_input_pending', message_id: 'child-message' }]; r.wake();
  assert.deepEqual(await waiting, pending);
  pending = [];
  const stop = new AbortController();
  const cancelled = r.wait(stop.signal); stop.abort(new Error('cancelled'));
  await assert.rejects(cancelled, /cancelled/);
  assert.equal(r.waiters.size, 0);
  const closing = r.wait(); r.close(); await assert.rejects(closing, /stopped/);
}));
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

test('old notifications and consumed inputs never become future wait credits', async () => fixture(async directory => {
  let pending = [];
  const r = new DriverRuntime({ directory, pendingInputs: () => pending });
  r.wake(); // Bootstrap input was already claimed before the model called wait.
  pending = [{reason:'native_input_pending', message_id:'state-1'}]; r.wake();
  pending = []; // Native pre-step claimed the State update during active work.
  const abort = new AbortController();
  let returned = false;
  const waiting = r.wait(abort.signal).then(() => { returned = true; });
  r.wake(); // An empty notification while blocked must recheck and stay blocked.
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.equal(returned, false);
  pending = [{reason:'native_input_pending', message_id:'real-new-input'}]; r.wake();
  await waiting; assert.equal(returned, true);
  pending = [];
  const blocked = r.wait(abort.signal); abort.abort(new Error('end fixture'));
  await assert.rejects(blocked, /end fixture/);
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
