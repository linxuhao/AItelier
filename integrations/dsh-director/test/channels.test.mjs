/** Design-steward 7.2: the owner's chat channels as the director connects them - a channel
 *  plugin's message reaches the host's relay or bind, the sender is answered only about its
 *  own message (and a stranger only about a binding attempt), and the root's message to its
 *  owner goes back out on the right channel once. The host here is a fake with the
 *  host.d.ts `channels` shape. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createOwnerChannels, CHANNEL_NAMES, CHANNEL_LIMITS, channelReplies, channelText, MORE, alertText } from '../lib/channels.mjs';
import { validateHost } from '../lib/host.mjs';
import { PROPOSAL_KINDS } from '../lib/proposals.mjs';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BODY = 'SECRET_BODY_TEXT 请帮我看看';
// A channel time a minute ago: a decision line is never sent more than an hour after the owner
// wrote (CHANNEL_LIMITS.lineMaxAgeMs), so a test about a line that IS sent dates its message now.
const RECENT = new Date(Date.now() - 60000).toISOString();
// The time of a message that must still get the receipt (CHANNEL_LIMITS.receiptMaxAgeMs, five
// minutes): taken when the test runs, not when this file loads - plugin-boundary.test.mjs
// imports it after minutes of other suites.
const NOW = () => new Date().toISOString();
// The owed store's default path comes from the environment ($DSH_CHANNEL_OWED_FILE). A test
// must never write where a running director writes, and two tests must never share one store:
// without this the ambient environment decides what the suite reads and writes.
delete process.env.DSH_CHANNEL_OWED_FILE;

/** A fake host.channels: one binding per channel, the relay answers what `next` says. */
function fakeHost({ bindings = { slack: 'U-OWNER' }, defaultChannel = null } = {}) {
  const calls = [], sources = new Map();
  const host = {
    next: undefined,
    bindResult: { bound: true },
    async list() {
      calls.push(['list']);
      return { default_channel: defaultChannel, channels: CHANNEL_NAMES.map(channel => ({ channel, bound: Boolean(bindings[channel]),
        sender_ref: bindings[channel] ?? null, bound_at: null, revoked_at: null, is_default: channel === defaultChannel })) };
    },
    async relay(message) { calls.push(['relay', message]); return host.next ?? { message_id: 'm-in', duplicate: false, approval: null }; },
    // The host's own answer to "may this text be kept" (channels.codeCheck). `storeText: false`
    // is the answer for a text carrying a live binding code; `codeCheckFails` is a host that
    // cannot answer at all, which is no permission either.
    storeText: true,
    async codeCheck(message) { calls.push(['codeCheck', message]); if (host.codeCheckFails) throw new Error('HTTP 502');
      return { store_text: host.storeText !== false }; },
    async bind(request) { calls.push(['bind', request]); if (host.bindResult.bound) bindings[request.channel] = request.senderRef; return host.bindResult; },
    async sourceOf(id) { calls.push(['sourceOf', id]); return sources.get(id) ?? null; },
    async recordDelivery(delivery) {
      calls.push(['recordDelivery', delivery]);
      sources.set(delivery.messageId, { direction: 'out', channel: delivery.channel, sender_ref: null,
        channel_message_ref: delivery.channelMessageRef ?? null, at: 'now', error: delivery.error ?? null });
      return { recorded: true };
    },
  };
  return { host, calls, sources, bindings };
}

/** A fake channel plugin: what it was asked to send. `fail` may be changed after it registers. */
function fakeChannel(name, { fail } = {}) {
  const sent = [];
  const fake = { sent, fail, channel: { name, status: () => ({ connected: true }),
    async send(text, options) { sent.push({ text, options }); if (fake.fail) throw new Error(fake.fail); return { ref: `${name}-ref-${sent.length}` }; } } };
  return fake;
}

/** A fresh owed store per setup (the file mode is what a restart reads); pass `owedFile` to
 *  share one between two setups (a restart), or `ready: false` to start the poll later. */
const setup = (options, limits, { owedFile, ready = true } = {}) => {
  const fake = fakeHost(options), logs = [];
  const file = owedFile ?? join(mkdtempSync(join(tmpdir(), 'dsh-channels-owed-')), 'owed.json');
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]),
    ...(limits ? { limits: { ...CHANNEL_LIMITS, ...limits } } : {}), owedFile: file });
  if (ready) channels.ready();
  const slack = fakeChannel('slack'), wechat = fakeChannel('wechat');
  channels.service.register(slack.channel);
  channels.service.register(wechat.channel);
  return { ...fake, channels, logs, slack, wechat, owedFile: file, start: () => channels.ready(),
    inbound: message => channels.service.inbound(message) };
};
const only = (calls, kind) => calls.filter(([name]) => name === kind).map(([, value]) => value);
/** What the durable owed store on disk holds, by channel message: the file is the
 *  promise a restart inherits, so a test about durability reads IT, not the process. */
const owed = file => { try { return Object.values(JSON.parse(readFileSync(file, 'utf8')) ?? {}); }
  catch { return []; } };

test('register: only slack and wechat, with send and status; one plugin per channel; unregister takes it back', () => {
  const channels = createOwnerChannels({ host: fakeHost().host });
  assert.throws(() => channels.service.register({ name: 'email', send() {}, status() {} }), /name must be one of slack, wechat/);
  assert.throws(() => channels.service.register({ name: 'slack', send() {} }), /send and status must be functions/);
  const unregister = channels.service.register(fakeChannel('slack').channel);
  assert.throws(() => channels.service.register(fakeChannel('slack').channel), /slack is already registered/);
  unregister();
  assert.doesNotThrow(() => channels.service.register(fakeChannel('slack').channel));
});

test('nothing reaches the host before it is ready: a message handed in early waits, then goes through', async () => {
  const fake = fakeHost();
  const channels = createOwnerChannels({ host: fake.host });
  const early = channels.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-0', text: 'early' });
  const mirrored = channels.ownerMessageSent('m-0', 'early reply');
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(fake.calls, []);
  channels.ready();
  assert.deepEqual(await early, { accepted: true });
  assert.deepEqual(await mirrored, { mirrored: false, reason: 'no_default_channel' });
  assert.equal(only(fake.calls, 'relay').length, 1);
});

test('an ordinary message from the bound owner is relayed with its fields, and answered with the receipt only', async () => {
  const s = setup();
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: BODY, at: '2026-11-18T10:00:00Z' }), { accepted: true });
  // The channel's own time goes along (the host dates an approval phrase by it); none, none.
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-2', text: BODY }), { accepted: true });
  assert.deepEqual(only(s.calls, 'relay'), [{ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: BODY, at: '2026-11-18T10:00:00Z' },
    { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-2', text: BODY }]);
  // The first, dated days ago, is no longer news (receiptMaxAgeMs): the undated second gets the receipt.
  assert.deepEqual(s.slack.sent, [{ text: channelReplies.received, options: { replyToRef: 'ts-2', to: 'U-OWNER' } }]);
});

test('six digits from an unbound sender bind it, and that sender is told in one line that never echoes the code', async () => {
  const s = setup({ bindings: {} });
  assert.deepEqual(await s.inbound({ channel: 'wechat', senderRef: 'wxid-new', messageRef: 'w-1', text: ' 123456\n' }), { accepted: true });
  assert.deepEqual(only(s.calls, 'bind'), [{ channel: 'wechat', senderRef: 'wxid-new', code: '123456', messageRef: 'w-1' }]);
  assert.equal(only(s.calls, 'relay').length, 0, 'a binding attempt is not an owner message');
  assert.equal(s.wechat.sent.length, 1);
  assert.deepEqual(s.wechat.sent[0].options, { replyToRef: 'w-1', to: 'wxid-new' });
  assert.match(s.wechat.sent[0].text, /^Bound/);
  assert.ok(!s.wechat.sent[0].text.includes('123456'));
  // A wrong / expired / used code: refused, and the sender is told so - nothing more.
  s.host.bindResult = { refused: 'code_expired' };
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'ts-9', text: '654321' }),
    { accepted: false, reason: 'code_expired' });
  assert.equal(s.slack.sent.length, 1);
  assert.match(s.slack.sent[0].text, /^Not bound/);
  assert.ok(!s.slack.sent[0].text.includes('654321') && !s.slack.sent[0].text.includes('code_expired'));
});

test('a binding message delivered again (a channel replaying after a restart) is the host\'s duplicate: no second line, no owner message', async () => {
  // A fresh process: nothing here remembers the first delivery; only the host does.
  const s = setup({ bindings: {} });
  s.host.bindResult = { bound: false, channel: 'slack', sender_ref: 'U-STRANGER', duplicate: true, refusal: 'invalid_code' };
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'g-1', text: '111111' }),
    { accepted: false, duplicate: true, reason: 'invalid_code' });
  s.host.bindResult = { bound: true, channel: 'wechat', sender_ref: 'wx-old', duplicate: true };
  assert.deepEqual(await s.inbound({ channel: 'wechat', senderRef: 'wx-old', messageRef: 'w-1', text: '222222' }),
    { accepted: true, duplicate: true });
  assert.deepEqual(only(s.calls, 'bind').map(call => call.messageRef), ['g-1', 'w-1'], 'the host gets the channel message to know it by');
  assert.equal(s.slack.sent.length + s.wechat.sent.length, 0, 'a duplicate is not answered');
  assert.ok(s.logs.some(([event]) => event === 'channel_bind_duplicate'));
  // The owner's code replayed after it bound: the owner is bound now, so it is relayed, and the
  // host answers it as a duplicate (it never was an owner message) - nothing is said either.
  const o = setup();
  o.host.next = { message_id: null, duplicate: true, approval: null };
  assert.deepEqual(await o.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-code', text: '123456' }),
    { accepted: true, duplicate: true });
  assert.equal(only(o.calls, 'relay').length, 1);
  assert.equal(o.slack.sent.length, 0);
});

test('six digits from the bound owner are an ordinary message; anything but exactly six ASCII digits is too', async () => {
  const s = setup();
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: '123456' });
  for (const text of ['12345', '1234567', '１２３４５６', '123 456', 'abc123'])
    await s.inbound({ channel: 'slack', senderRef: 'U-OTHER', messageRef: 'ts-' + text, text });
  assert.equal(only(s.calls, 'bind').length, 0);
  assert.equal(only(s.calls, 'relay').length, 6);
});

test('refusals: a stranger is never answered; the bound owner is told about a refusal once a minute', async () => {
  const s = setup();
  for (const refused of ['channel_sender_not_bound', 'body_too_long', 'rate_limited']) {
    s.host.next = { refused };
    assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'x-' + refused, text: BODY }),
      { accepted: false, reason: refused });
  }
  assert.equal(s.slack.sent.length, 0, 'no oracle for strangers');
  // A terminal refusal the owner can act on: one line, with the message it answers.
  s.host.next = { refused: 'body_too_long' };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: 'x'.repeat(5000) });
  assert.equal(s.slack.sent.length, 1);
  assert.match(s.slack.sent[0].text, /too long/);
  assert.deepEqual(s.slack.sent[0].options, { replyToRef: 'o-1', to: 'U-OWNER' });
  // The throttle: the second terminal refusal within the minute gets no line of its own.
  s.host.next = { refused: 'body_too_long' };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-2', text: 'x'.repeat(5000) });
  assert.equal(s.slack.sent.length, 1, 'a second line within the minute is not sent');
  // A transient answer is held; its Held line shares the minute with the other refusal lines,
  // so within the minute it gets none (the Held line itself: the test after the next).
  s.host.next = { refused: 'rate_limited' };
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-3', text: 'hi' }),
    { accepted: false, reason: 'rate_limited' });
  assert.equal(s.slack.sent.length, 1, 'a second line within the minute is not sent');
  assert.ok(s.logs.some(([event, fields]) => event === 'channel_relay_retrying' && fields.reason === 'rate_limited'));
  // On a channel it is not bound to, U-OWNER is a stranger: silent.
  s.host.next = { refused: 'invalid_request' };
  await s.inbound({ channel: 'wechat', senderRef: 'U-OWNER', messageRef: 'o-4', text: 'hi' });
  assert.equal(s.wechat.sent.length, 0);
});

test('refusals: any other refusal of the bound owner\'s message (the credential guard, an empty body) is one "not delivered" line', async () => {
  for (const refused of ['invalid_request', 'empty_body', 'idempotency_conflict']) {
    const s = setup();
    s.host.next = { refused };
    assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: BODY }), { accepted: false, reason: refused });
    assert.deepEqual(s.slack.sent, [{ text: channelReplies.refused(refused), options: { replyToRef: 'o-1', to: 'U-OWNER' } }]);
    assert.match(s.slack.sent[0].text, /^Not delivered/);
    assert.ok(!s.slack.sent[0].text.includes('SECRET_BODY_TEXT'));
    s.host.next = { refused };
    await s.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'x-1', text: BODY });
    assert.equal(s.slack.sent.length, 1, 'a stranger is told nothing');
  }
});

test('an undated approval and a pasted binding code: the bound owner is told in their own line, a stranger nothing; no time is made up', async () => {
  for (const refused of ['approval_undated', 'code_sent_to_bound_account']) {
    const s = setup();
    s.host.next = { refused };
    assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: '同意 proposal p-1' }),
      { accepted: false, reason: refused });
    assert.deepEqual(s.slack.sent, [{ text: channelReplies[refused], options: { replyToRef: 'o-1', to: 'U-OWNER' } }]);
    s.host.next = { refused };
    await s.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'x-1', text: '同意 proposal p-1' });
    assert.equal(s.slack.sent.length, 1, 'a stranger is told nothing');
    assert.ok(!('at' in only(s.calls, 'relay')[0]), 'a message without a time is relayed without one');
  }
  assert.match(channelReplies.code_sent_to_bound_account, /binding code; it is now retired.*重新生成/);
  // The host may answer it as the approval's refusal instead (the message written either way).
  const s = setup();
  s.host.next = { message_id: 'm', duplicate: false, approval: { proposal_id: 'p-1', action: 'approve', outcome: 'refused', refusal: 'approval_undated' } };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-2', text: '同意 proposal p-1', at: '2026-11-19T02:00:00.000Z' });
  assert.match(s.slack.sent[0].text, /^proposal p-1: not done - the message carried no time/);
  assert.equal(only(s.calls, 'relay')[0].at, '2026-11-19T02:00:00.000Z', "the channel's time is passed on as given");
});

test('an approval the host executed is answered with one line of its outcome; a redelivered message is not answered again', async () => {
  const s = setup();
  const approval = (action, outcome, refusal) => ({ message_id: 'm', duplicate: false,
    approval: { proposal_id: 'p-1', action, outcome, ...(refusal ? { refusal } : {}) } });
  const lines = [];
  for (const result of [approval('approve', 'executed'), approval('decline', 'executed'), approval('approve', 'refused', 'needs_dashboard_confirmation'),
    approval('approve', 'refused', 'channel_sender_not_bound'), approval('approve', 'refused', 'approval_clock_skew')]) {
    s.host.next = result;
    await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-' + lines.length, text: '同意 proposal p-1' });
    lines.push(s.slack.sent.at(-1).text);
  }
  assert.match(lines[0], /proposal p-1: approved and done/);
  assert.match(lines[1], /proposal p-1: declined/);
  assert.match(lines[2], /dashboard/);
  assert.match(lines[3], /not done \(channel_sender_not_bound\)/);
  assert.match(lines[4], /not done \(approval_clock_skew\)/, 'a phrase dated ahead of the host clock is named, not hidden');
  s.host.next = { ...approval('approve', 'executed'), duplicate: true };
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-0', text: '同意 proposal p-1' }),
    { accepted: true, duplicate: true });
  assert.equal(s.slack.sent.length, 5);
  // Even a replay a host does not flag (duplicate: false) is answered only once.
  s.host.next = approval('approve', 'executed');
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-1', text: '同意 proposal p-1' }), { accepted: true });
  assert.equal(s.slack.sent.length, 5);
  // The same ref on another channel is another message.
  const w = setup({ bindings: { wechat: 'U-OWNER' } });
  w.host.next = approval('approve', 'executed');
  await w.inbound({ channel: 'wechat', senderRef: 'U-OWNER', messageRef: 'a-1', text: '同意 proposal p-1' });
  assert.equal(w.wechat.sent.length, 1);
});

