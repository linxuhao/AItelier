/**
 * Todo plans end to end: a real pinned DSH boots this driver with a fake model,
 * a fake MCP/State server and a todo CLI. The root freezes two plans and
 * dispatches two native children; one walks every item, one stops after the
 * second and must be handed back by the runtime, not by its last message.
 *
 * The CLI is a minimal fake of the `core.todo_cli` contract (JSON files in a
 * temp store), reached exactly as production reaches it:
 * `$AITELIER_TODO_PYTHON -m core.todo_cli` in `$AITELIER_TODO_CWD`.
 * Set AITELIER_TODO_TEST_PYTHON and AITELIER_TODO_TEST_CWD to run the same
 * scenario against a real checkout instead.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm, realpath, stat } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { DriverRuntime, TodoCli, todoSummary, unfinishedWithoutChild, stateInjectionSource, runtimeEventSource, todoBriefSource } from './runtime.mjs';

const FAKE_CLI = String.raw`"""Fake core.todo_cli for runtime.todo.test.mjs: the CLI contract, minimally."""
import argparse, glob, hashlib, json, os, subprocess, sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('command')
for flag in ('--store', '--attempt', '--item', '--session', '--note', '--plan-json', '--out'):
    parser.add_argument(flag)
args = parser.parse_args()
store = Path(args.store or os.path.join(os.environ['AITELIER_DSH_STATE_DIR'], 'todo'))
store.mkdir(parents=True, exist_ok=True)

def emit(value, code=0):
    print(json.dumps(value))
    sys.exit(code)

def git(plan, *argv):
    return subprocess.run(['git', *argv], cwd=plan['workspace'], capture_output=True, text=True, check=True).stdout.strip()

def evaluate(plan, item):
    results = []
    for binding in item['predicates']:
        name, bound = binding['predicate'], binding.get('args', {})
        if name == 'files_present':
            ok = any(os.path.isfile(p) for p in glob.glob(os.path.join(plan['workspace'], bound['pattern'])))
        else:
            ok = git(plan, 'rev-parse', 'HEAD') != bound['base'] and not git(plan, 'status', '--porcelain')
        results.append({'predicate': name, 'outcome': 'pass' if ok else 'fail', 'detail': name})
    return results

def status(plan):
    done = plan['done']
    remaining = [i['id'] for i in plan['items'] if i['id'] not in done]
    upcoming = next((i['id'] for i in plan['items'] if i['id'] not in done and all(n in done for n in i['needs'])), None)
    brief = f"{plan['goal']}\nNext: {upcoming or '(none)'}\nRemaining {len(remaining)} item(s): {', '.join(remaining)}"
    return {'attempt_id': plan['attempt_id'], 'brief': brief, 'goal': plan['goal'], 'next': upcoming, 'done': done, 'remaining': remaining,
            'deferred': [], 'unmeasurable': [], 'child_session': plan['child_session'], 'items': plan['items'],
            'node_key': plan['node_key']}

def save(plan):
    path = store / (plan['attempt_id'] + '.json')
    path.with_suffix('.tmp').write_text(json.dumps(plan))
    path.with_suffix('.tmp').replace(path)

if args.command == 'create':
    plan = json.loads(Path(args.plan_json).read_text())
    for item in plan['items']:
        for binding in item['predicates']:
            if binding['predicate'] != 'files_present':
                emit({'refused': 'unknown_predicate', 'reason': binding['predicate']}, 2)
    plan['items'].append({'id': 'deliver', 'title': 'deliver', 'needs': [i['id'] for i in plan['items']],
                          'predicates': [{'predicate': 'git_delivered', 'args': {'base': plan['base_sha']}}]})
    plan.update(done=[], child_session=None, notes=[], results={})
    save(plan)
    emit({'plan_hash': hashlib.sha256(json.dumps(plan['items']).encode()).hexdigest()})
if args.command == 'list':
    plans = [status(json.loads(p.read_text())) for p in sorted(store.glob('*.json'))]
    emit({'plans': [{k: v for k, v in p.items() if k in ('attempt_id', 'node_key', 'next', 'done', 'remaining', 'child_session')} for p in plans]})

path = store / (args.attempt + '.json')
if not path.exists():
    emit({'refused': 'no_such_plan', 'reason': args.attempt}, 2)
plan = json.loads(path.read_text())
item = next((i for i in plan['items'] if i['id'] == args.item), None)
if args.command == 'status':
    emit(status(plan))
if args.command == 'handback':
    emit({**status(plan), 'reason': 'todo_unfinished', 'last_observations': {
        i: plan['results'].get(i, []) for i in status(plan)['remaining']}})
if args.command == 'begin':
    if not all(n in plan['done'] for n in item['needs']):
        emit({'refused': 'unmet_needs', 'reason': ', '.join(item['needs'])}, 2)
    plan['child_session'] = args.session or plan['child_session']
    save(plan)
    emit({'begun': item['id']})
if args.command == 'done':
    results = evaluate(plan, item)
    outcome = 'pass' if all(r['outcome'] == 'pass' for r in results) else 'fail'
    if outcome == 'pass' and item['id'] not in plan['done']:
        plan['done'].append(item['id'])
    plan['results'][item['id']] = results
    save(plan)
    emit({'outcome': outcome, 'results': results, 'brief': status(plan)['brief']})
if args.command == 'report':
    plan['notes'].append({'item': args.item, 'note': args.note})
    save(plan)
    emit({'noted': args.item})
if args.command == 'finish':
    failing = [i['id'] for i in plan['items'] if any(r['outcome'] != 'pass' for r in evaluate(plan, i))]
    if failing:
        emit({'refused': 'unfinished', 'failing': failing}, 2)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    bundle = out / (args.attempt + '.bundle')
    git(plan, 'bundle', 'create', str(bundle), 'HEAD')
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    report = out / (args.attempt + '.report.json')
    report.write_text(json.dumps({'status': 'candidate', 'artifact': git(plan, 'rev-parse', 'HEAD'),
                                  'git_bundle': {'path': str(bundle), 'sha256': digest}}))
    emit({'git_bundle': {'path': str(bundle), 'sha256': digest}, 'report': str(report)})
emit({'refused': 'usage', 'reason': args.command}, 2)
`;

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-todo-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.AITELIER_URL + path, { method: 'POST', body: JSON.stringify(body) });
const plans = JSON.parse(process.env.AITELIER_TEST_PLANS);
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const shell = (id, command) => tool(id, 'bash', { description: 'fixture work', command, run_in_background: false });
const work = (role, plan, ids) => ids.flatMap(item => [
  tool(role + '-todo_begin-' + item, 'todo_begin', { attempt_id: plan.attempt_id, item_id: item }),
  shell(role + '-shell-' + item, "printf " + item + " > '" + plan.workspace + "/" + item + ".txt'"),
  tool(role + '-todo_done-' + item, 'todo_done', { attempt_id: plan.attempt_id, item_id: item }),
]);
const commit = plan => shell('full-shell-commit', "cd '" + plan.workspace + "' && git add -A && git -c user.name=fixture -c user.email=fixture@example.invalid commit -q -m work");
const scripts = {
  full: [
    ...['record_evidence', 'verify_node', 'report_external_attempt'].map(name =>
      tool('full-judge-' + name, 'call_tool', { name: 'mcp__aitelier__' + name, arguments: { attempt_id: plans.a.attempt_id } })),
    tool('full-todo_status-start', 'todo_status', { attempt_id: plans.a.attempt_id }),
    ...work('full', plans.a, ['a', 'b']),
    'COMPACT',
    tool('full-todo_status-after-compact', 'todo_status', { attempt_id: plans.a.attempt_id }),
    ...work('full', plans.a, ['c']),
    commit(plans.a),
    tool('full-todo_begin-deliver', 'todo_begin', { attempt_id: plans.a.attempt_id, item_id: 'deliver' }),
    tool('full-todo_done-deliver', 'todo_done', { attempt_id: plans.a.attempt_id, item_id: 'deliver' }),
    { type: 'text', text: 'FULL_CHILD_DONE' },
  ],
  partial: [
    tool('partial-todo_status-start', 'todo_status', { attempt_id: plans.b.attempt_id }),
    ...work('partial', plans.b, ['a', 'b']),
    { type: 'text', text: 'PARTIAL_CHILD_STOPS' },
  ],
};
export function apply(ctx) {
  const roles = new Map(), steps = new Map(), root = process.env.AITELIER_DSH_SESSION;
  const done = new Set();
  let pressure = false, woke = false;
  const once = (key, block) => done.has(key) ? undefined : (done.add(key), block);
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 16000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      if (!roles.has(options.sessionId)) roles.set(options.sessionId, options.sessionId === root ? 'root'
        : text.includes('TODO_CHILD_FULL') ? 'full' : text.includes('TODO_CHILD_PARTIAL') ? 'partial' : 'other');
      const role = roles.get(options.sessionId);
      await post('/test/request', { session: options.sessionId, role, purpose: options.purpose ?? null, messages: options.messages, tools: options.tools?.map(t => t.name) });
      let blocks, usage = 20;
      if (options.purpose === 'compaction') blocks = [{ type: 'text', text: 'Checkpoint: continue the plan; use todo_status for the current step.' }];
      else if (!options.tools?.length) blocks = [{ type: 'text', text: 'Fixture session' }];
      else if (role === 'full' || role === 'partial') {
        const step = steps.get(options.sessionId) ?? 0;
        steps.set(options.sessionId, step + 1);
        const next = scripts[role][step] ?? { type: 'text', text: role.toUpperCase() + '_CHILD_IDLE' };
        if (next === 'COMPACT') {
          usage = 30000;
          blocks = [{ type: 'text', text: 'CHILD_PADDING ' + 'worker context '.repeat(14000) },
            tool('full-todo_status-before-compact', 'todo_status', { attempt_id: plans.a.attempt_id })];
        } else blocks = [next];
      } else if (role === 'root') {
        const handed = text.includes('todo_unfinished'), completed = text.includes('FULL_CHILD_DONE');
        const block = once('plan-a', tool('root-todo_plan-a', 'todo_plan', { plan: plans.a.input }))
          ?? once('plan-b', tool('root-todo_plan-b', 'todo_plan', { plan: plans.b.input }))
          ?? once('spawn-full', tool('root-spawn-full', 'subagent', { description: 'full todo child', prompt: 'TODO_CHILD_FULL attempt_id=' + plans.a.attempt_id + '; use todo_status, todo_begin and todo_done.' }))
          ?? once('spawn-partial', tool('root-spawn-partial', 'subagent', { description: 'partial todo child', prompt: 'TODO_CHILD_PARTIAL attempt_id=' + plans.b.attempt_id + '; use todo_status, todo_begin and todo_done.' }))
          ?? once('judge', tool('root-judge-record_evidence', 'call_tool', { name: 'mcp__aitelier__record_evidence', arguments: { attempt_id: plans.a.attempt_id } }))
          ?? (handed && completed ? once('finish', tool('root-todo_finish-a', 'todo_finish', { attempt_id: plans.a.attempt_id })) : undefined)
          ?? (done.has('finish') ? once('pressure', { type: 'text', text: 'ROOT_PADDING ' + 'director context '.repeat(14000) }) : undefined)
          ?? (woke ? once('status-b', tool('root-todo_status-after-compact', 'todo_status', { attempt_id: plans.b.attempt_id })) : undefined)
          ?? { type: 'text', text: done.has('status-b') ? 'ROOT_DONE' : 'Director idle.' };
        if (block.text?.startsWith('ROOT_PADDING')) { pressure = true; usage = 30000; }
        if (block.text === 'ROOT_DONE') void post('/test/root-done', {});
        blocks = [block];
      } else blocks = [{ type: 'text', text: 'Unexpected session' }];
      for (const [index, block] of blocks.entries()) {
        yield { type: 'block-start', index, blockType: block.type };
        if (block.type === 'text') yield { type: 'text-delta', index, text: block.text };
        else yield { type: 'tool-call-delta', index, id: block.id, name: block.name, argumentsDelta: block.arguments };
        yield { type: 'block-end', index, block };
      }
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: usage } };
      yield { type: 'finish', reason: { kind: blocks.at(-1).type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
  // The root's pressure reply leaves it idle; a State event wakes it into compaction.
  ctx.on('agent/status', ({ agent, status }) => {
    if (agent.session.id === root && status === 'idle' && pressure && !woke) { woke = true; void post('/test/change', {}); }
  });
  ctx.on('session/event', (session, event) => {
    if (['tool/result', 'user/message', 'compaction/end'].includes(event.type)) void post('/test/event', { session: session.id, event });
  });
}
`;

const git = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
async function workspace(path) {
  await mkdir(path, { recursive: true });
  git(path, 'init', '-q');
  await writeFile(join(path, 'README'), 'base\n');
  git(path, 'add', 'README');
  git(path, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '-m', 'base');
  return git(path, 'rev-parse', 'HEAD');
}
const planInput = (attempt_id, node_key, base_sha, path) => ({ attempt_id, node_key, context_hash: 'c'.repeat(64), base_sha,
  workspace: path, scope: ['*.txt'], goal: 'Write a.txt, b.txt and c.txt for ' + node_key,
  items: [
    { id: 'a', title: 'write a.txt', needs: [], predicates: [{ predicate: 'files_present', args: { pattern: 'a.txt' } }] },
    { id: 'b', title: 'write b.txt', needs: ['a'], predicates: [{ predicate: 'files_present', args: { pattern: 'b.txt' } }] },
    { id: 'c', title: 'write c.txt', needs: ['b'], predicates: [{ predicate: 'files_present', args: { pattern: 'c.txt' } }] },
  ] });
const toolText = event => event.data.message.content.map(block => block.text ?? '').join('');
const toolJson = event => JSON.parse(toolText(event));

async function fakeCli(directory) {
  await mkdir(join(directory, 'core'), { recursive: true });
  await writeFile(join(directory, 'core', '__init__.py'), '');
  await writeFile(join(directory, 'core', 'todo_cli.py'), FAKE_CLI);
  return { python: process.env.AITELIER_TODO_TEST_PYTHON ?? 'python3', cwd: process.env.AITELIER_TODO_TEST_CWD ?? directory };
}

test('real pinned DSH: plans walked by native children, runtime handback, State-writer refusal, briefs across compaction', { timeout: 90000 }, async () => {
  const directory = await mkdtemp(join(homedir(), 'dsh-todo-'));
  const entry = await realpath(process.env.AITELIER_DSH_TEST_ENTRY ?? new URL('./node_modules/.bin/dsh', import.meta.url));
  const here = dirname(fileURLToPath(import.meta.url));
  const requests = [], events = [], mcpCalls = [], activity = [], parked = new Set();
  let changed = false, rootDone = false, output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/mcp') {
      if (req.method === 'GET') { res.statusCode = 405; return res.end(); }
      if (req.method === 'DELETE') { res.statusCode = 200; return res.end(); }
      if (!Object.hasOwn(body, 'id')) { res.statusCode = 202; return res.end(); }
      let result;
      if (body.method === 'initialize') result = { protocolVersion: body.params.protocolVersion,
        capabilities: { tools: {} }, serverInfo: { name: 'fixture-aitelier', version: '1' } };
      else if (body.method === 'tools/list') result = { tools: ['record_evidence', 'verify_node', 'report_external_attempt'].map(name => ({ name, description: name,
        inputSchema: { type: 'object', properties: {}, additionalProperties: true } })) };
      else if (body.method === 'tools/call') { mcpCalls.push(body.params.name); result = { content: [{ type: 'text', text: 'fixture_state_write_ok' }] }; }
      else result = {};
      return send({ jsonrpc: '2.0', id: body.id, result });
    }
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [] } });
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture', event_seq: 10,
      nodes: [{ name: 'n', status: changed ? 'VERIFIED' : 'PENDING' }], project_note: { index: [] },
      private_note: { index: [] }, driver_skill: 'fixture driver skill' });
    if (req.url.endsWith('/activity')) { activity.push(body); return send({ ok: true }); }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url === '/api/state/query/wait_for_state_change') {
      if (changed && body.after < 11) return send({ timed_out: false, next_after: 11, next_inbox_after: 0, events: [{ type: 'node_changed' }] });
      parked.add(res); res.on('close', () => parked.delete(res)); return;
    }
    if (req.url === '/test/request') { requests.push(body); return send({ ok: true }); }
    if (req.url === '/test/event') { events.push(body); return send({ ok: true }); }
    if (req.url === '/test/root-done') { rootDone = true; return send({ ok: true }); }
    if (req.url === '/test/change') {
      changed = true;
      for (const target of parked) { target.setHeader('content-type', 'application/json'); target.end(JSON.stringify({ timed_out: false, next_after: 11, next_inbox_after: 0, events: [{ type: 'node_changed' }] })); }
      parked.clear(); return send({ ok: true });
    }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const cli = await fakeCli(join(directory, 'cli'));
    const plans = {};
    for (const [key, node] of [['a', 'execution.todo-full'], ['b', 'execution.todo-partial']]) {
      const path = join(directory, 'work', key);
      const input = planInput('attempt-todo-' + key, node, await workspace(path), path);
      plans[key] = { attempt_id: input.attempt_id, workspace: path, input };
    }
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-todo-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true }, { id: 'compaction-basic', config: { thresholdRatio: 0.8, retainTokens: 128, headroomTokens: 512 } },
      { id: 'tool-subagent', config: { provider: 'spawn', toolName: 'subagent', agentOptions: { provider: 'fixture', model: 'worker' } } },
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-todo-provider.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') },
        { id: 'mcp-aitelier', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'aitelier', transport: 'streamable-http', url: base + '/mcp', failOnStartupError: true } }] },
    ]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...process.env,
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'fixture-root',
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'),
      AITELIER_DSH_EXECUTION: 'host', DSH_PERMISSION_MODE: 'danger-full-access',
      AITELIER_TODO_PYTHON: cli.python, AITELIER_TODO_CWD: cli.cwd, AITELIER_TEST_PLANS: JSON.stringify(plans) } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);
    const deadline = Date.now() + 75000;
    while (Date.now() < deadline && proc.exitCode === null && !rootDone && !activity.some(a => a.phase === 'failed')) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    const diagnostics = () => output + '\n' + JSON.stringify(requests.map(r => ({ role: r.role, purpose: r.purpose,
      last: JSON.stringify(r.messages.at(-1)).slice(0, 300) })), null, 1).slice(-12000);
    assert.equal(proc.exitCode, null, output);
    assert.ok(!activity.some(a => a.phase === 'failed'), JSON.stringify(activity.filter(a => a.phase === 'failed')) + output);
    assert.ok(rootDone, 'scenario did not finish\n' + diagnostics());
    const results = events.filter(e => e.event.type === 'tool/result').map(e => ({ session: e.session, id: e.event.data.message.toolCallId, event: e.event }));
    const result = id => results.find(r => r.id === id);
    const roles = new Map(requests.map(r => [r.session, r.role]));
    const sessionOf = role => [...roles].find(([, value]) => value === role)?.[0];
    const full = sessionOf('full'), partial = sessionOf('partial');
    assert.ok(full && partial && full !== partial && ![full, partial].includes('fixture-root'));

    // 1. The full child walked a, b, c and deliver; every todo result carried the brief.
    for (const item of ['a', 'b', 'c', 'deliver']) assert.equal(toolJson(result('full-todo_done-' + item).event).outcome, 'pass', item);
    const todoResults = results.filter(r => /^(full|partial)-todo_/.test(r.id));
    assert.ok(todoResults.length >= 15);
    for (const r of todoResults) {
      assert.equal(r.event.data.message.isError, false, r.id + toolText(r.event));
      assert.match(toolJson(r.event).brief, /Write a\.txt, b\.txt and c\.txt/, r.id + ' result lacks the brief');
    }
    assert.ok(todoResults.filter(r => r.id.startsWith('full-')).every(r => r.session === full));
    const finalA = JSON.parse(execFileSync(cli.python, ['-m', 'core.todo_cli', 'status', '--attempt', plans.a.attempt_id],
      { cwd: cli.cwd, env: { ...process.env, AITELIER_DSH_STATE_DIR: directory }, encoding: 'utf8' }));
    assert.deepEqual(finalA.remaining, []); assert.equal(finalA.child_session, full, 'todo_begin bound the root, not the calling child');

    // 2. The partial child stopped after b: the runtime, not the model, handed it back.
    // (The goal text itself names todo_unfinished, so match the runtime's message.)
    const handbacks = events.filter(e => e.session === 'fixture-root' && e.event.type === 'user/message'
      && e.event.data.content?.[0]?.text?.startsWith('AItelier todo handback:'));
    assert.equal(handbacks.length, 1, 'expected exactly one handback\n' + diagnostics());
    assert.equal(handbacks[0].event.data.source.kind, runtimeEventSource);
    const handback = JSON.parse(handbacks[0].event.data.content[0].text.split('\n').slice(1).join('\n'));
    assert.equal(handback.reason, 'todo_unfinished'); assert.equal(handback.attempt_id, plans.b.attempt_id);
    assert.equal(handback.next, 'c'); assert.deepEqual(handback.done, ['a', 'b']);
    assert.deepEqual(handback.remaining, ['c', 'deliver']);
    assert.deepEqual(Object.keys(handback.last_observations).sort(), ['c', 'deliver']);

    // 3. A child cannot judge its own work; the root can.
    for (const name of ['record_evidence', 'verify_node', 'report_external_attempt']) {
      const refused = result('full-judge-' + name);
      assert.equal(refused.session, full); assert.equal(refused.event.data.message.isError, true);
      assert.match(toolText(refused.event), /workers do not judge their own work; the director records State evidence/);
    }
    const rootJudge = result('root-judge-record_evidence');
    assert.equal(rootJudge.event.data.message.isError, false); assert.match(toolText(rootJudge.event), /fixture_state_write_ok/);
    assert.deepEqual(mcpCalls, ['record_evidence'], 'a refused State write reached the server');

    // 4. Compaction: after each successful child compaction the child got one
    // brief line (a host-scoped agent/pre-step reaches native children) and its
    // next todo_status still carries the brief; the root's post-compaction State
    // snapshot lists the plans.
    const compactions = events.filter(e => e.session === full && e.event.type === 'compaction/end' && !e.event.data.error).map(e => e.event.seq);
    assert.ok(compactions.length, 'child never compacted\n' + diagnostics());
    const briefs = events.filter(e => e.session === full && e.event.type === 'user/message' && e.event.data.source?.kind === todoBriefSource);
    compactions.forEach((seq, index) => assert.equal(briefs.filter(b => b.event.seq > seq && b.event.seq < (compactions[index + 1] ?? Infinity)).length, 1,
      'expected exactly one brief after child compaction ' + seq));
    assert.equal(briefs.length, compactions.length);
    for (const brief of briefs) {
      assert.match(brief.event.data.content[0].text, /^todo brief: Write a\.txt, b\.txt and c\.txt for execution\.todo-full \/ Next: c/);
      assert.doesNotMatch(brief.event.data.content[0].text, /\n/);
    }
    assert.ok(requests.some(r => r.session === full && !r.purpose && JSON.stringify(r.messages).includes('todo brief:')), 'brief never reached the child model');
    assert.match(toolJson(result('full-todo_status-after-compact').event).brief, /Next: c/);
    assert.ok(!events.some(e => e.session === partial && e.event.data.source?.kind === todoBriefSource), 'brief without a compaction');
    assert.ok(events.some(e => e.session === 'fixture-root' && e.event.type === 'compaction/end' && !e.event.data.error), 'root never compacted');
    const snapshots = events.filter(e => e.session === 'fixture-root' && e.event.type === 'user/message' && e.event.data.source?.kind === stateInjectionSource);
    const state = s => JSON.parse(s.event.data.content[0].text.split('\n').slice(1).join('\n'));
    assert.equal(state(snapshots[0]).todo, undefined, 'bootstrap snapshot predates any plan');
    const after = snapshots.filter(s => s.event.data.source.compaction_seq > 0).at(-1);
    assert.ok(after, 'no post-compaction snapshot');
    assert.deepEqual(state(after).todo, [
      { attempt_id: plans.a.attempt_id, node_key: 'execution.todo-full', next: null, remaining_count: 0, child_alive: false },
      { attempt_id: plans.b.attempt_id, node_key: 'execution.todo-partial', next: 'c', remaining_count: 2, child_alive: false },
    ]);
    assert.match(toolJson(result('root-todo_status-after-compact').event).brief, /Next: c/);

    // 5. Finish on the all-pass plan returns the bundle and report paths.
    const finish = toolJson(result('root-todo_finish-a').event);
    assert.equal(finish.refused, undefined, JSON.stringify(finish));
    assert.ok((await stat(finish.git_bundle.path)).size > 0); assert.ok((await stat(finish.report)).size > 0);
    assert.ok(finish.git_bundle.path.startsWith(join(directory, 'todo', 'finish', plans.a.attempt_id)));
    git(plans.a.workspace, 'bundle', 'verify', finish.git_bundle.path);

    // Root-only tools stay off the children's tool lists; worker tools are on both.
    const toolsOf = role => requests.find(r => r.role === role && !r.purpose && r.tools?.length).tools;
    for (const role of ['root', 'full', 'partial']) for (const name of ['todo_status', 'todo_begin', 'todo_done', 'todo_report'])
      assert.ok(toolsOf(role).includes(name), role + ' lacks ' + name);
    for (const name of ['todo_plan', 'todo_finish']) {
      assert.ok(toolsOf('root').includes(name));
      assert.ok(!toolsOf('full').includes(name) && !toolsOf('partial').includes(name), 'children see ' + name);
    }
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of parked) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  }
});

test('CLI results: a refusal is returned with the brief, a missing brief is filled from status, a crash throws', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-todo-cli-'));
  try {
    const cli = await fakeCli(join(directory, 'cli'));
    const path = join(directory, 'work');
    const plan = planInput('attempt-unit', 'execution.unit', await workspace(path), path);
    await writeFile(join(directory, 'plan.json'), JSON.stringify(plan));
    const todo = new TodoCli({ python: cli.python, cwd: cli.cwd, stateDir: directory });
    const created = await todo.call(plan.attempt_id, ['create', '--plan-json', join(directory, 'plan.json')]);
    assert.match(created.plan_hash, /^[0-9a-f]{64}$/); assert.match(created.brief, /Next: a/);
    const refused = await todo.call(plan.attempt_id, ['begin', '--attempt', plan.attempt_id, '--item', 'b', '--session', 's1']);
    assert.ok(refused.refused); assert.match(refused.brief, /Next: a/);
    const absent = await todo.call('attempt-missing', ['status', '--attempt', 'attempt-missing']);
    assert.ok(absent.refused); assert.equal(absent.brief, null);
    await assert.rejects(new TodoCli({ python: cli.python, cwd: directory, stateDir: directory }).run(['status', '--attempt', 'x']), /todo_cli status/);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('idle review and snapshot views: unfinished plans without a live child, and render() only lists plans when there are any', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-todo-views-'));
  try {
    const cli = await fakeCli(join(directory, 'cli'));
    const todo = new TodoCli({ python: cli.python, cwd: cli.cwd, stateDir: directory });
    assert.deepEqual(await todo.plans(), [], 'no store yet is no plans, not an error');
    for (const key of ['open', 'done']) {
      const path = join(directory, 'work-' + key);
      const plan = planInput('attempt-' + key, 'execution.' + key, await workspace(path), path);
      plan.items = plan.items.slice(0, 1);
      await writeFile(join(directory, key + '.json'), JSON.stringify(plan));
      await todo.call(plan.attempt_id, ['create', '--plan-json', join(directory, key + '.json')]);
      await todo.call(plan.attempt_id, ['begin', '--attempt', plan.attempt_id, '--item', 'a', '--session', 'child-' + key]);
      if (key === 'done') {
        await writeFile(join(path, 'a.txt'), 'a');
        git(path, 'add', 'a.txt'); git(path, '-c', 'user.name=f', '-c', 'user.email=f@example.invalid', 'commit', '-q', '-m', 'a');
        for (const item of ['a', 'deliver']) {
          await todo.call(plan.attempt_id, ['begin', '--attempt', plan.attempt_id, '--item', item, '--session', 'child-done']);
          assert.equal((await todo.call(plan.attempt_id, ['done', '--attempt', plan.attempt_id, '--item', item])).outcome, 'pass');
        }
      }
    }
    const plans = await todo.plans();
    assert.deepEqual(plans.map(p => p.attempt_id), ['attempt-done', 'attempt-open']);
    assert.deepEqual(unfinishedWithoutChild(plans, new Set()), [
      { attempt_id: 'attempt-open', node_key: 'execution.open', next: 'a', remaining: ['a', 'deliver'] }]);
    assert.deepEqual(unfinishedWithoutChild(plans, new Set(['child-open'])), [], 'a plan with a live child is not handed back');
    assert.deepEqual(todoSummary(plans, new Set(['child-open'])), [
      { attempt_id: 'attempt-done', node_key: 'execution.done', next: null, remaining_count: 0, child_alive: false },
      { attempt_id: 'attempt-open', node_key: 'execution.open', next: 'a', remaining_count: 2, child_alive: true }]);
    let listed = [];
    const runtime = new DriverRuntime({ project: 'alpha', driver: 'driver-a', directory,
      request: async () => ({ nodes: [], project_note: {}, private_note: {} }), todoPlans: async () => listed });
    await runtime.refresh();
    assert.equal(JSON.parse(runtime.render()).todo, undefined);
    listed = todoSummary(plans, new Set());
    await runtime.refresh();
    assert.deepEqual(JSON.parse(runtime.render()).todo, listed);
  } finally { await rm(directory, { recursive: true, force: true }); }
});
