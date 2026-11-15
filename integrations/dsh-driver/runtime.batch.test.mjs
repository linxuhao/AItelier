/**
 * Todo batches end to end (core/todo_batch.py, design todo-batch-find-verify-20261114).
 *
 * 1. A real pinned DSH boots this driver with a fake model and a fake State server; the
 *    plan store is this repository's REAL `core.todo_cli` (a python with PyYAML, since
 *    batches come from a template). The root freezes `find_verify` with todo_batch_plan
 *    and dispatches the two finder prompts it returns, verbatim, to native children. Each
 *    finder reports three findings, one pair of them duplicates. One finder, still alive,
 *    keeps trying to take a verify sub-plan and is refused `distinct_from` once the root's
 *    todo_batch_status has expanded the stage: 5 unique findings, cap 4, so 4 verify
 *    sub-plans and 1 unverified, dispatched two at a time (max_parallel_verify 2). Fresh
 *    children verify; the root finishes the batch attempt; the candidate names the
 *    summary, which is not evidence and carries each verdict's artifact_ref.
 * 2. The startup snapshot after a host restart: a finder whose child died is listed in
 *    `batch_redispatch` with its prompt, the passed finder is not re-run, and a new child
 *    takes the dead one's sub-plan over.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { homedir, tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, execFileSync, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutCallerMode } from './dsh-entry.mjs';
import { DriverRuntime, TodoCli, batchRedispatchRows, batchesToRedispatch, todoSummary } from './runtime.mjs';
import { watchProgress } from './progress.mjs';

const REPO_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const TODO_ROOT = join(REPO_ROOT, 'integrations', 'dsh-director', 'todo');
/** A python that can run `core.todo_cli` against a template: it needs PyYAML. */
const PYTHON = [process.env.AITELIER_TODO_TEST_PYTHON, 'python3', '/usr/bin/python3']
  .filter(Boolean).find(python => spawnSync(python, ['-c', 'import yaml'], { stdio: 'ignore' }).status === 0);
process.env.PYTHONDONTWRITEBYTECODE = '1';     // the CLI runs from this checkout: leave no bytecode in it
const git = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();

async function checkout(path) {
  await mkdir(path, { recursive: true });
  git(path, 'init', '-q');
  await writeFile(join(path, 'app.py'), 'x = 1\ny = 2\n');
  await writeFile(join(path, 'other.py'), 'o = 1\n');
  git(path, 'add', '-A');
  git(path, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '-m', 'base');
  return git(path, 'rev-parse', 'HEAD');
}

/** find_verify exactly as shipped, except two verifiers at a time so the dispatch list is cut. */
async function templates(directory) {
  await mkdir(directory, { recursive: true });
  const shipped = await readFile(join(REPO_ROOT, 'todo_templates', 'find_verify.yaml'), 'utf8');
  assert.ok(shipped.includes('max_parallel_verify: 4'));
  await writeFile(join(directory, 'find_verify.yaml'), shipped.replace('max_parallel_verify: 4', 'max_parallel_verify: 2'));
  return directory;
}

// correctness/f3 and security/s1 are the same (file, line, claim): one verifier for both.
const FINDINGS = {
  correctness: [
    { id: 'f1', file: 'app.py', line: 1, claim: 'x is never validated', failure_scenario: 'x = None crashes' },
    { id: 'f2', file: 'app.py', line: 2, claim: 'y overflows', failure_scenario: 'y = 2**64' },
    { id: 'f3', file: 'other.py', line: 1, claim: 'o is read before it is set', failure_scenario: 'import order' },
  ],
  security: [
    { id: 's1', file: 'other.py', line: 1, claim: 'o is read before it is set', failure_scenario: 'import' },
    { id: 's2', file: 'app.py', claim: 'secrets are logged', failure_scenario: 'log shipping' },
    { id: 's3', file: 'other.py', line: 9, claim: 'path traversal', failure_scenario: 'dot dot slash' },
  ],
};

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-batch-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.AITELIER_URL + path, { method: 'POST', body: JSON.stringify(body) });
const T = JSON.parse(process.env.AITELIER_TEST_BATCH);
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const writeJson = (id, path, value) => tool(id, 'bash', { description: 'fixture report', run_in_background: false,
  command: "mkdir -p \"$(dirname '" + path + "')\" && echo " + Buffer.from(JSON.stringify(value)).toString('base64') + " | base64 -d > '" + path + "'" });
