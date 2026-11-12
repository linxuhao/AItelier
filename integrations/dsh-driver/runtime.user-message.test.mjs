/**
 * The native driver tool, end to end: a real pinned DSH boots this driver, the
 * model calls `send_user_message`, and the tool must POST the durable
 * driver -> user action with the project supplied and nothing else invented.
 *
 * It is separate from runtime.smoke.test.mjs because that test asserts the
 * whole resident lifecycle; this one asserts one tool reaches one real door.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { measuredEntryForTest, withoutCallerMode } from './dsh-entry.mjs';

const PROVIDER = `
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-provider';
export const inject = ['llm'];
export function apply(ctx) {
  let calls = 0;
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 16000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      await fetch(process.env.AITELIER_URL + '/test/turn', { method: 'POST', body: JSON.stringify(options.messages) });
      const call = calls++;
      const block = call === 0
        ? { type: 'tool-call', id: 'fixture-user-message', name: 'send_user_message',
            arguments: process.env.AITELIER_FIXTURE_TOOL_ARGS || JSON.stringify({ request_key: 'smoke-user-message', subject: 'Smoke report', body: 'durable body', node_key: 'n' }) }
        : call === 1 ? { type: 'text', text: 'Reported to the user.' }
        : {type:'text',text:'No further work.'};
      yield { type: 'block-start', index: 0, blockType: block.type };
      if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text };
      else yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments };
      yield { type: 'block-end', index: 0, block };
      yield { type: 'usage', usage: { inputTokens: 10, outputTokens: 10 } };
      yield { type: 'finish', reason: { kind: block.type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
}
`;

test('native send_user_message tool calls the durable driver -> user action', { timeout: 45000 }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;                       // nothing pinned is installed: reported unmeasured
  const directory = await mkdtemp(join(homedir(), 'dsh-user-message-'));
  const here = dirname(fileURLToPath(import.meta.url));
  const sent = [], turns = [], activity = [];
  let output = '', proc;
  const message = { message_id: 'fixture-message-id', project_id: 'alpha',
    sender_driver_id: 'dsh-fixture', created_at: '2026-11-03T10:00:00+00:00' };
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [] } });
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture',
      event_seq: 1, nodes: [], project_note: { index: [] }, private_note: { index: [] },
      driver_skill: 'fixture driver skill', runtime_skill: 'Automatic notifications.' });
    if (req.url.endsWith('/activity')) { activity.push(body); return send({ ok: true }); }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url === '/api/state/commands/send_user_message') { sent.push(body); return send(message); }
    if (req.url === '/api/state/query/wait_for_state_change') return;   // park the subscription
    if (req.url === '/test/turn') { turns.push(body); return send({ ok: true }); }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const profile = join(directory, 'home/profiles/driver');
    await mkdir(profile, { recursive: true });
    const provider = join(directory, 'fixture-provider.mjs');
    await writeFile(provider, PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { insert: [{ id: 'fixture', name: provider }, { id: 'driver', name: join(here, 'runtime.mjs') }] },
    ]));
    const token = join(directory, 'token');
    await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutCallerMode(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'fixture-root',
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets') } });
    proc.stdout.on('data', chunk => output += chunk);
    proc.stderr.on('data', chunk => output += chunk);
    const deadline = Date.now() + 35000;
    while (Date.now() < deadline && proc.exitCode === null) {
      if (sent.length && turns.some(turn => turn.some(m => m.role === 'tool' && JSON.stringify(m).includes('fixture-message-id')))) break;
      if (activity.some(a => a.phase === 'failed')) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.equal(proc.exitCode, null, output);
    assert.equal(sent.length, 1, 'the native tool never called the action\n' + output + JSON.stringify({ sent, activity }));
    assert.deepEqual(sent[0], { request_key: 'smoke-user-message', subject: 'Smoke report',
      body: 'durable body', project_id: 'alpha', refs: { node_key: 'n' } });
    // The identity the action returned reached the model as the tool result.
    assert.ok(turns.some(turn => turn.some(m => m.role === 'tool' && JSON.stringify(m).includes('fixture-message-id'))),
      'the durable message identity did not reach the model\n' + JSON.stringify(turns.at(-1)).slice(0, 2000));
    assert.ok(!activity.some(a => a.phase === 'failed'), JSON.stringify(activity) + output);
  } finally {
    proc?.kill('SIGKILL');
    server.close();
    await rm(directory, { recursive: true, force: true });
  }
});

// The same real driver, with the model's first tool call set to `toolArgs`.
// It reports what reached the State command and what the model got back, so a
// case can assert on the request body or on the refusal the tool returned.
async function driveSendUserMessage(t, toolArgs) {
  const entry = await measuredEntryForTest(t);
  if (!entry) return null;                  // nothing pinned is installed: reported unmeasured
  const directory = await mkdtemp(join(homedir(), 'dsh-user-message-'));
  const here = dirname(fileURLToPath(import.meta.url));
  const sent = [], turns = [], activity = [];
  let output = '', proc;
  const message = { message_id: 'fixture-message-id', project_id: 'alpha',
    sender_driver_id: 'dsh-fixture', created_at: '2026-11-03T10:00:00+00:00' };
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    if (req.url === '/api/drivers/me') return send({ driver_id: 'dsh-fixture' });
    if (req.url === '/api/state/schema') return send({ operations: {} });
    if (req.url === '/api/state/query/list_director_messages') return send({ result: { items: [] } });
    if (req.url.endsWith('/context')) return send({ project_id: 'alpha', driver_id: 'dsh-fixture',
      event_seq: 1, nodes: [], project_note: { index: [] }, private_note: { index: [] },
      driver_skill: 'fixture driver skill', runtime_skill: 'Automatic notifications.' });
    if (req.url.endsWith('/activity')) { activity.push(body); return send({ ok: true }); }
    if (req.url.endsWith('/heartbeat')) return send({ ok: true });
    if (req.url === '/api/state/commands/send_user_message') { sent.push(body); return send(message); }
    if (req.url === '/api/state/query/wait_for_state_change') return;   // park the subscription
    if (req.url === '/test/turn') { turns.push(body); return send({ ok: true }); }
    res.statusCode = 404; send({ error: req.url });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const profile = join(directory, 'home/profiles/driver');
    await mkdir(profile, { recursive: true });
    const provider = join(directory, 'fixture-provider.mjs');
    await writeFile(provider, PROVIDER);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      { insert: [{ id: 'fixture', name: provider }, { id: 'driver', name: join(here, 'runtime.mjs') }] },
    ]));
    const token = join(directory, 'token');
    await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutCallerMode(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'dsh-fixture', AITELIER_DSH_PROJECT: 'alpha', AITELIER_DSH_SESSION: 'fixture-root',
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: base,
      AITELIER_FIXTURE_TOOL_ARGS: JSON.stringify(toolArgs),
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets') } });
    proc.stdout.on('data', chunk => output += chunk);
    proc.stderr.on('data', chunk => output += chunk);
    const deadline = Date.now() + 35000;
    while (Date.now() < deadline && proc.exitCode === null) {
      // Stop once the model has its tool result (a sent body, or a refusal that reached the turn).
      if (turns.some(turn => turn.some(m => m.role === 'tool'))) break;
      if (activity.some(a => a.phase === 'failed')) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    return { sent, turns, activity, output, exitCode: proc.exitCode };
  } finally {
    proc?.kill('SIGKILL');
    server.close();
    await rm(directory, { recursive: true, force: true });
  }
}

test('native send_user_message forwards tagged segments and the reply link to the State command', { timeout: 60000 }, async (t) => {
  const segments = [{ kind: 'info', text: 'restart is coordinated' }, { kind: 'question', text: 'may I restart now?' }];
  const result = await driveSendUserMessage(t, { request_key: 'segmented', subject: 'Restart', body: 'see below',
    node_key: 'n', segments, reply_to_message_id: 'user-msg-1' });
  if (!result) return;
  assert.equal(result.exitCode, null, result.output);
  assert.equal(result.sent.length, 1, 'the native tool never reached the State command\n' + result.output + JSON.stringify(result.activity));
  // Both fields reach the command body exactly as the driver wrote them; the
  // tool adds only the project and the node ref, and invents no authority.
  assert.deepEqual(result.sent[0], { request_key: 'segmented', subject: 'Restart', body: 'see below',
    project_id: 'alpha', refs: { node_key: 'n' }, segments, reply_to_message_id: 'user-msg-1' });
});

test('native send_user_message refuses a segment whose kind is not info or question, and sends nothing', { timeout: 60000 }, async (t) => {
  const result = await driveSendUserMessage(t, { request_key: 'bad-kind', subject: 'Restart', body: 'x',
    segments: [{ kind: 'demand', text: 'do it now' }] });
  if (!result) return;
  assert.equal(result.sent.length, 0, 'an unknown segment kind reached the State command: ' + JSON.stringify(result.sent));
  // The refusal is returned to the model as the tool's result, naming the block.
  assert.ok(result.turns.some(turn => turn.some(m => m.role === 'tool' && JSON.stringify(m).includes('invalid_segments'))),
    'the refusal did not reach the model\n' + JSON.stringify(result.turns.at(-1)).slice(0, 1500));
});

// The two length bounds are the server's (2000 characters per block, 8000 in
// total) and the tool checks them locally too, so a block that could never be
// stored is refused before the request instead of arriving as a failed one.
test('native send_user_message refuses a block longer than 2000 characters, locally, and sends nothing', { timeout: 60000 }, async (t) => {
  const result = await driveSendUserMessage(t, { request_key: 'too-long-block', subject: 'Restart', body: 'x',
    segments: [{ kind: 'info', text: 'y'.repeat(2000) }, { kind: 'question', text: 'z'.repeat(2001) }] });
  if (!result) return;
  assert.equal(result.sent.length, 0, 'an over-long block reached the State command: ' + JSON.stringify(result.sent).slice(0, 500));
  // The refusal is the tool's own result, and it names the block that is too long.
  assert.ok(result.turns.some(turn => turn.some(m => m.role === 'tool'
    && JSON.stringify(m).includes('invalid_segments') && JSON.stringify(m).includes('segments[1].text'))),
  'the length refusal did not reach the model\n' + JSON.stringify(result.turns.at(-1)).slice(0, 1500));
});

test('native send_user_message refuses blocks totalling more than 8000 characters, locally, and sends nothing', { timeout: 60000 }, async (t) => {
  // Five blocks of 1700 characters: every one is legal on its own, the total is not.
  const segments = Array.from({ length: 5 }, (_, index) => ({ kind: 'info', text: String(index).repeat(1700) }));
  const result = await driveSendUserMessage(t, { request_key: 'too-long-total', subject: 'Restart', body: 'x', segments });
  if (!result) return;
  assert.equal(result.sent.length, 0, 'an over-total message reached the State command: ' + JSON.stringify(result.sent).slice(0, 500));
  assert.ok(result.turns.some(turn => turn.some(m => m.role === 'tool'
    && JSON.stringify(m).includes('invalid_segments') && JSON.stringify(m).includes('8000'))),
  'the total-length refusal did not reach the model\n' + JSON.stringify(result.turns.at(-1)).slice(0, 1500));
});
