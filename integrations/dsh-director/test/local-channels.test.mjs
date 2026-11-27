/** The built-in local host's /status and its owner chat channels (hosts/local-channels.mjs):
 *  every `channels` op on plain files, the owner's channel commands, the conformance suite with
 *  `channels` declared, the steward's prompt and snapshot with channels, and the channel layer
 *  (lib/channels.mjs) running against this host as a channel plugin drives it. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, readdir, stat } from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { conformance } from '../conformance/index.mjs';
import { localHost } from '../hosts/local.mjs';
import { localChannels, CODE_TTL_MS, BIND_FAILURES_MAX, RATE_PER_MINUTE } from '../hosts/local-channels.mjs';
import { statusText } from '../hosts/local-commands.mjs';
import { validateHost } from '../lib/host.mjs';
import { createOwnerChannels, CHANNEL_LIMITS } from '../lib/channels.mjs';
import { stewardPrompt, stewardPromptFeatures, stewardPromptFor } from '../lib/steward-prompt.mjs';
import { MemoryStore } from '../lib/memory/store.mjs';
import { OWNER_TEXTS } from '../lib/owner-text.mjs';

const md = readFileSync(new URL('../steward.md', import.meta.url), 'utf8');
const fresh = () => mkdtemp(join(tmpdir(), 'dsh-director-lc-'));
const steward = { provider: 'p', model: 'm', rootRole: 'steward' };
const fakeCommands = () => {
  const defs = new Map();
  return { defs, register: def => { defs.set(def.name, def); return () => defs.delete(def.name); },
    run: async (name, rawInput = '') => defs.get(name).handler({ rawInput, agent: {}, attachments: [], signal: AbortSignal.timeout(10000) }) };
};
/** A director api for events(): notify collects, commands go to `commands`, session events to `listeners`. */
const fakeApi = ({ commands, memory, mirrored = [] } = {}) => {
  const notified = [], notifiedIds = [], listeners = [];
  const session = { id: 'root' };
  return { notified, notifiedIds, listeners, mirrored, memory, isDisposed: () => false, agent: { session },
    notify: async (text, options = {}) => { notified.push(text); notifiedIds.push(options.id); },
    ownerMessageSent: async (...args) => { mirrored.push(args); return { mirrored: true, channel: 'slack' }; },
    emit: (type, data) => listeners.forEach(fn => fn(session, { type, data })),
    ctx: { goals: { get: () => undefined, create: () => {} }, get: name => name === 'commands' ? commands : undefined,
      on: (name, fn) => { if (name === 'session/event') listeners.push(fn); return () => listeners.splice(listeners.indexOf(fn), 1); } } };
};
const until = async (what, met, ms = 3000) => {
  const deadline = Date.now() + ms;
  while (!(await met()) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(await met(), what);
};
/** A channels store on a fresh root, a clock the test moves, and a bound slack owner U1. */
async function bound(options = {}) {
  const root = await fresh();
  let clock = Date.parse('2026-11-27T10:00:00Z');
  const store = localChannels({ root, inbox: join(root, 'inbox'), now: () => clock, ...options });
  const { code } = store.owner.issueCode('slack');
  assert.deepEqual(await store.ops.bind({ channel: 'slack', senderRef: 'U1', code, messageRef: 'b-1' }), { bound: true, channel: 'slack', sender_ref: 'U1' });
  return { root, store, ops: store.ops, tick: ms => { clock += ms; }, now: () => clock };
}

// ---- /status

test('/status on an empty root says what is not there, and never throws', async () => {
  const root = await fresh();
  const text = await statusText({ root });
  assert.equal(text, [`Status of ${root}`, 'Phase: no phase reported yet', 'Work items: none', 'Open questions: 0'].join('\n'));
  const projectsDir = join(root, 'nowhere');
  assert.match(await statusText({ root, projectsDir }), /Open proposals: 0\nProjects: .*nowhere does not exist$/);
});

test('/status on a populated root: phase, work by state and the open items, questions, proposals, one line per project; hand-edited files are named', async () => {
  const root = await fresh(), projectsDir = join(root, 'projects');
  await writeFile(join(root, 'progress.jsonl'), '{"at":"2026-11-27T09:00:00Z","phase":"running"}\n{"at":"2026-11-27T09:05:00Z","phase":"failed","error":"no key\\nmore"}\nnot json\n');
  await writeFile(join(root, 'work.json'), JSON.stringify({ items: [{ id: 'w1', title: 'one', state: 'working', holder: 'director' },
    { id: 'w2', state: 'completed' }, { id: 'w3', title: 'three', state: 'input_required' }, 'junk'] }));
  await writeFile(join(root, 'questions.json'), JSON.stringify({ q1: { text: 'a?', status: 'open' }, q2: { text: 'b?', status: 'answered' } }));
  await writeFile(join(root, 'proposals.json'), JSON.stringify({ 'p-1': { status: 'open' }, 'p-2': { status: 'done' } }));
  await mkdir(join(projectsDir, 'alpha'), { recursive: true });
  await mkdir(join(projectsDir, 'beta'), { recursive: true });
  await mkdir(join(projectsDir, '.hidden'), { recursive: true });
  await writeFile(join(projectsDir, 'alpha', 'project.json'), JSON.stringify({ title: 'Alpha' }));
  await writeFile(join(projectsDir, 'alpha', 'progress.jsonl'), '{"at":"t1","phase":"dormant"}\n');
  await writeFile(join(projectsDir, 'alpha', 'work.json'), JSON.stringify({ items: [{ id: 'a', state: 'working' }] }));
  await writeFile(join(projectsDir, 'alpha', 'questions.json'), JSON.stringify({ x: { status: 'open' } }));
  await writeFile(join(projectsDir, 'beta', 'work.json'), '{ broken');
  const text = await statusText({ root, projectsDir });
  assert.equal(text, [`Status of ${root}`, 'Phase: failed at 2026-11-27T09:05:00Z - no key', 'Work items: 3 (1 working, 1 completed, 1 input_required)',
    'Open work (2):', '- w1 "one" [working, held by director]', '- w3 "three" [input_required]', 'Open questions: 1', 'Open proposals: 1',
    `Projects (2) in ${projectsDir}:`, '- alpha "Alpha": dormant at t1; work items: 1 (1 working); open questions: 1',
    '- beta: no phase reported yet; work items: work.json is unreadable: does not parse (' + text.split('does not parse (')[1].split('); open')[0] + '); open questions: 0'].join('\n'));
  // Registered for every root, director or steward; reading writes nothing.
  const commands = fakeCommands();
  const events = await localHost({ root, provider: 'p', model: 'm', pollMs: 60000 }).events(fakeApi({ commands }));
  assert.equal((await commands.run('status')).text, (await statusText({ root })));
  events.stop(); await events.done;
  const many = await fresh();
  await writeFile(join(many, 'work.json'), JSON.stringify({ items: Array.from({ length: 25 }, (_, i) => ({ id: `w${i}`, state: 'working' })) }));
  assert.match(await statusText({ root: many }), /- w19 \[working\]\n {2}\.\.\. and 5 more \(work\.json\)/);
});

// ---- the host's config

test('channels: only for a steward root; the capability, the tool and the texts come only with it', async () => {
  const root = await fresh();
  assert.throws(() => localHost({ root, provider: 'p', model: 'm', channels: true }), /channels needs rootRole: steward/);
  assert.throws(() => localHost({ root, ...steward, channels: 'yes' }), /channels must be true or false/);
  const plain = localHost({ root, ...steward });
  assert.ok(!plain.capabilities.includes('channels') && plain.channels === undefined && plain.texts === undefined);
  const host = localHost({ root, ...steward, channels: true });
  assert.equal(validateHost(host), host);
  assert.deepEqual(host.capabilities, ['progress', 'workState', 'heldWork', 'roles', 'install', 'setup', 'attention', 'channels', 'texts']);
  assert.equal(host.config.ownerDashboard, false);
  assert.match(host.texts.tools.request_wechat_qr, /written to a file on this machine/);
  assert.doesNotMatch(JSON.stringify(Object.values(host.texts.channelReplies).map(v => typeof v === 'function' ? v('x') : v)), /dashboard/);
  const table = JSON.parse(readFileSync(host.config.rolesFile, 'utf8'));
  assert.ok(host.roles(table).steward.presented.includes('message_owner'));
  assert.ok(!plain.roles(table).steward.presented.includes('message_owner'));
});

conformance('local host with channels', async () => {
  const root = await fresh();
  await writeFile(join(root, 'work.json'), JSON.stringify({ items: [{ id: 'w-done', state: 'completed' }, { id: 'w-live', state: 'working', holder: 'director' }] }));
  const host = localHost({ root, ...steward, channels: true, pollMs: 20 });
  let n = 0;
  const api = fakeApi();
  return { host, api, seed: { settled: 'w-done', active: 'w-live', held: 'w-live',
    event: () => writeFile(join(root, 'inbox', `e${++n}.json`), JSON.stringify({ n })) } };
});

// ---- bind

test('bind: a valid code binds the sender who sends it, once; a reused, expired or wrong code is refused; a redelivery is a duplicate', async () => {
  const { store, ops, tick } = await bound();
  assert.deepEqual(await ops.list(), { default_channel: null, channels: [
    { channel: 'slack', bound: true, sender_ref: 'U1', bound_at: '2026-11-27T10:00:00.000Z', revoked_at: null, is_default: false },
    { channel: 'wechat', bound: false, sender_ref: null, bound_at: null, revoked_at: null, is_default: false }] });
  // The same binding message again: its first answer, nothing counted.
  assert.deepEqual(await ops.bind({ channel: 'slack', senderRef: 'U1', code: '000000', messageRef: 'b-1' }),
    { bound: true, channel: 'slack', sender_ref: 'U1', duplicate: true });
  const { code } = store.owner.issueCode('wechat');
  assert.deepEqual(await ops.bind({ channel: 'slack', senderRef: 'U2', code, messageRef: 'b-2' }), { refused: 'invalid_code' }, 'a code is its channel\'s');
  assert.deepEqual(await ops.bind({ channel: 'wechat', senderRef: 'W1', code, messageRef: 'w-1' }), { bound: true, channel: 'wechat', sender_ref: 'W1' });
  assert.deepEqual(await ops.bind({ channel: 'wechat', senderRef: 'W2', code, messageRef: 'w-2' }), { refused: 'code_used' });
  assert.deepEqual(await ops.bind({ channel: 'wechat', senderRef: 'W2', code, messageRef: 'w-2' }),
    { bound: false, channel: 'wechat', sender_ref: 'W2', duplicate: true, refusal: 'code_used' });
  const late = store.owner.issueCode('wechat').code;
  tick(CODE_TTL_MS + 1);
  assert.deepEqual(await ops.bind({ channel: 'wechat', senderRef: 'W2', code: late, messageRef: 'w-3' }), { refused: 'code_expired' });
  assert.equal((await ops.list()).channels[1].sender_ref, 'W1', 'a refused bind changes nothing');
  // A new code replaces the old one; wrong guesses while it is live retire it, even for the right code.
  const first = store.owner.issueCode('slack').code, second = store.owner.issueCode('slack').code;
  if (first !== second) assert.deepEqual(await ops.bind({ channel: 'slack', senderRef: 'U9', code: first, messageRef: 'x-0' }), { refused: 'invalid_code' });
  for (let i = 1; i < BIND_FAILURES_MAX; i++)
    assert.deepEqual(await ops.bind({ channel: 'slack', senderRef: 'U9', code: second === '999999' ? '999998' : '999999', messageRef: `x-${i}` }), { refused: 'invalid_code' });
  assert.deepEqual(await ops.bind({ channel: 'slack', senderRef: 'U9', code: second, messageRef: 'x-9' }), { refused: 'code_retired' });
  assert.equal((await ops.list()).channels[0].sender_ref, 'U1');
  // Nothing on disk carries a code.
  const disk = readFileSync(join(store.dir, 'channels.json'), 'utf8');
  for (const c of [code, late, first, second]) assert.ok(!disk.includes(`"${c}"`), 'a code reached channels.json');
  assert.equal((await stat(join(store.dir, 'channels.json'))).mode & 0o777, 0o600);
  assert.deepEqual(await ops.bind({ channel: 'email', senderRef: 'x', code, messageRef: 'm' }).then(r => r.refused), 'invalid_channel');
});

// ---- relay

test('relay: the bound sender\'s message wakes the steward once and is recorded; a stranger is refused; a redelivery is a duplicate', async () => {
  const memRoot = await fresh();
  const memory = await MemoryStore.open({ root: memRoot, userId: 'owner' });
  try {
    const { root, ops, store } = await bound({ memory: () => memory });
    const at = '2026-11-27T09:59:30Z';
    const first = await ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-1', text: 'hello from slack', at });
    assert.deepEqual(Object.keys(first).sort(), ['approval', 'duplicate', 'message_id']);
    assert.match(first.message_id, /^m-[0-9a-f]{20}$/);
    assert.equal(first.duplicate, false);
    const wake = JSON.parse(await readFile(join(root, 'inbox', `owner-message-${first.message_id}.json`), 'utf8'));
    assert.deepEqual(wake, { message_id: first.message_id, source_channel: 'slack', created_at: '2026-11-27T10:00:00.000Z', channel_at: '2026-11-27T09:59:30.000Z', text: 'hello from slack' });
    assert.deepEqual(await ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-1', text: 'hello from slack', at }),
      { message_id: first.message_id, duplicate: true, approval: null });
    assert.equal((await readdir(join(root, 'inbox'))).filter(n => n.startsWith('owner-message-')).length, 1);
    assert.deepEqual(await ops.relay({ channel: 'slack', senderRef: 'U2', messageRef: 'ts-1', text: 'me too' }), { refused: 'channel_sender_not_bound' });
    assert.deepEqual(await ops.relay({ channel: 'wechat', senderRef: 'U1', messageRef: 'ts-9', text: 'wrong channel' }), { refused: 'channel_sender_not_bound' });
    assert.deepEqual(await ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-2', text: '   ' }), { refused: 'empty_body' });
    assert.deepEqual(await ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-3', text: 'x'.repeat(4097) }), { refused: 'body_too_long' });
    // The binding message itself, relayed after it bound (the channel's redelivery): never an owner message.
    assert.deepEqual(await ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'b-1', text: '123456' }), { message_id: null, duplicate: true, approval: null });
    // Its source, and its memory turn with the channel.
    assert.deepEqual(await ops.sourceOf(first.message_id), { direction: 'in', channel: 'slack', sender_ref: 'U1', channel_message_ref: 'ts-1',
      at: '2026-11-27T10:00:00.000Z', error: null, channel_at: '2026-11-27T09:59:30.000Z' });
    assert.equal(await ops.sourceOf('m-none'), null);
    const entry = (await memory.read([`t-${first.message_id}-o`])).entries?.[0] ?? (await memory.read([`t-${first.message_id}-o`]));
    assert.match(JSON.stringify(entry), /hello from slack/);
    assert.match(JSON.stringify(entry), /slack/);
    assert.equal((await stat(join(store.dir, 'messages.jsonl'))).mode & 0o777, 0o600);
    // A restart reads the record: the redelivery is still a duplicate.
    const again = localChannels({ root, inbox: join(root, 'inbox'), now: () => Date.parse('2026-11-27T10:00:00Z') });
    assert.equal((await again.ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-1', text: 'hello from slack' })).duplicate, true);
  } finally { await memory.close(); }
});

test('relay: a live binding code from the bound account is retired and never written; the minute\'s share is bounded', async () => {
  const { root, store, ops, tick } = await bound();
  const { code } = store.owner.issueCode('wechat');
  const spaced = `my code ${code.slice(0, 3)} ${code.slice(3)}`;
  assert.deepEqual(await ops.codeCheck({ channel: 'slack', senderRef: 'U1', text: spaced }), { store_text: false });
  assert.deepEqual(await ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'c-1', text: spaced }), { refused: 'code_sent_to_bound_account' });
  assert.deepEqual(await ops.bind({ channel: 'wechat', senderRef: 'W1', code, messageRef: 'w-1' }), { refused: 'code_retired' });
  assert.ok(!existsSync(join(root, 'inbox')) || !(await readdir(join(root, 'inbox'))).length, 'nothing was written for it');
  // Full-width digits are the same code.
  const other = store.owner.issueCode('wechat').code;
  const wide = [...other].map(d => String.fromCodePoint(0xFF10 + Number(d))).join('');
  assert.deepEqual(await ops.codeCheck({ channel: 'slack', senderRef: 'U1', text: wide }), { store_text: false });
  assert.deepEqual(await ops.codeCheck({ channel: 'slack', senderRef: 'U1', text: 'an ordinary 123 message' }), { store_text: true });
  assert.deepEqual(await ops.codeCheck({ channel: 'slack', senderRef: 'U2', text: 'hi' }), { store_text: false }, 'a stranger\'s text is never kept');
  assert.deepEqual(await ops.senderBound({ channel: 'slack', senderRef: 'U1' }), { bound: true });
  assert.deepEqual(await ops.senderBound({ channel: 'slack', senderRef: 'U2' }), { bound: false });
  for (let i = 0; i < RATE_PER_MINUTE; i++) assert.ok(!(await ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: `r-${i}`, text: `m${i}` })).refused);
  assert.deepEqual(await ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'r-x', text: 'one too many' }), { refused: 'rate_limited' });
  tick(61000);
  assert.ok(!(await ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'r-x', text: 'one too many' })).refused);
});