test('a redelivery whose decision line never went out gets it, once: the first delivery had no answer, or the host decided only now', async () => {
  const executed = { proposal_id: 'p-1', action: 'approve', outcome: 'executed', kind: 'start_driver', target: 'wuxia' };
  const s = setup();
  // The host stopped between writing the message and recording the decision: no answer.
  s.host.relay = async message => { s.calls.push(['relay', message]); throw new Error('HTTP 502'); };
  const owner = { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-1', text: '同意 proposal p-1' };
  assert.deepEqual(await s.inbound(owner), { accepted: false, reason: 'unavailable' });
  assert.equal(s.slack.sent.length, 0);
  // The channel redelivers; the host runs the lost decision and answers it as a duplicate.
  s.host.relay = async message => { s.calls.push(['relay', message]); return { message_id: 'm', duplicate: true, approval: executed }; };
  assert.deepEqual(await s.inbound(owner), { accepted: true, duplicate: true });
  assert.equal(s.slack.sent.length, 1);
  assert.match(s.slack.sent[0].text, /proposal p-1: approved and done - start director of wuxia/);
  assert.deepEqual(s.slack.sent[0].options, { replyToRef: 'a-1', to: 'U-OWNER' });
  await s.inbound(owner);
  assert.equal(s.slack.sent.length, 1, 'its line went out: a further redelivery is not answered again');
  // After a restart this process remembers nothing: a duplicate whose line went out before
  // is not answered; one the host says it decided only now is.
  const r = setup();
  r.host.next = { message_id: 'm', duplicate: true, approval: executed };
  await r.inbound({ ...owner, messageRef: 'a-2' });
  assert.equal(r.slack.sent.length, 0, 'a genuine duplicate: its line went out the first time');
  r.host.next = { message_id: 'm', duplicate: true, decided_on_replay: true, approval: executed };
  await r.inbound({ ...owner, messageRef: 'a-3' });
  await r.inbound({ ...owner, messageRef: 'a-3' });
  assert.equal(r.slack.sent.length, 1, 'the decision the host ran on this redelivery is answered, once');
});

test('a redelivery the host answers while the first delivery still executes the approval is retryable and keeps the line owed', async () => {
  const executed = { proposal_id: 'p-1', action: 'approve', outcome: 'executed', kind: 'start_driver', target: 'wuxia' };
  const s = setup();
  // The host timed out while the approval's run went on.
  s.host.relay = async message => { s.calls.push(['relay', message]); throw new Error('HTTP 504'); };
  const owner = { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'e-1', text: '同意 proposal p-1' };
  assert.deepEqual(await s.inbound(owner), { accepted: false, reason: 'unavailable' });
  // A redelivery while it still runs: the host has no decision to answer yet, and says so -
  // retryable, never a finished answer (a terminal duplicate here lost the line for good).
  s.host.relay = async message => { s.calls.push(['relay', message]);
    return { message_id: 'm', duplicate: true, approval: null, approval_pending: true }; };
  assert.deepEqual(await s.inbound(owner), { accepted: false, reason: 'approval_pending' });
  assert.equal(s.slack.sent.length, 0);
  // Once the run has ended the host answers the decision as a plain duplicate: its line never went out.
  s.host.relay = async message => { s.calls.push(['relay', message]); return { message_id: 'm', duplicate: true, approval: executed }; };
  await s.inbound(owner);
  await s.inbound(owner);
  assert.equal(s.slack.sent.length, 1, 'the decision is answered once, on the first redelivery that carries it');
  assert.match(s.slack.sent[0].text, /proposal p-1: approved and done/);
});

test('the line a decision owes survives a restart: the owed message is kept in the store, asked again, and its line sent', async () => {
  const executed = { proposal_id: 'p-1', action: 'approve', outcome: 'executed', kind: 'start_driver', target: 'wuxia' };
  const owner = { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-1', text: '同意 proposal p-1', at: RECENT };
  // The first process asks; its call times out while the backend keeps deciding, so the
  // answer (with the decision) never comes back. The message is owed durably, not in memory.
  const first = setup();
  first.host.relay = async message => { first.calls.push(['relay', message]); throw new Error('The operation was aborted due to timeout'); };
  assert.deepEqual(await first.inbound(owner), { accepted: false, reason: 'unavailable' });
  assert.equal(first.slack.sent.length, 0);
  first.channels.stop();  // the process stops (a restart, a container recreate)
  // A fresh process over the same owed store: the host now holds the decision as a plain
  // duplicate, and the line goes out because this process knows it is owed.
  const second = setup(undefined, undefined, { owedFile: first.owedFile, ready: false });
  second.host.next = { message_id: 'm', duplicate: true, approval: executed };
  second.start();
  await second.channels.pollOwed();
  assert.equal(second.slack.sent.length, 1, 'the decision the stop left owed is reported');
  assert.match(second.slack.sent[0].text, /proposal p-1: approved and done - start director of wuxia/);
  assert.deepEqual(second.slack.sent[0].options, { replyToRef: 'a-1', to: 'U-OWNER' });
  assert.deepEqual(only(second.calls, 'relay').map(m => m.messageRef), ['a-1'], 'asked under the same ref');
  assert.deepEqual(only(second.calls, 'relay')[0].at, RECENT, 'with the time the owner wrote it');
  second.channels.stop();
  // Sent once: the store was cleared, so the next start has nothing to claim.
  const third = setup(undefined, undefined, { owedFile: first.owedFile, ready: false });
  third.host.next = { message_id: 'm', duplicate: true, approval: executed };
  third.start();
  await third.channels.pollOwed();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(third.slack.sent.length, 0);
  assert.deepEqual(only(third.calls, 'relay'), [], 'nothing owed, nothing asked');
  third.channels.stop();
});

test('a line whose send failed stays owed and goes out on the next delivery, never counted as sent', async () => {
  const executed = { proposal_id: 'p-1', action: 'approve', outcome: 'executed', kind: 'start_driver', target: 'wuxia' };
  const fake = fakeHost({ bindings: { slack: 'U-OWNER' } }), logs = [];
  const file = join(mkdtempSync(join(tmpdir(), 'dsh-channels-owed-')), 'owed.json');
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]),
    limits: { ...CHANNEL_LIMITS, answerRetryMs: 1 }, owedFile: file });
  channels.ready();
  const sent = [];
  let broken = true;
  channels.service.register({ name: 'slack', status: () => ({ connected: true }),
    async send(text) { if (broken) throw new Error('socket closed'); sent.push(text); return { ref: 'r-' + sent.length }; } });
  const owner = { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-1', text: '同意 proposal p-1' };
  fake.host.next = { message_id: 'm', duplicate: false, approval: executed };
  assert.deepEqual(await channels.service.inbound(owner), { accepted: true });
  assert.equal(sent.length, 0);
  assert.equal(logs.filter(([event]) => event === 'channel_answer_failed').length, CHANNEL_LIMITS.answerAttempts,
    'the send was tried its bounded number of times');
  // Owed is a durable fact, not this process's memory: the store the restart reads says so.
  assert.deepEqual(owed(file).map(message => message.messageRef), ['a-1'], 'the unsent line is on disk');
  // The send works now: the next delivery of that message sends the line that never went out.
  broken = false;
  await channels.service.inbound(owner);
  assert.equal(sent.length, 1);
  assert.match(sent[0], /proposal p-1: approved and done/);
  assert.deepEqual(owed(file), [], 'and nothing is owed once the line went out');
  await channels.service.inbound(owner);
  assert.equal(sent.length, 1, 'and it is not sent twice');
  channels.stop();
});

test('a message is owed on disk BEFORE the host is asked, so a stop while the relay is in flight still sends its line', async () => {
  // The id carries six digits in a row - real ids are 20 hex characters and about 38% of them
  // do, so this is the ordinary shape of an approval, not a corner.
  const executed = { proposal_id: 'prop-234567abcdef01234567', action: 'approve', outcome: 'executed',
    kind: 'start_driver', target: 'wuxia' };
  const first = setup(undefined, { answerRetryMs: 1 });
  // The relay never answers: this process is stopped while the decision runs inside it.
  first.host.relay = () => new Promise(() => {});
  const inFlight = first.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-9',
    text: '\u540c\u610f proposal prop-234567abcdef01234567', at: RECENT });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(owed(first.owedFile).map(message => message.messageRef), ['a-9'],
    'written ahead of the host answer, not after it');
  assert.equal(owed(first.owedFile)[0].text, '\u540c\u610f proposal prop-234567abcdef01234567',
    'and the host allowed this text to be kept, digits in a row or not');
  first.channels.stop();
  void inFlight;
  // The restart: the decision executed before the stop, the host answers it as a duplicate,
  // and the line the stop never sent goes out from the store.
  const second = setup(undefined, undefined, { owedFile: first.owedFile, ready: false });
  second.host.next = { message_id: 'm', duplicate: true, approval: executed };
  second.start();
  await second.channels.pollOwed();
  assert.equal(second.slack.sent.length, 1, 'the line the stop left owed is sent');
  assert.match(second.slack.sent[0].text, /proposal prop-234567abcdef01234567: approved and done - start director of wuxia/);
  assert.deepEqual(second.slack.sent[0].options, { replyToRef: 'a-9', to: 'U-OWNER' });
  assert.deepEqual(owed(first.owedFile), [], 'and the store is empty once the line went out');
  second.channels.stop();
});

test('a text the host will not keep is owed without it, and its sender is told to send it again', async () => {
  const first = setup(undefined, { answerRetryMs: 1 });
  // The host's answer for a text carrying a live binding code: the message is relayed (the
  // host refuses and retires the code), but the plugin keeps no copy of the words.
  first.host.storeText = false;
  first.host.next = { message_id: null, duplicate: false, refused: 'code_sent_to_bound_account' };
  assert.deepEqual(await first.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'b-1',
    text: 'binding code: \u2077\u00b9\u00b9\u2078\u00b3\u00b3 please bind' }), { accepted: false, reason: 'code_sent_to_bound_account' });
  assert.deepEqual(owed(first.owedFile), [], 'a refused body is neither durable nor replayed');
  // And the case the store exists for: the relay never answers, the host never allowed the
  // text, so the message is owed without it and a restart cannot replay a body it never had.
  first.host.storeText = false;
  first.host.relay = () => new Promise(() => {});
  const inFlight = first.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'b-2', text: 'hello' });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(owed(first.owedFile).map(message => message.messageRef), ['b-2'], 'still owed');
  assert.equal(owed(first.owedFile)[0].text, undefined, 'but not its text');
  first.channels.stop();
  void inFlight;
  const second = setup(undefined, undefined, { owedFile: first.owedFile, ready: false });
  second.start();
  await second.channels.pollOwed();
  assert.equal(second.slack.sent.length, 1, 'the owner learns it was not delivered');
  assert.match(second.slack.sent[0].text, /it cannot be retried: please send it again/);
  assert.deepEqual(second.slack.sent[0].options, { replyToRef: 'b-2', to: 'U-OWNER' });
  assert.deepEqual(owed(first.owedFile), [], 'and nothing is owed once it was told');
  second.channels.stop();
});

test('a "send it again" line that could not be sent keeps the record owed: the first drain runs before a channel registers', async () => {
  // The normal boot order this is about: ready() drains at the host's start, a channel plugin
  // registers later (Slack's socket-mode connect, the WeChat bridge's probe). The record here
  // is a stop's leftover without a body - the host never judged that text, so no line can be
  // replayed and the owner has to be told to send it again.
  const file = join(mkdtempSync(join(tmpdir(), 'dsh-channels-owed-')), 'owed.json');
  writeFileSync(file, JSON.stringify({ 'slack\nU-OWNER\nb-1': { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'b-1',
    owedAt: new Date().toISOString() } }), { mode: 0o600 });
  const fake = fakeHost(), logs = [];
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]),
    limits: { ...CHANNEL_LIMITS, answerRetryMs: 1 }, owedFile: file });
  channels.ready();
  await channels.pollOwed();
  assert.deepEqual(owed(file).map(message => message.messageRef), ['b-1'],
    'no channel is registered, so nothing was sent and the record is still owed');
  assert.deepEqual(logs.filter(([event]) => event === 'channel_answer_failed'), [], 'no line was reported sent');
  assert.ok(logs.some(([event, fields]) => event === 'channel_answer_skipped' && fields.reason === 'not_registered'));
  assert.ok(logs.some(([event, fields]) => event === 'channel_owed_held' && fields.reason === 'not_sent'),
    'the drain says so instead of forgetting it');
  // The channel registers, but its send fails: a failed notice is no notice either.
  const sent = [];
  let broken = true;
  channels.service.register({ name: 'slack', status: () => ({ connected: true }),
    async send(text, options) { if (broken) throw new Error('socket closed'); sent.push({ text, options }); return { ref: 'r-' + sent.length }; } });
  await channels.pollOwed();
  assert.equal(sent.length, 0);
  assert.equal(logs.filter(([event]) => event === 'channel_answer_failed').length, CHANNEL_LIMITS.answerAttempts,
    'the send was tried its bounded number of times and reported unsent');
  assert.deepEqual(owed(file).map(message => message.messageRef), ['b-1'], 'the record is still owed, not lost');
  // The send works now: the notice goes out once, and only then does the record leave the store.
  broken = false;
  await channels.pollOwed();
  assert.deepEqual(sent.map(line => line.text), [channelReplies.unknowable], 'the owner is told, in one line');
  assert.deepEqual(sent[0].options, { replyToRef: 'b-1', to: 'U-OWNER' });
  assert.deepEqual(owed(file), [], 'and the record is gone once the notice went out');
  await channels.pollOwed();
  assert.equal(sent.length, 1, 'and it is not sent twice');
  channels.stop();
});

test('nothing is written down before the host has allowed it, whatever digits the text carries', async () => {
  const s = setup();
  // The host cannot answer at all: no permission, and no relay either.
  s.host.codeCheckFails = true;
  s.host.bind = async () => { throw new Error('HTTP 502'); };
  s.host.relay = async () => { throw new Error('HTTP 502'); };
  // Which of these carries the live binding code is the HOST's question, and it folds every
  // rendering (core/state_channels.py ascii_digits: NFKC, arabic-indic, superscript, circled).
  // No body is stored before that answer, so a wrong guess - in either direction - costs
  // nothing: it cannot lose a line, and it cannot put a code on disk.
  const bodies = [['b-1', '711833'], ['b-2', ' 711833 '], ['b-3', 'binding code: 711833 - please bind my new laptop'],
    ['b-4', '\u0667\u0661\u0661\u0668\u0663\u0663'], ['b-5', '\uff17\uff11\uff11\uff18\uff13\uff13'],
    ['b-6', '\u2077\u00b9\u00b9\u2078\u00b3\u00b3'], ['b-7', '\u2790\u2791\u2792\u278a\u278b\u278c'],
    ['a-1', '\u540c\u610f proposal p-1']];
  for (const [ref, body] of bodies) {
    assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: ref, text: body }),
      { accepted: false, reason: 'unavailable' }, `${ref} fails without an answer`);
  }
  const records = owed(s.owedFile);
  assert.deepEqual(records.map(message => message.messageRef), bodies.map(([ref]) => ref), 'every one of them is owed');
  assert.deepEqual(records.filter(message => message.text !== undefined), [], 'and none of their texts is on disk');
});

test('the plugin-facing service is exactly {register, inbound}; the director-side handle carries the polls', () => {
  const channels = createOwnerChannels({ host: fakeHost().host });
  // host.d.ts: `OwnerChannels` is what a plugin injects (register/inbound only);
  // ready/pollStatus/pollOwed/stop live on the OwnerChannelsHandle createOwnerChannels returns.
  assert.deepEqual(Object.keys(channels.service).sort(), ['inbound', 'register']);
  assert.equal(typeof channels.ownerMessageSent, 'function');
  assert.equal(typeof channels.ready, 'function');
  assert.equal(typeof channels.pollStatus, 'function');
  assert.equal(typeof channels.pollOwed, 'function');
  assert.equal(typeof channels.stop, 'function');
  assert.equal(typeof channels.login, 'function', 'a new login is the director\'s to ask, not a plugin\'s');
});

test('a rate_limited answer is kept and asked again - by the channel and, after a restart, from the owed store', async () => {
  const message = { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'm-7', text: 'written during the outage',
    at: '2026-11-19T02:00:00.000Z' };
  const first = setup();
  // The backend's minute is spent: the seventh message of a replay burst is refused.
  first.host.relay = async call => { first.calls.push(['relay', call]); return { refused: 'rate_limited' }; };
  assert.deepEqual(await first.inbound(message), { accepted: false, reason: 'rate_limited' });
  assert.deepEqual(first.slack.sent.map(line => line.text), [channelReplies.rate_limited],
    'the owner is told once that it is held, not that it failed');
  assert.ok(first.logs.some(([event, fields]) => event === 'channel_relay_retrying' && fields.reason === 'rate_limited'));
  first.channels.stop();
  // The restart: the message the minute refused is asked again, and taken.
  const second = setup(undefined, undefined, { owedFile: first.owedFile, ready: false });
  second.host.relay = async call => { second.calls.push(['relay', call]); return { message_id: 'm', duplicate: false, approval: null }; };
  second.start();
  await second.channels.pollOwed();
  assert.deepEqual(only(second.calls, 'relay').map(m => m.messageRef), ['m-7'], 'the refused message is not lost');
  second.channels.stop();
  const third = setup(undefined, undefined, { owedFile: first.owedFile, ready: false });
  third.start();
  await third.channels.pollOwed();
  assert.deepEqual(only(third.calls, 'relay'), [], 'taken once: nothing is owed any more');
  third.channels.stop();
});

