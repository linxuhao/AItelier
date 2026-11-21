/** Plugin step 9 on the measured DSH pin: worker capacity with a reserved slot.
 *  DSH (maxActiveSubagents 3) and dsh-director (the same 3, reserve 1) with the
 *  built-in local host: the director starts two workers that hold, its third dispatch
 *  is refused at_capacity (not DSH's error), one worker finishes, the director is
 *  woken once with "worker slot free", and the dispatch it then makes starts. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutHostWiring } from './dsh-entry.mjs';

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.CAP_TEST_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-capacity-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.CAP_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) });
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const spawnChild = (id, who) => tool(id, 'subagent', { description: 'worker ' + who,
  prompt: 'HOLD_CHILD ' + who + '\nattempt_id: att-' + who + '\nnode_key: node-' + who });
const MODE = process.env.CAP_TEST_MODE ?? 'main';
export function apply(ctx) {
  const done = new Set();
  const once = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      let block;
      const who = /HOLD_CHILD (\w)/.exec(text)?.[1];
      if (options.sessionId !== 'cap-root' && who) {
        if (!done.has('held-' + options.sessionId)) { done.add('held-' + options.sessionId); await post('/test/hold', { who }); }
        block = { type: 'text', text: 'CHILD_DONE ' + who };
      } else if (!options.tools?.length || options.sessionId !== 'cap-root') block = { type: 'text', text: 'Fixture session' };
      else if (MODE === 'later-boot') block = { type: 'text', text: text.includes('Worker slot free') ? 'WOKEN' : 'Director idle.' };
      else if (MODE === 'race') block = once('race', () => ['P', 'Q', 'R', 'S'].map(who => spawnChild('spawn-' + who.toLowerCase(), who)))
        ?? once('wait', () => ({ type: 'text', text: 'WAITING_FOR_SLOT' })) ?? { type: 'text', text: 'Director idle.' };
      else block = once('a', () => spawnChild('spawn-a', 'A')) ?? once('b', () => spawnChild('spawn-b', 'B'))
        ?? once('c', () => spawnChild('spawn-c', 'C'))
        ?? once('wait', () => ({ type: 'text', text: 'WAITING_FOR_SLOT' }))
        ?? (text.includes('Worker slot free') ? once('d', () => spawnChild('spawn-d', 'D')) : undefined)
        ?? (done.has('d') ? once('ok', () => ({ type: 'text', text: 'DISPATCHED_AFTER_SLOT' })) : undefined)
        ?? { type: 'text', text: 'Director idle.' };
      const blocks = [block].flat();
      for (const [index, one] of blocks.entries()) {
        yield { type: 'block-start', index, blockType: one.type };
        if (one.type === 'text') yield { type: 'text-delta', index, text: one.text };
        else yield { type: 'tool-call-delta', index, id: one.id, name: one.name, argumentsDelta: one.arguments };
        yield { type: 'block-end', index, block: one };
      }
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } };
      yield { type: 'finish', reason: { kind: blocks[0].type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
  ctx.on('session/event', (session, event) => {
    if (session.id === 'cap-root' && ['tool/result', 'user/message', 'assistant/message'].includes(event.type))
      void post('/test/event', event).catch(() => {});
  });
}
`;

/** One fixture world: a director root, a DSH home, an HTTP side channel; `launch(mode)`
 *  boots DSH (cap 3) and dsh-director (host limit `hostLimit`, reserve 1) on it. */
