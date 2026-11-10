import { test } from 'node:test';
import assert from 'node:assert/strict';
import { LiveChildren, Continuation, NOTHING_LEFT, continuationWaitKey, dormancyRefusal, phaseReport,
  rebindRefusal, todoSummary, unfinishedWithoutChild } from './runtime.mjs';

// A child that ends WITHOUT the `subagent/end` edge - a host restart, a killed
// worker - is the defect these assertions are about (iss-5f2c09b74cb040ee,
// iss-de1b4e7f644f4f0e). The real DSH fixture cannot express it: the SDK emits
// `subagent/start` and `subagent/end` as a pair from one activation observer, so
// losing only the end edge is unreachable there. What IS reachable is the rule the
// runtime now follows: every reader consults the agent registry, and the registry
// answering absent IS the child being departed.
//
// So the oracle is injected here, exactly as the runtime injects `ctx.agents.get`:
// `agents.get(id)` answers with the held agent or `undefined`, the same call shape
// the idle_dormant path uses. Everything below then executes the runtime's own
// seams - LiveChildren, dormancyRefusal, Continuation, continuationWaitKey,
// todoSummary, unfinishedWithoutChild, rebindRefusal, phaseReport - rather than
// restating them.
function registry(ids = []) {
  const held = new Set(ids);
  return { forget: id => held.delete(id), agents: { get: id => held.has(id) ? { session: { id } } : undefined } };
}

const plan = (attempt_id, child_session) => ({ attempt_id, node_key: 'n', next: 'test',
  remaining: ['test'], child_session });
const dormancy = children => dormancyRefusal({ reason: 'nothing authorized is available',
  liveChildren: children.size(), childSessions: children.ids() });