test("an executed decision's line names the host's kind and target in both languages; an unknown kind reads as itself; a long target is cut", () => {
  const line = (action, kind, target) => channelReplies.approval({ proposal_id: 'prop-x', action, outcome: 'executed', kind, target });
  assert.equal(line('approve', 'stop_driver', 'alpha'),
    'proposal prop-x: approved and done - stop director of alpha. 已同意并执行：停止 alpha 项目的导演。');
  assert.equal(line('decline', 'stop_driver', 'alpha'), 'proposal prop-x: declined - stop director of alpha. 已拒绝：停止 alpha 项目的导演。');
  const each = { create_project: ['create project alpha', '创建 alpha 项目'], start_driver: ['start director of alpha', '启动 alpha 项目的导演'],
    stop_driver: ['stop director of alpha', '停止 alpha 项目的导演'], archive_project: ['archive project alpha', '归档 alpha 项目'],
    unarchive_project: ['unarchive project alpha', '取消归档 alpha 项目'] };
  assert.deepEqual(Object.keys(each).sort(), [...PROPOSAL_KINDS].sort(), 'every proposal kind has its phrase');
  for (const [kind, [en, zh]] of Object.entries(each)) {
    assert.equal(line('approve', kind, 'alpha'), `proposal prop-x: approved and done - ${en}. 已同意并执行：${zh}。`);
    assert.equal(line('decline', kind, 'alpha'), `proposal prop-x: declined - ${en}. 已拒绝：${zh}。`);
  }
  assert.equal(line('approve', 'rename_project', 'alpha'), 'proposal prop-x: approved and done - rename_project alpha. 已同意并执行：rename_project alpha。');
  const long = line('approve', 'archive_project', 'a'.repeat(5000));
  assert.ok(long.length < 200 && long.includes('a'.repeat(59) + '…') && !long.includes('a'.repeat(60)), long);
  assert.ok(line('approve', 'k'.repeat(5000), 'alpha').length < 200, 'an unknown long kind is cut too');
  // No kind from the host: the plain line as before; a refusal line is unchanged by a kind.
  assert.equal(line('approve'), 'proposal prop-x: approved and done. 已同意并执行。');
  assert.equal(channelReplies.approval({ proposal_id: 'prop-x', action: 'approve', outcome: 'refused', refusal: 'proposal_not_open',
    kind: 'stop_driver', target: 'alpha' }), 'proposal prop-x: not done (proposal_not_open). 未执行（proposal_not_open）。');
});

test('the decided line reaches the bound sender with the kind and target the relay answered', async () => {
  const s = setup();
  s.host.next = { message_id: 'm', duplicate: false,
    approval: { proposal_id: 'p-1', action: 'approve', outcome: 'executed', kind: 'start_driver', target: 'alpha', result: {} } };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'k-1', text: '同意 proposal p-1' });
  assert.equal(s.slack.sent.at(-1).text, 'proposal p-1: approved and done - start director of alpha. 已同意并执行：启动 alpha 项目的导演。');
});

test('an approval whose run failed is answered as failed, never as done', () => {
  const line = (kind, target, result) => channelReplies.approval({ proposal_id: 'p-1', action: 'approve', outcome: 'executed', kind, target, result });
  assert.equal(line('start_driver', 'alpha', { status: 'failed', result: { error: 'RuntimeError: boom' } }),
    'proposal p-1: approved, but it failed - start director of alpha. 已同意，但执行失败：启动 alpha 项目的导演。');
  assert.equal(line('create_project', 'alpha', { status: 'failed', result: { error: 'project_id alpha 已被占用' } }),
    'proposal p-1: approved, but it failed - create project alpha. 已同意，但执行失败：创建 alpha 项目。');
  assert.equal(line(undefined, undefined, { status: 'failed' }), 'proposal p-1: approved, but it failed. 已同意，但执行失败。');
  assert.equal(line('start_driver', 'alpha', { status: 'done' }), 'proposal p-1: approved and done - start director of alpha. 已同意并执行：启动 alpha 项目的导演。');
});

test('binding attempts are bounded per channel and minute: past the bound, six digits are relayed like any message, and nobody is answered', async () => {
  const s = setup({ bindings: {} });
  s.host.bindResult = { refused: 'invalid_code' };
  // The real host refuses a sender that is not bound (host.d.ts relay).
  s.host.next = { refused: 'channel_sender_not_bound' };
  for (let i = 0; i < CHANNEL_LIMITS.bindPerMinute + 3; i++)
    await s.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'b-' + i, text: String(100000 + i) });
  assert.equal(only(s.calls, 'bind').length, CHANNEL_LIMITS.bindPerMinute);
  assert.equal(only(s.calls, 'list').length, CHANNEL_LIMITS.bindPerMinute, 'no binding read past the bound');
  assert.equal(s.slack.sent.length, CHANNEL_LIMITS.bindPerMinute, 'no answer past the bound');
  assert.equal(only(s.calls, 'relay').length, 3);
  assert.equal(s.logs.filter(([event]) => event === 'channel_bind_throttled').length, 3);
  // Another channel has its own bound.
  await s.inbound({ channel: 'wechat', senderRef: 'wx', messageRef: 'w-1', text: '222222' });
  assert.equal(only(s.calls, 'bind').length, CHANNEL_LIMITS.bindPerMinute + 1);
});

test('a channel\'s messages reach the host one at a time, in the order they were handed in', async () => {
  const s = setup();
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const relay = s.host.relay;
  s.host.relay = async message => { if (message.messageRef === 'first') await gate; return relay(message); };
  const first = s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'first', text: 'one' });
  const second = s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'second', text: 'two' });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(only(s.calls, 'relay'), [], 'the second did not overtake the first');
  release();
  assert.deepEqual(await Promise.all([first, second]), [{ accepted: true }, { accepted: true }]);
  assert.deepEqual(only(s.calls, 'relay').map(m => m.messageRef), ['first', 'second']);
});

test('a host that never becomes ready: a message is refused as unavailable after the bound, not held forever', async () => {
  const fake = fakeHost();
  const channels = createOwnerChannels({ host: fake.host, limits: { ...CHANNEL_LIMITS, readyMs: 30 } });
  assert.deepEqual(await channels.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: 'hi' }),
    { accepted: false, reason: 'unavailable' });
  assert.deepEqual(await channels.ownerMessageSent('m-1', 'x'), { mirrored: false, reason: 'unavailable' });
  assert.deepEqual(fake.calls, []);
});

test('no log line carries a message body; a malformed message or a failing host is refused, never thrown', async () => {
  const s = setup();
  s.host.next = { refused: 'channel_sender_not_bound' };
  await s.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'l-1', text: BODY });
  assert.deepEqual(await s.inbound({ channel: 'irc', senderRef: 'x', messageRef: 'y', text: BODY }), { accepted: false, reason: 'invalid_inbound' });
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: '', messageRef: 'y', text: BODY }), { accepted: false, reason: 'invalid_inbound' });
  s.host.relay = async () => { throw new Error('backend down'); };
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'l-2', text: BODY }), { accepted: false, reason: 'unavailable' });
  const logged = JSON.stringify(s.logs);
  assert.ok(!logged.includes('SECRET_BODY_TEXT'), logged);
  const refusal = s.logs.find(([event]) => event === 'channel_relay_refused')[1];
  assert.deepEqual(Object.keys(refusal).sort(), ['at', 'channel', 'length', 'reason', 'sender_ref']);
  assert.equal(refusal.length, BODY.length);
});

test('a reply to a channel message goes back on that channel, to that message, exactly the text, once', async () => {
  const s = setup();
  s.sources.set('m-owner', { direction: 'in', channel: 'slack', sender_ref: 'U-OWNER', channel_message_ref: 'ts-1', at: 'a', error: null });
  const text = 'The answer: build 42 passed.\nNext: nothing.';
  assert.deepEqual(await s.channels.ownerMessageSent('m-reply', text, 'm-owner'), { mirrored: true, channel: 'slack' });
  assert.deepEqual(s.slack.sent, [{ text, options: { replyToRef: 'ts-1', to: 'U-OWNER' } }]);
  assert.deepEqual(only(s.calls, 'recordDelivery'), [{ messageId: 'm-reply', channel: 'slack', channelMessageRef: 'slack-ref-1' }]);
  // The host may report the same message again (an idempotent replay): it is not sent twice.
  assert.deepEqual(await s.channels.ownerMessageSent('m-reply', text, 'm-owner'), { mirrored: false, reason: 'already_mirrored' });
  assert.equal(s.slack.sent.length, 1);
  assert.equal(s.wechat.sent.length, 0);
});

// The shape of the steward's WeChat reply of 2026-11-20 13:01Z: 1604 characters in 17 lines,
// bold section titles - more than a phone chat shows well (owner, 2026-11-20).
const LONG_REPLY = ['两份都把你昨晚那两张卡答了，两位导演都已接走。下面是昨天到今天的变化。', '',
  '**平台——两天的活几乎全是「把看不见的门修成看得见的」**',
  ...Array.from({ length: 6 }, (_, i) => `- 第 ${i + 1} 条：${'这一条讲的是一处修好的缺陷和它的见证，'.repeat(9)}已修。`), '',
  '**武虾传奇——昨天到今天发了五六刀**',
  ...Array.from({ length: 4 }, (_, i) => `- 第 ${i + 1} 刀：${'玩家能看到的一处变化，'.repeat(8)}已上线。`), '',
  '两张卡都清了，眼下没有等你裁决的事。'].join('\n');

test('a long steward message reaches the channel cut at a line end, ending in MORE; the host and a replay see the whole text', async () => {
  assert.ok(LONG_REPLY.length > 1500 && LONG_REPLY.length > CHANNEL_LIMITS.mirrorChars, `${LONG_REPLY.length}`);
  assert.equal(CHANNEL_LIMITS.mirrorChars, 600);
  const s = setup();
  s.sources.set('m-owner', { direction: 'in', channel: 'slack', sender_ref: 'U-OWNER', channel_message_ref: 'ts-1', at: 'a', error: null });
  assert.deepEqual(await s.channels.ownerMessageSent('m-reply', LONG_REPLY, 'm-owner'), { mirrored: true, channel: 'slack' });
  const [{ text: sent }] = s.slack.sent;
  assert.ok(Array.from(sent).length <= CHANNEL_LIMITS.mirrorChars, `sent ${Array.from(sent).length} characters`);
  assert.ok(sent.endsWith(MORE), sent.slice(-40));
  const kept = sent.slice(0, -MORE.length);
  // Cut at a line end: what is kept is whole lines of the (bold-flattened) original, its first line first.
  assert.ok(LONG_REPLY.replace(/\*\*/g, '').startsWith(kept + '\n'), 'cut at the end of a line');
  assert.ok(kept.length > CHANNEL_LIMITS.mirrorChars / 2, 'the cut keeps most of what fits');
  // The cut is logged with both lengths, never the body.
  const [cutLog] = s.logs.filter(([event]) => event === 'channel_text_cut').map(([, fields]) => fields);
  assert.deepEqual(cutLog, { channel: 'slack', message_id: 'm-reply', chars: Array.from(LONG_REPLY).length, sent_chars: Array.from(sent).length });
  // Delivered once, recorded once; a replay of the same message sends nothing more.
  assert.equal(only(s.calls, 'recordDelivery').length, 1);
  assert.deepEqual(await s.channels.ownerMessageSent('m-reply', LONG_REPLY, 'm-owner'), { mirrored: false, reason: 'already_mirrored' });
  assert.equal(s.slack.sent.length, 1);
});

test('channelText: a short text goes as written; a blank line beats a line end, so a title is never the last line kept', () => {
  assert.equal(channelText('slack', 'The answer: build 42 passed.\nNext: nothing.'), 'The answer: build 42 passed.\nNext: nothing.');
  assert.equal(channelText('wechat', '答案：已经好了。\n明天再看。'), '答案：已经好了。\n明天再看。');
  const sections = ['结论：都好了。', '', '平台', '- ' + '一处修好的缺陷，'.repeat(5), '', '武虾传奇', '- ' + '一处上线的变化，'.repeat(5)].join('\n');
  const cut = channelText('wechat', sections, 80);
  assert.equal(cut, ['结论：都好了。', '', '平台', '- ' + '一处修好的缺陷，'.repeat(5)].join('\n') + MORE);
});

test('channelText: a long line is cut after a sentence end, never inside a number, version, reference or URL', () => {
  const paragraph = '这是一句完整的话。'.repeat(200);
  const cut = channelText('slack', paragraph, 100);
  assert.ok(cut.endsWith('。' + MORE), cut);
  assert.ok(Array.from(cut).length <= 100);
  assert.ok(paragraph.startsWith(cut.slice(0, -MORE.length)));
  // A dot inside 13.6x or v1.2.3 ends no sentence.
  const numbers = '提速' + '甲'.repeat(60) + '到 13.6x 版本 v1.2.3 已发布' + '乙'.repeat(60);
  const kept = channelText('slack', numbers, 80).slice(0, -MORE.length);
  assert.ok(!/\d\.$/.test(kept) && numbers.startsWith(kept), kept);
  // A hard cut steps back before the word it would split: never 见 Q1 for 见 Q12, never half a URL.
  assert.equal(channelText('slack', '甲'.repeat(86) + '见 Q12 的回答' + '乙'.repeat(40), 99), '甲'.repeat(86) + '见' + MORE);
  assert.equal(channelText('wechat', '甲'.repeat(60) + '详见 https://docs.example.com/a/b?c=1 乙' + '乙'.repeat(40), 80), '甲'.repeat(60) + '详见' + MORE);
  // No boundary at all: cut at the bound, by code point (a surrogate pair stays whole).
  const hard = channelText('slack', '\u{1F600}'.repeat(300), 100);
  assert.equal(Array.from(hard).length, 100);
  assert.ok(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(hard), 'no lone high surrogate');
  // A bound too small to hold MORE falls back to the default.
  assert.equal(channelText('slack', paragraph, 5), channelText('slack', paragraph));
});

test('channelText: an approval phrase line is never cut and loses only its marks; a binding code text goes as written', () => {
  const approval = `${LONG_REPLY}\n\n\`同意 proposal prop-123\`\n**拒绝 proposal prop-123**\n结尾一句。`;
  for (const channel of CHANNEL_NAMES) {
    const sent = channelText(channel, approval);
    assert.ok(Array.from(sent).length <= CHANNEL_LIMITS.mirrorChars, `${channel} ${Array.from(sent).length}`);
    assert.ok(sent.endsWith('\n同意 proposal prop-123\n拒绝 proposal prop-123' + MORE), sent.slice(-80));
  }
  const short = '要启动吗？\n`同意 proposal prop-9`\n拒绝 proposal prop-9';
  for (const channel of CHANNEL_NAMES) assert.equal(channelText(channel, short), '要启动吗？\n同意 proposal prop-9\n拒绝 proposal prop-9');
  const code = `${LONG_REPLY}\n482913\n`;
  for (const channel of CHANNEL_NAMES) assert.equal(channelText(channel, code), code);
});

test('channelText: headings, bold and tables are flattened on both channels; only WeChat loses code marks', () => {
  const md = '## 进展\n**武虾**：`main` 已合入\n| 项目 | 状态 |\n|---|:---:|\n| 武虾 | 已上线 |';
  assert.equal(channelText('wechat', md), '进展\n武虾：main 已合入\n项目 · 状态\n武虾 · 已上线');
  assert.equal(channelText('slack', md), '进展\n武虾：`main` 已合入\n项目 · 状态\n武虾 · 已上线');
});

test('a reply to a dashboard message stays on the dashboard; an unprompted one goes to the default channel only when set and bound', async () => {
  const s = setup();
  assert.deepEqual(await s.channels.ownerMessageSent('m-1', 'hello', 'm-dashboard'), { mirrored: false, reason: 'dashboard' });
  assert.deepEqual(await s.channels.ownerMessageSent('m-2', 'hello'), { mirrored: false, reason: 'no_default_channel' });
  assert.equal(only(s.calls, 'recordDelivery').length, 0);
  const d = setup({ bindings: { wechat: 'wxid-owner' }, defaultChannel: 'wechat' });
  assert.deepEqual(await d.channels.ownerMessageSent('m-3', 'A reminder came due.'), { mirrored: true, channel: 'wechat' });
  assert.deepEqual(d.wechat.sent, [{ text: 'A reminder came due.', options: { to: 'wxid-owner' } }]);
  const unbound = setup({ bindings: {}, defaultChannel: 'slack' });
  assert.deepEqual(await unbound.channels.ownerMessageSent('m-4', 'x'), { mirrored: false, reason: 'not_bound', channel: 'slack' });
  assert.equal(unbound.slack.sent.length, 0);
});