test('relay: an exact approval phrase from the bound owner decides the proposal (with projectsDir); undated, old or unknown ones do not', async () => {
  const root = await fresh(), projectsDir = join(root, 'projects');
  await mkdir(projectsDir);
  const host = localHost({ root, ...steward, channels: true, projectsDir });
  const store = host.channels;
  const seed = [{ key: 'g1', goal: 'write it', acceptance: [{ id: 'c', kind: 'review', description: 'ok' }] }];
  assert.equal((await host.proposals.propose({ kind: 'create_project', reason: 'asked', payload: { project_id: 'gamma', title: 'G', goal: 'try', seed_nodes: seed } })).proposal_id, 'p-1');
  assert.equal((await host.proposals.propose({ kind: 'create_project', reason: 'asked', payload: { project_id: 'delta', title: 'D', goal: 'try' } })).proposal_id, 'p-2');
  const commands = fakeCommands();
  const events = await host.events(fakeApi({ commands }));
  try {
    const code = (await commands.run('bind', ' slack')).text.match(/: (\d{6})$/m)[1];
    assert.equal((await store.bind({ channel: 'slack', senderRef: 'U1', code, messageRef: 'b' })).bound, true);
    const now = new Date().toISOString();
    const approved = await store.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'a-1', text: ' 同意 proposal p-1 ', at: now });
    assert.deepEqual(approved.approval, { proposal_id: 'p-1', action: 'approve', outcome: 'executed', kind: 'create_project', target: 'gamma',
      result: { status: 'done', result: { project_id: 'gamma', root: join(projectsDir, 'gamma'), work_items: 1 } } });
    const stored = JSON.parse(await readFile(join(root, 'proposals.json'), 'utf8'))['p-1'];
    assert.equal(stored.decided_by, 'owner:slack:U1');
    assert.ok(existsSync(join(projectsDir, 'gamma', 'work.json')));
    // The message still reaches the steward, and the redelivery answers the same decision.
    assert.ok(existsSync(join(root, 'inbox', `owner-message-${approved.message_id}.json`)) || existsSync(join(root, 'inbox', 'done', `owner-message-${approved.message_id}.json`)));
    assert.deepEqual((await store.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'a-1', text: '同意 proposal p-1', at: now })).approval, approved.approval);
    assert.deepEqual((await store.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'a-2', text: 'approve proposal p-1', at: now })).approval,
      { proposal_id: 'p-1', action: 'approve', outcome: 'refused', refusal: 'proposal_not_open' });
    assert.deepEqual((await store.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'a-3', text: 'decline proposal p-2' })).approval,
      { proposal_id: 'p-2', action: 'decline', outcome: 'refused', refusal: 'approval_undated' });
    assert.deepEqual((await store.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'a-4', text: 'decline proposal p-2', at: '2026-01-01T00:00:00Z' })).approval.refusal, 'approval_too_old');
    assert.equal((await store.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'a-5', text: '拒绝 proposal p-9', at: now })).approval, null, 'no such proposal: an ordinary message');
    const declined = await store.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'a-7', text: '拒绝 proposal p-2', at: now });
    assert.deepEqual([declined.approval.outcome, declined.approval.result.status], ['executed', 'declined']);
  } finally { events.stop(); await events.done; }
  // Without projectsDir the phrase is an ordinary message.
  const { ops } = await bound();
  assert.equal((await ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'z', text: '同意 proposal p-1', at: '2026-11-27T10:00:00Z' })).approval, null);
});

