import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutHostWiring } from './dsh-entry.mjs';

// The liveness predicate is what keeps a departed child from pinning dormancy: it
// drops ids it rejects, and the count in the reason is the number of ids that survive.
// runtime.mjs is imported dynamically so these stay independent of the real-DSH
// scenarios below, which run the whole runtime.
test('a departed child is dropped by the liveness predicate, not counted', async () => {
  const { dormancyRefusal } = await import('./runtime.mjs');
  const refusal = dormancyRefusal({ reason: 'x', liveChildren: 2, childSessions: ['ghost', 'live'], isLive: id => id === 'live' });
  assert.equal(refusal.refused, 'live_children');
  assert.equal(refusal.reason, '1 native child(ren) still running');
  assert.deepEqual(refusal.live_children, ['live']);
  assert.equal(dormancyRefusal({ reason: 'x', liveChildren: 1, childSessions: ['ghost'], isLive: () => false }), null);
});

test('pruning a live-child set leaves only the ids the registry still holds', async () => {
  const { pruneDepartedChildren } = await import('./runtime.mjs');
  const live = new Set(['ghost', 'live']);
  assert.deepEqual(pruneDepartedChildren(live, { isLocal: () => true, isLive: id => id === 'live' }), ['ghost']);
  assert.deepEqual([...live], ['live']);
  // A non-local activation owns no local Agent here, so nothing proves it gone.
  const remote = new Set(['remote']);
  assert.deepEqual(pruneDepartedChildren(remote, { isLocal: () => false, isLive: () => false }), []);
  assert.deepEqual([...remote], ['remote']);
});

test('the chosen sleep is an integer number of minutes between the review interval and a day', async () => {
  const { wakeAfterRefusal, DORMANT_MIN_MINUTES, DORMANT_MAX_MINUTES } = await import('./runtime.mjs');
  for (const ok of [DORMANT_MIN_MINUTES, 240, DORMANT_MAX_MINUTES]) assert.equal(wakeAfterRefusal(ok), null);
  for (const bad of [undefined, NaN, 0, DORMANT_MIN_MINUTES - 1, DORMANT_MAX_MINUTES + 1, 30.5, '60'])
    assert.equal(wakeAfterRefusal(bad)?.refused, 'wake_after_minutes_invalid', String(bad));
});

function review(overrides = {}) {
  const calls = { notify: [], changed: 0 };
  const agent = { status: 'idle' };
  const r = new IdleReviewClass({ agent, goals: { get: () => ({ phase: 'active' }), disarm() {} },
    notify: async (...args) => { calls.notify.push(args); }, onError: error => { throw error; },
    changed: () => { calls.changed++; }, intervalMs: 10_000, ...overrides });
  return { r, calls, agent };
}
let IdleReviewClass;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

test('dormancy that runs out ends itself and runs the parked review once, naming the timeout', async () => {
  ({ IdleReview: IdleReviewClass } = await import('./runtime.mjs'));
  const { r, calls } = review();
  const entered = r.enterDormancy('nothing to do', 60);
  assert.equal(Date.parse(entered.until) - Date.parse(entered.since), 60);
  assert.equal(calls.changed, 1);
  await sleep(150);
  assert.equal(r.dormant, undefined);
  assert.equal(calls.changed, 2);
  assert.equal(calls.notify.length, 1);
  assert.equal(calls.notify[0][0], 'dormancy_timeout');
  assert.equal(calls.notify[0][1].reason, 'nothing to do');
  r.close();
});

test('input before the sleep runs out cancels the timeout; a busy or paused director gets no review', async () => {
  ({ IdleReview: IdleReviewClass } = await import('./runtime.mjs'));
  const woken = review();
  woken.r.enterDormancy('x', 60);
  assert.ok(woken.r.wake());
  await sleep(150);
  assert.equal(woken.calls.notify.length, 0);
  assert.equal(woken.calls.changed, 2);
  const busy = review();
  busy.agent.status = 'running';
  busy.r.enterDormancy('x', 30);
  await sleep(100);
  assert.equal(busy.r.dormant, undefined, 'dormancy still ends');
  assert.equal(busy.calls.notify.length, 0);
  const paused = review({ goals: { get: () => ({ phase: 'paused' }), disarm() {} } });
  paused.r.enterDormancy('x', 30);
  await sleep(100);
  assert.equal(paused.r.dormant, undefined);
  assert.equal(paused.calls.notify.length, 0);
  const closed = review();
  closed.r.enterDormancy('x', 30);
  closed.r.close();
  await sleep(100);
  assert.equal(closed.calls.notify.length, 0);
});