test('a sender unbound since it wrote gets no reply; another account bound since gets none either', async () => {
  const s = setup({ bindings: { slack: 'U-NEW' } });
  s.sources.set('m-owner', { direction: 'in', channel: 'slack', sender_ref: 'U-OLD', channel_message_ref: 'ts-1', at: 'a', error: null });
  assert.deepEqual(await s.channels.ownerMessageSent('m-reply', 'answer', 'm-owner'), { mirrored: false, reason: 'not_bound', channel: 'slack' });
  assert.equal(s.slack.sent.length, 0);
});

test('a failed send is recorded once with its error and never retried; a channel with no plugin is recorded as not connected', async () => {
  const fake = fakeHost({ bindings: { slack: 'U-OWNER', wechat: 'wx' } });
  const channels = createOwnerChannels({ host: fake.host, log: () => {} });
  channels.ready();
  const slack = fakeChannel('slack', { fail: 'socket closed' });
  channels.service.register(slack.channel);
  fake.sources.set('in-1', { direction: 'in', channel: 'slack', sender_ref: 'U-OWNER', channel_message_ref: 'ts-1', at: 'a', error: null });
  fake.sources.set('in-2', { direction: 'in', channel: 'wechat', sender_ref: 'wx', channel_message_ref: 'w-1', at: 'a', error: null });
  assert.deepEqual(await channels.ownerMessageSent('out-1', 'answer', 'in-1'), { mirrored: false, reason: 'send_failed', channel: 'slack' });
  assert.deepEqual(await channels.ownerMessageSent('out-1', 'answer', 'in-1'), { mirrored: false, reason: 'already_mirrored' });
  assert.equal(slack.sent.length, 1);
  assert.deepEqual(await channels.ownerMessageSent('out-2', 'answer', 'in-2'), { mirrored: false, reason: 'send_failed', channel: 'wechat' });
  assert.deepEqual(only(fake.calls, 'recordDelivery'), [{ messageId: 'out-1', channel: 'slack', error: 'socket closed' },
    { messageId: 'out-2', channel: 'wechat', error: 'channel_not_connected' }]);
});

test('once per message across a restart: a fresh process finds the delivery row and sends nothing', async () => {
  const fake = fakeHost();
  fake.sources.set('m-in', { direction: 'in', channel: 'slack', sender_ref: 'U-OWNER', channel_message_ref: 'ts-1', at: 'a', error: null });
  fake.sources.set('m-out', { direction: 'out', channel: 'slack', sender_ref: null, channel_message_ref: 'slack-ref-1', at: 'a', error: null });
  const channels = createOwnerChannels({ host: fake.host });
  channels.ready();
  const slack = fakeChannel('slack');
  channels.service.register(slack.channel);
  assert.deepEqual(await channels.ownerMessageSent('m-out', 'answer', 'm-in'), { mirrored: false, reason: 'already_mirrored' });
  assert.equal(slack.sent.length, 0);
  assert.equal(only(fake.calls, 'recordDelivery').length, 0);
});

test('once per message within a process: a delivery that could not be recorded is still not sent again', async () => {
  const fake = fakeHost(), logs = [];
  fake.sources.set('m-in', { direction: 'in', channel: 'slack', sender_ref: 'U-OWNER', channel_message_ref: 'ts-1', at: 'a', error: null });
  fake.host.recordDelivery = async delivery => { fake.calls.push(['recordDelivery', delivery]); throw new Error('backend down'); };
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]),
    limits: { ...CHANNEL_LIMITS, recordRetryMs: 1 } });
  channels.ready();
  const slack = fakeChannel('slack');
  channels.service.register(slack.channel);
  assert.deepEqual(await channels.ownerMessageSent('m-out', 'answer', 'm-in'), { mirrored: true, channel: 'slack' });
  assert.deepEqual(await channels.ownerMessageSent('m-out', 'answer', 'm-in'), { mirrored: false, reason: 'already_mirrored' });
  assert.equal(slack.sent.length, 1);
  assert.equal(only(fake.calls, 'recordDelivery').length, CHANNEL_LIMITS.recordAttempts, 'the record is tried a bounded number of times');
  assert.equal(logs.filter(([event]) => event === 'channel_record_retry').length, CHANNEL_LIMITS.recordAttempts - 1);
  assert.deepEqual(logs.filter(([event]) => event === 'channel_record_failed').map(([, f]) => f.message_id), ['m-out']);
  // A delivery the host refuses to record is logged by its code.
  fake.host.recordDelivery = async () => ({ refused: 'delivery_recorded' });
  await channels.ownerMessageSent('m-out-2', 'answer', 'm-in');
  assert.deepEqual(logs.filter(([event]) => event === 'channel_record_refused').map(([, f]) => [f.message_id, f.reason]), [['m-out-2', 'delivery_recorded']]);
});

test('a record that fails after a successful send is tried again, so a restart replaying the same message sends nothing', async () => {
  const fake = fakeHost({ defaultChannel: 'slack' }), logs = [];
  const recordOnce = fake.host.recordDelivery;
  let failures = 2;
  // The backend is briefly down (a 502, a restart) when the delivery is recorded.
  fake.host.recordDelivery = async delivery => {
    if (failures-- > 0) { fake.calls.push(['recordDelivery', delivery]); throw new Error('HTTP 502'); }
    return recordOnce(delivery);
  };
  const limits = { ...CHANNEL_LIMITS, recordRetryMs: 1 };
  const first = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]), limits });
  first.ready();
  const slack = fakeChannel('slack');
  first.service.register(slack.channel);
  assert.deepEqual(await first.ownerMessageSent('m-out', 'Your build finished'), { mirrored: true, channel: 'slack' });
  assert.deepEqual(only(fake.calls, 'recordDelivery'), Array(3).fill({ messageId: 'm-out', channel: 'slack', channelMessageRef: 'slack-ref-1' }),
    'the same delivery each time');
  assert.equal(logs.filter(([event]) => event === 'channel_record_failed').length, 0);
  first.stop();
  // A restart: the in-memory guard is gone; the same message id is mirrored again (a
  // replayed ask_owner answers the same id).
  const second = createOwnerChannels({ host: fake.host, log: () => {}, limits });
  second.ready();
  const again = fakeChannel('slack');
  second.service.register(again.channel);
  assert.deepEqual(await second.ownerMessageSent('m-out', 'Your build finished'), { mirrored: false, reason: 'already_mirrored' });
  assert.equal(slack.sent.length + again.sent.length, 1, 'the owner got it once');
  // A refusal is the host's answer: not retried.
  fake.host.recordDelivery = async delivery => { fake.calls.push(['recordDelivery', delivery]); return { refused: 'unknown_message' }; };
  const before = only(fake.calls, 'recordDelivery').length;
  await second.ownerMessageSent('m-out-2', 'next');
  assert.equal(only(fake.calls, 'recordDelivery').length, before + 1);
});

test('a send that never answers is given up after the bound: recorded as failed, and the next message is mirrored', async () => {
  const fake = fakeHost({ defaultChannel: 'slack' });
  const channels = createOwnerChannels({ host: fake.host, log: () => {}, limits: { ...CHANNEL_LIMITS, sendMs: 30 } });
  channels.ready();
  const sent = [];
  channels.service.register({ name: 'slack', status: () => ({ connected: true }), send(text) {
    sent.push(text); return text === 'hangs' ? new Promise(() => {}) : Promise.resolve({ ref: 'r-' + text }); } });
  const one = channels.ownerMessageSent('o-1', 'hangs'), two = channels.ownerMessageSent('o-2', 'next');
  assert.deepEqual(await one, { mirrored: false, reason: 'send_failed', channel: 'slack' });
  assert.deepEqual(await two, { mirrored: true, channel: 'slack' });
  assert.deepEqual(only(fake.calls, 'recordDelivery'), [{ messageId: 'o-1', channel: 'slack', error: 'send_timeout' },
    { messageId: 'o-2', channel: 'slack', channelMessageRef: 'r-next' }]);
  // An answer on the channel is bounded the same way: the inbound call still returns.
  const hung = createOwnerChannels({ host: fakeHost().host, log: () => {}, limits: { ...CHANNEL_LIMITS, sendMs: 30 } });
  hung.ready();
  hung.service.register({ name: 'slack', status: () => ({ connected: true }), send: () => new Promise(() => {}) });
  assert.deepEqual(await hung.service.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'b-1', text: '123456' }),
    { accepted: true });
});

test('mirrors run one at a time, in the order the messages were written; a failing host never rejects', async () => {
  const fake = fakeHost({ defaultChannel: 'slack' });
  const channels = createOwnerChannels({ host: fake.host, log: () => {} });
  channels.ready();
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const order = [];
  // The first send waits: the second must not overtake it.
  channels.service.register({ name: 'slack', status: () => ({ connected: true }), async send(text) {
    if (text === 'first') await gate;
    order.push(text); return { ref: text }; } });
  const one = channels.ownerMessageSent('o-1', 'first'), two = channels.ownerMessageSent('o-2', 'second');
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(order, []);
  release();
  await Promise.all([one, two]);
  assert.deepEqual(order, ['first', 'second']);
  fake.host.list = async () => { throw new Error('backend down'); };
  assert.deepEqual(await channels.ownerMessageSent('o-3', 'third'), { mirrored: false, reason: 'unavailable' });
  assert.deepEqual(await channels.ownerMessageSent('', 'x'), { mirrored: false, reason: 'nothing_to_send' });
});

test('the channels capability is validated like the other object-shaped ones', () => {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-director-roles-'));
  const role = prompt => ({ route: 'r', reasoning_effort: 'high', compaction: { thresholdRatio: 0.5 }, prompt, presented: ['bash'], forbidden: [] });
  writeFileSync(join(dir, 'roles.json'), JSON.stringify({ version: 1, director: role('director.md'), worker: role('worker.md') }));
  writeFileSync(join(dir, 'director.md'), 'd'); writeFileSync(join(dir, 'worker.md'), 'w');
  const host = { version: '1.2.0', capabilities: ['channels'], channels: fakeHost().host,
    config: { sessionId: 's', provider: 'p', model: 'm', stateDir: '/tmp/x', dshEntry: '/x', rolesFile: join(dir, 'roles.json'),
      todo: { module: 'dsh_todo', stateDirEnv: 'X' } }, context: async () => '', events: async () => ({ stop() {} }) };
  assert.ok(validateHost(host));
  assert.throws(() => validateHost({ ...host, channels: { ...host.channels, recordDelivery: undefined } }),
    /capability declared but not implemented: channels \(relay, bind, sourceOf, recordDelivery and list must all be functions\)/);
  assert.throws(() => validateHost({ ...host, capabilities: [] }), /implemented but not declared: channels/);
});

// ---- status: what a channel reports reaches the host's optional reportStatus, on a change only

const QR_SVG = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M4 4.5h7"/></svg>';

/** A fake host that takes status reports (`answer` decides each answer), and one channel
 *  whose status the test sets. */
function statusSetup({ limits, answer = () => ({ recorded: true }) } = {}) {
  const fake = fakeHost(), logs = [], reports = [];
  fake.host.reportStatus = async status => { reports.push(status); return answer(status); };
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]),
    limits: { ...CHANNEL_LIMITS, ...limits } });
  const wechat = { now: { connected: false, detail: 'bridge not reached yet' } };
  const channel = { name: 'wechat', status: () => wechat.now, async send() { return {}; } };
  return { ...fake, channels, logs, reports, wechat, channel };
}

test('status: read when the channel registers, then reported to the host only when it changes', async () => {
  const { channels, reports, wechat, channel, logs } = statusSetup();
  channels.ready();
  try {
    const unregister = channels.service.register(channel);
    await channels.pollStatus();
    assert.deepEqual(reports, [{ channel: 'wechat', connected: false, detail: 'bridge not reached yet' }]);
    await channels.pollStatus();
    await channels.pollStatus();
    assert.equal(reports.length, 1, 'the same status is not reported again');
    wechat.now = { connected: false, detail: 'scan needed', qrSvg: QR_SVG };
    await channels.pollStatus();
    wechat.now = { connected: false, detail: 'scan needed', qrSvg: QR_SVG };
    await channels.pollStatus();
    wechat.now = { connected: true };
    await channels.pollStatus();
    await channels.pollStatus();
    assert.deepEqual(reports.slice(1), [{ channel: 'wechat', connected: false, detail: 'scan needed', qrSvg: QR_SVG },
      { channel: 'wechat', connected: true }]);
    // Taken back (the channel plugin stops): disconnected, once.
    unregister();
    unregister();
    await channels.pollStatus();
    assert.deepEqual(reports.slice(3), [{ channel: 'wechat', connected: false, detail: 'channel plugin stopped' }]);
    // Logged: the channel and whether it is connected - never the detail or the QR.
    assert.deepEqual(logs.filter(([event]) => event.startsWith('channel_status')).map(([, fields]) => fields),
      [{ channel: 'wechat', connected: false }, { channel: 'wechat', connected: false }, { channel: 'wechat', connected: true },
        { channel: 'wechat', connected: false }]);
    assert.ok(!JSON.stringify(logs).includes('<svg') && !JSON.stringify(logs).includes('scan needed'));
  } finally { channels.stop(); }
});

test('status: nothing reaches the host before it is ready; the poll runs on its own every statusMs until stop()', async () => {
  const { channels, reports, wechat, channel } = statusSetup({ limits: { statusMs: 15 } });
  channels.service.register(channel);
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.deepEqual(reports, [], 'the host is not ready yet');
  channels.ready();
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.deepEqual(reports, [{ channel: 'wechat', connected: false, detail: 'bridge not reached yet' }]);
  wechat.now = { connected: true };
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.deepEqual(reports.at(-1), { channel: 'wechat', connected: true }, 'picked up by the timer, no explicit poll');
  channels.stop();
  const count = reports.length;
  wechat.now = { connected: false, detail: 'later' };
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.equal(reports.length, count, 'stopped: no more polls');
});

test('status: a host that cannot be reached is asked again at the next poll; a refusal is its answer and is not repeated', async () => {
  let mode = 'fail';
  const { channels, reports, wechat, channel, logs } = statusSetup({ answer: () => {
    if (mode === 'fail') throw new Error('HTTP 503');
    return mode === 'refuse' ? { refused: 'qr_svg_refused' } : { recorded: true };
  } });
  channels.ready();
  channels.service.register(channel);
  await channels.pollStatus();
  assert.equal(reports.length, 2, 'a failure is tried again at the next poll');
  mode = 'refuse';
  await channels.pollStatus();
  await channels.pollStatus();
  assert.equal(reports.length, 3, 'a refusal is not repeated');
  mode = 'ok';
  wechat.now = { connected: true };
  await channels.pollStatus();
  assert.equal(reports.length, 4);
  assert.deepEqual(logs.filter(([event]) => event.startsWith('channel_status')).map(([event]) => event),
    ['channel_status_failed', 'channel_status_failed', 'channel_status_refused', 'channel_status_reported']);
  channels.stop();
});

test('status: a status() that throws reads as disconnected; a long detail is cut; junk fields are dropped', async () => {
  const { channels, reports, wechat, channel } = statusSetup();
  channels.ready();
  wechat.now = { connected: 'yes', detail: 'd'.repeat(400), qrSvg: 42, extra: 'x' };
  channels.service.register(channel);
  await channels.pollStatus();
  assert.deepEqual(reports, [{ channel: 'wechat', connected: false, detail: 'd'.repeat(300) }]);
  channels.service.register({ name: 'slack', status: () => { throw new Error('boom'); }, async send() { return {}; } });
  await channels.pollStatus();
  assert.deepEqual(reports.at(-1), { channel: 'slack', connected: false, detail: 'status unavailable' });
  channels.stop();
});

test('status: a host without reportStatus is never asked, and nothing polls', async () => {
  const fake = fakeHost();
  let read = 0;
  const channels = createOwnerChannels({ host: fake.host, limits: { ...CHANNEL_LIMITS, statusMs: 5 } });
  channels.ready();
  channels.service.register({ name: 'wechat', status: () => { read++; return { connected: true }; }, async send() { return {}; } });
  await new Promise(resolve => setTimeout(resolve, 30));
  await channels.pollStatus();
  assert.deepEqual(fake.calls, []);
  assert.ok(read <= 2, `status() was read ${read} times`);
  channels.stop();
});

test('the channels capability may implement reportStatus or not', () => {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-director-roles-'));
  const role = prompt => ({ route: 'r', reasoning_effort: 'high', compaction: { thresholdRatio: 0.5 }, prompt, presented: ['bash'], forbidden: [] });
  writeFileSync(join(dir, 'roles.json'), JSON.stringify({ version: 1, director: role('director.md'), worker: role('worker.md') }));
  writeFileSync(join(dir, 'director.md'), 'd'); writeFileSync(join(dir, 'worker.md'), 'w');
  const host = { version: '1.2.0', capabilities: ['channels'], channels: { ...fakeHost().host, reportStatus: async () => ({}) },
    config: { sessionId: 's', provider: 'p', model: 'm', stateDir: '/tmp/x', dshEntry: '/x', rolesFile: join(dir, 'roles.json'),
      todo: { module: 'dsh_todo', stateDirEnv: 'X' } }, context: async () => '', events: async () => ({ stop() {} }) };
  assert.ok(validateHost(host));
});