// ---- sourceOf / recordDelivery / list / default / unbind / status

test('recordDelivery takes only a message the steward sent, once; sourceOf then answers it; list, default and unbind', async () => {
  const { ops, store } = await bound();
  assert.deepEqual(await ops.recordDelivery({ messageId: 's-x', channel: 'slack' }), { refused: 'unknown_message', reason: 'not a message this steward sent its owner' });
  assert.equal((await store.steward.record({ message_id: 's-1' })).recorded, true);
  assert.equal((await store.steward.record({ message_id: 's-1' })).duplicate, true);
  assert.deepEqual(await ops.recordDelivery({ messageId: 's-1', channel: 'slack', channelMessageRef: 'out-ts' }), { recorded: true, message_id: 's-1', channel: 'slack' });
  assert.deepEqual(await ops.recordDelivery({ messageId: 's-1', channel: 'slack' }), { refused: 'delivery_recorded' });
  assert.deepEqual(await ops.sourceOf('s-1'), { direction: 'out', channel: 'slack', sender_ref: null, channel_message_ref: 'out-ts', at: '2026-11-27T10:00:00.000Z', error: null });
  assert.equal((await store.steward.record({ message_id: 's-2' })).recorded, true);
  await ops.recordDelivery({ messageId: 's-2', channel: 'wechat', error: 'channel_not_connected' });
  assert.equal((await ops.sourceOf('s-2')).error, 'channel_not_connected');
  assert.deepEqual(await store.owner.setDefault('wechat'), { default_channel: 'wechat' });
  assert.equal((await ops.list()).default_channel, 'wechat');
  assert.deepEqual((await ops.list()).channels.map(c => c.is_default), [false, true]);
  assert.deepEqual(await store.owner.setDefault(null), { default_channel: null });
  assert.equal((await store.owner.setDefault('email')).refused, 'invalid_channel');
  assert.deepEqual(await store.owner.unbind('slack'), { unbound: true, channel: 'slack', sender_ref: 'U1' });
  assert.deepEqual(await store.owner.unbind('slack'), { unbound: false, channel: 'slack' });
  const slack = (await ops.list()).channels[0];
  assert.equal(slack.bound, false);
  assert.ok(slack.revoked_at);
  assert.deepEqual(await ops.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'after', text: 'hi' }), { refused: 'channel_sender_not_bound' });
});