// Root script: refused while a claim is live, then dormant with a padded context,
// then a State event wakes it. Compaction requests answer with a checkpoint.
const PROVIDER = String.raw`
import { createRequire } from 'node:module';
import { unlinkSync } from 'node:fs';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-dormancy-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.AITELIER_URL + path, { method: 'POST', body: JSON.stringify(body) });
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
export function apply(ctx) {
  const root = process.env.AITELIER_DSH_SESSION, done = new Set();
  // Lazy: a step with a side effect runs it only when that step is taken.
  const once = (key, block) => done.has(key) ? undefined : (done.add(key), typeof block === 'function' ? block() : block);
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 16000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      await post('/test/request', { session: options.sessionId, purpose: options.purpose ?? null });
      let block;
      if (options.purpose === 'compaction') block = { type: 'text', text: 'Checkpoint: dormant director, nothing in flight.' };
      else if (options.sessionId !== root && text.includes('DORMANCY_CHILD')) {
        // The child stays alive until the test releases it.
        await post('/test/hold', {});
        block = { type: 'text', text: 'CHILD_DONE' };
      }
      else if (!options.tools?.length || options.sessionId !== root) block = { type: 'text', text: 'Fixture session' };
      else block = once('nowake', tool('root-dormant-nowake', 'idle_dormant', { reason: 'quiet' }))
        ?? once('refused', tool('root-dormant-refused', 'idle_dormant', { reason: 'quiet', wake_after_minutes: 15 }))
        ?? once('empty', tool('root-dormant-empty', 'idle_dormant', { reason: '  ', wake_after_minutes: 15 }))
        ?? once('plans', tool('root-dormant-plans', 'idle_dormant', { reason: 'quiet', wake_after_minutes: 15 }))
        ?? once('spawn', () => (unlinkSync(process.env.AITELIER_TEST_PLAN_FILE), tool('root-spawn', 'subagent', { description: 'dormancy child', prompt: 'DORMANCY_CHILD stay alive' })))
        ?? once('children', tool('root-dormant-children', 'idle_dormant', { reason: 'quiet', wake_after_minutes: 15 }))
        ?? once('release', () => (void post('/test/release', {}), { type: 'text', text: 'WAITING_FOR_CHILD' }))
        ?? (text.includes('CHILD_DONE') ? once('pad', { type: 'text', text: 'ROOT_PADDING ' + 'director context '.repeat(1500) }) : undefined)
        ?? (done.has('woken') ? undefined : text.includes('ROOT_PADDING') && !done.has('dormant') ? (done.add('dormant'), tool('root-dormant', 'idle_dormant', { reason: 'nothing authorized is available', wake_after_minutes: 15 })) : undefined)
        ?? (text.includes('AItelier runtime event') && !done.has('woken') ? (done.add('woken'), { type: 'text', text: 'WOKEN' }) : undefined)
        ?? { type: 'text', text: 'Director idle.' };
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
    if (session.id === root && ['tool/result', 'user/message', 'compaction/end', 'assistant/message'].includes(event.type)) void post('/test/event', { event });
  });
}
`;

