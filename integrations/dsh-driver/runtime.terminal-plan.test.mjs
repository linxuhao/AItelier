/**
 * A plan whose State attempt has SETTLED stops counting as unfinished work, while
 * the two safety properties stay exact: a plan bound to a live child keeps
 * answering `bound_to_other_session`, and the stranded plan of a NON-terminal (or
 * unanswerable) attempt keeps refusing `idle_dormant`.
 *
 * What is REAL here:
 *  - the plan files: written by this repository's `core.todo_cli`
 *    (`python3 -m core.todo_cli create --plan-json`), listed back by its own
 *    `list`/`PlanStore.attempts()`, in a temp `$AITELIER_DSH_STATE_DIR/todo`;
 *  - the runtime: a real pinned DSH boots `runtime.mjs` with a fixture model, and
 *    the director's `idle_dormant`/`todo_*` calls are the real tools;
 *  - the State read: the runtime asks `/api/state/query/get_attempt` with the
 *    plan's attempt id, exactly as it does against AItelier.
 *
 * The backend answering that read is the fixture HTTP server every real-DSH test
 * in this directory runs against; the status it answers with is the State-shaped
 * status vocabulary of `core/state_attempts.py` (`running`, `candidate`, `failed`,
 * ...). No status is handed to a decision function: the tests assert what the
 * runtime did with it (dormant accepted, refusal names/attempts, snapshot view),
 * and the unreadable case is a real HTTP 500 on that read.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, rm, realpath } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { TodoCli, dormancyRefusal, settledAttempts, attemptSettled, todoSummary, unfinishedWithoutChild } from './runtime.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PYTHON = process.env.AITELIER_TODO_TEST_PYTHON ?? 'python3';
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });

/** A valid plan document for `core.todo_cli create --plan-json`: one unfinished
 *  item (`implement`; the harness appends `deliver`), so the plan always has
 *  remaining work. Only the frozen plan is needed here - nothing is ever done. */
const planJson = (attempt_id, workspace) => ({
  attempt_id, node_key: 'execution.' + attempt_id, context_hash: 'c'.repeat(64),
  base_sha: '0'.repeat(40), workspace, scope: ['*.txt'], goal: 'Fixture plan for ' + attempt_id,
  items: [{ id: 'implement', title: 'Write a.txt', needs: [],
    predicates: [{ predicate: 'files_present', args: { pattern: 'a.txt' } }] }],
});

/** REAL plan files: the repository's CLI writes them into the temp store. */
async function realPlans(directory, attempt_ids) {
  const work = join(directory, 'work');
  await mkdir(work, { recursive: true });
  const todo = new TodoCli({ python: PYTHON, cwd: REPO_ROOT, stateDir: directory });
  for (const attempt_id of attempt_ids) {
    const file = join(directory, 'plan-' + attempt_id + '.json');
    await writeFile(file, JSON.stringify(planJson(attempt_id, work)));
    const { code, result } = await todo.run(['create', '--plan-json', file]);
    assert.equal(code, 0, 'the real todo CLI refused the fixture plan: ' + JSON.stringify(result));
    assert.equal(result.created, true, JSON.stringify(result));
  }
  return todo;
}

/** A State `get_attempt` stand-in: one status per attempt id, and `asked` records
 *  the calls so a test can prove the runtime really consulted State per plan. */
function stateAnswers(answers) {
  const asked = [];
  const read = async (path, body) => {
    asked.push({ path, arg: body?.attempt_id });
    const answer = answers[body?.attempt_id];
    if (answer === undefined) {
      const error = new Error('AItelier /api/state/query/get_attempt: HTTP 500');
      error.status = 500;
      throw error;
    }
    return answer;
  };
  return { read, asked };
}

