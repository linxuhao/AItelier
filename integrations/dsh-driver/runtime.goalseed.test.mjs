import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { mkdtemp, mkdir, writeFile, readFile, readdir, copyFile, rm, cp, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutHostWiring } from './dsh-entry.mjs';
import { execFileSync } from 'node:child_process';
import { bindDriverGoal, goalMdCommit, goalOverrideFile, loadGoalTemplate, GOAL_OVERRIDE_NAME, ROUND_TOP_UP_BELOW, ROUND_TOP_UP_TO } from './goal.mjs';

// A stateful stand-in for the native goals service: edit() bumps the revision
// and keeps everything else, as the pinned dsh-goal does.
function fakeGoals(initial) {
  const state = { goal: initial, edits: [], resumes: 0, creates: 0 };
  state.get = () => state.goal;
  state.edit = (_agent, ref, request) => {
    assert.deepEqual(ref, { id: state.goal.id, revision: state.goal.revision });
    state.edits.push(request);
    return state.goal = { ...state.goal, revision: state.goal.revision + 1,
      ...(request.objective === undefined ? {} : { objective: request.objective.trim() }),
      ...(request.maxGoalRounds === undefined ? {} : { maxGoalRounds: request.maxGoalRounds }) };
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
  const view = bindDriverGoal(goals, {}, 'NEW mandate\n', { onSeed: (after, before) => seeded.push({ after, before }) });
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
  assert.equal(seeded[0].after.id, 'g1');
  assert.equal(seeded[0].after.revision, 8);
  assert.equal(seeded[0].before.revision, 7);
  assert.equal(seeded[0].before.objective, 'OLD mandate');
});

