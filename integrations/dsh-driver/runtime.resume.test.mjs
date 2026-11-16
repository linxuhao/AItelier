/** Plugin step 6 on the measured DSH pin: a worker a host restart interrupted is resumed
 *  in its own session. Three boots of DSH + dsh-director with the built-in local host:
 *  1. the director plans att-R and dispatches worker W; W begins item `write` and is
 *     killed with the host while its turn is open;
 *  2. the director resumes W (same session id) with the recovery note; W makes the
 *     change and passes `write`; the director is woken once naming W as resumed;
 *  3. nothing is resumed again, and the logs load. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutCallerMode } from './dsh-entry.mjs';

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.RES_TEST_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-resume-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.RES_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) });
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const MODE = process.env.RES_TEST_MODE, REPO = process.env.RES_TEST_REPO, BASE = process.env.RES_TEST_BASE;
const PLAN = { template: 'change_files', params: { paths: ['a.txt'], scope: ['a.txt'] }, goal: 'write a.txt', attempt_id: 'att-R',
  node_key: 'n-R', context_hash: 'ctx-R', base_sha: BASE, workspace: REPO };
export function apply(ctx) {
  const done = new Set();
  const once = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      const director = options.sessionId === 'res-root';
      let block;
      if (!options.tools?.length) block = { type: 'text', text: 'Fixture session' };
      else if (director) block = MODE === 'b1'
        ? once('plan', () => tool('d-plan', 'todo_plan', PLAN))
          ?? once('spawn', () => tool('d-spawn', 'subagent', { description: 'worker R', prompt: 'RESUME_CHILD attempt_id: att-R item write' }))
          ?? { type: 'text', text: 'DISPATCHED' }
        : { type: 'text', text: text.includes('Workers resumed after a restart') ? 'SAW_RESUME' : 'Director idle.' };
      else if (text.includes('RESUME_CHILD')) {
        if (MODE === 'b1') block = once('begin', () => tool('w-begin', 'todo_begin', { attempt_id: 'att-R', item_id: 'write' }))
          ?? (await post('/test/hold', { who: options.sessionId }), { type: 'text', text: 'never reached' });
        else if (process.env.RES_TEST_STUBBORN && text.includes('You were interrupted')) block = (await post('/test/hold', { who: options.sessionId }), { type: 'text', text: 'never reached' });
        else if (text.includes('You were interrupted')) block = once('write', () => tool('w-write', 'bash', { description: 'make the change',
            command: 'cd ' + REPO + ' && echo resumed > a.txt && git -c user.name=t -c user.email=t@t add a.txt && git -c user.name=t -c user.email=t@t commit -qm resumed', run_in_background: false }))
          ?? once('done', () => tool('w-done', 'todo_done', { attempt_id: 'att-R', item_id: 'write' }))
          ?? { type: 'text', text: 'RESUMED_DONE' };
        else block = { type: 'text', text: 'Worker idle.' };
      } else block = { type: 'text', text: 'Fixture session' };
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
    if (['tool/result', 'user/message', 'assistant/message', 'turn/start', 'turn/end'].includes(event.type))
      void post('/test/event', { sid: session.id, boot: MODE, event }).catch(() => {});
  });
}
`;

async function world(t, run, { stubborn = false } = {}) {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-resume-'));
  const seen = [], holds = new Map();
  let output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    if (req.url === '/test/event') { seen.push(body); return res.end('{}'); }
    if (req.url === '/test/hold') { holds.set(body.who, res); return; }
    res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const root = join(directory, 'director'), repo = join(directory, 'repo');
  const git = (...args) => execFileSync('git', ['-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@t', ...args], { encoding: 'utf8' }).trim();
  const until = async (what, met, ms = 60000) => {
    const deadline = Date.now() + ms;
    while (!met() && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(met(), what + '\n' + JSON.stringify(seen.map(e => [e.boot, e.sid.slice(0, 8), e.event.type, e.event.data?.message?.toolCallId ?? e.event.data?.reason?.kind ?? ''])) + '\n' + output.slice(-3000));
  };
  const result = id => seen.find(e => e.event.type === 'tool/result' && e.event.data.message.toolCallId === id);
  const textOf = e => JSON.stringify(e.event.data);
  const stop = async () => {
    for (const res of holds.values()) res.destroy();
    holds.clear();
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
  };
  try {
    await mkdir(join(root, 'inbox'), { recursive: true }); await mkdir(repo);
    execFileSync('git', ['init', '-q', repo]); await writeFile(join(repo, 'README'), 'x\n'); git('add', 'README'); git('commit', '-qm', 'base');
    const base = git('rev-parse', 'HEAD');
    await writeFile(join(root, 'inbox', 'start.json'), JSON.stringify({ task: 'plan and dispatch att-R' }));
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-resume-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { id: 'tool-subagent', config: { provider: 'spawn', toolName: 'subagent', agentOptions: { provider: 'fixture', model: 'worker' } } },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-resume-provider.mjs') },
        { id: 'dsh-director', name: join(here, '../dsh-director/index.mjs') },
        { id: 'director-local', name: join(here, '../dsh-director/hosts/local.mjs'), config: { root, sessionId: 'res-root',
          provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100, idleReviewMs: 600000 } }] },
    ]));
    const environment = Object.fromEntries(Object.entries(withoutCallerMode(process.env)).filter(([k]) => !k.startsWith('AITELIER_')));
    const launch = mode => {
      output = '';
      proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...environment, ...(stubborn ? { RES_TEST_STUBBORN: '1' } : {}),
        DSH_HOME: join(directory, 'home'), DSH_PERMISSION_MODE: 'danger-full-access', RES_TEST_DSH_ENTRY: entry, RES_TEST_MODE: mode,
        RES_TEST_REPO: repo, RES_TEST_BASE: base, RES_TEST_URL: `http://127.0.0.1:${server.address().port}` } });
      proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);
    };

    const reports = async () => (await readFile(join(root, 'progress.jsonl'), 'utf8')).split('\n').filter(Boolean).length;
    const booted = async mode => {
      const before = await reports();
      launch(mode);
      let after = before;
      const polling = (async () => { while (after === before && proc.exitCode === null) { after = await reports(); await new Promise(resolve => setTimeout(resolve, 100)); } })();
      await until(mode + ' never reported running', () => after > before);
      await polling;
    };
    await run({ launch, booted, stop, until, result, textOf, seen, holds, root, output: () => output, proc: () => proc });
  } finally {
    await stop();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
}

test('real pinned DSH: a worker the host restart interrupted is resumed in its own session, finishes its item, and is not resumed again', { timeout: 240000 }, async t => {
  await world(t, async ({ launch, booted, stop, until, result, textOf, seen, holds, root, output, proc }) => {
    // Boot 1: plan, dispatch, the worker begins `write` and is killed mid-turn.
    launch('b1');
    await until('the worker never began its item and held', () => result('w-begin') && holds.size === 1);
    assert.match(textOf(result('d-plan')), /att-R/, 'todo_plan failed: ' + textOf(result('d-plan')));
    assert.doesNotMatch(textOf(result('w-begin')), /refused/, textOf(result('w-begin')));
    const worker = result('w-begin').sid;
    await stop();

    // Boot 2: resumed in its own session; it passes `write`.
    launch('b2');
    await until('the worker was not resumed and did not finish', () => result('w-done') && seen.some(e => e.boot === 'b2' && e.sid === 'res-root' && e.event.type === 'assistant/message' && textOf(e).includes('SAW_RESUME')), 90000);
    assert.equal(result('w-done').sid, worker, 'the item was finished by another session');
    assert.match(textOf(result('w-done')), /"outcome\\?":\\?"pass/, textOf(result('w-done')));
    const notes = seen.filter(e => e.sid === worker && e.event.type === 'user/message' && textOf(e).includes('You were interrupted'));
    assert.equal(notes.length, 1, 'resumed once');
    assert.match(textOf(notes[0]), /re-read git status/);
    const wakes = seen.filter(e => e.sid === 'res-root' && e.event.type === 'user/message' && textOf(e).includes('Workers resumed after a restart'));
    assert.equal(wakes.length, 1, 'one wake');
    assert.match(textOf(wakes[0]), new RegExp(`att-R.*${worker}`));
    assert.equal(JSON.parse(await readFile(join(root, 'state', 'resume', encodeURIComponent(worker) + '.json'), 'utf8')).resumes.length, 1);
    await until('the resumed worker did not end its turn', () => seen.some(e => e.boot === 'b2' && e.sid === worker && e.event.type === 'assistant/message' && textOf(e).includes('RESUMED_DONE')));
    await stop();

    // Boot 3: nothing left to resume.
    await booted('b3');
    await new Promise(resolve => setTimeout(resolve, 4000));
    assert.equal(seen.filter(e => e.sid === worker && e.event.type === 'user/message' && textOf(e).includes('You were interrupted')).length, 1, 'resumed again on the third boot');
    assert.equal(seen.filter(e => e.sid === 'res-root' && e.event.type === 'user/message' && textOf(e).includes('Workers resumed after a restart')).length, 1, 'a second resume wake');
    assert.equal(proc().exitCode, null, output().slice(-2000));
    assert.doesNotMatch(output(), /resume: /, 'the resume scan failed: ' + output().slice(-2000));
  });
});

test('real pinned DSH: a worker interrupted again is resumed at most twice; the third restart reports it failed with the way on', { timeout: 300000 }, async t => {
  await world(t, async ({ launch, booted, stop, until, result, textOf, seen, holds, root, output, proc }) => {
    launch('b1');
    await until('the worker never began its item and held', () => result('w-begin') && holds.size === 1);
    const worker = result('w-begin').sid;
    const notes = () => seen.filter(e => e.sid === worker && e.event.type === 'user/message' && textOf(e).includes('You were interrupted'));
    const body = e => JSON.parse(e.event.data.content.map(b => b.text ?? '').join('').split('\n').slice(1).join('\n'));
    const wakes = () => seen.filter(e => e.sid === 'res-root' && e.event.type === 'user/message' && textOf(e).includes('Workers resumed after a restart'));
    for (const [boot, n] of [['b2', 1], ['b3', 2]]) {
      await stop();
      launch(boot);
      await until(boot + ': not resumed', () => notes().length === n && holds.size === 1 && wakes().length === n);
      assert.deepEqual(body(wakes().at(-1)).resumed.map(r => [r.attempt_id, r.child_session]), [['att-R', worker]]);
    }
    await stop();
    await booted('b4');
    await until('b4: no wake naming the refused resume', () => wakes().length === 3);
    await new Promise(resolve => setTimeout(resolve, 1500));
    assert.equal(notes().length, 2, 'resumed a third time');
    const last = body(wakes().at(-1));
    assert.deepEqual(last.resumed, []);
    assert.deepEqual(last.failed.map(f => [f.attempt_id, f.child_session, f.reason]), [['att-R', worker, 'resume_limit_reached']]);
    assert.match(last.failed_next, /todo_rebind/, 'the wake does not name the way on');
    assert.equal(JSON.parse(await readFile(join(root, 'state', 'resume', encodeURIComponent(worker) + '.json'), 'utf8')).resumes.length, 2);
    assert.equal(proc().exitCode, null, output().slice(-2000));
  }, { stubborn: true });
});
