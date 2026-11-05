import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, rm, realpath } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

// Root script: one step of work and stop -> continuation question -> a check and
// NOTHING_LEFT -> idle (no second question) -> the review: start a detached job,
// dormancy refused for it, watch it, dormancy now allowed -> the job's exit wakes the
// director -> a tool-less turn -> the normal review is back.
const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-continuation-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.AITELIER_URL + path, { method: 'POST', body: JSON.stringify(body) });
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
const bash = (id, command) => tool(id, 'bash', { description: 'fixture', command, run_in_background: false });
export function apply(ctx) {
  const root = process.env.AITELIER_DSH_SESSION, done = new Set();
  const once = (key, make) => done.has(key) ? undefined : (done.add(key), make());
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      const questions = (text.match(/AItelier continuation check/g) ?? []).length;
      const pid = text.match(/JOB_PID=(\d+)/)?.[1];
      let block;
      if (!options.tools?.length || options.sessionId !== root) block = { type: 'text', text: 'Fixture session' };
      else block = once('step1', () => bash('root-step1', 'echo STEP1'))
        ?? once('stop1', () => ({ type: 'text', text: 'STOP1 one step done' }))
        ?? (questions >= 1 ? once('check', () => bash('root-check', 'echo CHECKED')) : undefined)
        ?? (questions >= 1 ? once('nothing', () => ({ type: 'text', text: 'NOTHING_LEFT: everything else waits on the owner' })) : undefined)
        ?? (text.includes('AItelier director review due') ? once('job', () => bash('root-job', 'setsid nohup sleep 3 >/dev/null 2>&1 < /dev/null & echo JOB_PID=$!')) : undefined)
        ?? (pid ? once('dormant-refused', () => tool('root-dormant-refused', 'idle_dormant', { reason: 'only the job is left' })) : undefined)
        ?? (pid ? once('watch', () => tool('root-watch', 'watch_process', { pid: Number(pid), note: 'fixture sleep' })) : undefined)
        ?? (pid ? once('dormant', () => tool('root-dormant', 'idle_dormant', { reason: 'only the watched job is left' })) : undefined)
        ?? (pid ? once('stop2', () => ({ type: 'text', text: 'STOP2 dormant, watching the job' })) : undefined)
        ?? (text.includes('AItelier background job finished') ? once('seen', () => ({ type: 'text', text: 'JOB_SEEN' })) : undefined)
        ?? { type: 'text', text: 'Director idle.' };
      await post('/test/request', { session: options.sessionId, block: block.type === 'text' ? block.text : block.name });
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
    if (session.id === root && ['tool/result', 'user/message', 'assistant/message'].includes(event.type)) void post('/test/event', { event });
  });
}
`;

test('real pinned DSH: continuation question, NOTHING_LEFT parks, background job refused then watched, its exit wakes, review resumes', { timeout: 60000 }, async () => {
  const directory = await mkdtemp(join(homedir(), 'dsh-continuation-'));
  const entry = await realpath(process.env.AITELIER_DSH_TEST_ENTRY ?? new URL('./node_modules/.bin/dsh', import.meta.url));
  const here = dirname(fileURLToPath(import.meta.url));
  const session = 'continuation-root-' + process.pid + '-' + Math.random().toString(36).slice(2, 8);
  const events = [], requests = [], parked = new Set();
  let output = '', proc;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [] } });
    if (req.url === '/api/state/query/list_claims') return send({ claims: [] });
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture', event_seq: 10,
      nodes: [], project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'fixture driver skill' });
    if (req.url.endsWith('/activity') || req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url === '/api/state/query/wait_for_state_change') { parked.add(res); res.on('close', () => parked.delete(res)); return; }
    if (req.url === '/test/request') { requests.push(body); return send({ ok: true }); }
    if (req.url === '/test/event') { events.push(body.event); return send({ ok: true }); }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const userText = e => e.data.content?.[0]?.text ?? '';
  const questions = () => events.filter(e => e.type === 'user/message' && e.data.source?.kind === 'aitelier-continue');
  const reviews = () => events.filter(e => e.type === 'user/message' && userText(e).startsWith('AItelier director review due:'));
  const finished = () => events.filter(e => e.type === 'user/message' && userText(e).startsWith('AItelier background job finished:'));
  const said = text => events.find(e => e.type === 'assistant/message' && JSON.stringify(e.data).includes(text));
  const result = id => events.find(e => e.type === 'tool/result' && e.data.message.toolCallId === id);
  const json = event => JSON.parse(event.data.message.content.map(b => b.text ?? '').join(''));
  const until = async (what, predicate, ms = 30000) => {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline && proc.exitCode === null && !predicate()) await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(proc.exitCode, null, output);
    assert.ok(predicate(), `${what}\nREQ ${JSON.stringify(requests.map(r => r.block))}\n${output.slice(-3000)}\n${JSON.stringify(events.map(e => [e.seq, e.type, e.data?.message?.toolCallId ?? e.data?.source?.kind ?? '']))}`);
  };
  try {
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-continuation-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-continuation-provider.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') }] },
    ]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...process.env,
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: session,
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'),
      AITELIER_DSH_EXECUTION: 'host', DSH_PERMISSION_MODE: 'danger-full-access',
      AITELIER_DSH_IDLE_REVIEW_MS: '1500', AITELIER_DSH_WATCH_INTERVAL_MS: '200' } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);

    await until('the NOTHING_LEFT answer never came', () => said('NOTHING_LEFT: everything else'));
    const parkedAt = said('NOTHING_LEFT: everything else').seq;
    // One stop after work was asked once; nothing before the first tool call.
    assert.equal(questions().length, 1, 'expected exactly one continuation question');
    assert.ok(questions()[0].seq > said('STOP1').seq);
    // NOTHING_LEFT parks: the next input is the review, not a second question.
    await until('the review never came after NOTHING_LEFT', () => reviews().some(r => r.seq > parkedAt));
    await until('the job was not started and handled', () => result('root-dormant'));
    const pid = Number(JSON.stringify(result('root-job').data).match(/JOB_PID=(\d+)/)[1]);
    const refused = json(result('root-dormant-refused'));
    assert.equal(refused.refused, 'background_processes', JSON.stringify(refused));
    assert.ok(refused.pids.includes(pid), `the detached job ${pid} must be found by its session: ${JSON.stringify(refused)}`);
    assert.equal(json(result('root-watch')).watching, true);
    assert.equal(json(result('root-dormant')).dormant, true, 'a watched job does not block dormancy: ' + JSON.stringify(json(result('root-dormant'))));
    // Dormant: the STOP2 turn is not asked.
    // The job's exit wakes the director with the pid and the note.
    await until('the job exit did not wake the director', () => said('JOB_SEEN'));
    const [notice] = finished();
    const payload = JSON.parse(userText(notice).split('\n').slice(1).join('\n'));
    assert.equal(payload.pid, pid); assert.equal(payload.note, 'fixture sleep'); assert.ok(payload.ran_seconds >= 0);
    assert.ok(notice.seq > said('STOP2').seq);
    assert.equal(questions().length, 1, 'neither NOTHING_LEFT, a dormant turn nor a tool-less turn is asked again');
    // Woken and tool-less: idle, and the normal review comes back.
    await until('the review did not come back', () => reviews().some(r => r.seq > said('JOB_SEEN').seq), 10000);
    assert.equal(questions().length, 1);
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of parked) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  }
});
