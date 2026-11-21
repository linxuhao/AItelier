/** Step 19 (design-steward 7.2), the wiring in a real pinned DSH: the steward's runtime
 *  declares `channels`, so dsh-director provides `ownerChannels` and a channel plugin that
 *  injects it loads. A fixture "slack" plugin then (1) relays an owner message - it reaches
 *  relay_owner_message with the channel's identity; (2) sends six digits from a stranger -
 *  bind_owner_channel answers 422 invalid_code and the stranger alone gets one line back;
 *  and the steward's send_user_message replying to the relayed message (3) goes out on slack,
 *  to that message, with exactly its text, and (4) is recorded with record_channel_delivery;
 *  and (5) the plugin's status reaches report_channel_status, once, with nothing logged.
 *  A director's runtime declares no channels: the same plugin never loads there. And (6) the
 *  owner's request for a new WeChat login QR (owner_channel_login_requested) is run by the
 *  runtime in code - the channel's login asked once, its answer completed - without a model
 *  turn. The State here is a fake HTTP server; nothing reaches a real chat service. */
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

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-channels-provider';
export const inject = ['llm'];
export function apply(ctx) {
  let replied = false;
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      await fetch(process.env.AITELIER_URL + '/test/request', { method: 'POST', body: JSON.stringify({ session: options.sessionId }) }).catch(() => {});
      const wake = options.tools?.length && options.sessionId === 'chan-root' && !replied && text.includes('AItelier runtime event');
      // In auto mode the steward answers in plain words: the runtime's auto-reply sends them.
      // In ask mode it asks its owner a question with ask_owner.
      const block = wake && process.env.CHANNELS_TEST_MODE === 'auto' ? (replied = true, { type: 'text', text: 'AUTO_REPLY_TEXT all good.' })
        : wake && process.env.CHANNELS_TEST_MODE === 'ask' ? (replied = true, { type: 'tool-call', id: 'ask-1', name: 'ask_owner',
          arguments: JSON.stringify({ question_id: 'q-deploy', question: 'QUESTION_TEXT may I deploy?\n同意 proposal p-1', context: 'CONTEXT_TEXT the release is ready.' }) })
        : wake ? (replied = true, { type: 'tool-call', id: 'reply-1', name: 'send_user_message', arguments: JSON.stringify({ request_key: 'r-1',
          subject: 'Re: hello', body: 'CHANNEL_REPLY_TEXT the build passed.', reply_to_message_id: 'm-in-1' }) })
        : { type: 'text', text: 'Done.' };
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

// A channel plugin of the shape the real ones have: it injects ownerChannels, registers
// 'slack' and hands two messages in. What it is asked to send goes to the test server.
const CHANNEL = String.raw`
export const name = 'fixture-channel';
export const inject = ['ownerChannels'];
const post = (path, body) => fetch(process.env.AITELIER_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
export function apply(ctx) {
  const off = ctx.ownerChannels.register({ name: 'slack', status: () => ({ connected: true, detail: 'FIXTURE_STATUS_DETAIL' }),
    async send(text, options) { await post('/test/channel-send', { text, options }); return { ref: 'slack-out-ts' }; } });
  ctx.on('dispose', off);
  // A WeChat channel with a login, as dsh-channel-wechat registers it (the QR mode only).
  if (process.env.CHANNELS_TEST_MODE === 'qr') {
    ctx.on('dispose', ctx.ownerChannels.register({ name: 'wechat', status: () => ({ connected: false, detail: 'no QR (boot)' }),
      async send() { return {}; },
      async login() { await post('/test/channel-login', {}); return { requested: true, qr: 'https://login.weixin.qq.com/l/SECRET_QR' }; } }));
  }
  void post('/test/channel-loaded', {});
  void (async () => {
    await post('/test/inbound', await ctx.ownerChannels.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: 'hello from slack' }));
    await post('/test/inbound', await ctx.ownerChannels.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'ts-2', text: '123456' }));
  })();
}
`;

async function run(t, { steward, mode = 'tool' }) {
  const entry = await measuredEntryForTest(t);
  if (!entry) return null;
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-channels-'));
  const project = steward ? 'aitelier-home' : 'alpha';
  const seen = { commands: [], queries: [], sends: [], inbound: [], loaded: 0, requests: 0, logins: 0, rootRequests: 0 };
  let output = '', proc, woke = false;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks).toString();
    const body = raw ? JSON.parse(raw) : {};
    const send = (value, status = 200) => { res.statusCode = status; res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
    const url = req.url;
    if (url === '/test/channel-send') { seen.sends.push(body); return send({}); }
    if (url === '/test/inbound') { seen.inbound.push(body); return send({}); }
    if (url === '/test/channel-loaded') { seen.loaded++; return send({}); }
    if (url === '/test/request') { seen.requests++; if (body.session === 'chan-root') seen.rootRequests++; return send({}); }
    if (url === '/test/channel-login') { seen.logins++; return send({}); }
    if (url === '/api/drivers/me') return send({ driver_id: 'drv-chan' });
    if (url === '/api/state/schema') return send({ operations: {} });
    if (url.endsWith('/context')) return send({ project_id: project, driver_id: 'drv-chan', event_seq: 1, nodes: [],
      project_note: { index: [] }, private_note: { index: [] }, driver_skill: 'fixture', runtime_skill: 'fixture' });
    if (url.endsWith('/activity') || url.endsWith('/heartbeat')) return send({ ok: true });
    if (url === '/api/state/query/wait_for_state_change') {
      // One owner-message event wakes the root once; after that the subscription parks.
      if (woke) return;
      woke = true;
      // The QR mode: the owner pressed "新二维码" - nothing for the model.
      if (mode === 'qr') return send({ events: [{ seq: 2, event_type: 'owner_channel_login_requested',
        payload: { request_id: 'qrreq-1', channel: 'wechat' } }], next_after: 2 });
      return send({ events: [{ seq: 2, event_type: 'user_message_created', payload: { message_id: 'm-in-1' } }], next_after: 2 });
    }
    if (url.startsWith('/api/state/commands/')) {
      const name = url.slice('/api/state/commands/'.length);
      seen.commands.push([name, body]);
      // The plugin's store-text question rides the same command in its precheck mode.
      if (name === 'relay_owner_message') return send(body.precheck ? { store_text: true }
        : { message_id: 'm-in-1', duplicate: false, approval: null });
      if (name === 'bind_owner_channel') return send({ detail: 'invalid_code: no such code for slack' }, 422);
      if (name === 'send_user_message') return send({ message_id: 'm-out-1', project_id: project });
      if (name === 'complete_steward_memory_request') return send({});
      if (name === 'record_channel_delivery') return send({ message_id: body.message_id, recorded: true });
      if (name === 'report_channel_status') return send({ recorded: true, channel: body.channel, connected: body.connected, at: 't' });
      if (name === 'complete_owner_channel_login_request') return send({ request_id: body.request_id, status: 'done' });
      // Step 23: the steward's host records each turn's audience before its first step.
      if (name === 'set_steward_turn_audience') return send({ recorded: true, turn: body.turn, audience: body.audience });
      return send({ detail: 'unexpected ' + name }, 422);
    }
    if (url.startsWith('/api/state/query/')) {
      const name = url.slice('/api/state/query/'.length);
      seen.queries.push([name, body]);
      if (name === 'list_owner_channels') return send({ default_channel: mode === 'ask' ? 'slack' : null, channels: [
        { channel: 'slack', bound: true, sender_ref: 'U-OWNER', bound_at: 't', revoked_at: null, is_default: false },
        { channel: 'wechat', bound: false, sender_ref: null, bound_at: null, revoked_at: null, is_default: false }] });
      // The owner's relayed message, answered once the steward sent anything.
      if (name === 'list_user_conversation') return send({ messages: [
        ...(seen.commands.some(([n]) => n === 'send_user_message') ? [{ message_id: 'm-out-1', sender_kind: 'driver', sender_driver_id: 'drv-chan',
          created_at: '2026-11-18T10:01:00Z', body: 'reply', segments: [], reply_to_message_id: 'm-in-1' }] : []),
        { message_id: 'm-in-1', sender_kind: 'user', sender_driver_id: null, created_at: '2026-11-18T10:00:00Z', subject: 'hello',
          body: 'hello from slack', segments: [], reply_to_message_id: null }], next_cursor: null });
      // One pending request, listed until it is completed (the start's sweep and the event both read it).
      if (name === 'list_owner_channel_login_requests') {
        const done = seen.commands.some(([n]) => n === 'complete_owner_channel_login_request');
        return send({ result: { requests: mode === 'qr' && !done ? [{ request_id: 'qrreq-1', channel: 'wechat', requested_at: 't' }] : [] } });
      }
      if (name === 'message_sources') return send({ sources: Object.fromEntries((body.message_ids ?? []).filter(id => id === 'm-in-1')
        .map(id => [id, { direction: 'in', channel: 'slack', sender_ref: 'U-OWNER', channel_message_ref: 'ts-1', at: 't', error: null }])) });
      return send({ result: {} });
    }
    send({ detail: url }, 404);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const profile = join(directory, 'home/profiles/driver');
    await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-provider.mjs'), PROVIDER);
    await writeFile(join(directory, 'fixture-channel.mjs'), CHANNEL);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-provider.mjs') },
        { id: 'dsh-director', name: join(here, '../dsh-director/index.mjs') },
        { id: 'aitelier-driver', name: join(here, 'runtime.mjs') },
        { id: 'dsh-channel-slack', name: join(directory, 'fixture-channel.mjs') }] },
    ]));
    const token = join(directory, 'token');
    await writeFile(token, 'fixture-only-driver-token', { mode: 0o600 });
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutCallerMode(process.env),
      DSH_HOME: join(directory, 'home'), AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_TOKEN_FILE: token,
      AITELIER_DRIVER_ID: 'drv-chan', AITELIER_DSH_PROJECT: project, AITELIER_DSH_SESSION: 'chan-root',
      AITELIER_DSH_PROVIDER: 'fixture', AITELIER_DSH_MODEL: 'fixture-model', AITELIER_URL: `http://127.0.0.1:${server.address().port}`,
      AITELIER_DSH_STATE_DIR: directory, AITELIER_SECRETS_DIR: join(directory, 'no-secrets'), CHANNELS_TEST_MODE: mode,
      ...(steward ? { AITELIER_DSH_ROLE: 'steward', AITELIER_DSH_MEMORY_USER: 'owner', AITELIER_DSH_MEMORY_ROOT: join(directory, 'memory') } : {}) } });
    proc.stdout.on('data', chunk => output += chunk);
    proc.stderr.on('data', chunk => output += chunk);
    return { seen, output: () => output, proc, until: async (what, met, ms = 60000) => {
      const deadline = Date.now() + ms;
      while (!met() && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
      assert.ok(met(), what + '\n' + JSON.stringify(seen).slice(0, 3000) + '\n' + output.slice(-3000));
    }, async close() {
      if (proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
      server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
      await rm(directory, { recursive: true, force: true });
    } };
  } catch (error) {
    proc?.kill('SIGKILL'); server.close(); await rm(directory, { recursive: true, force: true });
    throw error;
  }
}