// ---- channel-down alert (design-steward 7.9.2): claimed after a poll, sent through ANOTHER channel

const MINUTE = 60000;
/** A fake backend behind host.channels for the alert: the status rows the reports make (an
 *  outage keeps its down_since), and claim/record with the backend's rules that matter here -
 *  due 10 minutes in, via another bound connected channel, at most two sends per outage (the
 *  second 30 minutes after a failed first), one `up` after a delivered `down`, a replay per
 *  request key. No quiet hours (tests/unit/test_channel_alerts.py pins those). It outlives a
 *  plugin (a restart keeps the backend). */
function alertBackend(clock, { bindings = { slack: 'U-OWNER', wechat: 'wxid-owner' } } = {}) {
  const rows = new Map(), ledger = new Map(), claims = [], records = [];
  const iso = () => new Date(clock.now).toISOString();
  const via = channel => CHANNEL_NAMES.find(c => c !== channel && bindings[c] && rows.get(c)?.connected);
  return { rows, claims, records, bindings,
    reportStatus(status) {
      const prev = rows.get(status.channel), row = { ...(prev ?? { attempts: 0, alertedSince: null, upDue: false }), connected: status.connected,
        detail: status.detail ?? null };
      if (status.connected) {
        if (prev && !prev.connected) row.upDue = prev.downSince !== null && prev.alertedSince === prev.downSince;
        row.downSince = null;
      } else if (!prev || prev.connected) Object.assign(row, { downSince: iso(), attempts: 0, error: null, upDue: false });
      rows.set(status.channel, row);
      return { recorded: true, channel: status.channel, connected: status.connected, at: iso(), down_since: row.downSince,
        up_due: row.upDue, alerted_since: row.alertedSince, alert_attempts: row.attempts };
    },
    claimAlert({ channel, kind, downSince, attempt }) {
      const key = `channel-alert:${channel}:${kind}:${downSince}:${attempt}`;
      claims.push(key);
      if (ledger.has(key)) return ledger.get(key);
      const row = rows.get(channel);
      if (!row) return {};
      if (kind === 'up') { if (!row.upDue || row.alertedSince !== downSince) return {}; }
      else {
        if (row.connected || row.downSince !== downSince || !bindings[channel] || row.alertedSince === downSince || row.attempts >= 2) return {};
        if (row.attempts > 0 && row.error === null) return {};
        const due = Math.max(Date.parse(downSince) + 10 * MINUTE, row.attempts > 0 ? row.alertedAt + 30 * MINUTE : 0);
        if (clock.now < due) return { held: 'early', retry_at: new Date(due).toISOString() };
      }
      const through = via(channel);
      if (!through) return { held: 'no_channel' };
      const answer = { channel, kind, down_since: downSince, detail: row.detail ? row.detail.slice(0, 80) : null, via: through,
        attempt: kind === 'up' ? 1 : row.attempts + 1 };
      if (kind === 'up') { row.upDue = false; answer.up_at = iso(); }
      else Object.assign(row, { attempts: row.attempts + 1, alertedAt: clock.now, error: null });
      ledger.set(key, answer);
      return answer;
    },
    recordAlert(outcome) {
      records.push(outcome);
      const row = rows.get(outcome.channel);
      if (outcome.kind === 'down' && row?.downSince === outcome.downSince) {
        if (outcome.error) row.error = outcome.error; else row.alertedSince = outcome.downSince;
      }
      return { recorded: true };
    } };
}
/** Two channels on a plugin over `backend`; each channel's status and send are the test's. */
function alertSetup(backend, clock, { limits, wechatNow } = {}) {
  const fake = fakeHost({ bindings: backend.bindings }), logs = [];
  Object.assign(fake.host, { reportStatus: async s => backend.reportStatus(s), claimAlert: async c => backend.claimAlert(c),
    recordAlert: async o => backend.recordAlert(o) });
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]), clock: () => clock.now,
    limits: { ...CHANNEL_LIMITS, ...limits } });
  const make = name => {
    const c = { now: { connected: true }, sent: [], fail: undefined };
    c.channel = { name, status: () => c.now, async send(text, options) { c.sent.push({ text, options }); if (c.fail) throw new Error(c.fail); return { ref: 'r' }; } };
    return c;
  };
  const slack = make('slack'), wechat = make('wechat');
  if (wechatNow) wechat.now = wechatNow;  // what it reads when it registers (a restart mid-outage)
  channels.ready();
  channels.service.register(slack.channel);
  channels.service.register(wechat.channel);
  /** Polls every 30 s (the host's statusMs) for `ms`, as the timer would. */
  const run = async ms => { for (const end = clock.now + ms; clock.now < end;) { clock.now += CHANNEL_LIMITS.statusMs; await channels.pollStatus(); } };
  return { channels, logs, slack, wechat, run, poll: () => channels.pollStatus(), fake };
}
const T_ALERT = Date.parse('2026-11-23T11:00:00Z');

test('alert: a channel down 10 minutes is told once through the OTHER bound connected channel, in fixed text without the QR', async () => {
  const clock = { now: T_ALERT }, backend = alertBackend(clock);
  const s = alertSetup(backend, clock);
  try {
    await s.poll();
    s.wechat.now = { connected: false, detail: 'no QR (logged_out)', qrSvg: QR_SVG };
    await s.poll();
    await s.run(9 * MINUTE);
    assert.deepEqual(backend.claims, [], 'nothing is claimed before 10 minutes');
    assert.deepEqual(s.slack.sent, []);
    await s.run(MINUTE);
    assert.equal(backend.claims.length, 1);
    assert.deepEqual(s.wechat.sent, [], 'never through the channel that is down');
    assert.equal(s.slack.sent.length, 1);
    const [{ text, options }] = s.slack.sent;
    assert.deepEqual(options, { to: 'U-OWNER' });
    assert.match(text, /^微信断开了 1\d 分钟（no QR \(logged_out\)）。断开期间发到微信的消息收不到；去面板「渠道」页处理。$/);
    assert.ok(!text.includes('<svg') && !text.includes('qrSvg'));
    assert.deepEqual(backend.records.map(({ channel, kind, via, error }) => [channel, kind, via, error]), [['wechat', 'down', 'slack', undefined]]);
    // Stably down and already alerted: an hour of polls claims nothing more.
    const claims = backend.claims.length;
    await s.run(60 * MINUTE);
    assert.equal(backend.claims.length, claims, 'no claim while it stays down after its alert');
    assert.equal(s.slack.sent.length, 1);
    // Logged: channel, kind, via - never the text or the detail.
    assert.deepEqual(s.logs.filter(([event]) => event.startsWith('channel_alert')), [['channel_alert_sent', { channel: 'wechat', kind: 'down', via: 'slack' }]]);
    assert.ok(!JSON.stringify(s.logs).includes('logged_out') && !JSON.stringify(s.logs).includes('断开'));
    // Back: one recovery notice, through Slack, with how long it was down.
    s.wechat.now = { connected: true };
    await s.poll();
    assert.equal(s.slack.sent.length, 2);
    assert.match(s.slack.sent[1].text, /^微信恢复了（断开了 7\d 分钟）。$/);
    const after = backend.claims.length;
    await s.run(60 * MINUTE);
    assert.equal(backend.claims.length, after, 'stably connected: no claim');
    assert.equal(s.slack.sent.length, 2);
  } finally { s.channels.stop(); }
});

test('alert: with no other channel to send through nothing is sent; when one connects the alert goes out then', async () => {
  const clock = { now: T_ALERT }, backend = alertBackend(clock);
  const s = alertSetup(backend, clock);
  try {
    s.slack.now = { connected: false, detail: 'socket closed' };
    s.wechat.now = { connected: false, detail: 'bridge unreachable' };
    await s.poll();
    await s.run(15 * MINUTE);
    assert.deepEqual([s.slack.sent, s.wechat.sent], [[], []]);
    const held = backend.claims.length;
    assert.ok(held >= 1 && held <= 2, `one held claim per channel, not one per poll (${held})`);
    await s.run(15 * MINUTE);
    assert.equal(backend.claims.length, held, 'held no_channel: not asked again until a channel connects');
    s.slack.now = { connected: true };
    await s.poll();
    assert.equal(s.slack.sent.length, 1);
    assert.match(s.slack.sent[0].text, /^微信断开了 3\d 分钟（bridge unreachable）/);
    assert.deepEqual(s.wechat.sent, []);
  } finally { s.channels.stop(); }
});

test('alert: a failed send is recorded as failed and tried once more 30 minutes later; WeChat\'s recipient_unknown is one', async () => {
  const clock = { now: T_ALERT }, backend = alertBackend(clock);
  const s = alertSetup(backend, clock);
  try {
    s.wechat.fail = '409 recipient_unknown';
    s.slack.now = { connected: false, detail: 'socket closed' };
    await s.poll();
    await s.run(10 * MINUTE);
    assert.equal(s.wechat.sent.length, 1, 'tried through WeChat');
    assert.deepEqual(backend.records.map(r => [r.channel, r.via, r.error]), [['slack', 'wechat', '409 recipient_unknown']]);
    await s.run(29 * MINUTE);
    assert.equal(s.wechat.sent.length, 1, 'not again before 30 minutes');
    await s.run(MINUTE);
    assert.equal(s.wechat.sent.length, 2, 'the second try');
    assert.deepEqual(backend.records.map(r => [r.attempt, r.error]), [[1, '409 recipient_unknown'], [2, '409 recipient_unknown']]);
    await s.run(120 * MINUTE);
    assert.equal(s.wechat.sent.length, 2, 'never a third');
    assert.equal(backend.rows.get('slack').error, '409 recipient_unknown', 'what the dashboard shows');
  } finally { s.channels.stop(); }
});

test('alert: a host restart during an outage does not alert again, and a host without claimAlert is never asked', async () => {
  const clock = { now: T_ALERT }, backend = alertBackend(clock);
  let s = alertSetup(backend, clock);
  s.wechat.now = { connected: false, detail: 'bridge unreachable' };
  await s.poll();
  await s.run(12 * MINUTE);
  assert.equal(s.slack.sent.length, 1);
  s.channels.stop();
  // The restarted host reports "bridge not reached yet" first: the outage keeps its clock.
  s = alertSetup(backend, clock, { wechatNow: { connected: false, detail: 'bridge not reached yet' } });
  try {
    await s.poll();
    assert.equal(backend.rows.get('wechat').downSince, new Date(T_ALERT).toISOString(), 'the outage kept its clock');
    await s.run(30 * MINUTE);
    assert.deepEqual(s.slack.sent, [], 'the restarted host does not send it again');
    assert.equal(backend.claims.filter(key => key.endsWith(':1')).length, 1, 'attempt 1 is never asked again (its replay would resend it)');
    assert.ok(backend.claims.at(-1).endsWith(':2'), 'it asks with the next attempt the status answer gave');
  } finally { s.channels.stop(); }
  const plain = createOwnerChannels({ host: { ...fakeHost().host, reportStatus: async st => backend.reportStatus(st) }, clock: () => clock.now });
  plain.ready();
  plain.service.register({ name: 'slack', status: () => ({ connected: false }), async send() { throw new Error('never'); } });
  const claims = backend.claims.length;
  clock.now += 60 * MINUTE;
  await plain.pollStatus();
  await plain.pollStatus();
  plain.stop();
  assert.equal(backend.claims.length, claims, 'no claimAlert: no alert');
});

test('alertText: the two fixed texts; the detail is cut to 80 characters; no detail, no brackets', () => {
  const down = { channel: 'wechat', kind: 'down', down_since: '2026-11-23T11:00:00Z' };
  assert.equal(alertText({ ...down, detail: 'no QR (logged_out)' }, T_ALERT + 12 * MINUTE),
    '微信断开了 12 分钟（no QR (logged_out)）。断开期间发到微信的消息收不到；去面板「渠道」页处理。');
  assert.equal(alertText({ ...down, channel: 'slack' }, T_ALERT + 10 * MINUTE),
    'Slack断开了 10 分钟。断开期间发到Slack的消息收不到；去面板「渠道」页处理。');
  assert.ok(alertText({ ...down, detail: '长'.repeat(200) }, T_ALERT).includes('（' + '长'.repeat(80) + '）'));
  assert.equal(alertText({ ...down, kind: 'up', up_at: '2026-11-23T11:40:00Z' }, T_ALERT + 999 * MINUTE), '微信恢复了（断开了 40 分钟）。');
});

// --- what claude/s19-integrate held that this line did not (each fails at 9678b68e08) -------

const EXECUTED = Object.freeze({ proposal_id: 'p-1', action: 'approve', outcome: 'executed', kind: 'create_project',
  target: 'gamma', result: { status: 'done' } });
const SEND_AGAIN = /cannot be retried: please send it again/;
/** setup() whose Slack send fails while `s.broken` is true (what it did send: s.slack.sent). */
const brokenSetup = (options, limits) => {
  const fake = fakeHost(options), logs = [];
  const file = join(mkdtempSync(join(tmpdir(), 'dsh-channels-owed-')), 'owed.json');
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]),
    limits: { ...CHANNEL_LIMITS, answerRetryMs: 1, ...limits }, owedFile: file });
  channels.ready();
  const s = { ...fake, channels, logs, owedFile: file, broken: true, inbound: message => channels.service.inbound(message) };
  const slack = fakeChannel('slack');
  s.slack = slack;
  channels.service.register({ ...slack.channel, send: async (text, options) => {
    if (s.broken) throw new Error('socket closed');
    return slack.channel.send(text, options);
  } });
  return s;
};

test('a burst held for rate_limited: the bound owner is told once it is held, and the message\'s own answer is still owed', async () => {
  const s = setup();
  s.host.next = { refused: 'rate_limited' };
  await s.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'x-1', text: 'hi' });
  const approval = { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: '同意 proposal p-1', at: RECENT };
  for (const message of [approval, { ...approval, messageRef: 'o-2', text: 'and this' }])
    assert.deepEqual(await s.inbound(message), { accepted: false, reason: 'rate_limited' });
  assert.deepEqual(s.slack.sent, [{ text: channelReplies.rate_limited, options: { replyToRef: 'o-1', to: 'U-OWNER' } }],
    'one Held line for the burst, to the owner only');
  assert.match(channelReplies.rate_limited, /^Held: .*no need to send them again/);
  // The held approval is taken on its next delivery, and its own line still goes out.
  s.host.next = { message_id: 'm', duplicate: false, approval: EXECUTED };
  assert.deepEqual(await s.inbound(approval), { accepted: true });
  assert.match(s.slack.sent.at(-1).text, /^proposal p-1: approved and done/);
  s.channels.stop();
});

test('the binding-code line has its own minute: no other refusal line holds back the news that a code was retired', async () => {
  const s = setup({ bindings: { slack: 'U-OWNER', wechat: 'wxid-owner' } });
  s.host.next = { refused: 'body_too_long' };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: 'x'.repeat(5000) });
  s.host.storeText = false;
  s.host.next = { refused: 'code_sent_to_bound_account' };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-2', text: 'binding code: 711833' });
  assert.deepEqual(s.slack.sent.map(line => line.text), [channelReplies.body_too_long, channelReplies.code_sent_to_bound_account]);
  s.host.next = { refused: 'approval_undated' };
  await s.inbound({ channel: 'wechat', senderRef: 'wxid-owner', messageRef: 'w-1', text: '同意 proposal p-1' });
  s.host.next = { refused: 'code_sent_to_bound_account' };
  await s.inbound({ channel: 'wechat', senderRef: 'wxid-owner', messageRef: 'w-2', text: '711833' });
  assert.deepEqual(s.wechat.sent.map(line => line.text), [channelReplies.approval_undated, channelReplies.code_sent_to_bound_account]);
  // Still at most one code line per channel and minute.
  await s.inbound({ channel: 'wechat', senderRef: 'wxid-owner', messageRef: 'w-3', text: '711833' });
  assert.equal(s.wechat.sent.length, 2);
  s.channels.stop();
});