test('real plan files: only a settled attempt stops counting as unfinished work; a live, an unreadable and an unknown one keep blocking', { timeout: 60000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-terminal-plan-'));
  try {
    const ids = { settled: 'attempt-tp-candidate', running: 'attempt-tp-running', failed: 'attempt-tp-settled',
      unknown: 'attempt-tp-unknown', unreadable: 'attempt-tp-unreadable' };
    const todo = await realPlans(directory, Object.values(ids));
    // The plan files really are on disk, named after their attempt, and the CLI
    // rediscovers them (the runtime's only view of a plan).
    const plans = await todo.plans();
    assert.deepEqual(plans.map(p => p.attempt_id).sort(), Object.values(ids).sort());
    for (const plan of plans) assert.ok(plan.remaining.includes('implement'), JSON.stringify(plan));

    const { read, asked } = stateAnswers({
      [ids.settled]: { attempt_id: ids.settled, project_id: 'alpha', node_key: 'execution.n', status: 'candidate' },
      [ids.failed]: { attempt_id: ids.failed, project_id: 'alpha', node_key: 'execution.n', status: 'failed' },
      [ids.running]: { attempt_id: ids.running, project_id: 'alpha', node_key: 'execution.n', status: 'running' },
      [ids.unknown]: { attempt_id: ids.unknown, project_id: 'alpha', node_key: 'execution.n', status: 'awaiting-review' },
      // ids.unreadable: no answer at all -> the read throws, like State being down
    });
    const settled = await settledAttempts(plans, read);
    assert.deepEqual([...settled].sort(), [ids.settled, ids.failed].sort(), 'exactly the terminal attempts settled');
    // State was asked, per plan, by the attempt id the plan file is named after.
    assert.deepEqual(asked.map(a => a.path), plans.map(() => '/api/state/query/get_attempt'));
    assert.deepEqual(asked.map(a => a.arg).sort(), Object.values(ids).sort());

    // THE GUARD: a settled plan is not work; nothing else changed.
    assert.deepEqual(unfinishedWithoutChild(plans, new Set(), settled).map(p => p.attempt_id).sort(),
      [ids.running, ids.unknown, ids.unreadable].sort());
    assert.deepEqual(todoSummary(plans, new Set(), settled).map(p => p.attempt_id).sort(),
      [ids.running, ids.unknown, ids.unreadable].sort());
    // Remove the terminal-status check and every plan comes back: this is what the
    // runtime did before the change, and what it must still do for these three.
    assert.deepEqual(unfinishedWithoutChild(plans, new Set()).map(p => p.attempt_id).sort(), Object.values(ids).sort());
    assert.deepEqual(todoSummary(plans, new Set()).map(p => p.attempt_id).sort(), Object.values(ids).sort());
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('the settled test fails closed: an unreachable State, a malformed answer or an unknown status never settles a plan', { timeout: 60000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-terminal-plan-closed-'));
  try {
    const attempt = 'attempt-tp-closed';
    const todo = await realPlans(directory, [attempt]);
    const plans = await todo.plans();
    const cases = [
      [() => { throw Object.assign(new Error('HTTP 503'), { status: 503 }); }, false, 'State unreachable'],
      [async () => ({ detail: 'no such attempt' }), false, 'an answer with no status'],
      [async () => ({ status: 42 }), false, 'a non-string status'],
      [async () => ({ status: 'RUNNING' }), false, 'a status this runtime does not know'],
      [async () => ({ status: 'awaiting-review' }), false, 'a status State added later'],
      [async () => ({ status: 'reserved' }), false, 'still reserved'],
      [async () => ({ status: 'launching' }), false, 'launching'],
      [async () => ({ status: 'paused' }), false, 'paused'],
      [async () => ({ status: 'unknown' }), false, 'unknown'],
      [async () => ({ status: 'failed' }), true, 'failed'],
      [async () => ({ status: 'candidate' }), true, 'candidate'],
      [async () => ({ status: 'superseded' }), true, 'superseded'],
      [async () => ({ status: 'abandoned' }), true, 'abandoned'],
      [async () => ({ result: { status: 'failed' } }), true, 'a failed attempt in the tolerated wrapper'],
    ];
    for (const [answer, expected, why] of cases) {
      const settled = await settledAttempts(plans, answer);
      const blocks = unfinishedWithoutChild(plans, new Set(), settled).map(p => p.attempt_id);
      // The plan keeps blocking unless the answer proves the attempt settled -
      // "I could not ask" is never "it settled".
      assert.deepEqual(blocks, expected ? [] : [attempt], why);
      let verdict;
      try { verdict = attemptSettled(await answer()); } catch { verdict = false; }
      assert.equal(verdict, expected, why);
    }
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('a plan bound to a live child keeps its plan: bound_to_other_session, and dormancy still refuses it', { timeout: 60000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-terminal-plan-live-'));
  try {
    const attempt = 'attempt-tp-live';
    const todo = await realPlans(directory, [attempt]);
    const begun = await todo.call(attempt, ['begin', '--attempt=' + attempt, '--item=implement', '--session=child-live']);
    assert.equal(begun.child_session, 'child-live', JSON.stringify(begun));
    // The director is not the live child: the CLI refuses, as it always has.
    const done = await todo.call(attempt, ['done', '--attempt=' + attempt, '--item=implement', '--session=driver-root']);
    assert.equal(done.refused, 'bound_to_other_session', JSON.stringify(done));
    assert.equal(done.bound_session, 'child-live');
    const plans = await todo.plans();
    assert.equal(plans[0].child_session, 'child-live');
    // A live child's plan is never the director's to hand back, and the snapshot
    // still shows it as bound (it is not silenced either way).
    assert.deepEqual(unfinishedWithoutChild(plans, new Set(['child-live'])), []);
    assert.deepEqual(todoSummary(plans, new Set(['child-live'])).map(p => [p.attempt_id, p.child_alive]), [[attempt, true]]);
    // Dormancy refuses while that child is live, before any plan or State question.
    assert.equal(dormancyRefusal({ reason: 'quiet', liveChildren: 1, claims: [], plans: [] }).refused, 'live_children');
    // The stranded case todo_rebind exists for: attempt still live (or State cannot
    // answer), child gone - the plan must keep refusing dormancy unfinished_plans.
    const stranded = dormancyRefusal({ reason: 'quiet', liveChildren: 0, claims: [],
      plans: unfinishedWithoutChild(plans, new Set(), new Set()) });
    assert.equal(stranded.refused, 'unfinished_plans', JSON.stringify(stranded));
    assert.deepEqual(stranded.attempts, [attempt]);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

// ---------------------------------------------------------------------------
/** Sorted by attempt id, the way the CLI lists them: settled, running, unreadable. */
const PLANS = { settled: 'attempt-tp-a-settled', running: 'attempt-tp-b-running', unreadable: 'attempt-tp-c-unreadable' };

// End to end: a real pinned DSH. One director turn per idle_dormant call, so the
// SAME real plan files are measured before and after their attempts settle.
const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-terminal-plan-provider';
export const inject = ['llm'];
const post = async (path, body) => (await fetch(process.env.AITELIER_URL + path, { method: 'POST', body: JSON.stringify(body ?? {}) })).json();
const plans = JSON.parse(process.env.AITELIER_TEST_PLANS);
const call = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const idle = (id, reason) => call(id, 'idle_dormant', { reason });
export function apply(ctx) {
  const root = process.env.AITELIER_DSH_SESSION, done = new Set();
  // The settle hooks run BEFORE the tool call they precede, so the tool sees the
  // attempt's new status on the same turn (no polling, no sleeps).
  const script = [
    { key: 'idle-1', block: () => idle('root-idle-1', 'nothing authorized is available') },
    { key: 'idle-2', block: async () => { await post('/test/settle', { attempt_id: plans.running });
      return idle('root-idle-2', 'the running attempt was reported failed'); } },
    { key: 'idle-3', block: async () => { await post('/test/settle', { attempt_id: plans.unreadable });
      return idle('root-idle-3', 'the attempt State could not answer for has settled'); } },
    { key: 'scenario-done', block: async () => { await post('/test/scenario-done', {});
      return { type: 'text', text: 'TERMINAL_PLAN_SCENARIO_DONE' }; } },
  ];
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 16000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      await post('/test/request', { session: options.sessionId, purpose: options.purpose ?? null });
      let blocks;
      if (options.purpose === 'compaction') blocks = [{ type: 'text', text: 'Checkpoint: nothing in flight.' }];
      else if (!options.tools?.length || options.sessionId !== root) blocks = [{ type: 'text', text: 'Fixture session' }];
      else {
        const next = script.find(candidate => !done.has(candidate.key));
        if (!next) blocks = [{ type: 'text', text: 'ROOT_DONE' }];
        else { done.add(next.key); blocks = [await next.block()]; }
      }
      for (const [index, block] of blocks.entries()) {
        yield { type: 'block-start', index, blockType: block.type };
        if (block.type === 'text') yield { type: 'text-delta', index, text: block.text };
        else yield { type: 'tool-call-delta', index, id: block.id, name: block.name, argumentsDelta: block.arguments };
        yield { type: 'block-end', index, block };
      }
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } };
      yield { type: 'finish', reason: { kind: blocks.at(-1).type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
  ctx.on('session/event', (session, event) => {
    if (['tool/result', 'user/message', 'compaction/end'].includes(event.type)) void post('/test/event', { session: session.id, event });
  });
}
`;

let scenarioRun;
const terminalPlanScenario = () => (scenarioRun ??= bootTerminalPlanScenario());

async function bootTerminalPlanScenario() {
  const directory = await mkdtemp(join(homedir(), 'dsh-terminal-plan-boot-'));
  // Unique per run: idle_dormant's background-process scan matches by session, and
  // the other real-DSH test files run their own drivers in parallel.
  const session = 'terminal-plan-root-' + process.pid + '-' + Math.random().toString(36).slice(2, 8);
  const entry = await realpath(process.env.AITELIER_DSH_TEST_ENTRY ?? new URL('./node_modules/.bin/dsh', import.meta.url));
  const events = [], requests = [], attemptsRead = [], settled = new Set();
  let finished = false, output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [] } });
    if (req.url === '/api/state/query/list_claims') return send({ claims: [] });
    // The one State read this change adds: the attempt each plan is named after.
    if (req.url === '/api/state/query/get_attempt') {
      attemptsRead.push(body.attempt_id);
      const envelope = status => ({ attempt_id: body.attempt_id, project_id: 'alpha', node_key: 'execution.n', status });
      if (settled.has(body.attempt_id)) return send(envelope('failed'));
      if (body.attempt_id === PLANS.running) return send(envelope('running'));
      if (body.attempt_id === PLANS.settled) return send(envelope('candidate'));
      // PLANS.unreadable: State cannot answer for this one (yet).
      res.statusCode = 500;
      return send({ detail: 'state database is busy' });
    }
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture', event_seq: 10,
      nodes: [{ name: 'n', status: 'PENDING' }], project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'fixture driver skill' });
    if (req.url.endsWith('/activity') || req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url === '/api/state/query/wait_for_state_change') return; // never answers: no State events needed
    if (req.url === '/test/request') { requests.push(body); return send({ ok: true }); }
    if (req.url === '/test/event') { events.push(body); return send({ ok: true }); }
    if (req.url === '/test/settle') { settled.add(body.attempt_id); return send({ ok: true }); }
    if (req.url === '/test/scenario-done') { finished = true; return send({ ok: true }); }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const todo = await realPlans(directory, [PLANS.settled, PLANS.running, PLANS.unreadable]);
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-terminal-plan-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { id: 'tool-subagent', config: { provider: 'spawn', toolName: 'subagent', agentOptions: { provider: 'fixture', model: 'worker' } } },
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-terminal-plan-provider.mjs') }, { id: 'driver', name: join(HERE, 'runtime.mjs') }] },
    ]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...process.env,
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: session,
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'),
      AITELIER_DSH_EXECUTION: 'host', DSH_PERMISSION_MODE: 'danger-full-access', AITELIER_DSH_IDLE_REVIEW_MS: '60000',
      AITELIER_TODO_PYTHON: todo.python, AITELIER_TODO_CWD: todo.cwd, AITELIER_TEST_PLANS: JSON.stringify(PLANS) } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);
    const deadline = Date.now() + 75000;
    while (Date.now() < deadline && proc.exitCode === null && !finished) await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(proc.exitCode, null, output);
    const trace = () => JSON.stringify(requests) + '\n' + JSON.stringify(events.map(e => [e.event.type,
      e.event.data?.message?.toolCallId ?? e.event.data?.source?.kind ?? ''])) + '\n' + output.slice(-4000);
    assert.ok(finished, 'the terminal-plan scenario did not finish\n' + trace());
    const result = id => events.find(e => e.event.type === 'tool/result' && e.event.data.message.toolCallId === id);
    const json = id => {
      const found = result(id);
      assert.ok(found, 'no tool result was recorded for ' + id + '\n' + trace());
      return JSON.parse(found.event.data.message.content.map(block => block.text ?? '').join(''));
    };
    return { json, events, attemptsRead, plans: PLANS };
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  }
}

test('real pinned DSH: a settled attempt stops refusing idle_dormant, while a live or unanswerable attempt keeps blocking', { timeout: 120000 }, async () => {
  const run = await terminalPlanScenario();
  // The runtime asked State about every plan, by the attempt id its file is named after.
  assert.deepEqual([...new Set(run.attemptsRead)].sort(), Object.values(run.plans).sort());

  // The post-compaction snapshot is the director's summary: the settled attempt's
  // plan is not listed as awaiting work; the other two still are.
  const snapshot = run.events.map(e => e.event).find(e => e.type === 'user/message' && e.data?.source?.kind === 'aitelier-driver-state');
  assert.ok(snapshot, 'no driver-state snapshot was injected');
  const state = JSON.parse(snapshot.data.content.map(block => block.text ?? '').join('').split('\n').slice(1).join('\n'));
  assert.deepEqual((state.todo ?? []).map(plan => plan.attempt_id), [run.plans.running, run.plans.unreadable]);

  // 1. Before anything settles: the live attempt and the unanswerable one block.
  //    (Delete the fail-closed guard - treat "could not ask" as settled - and this
  //    goes red with the unreadable attempt missing.)
  const first = run.json('root-idle-1');
  assert.equal(first.refused, 'unfinished_plans', JSON.stringify(first));
  assert.deepEqual(first.attempts, [run.plans.running, run.plans.unreadable]);

  // 2. The running attempt is reported failed: its plan stops blocking at once.
  //    The unreadable one still does - an unanswerable State never settles a plan.
  const second = run.json('root-idle-2');
  assert.equal(second.refused, 'unfinished_plans', JSON.stringify(second));
  assert.deepEqual(second.attempts, [run.plans.unreadable]);

  // 3. Every attempt has now settled: dormancy is NOT refused for their plans.
  //    Delete the terminal-status check and this goes back to a refusal.
  const third = run.json('root-idle-3');
  assert.equal(third.refused, undefined, JSON.stringify(third));
  assert.equal(third.dormant, true, JSON.stringify(third));
});