const text = value => ({ type: 'text', text: value });
export function apply(ctx) {
  const root = process.env.AITELIER_DSH_SESSION;
  const results = new Map(), children = new Map(), spawned = new Set(), done = new Set(), statusIds = [];
  let lastWasStatus = false;
  const onceBlock = (key, block) => done.has(key) ? undefined : (done.add(key), block);
  // The root reads its own tool results the way a model would: the dispatch lists in them.
  const rootBlock = () => {
    const plan = results.get('root-batch-plan');
    if (!plan) return onceBlock('plan', tool('root-batch-plan', 'todo_batch_plan', T.input)) ?? text('Director idle.');
    const views = [plan, ...statusIds.map(id => results.get(id)).filter(Boolean)];
    for (const view of views) for (const entry of view.dispatch ?? []) if (!spawned.has(entry.subplan)) {
      spawned.add(entry.subplan);
      return tool('root-spawn-' + entry.subplan, 'subagent', { description: 'batch child',
        prompt: entry.prompt + '\nprompt_sha256: ' + entry.prompt_sha256 });
    }
    if (results.has('root-finish')) return onceBlock('root-done', text('ROOT_DONE')) ?? text('Director idle.');
    if (views.at(-1).complete) return onceBlock('finish', tool('root-finish', 'todo_finish', { attempt_id: T.attempt })) ?? text('Director idle.');
    if (lastWasStatus) { lastWasStatus = false; return text('Director idle.'); }
    lastWasStatus = true;
    const id = 'root-status-' + (statusIds.length + 1);
    statusIds.push(id);
    return tool(id, 'todo_batch_status', { attempt_id: T.attempt });
  };
  const childBlock = (session, all) => {
    if (!children.has(session)) children.set(session, { finder: all.includes('You are a FINDER'),
      sub: /Sub-plan: ([^\s\\"]+)/.exec(all)?.[1], report: /Report: ([^\s\\"]+)/.exec(all)?.[1],
      sha: /prompt_sha256: ([0-9a-f]{64})/.exec(all)?.[1], step: 0, probes: 0 });
    const c = children.get(session), step = c.step++, item = c.finder ? 'find' : 'verify';
    const key = c.sub.split(c.finder ? '~find-' : '~verify-')[1];
    const script = [tool('begin-' + c.sub, 'todo_begin', { attempt_id: c.sub, item_id: item, prompt_sha256: c.sha }),
      writeJson('write-' + c.sub, c.report, c.finder ? { findings: T.findings[key] }
        : { finding_id: key, verdict: 'confirmed', evidence: 'fixture read the code', artifact_ref: T.base }),
      tool('done-' + c.sub, 'todo_done', { attempt_id: c.sub, item_id: item })];
    if (step < script.length) return script[step];
    // The correctness finder, still alive after its own item passed, tries to verify a
    // finding until the stage exists - and is then refused, being a finder of this batch.
    if (c.finder && key === 'correctness' && results.get(c.probe)?.refused !== 'distinct_from' && c.probes < 600) {
      c.probe = 'probe-' + (++c.probes);
      return tool(c.probe, 'todo_begin', { attempt_id: T.attempt + '~verify-correctness_f1', item_id: 'verify' });
    }
    return text((c.finder ? 'FINDER_DONE ' : 'VERIFY_DONE ') + c.sub);
  };
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 200000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const all = JSON.stringify(options.messages);
      const role = options.sessionId === root ? 'root' : 'child';
      await post('/test/request', { session: options.sessionId, role, purpose: options.purpose ?? null,
        tools: (options.tools ?? []).map(t => t.name), last: JSON.stringify(options.messages.at(-1)).slice(0, 400) });
      let block;
      if (options.purpose === 'compaction') block = text('Checkpoint.');
      else if (!options.tools?.length) block = text('Fixture session');
      else block = role === 'root' ? rootBlock() : childBlock(options.sessionId, all);
      if (block.text === 'ROOT_DONE') void post('/test/root-done', {});
      yield { type: 'block-start', index: 0, blockType: block.type };
      if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text };
      else yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments };
      yield { type: 'block-end', index: 0, block };
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } };
      yield { type: 'finish', reason: { kind: block.type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
  ctx.on('session/event', (session, event) => {
    if (event.type === 'tool/result') {
      const body = event.data.message.content.map(part => part.text ?? '').join('');
      let value; try { value = JSON.parse(body); } catch { value = body; }
      results.set(event.data.message.toolCallId, value);
    }
    if (['tool/result', 'user/message'].includes(event.type)) void post('/test/event', { session: session.id, event });
  });
}
`;

const toolJson = event => JSON.parse(event.data.message.content.map(block => block.text ?? '').join(''));

test('real pinned DSH: a find_verify batch fans out, expands, refuses a finder as verifier, cuts dispatch and finishes once', { timeout: Infinity }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;                       // nothing pinned is installed: reported unmeasured
  assert.ok(PYTHON, 'no python with PyYAML (python3, /usr/bin/python3 or AITELIER_TODO_TEST_PYTHON) to run core.todo_cli');
  const directory = await mkdtemp(join(homedir(), 'dsh-batch-'));
  const here = dirname(fileURLToPath(import.meta.url));
  const requests = [], events = [], activity = [], parked = new Set();
  let rootDone = false, output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [] } });
    if (req.url === '/api/state/query/get_attempt') return send({ attempt_id: body.attempt_id, status: 'running' });
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture', event_seq: 10,
      nodes: [{ name: 'n', status: 'PENDING' }], project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'fixture driver skill' });
    if (req.url.endsWith('/activity')) { activity.push(body); return send({ ok: true }); }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url === '/api/state/query/wait_for_state_change') { parked.add(res); res.on('close', () => parked.delete(res)); return; }
    if (req.url === '/test/request') { requests.push(body); return send({ ok: true }); }
    if (req.url === '/test/event') { events.push(body); return send({ ok: true }); }
    if (req.url === '/test/root-done') { rootDone = true; return send({ ok: true }); }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const work = join(directory, 'checkout');
    const reviewed = await checkout(work);
    const attempt = 'attempt-batch-review';
    const input = { template: 'find_verify', goal: 'Review the fixture change', attempt_id: attempt, node_key: 'review.fixture',
      context_hash: 'c'.repeat(64), base_sha: reviewed, workspace: work,
      params: { target: 'the fixture change', reviewed_sha: reviewed, dimensions: ['correctness', 'security'], max_verify: 4, scope: ['**'] } };
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-batch-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { id: 'tool-subagent', config: { provider: 'spawn', toolName: 'subagent', agentOptions: { provider: 'fixture', model: 'worker' } } },
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-batch-provider.mjs') }, { id: 'director', name: join(here, '../dsh-director/index.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') }] },
    ]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutCallerMode(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'fixture-root',
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'),
      AITELIER_DSH_EXECUTION: 'host', DSH_PERMISSION_MODE: 'danger-full-access', AITELIER_DSH_IDLE_REVIEW_MS: '3000',
      AITELIER_TODO_PYTHON: PYTHON, AITELIER_TODO_CWD: TODO_ROOT, AITELIER_TODO_TEMPLATES: await templates(join(directory, 'templates')),
      AITELIER_TEST_BATCH: JSON.stringify({ attempt, base: reviewed, input, findings: FINDINGS }) } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);

    const resultOf = id => events.find(e => e.event.type === 'tool/result' && e.event.data.message.toolCallId === id);
    const json = id => toolJson(resultOf(id).event);
    const passed = prefix => events.filter(e => e.event.type === 'tool/result' && e.event.data.message.toolCallId.startsWith(prefix)
      && !e.event.data.message.isError && toolJson(e.event).outcome === 'pass').length;
    const statusResults = () => events.filter(e => e.session === 'fixture-root' && e.event.type === 'tool/result'
      && e.event.data.message.toolCallId.startsWith('root-status-')).map(e => toolJson(e.event));
    const expanded = () => statusResults().find(s => s.expanded);
    const refusedAsFinder = () => events.find(e => e.event.type === 'tool/result' && e.event.data.message.toolCallId.startsWith('probe-')
      && toolJson(e.event).refused === 'distinct_from');
    const notices = reason => events.filter(e => e.session === 'fixture-root' && e.event.type === 'user/message'
      && e.event.data.content?.[0]?.text?.startsWith('AItelier todo batch:')
      && JSON.parse(e.event.data.content[0].text.split('\n').slice(1).join('\n')).reason === reason);
    const diagnostics = () => output.slice(-6000) + '\n' + JSON.stringify(requests.slice(-40), null, 1).slice(-8000);
    const progress = await watchProgress({
      what: 'the find_verify batch to fan out, expand, verify and finish',
      running: () => proc.exitCode === null,
      failed: () => activity.some(a => a.phase === 'failed'),
      milestones: [
        { name: 'root froze the batch', met: () => resultOf('root-batch-plan') },
        { name: 'both finders passed their find item', met: () => passed('done-' + attempt + '~find-') >= 2 },
        { name: 'root expanded the verify stage', met: () => expanded() },
        { name: 'a finder was refused as verifier (distinct_from)', met: () => refusedAsFinder() },
        { name: 'two verifiers passed', met: () => passed('done-' + attempt + '~verify-') >= 2 },
        { name: 'all four verifiers passed', met: () => passed('done-' + attempt + '~verify-') >= 4 },
        { name: 'the runtime said the batch is complete', met: () => notices('batch_complete').length },
        { name: 'root finished the batch attempt', met: () => resultOf('root-finish') },
        { name: 'root reported done', met: () => rootDone },
      ],
    });
    assert.equal(proc.exitCode, null, output);
    assert.ok(!activity.some(a => a.phase === 'failed'), JSON.stringify(activity.filter(a => a.phase === 'failed')) + output);
    assert.ok(progress.done, `${progress.message}\n` + diagnostics());

    // The freeze: one finder per dimension, both prompts dispatched at once (max_parallel 4).
    const plan = json('root-batch-plan');
    assert.equal(plan.refused, undefined, JSON.stringify(plan));
    assert.deepEqual(plan.dispatch.map(d => d.subplan), [attempt + '~find-correctness', attempt + '~find-security']);
    assert.ok(plan.dispatch.every(d => d.prompt.includes('You are a FINDER') && d.prompt.includes(reviewed)));

    // The expansion: 6 findings, one duplicate pair, cap 4 -> 4 verifiers and 1 unverified,
    // and only two of them offered at a time.
    const first = expanded();
    const keys = ['correctness_f1', 'correctness_f2', 'correctness_f3', 'security_s2'];
    assert.deepEqual(first.subplans.filter(s => s.stage === 'verify').map(s => s.item), keys);
    assert.equal(first.unverified, 1);
    assert.deepEqual(first.dispatch.map(d => d.subplan), keys.slice(0, 2).map(k => attempt + '~verify-' + k));
    assert.deepEqual(first.pending, keys.slice(2).map(k => attempt + '~verify-' + k));
    assert.ok(notices('batch_expand_due').length || statusResults().length > 1, 'the root learned the finders were done');
    const refusal = toolJson(refusedAsFinder().event);
    assert.equal(refusal.bound_subplan, attempt + '~find-correctness', JSON.stringify(refusal));
    // Every verifier ran in a session that never held a finder.
    const record = JSON.parse(await readFile(join(directory, 'todo', attempt + '.batch.json'), 'utf8'));
    const bound = Object.entries(record.bindings);
    const finderSessions = bound.filter(([, sub]) => sub.includes('~find-')).map(([session]) => session);
    assert.equal(finderSessions.length, 2);
    assert.equal(bound.filter(([, sub]) => sub.includes('~verify-')).length, 4);
    assert.ok(bound.filter(([, sub]) => sub.includes('~verify-')).every(([session]) => !finderSessions.includes(session)));
    // Each binding carries the sha of the prompt the child was given, and it is the runtime's.
    const shaOf = Object.fromEntries([...record.find, ...record.verify].map(e => [e.id, e.prompt_sha256]));
    assert.equal(record.binding_log.length, 6);
    for (const entry of record.binding_log) assert.equal(entry.prompt_sha256, shaOf[entry.subplan], JSON.stringify(entry));
    assert.deepEqual(plan.dispatch.map(d => d.prompt_sha256), record.find.map(e => e.prompt_sha256));

    // One candidate for the one attempt; the summary is not evidence and names each verdict's artifact.
    const finish = json('root-finish');
    assert.equal(finish.refused, undefined, JSON.stringify(finish));
    assert.equal(finish.artifact, reviewed);
    const summaryBytes = await readFile(finish.batch.summary);
    assert.equal(finish.batch.summary_sha256, createHash('sha256').update(summaryBytes).digest('hex'));
    const summary = JSON.parse(summaryBytes);
    assert.equal(summary.evidence, false);
    assert.deepEqual(summary.findings.map(f => [f.finding_id, f.verdict, f.artifact_ref]), keys.map(k => [k, 'confirmed', reviewed]));
    assert.deepEqual(summary.unverified.map(f => f.finding_id), ['security_s3']);
    const candidate = JSON.parse(await readFile(finish.report, 'utf8'));
    assert.equal(candidate.batch.evidence, false);
    git(work, 'bundle', 'verify', finish.git_bundle.path);

    // Batch tools are the director's; children only get the worker tools.
    const toolsOf = role => requests.find(r => r.role === role && !r.purpose && r.tools.length).tools;
    for (const name of ['todo_batch_plan', 'todo_batch_status']) {
      assert.ok(toolsOf('root').includes(name), 'root lacks ' + name);
      assert.ok(!toolsOf('child').includes(name), 'a child sees ' + name);
    }
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of parked) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  }
});

test('after a host restart the snapshot lists the dead finder for re-dispatch and never re-runs the passed one', { timeout: 120000 }, async () => {
  assert.ok(PYTHON, 'no python with PyYAML (python3, /usr/bin/python3 or AITELIER_TODO_TEST_PYTHON) to run core.todo_cli');
  const directory = await mkdtemp(join(tmpdir(), 'todo-batch-restart-'));
  try {
    const work = join(directory, 'checkout');
    const reviewed = await checkout(work);
    const todo = new TodoCli({ python: PYTHON, cwd: TODO_ROOT, stateDir: directory });
    const attempt = 'attempt-restart';
    const cli = async args => { const { code, result } = await todo.run(args); assert.equal(code, 0, JSON.stringify(result)); return result; };
    await cli(['batch-create', '--template=find_verify', '--params=' + JSON.stringify({ target: 't', reviewed_sha: reviewed,
      dimensions: ['correctness', 'security'], scope: ['**'] }), '--attempt=' + attempt, '--node-key=review.fixture',
      '--context-hash=' + 'c'.repeat(64), '--base-sha=' + reviewed, '--workspace=' + work, '--goal=review']);
    const reports = join(directory, 'todo', 'batch', attempt, 'reports');
    await mkdir(reports, { recursive: true });
    await writeFile(join(reports, 'find-correctness.json'), JSON.stringify({ findings: FINDINGS.correctness }));
    for (const step of ['begin', 'done']) await cli([step, '--attempt=' + attempt + '~find-correctness', '--item=find', '--session=child-a']);
    await cli(['begin', '--attempt=' + attempt + '~find-security', '--item=find', '--session=child-b']);
    const passedAt = JSON.parse(await readFile(join(directory, 'todo', attempt + '~find-correctness.json'), 'utf8')).items.find.observed_at;

    // The host restarted: no native child survived, the root is back.
    const live = ['fixture-root'];
    const listed = await todo.plans();
    assert.deepEqual(batchesToRedispatch(listed, new Set(live)), [attempt]);
    assert.deepEqual(batchesToRedispatch(listed, new Set([...live, 'child-b'])), [], 'a live finder is not re-dispatched');
    const runtime = new DriverRuntime({ project: 'alpha', driver: 'driver-a', directory,
      request: async path => path.endsWith('/me') ? { driver_id: 'driver-a' } : { event_seq: 1, nodes: [], project_note: {}, private_note: {} },
      todoPlans: async () => todoSummary(await todo.plans(), new Set(live)),
      batchRedispatch: async () => batchRedispatchRows(todo, await todo.plans(), live) });
    await runtime.init();
    const snapshot = JSON.parse(runtime.render());
    assert.deepEqual(snapshot.batch_redispatch.map(r => [r.attempt_id, r.subplan, r.dead_session]),
      [[attempt, attempt + '~find-security', 'child-b']]);
    assert.ok(snapshot.batch_redispatch[0].prompt.includes('Sub-plan: ' + attempt + '~find-security'));
    assert.equal(snapshot.todo.find(p => p.attempt_id === attempt + '~find-correctness'), undefined, 'the passed finder is not work');

    // The new child takes the dead one's sub-plan over; the passed finder was never measured again.
    await writeFile(join(reports, 'find-security.json'), JSON.stringify({ findings: FINDINGS.security }));
    const rebound = await cli(['begin', '--attempt=' + attempt + '~find-security', '--item=find', '--session=child-c', '--rebind']);
    assert.equal(rebound.rebound_from, 'child-b');
    assert.equal((await cli(['done', '--attempt=' + attempt + '~find-security', '--item=find', '--session=child-c'])).outcome, 'pass');
    assert.equal(JSON.parse(await readFile(join(directory, 'todo', attempt + '~find-correctness.json'), 'utf8')).items.find.observed_at, passedAt);
    const rows = await todo.plans();
    assert.deepEqual(rows.filter(r => r.kind === 'batch').map(r => [r.attempt_id, r.remaining]), [[attempt, ['expand']]]);
  } finally { await rm(directory, { recursive: true, force: true }); }
});