test('reportStatus: status.json and, while a scan is needed, the QR in its own 0600 file; never a script', async () => {
  const { store, ops } = await bound();
  const qr = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#000"/><path d="M0 0h1v1H0z"/></svg>';
  assert.deepEqual(await ops.reportStatus({ channel: 'wechat', connected: false, detail: 'scan the QR', qrSvg: qr }),
    { recorded: true, channel: 'wechat', connected: false, at: '2026-11-27T10:00:00.000Z' });
  assert.equal(await readFile(store.qrFile('wechat'), 'utf8'), qr);
  assert.equal((await stat(store.qrFile('wechat'))).mode & 0o777, 0o600);
  assert.deepEqual(JSON.parse(await readFile(join(store.dir, 'status.json'), 'utf8')).wechat, { connected: false, detail: 'scan the QR', at: '2026-11-27T10:00:00.000Z' });
  assert.doesNotMatch(await readFile(join(store.dir, 'status.json'), 'utf8'), /svg/);
  assert.deepEqual(await ops.reportStatus({ channel: 'wechat', connected: false, qrSvg: '<svg><script>alert(1)</script></svg>' }), { refused: 'qr_svg_refused' });
  assert.deepEqual(await ops.reportStatus({ channel: 'wechat', connected: false, qrSvg: '<svg onload="x()"></svg>' }), { refused: 'qr_svg_refused' });
  assert.deepEqual(await ops.reportStatus({ channel: 'wechat', connected: false, detail: 'x'.repeat(301) }), { refused: 'detail_too_long' });
  assert.ok(existsSync(store.qrFile('wechat')), 'a refused report changes nothing');
  await ops.reportStatus({ channel: 'wechat', connected: true });
  assert.ok(!existsSync(store.qrFile('wechat')), 'connected: the QR is gone');
  await ops.reportStatus({ channel: 'slack', connected: true, detail: 'ok' });
  assert.deepEqual(Object.keys(JSON.parse(await readFile(join(store.dir, 'status.json'), 'utf8'))).sort(), ['slack', 'wechat']);
});

