import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { mkdtemp, mkdir, writeFile, readFile, readdir, copyFile, rm, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { bindDriverGoal } from './goal.mjs';

// A stateful stand-in for the native goals service: edit() bumps the revision
// and keeps everything else, as the pinned dsh-goal does.
function fakeGoals(initial) {
  const state = { goal: initial, edits: [], resumes: 0, creates: 0 };
  state.get = () => state.goal;
  state.edit = (_agent, ref, request) => {
    assert.deepEqual(ref, { id: state.goal.id, revision: state.goal.revision });
    state.edits.push(request);
    return state.goal = { ...state.goal, revision: state.goal.revision + 1, objective: request.objective.trim() };
  };
  state.resume = (_agent, ref) => {
    state.resumes++;
    return state.goal = { ...state.goal, revision: ref.revision + 1, activation: 'armed' };
  };
  state.create = (_agent, { objective }) => { state.creates++; return state.goal = { id: 'new', revision: 1, objective, phase: 'active', activation: 'armed' }; };
  return state;
}
const active = (over = {}) => ({ id: 'g1', revision: 7, objective: 'OLD mandate', phase: 'active', activation: 'disarmed', roundsStarted: 12, maxGoalRounds: 256, ...over });

test('an active goal whose objective differs from goal.md is re-seeded in place', () => {
  const goals = fakeGoals(active()), seeded = [];
  const view = bindDriverGoal(goals, {}, 'NEW mandate\n', { onSeed: v => seeded.push(v) });
  // Exactly one edit carrying only the objective: the round cap is not touched.
  assert.deepEqual(goals.edits, [{ objective: 'NEW mandate\n' }]);
  assert.equal(view.id, 'g1');
  assert.equal(view.phase, 'active');
  assert.equal(view.roundsStarted, 12);
  assert.equal(view.maxGoalRounds, 256);
  assert.equal(view.objective, 'NEW mandate');
  // The existing disarmed -> resume step continues from the edited revision.
  assert.equal(goals.resumes, 1);
  assert.equal(view.activation, 'armed');
  assert.equal(view.revision, 9);
  assert.equal(seeded.length, 1);
  assert.equal(seeded[0].id, 'g1');
  assert.equal(seeded[0].revision, 8);
});

test('the same objective is not edited again (no revision churn)', () => {
  // Native DSH trims on create/edit, so a template with a trailing newline is the same text.
  for (const [armed, rendered] of [['disarmed', 'OLD mandate\n'], ['armed', 'OLD mandate']]) {
    const goals = fakeGoals(active({ activation: armed })), seeded = [];
    bindDriverGoal(goals, {}, rendered, { onSeed: v => seeded.push(v) });
    assert.deepEqual(goals.edits, []);
    assert.deepEqual(seeded, []);
    assert.equal(goals.goal.revision, armed === 'disarmed' ? 8 : 7);
  }
});

test('paused, blocked and completed goals keep their objective; cleared goals are not recreated', () => {
  for (const phase of ['paused', 'blocked', 'complete']) {
    const goals = fakeGoals(active({ phase }));
    assert.equal(bindDriverGoal(goals, {}, 'NEW mandate'), goals.goal);
    assert.deepEqual(goals.edits, []);
    assert.equal(goals.goal.objective, 'OLD mandate');
    assert.equal(goals.goal.revision, 7);
  }
  const cleared = fakeGoals(undefined);
  const agent = { session: { snapshotEvents: () => [{ type: 'goal/change', data: { operation: 'clear' } }] } };
  assert.equal(bindDriverGoal(cleared, agent, 'NEW mandate'), undefined);
  assert.equal(cleared.creates, 0);
  assert.deepEqual(cleared.edits, []);
});

test('a re-seeded goal at its round cap is still blocked rather than resumed', () => {
  const goals = fakeGoals(active({ roundsStarted: 256 }));
  goals.block = (_a, ref, reason) => ({ ...goals.goal, phase: 'blocked', blockedReason: reason, seenRevision: ref.revision });
  const view = bindDriverGoal(goals, {}, 'NEW mandate');
  assert.equal(goals.goal.objective, 'NEW mandate');
  assert.equal(view.phase, 'blocked');
  assert.equal(view.seenRevision, 8, 'block must be addressed to the edited revision');
  assert.equal(goals.resumes, 0);
});

// ---- real pinned DSH: the host service's restart path --------------------------------

const here = dirname(fileURLToPath(import.meta.url));

async function sessionEvents(root, session) {
  const require = createRequire(process.env.AITELIER_DSH_ENTRY ?? '/opt/aitelier-dsh/node_modules/.bin/dsh');
  const { Context } = await import(require.resolve('@deepseek-ai/cordis'));
  const storage = await import(require.resolve('@deepseek-ai/dsh-session-persistence-jsonl'));
  const ctx = new Context();
  const fork = ctx.plugin(storage, { root });
  let handle;
  try {
    await fork.await();
    handle = await ctx.sessionPersistence.open(session, 'read');
    return (await handle.read()).events;
  } finally { await handle?.close(); await fork.dispose(); }
}

test('real pinned DSH: restart re-seeds an active director goal from goal.md exactly once', { timeout: 60000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-goalseed-'));
  const entry = await realpath(process.env.AITELIER_DSH_TEST_ENTRY ?? new URL('./node_modules/.bin/dsh', import.meta.url));
  // Two copies of the driver that differ only in goal.md stand for "goal.md edited, host service restarted".
  const variants = {};
  for (const [name, mandate] of [['old', 'OLD mandate for {{project_id}}\n'], ['new', 'NEW mandate for {{project_id}}\n']]) {
    const dir = variants[name] = join(directory, 'driver-' + name);
    await mkdir(dir);
    for (const file of await readdir(here)) if (/\.(mjs|md)$/.test(file) && !/\.test\.mjs$/.test(file) && file !== 'goal.md') await copyFile(join(here, file), join(dir, file));
    await writeFile(join(dir, 'goal.md'), mandate);
  }
  const activity = [], parked = new Set();
  let output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/api/drivers/me') return send({ driver_id: 'seed-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url.endsWith('/context')) return send({ nodes: [], project_note: {}, private_note: {}, event_seq: 1 });
    if (req.url.endsWith('/activity')) { activity.push(body); return send({ ok: true }); }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url.endsWith('/list_director_messages')) return send({ items: [] });
    if (req.url.endsWith('/wait_for_state_change')) { parked.add(res); res.on('close', () => parked.delete(res)); return; }
    if (req.url.endsWith('/completion')) return send({ text: 'Idle director.', reasoning_content: '', truncated: false, tool_calls: [], usage: { prompt_tokens: 100, completion_tokens: 10 } });
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const token = join(directory, 'token'); await writeFile(token, 'fixture-only', { mode: 0o600 });
  const profile = variant => ['driver-' + variant, JSON.stringify([{ id: 'hmr', disabled: true }, { insert: [{ id: 'driver', name: join(variants[variant], 'runtime.mjs') }] }])];
  for (const variant of ['old', 'new']) {
    const dir = join(directory, 'home/profiles/driver-' + variant); await mkdir(dir, { recursive: true });
    await writeFile(join(dir, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(dir, 'cordis.patch.yml'), profile(variant)[1]);
  }
  const seedFile = join(directory, 'goal-seed.json');
  const readSeed = async () => { try { return JSON.parse(await readFile(seedFile, 'utf8')); } catch (error) { if (error.code === 'ENOENT') return undefined; throw error; } };
  const run = async (variant, until) => {
    activity.length = 0;
    proc = spawn(process.execPath, [entry, '--profile', 'driver-' + variant], { cwd: directory, env: { ...process.env,
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'seed-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'seed-root',
      AITELIER_DSH_PROVIDER: 'aitelier', AITELIER_DSH_MODEL: 'driver', AITELIER_URL: `http://127.0.0.1:${server.address().port}`,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'absent') } });
    proc.stdout.on('data', c => output += c); proc.stderr.on('data', c => output += c);
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline && proc.exitCode === null && !(await until())) await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(proc.exitCode, null, output);
    assert.ok(await until(), 'host never reached the expected state\n' + output);
    await new Promise(resolve => setTimeout(resolve, 300));
    proc.kill('SIGTERM'); await once(proc, 'exit');
    return sessionEvents(join(directory, 'home/sessions'), 'seed-root');
  };
  const idle = async () => activity.some(a => a.phase === 'waiting');
  try {
    // First start: the goal is created from the old goal.md; nothing to re-seed.
    const firstAll = await run('old', idle), first = firstAll.filter(e => e.type === 'goal/change');
    const created = first.filter(e => e.data.operation === 'create');
    assert.equal(created.length, 1);
    assert.equal(created[0].data.goal.objective, 'OLD mandate for alpha');
    assert.equal(first.filter(e => e.data.operation === 'edit').length, 0);
    assert.equal(await readSeed(), undefined, 'a plain create is not a re-seed');
    const goalId = created[0].data.goal.id;

    // goal.md edited + restart: the active goal follows it.
    const secondAll = await run('new', async () => (await readSeed()) !== undefined), second = secondAll.filter(e => e.type === 'goal/change');
    const edits = second.filter(e => e.data.operation === 'edit');
    assert.equal(edits.length, 1, JSON.stringify(second.map(e => e.data.operation)));
    assert.equal(second.filter(e => e.data.operation === 'create').length, 1, 'goal was recreated');
    assert.equal(edits[0].data.goal.id, goalId);
    assert.equal(edits[0].data.goal.phase, 'active');
    assert.equal(edits[0].data.goal.objective, 'NEW mandate for alpha');
    assert.equal(edits[0].data.goal.maxGoalRounds, created[0].data.goal.maxGoalRounds, 'the round cap changed');
    // Counters carry over: the edit sees exactly the goal rounds the first run started.
    const roundsBefore = firstAll.filter(e => e.type === 'user/message' && e.data.source?.kind === 'goal').length;
    assert.ok(roundsBefore >= 1, 'first run should have started a goal round');
    assert.equal(edits[0].data.roundsStarted, roundsBefore);
    assert.ok(second.some(e => e.data.operation === 'resume' && e.seq > edits[0].seq), 'disarmed -> resume did not follow the edit');
    const seed = await readSeed();
    assert.deepEqual(Object.keys(seed).sort(), ['at', 'goal_id', 'revision', 'sha256']);
    assert.equal(seed.sha256, createHash('sha256').update('NEW mandate for alpha\n').digest('hex'));
    assert.equal(seed.goal_id, goalId);
    assert.equal(seed.revision, edits[0].data.goal.revision);
    assert.ok(Number.isFinite(Date.parse(seed.at)));
    // The log line goes through ctx.logger.info, which the headless host does not echo; the record above is the observable evidence.

    // Restart again with the same goal.md: no further edit, the record stays.
    const third = (await run('new', idle)).filter(e => e.type === 'goal/change');
    assert.equal(third.filter(e => e.data.operation === 'edit').length, 1, 'same text was edited again');
    assert.deepEqual(await readSeed(), seed);
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of parked) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  }
});