async function harness(t, { hostLimit = 3 } = {}) {
  const entry = await measuredEntryForTest(t);
  if (!entry) return undefined;             // nothing pinned is installed: reported unmeasured
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-capacity-'));
  const h = { events: [], holds: new Map(), output: '', proc: undefined, directory };
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    if (req.url === '/test/event') { h.events.push(body); return res.end('{}'); }
    if (req.url === '/test/hold') { h.holds.set(body.who, res); return; }   // answered when the test releases it
    res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const root = join(directory, 'director');
  h.root = root;
  await mkdir(join(root, 'inbox'), { recursive: true });
  await writeFile(join(root, 'inbox', 'start.json'), JSON.stringify({ task: 'dispatch' }));
  const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
  await writeFile(join(directory, 'fixture-capacity-provider.mjs'), PROVIDER);
  await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
  await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
    { id: 'hmr', disabled: true },
    { id: 'subagent', config: { maxActiveSubagents: 3 } },
    { id: 'tool-subagent', config: { provider: 'spawn', toolName: 'subagent', agentOptions: { provider: 'fixture', model: 'worker' } } },
    ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
    { insert: [{ id: 'fixture', name: join(directory, 'fixture-capacity-provider.mjs') },
      { id: 'dsh-director', name: join(here, '../dsh-director/index.mjs') },
      { id: 'director-local', name: join(here, '../dsh-director/hosts/local.mjs'), config: { root, sessionId: 'cap-root',
        provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100, idleReviewMs: 600000,
        maxActiveSubagents: hostLimit, reservedSlots: 1 } }] },
  ]));
  const environment = withoutHostWiring(process.env);
  h.launch = mode => {
    h.output = '';
    h.proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...environment,
      DSH_HOME: join(directory, 'home'), DSH_PERMISSION_MODE: 'danger-full-access', CAP_TEST_DSH_ENTRY: entry, CAP_TEST_MODE: mode,
      CAP_TEST_URL: `http://127.0.0.1:${server.address().port}` } });
    h.proc.stdout.on('data', chunk => h.output += chunk); h.proc.stderr.on('data', chunk => h.output += chunk);
  };
  h.stop = async () => {
    for (const res of h.holds.values()) res.end('{}');
    h.holds.clear();
    if (h.proc && h.proc.exitCode === null) { h.proc.kill('SIGTERM'); await once(h.proc, 'exit'); }
  };
  h.result = id => h.events.find(e => e.type === 'tool/result' && e.data.message.toolCallId === id);
  h.text = id => h.result(id)?.data.message.content.map(b => b.text ?? '').join('');
  h.said = what => h.events.some(e => e.type === 'assistant/message' && JSON.stringify(e.data).includes(what));
  h.wakes = () => h.events.filter(e => e.type === 'user/message' && JSON.stringify(e.data).includes('Worker slot free'));
  h.until = async (what, met, ms = 60000) => {
    const deadline = Date.now() + ms;
    while (!met() && Date.now() < deadline && h.proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(met(), what + '\n' + JSON.stringify(h.events.map(e => [e.type, e.data?.message?.toolCallId ?? e.data?.source?.kind ?? ''])) + '\n' + h.output.slice(-3000));
  };
  h.release = who => { const res = h.holds.get(who); h.holds.delete(who); res?.end('{}'); };
  h.close = async () => {
    await h.stop();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  };
  return h;
}
const wakeBody = event => JSON.parse(/\{.*\}/s.exec((event.data.message ?? event.data).content.map(b => b.text ?? '').join(''))[0]);

test('real pinned DSH: a dispatch past capacity minus the reserved slot is refused at_capacity, naming it, never above DSH\'s limit, and the director is woken once when a slot frees', { timeout: 120000 }, async t => {
  const h = await harness(t, { hostLimit: 5 });   // the host asks for more than DSH runs with
  if (!h) return;
  try {
    h.launch('main');
    await h.until('the director never reached the capacity refusal', () => h.said('WAITING_FOR_SLOT') && h.holds.size === 2);
    assert.doesNotMatch(h.text('spawn-a'), /at_capacity/); assert.doesNotMatch(h.text('spawn-b'), /at_capacity/);
    const refusal = JSON.parse(/\{.*\}/s.exec(h.text('spawn-c'))[0]);
    assert.deepEqual({ ...refusal, reason: undefined }, { refused: 'at_capacity', live: 2, capacity: 3, plugin_limit: 5, dsh_limit: 3,
      reserved: 1, dispatch: { description: 'worker C', attempt_id: 'att-C', node: 'node-C' }, reason: undefined },
      'capacity is DSH\'s 3, not the host\'s 5; the refusal names the dispatch');
    assert.doesNotMatch(h.text('spawn-c'), /ACTIVATION_LIMIT_REACHED/, 'the director met DSH\'s error, not the guard');
    assert.equal(h.holds.size, 2, 'the refused worker started anyway');
    h.release('A');
    await h.until('no slot-free wake after a worker finished', () => h.said('DISPATCHED_AFTER_SLOT'));
    assert.equal(h.wakes().length, 1, 'woken once');
    const wake = wakeBody(h.wakes()[0]);
    assert.deepEqual(wake.refused.map(({ at, ...entry }) => entry), [{ description: 'worker C', attempt_id: 'att-C', node: 'node-C', by: 'plugin' }]);
    assert.deepEqual({ capacity: wake.capacity, dsh_limit: wake.dsh_limit, plugin_limit: wake.plugin_limit, reserved: wake.reserved },
      { capacity: 3, dsh_limit: 3, plugin_limit: 5, reserved: 1 });
    assert.doesNotMatch(h.text('spawn-d'), /at_capacity|ACTIVATION_LIMIT_REACHED/);
    await h.until('the dispatch after the wake did not start', () => h.holds.has('D'));
    h.release('B'); h.release('D');
    await new Promise(resolve => setTimeout(resolve, 1000));
    assert.equal(h.wakes().length, 1, 'woken again although no dispatch was refused since');
    assert.equal(h.proc.exitCode, null, h.output.slice(-2000));
  } finally { await h.close(); }
});

