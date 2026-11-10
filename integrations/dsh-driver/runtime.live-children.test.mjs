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
//
// EVERY reader gets its OWN world, and that is the point of the shape below.
// `reconcile()` is destructive and shared: the first reconciling read after a child
// departs prunes it out of `children.sessions` for good. One shared set therefore
// makes every reader after the first look correct whether it reconciles or not - a
// reader that reads the raw map instead is handed an already-pruned map and passes.
// That is exactly how an earlier version of this file stayed GREEN while
// `phaseReport` and `continuationWaitKey` bypassed the reconciliation (delivered at
// 97572ca1; a reviewer's mutations B and C). `twoChildren()` builds a fresh
// LiveChildren + agent registry per section, and each section's FIRST read after
// `depart()` is the reader that section witnesses - remove that reader's own
// reconciliation and its section, and only its section, goes red.
//
// So there are five readers and one section each: the dormancy refusal (the count and
// the names), the continuation wait key, the plan binding (`sessionSet()`), the phase
// report, and the membership answer `has()` - the reader `todo_begin` asks before it
// treats a plan bound to another session as attended.
function registry(ids = []) {
  const held = new Set(ids);
  return { forget: id => held.delete(id), agents: { get: id => held.has(id) ? { session: { id } } : undefined } };
}

/** One fresh world: worker-0 stays live, worker-1 departs without its end edge.
 *  Nothing is read here, so the set is unpruned when a section gets it. */
function twoChildren() {
  const reg = registry(['worker-0', 'worker-1']);
  const children = new LiveChildren({ isLive: id => reg.agents.get(id) !== undefined });
  children.start({ id: 'worker-0', runId: 'run-0', local: true });
  children.start({ id: 'worker-1', runId: 'run-1', local: true });
  // `depart()` IS the missing `subagent/end`: the registry forgets the child and
  // nothing else changes - no end edge, no explicit removal.
  return { children, depart: () => reg.forget('worker-1') };
}

const plan = (attempt_id, child_session) => ({ attempt_id, node_key: 'n', next: 'test',
  remaining: ['test'], child_session });
const dormancy = children => dormancyRefusal({ reason: 'nothing authorized is available',
  liveChildren: children.size(), childSessions: children.ids() });

// (1) idle_dormant's refusal: the live-child count and the sessions it names.
test('the dormancy refusal counts and names live children only, never a departed one', () => {
  const { children, depart } = twoChildren();
  // The half that must not weaken: while the registry holds both, both are counted
  // and both are named.
  assert.deepEqual(dormancy(children).live_children, ['worker-0', 'worker-1']);
  assert.equal(dormancy(children).reason, '2 native child(ren) still running');

  // worker-1 ends with no end edge. This is this set's FIRST read after that, so the
  // reconciliation under witness is this reader's own.
  depart();
  const refusal = dormancy(children);
  assert.equal(refusal.refused, 'live_children');
  assert.deepEqual(refusal.live_children, ['worker-0'], 'a departed child must not be named');
  assert.equal(refusal.reason, '1 native child(ren) still running', 'a departed child must not be counted');

  // With nothing left to name, dormancy is not refused at all (its own fresh set).
  const solo = registry(['gone']);
  const only = new LiveChildren({ isLive: id => solo.agents.get(id) !== undefined });
  only.start({ id: 'gone', runId: 'run-gone', local: true });
  solo.forget('gone');
  assert.equal(dormancy(only), null, 'a departed child must not refuse dormancy');
});

// (2) The continuation wait key: what holds a NOTHING_LEFT answer and what wakes it.
test('the continuation wait key names live children only, so a departed one wakes the director', () => {
  const { children, depart } = twoChildren();
  const continuation = new Continuation({ waitKey: () => continuationWaitKey({ children }) });
  const heldKey = continuationWaitKey({ children });
  assert.equal(heldKey, 'worker-0,worker-1');
  const turn = text => {
    continuation.observe({ type: 'tool/call' });
    continuation.observe({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text }] } } });
    return continuation.turnEnded();
  };
  assert.equal(turn(NOTHING_LEFT + ': waiting on the children'), false);
  assert.equal(turn('took another look, nothing new'), false, 'the same wait key holds one answer');

  // First read of this set after the departure: the key the continuation is holding.
  // The answer that named the child must no longer hold.
  depart();
  const afterKey = continuationWaitKey({ children });
  assert.notEqual(afterKey, heldKey);
  assert.equal(afterKey, 'worker-0', 'the key names live sessions only');
  assert.equal(turn('took another look, nothing new'), true, 'a changed wait key asks the director again');

  // Watched jobs ride in the same key, after the children - the runtime passes them
  // already sorted (again a fresh set, so the departure is still unpruned when the
  // key is read).
  const next = twoChildren();
  next.depart();
  assert.equal(continuationWaitKey({ children: next.children, watched: [7, 9] }), 'worker-0,pid:7,pid:9');
});

