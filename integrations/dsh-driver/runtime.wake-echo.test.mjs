import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, rm, realpath } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

// Root script: start a detached job and watch it, stop -> asked once (a fresh wait)
// -> NOTHING_LEFT. The test then releases a State tick of only the director's own
// writes (must not wake it), a tick carrying an authorless lease_expired that names
// this director in driver_id (must wake it: it is a system event, not an own write)
// and a tick by another driver (must): those turns check and stop - same wait,
// already answered, so not asked. The test ends the job: its notice wakes the
// director, which collects it, starts and watches the next job and stops - a
// different wait, so asked again.
const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-wake-echo-provider';
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
      else block = once('job', () => bash('root-job', 'setsid nohup sleep 120 >/dev/null 2>&1 < /dev/null & echo JOB_PID=$!'))
        ?? (pid ? once('watch', () => tool('root-watch', 'watch_process', { pid: Number(pid), note: 'fixture job' })) : undefined)
        ?? once('stop1', () => ({ type: 'text', text: 'STOP1 waiting on the job' }))
        ?? (questions >= 1 ? once('nothing1', () => ({ type: 'text', text: 'NOTHING_LEFT: the watched job owns the next step' })) : undefined)
        ?? (text.includes('LEASE_NODE') ? once('lease-check', () => bash('root-lease', 'echo SAW_LEASE')) : undefined)
        ?? (text.includes('LEASE_NODE') ? once('stop1b', () => ({ type: 'text', text: 'STOP1b the lease expiry reached me' })) : undefined)
        ?? (text.includes('FOREIGN_NODE') ? once('foreign-check', () => bash('root-foreign', 'echo SAW_FOREIGN')) : undefined)
        ?? (text.includes('FOREIGN_NODE') ? once('stop2', () => ({ type: 'text', text: 'STOP2 still waiting on the job' })) : undefined)
        ?? (text.includes('AItelier background job finished') ? once('collect', () => bash('root-collect', 'setsid nohup sleep 120 >/dev/null 2>&1 < /dev/null & echo JOB2_PID=$!')) : undefined)
        ?? (text.match(/JOB2_PID=(\d+)/) ? once('watch2', () => tool('root-watch2', 'watch_process', { pid: Number(text.match(/JOB2_PID=(\d+)/)[1]), note: 'fixture job 2' })) : undefined)
        ?? (text.includes('AItelier background job finished') ? once('stop3', () => ({ type: 'text', text: 'STOP3 collected, waiting on the next job' })) : undefined)
        ?? (questions >= 2 ? once('nothing2', () => ({ type: 'text', text: 'NOTHING_LEFT: all collected' })) : undefined)
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