async function scenario(t, floor, { minuteMs } = {}) {
  const entry = await measuredEntryForTest(t);
  if (!entry) return null;                  // nothing pinned is installed: reported unmeasured
  const directory = await mkdtemp(join(homedir(), 'dsh-dormancy-'));
  // Unique per run: background-job detection matches processes by session, and other
  // real-DSH test files run their own drivers in parallel.
  const session = 'dormancy-root-' + process.pid + '-' + Math.random().toString(36).slice(2, 8);
  const here = dirname(fileURLToPath(import.meta.url));
  const events = [], requests = [], claimQueries = [], issueQueries = [], parked = new Set(), phases = [];
  const held = new Set();
  let changed = false, released = false, output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [] } });
    if (req.url === '/api/state/query/list_claims') {
      claimQueries.push(body);
      // Live claim on the first query only; another driver's claim never counts.
      return send({ claims: [...(claimQueries.length === 1 ? [{ node_key: 'n', status: 'live', driver_id: 'dsh-fixture' }] : []),
        { node_key: 'other', status: 'live', driver_id: 'someone-else' }] });
    }
    // Open issues, two pages: one linked to a node still to be done, one with a work item, and three untracked:
    // no node, or linked only to VERIFIED / SUPERSEDED nodes.
    if (req.url === '/api/state/query/list_issues') {
      issueQueries.push(body);
      const at = (id, kind, extra = {}) => ({ issue_id: id, kind, title: 'title of ' + id, status: 'open', nodes: [], work_item: null,
        created_at: id === 'iss-old' ? '2026-10-01T00:00:00Z' : id === 'iss-done' ? '2026-10-15T00:00:00Z' : '2026-11-20T00:00:00Z', ...extra });
      return send(body.after === 0
        ? { issues: [at('iss-linked', 'defect', { nodes: [{ node_key: 'n', status: 'READY' }, { node_key: 'm', status: 'VERIFIED' }] }), at('iss-new', 'gap')], has_more: true, next_after: 7, status_counts: { open: 5, fixed: 9 } }
        : { issues: [at('iss-old', 'gap'), at('iss-worked', 'defect', { work_item: 'issue.iss-worked' }),
          at('iss-done', 'defect', { nodes: [{ node_key: 'v', status: 'VERIFIED' }, { node_key: 's', status: 'SUPERSEDED' }] })], has_more: false, next_after: 9, status_counts: { open: 5, fixed: 9 } });
    }
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture', event_seq: 10,
      nodes: [{ name: 'n', status: changed ? 'VERIFIED' : 'PENDING' }], project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'fixture driver skill' });
    if (req.url.endsWith('/activity')) { phases.push({ phase: body.phase, at: Date.now(), events: events.length }); return send({ ok: true }); }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url === '/api/state/query/wait_for_state_change') {
      if (changed && body.after < 11) return send({ timed_out: false, next_after: 11, next_inbox_after: 0, events: [{ type: 'node_changed' }] });
      parked.add(res); res.on('close', () => parked.delete(res)); return;
    }
    if (req.url === '/test/hold') { if (released) return send({ ok: true }); held.add(res); return; }
    if (req.url === '/test/release') { released = true; for (const target of held) { target.setHeader('content-type', 'application/json'); target.end('{}'); } held.clear(); return send({ ok: true }); }
    if (req.url === '/test/request') { requests.push(body); return send({ ok: true }); }
    if (req.url === '/test/event') { events.push(body.event); return send({ ok: true }); }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const change = () => {
    changed = true;
    for (const target of parked) { target.setHeader('content-type', 'application/json'); target.end(JSON.stringify({ timed_out: false, next_after: 11, next_inbox_after: 0, events: [{ type: 'node_changed' }] })); }
    parked.clear();
  };
  const result = id => events.find(e => e.type === 'tool/result' && e.data.message.toolCallId === id);
  const json = event => JSON.parse(event.data.message.content.map(b => b.text ?? '').join(''));
  const reviews = () => events.filter(e => e.type === 'user/message' && e.data.content?.[0]?.text?.startsWith('AItelier director review due:'));
  // A real pinned DSH is a separate process: on a loaded host one fixture turn can take far
  // longer than a fixed wall budget while the scenario is making progress. Wait for the
  // condition, with a watchdog on *quiet* rather than on total time: every new request, event,
  // phase or line of output buys another idle window. The cap only catches a wedged driver.
  const progress = () => `${requests.length}/${events.length}/${phases.length}/${claimQueries.length}/${output.length}`;
  const until = async (what, predicate, quietMs = 45000) => {
    const cap = Date.now() + 180000;
    let mark = progress(), quietSince = Date.now();
    while (Date.now() < cap && proc.exitCode === null && !predicate()) {
      await sleep(50);
      const seen = progress();
      if (seen !== mark) { mark = seen; quietSince = Date.now(); }
      if (Date.now() - quietSince > quietMs) break;
    }
    assert.equal(proc.exitCode, null, output);
    assert.ok(predicate(), `${what}\nREQ ${JSON.stringify(requests.map(r => [r.session === session ? 'root' : r.session.slice(0, 8), r.purpose]))}\n${output}\n${JSON.stringify(events.map(e => [e.seq, e.type, e.data?.message?.toolCallId ?? e.data?.source?.kind ?? '']))}`);
  };
  try {
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-dormancy-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { id: 'tool-subagent', config: { provider: 'spawn', toolName: 'subagent', agentOptions: { provider: 'fixture', model: 'worker' } } },
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-dormancy-provider.mjs') }, { id: 'director', name: join(here, '../dsh-director/index.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') }] },
    ]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    // A fake todo CLI listing one unfinished, childless plan until the provider deletes it.
    const cli = join(directory, 'cli'); await mkdir(join(cli, 'dsh_todo'), { recursive: true }); await mkdir(join(directory, 'todo'));
    const planFile = join(directory, 'one-plan.json');
    await writeFile(planFile, JSON.stringify([{ attempt_id: 'attempt-open', node_key: 'n', next: 'test', remaining: ['test'], child_session: 'gone' }]));
    await writeFile(join(cli, 'dsh_todo', '__init__.py'), '');
    await writeFile(join(cli, 'dsh_todo', '__main__.py'), [
      'import json, os, sys',
      'path = ' + JSON.stringify(planFile),
      'plans = json.load(open(path)) if os.path.exists(path) else []',
      'print(json.dumps({"plans": plans}))'].join('\n') + '\n');
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutHostWiring(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: session,
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'),
      AITELIER_DSH_IDLE_REVIEW_MS: '400', AITELIER_DSH_DORMANT_COMPACT_MIN_TOKENS: String(floor),
      ...(minuteMs ? { AITELIER_DSH_DORMANT_MINUTE_MS: String(minuteMs) } : {}),
      AITELIER_TODO_PYTHON: 'python3', AITELIER_TODO_CWD: cli, AITELIER_TEST_PLAN_FILE: planFile } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);

    await until('dormancy never entered', () => result('root-dormant'));
    // The director must say how long it sleeps: DSH refuses the call without the required argument.
    const nowake = result('root-dormant-nowake').data.message.content.map(b => b.text ?? '').join('');
    console.log('DORMANCY_NOWAKE ' + nowake.slice(0, 300));
    assert.match(nowake, /wake_after_minutes/);
    const refused = json(result('root-dormant-refused'));
    assert.equal(refused.refused, 'live_claims');
    assert.deepEqual(refused.node_keys, ['n'], 'only this driver\'s live claims count');
    assert.equal(json(result('root-dormant-empty')).refused, 'reason_required');
    const plans = json(result('root-dormant-plans'));
    assert.equal(plans.refused, 'unfinished_plans', JSON.stringify(plans));
    assert.deepEqual(plans.attempts, ['attempt-open']);
    const children = json(result('root-dormant-children'));
    assert.equal(children.refused, 'live_children', JSON.stringify(children));
    // Naming, not only counting: the refusal must carry the id of the child that
    // holds dormancy off. This scenario holds exactly one child open (DORMANCY_CHILD),
    // and its session is the only non-root session the fixture provider is called for.
    // Printed verbatim so the evidence file quotes the payload instead of restating it.
    console.log('DORMANCY_CHILDREN_REFUSAL ' + JSON.stringify(children));
    await until('the held child never ran', () => requests.some(r => r.session !== session));
    const childSession = requests.find(r => r.session !== session).session;
    assert.deepEqual(children.live_children, [childSession], JSON.stringify(children));
    assert.equal(children.reason, '1 native child(ren) still running');
    assert.equal(claimQueries[0].driver_id, 'dsh-fixture');
    const entered = json(result('root-dormant'));
    assert.equal(entered.dormant, true);
    assert.equal(entered.reason, 'nothing authorized is available');
    assert.equal(entered.wake_after_minutes, 15);
    // Open issues are reported, never a refusal: counted by kind, the untracked ones (no node, no
    // work item) named oldest first, with the triage note; both list_issues pages were read.
    assert.deepEqual(issueQueries.map(q => [q.project_id, q.statuses, q.after]), [['alpha', ['open'], 0], ['alpha', ['open'], 7]]);
    assert.equal(entered.open_issues.total, 5);
    assert.deepEqual(entered.open_issues.by_kind, { defect: 3, gap: 2 });
    assert.equal(entered.open_issues.untracked, 3);
    assert.deepEqual(entered.open_issues.triage.map(i => i.id), ['iss-old', 'iss-done', 'iss-new']);
    assert.match(entered.open_issues.note, /resolve_issue/);
    assert.equal(Date.parse(entered.until) - Date.parse(entered.since), 15 * (minuteMs ?? 60000));
    const enteredAt = Date.now();
    if (minuteMs) {
      // No input at all: the chosen sleep runs out and the parked review runs once, saying why.
      const timedOut = () => reviews().filter(e => e.data.content[0].text.includes('"reason":"dormancy_timeout"'));
      await until('the dormancy timeout never ran the review', () => timedOut().length === 1, Math.max(15 * minuteMs + 15000, 45000));
      assert.ok(Date.now() - enteredAt >= 15 * minuteMs - 1000, 'the review ran before the chosen sleep ran out');
      const review = JSON.parse(timedOut()[0].data.content[0].text.split('\n').slice(1).join('\n'));
      assert.equal(review.dormant_reason, 'nothing authorized is available');
      assert.match(review.instruction, /list_user_questions/);
      const dormantAt = phases.findIndex(p => p.phase === 'dormant');
      assert.ok(dormantAt >= 0, 'the dormant phase was never reported: ' + JSON.stringify(phases.map(p => p.phase)));
      assert.ok(phases.slice(dormantAt).some(p => p.phase === 'running'), 'the phase stayed dormant after the timeout');
      return { phases, output };
    }
    await until('the dormant phase was never reported: ' + JSON.stringify(phases.map(p => p.phase)), () => phases.at(-1)?.phase === 'dormant');
    // The refusals were turns too: by now the normal review may have fired between them. From the
    // dormant turn on, ten review intervals pass without one.
    const reviewsBefore = reviews().length;
    await new Promise(resolve => setTimeout(resolve, 4000));
    assert.equal(reviews().length, reviewsBefore, 'a timed review fired while dormant\n' + JSON.stringify(events.map(e => [e.seq, e.type, e.data?.message?.toolCallId ?? e.data?.source?.kind ?? '', (e.data?.content?.[0]?.text ?? '').slice(0, 40)])) + output.slice(-3000));
    const dormantSeq = result('root-dormant').seq;
    const compactions = events.filter(e => e.type === 'compaction/end' && !e.data.error && e.seq > dormantSeq);
    const summaryRequests = requests.filter(r => r.purpose === 'compaction').length;
    assert.ok(!events.some(e => e.type === 'compaction/end' && e.seq < dormantSeq), 'fixture: an automatic compaction ran before dormancy');

    // A State event wakes it; after that turn the 400 ms review is back.
    change();
    await until('the State event did not wake the director', () => events.some(e => e.type === 'assistant/message' && JSON.stringify(e.data).includes('WOKEN')));
    await until('the review did not resume after waking', () => reviews().length > reviewsBefore);
    assert.notEqual(phases.at(-1).phase, 'dormant', 'still reported dormant after waking');
    assert.ok(!reviews().some(e => e.data.content[0].text.includes('dormancy_timeout')), 'an input wake must not also time out');
    return { compactions, summaryRequests, output };
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of parked) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  }
}

test('real pinned DSH: dormancy is refused while work is live, parks reviews, compacts a large context once, and a State event wakes it', { timeout: 240000 }, async (t) => {
  const run = await scenario(t, 1000);
  if (!run) return;
  const { compactions, summaryRequests } = run;
  assert.equal(compactions.length, 1, 'expected exactly one compaction on entering dormancy');
  assert.equal(summaryRequests, 1);
});

test('real pinned DSH: with no input the chosen sleep runs out, the phase leaves dormant and the review runs', { timeout: 240000 }, async (t) => {
  await scenario(t, 10_000_000, { minuteMs: 100 });
});

test('real pinned DSH: below the token floor dormancy does not compact', { timeout: 240000 }, async (t) => {
  const run = await scenario(t, 10_000_000);
  if (!run) return;
  const { compactions, summaryRequests } = run;
  assert.equal(compactions.length, 0);
  assert.equal(summaryRequests, 0);
});
