/**
 * A plan whose items have ALL passed but which was never `todo_finish`'d, on an
 * attempt State still calls live, is work waiting on the DIRECTOR: the runtime's
 * plan view (the post-compaction snapshot) and its work view (what the idle
 * review hands back and what refuses `idle_dormant`) both carry it, marked
 * `ready_to_finish`, and never as items to run again.
 *
 * This is the second half of iss-af4fdabe05954077. Both views used to filter on
 * `remaining.length`, so the moment a child's LAST item passed the plan dropped
 * out of them - while the attempt was still `running`, had no candidate report
 * and `todo_finish` had not been run. That is exactly what a host restart leaves
 * behind when it kills a child right after its last `todo_done`: nothing ever
 * reminded the director that the attempt was ready to finish.
 *
 * What is REAL here:
 *  - the plan files and every state change: this repository's `core.todo_cli`
 *    (`create --plan-json`, `begin`, `done`, `finish`, `list`), in a temp
 *    `$AITELIER_DSH_STATE_DIR/todo`, with real git workspaces and real predicate
 *    evaluation - no hand-built plan dictionaries, including for the finished
 *    plan (its `finish` really writes a git bundle and a candidate report);
 *  - the State read: `settledAttempts` asks `/api/state/query/get_attempt` per
 *    plan, exactly as the runtime does against AItelier, and the status it gets
 *    back is the State vocabulary of `core/state_attempts.py`;
 *  - the plan view the model sees: a real `DriverRuntime`, rendered the way the
 *    post-compaction snapshot renders it.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { DriverRuntime, TodoCli, awaitingFinish, dormancyRefusal, settledAttempts, todoSummary, unfinishedWithoutChild }
  from './runtime.mjs';

const REPO_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PYTHON = process.env.AITELIER_TODO_TEST_PYTHON ?? 'python3';
const git = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
/** Every fixture commit is made by the fixture, never by the host's git identity. */
const commit = (cwd, message) => {
  git(cwd, 'add', '-A');
  git(cwd, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '-m', message);
};

/** A workspace whose HEAD is the plan's base: one commit to build on. */
async function workspace(path) {
  await mkdir(path, { recursive: true });
  git(path, 'init', '-q');
  await writeFile(join(path, 'README'), 'base\n');
  commit(path, 'base');
  return git(path, 'rev-parse', 'HEAD');
}

/** A valid plan document for `core.todo_cli create --plan-json`: one item that
 *  passes once `a.txt` exists (the harness appends `deliver`, which passes once
 *  the work is committed inside the scope). */
const planJson = (attempt_id, node_key, base_sha, workspace) => ({
  attempt_id, node_key, context_hash: 'c'.repeat(64), base_sha, workspace, scope: ['*.txt'],
  goal: 'Fixture plan for ' + attempt_id,
  items: [{ id: 'implement', title: 'Write a.txt', needs: [],
    predicates: [{ predicate: 'files_present', args: { pattern: 'a.txt' } }] }],
});

/** Walk every item to `pass`, through the real CLI and real predicates: the work
 *  is written, committed (so `deliver`'s git predicates pass) and measured. */
async function allItemsPass(todo, attempt_id, path, session) {
  await writeFile(join(path, 'a.txt'), 'a\n');
  commit(path, 'implement a.txt');
  for (const item of ['implement', 'deliver']) {
    const begun = await todo.call(attempt_id, ['begin', '--attempt=' + attempt_id, '--item=' + item, '--session=' + session]);
    assert.equal(begun.refused, undefined, JSON.stringify(begun));
    const done = await todo.call(attempt_id, ['done', '--attempt=' + attempt_id, '--item=' + item, '--session=' + session]);
    assert.equal(done.outcome, 'pass', item + ': ' + JSON.stringify(done));
  }
}

/** The five states a stored plan can be in, and the neighbour behaviour each one
 *  pins. Ids sort in this order, which is the order the CLI lists plans in. */