test('a refusal line whose send failed is owed, not lost, and spends no minute: the owner learns it on the next attempt', async () => {
  // The channel plugin is reconnecting exactly when the host refuses the message for good.
  const first = brokenSetup();
  first.host.next = { refused: 'body_too_long' };
  const refused = { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: 'x'.repeat(5000) };
  assert.deepEqual(await first.inbound(refused), { accepted: false, reason: 'body_too_long' });
  assert.equal(first.slack.sent.length, 0, 'the send failed: nothing went out');
  assert.equal(first.logs.filter(([event]) => event === 'channel_answer_failed').length, CHANNEL_LIMITS.answerAttempts,
    'the send was tried its bounded number of times');
  // The reason is NOT dropped with the record: the store keeps it (and it was the host's answer,
  // so the text the host allowed to be kept stays too).
  const records = owed(first.owedFile);
  assert.deepEqual(records.map(message => [message.messageRef, message.refusal]), [['o-1', 'body_too_long']]);
  assert.equal(records[0].text, refused.text);
  assert.ok(first.logs.some(([event, fields]) => event === 'channel_refusal_owed' && fields.reason === 'body_too_long'));
  // And the failed send spent no minute: the next refusal of the same minute is told.
  first.broken = false;
  first.host.next = { refused: 'approval_undated' };
  await first.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-2', text: '同意 proposal p-1' });
  assert.deepEqual(first.slack.sent.map(line => line.text), [channelReplies.approval_undated],
    "the failed send silenced the next refusal's line");
  first.channels.stop();
  // The reason itself, told on a later attempt: a fresh process drains the store. It sends the
  // line the failed send left owed - under the refused message's ref, once, and without relaying
  // a message the host already refused for good.
  const second = setup(undefined, undefined, { owedFile: first.owedFile, ready: false });
  second.start();
  await second.channels.pollOwed();
  assert.deepEqual(second.slack.sent.map(line => line.text), [channelReplies.body_too_long]);
  assert.deepEqual(second.slack.sent[0].options, { replyToRef: 'o-1', to: 'U-OWNER' });
  assert.deepEqual(only(second.calls, 'relay'), [], 'a message refused for good is not relayed again');
  assert.deepEqual(owed(first.owedFile), [], 'and nothing is owed once the line went out');
  await second.channels.pollOwed();
  assert.equal(second.slack.sent.length, 1, 'the line is not sent twice');
  second.channels.stop();
});

test('a Held line whose send failed spends no minute: the next rate_limited in the same minute is told', async () => {
  const s = brokenSetup();
  s.host.next = { refused: 'rate_limited' };
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: 'hi' }),
    { accepted: false, reason: 'rate_limited' });
  assert.equal(s.slack.sent.length, 0, 'the send failed: the owner was not told');
  s.broken = false;
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-2', text: 'and this' }),
    { accepted: false, reason: 'rate_limited' });
  assert.deepEqual(s.slack.sent.map(line => [line.text, line.options.to]), [[channelReplies.rate_limited, 'U-OWNER']],
    "the failed Held send spent the minute it never used");
  s.channels.stop();
});

test('an owed refusal line outlives a minute another line spent: the same process drains it later, once', async () => {
  // The store, not a fresh process: the record must survive the minute a later line spends,
  // and 'held' must not be read as "already told" - the line a drain in a NEW process sends
  // (with an empty in-memory throttle) proves nothing about this one.
  const s = brokenSetup();
  s.host.next = { refused: 'body_too_long' };
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: 'x'.repeat(5000) }),
    { accepted: false, reason: 'body_too_long' });
  assert.equal(s.slack.sent.length, 0, 'the send failed: nothing went out');
  assert.deepEqual(owed(s.owedFile).map(message => [message.messageRef, message.refusal]), [['o-1', 'body_too_long']]);
  // The channel is back: the NEXT refusal's line goes out and spends this minute.
  s.broken = false;
  s.host.next = { refused: 'approval_undated' };
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-2', text: '同意 proposal p-1' }),
    { accepted: false, reason: 'approval_undated' });
  assert.deepEqual(s.slack.sent.map(line => line.text), [channelReplies.approval_undated]);
  // An owed tick now, in the same minute: every line this channel may send is held, so
  // nothing goes out - and the reason o-1 still owes is kept for a later tick.
  await s.channels.pollOwed();
  assert.deepEqual(s.slack.sent.map(line => line.text), [channelReplies.approval_undated], 'the minute is spent');
  assert.deepEqual(owed(s.owedFile).map(message => [message.messageRef, message.refusal]), [['o-1', 'body_too_long']],
    "a 'held' tick dropped a line that never went out");
  assert.ok(s.logs.some(([event, fields]) => event === 'channel_owed_held' && fields.reason === 'throttled'));
  // The next minute: the drain says it, to the owner, under the refused message's own ref.
  const realNow = Date.now;
  try {
    Date.now = () => realNow() + 61000;
    await s.channels.pollOwed();
    assert.deepEqual(s.slack.sent.map(line => line.text), [channelReplies.approval_undated, channelReplies.body_too_long]);
    assert.deepEqual(s.slack.sent[1].options, { replyToRef: 'o-1', to: 'U-OWNER' });
    assert.deepEqual(owed(s.owedFile), [], 'the line went out: nothing is owed any more');
    await s.channels.pollOwed();
    assert.equal(s.slack.sent.length, 2, 'the line is not sent twice');
  } finally { Date.now = realNow; }
  s.channels.stop();
});

test('a stop inside the refusal send: the reason is on disk before the send, so the restart owes the line', async () => {
  // The window the review found: the reason used to join the record only AFTER the send, and a
  // send can take two 30 s timeouts plus the retry wait. The fake here is a send that never
  // answers; the store is read at the moment that send is entered, and a second process over
  // the same file is the restart.
  const fake = fakeHost(), logs = [], seenAtSend = [];
  const file = join(mkdtempSync(join(tmpdir(), 'dsh-channels-owed-')), 'owed.json');
  const first = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]),
    limits: { ...CHANNEL_LIMITS, answerRetryMs: 1 }, owedFile: file });
  first.ready();
  let released, atSend, calls = 0;
  const entered = new Promise(resolve => { atSend = resolve; });
  first.service.register({ name: 'slack', status: () => ({ connected: true }),
    async send() {
      calls++;
      seenAtSend.push(owed(file).map(record => [record.messageRef, record.refusal, record.text]));
      if (calls === 1) { atSend(); return new Promise((_, reject) => { released = () => reject(new Error('channels_stopped')); }); }
      throw new Error('socket closed');
    } });
  // A live binding code: the host allows no body on disk, so this record can only carry the
  // reason - and the text-less drain is the one that used to ask the owner for a new code.
  fake.host.storeText = false;
  fake.host.next = { refused: 'code_sent_to_bound_account' };
  const run = first.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: 'binding code: 711833' });
  await entered;  // the send has begun and has not settled
  assert.deepEqual(seenAtSend, [[['o-1', 'code_sent_to_bound_account', undefined]]],
    'the record owed the reason only after the send');
  // A stop inside that send: this process is gone and the send never answers.
  first.stop();
  assert.deepEqual(owed(file).map(record => [record.messageRef, record.refusal, record.text]),
    [['o-1', 'code_sent_to_bound_account', undefined]], 'the reason and no body are on disk at the stop');
  const second = setup(undefined, undefined, { owedFile: file, ready: false });
  second.start();
  await second.channels.pollOwed();
  assert.deepEqual(second.slack.sent.map(line => line.text), [channelReplies.code_sent_to_bound_account]);
  assert.deepEqual(second.slack.sent[0].options, { replyToRef: 'o-1', to: 'U-OWNER' });
  assert.ok(!second.slack.sent.some(line => SEND_AGAIN.test(line.text)),
    'the restart asked the owner to send the refused binding code again');
  assert.deepEqual(only(second.calls, 'relay'), [], 'a message refused for good is not relayed again');
  assert.deepEqual(owed(file), []);
  second.channels.stop();
  // The stopped process's send finally answers: nothing more goes out and nothing is written back.
  released();
  assert.deepEqual(await run, { accepted: false, reason: 'code_sent_to_bound_account' });
  assert.equal(calls, CHANNEL_LIMITS.answerAttempts, "the stopped process's send was tried its bounded number of times");
  assert.equal(second.slack.sent.length, 1);
  assert.deepEqual(owed(file), []);
});

test('a stop inside the refusal\'s binding read: the reason is on disk BEFORE that read, so the restart owes the line', async () => {
  // The window nobody sampled: every other test here reads the store when the SEND is entered,
  // so a `remember` moved after the binding read (`host.list`, which can sit on the host's own
  // 30 s timeout) left the store looking like a body the host never judged, and the whole suite
  // stayed green. The fake's `list` IS that read: the store is read the moment it is entered for
  // this message, and that call never settles - the process stops inside the read.
  const fake = fakeHost(), logs = [], seenAtBinding = [];
  const file = join(mkdtempSync(join(tmpdir(), 'dsh-channels-owed-')), 'owed.json');
  const first = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]), owedFile: file });
  first.ready();
  let atBinding;
  const entered = new Promise(resolve => { atBinding = resolve; });
  fake.host.list = async () => {
    seenAtBinding.push(owed(file).map(record => [record.messageRef, record.refusal, record.text]));
    atBinding();
    return new Promise(() => {});  // the host never answers: this process is gone mid-read
  };
  // A live binding code: the host allows no body on disk, so the record can only carry the reason
  // (the sharpest shape - a stop before the reason is written leaves nothing that says WHY).
  fake.host.storeText = false;
  fake.host.next = { refused: 'code_sent_to_bound_account' };
  // Not awaited: this delivery never settles, which is the stopped process.
  void first.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: 'binding code: 711833' });
  await entered;
  // The invariant: at the binding read the record for o-1 already says WHY it is owed, and holds
  // no body (a live code is never written down). Written any later, a stop here would leave a
  // record the drain can only read as a message the host never judged.
  assert.deepEqual(seenAtBinding, [[['o-1', 'code_sent_to_bound_account', undefined]]],
    'the reason joined the record only after the binding read, so a stop inside that read left no reason on it');
  // The stop: this process is gone, the read never answered.
  first.stop();
  assert.deepEqual(owed(file).map(record => [record.messageRef, record.refusal, record.text]),
    [['o-1', 'code_sent_to_bound_account', undefined]], 'the reason and no body are on disk at the stop');
  // The restart drains the same file: the line that says WHY goes out - and nothing asks the owner
  // for a new code ("send it again") or offers the refused body to the host a second time.
  const second = setup(undefined, undefined, { owedFile: file, ready: false });
  second.start();
  await second.channels.pollOwed();
  assert.deepEqual(second.slack.sent.map(line => line.text), [channelReplies.code_sent_to_bound_account]);
  assert.deepEqual(second.slack.sent[0].options, { replyToRef: 'o-1', to: 'U-OWNER' });
  assert.ok(!second.slack.sent.some(line => SEND_AGAIN.test(line.text)),
    'the restart asked the owner to send the refused binding code again');
  assert.deepEqual(only(second.calls, 'relay'), [], 'a message refused for good is relayed again');
  assert.deepEqual(owed(file), [], 'the line went out: nothing is owed any more');
  second.channels.stop();
});

test('one pending refusal line per channel: a second refused message is not owed a line while one is still owed', async () => {
  // The bound that keeps 'held' records from turning a flood into one line per refused message.
  const s = brokenSetup();
  s.host.next = { refused: 'body_too_long' };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: 'x'.repeat(5000) });
  s.host.next = { refused: 'approval_undated' };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-2', text: '同意 proposal p-1' });
  assert.deepEqual(s.slack.sent, [], 'both sends failed: nothing went out');
  assert.deepEqual(owed(s.owedFile).map(message => [message.messageRef, message.refusal]), [['o-1', 'body_too_long']],
    'the channel owes one refusal line, not one per refused message');
  assert.ok(s.logs.some(([event, fields]) => event === 'channel_refusal_throttled' && fields.reason === 'approval_undated'));
  // And the one line it owes still goes out: a fresh process drains the store.
  s.channels.stop();
  const second = setup(undefined, undefined, { owedFile: s.owedFile, ready: false });
  second.start();
  await second.channels.pollOwed();
  assert.deepEqual(second.slack.sent.map(line => line.text), [channelReplies.body_too_long]);
  assert.deepEqual(owed(s.owedFile), []);
  second.channels.stop();
});

test("a refusal line held by a minute another line spent is kept, not thrown away: the next minute says it", async () => {
  // The lane's half of the drain/lane race: another line (here a refusal that DID go out)
  // spends the minute, the lane's own refusal is 'held' - and a minute later it is still owed.
  const s = setup();
  s.host.next = { refused: 'body_too_long' };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: 'x'.repeat(5000) });
  assert.deepEqual(s.slack.sent.map(line => line.text), [channelReplies.body_too_long], 'the minute is spent by a line that went out');
  s.host.next = { refused: 'approval_undated' };
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-2', text: '同意 proposal p-1' }),
    { accepted: false, reason: 'approval_undated' });
  assert.deepEqual(s.slack.sent.map(line => line.text), [channelReplies.body_too_long], 'one line per minute');
  assert.deepEqual(owed(s.owedFile).map(message => [message.messageRef, message.refusal]), [['o-2', 'approval_undated']],
    "a 'held' line was dropped although it never went out");
  const realNow = Date.now;
  try {
    Date.now = () => realNow() + 61000;
    await s.channels.pollOwed();
    assert.deepEqual(s.slack.sent.map(line => line.text), [channelReplies.body_too_long, channelReplies.approval_undated]);
    assert.deepEqual(s.slack.sent[1].options, { replyToRef: 'o-2', to: 'U-OWNER' });
    assert.deepEqual(owed(s.owedFile), []);
  } finally { Date.now = realNow; }
  s.channels.stop();
});

// --- what the non-author review still measured, reachable at 9dab5b09ab ---------------------
// Three losses left in the refusal path: the reason was written only when no other refusal line
// was pending (1), a record was forgotten whenever another line was pending, whatever the send
// returned (2), and the pending-slot check matched the minute-key but not the sender (3).

for (const [label, refused, storeText, first1, second1] of [
  ['a refused message whose body the host let be kept', 'approval_undated', true, '同意 proposal p-1', '同意 proposal p-2'],
  ['a refused binding code the host would not let be kept', 'code_sent_to_bound_account', false, 'binding code: 711833', '711833'],
]) {
  test(`a stop inside the refusal send while another line is pending (${label}): the reason is on disk before it, so the restart neither asks for a resend nor relays it again`, async () => {
    // The window defect (1): with another refusal line already owed, the reason never joined the
    // record, so the record held the write-ahead alone. A stop during the binding read or the send
    // (two 30 s timeouts plus the retry wait) then left the restart reading it as a body the host
    // never judged: "please send it again" about a message refused for good (the code variant: the
    // store must never hold the code, so the record has no body at all), or the refused body
    // relayed again (the variant whose body the host allowed to be kept). A send that never answers
    // is the fake's second call; the store is read as it is when that send begins, and a second
    // process over the same file is the restart.
    const fake = fakeHost(), logs = [], seenAtSend = [];
    fake.host.storeText = storeText;
    const file = join(mkdtempSync(join(tmpdir(), 'dsh-channels-owed-')), 'owed.json');
    const first = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]),
      limits: { ...CHANNEL_LIMITS, answerAttempts: 1, answerRetryMs: 1 }, owedFile: file });
    first.ready();
    let released, atSend, calls = 0;
    const entered = new Promise(resolve => { atSend = resolve; });
    first.service.register({ name: 'slack', status: () => ({ connected: true }),
      async send() {
        calls++;
        seenAtSend.push(owed(file).map(record => [record.messageRef, record.refusal, record.throttled, record.text]));
        if (calls === 2) { atSend(); return new Promise((_, reject) => { released = () => reject(new Error('channels_stopped')); }); }
        throw new Error('socket closed');  // the first refusal's line did not go out
      } });
    // o-1: refused for good, and its line could not be sent - so the channel owes that line.
    fake.host.next = { refused };
    assert.deepEqual(await first.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: first1 }),
      { accepted: false, reason: refused });
    assert.equal(calls, 1, 'the line could not be sent');
    assert.deepEqual(owed(file).map(record => [record.messageRef, record.refusal]), [['o-1', refused]]);
    // o-2: refused while that line is still owed. Its send is entered and never answers.
    fake.host.next = { refused };
    const run = first.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-2', text: second1 });
    await entered;
    // A stop inside that send: this process is gone, the send never answers.
    first.stop();
    const atStop = owed(file).map(record => [record.messageRef, record.refusal, record.throttled]);
    // The restart drains: the pending line goes out once, o-2 owes none of its own - and above all
    // there is no "send it again" notice about a refusal and no relay of a refused body.
    const second = setup(undefined, undefined, { owedFile: file, ready: false });
    second.start();
    await second.channels.pollOwed();
    assert.ok(!second.slack.sent.some(line => SEND_AGAIN.test(line.text)),
      'the restart asked the owner to send a refused message again');
    assert.deepEqual(only(second.calls, 'relay'), [], 'a message the host refused for good was relayed again');
    assert.deepEqual(second.slack.sent.map(line => line.text), [channelReplies[refused]]);
    assert.deepEqual(second.slack.sent[0].options, { replyToRef: 'o-1', to: 'U-OWNER' });
    assert.ok(second.logs.some(([event, fields]) => event === 'channel_owed_dropped' && fields.reason === 'throttled'));
    assert.deepEqual(owed(file), [], 'the pending line went out and the record that owed none of its own is done');
    second.channels.stop();
    // What the review measured missing: at the moment the send began, and at the stop, o-2's record
    // said WHY it is owed - and that it owes no line of its own (the pending line carries the
    // channel's news). This is the root cause of the two losses above.
    assert.deepEqual(seenAtSend.at(-1),
      [['o-1', refused, undefined, storeText ? first1 : undefined], ['o-2', refused, true, storeText ? second1 : undefined]],
      'the reason was written only after the send, or not at all while another line was pending');
    assert.deepEqual(atStop, [['o-1', refused, undefined], ['o-2', refused, true]], 'the reason is on disk at the stop');
    // The stopped process's send finally answers: its own memory is not the store the restart
    // drained, and nothing more goes out here.
    released();
    assert.deepEqual(await run, { accepted: false, reason: refused });
    assert.equal(second.slack.sent.length, 1);
  });
}