test('goal_md_commit is the commit of goal.md, falls back to AITELIER_TODO_CWD, and is "unknown" without git', async () => {
  const repo = await mkdtemp(join(tmpdir(), 'goalmd-repo-'));
  const plain = await mkdtemp(join(tmpdir(), 'goalmd-plain-'));
  try {
    const run = (...args) => execFileSync('git', ['-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@t', ...args], { encoding: 'utf8' }).trim();
    run('init', '-q');
    await mkdir(join(repo, 'integrations/dsh-driver'), { recursive: true });
    await writeFile(join(repo, 'integrations/dsh-driver/goal.md'), 'v1\n');
    await writeFile(join(repo, 'other.txt'), 'x');
    run('add', '.'); run('commit', '-q', '-m', 'goal');
    const goalCommit = run('rev-parse', 'HEAD');
    await writeFile(join(repo, 'other.txt'), 'y'); run('commit', '-q', '-am', 'unrelated');
    const goalFile = join(repo, 'integrations/dsh-driver/goal.md');
    assert.equal(await goalMdCommit(goalFile), goalCommit, 'must be the last commit touching goal.md, not HEAD');
    // A copy outside any repository resolves through the configured checkout.
    await writeFile(join(plain, 'goal.md'), 'v1\n');
    assert.equal(await goalMdCommit(join(plain, 'goal.md')), 'unknown');
    assert.equal(await goalMdCommit(join(plain, 'goal.md'), { cwd: repo }), goalCommit);
    // git unavailable, a missing file, a missing directory: 'unknown', never a throw.
    assert.equal(await goalMdCommit(goalFile, { env: { PATH: plain } }), 'unknown');
    assert.equal(await goalMdCommit(join(repo, 'nope/goal.md')), 'unknown');
    assert.equal(await goalMdCommit(join(plain, 'goal.md'), { cwd: join(plain, 'missing') }), 'unknown');
  } finally { await rm(repo, { recursive: true, force: true }); await rm(plain, { recursive: true, force: true }); }
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

test('an active goal near its round cap gets its cap raised in the same edit, then resumes', () => {
  assert.equal(ROUND_TOP_UP_BELOW, 64);
  assert.equal(ROUND_TOP_UP_TO, 256);
  // [roundsStarted, rendered objective, expected edit request]
  for (const [rounds, rendered, expected] of [
    [256, 'NEW mandate', { objective: 'NEW mandate', maxGoalRounds: 512 }], // exhausted + goal.md changed: one edit carries both
    [256, 'OLD mandate', { maxGoalRounds: 512 }],                         // exhausted, objective unchanged: cap only
    [193, 'OLD mandate', { maxGoalRounds: 449 }],                         // 63 left: below the threshold
    [300, 'OLD mandate', { maxGoalRounds: 556 }],                         // already past the cap
  ]) {
    const goals = fakeGoals(active({ roundsStarted: rounds })), seeded = [];
    goals.block = () => assert.fail('a healthy resident goal must not be blocked by its round count');
    const view = bindDriverGoal(goals, {}, rendered, { onSeed: (after, before) => seeded.push({ after, before }) });
    assert.deepEqual(goals.edits, [expected], `rounds ${rounds}`);
    assert.equal(seeded.length, 1);
    assert.equal(seeded[0].before.maxGoalRounds, 256);
    assert.equal(seeded[0].after.maxGoalRounds, rounds + 256);
    assert.equal(seeded[0].after.revision, 8, 'one edit, one revision');
    // id, phase and the counter are untouched: the budget is raised, never reset.
    assert.equal(view.id, 'g1');
    assert.equal(view.roundsStarted, rounds);
    assert.equal(view.phase, 'active');
    assert.equal(goals.resumes, 1);
    assert.equal(view.activation, 'armed');
    assert.equal(view.revision, 9, 'resume is addressed to the edited revision');
  }
  // Exactly at the threshold (64 left) nothing is edited.
  const goals = fakeGoals(active({ roundsStarted: 192 }));
  bindDriverGoal(goals, {}, 'OLD mandate', { onSeed: () => assert.fail('no edit expected') });
  assert.deepEqual(goals.edits, []);
  assert.equal(goals.resumes, 1);
  // A goal that is already blocked (round-limit or otherwise) is an explicit state: left alone.
  const blocked = fakeGoals(active({ phase: 'blocked', roundsStarted: 256, blockedReason: { code: 'round-limit', message: 'x' } }));
  assert.equal(bindDriverGoal(blocked, {}, 'OLD mandate'), blocked.goal);
  assert.deepEqual(blocked.edits, []);
  assert.equal(blocked.resumes, 0);
});

// ---- real pinned DSH: the host service's restart path --------------------------------

const here = dirname(fileURLToPath(import.meta.url));

async function sessionEvents(root, session, entry) {
  // Read the persisted session through the DSH that produced it: the entry the
  // harness measured, never the caller's shell and never a hard-coded install.
  const require = createRequire(entry);
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

// One fixture directory, two driver copies (old/new goal.md), a fake AItelier API, and run(variant, until)
// = start the real pinned host on that copy, wait, SIGTERM it, return the persisted session events.
// goalRows are extra profile rows, e.g. a smaller native default round cap.
async function driverHarness(t, goalRows = []) {
  const entry = await measuredEntryForTest(t);
  if (!entry) return null;                  // nothing pinned is installed: reported unmeasured
  const directory = await mkdtemp(join(tmpdir(), 'dsh-goalseed-'));
  // Two copies of the driver that differ only in goal.md stand for "goal.md edited, host service restarted".
  const variants = {};
  // Both copies import the director plugin as ../dsh-director: one shared sibling copy.
  await cp(join(here, '..', 'dsh-director'), join(directory, 'dsh-director'), { recursive: true, filter: source => !source.includes('node_modules') });
  for (const [name, mandate] of [['old', 'OLD mandate for {{project_id}}\n'], ['new', 'NEW mandate for {{project_id}}\n']]) {
    const dir = variants[name] = join(directory, 'driver-' + name);
    await mkdir(dir);
    for (const file of await readdir(here)) if ((/\.(mjs|md)$/.test(file) || file === 'roles.json') && !/\.test\.mjs$/.test(file) && file !== 'goal.md') await copyFile(join(here, file), join(dir, file));
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
  const profile = variant => ['driver-' + variant, JSON.stringify([{ id: 'hmr', disabled: true }, ...goalRows, { insert: [{ id: 'director', name: join(directory, 'dsh-director', 'index.mjs') }, { id: 'driver', name: join(variants[variant], 'runtime.mjs') }] }])];
  for (const variant of ['old', 'new']) {
    const dir = join(directory, 'home/profiles/driver-' + variant); await mkdir(dir, { recursive: true });
    await writeFile(join(dir, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(dir, 'cordis.patch.yml'), profile(variant)[1]);
  }
  // The deployment's data root, so a mandate override on the machine running the suite never leaks in.
  const aitelierHome = join(directory, 'aitelier-home'); await mkdir(aitelierHome);
  const seedFile = join(directory, 'goal-seed.jsonl');
  const sha = text => createHash('sha256').update(text).digest('hex');
  const readSeeds = async () => { try { return (await readFile(seedFile, 'utf8')).split('\n').filter(Boolean).map(line => JSON.parse(line)); } catch (error) { if (error.code === 'ENOENT') return []; throw error; } };
  const start = variant => {
    activity.length = 0;
    proc = spawn(process.execPath, [entry, '--profile', 'driver-' + variant], { cwd: directory, env: { ...withoutHostWiring(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'seed-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'seed-root',
      AITELIER_DSH_PROVIDER: 'aitelier', AITELIER_DSH_MODEL: 'driver', AITELIER_URL: `http://127.0.0.1:${server.address().port}`,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'absent'), AITELIER_HOME: aitelierHome } });
    proc.stdout.on('data', c => output += c); proc.stderr.on('data', c => output += c);
  };
  // A start that must end by itself: its exit code and everything it printed.
  const crash = async variant => {
    output = ''; start(variant);
    const [code] = await Promise.race([once(proc, 'exit'), new Promise(resolve => setTimeout(() => resolve(['still running']), 20000))]);
    return { code, output };
  };
  const run = async (variant, until) => {
    start(variant);
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline && proc.exitCode === null && !(await until())) await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(proc.exitCode, null, output);
    assert.ok(await until(), 'host never reached the expected state\n' + output);
    await new Promise(resolve => setTimeout(resolve, 300));
    proc.kill('SIGTERM'); await once(proc, 'exit');
    return sessionEvents(join(directory, 'home/sessions'), 'seed-root', entry);
  };
  const idle = async () => activity.some(a => a.phase === 'waiting');
  const close = async () => {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of parked) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  };
  return { run, crash, idle, readSeeds, sha, close, aitelierHome, variants };
}

test('real pinned DSH: restart re-seeds an active director goal from goal.md exactly once', { timeout: 60000 }, async (t) => {
  const harness = await driverHarness(t);
  if (!harness) return;
  const { run, idle, readSeeds, sha, close } = harness;
  try {
    // First start: the goal is created from the old goal.md; nothing to re-seed.
    const firstAll = await run('old', idle), first = firstAll.filter(e => e.type === 'goal/change');
    const created = first.filter(e => e.data.operation === 'create');
    assert.equal(created.length, 1);
    assert.equal(created[0].data.goal.objective, 'OLD mandate for alpha');
    assert.equal(first.filter(e => e.data.operation === 'edit').length, 0);
    assert.deepEqual(await readSeeds(), [], 'a plain create is not a re-seed');
    const goalId = created[0].data.goal.id;

    // goal.md edited + restart: the active goal follows it.
    const secondAll = await run('new', async () => (await readSeeds()).length > 0), second = secondAll.filter(e => e.type === 'goal/change');
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
    const seeds = await readSeeds();
    assert.equal(seeds.length, 1);
    const [seed] = seeds;
    assert.deepEqual(Object.keys(seed).sort(), ['at', 'goal_id', 'goal_md_commit', 'goal_md_file', 'goal_md_override', 'goal_md_sha256', 'new_max_goal_rounds', 'new_objective_sha256', 'new_revision', 'old_max_goal_rounds', 'old_objective_sha256', 'old_revision', 'rounds_started']);
    assert.equal(seed.goal_md_override, false, 'no override in the deployment directory: the default was loaded');
    assert.equal(seed.goal_md_file, join(harness.variants.new, 'goal.md'));
    // Far from the cap: the objective edit leaves the cap alone, and the record says so.
    assert.equal(seed.old_max_goal_rounds, created[0].data.goal.maxGoalRounds);
    assert.equal(seed.new_max_goal_rounds, seed.old_max_goal_rounds);
    assert.equal(seed.rounds_started, roundsBefore);
    assert.equal(seed.goal_id, goalId);
    assert.equal(seed.old_objective_sha256, sha(created[0].data.goal.objective), 'old digest is the start-1 objective');
    assert.equal(seed.old_objective_sha256, sha('OLD mandate for alpha'));
    assert.equal(seed.new_objective_sha256, sha('NEW mandate for alpha'));
    assert.equal(seed.new_objective_sha256, sha(edits[0].data.goal.objective));
    assert.equal(seed.goal_md_sha256, sha('NEW mandate for {{project_id}}\n'), 'digest of the goal.md file as loaded');
    assert.equal(seed.goal_md_commit, 'unknown', 'the copied driver is not in a git repository');
    assert.equal(seed.new_revision, edits[0].data.goal.revision);
    assert.ok(seed.old_revision < seed.new_revision);
    assert.ok(Number.isFinite(Date.parse(seed.at)));
    // The log line goes through ctx.logger.info, which the headless host does not echo; the record above is the observable evidence.

    // Restart again with the same goal.md: no further edit, the record stays.
    const third = (await run('new', idle)).filter(e => e.type === 'goal/change');
    assert.equal(third.filter(e => e.data.operation === 'edit').length, 1, 'same text was edited again');
    assert.deepEqual(await readSeeds(), seeds, 'a start with unchanged goal.md must append nothing');
  } finally { await close(); }
});

test('real pinned DSH: a start near the round cap raises the cap in place, once', { timeout: 60000 }, async (t) => {
  // A native default cap of 40 leaves 39 rounds after the first start: below ROUND_TOP_UP_BELOW.
  const harness = await driverHarness(t, [{ id: 'goal', config: { defaultMaxGoalRounds: 40 } }]);
  if (!harness) return;
  const { run, idle, readSeeds, sha, close } = harness;
  try {
    const firstAll = await run('old', idle), first = firstAll.filter(e => e.type === 'goal/change');
    const created = first.filter(e => e.data.operation === 'create');
    assert.equal(created.length, 1);
    assert.equal(created[0].data.goal.maxGoalRounds, 40, 'fixture: the profile row did not reach the goal service');
    const roundsBefore = firstAll.filter(e => e.type === 'user/message' && e.data.source?.kind === 'goal').length;
    assert.ok(roundsBefore >= 1 && 40 - roundsBefore < ROUND_TOP_UP_BELOW);

    // Same goal.md, restart: the only reason to edit is the cap.
    const secondAll = await run('old', async () => (await readSeeds()).length > 0), second = secondAll.filter(e => e.type === 'goal/change');
    const edits = second.filter(e => e.data.operation === 'edit');
    assert.equal(edits.length, 1, JSON.stringify(second.map(e => e.data.operation)));
    assert.equal(edits[0].data.goal.id, created[0].data.goal.id);
    assert.equal(edits[0].data.goal.phase, 'active');
    assert.equal(edits[0].data.goal.objective, 'OLD mandate for alpha', 'objective must not change');
    assert.equal(edits[0].data.goal.maxGoalRounds, roundsBefore + ROUND_TOP_UP_TO);
    assert.equal(edits[0].data.roundsStarted, roundsBefore, 'the counter is kept, not reset');
    assert.ok(second.some(e => e.data.operation === 'resume' && e.seq > edits[0].seq), 'disarmed -> resume did not follow the edit');
    assert.ok(!second.some(e => e.data.goal?.phase === 'blocked'), 'goal was blocked');
    const seeds = await readSeeds();
    assert.equal(seeds.length, 1);
    assert.equal(seeds[0].old_max_goal_rounds, 40);
    assert.equal(seeds[0].new_max_goal_rounds, roundsBefore + ROUND_TOP_UP_TO);
    assert.equal(seeds[0].rounds_started, roundsBefore);
    assert.equal(seeds[0].old_objective_sha256, seeds[0].new_objective_sha256);
    assert.equal(seeds[0].new_objective_sha256, sha('OLD mandate for alpha'));
    assert.equal(seeds[0].new_revision, edits[0].data.goal.revision);

    // A further start has plenty left: no edit, no record.
    const third = (await run('old', idle)).filter(e => e.type === 'goal/change');
    assert.equal(third.filter(e => e.data.operation === 'edit').length, 1, 'cap was raised again');
    assert.deepEqual(await readSeeds(), seeds);
  } finally { await close(); }
});

test('an unwritable trace target does not throw - the record is lost, the host is not', async () => {
  const { appendGoalSeedRecord } = await import('./goal.mjs');
  const directory = await mkdtemp(join(tmpdir(), 'dsh-seedblocked-'));
  // Deterministic EISDIR without touching permissions: the trace path IS a directory.
  await mkdir(join(directory, 'goal-seed.jsonl'));
  const result = await appendGoalSeedRecord(directory, { at: 'x', goal_id: 'g' });
  assert.equal(result.written, false);
  assert.equal(result.error, 'EISDIR');
  // and the happy path still writes
  const clean = await mkdtemp(join(tmpdir(), 'dsh-seedok-'));
  assert.deepEqual(await appendGoalSeedRecord(clean, { at: 'x', goal_id: 'g' }), { written: true });
  const written = (await readFile(join(clean, 'goal-seed.jsonl'), 'utf8')).trim();
  assert.deepEqual(JSON.parse(written), { at: 'x', goal_id: 'g' });
});

// ---- the deployment's mandate override (review 2026-11-24, 重-12) --------------------

const DEFAULT_GOAL = fileURLToPath(new URL('./goal.md', import.meta.url));

test('the override lives in the AItelier data root: $AITELIER_HOME, else ~/.AItelier', () => {
  assert.equal(GOAL_OVERRIDE_NAME, 'director-goal.md');
  assert.equal(goalOverrideFile({ AITELIER_HOME: '/srv/data' }), '/srv/data/director-goal.md');
  assert.match(goalOverrideFile({}), /\/\.AItelier\/director-goal\.md$/);
});

test('no override: the repository default is used, byte for byte', async () => {
  const home = await mkdtemp(join(tmpdir(), 'goal-none-'));
  try {
    const loaded = await loadGoalTemplate(DEFAULT_GOAL, { AITELIER_HOME: home });
    assert.deepEqual(loaded, { file: DEFAULT_GOAL, override: false, text: await readFile(DEFAULT_GOAL, 'utf8') });
    // A data root that does not exist yet is the same as one without an override.
    assert.equal((await loadGoalTemplate(DEFAULT_GOAL, { AITELIER_HOME: join(home, 'missing') })).override, false);
  } finally { await rm(home, { recursive: true, force: true }); }
});

test('an override wins, byte for byte (CJK, CRLF, trailing whitespace kept)', async () => {
  const home = await mkdtemp(join(tmpdir(), 'goal-override-'));
  try {
    const bytes = '你是 {{project_id}} 项目的常驻导演。\r\nline two  \n\n';
    await writeFile(join(home, GOAL_OVERRIDE_NAME), bytes);
    const loaded = await loadGoalTemplate(DEFAULT_GOAL, { AITELIER_HOME: home });
    assert.deepEqual(loaded, { file: join(home, GOAL_OVERRIDE_NAME), override: true, text: bytes });
    assert.ok(Buffer.from(loaded.text, 'utf8').equals(await readFile(join(home, GOAL_OVERRIDE_NAME))));
  } finally { await rm(home, { recursive: true, force: true }); }
});

test('an override that exists but cannot be used is an error naming it, never the default', async () => {
  const home = await mkdtemp(join(tmpdir(), 'goal-broken-'));
  const file = join(home, GOAL_OVERRIDE_NAME);
  const refused = async (why) => {
    const loaded = await loadGoalTemplate(DEFAULT_GOAL, { AITELIER_HOME: home });
    assert.equal(loaded.text, undefined, 'the default must not be returned in its place');
    assert.equal(loaded.override, true);
    assert.equal(loaded.file, file);
    assert.match(loaded.error.message, new RegExp(`director goal override ${file.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} ${why}; refusing to fall back`));
  };
  try {
    await mkdir(file);                                  // unreadable as a file, also when the suite runs as root
    await refused('cannot be read \\(EISDIR\\)');
    await rm(file, { recursive: true });
    await symlink(join(home, 'gone.md'), file);          // a dangling link: ENOENT, yet present
    await refused('is a link to a missing file');
    await rm(file);
    await writeFile(file, ' \n\t\n');                   // empty mandate
    await refused('is empty');
  } finally { await rm(home, { recursive: true, force: true }); }
});

test('the default mandate grants no authority and is neutral', async () => {
  const text = await readFile(DEFAULT_GOAL, 'utf8');
  assert.doesNotMatch(text, /[\u3000-\u9fff\uff00-\uffef]/, 'the default is English');
  // No vendor or model names: a deployment chooses its own.
  assert.doesNotMatch(text, /Claude|Sonnet|Opus|GPT|\bSol\b|Gemini|DeepSeek|OpenCode|Ollama|Codex/i);
  // No grant in any phrasing: nothing is "authorised", "allowed", "approved" or "permitted",
  // no "may push", no "without asking".
  assert.doesNotMatch(text, /authori[sz]ed|\b(?:is|are|has been|have been) (?:allowed|approved|permitted)\b|\bmay (?:push|publish|release|deploy|merge)\b|without (?:asking|approval|confirmation)/i);
  // This deployment's own rules are not the default's.
  assert.doesNotMatch(text, /AItelier|Godot|pytest|\b4\b|four/i);
  // It says so, and says that outward actions wait for the owner.
  assert.match(text, /grants no standing authority/);
  const askFirst = text.split('\n').find(line => line.includes('grants no standing authority'));
  for (const action of ['pushing', 'publishing', 'deploying']) assert.match(askFirst, new RegExp(`Ask the owner first[^.]*${action}`));
  assert.match(askFirst, /docs\/dsh-driver\.md/, 'it names where a deployment writes its own mandate');
});

test('real pinned DSH: a deployment override is the director goal; an unreadable one stops the director', { timeout: 90000 }, async (t) => {
  const harness = await driverHarness(t);
  if (!harness) return;
  const { run, crash, idle, readSeeds, sha, close, aitelierHome } = harness;
  const file = join(aitelierHome, GOAL_OVERRIDE_NAME);
  try {
    const created = (await run('old', idle)).filter(e => e.type === 'goal/change' && e.data.operation === 'create');
    assert.equal(created[0].data.goal.objective, 'OLD mandate for alpha');
    // The operator places a mandate: the next start follows it, not the repository's goal.md.
    const bytes = 'Deployment mandate for {{project_id}}: 业主授权范围见此。\n';
    await writeFile(file, bytes);
    const edits = (await run('old', async () => (await readSeeds()).length > 0)).filter(e => e.type === 'goal/change' && e.data.operation === 'edit');
    assert.equal(edits.length, 1);
    assert.equal(edits[0].data.goal.objective, 'Deployment mandate for alpha: 业主授权范围见此。');
    const [seed] = await readSeeds();
    assert.equal(seed.goal_md_override, true);
    assert.equal(seed.goal_md_file, file);
    assert.equal(seed.goal_md_sha256, sha(bytes), 'digest of the override as placed');
    assert.equal(seed.goal_md_commit, 'unknown', "an override outside a repository never reports the default's commit");
    // The override becomes unreadable: the director refuses to start, names the file, and the
    // persisted goal keeps the override's text (no silent re-seed to the default).
    await rm(file); await mkdir(file);
    const { code, output } = await crash('old');
    assert.equal(code, 1, output);
    assert.match(output, /AItelier DSH exit: exit code 1 \(dsh-director startup\): Error: director goal override \S+director-goal\.md cannot be read \(EISDIR\)/);
    assert.equal((await readSeeds()).length, 1, 'nothing was re-seeded');
    await rm(file, { recursive: true });
    await writeFile(file, bytes);
    const after = (await run('old', idle)).filter(e => e.type === 'goal/change');
    assert.equal(after.filter(e => e.data.operation === 'edit').length, 1, 'the stored objective is still the override');
  } finally { await close(); }
});
