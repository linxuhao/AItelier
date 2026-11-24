/** dsh-director with its built-in local host and NO AItelier: a real pinned DSH runs
 *  the director plugin and hosts/local.mjs only (no adapter, no backend). It lives in
 *  this suite because this suite owns the measured DSH pin (dsh-entry.mjs).
 *
 *  Script: an inbox file starts a turn -> the director takes a work item -> dormancy
 *  is refused for the held work -> the item is completed and released -> dormancy is
 *  entered -> a second inbox file wakes it. The progress log shows the phases. */
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
const require = createRequire(process.env.LOCAL_TEST_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-local-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.LOCAL_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
export function apply(ctx) {
  const done = new Set();
  const once = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      await post('/test/request', { session: options.sessionId, tools: (options.tools ?? []).map(t => t.name) });
      let block;
      if (!options.tools?.length || options.sessionId !== 'local-root') block = { type: 'text', text: 'Fixture session' };
      else block = once('take', () => tool('take', 'work_update', { id: 'w1', title: 'local work', holder: 'local-root' }))
        ?? once('refused', () => tool('dormant-refused', 'idle_dormant', { reason: 'quiet', wake_after_minutes: 15 }))
        ?? once('release', () => tool('release', 'work_update', { id: 'w1', state: 'completed', holder: '' }))
        ?? once('note', () => tool('note', 'note_write', { text: 'LOCAL_NOTE_MARKER', request_key: 'note-1' }))
        ?? once('dormant', () => tool('dormant', 'idle_dormant', { reason: 'nothing left', wake_after_minutes: 15 }))
        ?? once('parked', () => ({ type: 'text', text: 'PARKED' }))
        ?? (text.includes('wake.json') ? once('woken', () => ({ type: 'text', text: 'WOKEN' })) : undefined)
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
    if (session.id === 'local-root' && ['tool/result', 'user/message', 'assistant/message'].includes(event.type)) void post('/test/event', event);
  });
}
`;

test('real pinned DSH: dsh-director with its built-in local host, no AItelier: held work refuses dormancy, an inbox file wakes it', { timeout: 120000 }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;                       // nothing pinned is installed: reported unmeasured
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-local-host-'));
  const root = join(directory, 'director');
  const events = [], requests = [];
  let output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    if (req.url === '/test/event') events.push(body);
    else if (req.url === '/test/request') requests.push(body);
    res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const result = id => events.find(e => e.type === 'tool/result' && e.data.message.toolCallId === id);
  const json = id => JSON.parse(result(id).data.message.content.map(b => b.text ?? '').join(''));
  const said = text => events.some(e => e.type === 'assistant/message' && JSON.stringify(e.data).includes(text));
  const until = async (what, met, ms = 60000) => {
    const deadline = Date.now() + ms;
    while (!met() && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(met(), what + '\n' + JSON.stringify(events.map(e => [e.type, e.data?.message?.toolCallId ?? e.data?.source?.kind ?? ''])) + '\n' + output.slice(-3000));
  };
  try {
    await mkdir(join(root, 'inbox'), { recursive: true });
    await writeFile(join(root, 'inbox', 'start.json'), JSON.stringify({ task: 'begin' }));
    // A notice a previous process recorded but never delivered (plugin step 7): the boot
    // replays it once; the next boot does not replay it again.
    await mkdir(join(root, 'state'), { recursive: true });
    await writeFile(join(root, 'state', 'outbox.jsonl'), JSON.stringify({ id: 'test:replay-1', text: 'REPLAYED_NOTICE_MARKER' }) + '\n');
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-local-provider.mjs'), PROVIDER);
    await writeFile(join(directory, 'probe-host.mjs'), [
      `import { localHost } from ${JSON.stringify(join(here, '../dsh-director/hosts/local.mjs'))};`,
      "import { writeFileSync } from 'node:fs';",
      "export const name = 'probe-host'; export const inject = [];",
      'export function apply(ctx, { probeFile, ...config }) {',
      '  const host = localHost(config);',
      // The local host has an install() of its own (its startup checks): the probe runs first, then it.
      "  const own = host.install;",
      "  host.capabilities = [...new Set([...host.capabilities, 'install'])];",
      '  host.install = async api => {',
      "    let refusal = ''; try { await api.notify('too early'); } catch (error) { refusal = String(error.message); }",
      '    writeFileSync(probeFile, JSON.stringify({ keys: Object.keys(api), refusal, agentUndefined: api.agent === undefined }));',
      '    await own?.(api);',
      '  };',
      "  ctx.provide('directorHost', host);",
      '}'].join('\n'));
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-local-provider.mjs') },
        { id: 'director', name: join(here, '../dsh-director/index.mjs') },
        // The built-in local host, wrapped by a probe that records what install() receives.
        { id: 'director-local', name: join(directory, 'probe-host.mjs'), config: { probeFile: join(directory, 'probe.json'), root, sessionId: 'local-root',
          provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100,
          // Nothing but the root and the fixture model: the plugin's own roles.json, prompts and todo package.
          idleReviewMs: 600000 } }] },
    ]));
    // No AITELIER_* variable reaches this process: the director must not need one.
    const environment = withoutHostWiring(process.env);
    const launch = () => {
      const child = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...environment,
        DSH_HOME: join(directory, 'home'), LOCAL_TEST_DSH_ENTRY: entry, LOCAL_TEST_URL: `http://127.0.0.1:${server.address().port}` } });
      child.stdout.on('data', chunk => output += chunk); child.stderr.on('data', chunk => output += chunk);
      return child;
    };
    proc = launch();

    await until('the director never parked', () => said('PARKED'));
    // install() receives every DirectorApi member host.d.ts documents; notify refuses (naming
    // why) before the session exists, and agent is still undefined there.
    const probe = JSON.parse(await readFile(join(directory, 'probe.json'), 'utf8'));
    const doc = await readFile(join(here, '../dsh-director/host.d.ts'), 'utf8');
    const body = doc.slice(doc.indexOf('export interface DirectorApi'), doc.indexOf('export interface DirectorHost'));
    // A member name starts a declaration: after '{', ';' or a line break - never a parameter.
    const documented = [...body.replace(/\/\*\*[\s\S]*?\*\//g, '').matchAll(/(?:^|[{;\n])\s*(?:readonly\s+)?([A-Za-z]+)\s*(?:\(|:)/g)].map(m => m[1]).filter(n => n !== 'export');
    assert.ok(documented.length >= 23, documented.join(', '));
    assert.deepEqual(documented.filter(n => !probe.keys.includes(n)), [], 'install() lacks documented members');
    assert.match(probe.refusal, /notify is not available before the director session exists/);
    assert.equal(probe.agentUndefined, true);
    assert.equal(json('take').item.holder, 'local-root');
    const refused = json('dormant-refused');
    assert.equal(refused.refused, 'live_claims', JSON.stringify(refused));
    assert.deepEqual(refused.node_keys, ['w1']);
    assert.equal(json('release').item.state, 'completed');
    assert.equal(json('dormant').dormant, true, JSON.stringify(json('dormant')));
    // The director's own tools only: no AItelier tool is registered or presented.
    const listed = requests.find(r => r.session === 'local-root' && r.tools.length)?.tools ?? [];
    assert.ok(listed.includes('idle_dormant') && listed.includes('todo_plan'), JSON.stringify(listed));
    assert.ok(!listed.some(name => /state_|project_work|send_user_message/.test(name)), JSON.stringify(listed));
    let progress = '';
    const deadline = Date.now() + 20000;
    while (!/"phase":"dormant"/.test(progress) && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 100));
      progress = await readFile(join(root, 'progress.jsonl'), 'utf8').catch(() => '');
    }
    assert.match(progress, /"phase":"dormant"/);
    await writeFile(join(root, 'inbox', 'wake.json'), JSON.stringify({ wake: true }));
    await until('an inbox file did not wake the dormant director', () => said('WOKEN'));
    const wake = events.find(e => e.type === 'user/message' && JSON.stringify(e.data).includes('wake.json'));
    assert.equal(wake.data.source.kind, 'dsh-director-runtime-event');
    assert.match(await readFile(join(root, 'notes.md'), 'utf8'), /LOCAL_NOTE_MARKER/);
    assert.equal(proc.exitCode, null, output.slice(-2000));
    const replays = () => events.filter(e => e.type === 'user/message' && JSON.stringify(e.data).includes('REPLAYED_NOTICE_MARKER')).length;
    assert.equal(replays(), 1, 'the recorded notice was not replayed exactly once');
    assert.match(await readFile(join(root, 'state', 'delivered.jsonl'), 'utf8'), /test:replay-1/);
    // A second boot of the same session: the log loads, nothing is replayed again, and an
    // inbox file still wakes it (two-restart rule for a change on the delivery path).
    proc.kill('SIGTERM'); await once(proc, 'exit');
    proc = launch();
    await writeFile(join(root, 'inbox', 'wake2.json'), JSON.stringify({ wake: 2 }));
    await until('the second boot did not deliver a new inbox file', () => events.some(e => e.type === 'user/message' && JSON.stringify(e.data).includes('wake2.json')));
    assert.doesNotMatch(output, /SessionPersistenceCorruptionError|stored log is corrupt/);
    assert.equal(replays(), 1, 'the second boot replayed a delivered notice');
    assert.equal(events.filter(e => e.type === 'user/message' && JSON.stringify(e.data).includes('(wake.json)')).length, 1, 'an inbox file was delivered twice');
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
