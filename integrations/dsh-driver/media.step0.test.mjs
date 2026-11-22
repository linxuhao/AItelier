/** Step 24.2, step 0 item 2 (design-steward 7.8.12, 7.8.19): does the pinned DSH write the
 *  arguments of a NESTED tool call - one a tool makes through ctx.tools.execute, as
 *  media_call does when it fills a media file's base64 into the named argument host-side -
 *  into its session log? Measured in a real pinned DSH with a fixture model: the model calls
 *  call_tool({name: 'probe_outer'}); probe_outer calls probe_inner with an argument the model
 *  never saw (a fresh marker); afterwards every file under the DSH home and the director's
 *  state directory is searched for that marker. The model's own call_tool arguments are
 *  logged as usual; the nested arguments must not be anywhere - that is what lets
 *  media_call pass base64 instead of the gateway-side upload the design names as the
 *  alternative. If DSH ever starts logging nested arguments this test goes red. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { randomBytes } from 'node:crypto';
import { measuredEntryForTest, withoutHostWiring } from './dsh-entry.mjs';

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.S0_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
const { defineTool } = await import(require.resolve('@deepseek-ai/dsh-tools'));
export const name = 'fixture-step0-provider';
export const inject = ['llm', 'tools'];
const post = (path, body) => fetch(process.env.S0_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
const output = { schema: { type: 'json' }, render: (_a, v) => [{ type: 'text', text: JSON.stringify(v) }] };
export function apply(ctx) {
  let calls = 0;
  ctx.tools.register(defineTool({ name: 'probe_inner', description: 'step 0 probe (inner)',
    parameters: { payload: { type: 'string', required: true } }, output,
    execute: async args => { await post('/test/inner', { length: args.payload.length }); return { ok: true, length: args.payload.length }; } }));
  ctx.tools.register(defineTool({ name: 'probe_outer', description: 'step 0 probe (outer)', parameters: {}, output,
    async execute(_args, exec) {
      const result = await ctx.tools.execute({ name: 'probe_inner', arguments: { payload: process.env.S0_MARKER.repeat(64) },
        agent: exec.agent, callId: exec.callId + ':probe', rootCallId: exec.rootCallId, parent: exec.token, signal: exec.signal });
      return { nested: !result.isError };
    } }));
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      let block;
      if (!options.tools?.length || options.sessionId !== 's0-root' || options.purpose === 'compaction') block = { type: 'text', text: 'Fixture session' };
      else if (calls++ === 0) block = { type: 'tool-call', id: 'p1', name: 'call_tool', arguments: JSON.stringify({ name: 'probe_outer', arguments: {} }) };
      else block = { type: 'text', text: 'PROBE_DONE' };
      yield { type: 'block-start', index: 0, blockType: block.type };
      if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text };
      else yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments };
      yield { type: 'block-end', index: 0, block };
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20 } };
      yield { type: 'finish', reason: { kind: block.type === 'text' ? 'stop' : 'tool-calls' } };
      if (block.type === 'text' && block.text === 'PROBE_DONE') await post('/test/done', {});
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
  ctx.on('session/event', (session, event) => { if (session.id === 's0-root') void post('/test/event', event); });
}
`;

async function filesUnder(root) {
  const out = [];
  for (const entry of await readdir(root, { withFileTypes: true }).catch(() => [])) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) out.push(...await filesUnder(path));
    else if (entry.isFile()) out.push(path);
  }
  return out;
}

test('step 0 item 2 (real pinned DSH): a nested tool call\'s arguments are not written to the session log', { timeout: 120000 }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-step0-'));
  const root = join(directory, 'director');
  const marker = 'NESTED_ARG_MARKER_' + randomBytes(8).toString('hex');
  const events = [];
  let output = '', proc, done = false, inner = 0;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    if (req.url === '/test/event') events.push(body);
    else if (req.url === '/test/done') done = true;
    else if (req.url === '/test/inner') inner += 1;
    res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    await mkdir(join(root, 'inbox'), { recursive: true });
    await writeFile(join(root, 'inbox', 'start.json'), JSON.stringify({ task: 'probe' }));
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-step0-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-step0-provider.mjs') },
        { id: 'director', name: join(here, '../dsh-director/index.mjs') },
        { id: 'director-local', name: join(here, '../dsh-director/hosts/local.mjs'), config: { root, sessionId: 's0-root',
          provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100, idleReviewMs: 600000 } }] },
    ]));
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutHostWiring(process.env),
      DSH_HOME: join(directory, 'home'), S0_DSH_ENTRY: entry, S0_MARKER: marker, S0_URL: `http://127.0.0.1:${server.address().port}` } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);
    const deadline = Date.now() + 90000;
    while (!done && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(done, 'the probe turn never finished\n' + output.slice(-3000));
    assert.equal(inner, 1, 'the nested call ran once');
    const result = events.find(e => e.type === 'tool/result' && e.data?.message?.toolCallId === 'p1');
    assert.match(JSON.stringify(result?.data?.message ?? {}), /nested/, 'call_tool returned the outer result');
    proc.kill('SIGTERM'); await once(proc, 'exit');
    // Every byte DSH and the director wrote: the session log, the director's own state.
    const files = [...await filesUnder(join(directory, 'home')), ...await filesUnder(root)];
    assert.ok(files.length > 0, 'nothing was written');
    const sessionFiles = files.filter(f => /session|\.jsonl$/i.test(f));
    t.diagnostic(`searched ${files.length} file(s), ${sessionFiles.length} session-looking: ${sessionFiles.map(f => f.slice(directory.length)).join(', ').slice(0, 600)}`);
    const logged = [];
    for (const file of files) if ((await readFile(file)).includes(marker)) logged.push(file.slice(directory.length));
    // The model's own call is in the log (proof the log was searched at all).
    let modelCall = false;
    for (const file of files) if ((await readFile(file)).includes('probe_outer')) modelCall = true;
    assert.ok(modelCall, 'the model\'s own call_tool call is in what DSH wrote');
    assert.deepEqual(logged, [], 'the nested call\'s arguments were written to: ' + logged.join(', '));
    assert.ok(!events.some(e => JSON.stringify(e).includes(marker)), 'no session event carries the nested arguments');
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