test('real pinned DSH, the steward: a channel plugin loads, its messages reach relay / bind, and the steward\'s reply goes back on the channel once', { timeout: 120000 }, async (t) => {
  const r = await run(t, { steward: true });
  if (!r) return;
  const { seen } = r;
  try {
    await r.until('the channel plugin never loaded', () => seen.loaded === 1);
    await r.until('both inbound messages were not answered', () => seen.inbound.length === 2);
    assert.deepEqual(seen.inbound, [{ accepted: true }, { accepted: false, reason: 'invalid_code' }]);
    const command = name => seen.commands.filter(([n]) => n === name).map(([, body]) => body);
    // Two requests, in this order: the store-text question the plugin asks before it writes
    // the owner's words down, then the relay of the message itself.
    const [check, relay] = command('relay_owner_message');
    assert.deepEqual({ ...check, request_key: undefined }, { request_key: undefined, precheck: true, channel: 'slack',
      sender_ref: 'U-OWNER', channel_message_ref: 'ts-1', body: 'hello from slack' });
    assert.match(check.request_key, /^relay-check:slack:[0-9a-f]{16}$/);
    assert.deepEqual({ ...relay, request_key: undefined }, { request_key: undefined, channel: 'slack', sender_ref: 'U-OWNER',
      channel_message_ref: 'ts-1', body: 'hello from slack' });
    assert.match(relay.request_key, /^relay:slack:[0-9a-f]{16}$/);
    assert.deepEqual(command('bind_owner_channel').map(({ request_key, ...rest }) => rest),
      [{ channel: 'slack', sender_ref: 'U-STRANGER', code: '123456', channel_message_ref: 'ts-2' }]);
    // The stranger gets one line about its own binding attempt, addressed to it alone.
    const to = account => seen.sends.filter(s => s.options.to === account);
    await r.until('the binding answer was not sent', () => to('U-STRANGER').length >= 1);
    assert.deepEqual(to('U-STRANGER').map(s => s.options), [{ replyToRef: 'ts-2', to: 'U-STRANGER' }]);
    assert.match(to('U-STRANGER')[0].text, /^Not bound/);
    // The owner's relayed message gets the code's receipt at once, then the steward's answer:
    // on slack, to that message, verbatim. The receipt is no steward message (no
    // send_user_message, no delivery row of its own).
    await r.until('the steward\'s reply was never recorded as delivered', () => command('record_channel_delivery').length === 1);
    assert.deepEqual(to('U-OWNER'), [{ text: '已收到，正在处理…', options: { replyToRef: 'ts-1', to: 'U-OWNER' } },
      { text: 'CHANNEL_REPLY_TEXT the build passed.', options: { replyToRef: 'ts-1', to: 'U-OWNER' } }]);
    assert.deepEqual(command('record_channel_delivery').map(({ request_key, ...rest }) => [request_key, rest]),
      [['delivery:m-out-1:slack', { message_id: 'm-out-1', channel: 'slack', channel_message_ref: 'slack-out-ts' }]]);
    assert.deepEqual(command('send_user_message').map(b => b.reply_to_message_id), ['m-in-1']);
    // Nothing else the steward did reached the channel.
    await new Promise(resolve => setTimeout(resolve, 1000));
    assert.equal(seen.sends.length, 3);
    assert.equal(command('record_channel_delivery').length, 1);
    assert.ok(!r.output().includes('hello from slack'), 'a message body reached the log');
    // The channel's status reached the host once (it did not change), and its detail no log.
    assert.deepEqual(command('report_channel_status').map(({ request_key, ...rest }) => rest),
      [{ channel: 'slack', connected: true, detail: 'FIXTURE_STATUS_DETAIL' }]);
    assert.match(command('report_channel_status')[0].request_key, /^channel-status:slack:\d+:[0-9a-f]{16}$/);
    assert.ok(!r.output().includes('FIXTURE_STATUS_DETAIL'), 'a status detail reached the log');
  } finally { await r.close(); }
});

