import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DriverRuntime, grepHistory } from './runtime.mjs';

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
  const r = new DriverRuntime({ directory });
  const waiting = r.wait();
  r.wake({ reason: 'subagent_completed', id: 'child' });
  assert.deepEqual(await waiting, [{ reason: 'subagent_completed', id: 'child' }]);
  const stop = new AbortController();
  const cancelled = r.wait(stop.signal); stop.abort(new Error('cancelled'));
  await assert.rejects(cancelled, /cancelled/);
  assert.equal(r.waiters.size, 0);
  const closing = r.wait(); r.close(); await assert.rejects(closing, /stopped/);
}));
test('subscription reconnects without a model turn and never retries a revoked token', async () => fixture(async directory => {
  let waits = 0, failures = 0, delivered = 0;
  const r = new DriverRuntime({ project:'alpha', driver:'driver-a', directory, retryDelayMs:1,
    request: async path => {
      if (path.endsWith('/heartbeat')) return {};
      if (++waits === 1) throw new TypeError('network lost');
      return {timed_out:false,next_after:12,next_inbox_after:3};
    }, onConnectionError: async () => {failures++;}, onEvent: async () => {delivered++;r.close();} });
  await r.pump();
  assert.equal(failures,1);assert.equal(delivered,1);assert.equal(r.after,12);
  const denied = new DriverRuntime({project:'alpha',directory,request:async()=>{
    const error = new Error('revoked');error.status=403;throw error;
  }});
  await assert.rejects(denied.pump(), /revoked/);
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
