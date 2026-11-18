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
 *  message body (B3).
 *
 *  Status: when the host can take it (the optional host.channels.reportStatus), each
 *  registered channel's status() - connected, a detail line, and while a WeChat login
 *  needs a scan its QR as an SVG (`qrSvg`) - is handed to the host when it changes: read
 *  when the channel registers, every `statusMs` after, and as disconnected when it is
 *  taken back. The detail and the QR are never logged (the QR is a login token). */

export const CHANNEL_NAMES = ['slack', 'wechat'];

/** What an executed decision was about, in both languages: the host's own record of the
 *  proposal (its kind and target), never the message. An unknown kind reads as itself; a
 *  long target or kind is cut, so the line stays one short line. */
const DECIDED = Object.freeze({
  create_project: t => [`create project ${t}`, `创建 ${t} 项目`],
  start_driver: t => [`start director of ${t}`, `启动 ${t} 项目的导演`],
  stop_driver: t => [`stop director of ${t}`, `停止 ${t} 项目的导演`],
  archive_project: t => [`archive project ${t}`, `归档 ${t} 项目`],
  unarchive_project: t => [`unarchive project ${t}`, `取消归档 ${t} 项目`],
});
const cut = (value, max) => { const s = String(value ?? ''); return s.length > max ? s.slice(0, max - 1) + '…' : s; };
const decided = ({ kind, target }) => {
  if (!text(kind)) return null;
  const t = cut(target, 60), known = Object.hasOwn(DECIDED, kind);
  return known ? DECIDED[kind](t) : Array(2).fill(`${cut(kind, 40)}${t ? ' ' + t : ''}`);
};

/** The lines a sender is answered with on the channel: short, the same in both languages,
 *  never an echo of what they wrote. */
export const channelReplies = Object.freeze({
  bound: 'Bound: this account now talks to the steward. 已绑定：这个账号现在可以和管家对话。',
  bindRefused: 'Not bound: the code is wrong, expired or already used. 未绑定：验证码错误、已过期或已用过。',
  body_too_long: 'Not delivered: the message is too long. 未送达：消息太长。',
  rate_limited: 'Not delivered: too many messages, try again later. 未送达：消息太频繁，请稍后再发。',
  refused: code => `Not delivered (${code}); the dashboard has the conversation. 未送达（${code}），对话在面板上。`,
  approval({ proposal_id: id, action, outcome, refusal, kind, target }) {
    if (outcome === 'executed') {
      const [en, zh] = decided({ kind, target }) ?? [];
      if (action === 'decline') return en ? `proposal ${id}: declined - ${en}. 已拒绝：${zh}。` : `proposal ${id}: declined. 已拒绝。`;
      return en ? `proposal ${id}: approved and done - ${en}. 已同意并执行：${zh}。` : `proposal ${id}: approved and done. 已同意并执行。`;
    }
    if (refusal === 'needs_dashboard_confirmation') return `proposal ${id}: not done - it needs your confirmation on the dashboard. 未执行：这条需要在面板上勾选确认。`;
    return `proposal ${id}: not done (${refusal ?? 'refused'}). 未执行（${refusal ?? 'refused'}）。`;
  },
});

/** Refusals a BOUND sender is told about with their own line; any other refusal gets the
 *  generic `refused` line (a stranger is told none of them). */
const TOLD = new Set(['body_too_long', 'rate_limited']);
const CODE = /^[0-9]{6}$/;
const text = value => typeof value === 'string' && value.length > 0;
const short = error => String(error?.message ?? error).slice(0, 300);
/** `promise`, or a rejection naming `what` after `ms`: a hung channel or host never holds a
 *  queue for good. */
const within = (promise, ms, what) => {
  let timer;
  const late = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(what)), ms); timer.unref?.(); });
  return Promise.race([promise, late]).finally(() => clearTimeout(timer));
};
/** Bounds: a channel's send, the wait for the host, and binding attempts per channel and
 *  minute (each costs a binding read, a bind write and a line through the owner's bot). */
export const CHANNEL_LIMITS = Object.freeze({ sendMs: 30000, readyMs: 120000, bindPerMinute: 5, answeredRefs: 1000, statusMs: 30000,
  statusDetail: 300 });

/** @param host  host.channels: relay, bind, sourceOf, recordDelivery, list, and optionally
 *    reportStatus (host.d.ts).
 *  @param log   (event, fields) => void; fields never carry a body.
 *  @returns { service, ownerMessageSent, ready, pollStatus, stop }: `service` is what channel
 *    plugins get as `ownerChannels`; `ownerMessageSent` is the director's half (DirectorApi);
 *    `ready()` says the host can be called (and starts the status poll); `stop()` ends that
 *    poll. The service exists from the plugin's apply on, so a channel plugin can hand a
 *    message in before the host has installed itself (measured: its caller did not exist
 *    yet); every call to the host waits for ready(). */