test('real pinned DSH, the steward: an automatic reply (its last words in a turn) also goes back on the channel it answers', { timeout: 120000 }, async (t) => {
  const r = await run(t, { steward: true, mode: 'auto' });
  if (!r) return;
  const { seen } = r;
  try {
    const command = name => seen.commands.filter(([n]) => n === name).map(([, body]) => body);
    await r.until('the automatic reply was never recorded as delivered', () => command('record_channel_delivery').length === 1);
    assert.deepEqual(command('send_user_message').map(b => [b.request_key, b.reply_to_message_id, b.body]),
      [['auto-reply:m-in-1', 'm-in-1', 'AUTO_REPLY_TEXT all good.']]);
    // The receipt went out first and did not count as the steward's answer: the message was
    // still unanswered, so the steward's own last words were still sent as its reply.
    assert.deepEqual(seen.sends.filter(s => s.options.to === 'U-OWNER'),
      [{ text: '已收到，正在处理…', options: { replyToRef: 'ts-1', to: 'U-OWNER' } },
        { text: 'AUTO_REPLY_TEXT all good.', options: { replyToRef: 'ts-1', to: 'U-OWNER' } }]);
  } finally { await r.close(); }
});

test('real pinned DSH, the steward: a question asked with ask_owner is an unprompted message, so it also goes to the default channel, as the owner reads it', { timeout: 120000 }, async (t) => {
  const r = await run(t, { steward: true, mode: 'ask' });
  if (!r) return;
  const { seen } = r;
  try {
    const command = name => seen.commands.filter(([n]) => n === name).map(([, body]) => body);
    await r.until('the question was never recorded as delivered', () => command('record_channel_delivery').some(b => b.message_id === 'm-out-1'));
    assert.deepEqual(command('send_user_message').map(b => [b.request_key, b.reply_to_message_id, b.segments]),
      [['ask-owner:aitelier-home:q-deploy', undefined, [{ kind: 'question', id: 'q-deploy', text: 'QUESTION_TEXT may I deploy?\n同意 proposal p-1' }]]]);
    // Body, then the question block: the text the dashboard shows, the approval phrase on its own line.
    assert.deepEqual(seen.sends.filter(s => s.options.to === 'U-OWNER'),
      [{ text: '已收到，正在处理…', options: { replyToRef: 'ts-1', to: 'U-OWNER' } },
        { text: 'CONTEXT_TEXT the release is ready.\n\nQUESTION_TEXT may I deploy?\n同意 proposal p-1', options: { to: 'U-OWNER' } }]);
    assert.deepEqual(command('record_channel_delivery').map(({ request_key, ...rest }) => [request_key, rest]),
      [['delivery:m-out-1:slack', { message_id: 'm-out-1', channel: 'slack', channel_message_ref: 'slack-out-ts' }]]);
  } finally { await r.close(); }
});