test("a drain's refusal send in flight is not a spent minute: the lane's 'held' line is still owed, and the freed minute carries it", async () => {
  // The race in defect (2): the drain's refusal send holds the minute (told.set), the lane is
  // refused the same minute-key, tellThrottled says the minute is taken and the lane forgot its
  // record. The drain's send then fails and gives the minute back - so the minute was never
  // spent, and the lane's line was gone. 'inflight' is now told apart from 'held'.
  const fake = fakeHost(), logs = [], sent = [];
  const file = join(mkdtempSync(join(tmpdir(), 'dsh-channels-owed-')), 'owed.json');
  const script = [];  // what the next sends do: 'fail' (socket closed), 'hold' (never answers), else send
  const holds = [];
  let heldStarted;
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]),
    limits: { ...CHANNEL_LIMITS, answerAttempts: 1, answerRetryMs: 1 }, owedFile: file });
  channels.ready();
  channels.service.register({ name: 'slack', status: () => ({ connected: true }),
    async send(text, options) {
      sent.push({ text, options });
      const step = script.shift() ?? 'ok';
      if (step === 'fail') throw new Error('socket closed');
      if (step === 'hold') {
        heldStarted?.(); heldStarted = undefined;
        await new Promise((_, reject) => holds.push(() => reject(new Error('socket closed'))));
      }
      return { ref: `slack-ref-${sent.length}` };
    } });
  const inbound = message => channels.service.inbound(message);
  const owedNow = () => owed(file).map(record => [record.messageRef, record.refusal, record.throttled]);
  // o-1: refused for good, its line could not be sent - the channel owes that line.
  script.push('fail');
  fake.host.next = { refused: 'body_too_long' };
  assert.deepEqual(await inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: 'x'.repeat(5000) }),
    { accepted: false, reason: 'body_too_long' });
  assert.deepEqual(owedNow(), [['o-1', 'body_too_long', undefined]]);
  // A drain tick starts sending that line; the send is in flight and will fail.
  script.push('hold');
  const held = new Promise(resolve => { heldStarted = resolve; });
  const drain = channels.pollOwed();
  await held;
  // The lane is refused o-2 while that send holds the minute.
  const sentBefore = sent.length;
  fake.host.next = { refused: 'approval_undated' };
  assert.deepEqual(await inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-2', text: '同意 proposal p-1' }),
    { accepted: false, reason: 'approval_undated' });
  assert.equal(sent.length, sentBefore, 'the minute was taken: the lane attempted no send for o-2');
  assert.deepEqual(owedNow(), [['o-1', 'body_too_long', undefined], ['o-2', 'approval_undated', undefined]],
    'a line that never went out was dropped for a minute that was not spent');
  assert.ok(logs.some(([event, fields]) => event === 'channel_refusal_throttled' && fields.reason === 'approval_undated'));
  assert.ok(logs.some(([event, fields]) => event === 'channel_refusal_owed' && fields.outcome === 'inflight'));
  // That send fails (nothing was said, the minute is given back): a later tick sends o-1's line
  // again - it fails once more, which frees the minute for o-2's own line in the same tick.
  script.push('fail', 'ok');
  holds.shift()();
  await drain;
  await channels.pollOwed();
  assert.deepEqual(sent.map(line => line.text),
    [channelReplies.body_too_long, channelReplies.body_too_long, channelReplies.body_too_long, channelReplies.approval_undated]);
  assert.deepEqual(sent.at(-1).options, { replyToRef: 'o-2', to: 'U-OWNER' });
  assert.deepEqual(owedNow(), [['o-1', 'body_too_long', undefined]], "o-2's line went out; the failing one is still owed");
  // The line that kept failing goes out in a later minute (the one o-2's line spent is over),
  // after which nothing is owed.
  const realNow = Date.now;
  try {
    Date.now = () => realNow() + 61000;
    await channels.pollOwed();
    assert.deepEqual(sent.map(line => line.text).at(-1), channelReplies.body_too_long);
    assert.deepEqual(owedNow(), []);
  } finally { Date.now = realNow; }
  channels.stop();
});

test("a stale refusal record never holds the bound owner's slot: after a rebind the owner's line is owed and goes out", async () => {
  // Defect (3): pendingRefusal matched the minute-key alone, so a refusal line still owed to the
  // account Slack was bound to BEFORE the owner rebound silenced the new owner's line - and the
  // drain then reads the binding, drops that stale record as not_bound, and the owner is told
  // nothing at all. The pending slot is the sender's: it is counted for the sender the line would
  // go to, so a record another account owes never holds the owner's.
  const fake = fakeHost({ bindings: { slack: 'U-OLD' } }), logs = [];
  const file = join(mkdtempSync(join(tmpdir(), 'dsh-channels-owed-')), 'owed.json');
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]),
    limits: { ...CHANNEL_LIMITS, answerAttempts: 1, answerRetryMs: 1 }, owedFile: file });
  channels.ready();
  const slack = fakeChannel('slack');
  const s = { broken: true };
  channels.service.register({ name: 'slack', status: () => ({ connected: true }),
    async send(text, options) { if (s.broken) throw new Error('socket closed'); return slack.channel.send(text, options); } });
  const owedNow = () => owed(file).map(record => [record.messageRef, record.refusal, record.throttled]);
  // The account bound at the time is refused for good and its line cannot be sent: it stays owed.
  fake.host.next = { refused: 'body_too_long' };
  assert.deepEqual(await channels.service.inbound({ channel: 'slack', senderRef: 'U-OLD', messageRef: 'r-1', text: 'x'.repeat(5000) }),
    { accepted: false, reason: 'body_too_long' });
  assert.deepEqual(owedNow(), [['r-1', 'body_too_long', undefined]]);
  // The owner rebinds Slack; the stale line is still owed to U-OLD, and the drain would drop it.
  fake.bindings.slack = 'U-NEW';
  fake.host.next = { refused: 'body_too_long' };
  assert.deepEqual(await channels.service.inbound({ channel: 'slack', senderRef: 'U-NEW', messageRef: 'o-1', text: 'x'.repeat(5000) }),
    { accepted: false, reason: 'body_too_long' });
  assert.deepEqual(owedNow(), [['r-1', 'body_too_long', undefined], ['o-1', 'body_too_long', undefined]],
    "another account's owed line throttled the owner's, and the owner's line was then dropped");
  assert.ok(!logs.some(([event, fields]) => event === 'channel_refusal_throttled' && fields.length === 5000),
    "the owner's refusal was held back by the account the channel was bound to before");
  // The channel is back: the drain drops the stale record as not_bound and says the owner's line,
  // to the account bound now and under the owner's own message ref.
  s.broken = false;
  await channels.pollOwed();
  assert.deepEqual(logs.filter(([event, fields]) => event === 'channel_owed_dropped' && fields.reason === 'not_bound').length, 1);
  assert.deepEqual(slack.sent, [{ text: channelReplies.body_too_long, options: { replyToRef: 'o-1', to: 'U-NEW' } }],
    'the owner was told nothing about a message the host refused');
  assert.deepEqual(owedNow(), []);
  channels.stop();
});

/** A backend outage: precheck, relay and the binding read all fail until `down` is false. */
const outage = s => {
  const { relay, codeCheck, list } = s.host;
  s.host.down = true;
  s.host.relay = async message => { if (s.host.down) { s.calls.push(['relay', message]); throw new Error('HTTP 502'); } return relay(message); };
  s.host.codeCheck = async message => { if (s.host.down) throw new Error('HTTP 502'); return codeCheck(message); };
  s.host.list = async () => { if (s.host.down) throw new Error('HTTP 502'); return list(); };
};

for (const [label, text, next, line] of [
  // Delivered once, so it gets the receipt once, and nothing else.
  ['an ordinary message', 'please look at the wuxia build', undefined, /^已收到，正在处理…$/],
  ['an approval', '同意 proposal p-1', { message_id: 'm', duplicate: false, approval: EXECUTED }, /^proposal p-1: approved and done/]]) {
  test(`${label} the plugin holds through a backend outage is never answered "send it again", and is delivered once`, async () => {
    const s = setup();
    outage(s);
    const message = { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text, at: NOW() };
    assert.deepEqual(await s.inbound(message), { accepted: false, reason: 'unavailable' });
    await s.channels.pollOwed();                                  // an owed tick during the outage
    assert.deepEqual(await s.inbound(message), { accepted: false, reason: 'unavailable' });  // the plugin's held retry
    s.host.down = false;                                          // the backend is back ...
    await s.channels.pollOwed();                                  // ... and the owed tick comes before the next retry
    s.host.next = next;
    assert.equal((await s.inbound(message)).accepted, true, 'the held retry delivers it');
    await s.channels.pollOwed();
    const lines = s.slack.sent.map(sent => sent.text);
    assert.ok(!lines.some(sent => SEND_AGAIN.test(sent)), `a delivered message was answered "send it again": ${JSON.stringify(lines)}`);
    assert.equal(lines.length, 1, JSON.stringify(lines));
    assert.match(lines[0], line);
    s.channels.stop();
  });
}

test('a message whose precheck is not answered is not relayed: a stop then costs a true "send it again", never an executed approval\'s line', async () => {
  const first = setup();
  first.host.codeCheckFails = true;
  first.host.next = { message_id: 'm', duplicate: false, approval: EXECUTED };
  const approval = { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-x', text: '同意 proposal p-1', at: RECENT };
  assert.deepEqual(await first.inbound(approval), { accepted: false, reason: 'unavailable' });
  assert.deepEqual(only(first.calls, 'relay'), [], 'no relay without the host\'s verdict on the text');
  assert.equal(owed(first.owedFile)[0].text, undefined);
  first.channels.stop();  // a restart: Slack's held copy is gone
  const second = setup(undefined, undefined, { owedFile: first.owedFile, ready: false });
  second.start();
  await second.channels.pollOwed();
  assert.deepEqual(only(second.calls, 'relay'), [], 'the backend never took it, so "not delivered" is the truth');
  assert.equal(second.slack.sent.length, 1);
  assert.match(second.slack.sent[0].text, SEND_AGAIN);
  second.channels.stop();
  // Without a stop, the plugin's held retry is relayed once the precheck answers, and the
  // approval gets its own line - no "send it again" before it.
  const s = setup();
  s.host.codeCheckFails = true;
  assert.deepEqual(await s.inbound(approval), { accepted: false, reason: 'unavailable' });
  await s.channels.pollOwed();
  s.host.codeCheckFails = false;
  s.host.next = { message_id: 'm', duplicate: false, approval: EXECUTED };
  assert.deepEqual(await s.inbound(approval), { accepted: true });
  assert.deepEqual(only(s.calls, 'relay').map(m => m.messageRef), ['ts-x']);
  assert.deepEqual(s.slack.sent.map(sent => sent.text), [channelReplies.approval(EXECUTED)]);
  s.channels.stop();
});

test('a ring replay whose head (an approval decided yesterday) meets one transient failure sends no stale line', async () => {
  const s = setup({ bindings: { wechat: 'wxid-owner' } });
  const old = { channel: 'wechat', senderRef: 'wxid-owner', messageRef: 'w-1', text: '同意 proposal p-1',
    at: new Date(Date.now() - 86400000).toISOString() };
  s.host.relay = async message => { s.calls.push(['relay', message]); throw new Error('HTTP 502'); };  // a backend still booting
  assert.deepEqual(await s.inbound(old), { accepted: false, reason: 'unavailable' });
  s.host.relay = async message => { s.calls.push(['relay', message]); return { message_id: 'm', duplicate: true, approval: EXECUTED }; };
  assert.deepEqual(await s.inbound(old), { accepted: true, duplicate: true });
  await s.channels.pollOwed();
  assert.deepEqual(s.wechat.sent, [], 'yesterday\'s decision is not announced again');
  assert.deepEqual(owed(s.owedFile), [], 'and nothing stays owed for it');
  s.channels.stop();
});

test('an owed line expires an hour after the owner wrote: a long outage neither sends it days later nor re-asks it for good', async () => {
  // A record a stop left owed three days ago.
  const file = join(mkdtempSync(join(tmpdir(), 'dsh-channels-owed-')), 'owed.json');
  writeFileSync(file, JSON.stringify({ 'slack\nU-OWNER\na-1': { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-1',
    text: '同意 proposal p-1', at: new Date(Date.now() - 3 * 86400000).toISOString() } }), { mode: 0o600 });
  const s = setup(undefined, undefined, { owedFile: file, ready: false });
  s.host.next = { message_id: 'm', duplicate: true, approval: EXECUTED };
  s.start();
  await s.channels.pollOwed();
  assert.equal(s.slack.sent.length, 0, 'a 3-day-old line is not sent');
  assert.deepEqual(owed(file), []);
  s.channels.stop();
  // A line whose send keeps failing, then three days pass (a channel time ahead of now cannot keep it).
  const realNow = Date.now;
  try {
    const r = brokenSetup();
    r.host.next = { message_id: 'm', duplicate: false, approval: EXECUTED };
    await r.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-2', text: '同意 proposal p-1',
      at: new Date(realNow() + 86400000).toISOString() });
    assert.equal(owed(r.owedFile).length, 1, 'owed while the send fails');
    Date.now = () => realNow() + 3 * 86400000;
    r.broken = false;
    r.host.next = { message_id: 'm', duplicate: true, approval: EXECUTED };
    await r.channels.pollOwed();
    assert.deepEqual(r.slack.sent, [], 'not sent three days after the owner wrote');
    assert.deepEqual(owed(r.owedFile), [], 'and no longer re-asked');
    const asked = only(r.calls, 'relay').length;
    await r.channels.pollOwed();
    assert.equal(only(r.calls, 'relay').length, asked);
    r.channels.stop();
  } finally { Date.now = realNow; }
});

test('an owed decision line goes only to the account bound now: an account unbound or replaced since gets nothing', async () => {
  const s = brokenSetup({ bindings: { slack: 'U-OLD' } });
  s.host.next = { message_id: 'm1', duplicate: false, approval: EXECUTED };
  await s.inbound({ channel: 'slack', senderRef: 'U-OLD', messageRef: 'r-1', text: '同意 proposal p-1', at: RECENT });
  assert.equal(owed(s.owedFile).length, 1, 'the line is owed');
  s.bindings.slack = 'U-NEW';         // the owner rebinds Slack (the old account was taken over)
  s.host.storeText = false;           // the precheck of an unbound sender
  s.host.next = { message_id: 'm1', duplicate: true, approval: EXECUTED };  // the backend replays before the binding check
  s.broken = false;
  await s.channels.pollOwed();
  assert.deepEqual(s.slack.sent, [], 'no decision line to U-OLD');
  assert.deepEqual(owed(s.owedFile), []);
  s.channels.stop();
});

test('a flood of stranger messages during an outage never pushes the owner\'s owed line out of the store', async () => {
  const s = brokenSetup();
  s.host.next = { message_id: 'm', duplicate: false, approval: EXECUTED };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-1', text: '同意 proposal p-1', at: RECENT });
  outage(s);
  for (let i = 0; i < CHANNEL_LIMITS.owedMax; i++)
    await s.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'x-' + i, text: 'spam ' + i });
  assert.ok(owed(s.owedFile).some(m => m.messageRef === 'a-1'), 'the owner\'s line is still owed');
  assert.ok(owed(s.owedFile).length <= CHANNEL_LIMITS.owedMax, 'the store stays bounded');
  s.host.down = false;
  s.broken = false;
  s.host.next = { message_id: 'm', duplicate: true, approval: EXECUTED };
  await s.channels.pollOwed();
  assert.deepEqual(s.slack.sent.filter(line => line.options.to === 'U-OWNER').map(line => line.text), [channelReplies.approval(EXECUTED)]);
  s.channels.stop();
});