// (3) The plan-binding check: the plan view the model is handed, the work view the
// idle review hands back, and the takeover gate built on them.
test('a plan stays bound to a live session, and a departed child\'s plan is handed back as work', () => {
  const { children, depart } = twoChildren();
  const orphan = plan('attempt-orphan', 'worker-1'), attended = plan('attempt-attended', 'worker-0');

  // While the registry holds both, both plans are attended and neither is handed back.
  assert.deepEqual(todoSummary([orphan, attended], children.sessionSet()).map(p => p.child_alive), [true, true]);
  assert.deepEqual(unfinishedWithoutChild([orphan, attended], children.sessionSet()), []);

  // The plan view the runtime renders is `todoSummary(plans, children.sessionSet())`
  // and the work view idle_dormant refuses on is `unfinishedWithoutChild(plans,
  // children.sessionSet())`. This is that set's first read after the departure.
  depart();
  assert.equal(todoSummary([orphan], children.sessionSet())[0].child_alive, false,
    'a plan must not be reported as bound to a live session it is not bound to');
  assert.deepEqual(unfinishedWithoutChild([orphan], children.sessionSet()).map(p => p.attempt_id),
    ['attempt-orphan'], 'the stranded plan is handed back as work, not hidden behind a dead child');

  // ... and the dormancy gate built on that view refuses it as work to finish or hand
  // back, instead of letting the director go dormant on a plan nobody is running.
  const stranded = unfinishedWithoutChild([orphan], children.sessionSet());
  assert.deepEqual(Object.keys(stranded[0]).sort(),
    ['attempt_id', 'next', 'node_key', 'ready_to_finish', 'remaining'], 'the work view carries no child_alive claim');
  const refusal = dormancyRefusal({ reason: 'nothing authorized is available', liveChildren: 0, childSessions: [],
    plans: stranded });
  assert.equal(refusal.refused, 'unfinished_plans');
  assert.deepEqual(refusal.attempts, ['attempt-orphan']);

  // A live child keeps its own plan: not handed back, not refused.
  assert.equal(todoSummary([attended], children.sessionSet())[0].child_alive, true);
  assert.deepEqual(unfinishedWithoutChild([attended], children.sessionSet()), [], 'a live child keeps its plan');

  // The takeover that used to be refused is available now: worker-1 is provably dead.
  assert.equal(rebindRefusal({ attempt_id: 'attempt-orphan', item_id: 'test', session_id: 'worker-1',
    bound_session: 'worker-1', live: children.ids(), root_session: 'root' }), null,
  'a departed child\'s plan may be taken over');
  assert.equal(rebindRefusal({ attempt_id: 'attempt-attended', item_id: 'test', session_id: 'worker-0',
    bound_session: 'worker-0', live: children.ids(), root_session: 'root' }).refused, 'live_session',
  'a live child still owns its plan');
});

// (4) The phase report: the subagents Activity carries.
test('the phase report names live child sessions only, never a runId', () => {
  const { children, depart } = twoChildren();
  assert.deepEqual(phaseReport({ phase: 'running', children }).subagents, ['worker-0', 'worker-1']);

  // This set's first read after the departure is the report itself.
  depart();
  const report = phaseReport({ phase: 'running', children });
  assert.deepEqual(report.subagents, ['worker-0'], 'the phase report must name live sessions only');
  assert.ok(!report.subagents.includes('run-0') && !report.subagents.includes('run-1'),
    'the phase report names session ids, never runIds');
  assert.deepEqual(Object.keys(report).sort(), ['error', 'phase', 'subagents'],
    'the report body carries no extra key: Activity forbids extras, so a second identity needs its own contract');
});

// (5) The membership answer: `has(id)` is a reader too. `todo_begin` asks it
// `children.has(bound)` to decide whether a plan bound to another session is attended,
// so a `has()` that reads the raw map answers yes for a child the registry has already
// proved gone - and the plan looks attended by a session that no longer exists.
test('the membership answer reports a departed child absent and a live one present', () => {
  const { children, depart } = twoChildren();
  // The half that must not weaken: while the registry holds both, both are members.
  assert.equal(children.has('worker-1'), true);
  assert.equal(children.has('worker-0'), true);

  // This set's first read after the departure is the membership answer itself.
  depart();
  assert.equal(children.has('worker-1'), false, 'a departed child must not answer as a live session');
  assert.equal(children.has('worker-0'), true, 'a live child must still answer as live');
});

// The set's own invariants, which no single reader owns: reconciling is idempotent,
// never re-adds a departed child, leaves a live one alone, and takes the runId
// pairing with the session it belonged to.
test('reconciling never re-adds a departed child, keeps a live one, and drops its runId', () => {
  const { children, depart } = twoChildren();
  depart();
  assert.deepEqual(children.reconcile(), ['worker-1']);
  assert.ok(children.has('worker-0'));
  assert.deepEqual(children.ids(), ['worker-0']);
  // Idempotent: repeated reads stay at one child and never re-add the dropped one.
  assert.deepEqual(children.reconcile(), []);
  assert.deepEqual(children.reconcile(), []);
  assert.deepEqual(children.ids(), ['worker-0']);
  // The runId pairing follows its session out: the second identity cannot leak either.
  assert.deepEqual([...children.runs.keys()], ['run-0']);
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