test('real pinned DSH, a director: no channels capability, so ownerChannels is not provided and the channel plugin never loads', { timeout: 120000 }, async (t) => {
  const r = await run(t, { steward: false });
  if (!r) return;
  const { seen } = r;
  try {
    // The director boots and takes its turn; give the plugin loader time to have applied it.
    await r.until('the director never asked its model', () => seen.requests > 0);
    await new Promise(resolve => setTimeout(resolve, 2000));
    assert.equal(r.proc.exitCode, null, r.output());
    assert.equal(seen.loaded, 0);
    assert.deepEqual(seen.inbound, []);
    assert.deepEqual(seen.queries.filter(([name]) => ['list_owner_channels', 'message_sources'].includes(name)), []);
    assert.deepEqual(seen.commands.filter(([name]) => name === 'report_channel_status'), []);
  } finally { await r.close(); }
});

test('real pinned DSH, the steward: the owner\'s request for a new WeChat QR is run in code - the channel\'s login once, the answer completed, no model turn', { timeout: 120000 }, async (t) => {
  const r = await run(t, { steward: true, mode: 'qr' });
  if (!r) return;
  const { seen } = r;
  try {
    await r.until('the channel plugin never loaded', () => seen.loaded === 1);
    await r.until('the login request was never completed',
      () => seen.commands.some(([n]) => n === 'complete_owner_channel_login_request'));
    // The event itself arrives too; the request is already done, so nothing runs twice.
    await r.until('the subscription never delivered the event', () => seen.queries.filter(([n]) => n === 'list_owner_channel_login_requests').length >= 2);
    await new Promise(resolve => setTimeout(resolve, 1500));
    assert.equal(seen.logins, 1, 'the channel was asked for a login once');
    const completed = seen.commands.filter(([n]) => n === 'complete_owner_channel_login_request').map(([, body]) => body);
    assert.deepEqual(completed, [{ request_key: 'channel-login:qrreq-1', request_id: 'qrreq-1', result: { requested: true } }],
      'only {requested}: never the QR the channel answered with');
    assert.equal(seen.rootRequests, 0, 'the request woke no model turn');
    assert.ok(!r.output().includes('SECRET_QR') && !JSON.stringify(seen).includes('SECRET_QR'), 'the QR went somewhere');
  } finally { await r.close(); }
});
