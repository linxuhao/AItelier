/** The owner's chat channels (design-steward 7.2): Slack and WeChat as two more ways for the
 *  owner to talk to the steward. Provided to channel plugins as the cordis service
 *  `ownerChannels`, only when the host declares the `channels` capability (host.d.ts v1.2).
 *
 *  Who decides what: a channel plugin only carries text; the host decides who the owner is
 *  (the binding) and writes what arrives into the owner's conversation; this file connects
 *  the two and nothing more. In particular it never interprets a message - an approval
 *  phrase is matched and executed by the host, in code - and it never sends a channel
 *  anything but the steward's own message to its owner or a one-line answer to the sender
 *  about their own message (B2: a channel is a third party's server).
 *
 *  Answers to a sender who is not the bound owner are deliberately few: a binding attempt
 *  gets one line (bound / not bound), anything else gets nothing, so a stranger learns
 *  nothing from probing (no oracle). Logs carry channel, sender, time and length, never a
 *  message body (B3). */

export const CHANNEL_NAMES = ['slack', 'wechat'];

/** The lines a sender is answered with on the channel: short, the same in both languages,
 *  never an echo of what they wrote. */
export const channelReplies = Object.freeze({
  bound: 'Bound: this account now talks to the steward. 已绑定：这个账号现在可以和管家对话。',
  bindRefused: 'Not bound: the code is wrong, expired or already used. 未绑定：验证码错误、已过期或已用过。',
  body_too_long: 'Not delivered: the message is too long. 未送达：消息太长。',
  rate_limited: 'Not delivered: too many messages, try again later. 未送达：消息太频繁，请稍后再发。',
  approval({ proposal_id: id, action, outcome, refusal }) {
    if (outcome === 'executed') return action === 'decline' ? `proposal ${id}: declined. 已拒绝。` : `proposal ${id}: approved and done. 已同意并执行。`;
    if (refusal === 'needs_dashboard_confirmation') return `proposal ${id}: not done - it needs your confirmation on the dashboard. 未执行：这条需要在面板上勾选确认。`;
    return `proposal ${id}: not done (${refusal ?? 'refused'}). 未执行（${refusal ?? 'refused'}）。`;
  },
});

/** Refusals a BOUND sender is told about (one line each); every other refusal is silent. */
const TOLD = new Set(['body_too_long', 'rate_limited']);
const CODE = /^[0-9]{6}$/;
const text = value => typeof value === 'string' && value.length > 0;
const short = error => String(error?.message ?? error).slice(0, 300);

/** @param host  host.channels: relay, bind, sourceOf, recordDelivery, list (host.d.ts).
 *  @param log   (event, fields) => void; fields never carry a body.
 *  @returns { service, ownerMessageSent }: `service` is what channel plugins get as
 *    `ownerChannels`; `ownerMessageSent` is the director's half (DirectorApi). */
