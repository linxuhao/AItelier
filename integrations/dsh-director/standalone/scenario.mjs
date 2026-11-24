/** Plugin step 5: dsh-director on its own. A real DSH process loads only a fixture
 *  model, dsh-director and the built-in local host - no backend, no backend code, no
 *  backend variable (the process gets an allowlisted environment) - and the director takes two work items, freezes a todo plan
 *  for each from a template, dispatches one native worker per plan, the workers walk
 *  their items (todo_begin / change / commit / todo_done), the director finishes both
 *  plans into candidate reports, closes the work items and goes dormant.
 *
 *  Usage: [STANDALONE_PYTHON=<python with PyYAML>] node scenario.mjs <dsh entry (bin.js)>   - exits 0 and prints a JSON summary
 *  on success; prints what is missing and exits 1 otherwise. Used by
 *  run-in-container.sh (nothing but DSH + this plugin in a disposable container) and by
 *  the dsh-driver suite (runtime.standalone.test.mjs) on the measured DSH pin. */
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm, stat } from 'node:fs/promises';
import { execFileSync, spawn } from 'node:child_process';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { leaks } from '../tools/vocabulary.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const plugin = join(here, '..');

// The template is the plugin's own default (todo/todo_templates/change_files.yaml): no
// template directory is configured, so the run also proves the zero-config default.