const IDS = {
  open: 'attempt-fp-a-open',           // items left
  awaiting: 'attempt-fp-b-awaiting',   // every item passed, never finished, attempt running
  settled: 'attempt-fp-c-settled',     // every item passed, attempt settled by State
  finished: 'attempt-fp-d-finished',   // every item passed, todo_finish already ran
  live: 'attempt-fp-e-live',           // every item passed, child still alive
};
const LIVE_CHILD = 'child-live';

/** The State answer for every fixture attempt: only `settled` is terminal. */
const answers = () => ({
  [IDS.open]: attemptAnswer(IDS.open, 'running'),
  [IDS.awaiting]: attemptAnswer(IDS.awaiting, 'running'),
  [IDS.settled]: attemptAnswer(IDS.settled, 'candidate'),
  [IDS.finished]: attemptAnswer(IDS.finished, 'running'),
  [IDS.live]: attemptAnswer(IDS.live, 'running'),
});
const attemptAnswer = (attempt_id, status) => ({ attempt_id, project_id: 'alpha', node_key: 'execution.n', status });

/** A State `get_attempt` stand-in: one status per attempt id. `asked` records the
 *  calls, so a test can prove the runtime consulted State per plan - including the
 *  plans whose items have all passed. */
function stateAnswers(statuses) {
  const asked = [];
  const read = async (path, body) => {
    asked.push({ path, arg: body?.attempt_id });
    const answer = statuses[body?.attempt_id];
    if (answer === undefined) throw Object.assign(new Error('AItelier /api/state/query/get_attempt: HTTP 500'), { status: 500 });
    return answer;
  };
  return { read, asked };
}

/** REAL plan files, in every state the views must tell apart, written and moved
 *  through this repository's CLI. */
async function fixture() {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-finished-plan-'));
  const todo = new TodoCli({ python: PYTHON, cwd: REPO_ROOT, stateDir: directory });
  const make = async key => {
    const path = join(directory, 'work', key);
    const base_sha = await workspace(path);
    const attempt_id = IDS[key];
    const file = join(directory, 'plan-' + key + '.json');
    await writeFile(file, JSON.stringify(planJson(attempt_id, 'execution.' + key, base_sha, path)));
    const { code, result } = await todo.run(['create', '--plan-json', file]);
    assert.equal(code, 0, 'the real todo CLI refused the fixture plan: ' + JSON.stringify(result));
    assert.equal(result.created, true, JSON.stringify(result));
    return path;
  };
  // a: items left - created and begun, never finished.
  await make('open');
  const begun = await todo.call(IDS.open, ['begin', '--attempt=' + IDS.open, '--item=implement', '--session=child-open']);
  assert.equal(begun.item, 'implement', JSON.stringify(begun));
  // b, c, d: every item passes (the state the defect is about).
  await allItemsPass(todo, IDS.awaiting, await make('awaiting'), 'child-awaiting');
  await allItemsPass(todo, IDS.settled, await make('settled'), 'child-settled');
  await allItemsPass(todo, IDS.finished, await make('finished'), 'child-finished');
  // d really is finished: the CLI writes the bundle and the candidate report.
  const finish = await todo.call(IDS.finished, ['finish', '--attempt=' + IDS.finished, '--out=' + todo.finishDir(IDS.finished)]);
  assert.equal(finish.refused, undefined, 'the real todo CLI refused to finish the fixture plan: ' + JSON.stringify(finish));
  assert.ok((await stat(todo.finishReport(IDS.finished))).size > 0, JSON.stringify(finish));
  // e: every item passes with a child still holding the plan.
  await allItemsPass(todo, IDS.live, await make('live'), LIVE_CHILD);
  return { directory, todo };
}

const ids = list => list.map(entry => entry.attempt_id);
const sorted = list => ids(list).sort();
const evidence = list => JSON.stringify(list);