test("stop() ends a delivery record's wait between tries: no retry timer outlives the plugin", async () => {
  const s = setup({ defaultChannel: 'slack' }, { recordRetryMs: 60000 });
  s.host.recordDelivery = async delivery => { s.calls.push(['recordDelivery', delivery]); throw new Error('backend down'); };
  const mirrored = s.channels.ownerMessageSent('m-out', 'one');
  while (only(s.calls, 'recordDelivery').length < 1) await new Promise(resolve => setTimeout(resolve, 5));
  s.channels.stop();
  const settled = await Promise.race([mirrored, new Promise(resolve => setTimeout(() => resolve('still waiting'), 200).unref())]);
  assert.deepEqual(settled, { mirrored: true, channel: 'slack' }, 'the wait ended at stop, not after the retry pause');
  assert.equal(only(s.calls, 'recordDelivery').length, 1, 'and no try after stop');
});

test('a recorded delivery leaves the in-process guard: a second call is answered by the delivery row', async () => {
  const s = setup({ defaultChannel: 'slack' });
  assert.deepEqual(await s.channels.ownerMessageSent('m-out', 'one'), { mirrored: true, channel: 'slack' });
  const asked = only(s.calls, 'sourceOf').length;
  assert.deepEqual(await s.channels.ownerMessageSent('m-out', 'one'), { mirrored: false, reason: 'already_mirrored' });
  assert.equal(only(s.calls, 'sourceOf').length, asked + 1, 'asked the host, not a set that only grows');
  assert.equal(s.slack.sent.length, 1);
  s.channels.stop();
});

// The receipt (replies.received): the code's own line that a NEW owner message reached the
// steward, sent at once, before the steward's answer.
const tick = () => new Promise(resolve => setImmediate(resolve));
const RECEIPT = channelReplies.received;

test('receipt: a fresh relay is answered once with the fixed line, on its channel, to the bound account, and nothing reaches the host', async () => {
  assert.equal(RECEIPT, '已收到，正在处理…');
  const s = setup({ bindings: { slack: 'U-OWNER', wechat: 'wx-owner' } });
  assert.deepEqual(await s.inbound({ channel: 'wechat', senderRef: 'wx-owner', messageRef: 'w-1', text: BODY, at: NOW() }), { accepted: true });
  await tick();
  assert.deepEqual(s.wechat.sent, [{ text: RECEIPT, options: { replyToRef: 'w-1', to: 'wx-owner' } }]);
  assert.equal(s.slack.sent.length, 0, 'only on the channel the message came from');
  // Not a steward message: the host is asked exactly what it was asked before the receipt
  // existed (the text check and the relay), and no delivery is recorded for it.
  assert.deepEqual(s.calls.map(([name]) => name), ['codeCheck', 'relay']);
  assert.deepEqual(owed(s.owedFile), [], 'and nothing is owed');
  assert.ok(s.logs.some(([event, fields]) => event === 'channel_receipt_sent' && fields.channel === 'wechat'));
  // The steward's real answer to that message still goes out, mirrored as before.
  s.sources.set('m-in', { direction: 'in', channel: 'wechat', sender_ref: 'wx-owner', channel_message_ref: 'w-1' });
  assert.deepEqual(await s.channels.ownerMessageSent('m-out', 'the answer', 'm-in'), { mirrored: true, channel: 'wechat' });
  assert.deepEqual(s.wechat.sent.map(line => line.text), [RECEIPT, 'the answer']);
  assert.equal(only(s.calls, 'recordDelivery').length, 1, 'only the answer is a delivery');
  s.channels.stop();
});

test('receipt: none for a duplicate, a binding code, a stranger, a decision, a decision still running, or a refusal', async () => {
  const quiet = async (options, next, message, what) => {
    const s = setup(options);
    s.host.next = next;
    await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: BODY, at: NOW(), ...message });
    await tick();
    assert.ok(!s.slack.sent.some(line => line.text === RECEIPT), `${what}: ${JSON.stringify(s.slack.sent)}`);
    s.channels.stop();
    return s;
  };
  await quiet(undefined, { message_id: 'm-in', duplicate: true, approval: null }, {}, 'a redelivery');
  await quiet(undefined, { message_id: null, duplicate: true, approval: null }, { text: '123456' }, 'the owner\'s code replayed');
  const bound = await quiet({ bindings: {} }, undefined, { senderRef: 'U-NEW', text: '123456' }, 'a binding code');
  assert.match(bound.slack.sent[0].text, /^Bound/);
  await quiet(undefined, { refused: 'channel_sender_not_bound' }, { senderRef: 'U-STRANGER' }, 'a stranger');
  const decided = await quiet(undefined, { message_id: 'm', duplicate: false, approval: EXECUTED }, { text: '同意 proposal p-1' }, 'a decision');
  assert.deepEqual(decided.slack.sent.map(line => line.text), [channelReplies.approval(EXECUTED)], 'its own line, at once');
  await quiet(undefined, { duplicate: true, approval: null, approval_pending: true }, {}, 'a decision still running');
  for (const refused of ['body_too_long', 'code_sent_to_bound_account', 'rate_limited'])
    await quiet(undefined, { refused }, {}, refused);
});

test('receipt: at most one per channel within receiptMs, each channel its own window, and again after it', async t => {
  // The clock is the test's: how long the three relays take on a loaded host decides nothing.
  t.mock.timers.enable({ apis: ['Date'], now: Date.now() });
  const s = setup({ bindings: { slack: 'U-OWNER', wechat: 'wx-owner' } });
  for (const ref of ['ts-1', 'ts-2', 'ts-3'])
    assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: ref, text: BODY }), { accepted: true });
  await s.inbound({ channel: 'wechat', senderRef: 'wx-owner', messageRef: 'w-1', text: BODY });
  await tick();
  assert.deepEqual(s.slack.sent.map(line => line.options.replyToRef), ['ts-1'], 'a burst gets one');
  assert.deepEqual(s.wechat.sent.map(line => line.options.replyToRef), ['w-1']);
  assert.equal(s.logs.filter(([event]) => event === 'channel_receipt_skipped').length, 2);
  t.mock.timers.tick(CHANNEL_LIMITS.receiptMs - 1);
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-3b', text: BODY });
  await tick();
  assert.deepEqual(s.slack.sent.map(line => line.options.replyToRef), ['ts-1'], 'still inside the window');
  t.mock.timers.tick(1);
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-4', text: BODY });
  await tick();
  assert.deepEqual(s.slack.sent.map(line => line.options.replyToRef), ['ts-1', 'ts-4']);
  assert.equal(only(s.calls, 'relay').length, 6, 'every message was relayed');
  s.channels.stop();
});

test('receipt: the steward\'s reply sent on a channel ends its window; a failed reply or a receipt does not', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: Date.now() });
  const s = setup();
  const receipts = () => s.slack.sent.filter(line => line.text === RECEIPT).map(line => line.options.replyToRef);
  const owner = async ref => { await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: ref, text: BODY }); await tick(); };
  s.sources.set('m-in', { direction: 'in', channel: 'slack', sender_ref: 'U-OWNER', channel_message_ref: 'ts-1' });
  await owner('ts-1');
  await owner('ts-2');
  assert.deepEqual(receipts(), ['ts-1'], 'a receipt starts the window, it does not end it');
  // The reply fails to go out: the owner has heard nothing new, the window stands.
  s.slack.fail = 'socket closed';
  assert.deepEqual(await s.channels.ownerMessageSent('m-fail', 'lost answer', 'm-in'), { mirrored: false, reason: 'send_failed', channel: 'slack' });
  s.slack.fail = undefined;
  await owner('ts-3');
  assert.deepEqual(receipts(), ['ts-1'], 'a failed reply starts nothing');
  // The reply goes out: the owner's next message, still well inside receiptMs, is news again.
  t.mock.timers.tick(1000);
  assert.deepEqual(await s.channels.ownerMessageSent('m-out', 'the answer', 'm-in'), { mirrored: true, channel: 'slack' });
  await owner('ts-4');
  await owner('ts-5');
  assert.deepEqual(receipts(), ['ts-1', 'ts-4'], 'the reply starts the window again: ts-4 gets one, ts-5 is inside it');
  s.channels.stop();
});

test('receipt: a reply sent on one channel restarts only that channel\'s window', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: Date.now() });
  const s = setup({ bindings: { slack: 'U-OWNER', wechat: 'wx-OWNER' } });
  const got = ch => s[ch].sent.filter(line => line.text === RECEIPT).map(line => line.options.replyToRef);
  const owner = async (channel, senderRef, ref) => { await s.inbound({ channel, senderRef, messageRef: ref, text: BODY }); await tick(); };
  s.sources.set('m-in', { direction: 'in', channel: 'slack', sender_ref: 'U-OWNER', channel_message_ref: 's-1' });
  await owner('slack', 'U-OWNER', 's-1');
  await owner('wechat', 'wx-OWNER', 'w-1');
  assert.deepEqual([got('slack'), got('wechat')], [['s-1'], ['w-1']]);
  t.mock.timers.tick(1000);
  assert.deepEqual(await s.channels.ownerMessageSent('m-out', 'the answer', 'm-in'), { mirrored: true, channel: 'slack' });
  await owner('slack', 'U-OWNER', 's-2');
  await owner('wechat', 'wx-OWNER', 'w-2');
  assert.deepEqual([got('slack'), got('wechat')], [['s-1', 's-2'], ['w-1']], 'the Slack reply leaves the WeChat window standing');
  s.channels.stop();
});

test('receipt: none when it would no longer be news, or when the channel is not registered - both logged', async () => {
  const s = setup();
  const late = new Date(Date.now() - CHANNEL_LIMITS.receiptMaxAgeMs - 1000).toISOString();
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: BODY, at: late }), { accepted: true });
  await tick();
  assert.equal(s.slack.sent.length, 0, 'a message relayed long after it was written');
  assert.ok(s.logs.some(([event, fields]) => event === 'channel_receipt_skipped' && fields.reason === 'late'));
  // The window was not spent by it: the next fresh message still gets its receipt.
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-2', text: BODY, at: new Date().toISOString() });
  await tick();
  assert.deepEqual(s.slack.sent.map(line => line.options.replyToRef), ['ts-2']);
  s.channels.stop();
  const fake = fakeHost(), logs = [];
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]), owedFile: undefined });
  channels.ready();
  assert.deepEqual(await channels.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: BODY }), { accepted: true });
  assert.ok(logs.some(([event, fields]) => event === 'channel_receipt_skipped' && fields.reason === 'not_registered'));
  channels.stop();
});

test('receipt: a send that fails is logged and dropped - no retry, nothing owed, the message still accepted', async () => {
  const fake = fakeHost(), logs = [];
  const file = join(mkdtempSync(join(tmpdir(), 'dsh-channels-owed-')), 'owed.json');
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]), owedFile: file });
  channels.ready();
  const slack = fakeChannel('slack', { fail: 'socket closed' });
  channels.service.register(slack.channel);
  assert.deepEqual(await channels.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: BODY }), { accepted: true });
  await tick();
  assert.equal(slack.sent.length, 1);
  assert.deepEqual(logs.filter(([event]) => event === 'channel_receipt_failed'), [['channel_receipt_failed', { channel: 'slack', error: 'socket closed' }]]);
  assert.ok(!logs.some(([event]) => event === 'channel_answer_failed'), 'not the answer path, which retries');
  assert.deepEqual(owed(file), []);
  await channels.pollOwed();
  await new Promise(resolve => setTimeout(resolve, CHANNEL_LIMITS.answerRetryMs + 50));
  assert.equal(slack.sent.length, 1, 'never tried again');
  channels.stop();
});

test('receipt: a send that never answers holds up neither the message nor the next one', async () => {
  const fake = fakeHost();
  const channels = createOwnerChannels({ host: fake.host, owedFile: undefined });
  channels.ready();
  const sent = [];
  channels.service.register({ name: 'slack', status: () => ({ connected: true }), send: text => { sent.push(text); return new Promise(() => {}); } });
  const answered = await Promise.race([
    Promise.all(['ts-1', 'ts-2'].map(ref => channels.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: ref, text: BODY }))),
    new Promise(resolve => setTimeout(() => resolve('held up'), 200).unref())]);
  assert.deepEqual(answered, [{ accepted: true }, { accepted: true }]);
  assert.deepEqual(sent, [RECEIPT]);
  channels.stop();
});

test('login: a channel may register an optional login; the handle asks it and passes only {requested} or {refused, retry_after_s}', async () => {
  const { channels, reports, wechat, logs } = statusSetup({ limits: { fastStatusMs: 20, fastStatusForMs: 200 } });
  channels.ready();
  try {
    assert.throws(() => channels.service.register({ name: 'slack', status: () => ({}), async send() {}, login: 'yes' }),
      /login, when given, must be a function/);
    assert.deepEqual(await channels.login('wechat'), { refused: 'not_registered' });
    const plain = channels.service.register({ name: 'slack', status: () => ({ connected: true }), async send() { return {}; } });
    assert.deepEqual(await channels.login('slack'), { refused: 'not_supported' }, 'a channel without login');
    plain();
    let answer = { requested: true, qr: 'https://login.weixin.qq.com/l/SECRET', qrSvg: '<svg/>' };
    const asked = [];
    channels.service.register({ name: 'wechat', status: () => wechat.now, async send() { return {}; },
      async login() { asked.push('login'); return answer; } });
    assert.deepEqual(await channels.login('wechat'), { requested: true }, 'nothing but `requested` leaves');
    // Accepted: the status is reported quickly for a while, so the QR shows up in seconds.
    wechat.now = { connected: false, detail: 'scan needed', qrSvg: '<svg xmlns="http://www.w3.org/2000/svg"/>' };
    const until = Date.now() + 2000;
    while (!reports.some(r => r.qrSvg) && Date.now() < until) await new Promise(resolve => setTimeout(resolve, 10));
    assert.ok(reports.some(r => r.qrSvg), 'reported by the fast poll, long before statusMs');
    answer = { refused: 'rate_limited', retry_after_s: 90, qr: 'SECRET' };
    assert.deepEqual(await channels.login('wechat'), { refused: 'rate_limited', retry_after_s: 90 });
    answer = { refused: 'qr_waiting' };
    assert.deepEqual(await channels.login('wechat'), { refused: 'qr_waiting' });
    answer = undefined;
    assert.deepEqual(await channels.login('wechat'), { refused: 'refused' });
    assert.equal(asked.length, 4);
    assert.ok(!JSON.stringify(logs).includes('SECRET') && !JSON.stringify(logs).includes('<svg'));
  } finally { channels.stop(); }
});

test('login: a channel login that throws or never answers is refused as unavailable, never thrown', async () => {
  const { channels } = statusSetup({ limits: { sendMs: 50 } });
  channels.ready();
  try {
    let behave = 'throw';
    channels.service.register({ name: 'wechat', status: () => ({ connected: false }), async send() { return {}; },
      login: () => behave === 'throw' ? Promise.reject(new Error('boom')) : new Promise(() => {}) });
    assert.deepEqual(await channels.login('wechat'), { refused: 'unavailable' });
    behave = 'hang';
    assert.deepEqual(await channels.login('wechat'), { refused: 'unavailable' });
  } finally { channels.stop(); }
});

test('login: past loginPerHour or loginPerDay accepted logins the channel is not asked, whatever a bridge restart forgot', async () => {
  const realNow = Date.now;
  let clock = Date.parse('2026-11-20T12:00:00Z');
  Date.now = () => clock;
  const { channels, logs } = statusSetup({ limits: { loginPerHour: 2, loginPerDay: 3 } });
  try {
    channels.ready();
    let answer = { requested: true };
    const asked = [];
    channels.service.register({ name: 'wechat', status: () => ({ connected: false }), async send() { return {}; },
      async login() { asked.push(clock); return answer; } });
    assert.deepEqual(await channels.login('wechat'), { requested: true });
    clock += 60_000;
    // A refusal of the channel's own is not counted.
    answer = { refused: 'qr_waiting' };
    assert.deepEqual(await channels.login('wechat'), { refused: 'qr_waiting' });
    answer = { requested: true };
    assert.deepEqual(await channels.login('wechat'), { requested: true });
    clock += 60_000;
    // Two in the last hour: the third waits until the first is an hour old (the bridge, just
    // restarted, would have taken it).
    assert.deepEqual(await channels.login('wechat'), { refused: 'rate_limited', retry_after_s: 3600 - 120 });
    assert.equal(asked.length, 3, 'the channel was not asked');
    clock = Date.parse('2026-11-20T13:00:00Z');
    assert.deepEqual(await channels.login('wechat'), { requested: true });
    // Three in 24 hours: the fourth waits until the first is a day old.
    clock = Date.parse('2026-11-20T15:00:00Z');
    assert.deepEqual(await channels.login('wechat'), { refused: 'rate_limited', retry_after_s: 21 * 3600 });
    clock = Date.parse('2026-11-21T12:00:00Z');
    assert.deepEqual(await channels.login('wechat'), { requested: true });
    assert.equal(asked.length, 5);
    assert.ok(logs.some(([event, fields]) => event === 'channel_login_refused' && fields.reason === 'rate_limited'));
  } finally { Date.now = realNow; channels.stop(); }
});