// ---- the owner's commands

test('/bind, /channels, /unbind and /default: the owner binds, sees and changes the channels; the code is shown once, never stored', async () => {
  const root = await fresh();
  const host = localHost({ root, ...steward, channels: true, pollMs: 60000 });
  const commands = fakeCommands();
  const events = await host.events(fakeApi({ commands }));
  try {
    assert.deepEqual([...commands.defs.keys()].sort(), ['answer', 'bind', 'channels', 'default', 'questions', 'status', 'unbind']);
    assert.equal((await commands.run('bind', '')).kind, 'error');
    assert.equal((await commands.run('bind', ' email')).kind, 'error');
    const bind = await commands.run('bind', ' WeChat');
    assert.equal(bind.kind, 'success');
    assert.match(bind.text, /^Send this code to the bot from your own WeChat account before \S+: \d{6}\n/);
    assert.match(bind.text, /No WeChat channel plugin has reported yet/);
    const code = bind.text.match(/: (\d{6})\n/)[1];
    let shown = (await commands.run('channels')).text;
    assert.match(shown, /- WeChat: not bound; its plugin has not reported/);
    assert.match(shown, /a binding code is waiting until /);
    assert.doesNotMatch(shown, new RegExp(code), '/channels never shows the code again');
    assert.match(shown, /Default channel: none/);
    assert.equal((await host.channels.bind({ channel: 'wechat', senderRef: 'wx-owner', code, messageRef: 'm1' })).bound, true);
    const qr = '<svg viewBox="0 0 1 1"><rect width="1" height="1"/></svg>';
    await host.channels.reportStatus({ channel: 'wechat', connected: false, detail: 'logged out', qrSvg: qr });
    shown = (await commands.run('channels')).text;
    assert.match(shown, /- WeChat: bound to wx-owner since \S+; not connected \(logged out\), reported \S+/);
    assert.match(shown, new RegExp(`login QR: ${join(root, 'channels', 'wechat-qr.svg').replace(/[.]/g, '\\.')} - open it and scan it`));
    assert.doesNotMatch(shown, /<svg/, 'the QR itself is never shown');
    assert.equal((await commands.run('default', ' wechat')).text, 'Default channel: WeChat.');
    assert.match((await commands.run('channels')).text, /Default channel: WeChat - /);
    assert.equal((await commands.run('default', ' none')).kind, 'success');
    assert.equal((await commands.run('default', ' fax')).kind, 'error');
    assert.match((await commands.run('unbind', ' wechat')).text, /^Unbound WeChat \(wx-owner\)/);
    assert.equal((await commands.run('unbind', ' wechat')).text, 'WeChat is not bound.');
    assert.match((await commands.run('channels')).text, /- WeChat: not bound \(unbound at /);
    for (const file of await readdir(join(root, 'channels'))) assert.ok(!readFileSync(join(root, 'channels', file), 'utf8').includes(code), file);
  } finally { events.stop(); await events.done; }
});

// ---- the steward's side: the wake, message_owner and the turn's last words

/** setup()'s tools: what the local host registers in the root's scope. */
async function stewardTools(host, ownerMessageSent) {
  const tools = new Map();
  await host.setup({ tools: { register: tool => tools.set(tool.name, tool) }, systemPrompt: { section() {} } }, undefined,
    { defineTool: tool => tool, jsonOutput: {}, runtimeSection() {}, ownerMessageSent });
  return tools;
}

test('the steward is woken by the owner message, answers it with message_owner (mirrored once, recorded), and a retry sends nothing new', async () => {
  const root = await fresh();
  const host = localHost({ root, ...steward, channels: true, pollMs: 20 });
  const commands = fakeCommands();
  const api = fakeApi({ commands });
  const events = await host.events(api);
  try {
    const store = host.channels;
    const tools = await stewardTools(host, api.ownerMessageSent);
    assert.ok(tools.has('message_owner'));
    const bindCode = (await commands.run('bind', ' slack')).text.match(/: (\d{6})\n/)[1];
    assert.equal((await store.bind({ channel: 'slack', senderRef: 'U1', code: bindCode, messageRef: 'b' })).bound, true);
    const { message_id: id } = await store.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-1', text: 'how is alpha?', at: new Date().toISOString() });
    await until('the owner message never woke the steward', () => api.notified.length === 1);
    assert.match(api.notified[0], new RegExp(`^Owner message from slack \\(owner-message-${id}\\.json\\):\\n\\{"message_id":"${id}","source_channel":"slack"`));
    assert.match(api.notifiedIds[0], new RegExp(`^inbox:owner-message-${id}\\.json:`));
    const send = tools.get('message_owner');
    assert.equal((await send.execute({ request_key: 'k', body: 'hi', reply_to_message_id: 'm-nope' })).refused, 'unknown_message');
    assert.equal((await send.execute({ request_key: '', body: 'hi' })).refused, 'request_key_required');
    assert.equal((await send.execute({ request_key: 'k', body: '  ' })).refused, 'empty_body');
    const sent = await send.execute({ request_key: 'r-1', body: 'Alpha is on track.', reply_to_message_id: id });
    assert.deepEqual(sent, { sent: true, message_id: sent.message_id, mirrored: true, channel: 'slack' });
    assert.deepEqual(api.mirrored, [[sent.message_id, 'Alpha is on track.', id]]);
    const retry = await send.execute({ request_key: 'r-1', body: 'Alpha is on track.', reply_to_message_id: id });
    assert.equal(retry.duplicate, true);
    assert.equal(retry.sent, false);
    assert.equal((await send.execute({ request_key: 'r-1', body: 'Something else.' })).refused, 'request_key_reused');
    // Answered: the turn's last words do not go there again.
    api.emit('turn/start', {});
    api.emit('user/message', { content: [{ type: 'text', text: api.notified[0] }] });
    api.emit('assistant/message', { message: { content: [{ type: 'text', text: 'Done here.' }] } });
    api.emit('turn/end', { reason: { kind: 'completed' } });
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(api.mirrored.length, 2, 'only the retry asked the channel layer again (which answers already_mirrored itself)');
    // Unprompted: no reply_to, the default channel's business (the channel layer's).
    const own = await send.execute({ request_key: 'r-2', body: 'News: beta finished.' });
    assert.deepEqual(api.mirrored.at(-1), [own.message_id, 'News: beta finished.', undefined]);
  } finally { events.stop(); await events.done; }
});

test('a turn given an owner message that ends with it unanswered sends its last words there, once; a turn that was not given one sends nothing', async () => {
  const root = await fresh();
  const memRoot = await fresh();
  const memory = await MemoryStore.open({ root: memRoot, userId: 'owner' });
  const host = localHost({ root, ...steward, channels: true, pollMs: 20 });
  const commands = fakeCommands();
  const api = fakeApi({ commands, memory });
  const events = await host.events(api);
  try {
    const code = (await commands.run('bind', ' slack')).text.match(/: (\d{6})\n/)[1];
    assert.equal((await host.channels.bind({ channel: 'slack', senderRef: 'U1', code, messageRef: 'b' })).bound, true);
    const { message_id: id } = await host.channels.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-1', text: 'are you there?' });
    await until('no wake', () => api.notified.length === 1);
    // A turn without it: nothing.
    api.emit('turn/start', {});
    api.emit('user/message', { content: [{ type: 'text', text: 'Steward event (x.json): {}' }] });
    api.emit('assistant/message', { message: { content: [{ type: 'text', text: 'nothing for the owner' }] } });
    api.emit('turn/end', { reason: { kind: 'completed' } });
    // The turn with it, ending with the clock line copied in front.
    api.emit('turn/start', {});
    api.emit('user/message', { content: [{ type: 'text', text: api.notified[0] }] });
    api.emit('assistant/message', { message: { content: [{ type: 'text', text: '[system clock] 2026-11-27 10:00 UTC\nYes, I am here.' }] } });
    api.emit('turn/end', { reason: { kind: 'completed' } });
    await until('the last words never went out', () => api.mirrored.length === 1);
    assert.deepEqual(api.mirrored[0], [`auto-${id}`, 'Yes, I am here.', id]);
    // The same turn's events again (a replay): nothing more.
    api.emit('turn/start', {});
    api.emit('user/message', { content: [{ type: 'text', text: api.notified[0] }] });
    api.emit('assistant/message', { message: { content: [{ type: 'text', text: 'Again.' }] } });
    api.emit('turn/end', { reason: { kind: 'completed' } });
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(api.mirrored.length, 1);
    // The owner's words and the reply are one turn in memory, the owner's with its channel.
    await until('the reply never reached memory', async () => JSON.stringify(await memory.read([`t-${id}-auto-${id}`])).includes('Yes, I am here.'));
    assert.match(JSON.stringify(await memory.read([`t-${id}-o`])), /are you there\?/);
  } finally { events.stop(); await events.done; await memory.close(); }
});

// ---- the channel layer against this host, as a channel plugin drives it

test('lib/channels.mjs on the local host: a stranger\'s code binds, the owner\'s message is relayed with a receipt, the reply is mirrored once', async () => {
  const root = await fresh();
  const host = localHost({ root, ...steward, channels: true, pollMs: 20 });
  const commands = fakeCommands();
  const api = fakeApi({ commands });
  const events = await host.events(api);
  const logs = [];
  const channels = createOwnerChannels({ host: host.channels, replies: { ...OWNER_TEXTS.en.replies, ...host.texts.channelReplies },
    limits: CHANNEL_LIMITS, owedFile: join(root, 'state', 'channel-owed.json'), log: (event, fields) => logs.push([event, fields]) });
  const sent = [];
  channels.service.register({ name: 'slack', status: () => ({ connected: true, detail: 'ok' }),
    async send(text, options) { sent.push({ text, options }); return { ref: `out-${sent.length}` }; } });
  channels.ready();
  try {
    const code = (await commands.run('bind', ' slack')).text.match(/: (\d{6})\n/)[1];
    assert.deepEqual(await channels.service.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 's-1', text: '000000' === code ? '111111' : '000000' }),
      { accepted: false, reason: 'invalid_code' });
    assert.deepEqual(await channels.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 's-2', text: code }), { accepted: true });
    assert.deepEqual(sent.map(s => [s.text.split(':')[0], s.options.to]), [['Not bound', 'U-STRANGER'], ['Bound', 'U-OWNER']]);
    assert.deepEqual(await channels.service.inbound({ channel: 'slack', senderRef: 'U-OTHER', messageRef: 's-3', text: 'let me in' }), { accepted: false, reason: 'channel_sender_not_bound' });
    assert.equal(sent.length, 2, 'a stranger is told nothing');
    assert.deepEqual(await channels.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 's-4', text: 'status please', at: new Date().toISOString() }), { accepted: true });
    assert.deepEqual(sent.at(-1).text, 'Received; working on it…');
    await until('no wake', () => api.notified.length === 1);
    const id = JSON.parse(api.notified[0].split('\n').slice(1).join('\n')).message_id;
    // The steward's reply through the real channel layer: out once, recorded once.
    const tools = await stewardTools(host, channels.ownerMessageSent);
    const reply = await tools.get('message_owner').execute({ request_key: 'r', body: 'All quiet.', reply_to_message_id: id });
    assert.deepEqual(reply, { sent: true, message_id: reply.message_id, mirrored: true, channel: 'slack' });
    assert.deepEqual(sent.at(-1), { text: 'All quiet.', options: { replyToRef: 's-4', to: 'U-OWNER' } });
    assert.equal((await host.channels.sourceOf(reply.message_id)).channel_message_ref, `out-${sent.length}`);
    const again = await tools.get('message_owner').execute({ request_key: 'r', body: 'All quiet.', reply_to_message_id: id });
    assert.deepEqual([again.duplicate, again.mirrored, again.reason], [true, false, 'already_mirrored']);
    assert.equal(sent.filter(s => s.text === 'All quiet.').length, 1);
    // A long reply is cut with this host's tail, which names no dashboard.
    await host.channels.reportStatus({ channel: 'slack', connected: true });
    const long = await tools.get('message_owner').execute({ request_key: 'long', body: 'word '.repeat(400), reply_to_message_id: id });
    assert.equal(long.mirrored, true);
    assert.match(sent.at(-1).text, /… \(full text in your steward's DSH conversation\)$/);
    // Unprompted, with no default channel: not mirrored, and said so.
    assert.equal((await tools.get('message_owner').execute({ request_key: 'u', body: 'news' })).reason, 'no_default_channel');
    assert.equal((await commands.run('default', ' slack')).kind, 'success');
    const news = await tools.get('message_owner').execute({ request_key: 'u2', body: 'news again' });
    assert.deepEqual([news.mirrored, sent.at(-1).text, sent.at(-1).options.to], [true, 'news again', 'U-OWNER']);
  } finally { channels.stop(); events.stop(); await events.done; }
});