export function createOwnerChannels({ host, log = () => {}, replies = channelReplies }) {
  const registered = new Map(), told = new Map();
  // A reply goes out on the channel it is about; a failure to answer is logged, never thrown
  // into the caller (the message itself was handled).
  const answer = async (channel, line, options) => {
    const entry = registered.get(channel);
    if (!entry) return log('channel_answer_skipped', { channel, reason: 'not_registered' });
    try { await entry.send(line, options); }
    catch (error) { log('channel_answer_failed', { channel, error: short(error) }); }
  };
  // The channel's current binding, or undefined when it has none.
  const binding = async (channel, signal) => {
    const listed = await host.list(signal);
    return (listed?.channels ?? []).find(row => row.channel === channel && row.bound && !row.revoked_at && row.sender_ref);
  };

  const service = {
    /** A channel plugin announces itself; the returned function takes it back. */
    register(channel) {
      if (!channel || !CHANNEL_NAMES.includes(channel.name)) throw new Error(`ownerChannels.register: name must be one of ${CHANNEL_NAMES.join(', ')}`);
      if (typeof channel.send !== 'function' || typeof channel.status !== 'function')
        throw new Error('ownerChannels.register: send and status must be functions');
      if (registered.has(channel.name)) throw new Error(`ownerChannels.register: ${channel.name} is already registered`);
      const entry = { send: channel.send, status: channel.status };
      registered.set(channel.name, entry);
      log('channel_registered', { channel: channel.name });
      return () => { if (registered.get(channel.name) === entry) registered.delete(channel.name); };
    },

    /** One message that arrived on a channel. */
    async inbound({ channel, senderRef, messageRef, text: body, at } = {}) {
      if (!CHANNEL_NAMES.includes(channel) || !text(senderRef) || !text(messageRef) || typeof body !== 'string') {
        log('channel_inbound_invalid', { channel: String(channel), length: typeof body === 'string' ? body.length : null });
        return { accepted: false, reason: 'invalid_inbound' };
      }
      const seen = { channel, sender_ref: senderRef, at: at ?? new Date().toISOString(), length: body.length };
      const trimmed = body.trim();
      try {
        // Six digits from someone who is not the bound owner is a binding attempt; the bound
        // owner's six digits are an ordinary message.
        if (CODE.test(trimmed) && (await binding(channel))?.sender_ref !== senderRef) {
          const result = await host.bind({ channel, senderRef, code: trimmed, messageRef });
          const bound = result?.bound === true;
          log(bound ? 'channel_bound' : 'channel_bind_refused', { ...seen, ...(bound ? {} : { reason: result?.refused ?? 'refused' }) });
          await answer(channel, bound ? replies.bound : replies.bindRefused, { replyToRef: messageRef, to: senderRef });
          return bound ? { accepted: true } : { accepted: false, reason: String(result?.refused ?? 'refused') };
        }
        const result = await host.relay({ channel, senderRef, messageRef, text: body });
        if (result?.refused) {
          const reason = String(result.refused);
          log('channel_relay_refused', { ...seen, reason });
          // Only the bound owner learns why: the binding is read again, not inferred from the code.
          // At most one such line per channel and minute, so a flood is not answered line for line.
          if (TOLD.has(reason) && Date.now() - (told.get(channel) ?? 0) >= 60000 && (await binding(channel))?.sender_ref === senderRef) {
            told.set(channel, Date.now());
            await answer(channel, replies[reason], { replyToRef: messageRef, to: senderRef });
          }
          return { accepted: false, reason };
        }
        // A redelivered channel message was handled the first time, its answer included.
        if (result?.approval && !result.duplicate)
          await answer(channel, replies.approval(result.approval), { replyToRef: messageRef, to: senderRef });
        return { accepted: true, ...(result?.duplicate ? { duplicate: true } : {}) };
      } catch (error) {
        log('channel_inbound_failed', { ...seen, error: short(error) });
        return { accepted: false, reason: 'unavailable' };
      }
    },
  };

  // One mirror at a time, in the order the messages were written: a channel shows them in
  // the order it receives them.
  let queue = Promise.resolve();
  const mirroring = new Set();
  const mirror = async (messageId, body, replyTo) => {
    // Once per message, also across a restart: a delivery row means it was sent (or failed) before.
    if (mirroring.has(messageId) || await host.sourceOf(messageId)) return { mirrored: false, reason: 'already_mirrored' };
    let channel, to, replyToRef;
    const listed = await host.list();
    const rows = listed?.channels ?? [];
    if (replyTo) {
      const source = await host.sourceOf(replyTo);
      // A reply to a dashboard message stays on the dashboard.
      if (!source || source.direction !== 'in' || !CHANNEL_NAMES.includes(source.channel)) return { mirrored: false, reason: 'dashboard' };
      channel = source.channel; to = source.sender_ref; replyToRef = source.channel_message_ref ?? undefined;
    } else {
      // Unprompted: the default channel, when the owner chose one.
      channel = listed?.default_channel;
      if (!CHANNEL_NAMES.includes(channel)) return { mirrored: false, reason: 'no_default_channel' };
    }
    // Only to the account bound now: one unbound since the owner wrote gets nothing.
    const bound = rows.find(row => row.channel === channel && row.bound && !row.revoked_at && row.sender_ref);
    if (!bound || (to !== undefined && bound.sender_ref !== to)) return { mirrored: false, reason: 'not_bound', channel };
    to = bound.sender_ref;
    mirroring.add(messageId);
    const entry = registered.get(channel);
    let ref, error;
    if (!entry) error = 'channel_not_connected';
    else {
      try { ref = (await entry.send(body, { ...(replyToRef ? { replyToRef } : {}), to }))?.ref; }
      catch (failure) { error = short(failure); }
    }
    // Recorded once, success or failure; a failed send is never retried.
    await host.recordDelivery({ messageId, channel, ...(text(ref) ? { channelMessageRef: ref } : {}), ...(error ? { error } : {}) })
      .catch(failure => log('channel_record_failed', { channel, message_id: messageId, error: short(failure) }));
    if (error) log('channel_send_failed', { channel, message_id: messageId, error });
    return error ? { mirrored: false, reason: 'send_failed', channel } : { mirrored: true, channel };
  };

  /** The root wrote `body` to its owner as message `messageId` (a reply to `replyTo` when
   *  given): send the same text where the owner is listening. Never throws. */
  const ownerMessageSent = (messageId, body, replyTo) => {
    if (!text(messageId) || typeof body !== 'string' || !body.trim()) return Promise.resolve({ mirrored: false, reason: 'nothing_to_send' });
    const run = queue.then(() => mirror(messageId, body, text(replyTo) ? replyTo : undefined)).catch(error => {
      log('channel_mirror_failed', { message_id: messageId, error: short(error) });
      return { mirrored: false, reason: 'unavailable' };
    });
    queue = run;
    return run;
  };

  return { service, ownerMessageSent };
}