test('real plan files: an all-passed plan nobody finished is surfaced in both views as ready to finish, and never as items to run again', { timeout: 120000 }, async () => {
  const { directory, todo } = await fixture();
  try {
    const plans = await todo.plans();
    assert.deepEqual(sorted(plans), Object.values(IDS).sort(), 'the CLI did not rediscove the fixture plans');
    // The plan files carry the state each view has to tell apart, measured by the CLI.
    const byId = Object.fromEntries(plans.map(plan => [plan.attempt_id, plan]));
    assert.deepEqual(byId[IDS.open].remaining, ['implement', 'deliver']);
    for (const key of ['awaiting', 'settled', 'finished', 'live']) {
      const plan = byId[IDS[key]];
      assert.deepEqual(plan.remaining, [], key + ' should have every item passed: ' + evidence(plan));
      assert.equal(plan.next, null, key + ' still offers an item as next: ' + evidence(plan));
      assert.deepEqual([...plan.done].sort(), ['deliver', 'implement'], key);
    }
    // Only the plan whose candidate report exists is known to have been finished -
    // the plan file itself is never rewritten by `todo_finish`.
    assert.deepEqual(plans.filter(plan => plan.finish_reported).map(plan => plan.attempt_id), [IDS.finished]);
    assert.deepEqual(sorted(plans.filter(awaitingFinish)), [IDS.awaiting, IDS.live, IDS.settled].sort(),
      'awaitingFinish does not name the plans nobody finished');

    // State is asked about EVERY plan a view could show - the all-passed ones
    // included, or their settled attempt could never be recognised. A plan whose
    // candidate report exists cannot be shown, so it is not asked about.
    const { read, asked } = stateAnswers(answers());
    const settled = await settledAttempts(plans, read);
    assert.deepEqual([...settled], [IDS.settled]);
    assert.deepEqual(asked.map(entry => entry.arg).sort(),
      [IDS.open, IDS.awaiting, IDS.settled, IDS.live].sort(), 'State was not asked about every plan a view can show');
    assert.deepEqual(asked.map(entry => entry.path), asked.map(() => '/api/state/query/get_attempt'));

    const live = new Set([LIVE_CHILD]);
    // THE FIX: the plan whose items all passed is work waiting on the director,
    // in the plan view...
    assert.deepEqual(todoSummary(plans, live, settled), [
      { attempt_id: IDS.open, node_key: 'execution.open', next: 'implement', remaining_count: 2,
        ready_to_finish: false, child_alive: false },
      { attempt_id: IDS.awaiting, node_key: 'execution.awaiting', next: null, remaining_count: 0,
        ready_to_finish: true, child_alive: false },
      { attempt_id: IDS.live, node_key: 'execution.live', next: null, remaining_count: 0,
        ready_to_finish: true, child_alive: true },
    ], 'the plan view lost or mislabelled a plan');
    // ...and in the work view (what the idle review hands back), with `remaining`
    // empty: there is no item to dispatch again, only a finish to run.
    assert.deepEqual(unfinishedWithoutChild(plans, live, settled), [
      { attempt_id: IDS.open, node_key: 'execution.open', next: 'implement', remaining: ['implement', 'deliver'],
        ready_to_finish: false },
      { attempt_id: IDS.awaiting, node_key: 'execution.awaiting', next: null, remaining: [], ready_to_finish: true },
    ], 'the work view lost or mislabelled a plan');

    // Delete the marker's branch - filter on `remaining.length` alone, which is
    // the defect - and the plan this test is about disappears from both views.
    assert.deepEqual(sorted(plans.filter(plan => plan.remaining?.length)), [IDS.open],
      'the fixture no longer reproduces the drop-out');

    // The refusal the director actually gets names the plan it only has to
    // finish, separately from the one with items left.
    const refusal = dormancyRefusal({ reason: 'nothing authorized is available', liveChildren: 0, claims: [],
      plans: unfinishedWithoutChild(plans, live, settled) });
    assert.equal(refusal.refused, 'unfinished_plans', evidence(refusal));
    assert.deepEqual(refusal.attempts, [IDS.open, IDS.awaiting]);
    assert.deepEqual(refusal.ready_to_finish, [IDS.awaiting], 'the refusal did not say which plan only needs finishing');

    // The plan view the director is SHOWN (the post-compaction snapshot) is the
    // same list, through the real DriverRuntime.
    const runtime = new DriverRuntime({ project: 'alpha', driver: 'driver-a', directory,
      request: async path => path.endsWith('/me') ? { driver_id: 'driver-a' }
        : { event_seq: 1, nodes: [], project_note: {}, private_note: {} },
      todoPlans: async () => { const listed = await todo.plans(); return todoSummary(listed, live, await settledAttempts(listed, read)); } });
    await runtime.init();
    const shown = JSON.parse(runtime.render()).todo;
    assert.deepEqual(shown.map(plan => [plan.attempt_id, plan.ready_to_finish, plan.remaining_count]), [
      [IDS.open, false, 2], [IDS.awaiting, true, 0], [IDS.live, true, 0]], 'the rendered plan view: ' + evidence(shown));
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('the neighbours: a settled attempt, a plan with items left and a live child keep their behaviour, each mutant-sensitive', { timeout: 120000 }, async () => {
  const { directory, todo } = await fixture();
  try {
    const plans = await todo.plans();
    const { read } = stateAnswers(answers());
    const settled = await settledAttempts(plans, read);
    const live = new Set([LIVE_CHILD]);

    // (a) A settled attempt's plan still does not count. `settled` is what keeps
    // it out: delete the terminal check - in either view, or in the State read
    // that feeds them - and the all-passed plan of a settled attempt is surfaced
    // as work that no longer exists.
    assert.equal(ids(todoSummary(plans, live, settled)).includes(IDS.settled), false);
    assert.equal(ids(unfinishedWithoutChild(plans, live, settled)).includes(IDS.settled), false);
    assert.equal(ids(todoSummary(plans, live)).includes(IDS.settled), true, 'nothing but the settled set was hiding it');
    // A settled attempt State cannot answer for stays unsettled: fail closed.
    const unreadable = await settledAttempts(plans, async () => { throw Object.assign(new Error('HTTP 503'), { status: 503 }); });
    assert.deepEqual([...unreadable], []);
    assert.equal(ids(unfinishedWithoutChild(plans, live, unreadable)).includes(IDS.settled), true,
      'an unreadable State silently settled a plan');
    // A plan whose candidate report is unknown (no annotation at all) is still
    // surfaced: forgetting is safer than dropping.
    assert.equal(awaitingFinish({ attempt_id: 'x', remaining: [], next: null }), true);
    assert.equal(awaitingFinish({ attempt_id: 'x', remaining: [], finish_reported: true }), false);

    // (b) A plan with items left is still reported as unfinished, and is NOT
    // reported ready to finish. Flip that marking and this reddens.
    assert.deepEqual(unfinishedWithoutChild(plans, live, settled).find(plan => plan.attempt_id === IDS.open), {
      attempt_id: IDS.open, node_key: 'execution.open', next: 'implement', remaining: ['implement', 'deliver'],
      ready_to_finish: false });
    assert.deepEqual(todoSummary(plans, live, settled).find(plan => plan.attempt_id === IDS.open), {
      attempt_id: IDS.open, node_key: 'execution.open', next: 'implement', remaining_count: 2,
      ready_to_finish: false, child_alive: false });
    // Every entry's marker is exactly "nothing left to run": never a plan with
    // items flagged as ready, never an all-passed plan reported as items.
    for (const plan of unfinishedWithoutChild(plans, live, settled)) {
      assert.equal(plan.ready_to_finish, plan.remaining.length === 0, evidence(plan));
    }

    // (c) A live child's plan is still its own: done from any other session is
    // refused, and dormancy refuses while that child runs.
    const refused = await todo.call(IDS.live, ['done', '--attempt=' + IDS.live, '--item=deliver', '--session=driver-root']);
    assert.equal(refused.refused, 'bound_to_other_session', JSON.stringify(refused));
    assert.equal(refused.bound_session, LIVE_CHILD);
    assert.equal(ids(unfinishedWithoutChild(plans, live, settled)).includes(IDS.live), false,
      "a live child's plan was handed back");
    assert.equal(dormancyRefusal({ reason: 'quiet', liveChildren: 1, claims: [], plans: [] }).refused, 'live_children');

    // An all-passed plan is never re-run as unfinished items: no view offers one,
    // and the CLI has no item left to begin.
    const status = await todo.call(IDS.awaiting, ['status', '--attempt=' + IDS.awaiting]);
    assert.deepEqual(status.remaining, []);
    assert.equal(status.next, null);
    assert.deepEqual([...status.done].sort(), ['deliver', 'implement']);
    const begin = await todo.call(IDS.awaiting, ['begin', '--attempt=' + IDS.awaiting, '--item=deliver', '--session=driver-root']);
    assert.equal(begin.refused, 'bound_to_other_session', JSON.stringify(begin));
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('the marker tracks the candidate report: a finished plan is not nagged, and one whose report is missing is', { timeout: 120000 }, async () => {
  const { directory, todo } = await fixture();
  try {
    const plans = await todo.plans();
    const { read } = stateAnswers(answers());
    const settled = await settledAttempts(plans, read);
    const live = new Set([LIVE_CHILD]);
    // The finished plan's candidate report exists, so finishing already ran: it is
    // not offered as ready to finish again (a second finish would re-do the bundle).
    const candidate = todo.finishReport(IDS.finished);
    const bytes = await readFile(candidate);
    assert.equal(plans.find(plan => plan.attempt_id === IDS.finished).finish_reported, true);
    assert.equal(ids(unfinishedWithoutChild(plans, live, settled)).includes(IDS.finished), false);
    assert.equal(ids(todoSummary(plans, live, settled)).includes(IDS.finished), false);
    // Lose the report and the same plan comes back as work waiting on the
    // director: "no candidate" is the only thing that makes finishing pending.
    await rm(candidate);
    const without = await todo.plans();
    assert.equal(without.find(plan => plan.attempt_id === IDS.finished).finish_reported, false);
    assert.deepEqual(todoSummary(without, live, settled).find(plan => plan.attempt_id === IDS.finished), {
      attempt_id: IDS.finished, node_key: 'execution.finished', next: null, remaining_count: 0,
      ready_to_finish: true, child_alive: false });
    assert.deepEqual(unfinishedWithoutChild(without, live, settled).find(plan => plan.attempt_id === IDS.finished), {
      attempt_id: IDS.finished, node_key: 'execution.finished', next: null, remaining: [], ready_to_finish: true });
    await writeFile(candidate, bytes);
    assert.equal((await todo.plans()).find(plan => plan.attempt_id === IDS.finished).finish_reported, true);

    // A finish that REFUSES writes no candidate, so its plan keeps asking to be
    // finished - a refusal is a defect in the work, not an ending.
    const refused = await todo.call(IDS.open, ['finish', '--attempt=' + IDS.open, '--out=' + todo.finishDir(IDS.open)]);
    assert.equal(refused.refused, 'unfinished', JSON.stringify(refused));
    const after = await todo.plans();
    assert.equal(after.find(plan => plan.attempt_id === IDS.open).finish_reported, false);
    assert.equal(ids(unfinishedWithoutChild(after, live, settled)).includes(IDS.open), true);
  } finally { await rm(directory, { recursive: true, force: true }); }
});