// The scripted model. Root: take two work items, plan both, dispatch two workers,
// then (once both reported back) finish both, close them and go dormant. Each worker:
// status, begin write, change + commit, done, begin deliver, done, a final line.
const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.STANDALONE_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-standalone-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.STANDALONE_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const W = JSON.parse(process.env.STANDALONE_WORK);
export function apply(ctx) {
  const done = new Set(), steps = new Map();
  const once = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  const plan = w => tool('plan-' + w.id, 'todo_plan', { template: 'change_files', params: { paths: ['work.txt'], scope: ['work.txt'] },
    goal: 'write work.txt for ' + w.id, attempt_id: w.id,
    // The local host's work items carry no node key or context hash: the second plan gives none.
    ...(w === W[1] ? {} : { node_key: 'demo.' + w.id, context_hash: 'c'.repeat(64) }),
    base_sha: w.base, workspace: w.repo });
  const worker = (w, n) => [
    () => tool(w.id + '-status', 'todo_status', { attempt_id: w.id }),
    () => tool(w.id + '-begin-write', 'todo_begin', { attempt_id: w.id, item_id: 'write' }),
    () => tool(w.id + '-change', 'bash', { description: 'change and commit', run_in_background: false,
      command: "printf '" + w.id + "' > work.txt && git add work.txt && git -c user.name=worker -c user.email=worker@example.invalid commit -q -m " + w.id }),
    () => tool(w.id + '-done-write', 'todo_done', { attempt_id: w.id, item_id: 'write' }),
    () => tool(w.id + '-begin-deliver', 'todo_begin', { attempt_id: w.id, item_id: 'deliver' }),
    () => tool(w.id + '-done-deliver', 'todo_done', { attempt_id: w.id, item_id: 'deliver' }),
    () => ({ type: 'text', text: 'WORKER_DONE ' + w.id }),
  ][n]?.() ?? { type: 'text', text: 'WORKER_IDLE' };
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      // Everything the model receives: system prompt, messages and the tool schemas.
      await post('/test/request', { session: options.sessionId, tools: (options.tools ?? []).map(t => t.name),
        text: JSON.stringify({ messages: options.messages, tools: options.tools }) });
      let block;
      const mine = W.find(w => options.sessionId !== 'director' && text.includes('WORKER_FOR ' + w.id));
      if (!options.tools?.length) block = { type: 'text', text: 'Fixture session' };
      else if (mine) { const n = steps.get(options.sessionId) ?? 0; steps.set(options.sessionId, n + 1); block = worker(mine, n); }
      else if (options.sessionId !== 'director') block = { type: 'text', text: 'Fixture session' };
      else block = once('take1', () => tool('take-w1', 'work_update', { id: W[0].id, title: 'first', holder: 'director' }))
        ?? once('take2', () => tool('take-w2', 'work_update', { id: W[1].id, title: 'second', holder: 'director' }))
        // A background job the director's shell starts, in a session of its own (so it outlives
        // the shell call), then watched: the job carries the director's session tag.
        ?? once('job', () => tool('job', 'bash', { description: 'start a background job', run_in_background: false,
          command: "(setsid sleep 1 >/dev/null 2>&1 & echo WATCH_PID=$!)" }))
        ?? once('watch', () => tool('watch', 'watch_process', { pid: Number(/WATCH_PID=(\d+)/.exec(text)?.[1]), note: 'the sleep job' }))
        ?? once('held', () => tool('held-sleep', 'idle_dormant', { reason: 'nothing to do yet', wake_after_minutes: 60 }))
        ?? once('plan1', () => plan(W[0])) ?? once('plan2', () => plan(W[1]))
        ?? once('spawn1', () => tool('spawn-w1', 'subagent', { description: 'worker ' + W[0].id, prompt: 'WORKER_FOR ' + W[0].id + ': run your plan', cwd: W[0].repo }))
        ?? once('spawn2', () => tool('spawn-w2', 'subagent', { description: 'worker ' + W[1].id, prompt: 'WORKER_FOR ' + W[1].id + ': run your plan', cwd: W[1].repo }))
        ?? once('dispatched', () => ({ type: 'text', text: 'DISPATCHED' }))
        ?? (text.includes('WORKER_DONE ' + W[0].id) && text.includes('WORKER_DONE ' + W[1].id)
          ? once('finish1', () => tool('finish-w1', 'todo_finish', { attempt_id: W[0].id }))
            ?? once('finish2', () => tool('finish-w2', 'todo_finish', { attempt_id: W[1].id }))
            ?? once('close1', () => tool('close-w1', 'work_update', { id: W[0].id, state: 'completed', holder: '' }))
            ?? once('close2', () => tool('close-w2', 'work_update', { id: W[1].id, state: 'completed', holder: '' }))
            ?? once('sleep', () => tool('sleep', 'idle_dormant', { reason: 'both work items delivered', wake_after_minutes: 60 }))
            ?? once('all', () => ({ type: 'text', text: 'ALL_DONE' }))
          : undefined)
        ?? { type: 'text', text: 'Director waiting.' };
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
    if (['tool/result', 'assistant/message'].includes(event.type)) void post('/test/event', { session: session.id, event });
  });
}
`;

export async function runStandalone(entry, { timeoutMs = 150000, keep = false, python = process.env.STANDALONE_PYTHON || 'python3' } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-director-standalone-'));
  const events = [], requests = [];
  let output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    if (req.url === '/test/event') events.push(body); else if (req.url === '/test/request') requests.push(body);
    res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const results = id => events.filter(e => e.event.type === 'tool/result' && e.event.data.message.toolCallId === id)
    .map(e => { const text = e.event.data.message.content.map(b => b.text ?? '').join(''); try { return JSON.parse(text); } catch { return text; } });
  const said = text => events.some(e => e.event.type === 'assistant/message' && JSON.stringify(e.event.data).includes(text));
  const problems = [];
  const check = (ok, what) => { if (!ok) problems.push(what); };
  try {
    const root = join(directory, 'director');
    await mkdir(join(root, 'inbox'), { recursive: true });
    const git = (cwd, ...args) => execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf8' }).trim();
    const work = [];
    for (const id of ['w1', 'w2']) {
      const repo = join(directory, 'repo-' + id); await mkdir(repo);
      git(repo, 'init', '-q', '-b', 'main'); await writeFile(join(repo, 'work.txt'), 'base\n');
      git(repo, 'add', '-A'); git(repo, '-c', 'user.name=base', '-c', 'user.email=base@example.invalid', 'commit', '-q', '-m', 'base');
      work.push({ id, repo, base: git(repo, 'rev-parse', 'HEAD') });
    }
    await writeFile(join(root, 'inbox', 'start.json'), JSON.stringify({ task: 'deliver w1 and w2' }));
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-standalone-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { id: 'tool-subagent', config: { provider: 'spawn', toolName: 'subagent', agentOptions: { provider: 'fixture', model: 'worker' } } },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-standalone-provider.mjs') },
        { id: 'dsh-director', name: join(plugin, 'index.mjs') },
        { id: 'director-local', name: join(plugin, 'hosts', 'local.mjs'), config: { root, sessionId: 'director',
          provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100, idleReviewMs: 600000, watchIntervalMs: 100, todo: { python } } }] },
    ]));
    // An allowlist, not a filter: the DSH process sees nothing of the caller's environment
    // but these, so no backend variable can reach it whatever the caller has set.
    const environment = Object.fromEntries(['PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'TZ', 'USER']
      .filter(k => process.env[k] !== undefined).map(k => [k, process.env[k]]));
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...environment,
      DSH_HOME: join(directory, 'home'), DSH_PERMISSION_MODE: 'danger-full-access',
      STANDALONE_DSH_ENTRY: entry, STANDALONE_URL: `http://127.0.0.1:${server.address().port}`, STANDALONE_WORK: JSON.stringify(work) } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);
    const deadline = Date.now() + timeoutMs;
    const jobEnded = () => requests.some(r => r.session === 'director' && r.text.includes('Background job finished'));
    while (!(said('ALL_DONE') && jobEnded()) && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 200));
    check(said('ALL_DONE'), 'the director never finished (ALL_DONE)');
    // The work-item half: both items were taken (created, held by the director, working)
    // before anything closed them, and dormancy was refused while they were held.
    for (const w of work) {
      const [taken] = results('take-' + w.id);
      check(taken?.item?.id === w.id && taken.item.holder === 'director' && taken.item.state === 'working',
        `${w.id}: not taken by the director: ${String(JSON.stringify(taken)).slice(0, 300)}`);
    }
    const [watched] = results('watch');
    check(watched?.watching === true, 'watch_process refused the director\'s own job: ' + String(JSON.stringify(watched)).slice(0, 300));
    check(jobEnded(), 'the watched job\'s end never reached the director');
    const [heldSleep] = results('held-sleep');
    check(heldSleep?.refused === 'live_claims' && JSON.stringify(heldSleep.node_keys) === JSON.stringify(work.map(w => w.id)),
      'dormancy was not refused for the held work items: ' + String(JSON.stringify(heldSleep)).slice(0, 300));
    for (const w of work) {
      const [planned] = results('plan-' + w.id);
      check(Array.isArray(planned?.items) && planned.items.join() === 'write,deliver', `${w.id}: plan not frozen as write,deliver: ${String(JSON.stringify(planned)).slice(0, 300)}`);
      const writes = results(w.id + '-done-write'), delivers = results(w.id + '-done-deliver');
      check(writes.at(-1)?.outcome === 'pass', `${w.id}: write item did not pass: ${JSON.stringify(writes).slice(0, 300)}`);
      check(delivers.at(-1)?.outcome === 'pass', `${w.id}: deliver item did not pass: ${JSON.stringify(delivers).slice(0, 300)}`);
      const [finished] = results('finish-' + w.id);
      check(finished?.candidate === true, `${w.id}: todo_finish gave no candidate: ${String(JSON.stringify(finished)).slice(0, 300)}`);
      if (finished?.candidate) {
        // Something was delivered: the candidate is a NEW commit (not the plan's base), it is
        // the workspace's HEAD, and the file it carries holds what the worker wrote.
        check(finished.artifact !== w.base, `${w.id}: the candidate is the plan's base - nothing was delivered`);
        check(finished.artifact === git(w.repo, 'rev-parse', 'HEAD'), `${w.id}: candidate artifact is not the worker's commit`);
        let delivered = '';
        try { delivered = git(w.repo, 'show', finished.artifact + ':work.txt'); } catch { /* reported below */ }
        check(delivered === w.id, `${w.id}: the candidate's work.txt is ${JSON.stringify(delivered)}, not what the worker wrote`);
        check((await stat(finished.report).catch(() => null)) !== null, `${w.id}: candidate report missing`);
      }
      // The worker sessions did the item work; the director never ran a worker's item.
      const begun = events.filter(e => e.event.type === 'tool/result' && e.event.data.message.toolCallId === w.id + '-begin-write');
      check(begun.length && begun.every(e => e.session !== 'director'), `${w.id}: the item was not begun by a worker session`);
    }
    const workers = new Set(events.filter(e => e.session !== 'director' && e.event.type === 'tool/result').map(e => e.session));
    check(workers.size === 2, `expected two worker sessions, saw ${workers.size}`);
    const [dormant] = results('sleep');
    check(dormant?.dormant === true, 'dormancy refused at the end: ' + String(JSON.stringify(dormant)).slice(0, 300));
    const items = JSON.parse(await readFile(join(root, 'work.json'), 'utf8').catch(() => 'null'))?.items;
    check(items?.length && items.every(i => i.state === 'completed' && !i.holder), 'work items not closed: ' + JSON.stringify(items));
    const directorTools = requests.find(r => r.session === 'director' && r.tools.length)?.tools ?? [];
    check(directorTools.includes('work_update') && directorTools.includes('note_write'), 'the host\'s tools are not shown to the director: ' + JSON.stringify(directorTools));
    check(requests.some(r => r.session === 'director' && r.text.includes('built-in local host')), 'the director never read the host note');
    const workerTools = requests.find(r => r.session !== 'director' && r.tools.length)?.tools ?? [];
    check(!workerTools.includes('subagent') && !workerTools.includes('todo_plan') && workerTools.includes('todo_done'),
      'worker tool list is not the worker role: ' + JSON.stringify(workerTools));
    check(proc.exitCode === null, 'DSH exited: ' + output.slice(-1500));
    // The model-facing text of this backend-free run carries none of the backend's vocabulary.
    check(requests.length > 0 && requests.every(r => typeof r.text === 'string' && r.text.length > 0), 'request text was not recorded');
    for (const [i, r] of requests.entries()) {
      const found = leaks(r.text);
      if (found.length) problems.push(`request ${i} (${r.session === 'director' ? 'director' : 'worker'}) carries ${found.join(', ')}`);
    }
    return { ok: problems.length === 0, problems, workers: [...workers], requests: requests.length,
      texts: requests.map(r => r.text),
      output: problems.length ? output.slice(-3000) : undefined };
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!keep) await rm(directory, { recursive: true, force: true });
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const entry = process.argv[2];
  if (!entry) { console.error('usage: node scenario.mjs <dsh entry>'); process.exit(2); }
  const result = await runStandalone(entry, { keep: !!process.env.STANDALONE_KEEP });
  const { texts, ...summary } = result;
  console.log(JSON.stringify({ ...summary, request_text_chars: texts.reduce((n, t) => n + t.length, 0) }, null, 2));
  process.exit(result.ok ? 0 : 1);
}