test('real pinned DSH: own State writes do not wake the director; one NOTHING_LEFT holds while it waits; a changed wait is asked again', { timeout: 60000 }, async () => {
  const directory = await mkdtemp(join(homedir(), 'dsh-wake-echo-'));
  const entry = await realpath(process.env.AITELIER_DSH_TEST_ENTRY ?? new URL('./node_modules/.bin/dsh', import.meta.url));
  const here = dirname(fileURLToPath(import.meta.url));
  const session = 'wake-echo-root-' + process.pid + '-' + Math.random().toString(36).slice(2, 8);
  const events = [], requests = [], waits = [], parked = [];
  let output = '', proc, pid, pid2;
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
    if (req.url === '/api/state/query/wait_for_state_change') {
      waits.push(body.after);
      parked.push(res); res.on('close', () => { const i = parked.indexOf(res); if (i >= 0) parked.splice(i, 1); }); return;
    }
    if (req.url === '/test/request') { requests.push(body); return send({ ok: true }); }
    if (req.url === '/test/event') { events.push(body.event); return send({ ok: true }); }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const userText = e => JSON.stringify(e.data.content ?? []);
  const questions = () => events.filter(e => e.type === 'user/message' && e.data.source?.kind === 'aitelier-continue');
  const said = text => events.find(e => e.type === 'assistant/message' && JSON.stringify(e.data).includes(text));
  const result = id => events.find(e => e.type === 'tool/result' && e.data.message.toolCallId === id);
  const delivered = marker => events.find(e => e.type === 'user/message' && userText(e).includes(marker));
  const until = async (what, predicate, ms = 30000) => {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline && proc.exitCode === null && !predicate()) await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(proc.exitCode, null, output);
    assert.ok(predicate(), `${what}\nREQ ${JSON.stringify(requests.map(r => r.block))}\nWAITS ${JSON.stringify(waits)}\n${output.slice(-3000)}\n${JSON.stringify(events.map(e => [e.seq, e.type, e.data?.message?.toolCallId ?? e.data?.source?.kind ?? '']))}`);
  };
  // Answer the subscription's current long poll with one tick, then wait for it to poll again.
  const release = async (tick) => {
    await until('the subscription is not polling', () => parked.length > 0);
    const polls = waits.length;
    const res = parked.shift();
    res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(tick));
    await until('the subscription did not poll again after a tick', () => waits.length > polls);
  };
  try {
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-wake-echo-provider.mjs'), PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-wake-echo-provider.mjs') }, { id: 'driver', name: join(here, 'runtime.mjs') }] },
    ]));
    const token = join(directory, 'token'); await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...process.env,
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: session,
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'),
      AITELIER_DSH_EXECUTION: 'host', DSH_PERMISSION_MODE: 'danger-full-access',
      AITELIER_DSH_IDLE_REVIEW_MS: '600000', AITELIER_DSH_WATCH_INTERVAL_MS: '200' } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);

    // A fresh wait is asked once; NOTHING_LEFT answers it.
    await until('the first NOTHING_LEFT never came', () => said('NOTHING_LEFT: the watched job'));
    pid = Number(JSON.stringify(result('root-job').data).match(/JOB_PID=(\d+)/)[1]);
    assert.equal(JSON.parse(result('root-watch').data.message.content[0].text).watching, true);
    assert.equal(questions().length, 1, 'the fresh wait is asked exactly once');
    assert.ok(questions()[0].seq > said('STOP1').seq);

    // The director's own writes come back: the cursor moves, no turn.
    await release({ timed_out: false, next_after: 11, next_inbox_after: 0, driver_inbox: [], events: [
      { seq: 11, project_id: 'alpha', node_key: 'OWN_ECHO_NODE', event_type: 'claim_acquired', payload: { actor: 'driver:dsh-fixture', driver_id: 'dsh-fixture' } }] });
    assert.equal(waits.at(-1), 11, 'the own tick still advances the cursor');
    // An authorless system event that names this driver: a lease expiry is emitted
    // with the claim holder's driver_id and no actor. Everyone else is woken by it,
    // so the holder must be too - it is not this driver's own write.
    await release({ timed_out: false, next_after: 12, next_inbox_after: 0, driver_inbox: [], events: [
      { seq: 12, project_id: 'alpha', node_key: 'LEASE_NODE', event_type: 'lease_expired',
        payload: { subject: 'node:alpha/LEASE_NODE', lease_id: 'lease-1', driver_id: 'dsh-fixture', phase: 'expired', fence: 4 } }] });
    await until('the authorless lease_expired did not wake the director', () => said('STOP1b'));
    assert.equal(waits.at(-1), 12, 'the authorless tick advances the cursor too');
    assert.ok(delivered('LEASE_NODE'), 'an authorless event naming me reaches the session');
    assert.ok(delivered('lease-1') && delivered('expired'), 'it reaches the session whole, payload included');
    assert.ok(result('root-lease'), 'the woken turn called a tool');
    assert.equal(questions().length, 1, 'still the same, already answered wait: not asked again');
    // The director's own acknowledge of a message: the emitter stamps its actor, so
    // it is an echo like any other own write and must not wake the director. The
    // ack tick is released before the foreign one, so a woken turn would show first.
    const turns = events.filter(e => e.type === 'user/message').length;
    await release({ timed_out: false, next_after: 13, next_inbox_after: 0, driver_inbox: [], events: [
      { seq: 13, project_id: 'alpha', node_key: null, event_type: 'director_message_acked',
        payload: { delivery_id: 'delivery-1', driver_id: 'dsh-fixture', actor: 'driver:dsh-fixture', acks: [] } }] });
    assert.equal(waits.at(-1), 13, 'the own ack tick advances the cursor');
    await new Promise(resolve => setTimeout(resolve, 500));
    assert.equal(events.filter(e => e.type === 'user/message').length, turns, 'an own ack tick must not start a turn');
    // Another driver's write: input.
    await release({ timed_out: false, next_after: 14, next_inbox_after: 0, driver_inbox: [], events: [
      { seq: 14, project_id: 'alpha', node_key: 'FOREIGN_NODE', event_type: 'driver_note_updated', payload: { actor: 'driver:grok' } }] });
    await until('the foreign tick did not wake the director', () => said('STOP2'));
    assert.equal(delivered('OWN_ECHO_NODE'), undefined, 'an own-writes tick must never reach the session');
    assert.equal(delivered('delivery-1'), undefined, 'an own ack tick must never reach the session');
    assert.ok(delivered('FOREIGN_NODE'), 'a foreign tick is delivered');
    assert.ok(result('root-foreign'), 'the woken turn called a tool');
    // Same wait, already answered: the tool turn is not asked again.
    await new Promise(resolve => setTimeout(resolve, 1500));
    assert.equal(questions().length, 1, 'a wait already answered with NOTHING_LEFT is not asked again');

    // The job ends: its notice wakes; the turn collects it and waits on a new job - a
    // different wait than the one answered, so asked again.
    process.kill(pid, 'SIGTERM');
    await until('the director was not asked when the wait changed', () => said('NOTHING_LEFT: all collected'));
    pid2 = Number(JSON.stringify(result('root-collect').data).match(/JOB2_PID=(\d+)/)[1]);
    assert.equal(JSON.parse(result('root-watch2').data.message.content[0].text).watching, true);
    assert.equal(questions().length, 2);
    assert.ok(questions()[1].seq > said('STOP3').seq, 'the second question follows the turn that changed the wait');
  } finally {
    for (const job of [pid, pid2]) if (job) try { process.kill(job, 'SIGKILL'); } catch {}
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    for (const response of parked) response.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    if (!process.env.DSH_TEST_KEEP) await rm(directory, { recursive: true, force: true });
  }
});