test('a child that ends without the subagent/end edge stops refusing dormancy, stops keying the '
  + 'continuation wait, stops holding its plan and stops appearing in the phase report', () => {
  const reg = registry(['worker-0', 'worker-1']);
  const children = new LiveChildren({ isLive: id => reg.agents.get(id) !== undefined });
  children.start({ id: 'worker-0', runId: 'run-0', local: true });
  children.start({ id: 'worker-1', runId: 'run-1', local: true });
  // The plan whose child never sent its end edge, and the one whose child is still live.
  const orphan = plan('attempt-orphan', 'worker-1'), attended = plan('attempt-attended', 'worker-0');

  // While the registry holds both, every reader sees both: this is the half that must
  // not weaken. worker-0 stays live for the rest of this test (point e).
  assert.deepEqual(dormancy(children).live_children, ['worker-0', 'worker-1']);
  assert.equal(dormancy(children).reason, '2 native child(ren) still running');
  assert.deepEqual(todoSummary([orphan, attended], children.sessionSet()).map(p => p.child_alive), [true, true]);
  assert.deepEqual(unfinishedWithoutChild([orphan, attended], children.sessionSet()), []);
  assert.deepEqual(phaseReport({ phase: 'running', children }).subagents, ['worker-0', 'worker-1']);

  // The continuation answer that named the child is held for that key...
  const continuation = new Continuation({ waitKey: () => continuationWaitKey({ children }) });
  const heldKey = continuationWaitKey({ children });
  const turn = text => {
    continuation.observe({ type: 'tool/call' });
    continuation.observe({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text }] } } });
    return continuation.turnEnded();
  };
  assert.equal(turn(NOTHING_LEFT + ': waiting on the children'), false);
  assert.equal(turn('took another look, nothing new'), false, 'the same wait key holds one answer');

  // worker-1 ends with no end edge: its registry entry is gone, nothing else changed.
  reg.forget('worker-1');

  // (a) Dormancy: the departed child is no longer named and no longer counted.
  const refusal = dormancy(children);
  assert.equal(refusal.refused, 'live_children');
  assert.deepEqual(refusal.live_children, ['worker-0'], 'a departed child must not be named');
  assert.equal(refusal.reason, '1 native child(ren) still running', 'a departed child must not be counted');
  // And with nothing left to name, dormancy is not refused at all.
  const solo = registry(['gone']);
  const only = new LiveChildren({ isLive: id => solo.agents.get(id) !== undefined });
  only.start({ id: 'gone', runId: 'run-gone', local: true });
  solo.forget('gone');
  assert.equal(dormancy(only), null, 'a departed child must not refuse dormancy');

  // (b) The continuation waitKey changed: the answer that named the child no longer holds.
  const afterKey = continuationWaitKey({ children });
  assert.notEqual(afterKey, heldKey);
  assert.equal(afterKey, 'worker-0', 'the key names live sessions only');
  assert.equal(turn('took another look, nothing new'), true, 'a changed wait key asks the director again');

  // (c) A plan bound to it is no longer reported as bound to a live session.
  assert.equal(todoSummary([orphan], children.sessionSet())[0].child_alive, false);
  assert.deepEqual(unfinishedWithoutChild([orphan], children.sessionSet()).map(p => p.attempt_id),
    ['attempt-orphan'], 'the stranded plan is handed back as work, not hidden behind a dead child');

  // (d) The phase report no longer lists it - and never listed a runId to begin with.
  const report = phaseReport({ phase: 'running', children });
  assert.deepEqual(report.subagents, ['worker-0'], 'the phase report must name live sessions only');
  assert.ok(!report.subagents.includes('run-0') && !report.subagents.includes('run-1'),
    'the phase report names session ids, never runIds');
  assert.deepEqual(Object.keys(report).sort(), ['error', 'phase', 'subagents'],
    'the report body carries no extra key: Activity forbids extras, so a second identity needs its own contract');

  // (e) A child whose registry entry still answers live is still refused and still
  // blocks: worker-0 was never dropped by any read above.
  assert.ok(children.has('worker-0'));
  assert.deepEqual(children.ids(), ['worker-0']);
  assert.equal(dormancy(children).refused, 'live_children');
  assert.deepEqual(dormancy(children).live_children, ['worker-0']);
  assert.equal(todoSummary([attended], children.sessionSet())[0].child_alive, true);
  assert.deepEqual(unfinishedWithoutChild([attended], children.sessionSet()), [],
    'a live child keeps its plan');
  assert.equal(rebindRefusal({ attempt_id: 'attempt-attended', item_id: 'test', session_id: 'worker-0',
    bound_session: 'worker-0', live: children.ids(), root_session: 'root' }).refused, 'live_session',
  'a live child still owns its plan');
  // Reconciling is idempotent and never re-adds: repeated reads stay at one child.
  assert.deepEqual(children.reconcile(), []);
  assert.deepEqual(children.reconcile(), []);
  assert.deepEqual(children.ids(), ['worker-0']);

  // The runId pairing follows its session out: the second identity cannot leak either.
  assert.deepEqual([...children.runs.keys()], ['run-0']);

  // The takeover that used to be refused is available now: worker-1 is provably dead.
  assert.equal(rebindRefusal({ attempt_id: 'attempt-orphan', item_id: 'test', session_id: 'worker-1',
    bound_session: 'worker-1', live: children.ids(), root_session: 'root' }), null);
});

test('a child the registry cannot be asked about is kept: no proof exists that it departed', () => {
  const reg = registry([]);
  const children = new LiveChildren({ isLive: id => reg.agents.get(id) !== undefined });
  // `local: false`: its start event says this process owns no Agent for it, so its
  // absence from the registry is not evidence of anything.
  children.start({ id: 'remote', runId: 'run-remote', local: false });
  assert.deepEqual(children.reconcile(), []);
  assert.deepEqual(children.ids(), ['remote']);
  assert.equal(dormancy(children).refused, 'live_children');
  // The rule is per activation, not global: a local child in the same set is dropped.
  children.start({ id: 'local', runId: 'run-local', local: true });
  assert.deepEqual(children.reconcile(), ['local']);
  assert.deepEqual(children.ids(), ['remote']);
  // A caller with no oracle at all proves nothing: the class default answers live,
  // which is the same rule the runtime applies when ctx.agents has no registry.
  const blind = new LiveChildren();
  blind.start({ id: 'unverifiable', runId: 'run-u', local: true });
  assert.deepEqual(blind.reconcile(), []);
  assert.deepEqual(blind.ids(), ['unverifiable']);
});
