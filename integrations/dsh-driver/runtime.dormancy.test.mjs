import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutCallerMode } from './dsh-entry.mjs';

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
      else block = once('refused', tool('root-dormant-refused', 'idle_dormant', { reason: 'quiet' }))
        ?? once('empty', tool('root-dormant-empty', 'idle_dormant', { reason: '  ' }))
        ?? once('plans', tool('root-dormant-plans', 'idle_dormant', { reason: 'quiet' }))
        ?? once('spawn', () => (unlinkSync(process.env.AITELIER_TEST_PLAN_FILE), tool('root-spawn', 'subagent', { description: 'dormancy child', prompt: 'DORMANCY_CHILD stay alive' })))
        ?? once('children', tool('root-dormant-children', 'idle_dormant', { reason: 'quiet' }))
        ?? once('release', () => (void post('/test/release', {}), { type: 'text', text: 'WAITING_FOR_CHILD' }))
        ?? (text.includes('CHILD_DONE') ? once('pad', { type: 'text', text: 'ROOT_PADDING ' + 'director context '.repeat(1500) }) : undefined)
        ?? (done.has('woken') ? undefined : text.includes('ROOT_PADDING') && !done.has('dormant') ? (done.add('dormant'), tool('root-dormant', 'idle_dormant', { reason: 'nothing authorized is available' })) : undefined)
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

async function scenario(t, floor) {
  const entry = await measuredEntryForTest(t);
  if (!entry) return null;                  // nothing pinned is installed: reported unmeasured
  const directory = await mkdtemp(join(homedir(), 'dsh-dormancy-'));
  // Unique per run: background-job detection matches processes by session, and other
  // real-DSH test files run their own drivers in parallel.
  const session = 'dormancy-root-' + process.pid + '-' + Math.random().toString(36).slice(2, 8);
  const here = dirname(fileURLToPath(import.meta.url));
  const events = [], requests = [], claimQueries = [], parked = new Set();
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
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture', event_seq: 10,
      nodes: [{ name: 'n', status: changed ? 'VERIFIED' : 'PENDING' }], project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'fixture driver skill' });
    if (req.url.endsWith('/activity') || req.url.endsWith('/heartbeat')) return send({ ok: true });
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
  const until = async (what, predicate, ms = 20000) => {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline && proc.exitCode === null && !predicate()) await new Promise(resolve => setTimeout(resolve, 50));
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
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-dormancy-provider.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') }] },
    ]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    // A fake todo CLI listing one unfinished, childless plan until the provider deletes it.
    const cli = join(directory, 'cli'); await mkdir(join(cli, 'core'), { recursive: true }); await mkdir(join(directory, 'todo'));
    const planFile = join(directory, 'one-plan.json');
    await writeFile(planFile, JSON.stringify([{ attempt_id: 'attempt-open', node_key: 'n', next: 'test', remaining: ['test'], child_session: 'gone' }]));
    await writeFile(join(cli, 'core', '__init__.py'), '');
    await writeFile(join(cli, 'core', 'todo_cli.py'), [
      'import json, os, sys',
      'path = ' + JSON.stringify(planFile),
      'plans = json.load(open(path)) if os.path.exists(path) else []',
      'print(json.dumps({"plans": plans}))'].join('\n') + '\n');
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutCallerMode(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: session,
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'),
      AITELIER_DSH_IDLE_REVIEW_MS: '400', AITELIER_DSH_DORMANT_COMPACT_MIN_TOKENS: String(floor),
      AITELIER_TODO_PYTHON: 'python3', AITELIER_TODO_CWD: cli, AITELIER_TEST_PLAN_FILE: planFile } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);

    await until('dormancy never entered', () => result('root-dormant'));
    const refused = json(result('root-dormant-refused'));
    assert.equal(refused.refused, 'live_claims');
    assert.deepEqual(refused.node_keys, ['n'], 'only this driver\'s live claims count');
    assert.equal(json(result('root-dormant-empty')).refused, 'reason_required');
    const plans = json(result('root-dormant-plans'));
    assert.equal(plans.refused, 'unfinished_plans', JSON.stringify(plans));
    assert.deepEqual(plans.attempts, ['attempt-open']);
    const children = json(result('root-dormant-children'));
    assert.equal(children.refused, 'live_children', JSON.stringify(children));
    assert.equal(claimQueries[0].driver_id, 'dsh-fixture');
    const entered = json(result('root-dormant'));
    assert.equal(entered.dormant, true);
    assert.equal(entered.reason, 'nothing authorized is available');
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
    return { compactions, summaryRequests, output };
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of parked) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  }
}

test('real pinned DSH: dormancy is refused while work is live, parks reviews, compacts a large context once, and a State event wakes it', { timeout: 60000 }, async (t) => {
  const run = await scenario(t, 1000);
  if (!run) return;
  const { compactions, summaryRequests } = run;
  assert.equal(compactions.length, 1, 'expected exactly one compaction on entering dormancy');
  assert.equal(summaryRequests, 1);
});

test('real pinned DSH: below the token floor dormancy does not compact', { timeout: 60000 }, async (t) => {
  const run = await scenario(t, 10_000_000);
  if (!run) return;
  const { compactions, summaryRequests } = run;
  assert.equal(compactions.length, 0);
  assert.equal(summaryRequests, 0);
});
