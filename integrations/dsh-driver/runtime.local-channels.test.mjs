/** The owner's chat channels on the built-in local host, in a real pinned DSH: dsh-director with
 *  its local host only (no adapter, no backend), the root a steward with `channels: true`, and a
 *  fake channel plugin of the real ones' shape (it injects ownerChannels and registers 'slack').
 *  The owner runs /bind slack through DSH's command registry (a probe plugin, as in
 *  runtime.standalone-steward.test.mjs); the channel plugin hands in a stranger's guess (refused,
 *  one line to the stranger), the code from the owner's account (bound) and two owner messages.
 *  The steward answers the first with message_owner and the second in plain words (the turn's
 *  last words): each reply goes back to slack exactly once, to the message it answers, and is
 *  recorded in channels/messages.jsonl. Nothing reaches a real chat service. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm, rename } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { measuredEntryForTest, withoutHostWiring } from './dsh-entry.mjs';

const PROVIDER = String.raw`
import { createRequire } from 'node:module';
const require = createRequire(process.env.LC_TEST_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'fixture-lc-provider';
export const inject = ['llm'];
const post = (path, body) => fetch(process.env.LC_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
export function apply(ctx) {
  const handled = [];
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const text = JSON.stringify(options.messages);
      await post('/test/request', { session: options.sessionId, purpose: options.purpose ?? null, tools: (options.tools ?? []).map(t => t.name) });
      // The owner messages this conversation was woken with, in order; the first not answered yet.
      const ids = [...new Set([...text.matchAll(/owner-message-(m-[0-9a-f]{20})\.json/g)].map(m => m[1]))];
      const next = ids.find(id => !handled.includes(id));
      let block;
      if (options.purpose === 'compaction') block = { type: 'text', text: 'Checkpoint: nothing in flight.' };
      else if (!options.tools?.length || options.sessionId !== 'lc-root' || !next) block = { type: 'text', text: 'Steward idle.' };
      else if (handled.push(next) === 1) block = { type: 'tool-call', id: 'reply-1', name: 'message_owner',
        arguments: JSON.stringify({ request_key: 'r-' + next, body: 'TOOL_REPLY the build passed.', reply_to_message_id: next }) };
      else block = { type: 'text', text: 'AUTO_REPLY all good.' };
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
    if (session.id === 'lc-root' && ['tool/result', 'user/message', 'assistant/message'].includes(event.type)) void post('/test/event', event);
  });
}
`;

// A channel plugin of the real ones' shape: what it sends goes to the test server; each
// <dir>/<n>.in (one inbound message as JSON) is handed in, its answer posted back.
const CHANNEL = String.raw`
import { readdir, readFile, rename } from 'node:fs/promises';
import { join } from 'node:path';
export const name = 'fixture-lc-channel';
export const inject = ['ownerChannels'];
const post = (path, body) => fetch(process.env.LC_TEST_URL + path, { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
export function apply(ctx, { dir }) {
  let n = 0;
  ctx.on('dispose', ctx.ownerChannels.register({ name: 'slack', status: () => ({ connected: true, detail: 'FIXTURE_STATUS' }),
    async send(text, options) { await post('/test/channel-send', { text, options }); return { ref: 'out-' + (++n) }; } }));
  let busy = false;
  const timer = setInterval(async () => {
    if (busy) return;
    busy = true;
    try {
      for (const file of (await readdir(dir)).filter(name => name.endsWith('.in')).sort()) {
        const message = JSON.parse(await readFile(join(dir, file), 'utf8'));
        await rename(join(dir, file), join(dir, file + '.done'));
        await post('/test/inbound', { file, answer: await ctx.ownerChannels.inbound(message) });
      }
    } catch {} finally { busy = false; }
  }, 50);
  ctx.on('dispose', () => clearInterval(timer));
  void post('/test/channel-loaded', {});
}
`;

// What DSH's client does with a slash line (runtime.standalone-steward.test.mjs's probe).
const PROBE = String.raw`
import { readdir, readFile, writeFile, rename } from 'node:fs/promises';
import { join } from 'node:path';
export const name = 'lc-command-probe';
export const inject = ['commands', 'agents'];
export function apply(ctx, { dir, session }) {
  let busy = false;
  const timer = setInterval(async () => {
    if (busy) return;
    busy = true;
    try {
      for (const file of (await readdir(dir)).filter(n => n.endsWith('.cmd')).sort()) {
        const agent = ctx.agents.get(session);
        if (!agent) break;
        const line = await readFile(join(dir, file), 'utf8');
        let out;
        try { out = (await ctx.commands.execute(agent, line, [], AbortSignal.timeout(60000)))?.result ?? { unresolved: true }; }
        catch (error) { out = { thrown: String(error?.message ?? error) }; }
        await writeFile(join(dir, file.replace(/\.cmd$/, '.out.tmp')), JSON.stringify(out));
        await rename(join(dir, file.replace(/\.cmd$/, '.out.tmp')), join(dir, file.replace(/\.cmd$/, '.out')));
        await rename(join(dir, file), join(dir, file + '.done'));
      }
    } catch {} finally { busy = false; }
  }, 50);
  ctx.on('dispose', () => clearInterval(timer));
}
`;

test('real pinned DSH: a standalone steward on the local host with channels - bound with /bind, an owner message relayed, each reply mirrored back once', { timeout: 240000 }, async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const here = dirname(fileURLToPath(import.meta.url));
  const directory = await mkdtemp(join(homedir(), 'dsh-lc-'));
  const root = join(directory, 'steward'), cmdDir = join(directory, 'cmds'), inDir = join(directory, 'in');
  const events = [], requests = [], sends = [], inbound = [];
  let output = '', proc, n = 0, loaded = 0;
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    if (req.url === '/test/event') events.push(body);
    else if (req.url === '/test/request') requests.push(body);
    else if (req.url === '/test/channel-send') sends.push(body);
    else if (req.url === '/test/inbound') inbound.push(body);
    else if (req.url === '/test/channel-loaded') loaded++;
    res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const until = async (what, met, ms = 60000) => {
    const deadline = Date.now() + ms;
    while (!(await met()) && Date.now() < deadline && proc.exitCode === null) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(await met(), what + '\n' + JSON.stringify({ sends, inbound }) + '\n' + output.slice(-3000));
  };
  const command = async line => {
    const deadline = Date.now() + 60000;
    for (;;) {
      const name = String(++n).padStart(4, '0');
      await writeFile(join(cmdDir, name + '.tmp'), line);
      await rename(join(cmdDir, name + '.tmp'), join(cmdDir, name + '.cmd'));
      let out;
      await until(`no answer to ${line}`, async () => (out = await readFile(join(cmdDir, name + '.out'), 'utf8').catch(() => undefined)) !== undefined);
      const value = JSON.parse(out);
      if (!value.unresolved || Date.now() > deadline) return value;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
  };
  let m = 0;
  /** One message the channel hands in; resolves with the channel layer's answer. */
  const handIn = async message => {
    const file = String(++m).padStart(4, '0') + '.in';
    await writeFile(join(inDir, file + '.tmp'), JSON.stringify(message));
    await rename(join(inDir, file + '.tmp'), join(inDir, file));
    await until(`no answer to inbound ${file}`, () => inbound.some(i => i.file === file));
    return inbound.find(i => i.file === file).answer;
  };
  const replies = text => sends.filter(s => s.text.includes(text));
  try {
    await mkdir(join(root, 'inbox'), { recursive: true });
    await mkdir(cmdDir, { recursive: true });
    await mkdir(inDir, { recursive: true });
    const profile = join(directory, 'home/profiles/driver'); await mkdir(profile, { recursive: true });
    await writeFile(join(directory, 'fixture-lc-provider.mjs'), PROVIDER);
    await writeFile(join(directory, 'fixture-lc-channel.mjs'), CHANNEL);
    await writeFile(join(directory, 'lc-command-probe.mjs'), PROBE);
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'hmr', disabled: true },
      ...['tool-subagent-fork', 'tool-workflow', 'plan-mode', 'tool-todo'].map(id => ({ id, disabled: true })),
      { insert: [{ id: 'fixture', name: join(directory, 'fixture-lc-provider.mjs') },
        { id: 'director', name: join(here, '../dsh-director/index.mjs') },
        { id: 'director-local', name: join(here, '../dsh-director/hosts/local.mjs'), config: { root, sessionId: 'lc-root',
          provider: 'fixture', model: 'fixture-model', dshEntry: entry, pollMs: 100, idleReviewMs: 600000,
          dormantCompactMinTokens: 0, rootRole: 'steward', channels: true } },
        { id: 'lc-channel', name: join(directory, 'fixture-lc-channel.mjs'), config: { dir: inDir } },
        { id: 'lc-probe', name: join(directory, 'lc-command-probe.mjs'), config: { dir: cmdDir, session: 'lc-root' } }] },
    ]));
    proc = spawn(process.execPath, [entry, '--profile', 'driver'], { cwd: directory, env: { ...withoutHostWiring(process.env),
      DSH_HOME: join(directory, 'home'), LC_TEST_DSH_ENTRY: entry, LC_TEST_URL: `http://127.0.0.1:${server.address().port}` } });
    proc.stdout.on('data', chunk => output += chunk); proc.stderr.on('data', chunk => output += chunk);
    await until('the channel plugin never loaded next to the local host', () => loaded === 1);

    const bind = await command('/bind slack');
    assert.equal(bind.kind, 'success', JSON.stringify(bind));
    const code = /: (\d{6})\n/.exec(bind.text)[1];
    // A stranger's guess: refused, one line to the stranger alone; the owner's code binds the owner.
    assert.deepEqual(await handIn({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'ts-0', text: code === '000000' ? '111111' : '000000' }),
      { accepted: false, reason: 'invalid_code' });
    assert.deepEqual(await handIn({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: code }), { accepted: true });
    assert.deepEqual(sends.map(s => [s.text.split(':')[0], s.options.to]), [['Not bound', 'U-STRANGER'], ['Bound', 'U-OWNER']]);
    assert.match((await command('/channels')).text, /- Slack: bound to U-OWNER since \S+; connected \(FIXTURE_STATUS\)/);
    assert.deepEqual(await handIn({ channel: 'slack', senderRef: 'U-OTHER', messageRef: 'ts-x', text: 'let me in' }), { accepted: false, reason: 'channel_sender_not_bound' });

    // The first owner message: answered with message_owner, mirrored once, to that message.
    assert.deepEqual(await handIn({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-2', text: 'how is the build?', at: new Date().toISOString() }), { accepted: true });
    await until('the tool reply never went out', () => replies('TOOL_REPLY').length === 1);
    assert.deepEqual(replies('TOOL_REPLY')[0], { text: 'TOOL_REPLY the build passed.', options: { replyToRef: 'ts-2', to: 'U-OWNER' } });
    const wake = events.find(e => e.type === 'user/message' && JSON.stringify(e.data).includes('Owner message from slack'));
    assert.match(JSON.stringify(wake.data), /how is the build\?/);
    assert.match(JSON.stringify(wake.data), /\\"source_channel\\":\\"slack\\"/);
    // The second: answered in plain words, the turn's last words mirrored once.
    assert.deepEqual(await handIn({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-3', text: 'and the tests?' }), { accepted: true });
    await until('the last words never went out', () => replies('AUTO_REPLY').length === 1);
    assert.deepEqual(replies('AUTO_REPLY')[0], { text: 'AUTO_REPLY all good.', options: { replyToRef: 'ts-3', to: 'U-OWNER' } });
    // A redelivery of an owner message is a duplicate: no second wake, no second reply.
    assert.deepEqual(await handIn({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-3', text: 'and the tests?' }), { accepted: true, duplicate: true });
    await new Promise(resolve => setTimeout(resolve, 3000));
    assert.equal(replies('TOOL_REPLY').length, 1, 'the tool reply went out more than once');
    assert.equal(replies('AUTO_REPLY').length, 1, 'the last words went out more than once');
    assert.ok(!replies('Steward idle').length, 'a turn that answered nobody was mirrored');
    // Recorded: two owner messages in, two replies out with the channel's refs.
    const rows = (await readFile(join(root, 'channels', 'messages.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    assert.deepEqual(rows.filter(r => r.direction === 'in').map(r => r.channel_message_ref), ['ts-2', 'ts-3']);
    const out = rows.filter(r => r.direction === 'out');
    assert.equal(out.length, 2, JSON.stringify(rows));
    assert.ok(out.every(r => /^out-\d+$/.test(r.channel_message_ref) && r.error === null), JSON.stringify(out));
    // The steward has this host's message tool and the QR tool; no command reached the model.
    const rootTools = requests.filter(r => r.session === 'lc-root' && r.purpose !== 'compaction' && r.tools.length).map(r => r.tools);
    assert.ok(rootTools.length && rootTools.every(tools => tools.includes('message_owner') && tools.includes('request_wechat_qr')), JSON.stringify(rootTools[0]));
    assert.ok(!events.some(e => JSON.stringify(e.data).includes(code)), 'the binding code reached the steward');
    assert.doesNotMatch(output, /SessionPersistenceCorruptionError|already registered/);
  } finally {
    if (proc && proc.exitCode === null) { proc.kill('SIGTERM'); await once(proc, 'exit'); }
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