// ---- the prompt and the snapshot

test('with channels the steward is told about its chat channels and how to answer there - and still about no dashboard, relay tool, reminder or brief', () => {
  for (const [label, options] of [['bare', {}], ['projectsDir and memory', { projectsDir: '/tmp/projects', memory: { userId: 'o' } }]]) {
    const host = localHost({ root: '/tmp/x', ...steward, channels: true, ...options });
    const features = stewardPromptFeatures(host.capabilities, { memory: host.config.memory, dashboard: host.config.ownerDashboard !== false });
    const prompt = stewardPrompt(md, features);
    for (const word of ['dashboard', 'Daily brief', 'reminder', 'Q12', 'relay tool', 'Relaying is not answering', 'handoff', 'family', 'portfolio_inbox',
      'has no Slack', 'message tool your host lists'])
      assert.ok(!prompt.includes(word), `${label}: the steward is told about ${word}:\n${prompt}`);
    for (const passage of ['Every message to your owner is read on a phone', 'A chat channel shows a long message\ncut short.',
      'Chat channels: your owner binds their chat accounts (Slack, WeChat) with the /bind command;', 'call request_wechat_qr once',
      'tell your owner to type /channels', 'answer it with message_owner', 'sent there as your reply', '`channel_at`, when present',
      'to your owner\'s default channel when they chose one'])
      assert.ok(prompt.includes(passage), `${label}: not told ${passage}:\n${prompt}`);
    assert.ok(!/\n{3}/.test(prompt) && !/ \n/.test(prompt), 'no paragraph left torn');
    if (options.projectsDir) {
      assert.match(prompt, /`同意 proposal <id>` to approve, `拒绝 proposal <id>` to decline/);
      assert.match(prompt, /send that line back from Slack or WeChat,\n {2}or decide here with the command your context snapshot's owner_commands names\. Only that exact\n {2}line or that command, sent by your owner, decides; you\n {2}never approve/);
      assert.match(prompt, /\(the approval phrase above and a command your snapshot's owner_commands\nnames, with its id, are the two exceptions\)/);
      assert.match(prompt, /Your memory records it with its channel\./);
      const en = stewardPromptFor(md, { features, language: 'en' });
      assert.match(en, /`approve proposal <id>` to approve/);
    } else {
      assert.ok(!prompt.includes('proposal'), prompt);
      assert.ok(!prompt.includes('Your memory records it'), 'no memory: not told it records');
    }
  }
  // The local host without channels is told exactly what it was told before.
  const before = localHost({ root: '/tmp/x', ...steward, projectsDir: '/tmp/p' });
  assert.equal(stewardPrompt(md, stewardPromptFeatures(before.capabilities, { dashboard: false })), stewardPrompt(md, stewardPromptFeatures(before.capabilities, {})));
});

test('the steward\'s snapshot with channels names the channels and both ways to decide', async () => {
  const root = await fresh();
  const plain = JSON.parse(await localHost({ root, ...steward, channels: true }).context('steward')).owner_commands;
  assert.match(plain, /^Your owner is in this DSH and on the chat channels they bound with \/bind \(Slack, WeChat; \/channels lists them\), with no dashboard: they list your open questions with \/questions/);
  assert.doesNotMatch(plain, /no Slack/);
  const full = JSON.parse(await localHost({ root, ...steward, channels: true, projectsDir: root }).context('steward')).owner_commands;
  assert.match(full, /\/approve <id> or \/decline <id> \[reason\] here, or from a bound chat channel with the exact approval phrase line\. When you ask them to decide a proposal, give them both the phrase line and the command\.$/);
});
