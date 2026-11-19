/** Design-steward 7.2: the owner's chat channels as the director connects them - a channel
 *  plugin's message reaches the host's relay or bind, the sender is answered only about its
 *  own message (and a stranger only about a binding attempt), and the root's message to its
 *  owner goes back out on the right channel once. The host here is a fake with the
 *  host.d.ts `channels` shape. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createOwnerChannels, CHANNEL_NAMES, CHANNEL_LIMITS, channelReplies } from '../lib/channels.mjs';
import { validateHost } from '../lib/host.mjs';
import { PROPOSAL_KINDS } from '../lib/proposals.mjs';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BODY = 'SECRET_BODY_TEXT 请帮我看看';
// A channel time a minute ago: a decision line is never sent more than an hour after the owner
// wrote (CHANNEL_LIMITS.lineMaxAgeMs), so a test about a line that IS sent dates its message now.
const RECENT = new Date(Date.now() - 60000).toISOString();
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

/** A fake channel plugin: what it was asked to send. */
function fakeChannel(name, { fail } = {}) {
  const sent = [];
  return { sent, channel: { name, status: () => ({ connected: true }),
    async send(text, options) { sent.push({ text, options }); if (fail) throw new Error(fail); return { ref: `${name}-ref-${sent.length}` }; } } };
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

test('an ordinary message from the bound owner is relayed with its fields, and nothing is answered', async () => {
  const s = setup();
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: BODY, at: '2026-11-18T10:00:00Z' }), { accepted: true });
  // The channel's own time goes along (the host dates an approval phrase by it); none, none.
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-2', text: BODY }), { accepted: true });
  assert.deepEqual(only(s.calls, 'relay'), [{ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: BODY, at: '2026-11-18T10:00:00Z' },
    { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-2', text: BODY }]);
  assert.equal(s.slack.sent.length, 0);
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

/** A backend outage: precheck, relay and the binding read all fail until `down` is false. */
const outage = s => {
  const { relay, codeCheck, list } = s.host;
  s.host.down = true;
  s.host.relay = async message => { if (s.host.down) { s.calls.push(['relay', message]); throw new Error('HTTP 502'); } return relay(message); };
  s.host.codeCheck = async message => { if (s.host.down) throw new Error('HTTP 502'); return codeCheck(message); };
  s.host.list = async () => { if (s.host.down) throw new Error('HTTP 502'); return list(); };
};

for (const [label, text, next, line] of [
  ['an ordinary message', 'please look at the wuxia build', undefined, null],
  ['an approval', '同意 proposal p-1', { message_id: 'm', duplicate: false, approval: EXECUTED }, /^proposal p-1: approved and done/]]) {
  test(`${label} the plugin holds through a backend outage is never answered "send it again", and is delivered once`, async () => {
    const s = setup();
    outage(s);
    const message = { channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text, at: RECENT };
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
    assert.equal(lines.length, line ? 1 : 0, JSON.stringify(lines));
    if (line) assert.match(lines[0], line);
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
