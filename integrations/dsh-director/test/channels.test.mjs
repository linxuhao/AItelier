/** Design-steward 7.2: the owner's chat channels as the director connects them - a channel
 *  plugin's message reaches the host's relay or bind, the sender is answered only about its
 *  own message (and a stranger only about a binding attempt), and the root's message to its
 *  owner goes back out on the right channel once. The host here is a fake with the
 *  host.d.ts `channels` shape. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createOwnerChannels, CHANNEL_NAMES, CHANNEL_LIMITS, channelReplies } from '../lib/channels.mjs';
import { validateHost } from '../lib/host.mjs';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BODY = 'SECRET_BODY_TEXT 请帮我看看';

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

const setup = (options, limits) => {
  const fake = fakeHost(options), logs = [];
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]),
    ...(limits ? { limits: { ...CHANNEL_LIMITS, ...limits } } : {}) });
  channels.ready();
  const slack = fakeChannel('slack'), wechat = fakeChannel('wechat');
  channels.service.register(slack.channel);
  channels.service.register(wechat.channel);
  return { ...fake, channels, logs, slack, wechat, inbound: message => channels.service.inbound(message) };
};
const only = (calls, kind) => calls.filter(([name]) => name === kind).map(([, value]) => value);

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
  assert.deepEqual(only(s.calls, 'relay'), [{ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: BODY }]);
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

test('six digits from the bound owner are an ordinary message; anything but exactly six ASCII digits is too', async () => {
  const s = setup();
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: '123456' });
  for (const text of ['12345', '1234567', '１２３４５６', '123 456', 'abc123'])
    await s.inbound({ channel: 'slack', senderRef: 'U-OTHER', messageRef: 'ts-' + text, text });
  assert.equal(only(s.calls, 'bind').length, 0);
  assert.equal(only(s.calls, 'relay').length, 6);
});

test('refusals: a stranger is never answered; the bound owner is told about too long / too many, at most once a minute', async () => {
  const s = setup();
  for (const refused of ['channel_sender_not_bound', 'body_too_long', 'rate_limited']) {
    s.host.next = { refused };
    assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-STRANGER', messageRef: 'x-' + refused, text: BODY }),
      { accepted: false, reason: refused });
  }
  assert.equal(s.slack.sent.length, 0, 'no oracle for strangers');
  s.host.next = { refused: 'body_too_long' };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-1', text: 'x'.repeat(5000) });
  assert.equal(s.slack.sent.length, 1);
  assert.match(s.slack.sent[0].text, /too long/);
  assert.deepEqual(s.slack.sent[0].options, { replyToRef: 'o-1', to: 'U-OWNER' });
  s.host.next = { refused: 'rate_limited' };
  await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'o-2', text: 'hi' });
  assert.equal(s.slack.sent.length, 1, 'a second line within the minute is not sent');
  // On a channel it is not bound to, U-OWNER is a stranger: silent.
  s.host.next = { refused: 'invalid_request' };
  await s.inbound({ channel: 'wechat', senderRef: 'U-OWNER', messageRef: 'o-3', text: 'hi' });
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

test('an approval the host executed is answered with one line of its outcome; a redelivered message is not answered again', async () => {
  const s = setup();
  const approval = (action, outcome, refusal) => ({ message_id: 'm', duplicate: false,
    approval: { proposal_id: 'p-1', action, outcome, ...(refusal ? { refusal } : {}) } });
  const lines = [];
  for (const result of [approval('approve', 'executed'), approval('decline', 'executed'), approval('approve', 'refused', 'needs_dashboard_confirmation'),
    approval('approve', 'refused', 'channel_sender_not_bound')]) {
    s.host.next = result;
    await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-' + lines.length, text: '同意 proposal p-1' });
    lines.push(s.slack.sent.at(-1).text);
  }
  assert.match(lines[0], /proposal p-1: approved and done/);
  assert.match(lines[1], /proposal p-1: declined/);
  assert.match(lines[2], /dashboard/);
  assert.match(lines[3], /not done \(channel_sender_not_bound\)/);
  s.host.next = { ...approval('approve', 'executed'), duplicate: true };
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-0', text: '同意 proposal p-1' }),
    { accepted: true, duplicate: true });
  assert.equal(s.slack.sent.length, 4);
  // The backend replays a redelivery under the same request key as the first result,
  // duplicate: false included: the message is still answered only once.
  s.host.next = approval('approve', 'executed');
  assert.deepEqual(await s.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'a-1', text: '同意 proposal p-1' }), { accepted: true });
  assert.equal(s.slack.sent.length, 4);
  // The same ref on another channel is another message.
  const w = setup({ bindings: { wechat: 'U-OWNER' } });
  w.host.next = approval('approve', 'executed');
  await w.inbound({ channel: 'wechat', senderRef: 'U-OWNER', messageRef: 'a-1', text: '同意 proposal p-1' });
  assert.equal(w.wechat.sent.length, 1);
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
  const channels = createOwnerChannels({ host: fake.host, log: (event, fields) => logs.push([event, fields]) });
  channels.ready();
  const slack = fakeChannel('slack');
  channels.service.register(slack.channel);
  assert.deepEqual(await channels.ownerMessageSent('m-out', 'answer', 'm-in'), { mirrored: true, channel: 'slack' });
  assert.deepEqual(await channels.ownerMessageSent('m-out', 'answer', 'm-in'), { mirrored: false, reason: 'already_mirrored' });
  assert.equal(slack.sent.length, 1);
  assert.deepEqual(logs.filter(([event]) => event === 'channel_record_failed').map(([, f]) => f.message_id), ['m-out']);
  // A delivery the host refuses to record is logged by its code.
  fake.host.recordDelivery = async () => ({ refused: 'delivery_recorded' });
  await channels.ownerMessageSent('m-out-2', 'answer', 'm-in');
  assert.deepEqual(logs.filter(([event]) => event === 'channel_record_refused').map(([, f]) => [f.message_id, f.reason]), [['m-out-2', 'delivery_recorded']]);
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