test('real pinned DSH: a dispatch refused before a restart wakes the director once after it, and never again', { timeout: 180000 }, async t => {
  const h = await harness(t);
  if (!h) return;
  try {
    h.launch('main');
    await h.until('the director never reached the capacity refusal', () => h.said('WAITING_FOR_SLOT') && h.holds.size === 2);
    assert.match(h.text('spawn-c'), /at_capacity/);
    await h.stop();                         // restart with the refusal still waiting
    assert.equal(h.wakes().length, 0, 'woken before any slot freed');
    h.launch('later-boot');
    await h.until('no slot-free wake after the restart', () => h.said('WOKEN'));
    assert.equal(h.wakes().length, 1, 'woken once after the restart');
    assert.deepEqual(wakeBody(h.wakes()[0]).refused.map(entry => entry.attempt_id), ['att-C']);
    await new Promise(resolve => setTimeout(resolve, 1500));
    assert.equal(h.wakes().length, 1, 'woken twice in one boot');
    await h.stop();
    assert.deepEqual(JSON.parse(await readFile(join(h.root, 'state', 'refused-dispatches.json'), 'utf8')), [], 'the named refusal was not dropped');
    const reports = async () => (await readFile(join(h.root, 'progress.jsonl'), 'utf8')).split('\n').filter(Boolean).length;
    const before = await reports();
    h.launch('later-boot');                 // third boot: nothing refused is left
    let after = before;
    const polling = (async () => { while (after === before && h.proc.exitCode === null) { after = await reports(); await new Promise(resolve => setTimeout(resolve, 100)); } })();
    await h.until('third boot never reported running', () => after > before);
    await polling;
    await new Promise(resolve => setTimeout(resolve, 5000));
    assert.equal(h.wakes().length, 1, 'the wake came again on the third boot');
    assert.equal(h.proc.exitCode, null, h.output.slice(-2000));
  } finally { await h.close(); }
});

test('real pinned DSH: parallel dispatches past capacity are refused - by the guard, or by DSH and then treated as refused', { timeout: 120000 }, async t => {
  const h = await harness(t);
  if (!h) return;
  try {
    h.launch('race');
    await h.until('the four parallel dispatches never all answered', () => ['p', 'q', 'r', 's'].every(x => h.result('spawn-' + x)) && h.said('WAITING_FOR_SLOT'));
    const answers = Object.fromEntries(['p', 'q', 'r', 's'].map(x => [x, /at_capacity/.test(h.text('spawn-' + x)) ? 'guard'
      : /ACTIVATION_LIMIT_REACHED|limit reached/.test(h.text('spawn-' + x)) ? 'dsh' : 'started']));
    t.diagnostic('answers: ' + JSON.stringify(answers));
    const refused = Object.entries(answers).filter(([, how]) => how !== 'started').map(([x]) => x.toUpperCase());
    assert.deepEqual(answers, { p: 'started', q: 'started', r: 'guard', s: 'guard' },
      'a parallel batch ran past the reserved slot (the guard counts dispatches it let through, not only started children)');
    await h.until('the started workers did not hold', () => h.holds.size === 4 - refused.length);
    h.release([...h.holds.keys()][0]);
    await h.until('no slot-free wake after a worker finished', () => h.wakes().length === 1);
    const named = wakeBody(h.wakes()[0]).refused;
    assert.deepEqual(named.map(entry => entry.attempt_id).sort(), refused.map(x => 'att-' + x).sort(), 'every refusal, guard or DSH, is named');
    assert.deepEqual(named.map(entry => entry.by).sort(), refused.map(x => answers[x.toLowerCase()] === 'guard' ? 'plugin' : 'dsh').sort());
  } finally { await h.close(); }
});