export function createOwnerChannels({ host, log = () => {}, replies = channelReplies, limits = CHANNEL_LIMITS }) {
  const registered = new Map(), told = new Map(), binds = new Map(), lanes = new Map(), answeredRefs = new Set();
  let ready;
  const started = new Promise(resolve => { ready = resolve; });
  const hostReady = () => within(started, limits.readyMs, 'host_not_ready');
  // A reply goes out on the channel it is about, at most once per channel message (a
  // redelivered message is not answered again, whatever the host answers the second time);
  // a failure to answer is logged, never thrown into the caller (the message itself was handled).
  const answer = async (channel, line, options) => {
    const entry = registered.get(channel);
    if (!entry) return log('channel_answer_skipped', { channel, reason: 'not_registered' });
    const key = channel + '\n' + options.replyToRef;
    if (answeredRefs.has(key)) return log('channel_answer_skipped', { channel, reason: 'already_answered' });
    answeredRefs.add(key);
    if (answeredRefs.size > limits.answeredRefs) answeredRefs.delete(answeredRefs.values().next().value);
    try { await within(entry.send(line, options), limits.sendMs, 'send_timeout'); }
    catch (error) { log('channel_answer_failed', { channel, error: short(error) }); }
  };
  // One more binding attempt on `channel` within the minute, or false when it has had its share.
  const bindAllowed = channel => {
    const now = Date.now(), window = binds.get(channel);
    if (!window || now - window.start >= 60000) { binds.set(channel, { start: now, count: 1 }); return true; }
    return ++window.count <= limits.bindPerMinute;
  };
  // The channel's current binding, or undefined when it has none.
  const binding = async (channel, signal) => {
    const listed = await host.list(signal);
    return (listed?.channels ?? []).find(row => row.channel === channel && row.bound && !row.revoked_at && row.sender_ref);
  };

  // A channel's status as the host takes it; a status() that throws reads as disconnected.
  const statusOf = entry => {
    let raw;
    try { raw = entry.status(); } catch { raw = { connected: false, detail: 'status unavailable' }; }
    return { connected: raw?.connected === true,
      ...(text(raw?.detail) ? { detail: raw.detail.slice(0, limits.statusDetail) } : {}),
      ...(text(raw?.qrSvg) ? { qrSvg: raw.qrSvg } : {}) };
  };
  // What the host last took per channel. One report at a time; only a change is reported.
  // A refusal is the host's answer to that status (not reported again); a failure to reach
  // it is tried again at the next poll.
  const reported = new Map();
  let statusQueue = Promise.resolve(), statusTimer;
  const reportStatus = (channel, status) => {
    if (typeof host.reportStatus !== 'function') return statusQueue;
    statusQueue = statusQueue.then(async () => {
      const key = JSON.stringify([status.connected, status.detail ?? null, status.qrSvg ?? null]);
      if (reported.get(channel) === key) return;
      try {
        await hostReady();
        const result = await host.reportStatus({ channel, ...status });
        reported.set(channel, key);
        if (result?.refused) log('channel_status_refused', { channel, connected: status.connected, reason: String(result.refused) });
        else log('channel_status_reported', { channel, connected: status.connected });
      } catch (error) { log('channel_status_failed', { channel, error: short(error) }); }
    });
    return statusQueue;
  };
  const pollStatus = () => {
    for (const [channel, entry] of registered) void reportStatus(channel, statusOf(entry));
    return statusQueue;
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
      void reportStatus(channel.name, statusOf(entry));
      return () => {
        if (registered.get(channel.name) !== entry) return;
        registered.delete(channel.name);
        void reportStatus(channel.name, { connected: false, detail: 'channel plugin stopped' });
      };
    },

    /** One message that arrived on a channel. A channel's messages reach the host one at a
     *  time, in the order they were handed in. */
    inbound(message = {}) {
      const { channel, senderRef, messageRef, text: body } = message;
      if (!CHANNEL_NAMES.includes(channel) || !text(senderRef) || !text(messageRef) || typeof body !== 'string') {
        log('channel_inbound_invalid', { channel: String(channel), length: typeof body === 'string' ? body.length : null });
        return Promise.resolve({ accepted: false, reason: 'invalid_inbound' });
      }
      const run = (lanes.get(channel) ?? Promise.resolve()).then(() => handle(message));
      lanes.set(channel, run);
      return run;
    },
  };

  // Never rejects: every failure is an answer.
  const handle = async ({ channel, senderRef, messageRef, text: body, at }) => {
    const seen = { channel, sender_ref: senderRef, at: at ?? new Date().toISOString(), length: body.length };
    const trimmed = body.trim();
    try {
      await hostReady();
      // Six digits from someone who is not the bound owner is a binding attempt; the bound
      // owner's six digits are an ordinary message. Past the channel's binding attempts for
      // the minute, six digits are relayed like any message: the host takes the owner's and
      // refuses a stranger's, silently.
      const attempt = CODE.test(trimmed) && bindAllowed(channel);
      if (CODE.test(trimmed) && !attempt) log('channel_bind_throttled', seen);
      if (attempt && (await binding(channel))?.sender_ref !== senderRef) {
        const result = await host.bind({ channel, senderRef, code: trimmed, messageRef });
        const bound = result?.bound === true;
        // A binding message delivered again (a channel replaying after a restart) was
        // answered the first time: no second line, whatever this process remembers.
        if (result?.duplicate) {
          log('channel_bind_duplicate', seen);
          return { accepted: bound, duplicate: true, ...(bound ? {} : { reason: String(result?.refusal ?? 'refused') }) };
        }
        log(bound ? 'channel_bound' : 'channel_bind_refused', { ...seen, ...(bound ? {} : { reason: result?.refused ?? 'refused' }) });
        await answer(channel, bound ? replies.bound : replies.bindRefused, { replyToRef: messageRef, to: senderRef });
        return bound ? { accepted: true } : { accepted: false, reason: String(result?.refused ?? 'refused') };
      }
      const result = await host.relay({ channel, senderRef, messageRef, text: body, ...(text(at) ? { at } : {}) });
      if (result?.refused) {
        const reason = String(result.refused);
        log('channel_relay_refused', { ...seen, reason });
        // Only the bound owner learns why: the binding is read again, not inferred from the code.
        // At most one such line per channel and minute, so a flood is not answered line for line.
        if (reason !== 'channel_sender_not_bound' && Date.now() - (told.get(channel) ?? 0) >= 60000
          && (await binding(channel))?.sender_ref === senderRef) {
          told.set(channel, Date.now());
          await answer(channel, TOLD.has(reason) ? replies[reason] : replies.refused(reason), { replyToRef: messageRef, to: senderRef });
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
  };

  // One mirror at a time, in the order the messages were written: a channel shows them in
  // the order it receives them.
  let queue = Promise.resolve();
  const mirroring = new Set();
  const mirror = async (messageId, body, replyTo) => {
    await hostReady();
    // Once per message, also across a restart: a delivery row means it was sent (or failed) before.
    if (mirroring.has(messageId) || await host.sourceOf(messageId)) return { mirrored: false, reason: 'already_mirrored' };
    let channel, to, replyToRef;
    if (replyTo) {
      const source = await host.sourceOf(replyTo);
      // A reply to a dashboard message stays on the dashboard.
      if (!source || source.direction !== 'in' || !CHANNEL_NAMES.includes(source.channel)) return { mirrored: false, reason: 'dashboard' };
      channel = source.channel; to = source.sender_ref; replyToRef = source.channel_message_ref ?? undefined;
    }
    const listed = await host.list();
    // Unprompted: the default channel, when the owner chose one.
    if (!replyTo) channel = listed?.default_channel;
    if (!CHANNEL_NAMES.includes(channel)) return { mirrored: false, reason: 'no_default_channel' };
    // Only to the account bound now: one unbound since the owner wrote gets nothing.
    const bound = (listed?.channels ?? []).find(row => row.channel === channel && row.bound && !row.revoked_at && row.sender_ref);
    if (!bound || (to !== undefined && bound.sender_ref !== to)) return { mirrored: false, reason: 'not_bound', channel };
    to = bound.sender_ref;
    mirroring.add(messageId);
    const entry = registered.get(channel);
    let ref, error;
    if (!entry) error = 'channel_not_connected';
    else {
      try { ref = (await within(entry.send(body, { ...(replyToRef ? { replyToRef } : {}), to }), limits.sendMs, 'send_timeout'))?.ref; }
      catch (failure) { error = short(failure); }
    }
    // Recorded once, success or failure; a failed send is never retried.
    const recorded = await host.recordDelivery({ messageId, channel, ...(text(ref) ? { channelMessageRef: ref } : {}), ...(error ? { error } : {}) })
      .catch(failure => log('channel_record_failed', { channel, message_id: messageId, error: short(failure) }));
    if (recorded?.refused) log('channel_record_refused', { channel, message_id: messageId, reason: String(recorded.refused) });
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

  return {
    service, ownerMessageSent,
    ready() {
      ready();
      if (!statusTimer && typeof host.reportStatus === 'function') {
        statusTimer = setInterval(pollStatus, limits.statusMs);
        statusTimer.unref?.();
      }
    },
    /** Every registered channel's status now, reported if it changed (the poll's own step). */
    pollStatus,
    stop: () => { clearInterval(statusTimer); statusTimer = undefined; },
  };
}
