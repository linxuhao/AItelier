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

import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

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
  approval_undated: 'Not done: the message carried no time, so it cannot approve or decline; use the dashboard. 未执行：消息没有时间，不能用来批准或拒绝，请在面板上操作。',
  code_sent_to_bound_account: 'That was a binding code; it is now retired - issue a new one on the dashboard. 这是绑定码，已作废，请在面板重新生成。',
  refused: code => `Not delivered (${code}); the dashboard has the conversation. 未送达（${code}），对话在面板上。`,
  approval({ proposal_id: id, action, outcome, refusal, kind, target, result }) {
    if (outcome === 'executed') {
      const [en, zh] = decided({ kind, target }) ?? [];
      if (action === 'decline') return en ? `proposal ${id}: declined - ${en}. 已拒绝：${zh}。` : `proposal ${id}: declined. 已拒绝。`;
      // An approval whose run failed (the proposal ends 'failed') is not reported as done.
      if (result?.status === 'failed')
        return en ? `proposal ${id}: approved, but it failed - ${en}. 已同意，但执行失败：${zh}。` : `proposal ${id}: approved, but it failed. 已同意，但执行失败。`;
      return en ? `proposal ${id}: approved and done - ${en}. 已同意并执行：${zh}。` : `proposal ${id}: approved and done. 已同意并执行。`;
    }
    if (refusal === 'approval_undated') return `proposal ${id}: not done - the message carried no time, so it cannot decide; use the dashboard. 未执行：消息没有时间，不能用来决定，请在面板上操作。`;
    if (refusal === 'needs_dashboard_confirmation') return `proposal ${id}: not done - it needs your confirmation on the dashboard. 未执行：这条需要在面板上勾选确认。`;
    return `proposal ${id}: not done (${refusal ?? 'refused'}). 未执行（${refusal ?? 'refused'}）。`;
  },
});

/** Refusals a BOUND sender is told about with their own line; any other refusal gets the
 *  generic `refused` line (a stranger is told none of them). A TRANSIENT refusal is not in
 *  here on purpose: the message is held and asked again (RETRYABLE), so "too many messages,
 *  try later" would be a line about a message that is still on its way. */
const TOLD = new Set(['body_too_long', 'approval_undated', 'code_sent_to_bound_account']);
/** The answers that are transient, not terminal: the host or the backend could not be
 *  reached, the channel's minute's share is spent, or the decision this message carries
 *  is still running. The message is held and asked again; it is never dropped. */
const RETRYABLE = new Set(['unavailable', 'rate_limited', 'approval_pending']);
const CODE = /^[0-9]{6}$/;
const text = value => typeof value === 'string' && value.length > 0;
const short = error => String(error?.message ?? error).slice(0, 300);
/** A message is owed while its delivery or the line its decision owes has not been
 *  acknowledged: keyed by the channel message the host takes once. */
const owedKey = ({ channel, senderRef, messageRef }) => `${channel}\n${senderRef}\n${messageRef}`;
/** Where the owed messages are kept across a restart when the caller names no file: the
 *  path $DSH_CHANNEL_OWED_FILE gives, else none - memory only, i.e. a restart loses what was
 *  owed (the director plugin passes the host's own state dir instead). The file holds the
 *  owner's own text verbatim, in mode 0600, and it holds it in exactly the case the backend
 *  has NOT judged it yet (the host was unreachable, threw, or the text was never handed in) -
 *  so a body carrying a live binding code can be here until the host answers. Its bounds:
 *  0600 in the director's own directory, never the backend's world-readable database;
 *  cleared as soon as the host answers; and a binding code expires within minutes, so a
 *  stored one is dead by the time the retry lands. The backend also answers a code-carrying
 *  body BEFORE any transient refusal (its own `_write_relay`), so no transient refusal can
 *  put a live code here at all. */
export function defaultOwedFile(env = process.env) {
  return text(env.DSH_CHANNEL_OWED_FILE) ? env.DSH_CHANNEL_OWED_FILE : undefined;
}
/** `promise`, or a rejection naming `what` after `ms`: a hung channel or host never holds a
 *  queue for good. */
const within = (promise, ms, what) => {
  let timer;
  const late = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(what)), ms); timer.unref?.(); });
  return Promise.race([promise, late]).finally(() => clearTimeout(timer));
};
/** Bounds: a channel's send, the wait for the host, and binding attempts per channel and
 *  minute (each costs a binding read, a bind write and a line through the owner's bot).
 *  A delivery record is tried recordAttempts times, recordRetryMs doubling between tries
 *  (1+2+4+8+16 s: a backend restart); an answer line answerAttempts times, answerRetryMs
 *  between tries; the owed store is re-asked every owedMs, at most owedMax messages. */
export const CHANNEL_LIMITS = Object.freeze({ sendMs: 30000, readyMs: 120000, bindPerMinute: 5, answeredRefs: 1000, statusMs: 30000,
  statusDetail: 300, recordAttempts: 6, recordRetryMs: 1000, answerAttempts: 2, answerRetryMs: 500, owedMs: 30000, owedMax: 200 });

/** @param host  host.channels: relay, bind, sourceOf, recordDelivery, list, and optionally
 *    reportStatus (host.d.ts).
 *  @param log   (event, fields) => void; fields never carry a body.
 *  @param owedFile  where the messages that still owe an answer are kept (defaultOwedFile;
 *    none = this process's memory only).
 *  @returns { service, ownerMessageSent, ready, pollStatus, pollOwed, stop }: `service` is
 *    what channel plugins get as `ownerChannels`; `ownerMessageSent` is the director's half
 *    (DirectorApi); `ready()` says the host can be called (and starts the status poll and
 *    the owed re-asks); `pollOwed()` re-asks every owed message now; `stop()` ends both
 *    timers. The service exists from the plugin's apply on, so a channel plugin can hand a
 *    message in before the host has installed itself (measured: its caller did not exist
 *    yet); every call to the host waits for ready(). */
export function createOwnerChannels({ host, log = () => {}, replies = channelReplies, limits = CHANNEL_LIMITS,
  owedFile = defaultOwedFile() }) {
  const registered = new Map(), told = new Map(), binds = new Map(), lanes = new Map(), answeredRefs = new Set();
  // Channel messages this process handed to the host and got no answer for: whatever the
  // host decided then, no line about it went out.
  const unanswered = new Set();
  // The messages whose delivery, or whose decision's line, is still owed - kept in
  // `owedFile` when there is one, so a restart claims them (see drainOwed). Only a
  // successful answer clears one: a line that could not be sent is owed, not lost.
  const owed = new Map(), answering = new Set();
  // The messages this process is handling right now: the owed poll must not ask one of them
  // again while its relay is still in flight (the write-ahead put it in the store first).
  const handling = new Set();
  const owedSave = () => {
    if (!owedFile) return;
    try {
      mkdirSync(dirname(owedFile), { recursive: true });
      const temporary = `${owedFile}.tmp`;
      writeFileSync(temporary, JSON.stringify(Object.fromEntries(owed)), { mode: 0o600 });
      renameSync(temporary, owedFile);
    } catch (error) { log('channel_owed_unwritable', { error: short(error) }); }
  };
  if (owedFile) {
    try {
      const stored = JSON.parse(readFileSync(owedFile, 'utf8'));
      for (const [key, message] of Object.entries(stored ?? {})) {
        if (message && text(message.channel) && text(message.senderRef) && text(message.messageRef) && typeof message.text === 'string')
          owed.set(key, message);
      }
    } catch (error) {
      if (error?.code !== 'ENOENT') log('channel_owed_unreadable', { error: short(error) });
    }
  }
  const remember = message => {
    const key = owedKey(message);
    owed.set(key, { channel: message.channel, senderRef: message.senderRef, messageRef: message.messageRef,
      text: message.text, ...(text(message.at) ? { at: message.at } : {}) });
    if (owed.size > limits.owedMax) owed.delete(owed.keys().next().value);
    owedSave();
    return key;
  };
  const forget = key => { if (owed.delete(key)) owedSave(); };
  let ready;
  const started = new Promise(resolve => { ready = resolve; });
  const hostReady = () => within(started, limits.readyMs, 'host_not_ready');
  // A reply goes out on the channel it is about, at most once per channel message (a
  // redelivered message is not answered again, whatever the host answers the second time);
  // a failure to answer is logged and tried a bounded number of times, and then REPORTED -
  // what owes the line keeps it owed instead of counting it sent. Never throws into the
  // caller (the message itself was handled).
  const answer = async (channel, line, options) => {
    const entry = registered.get(channel);
    if (!entry) { log('channel_answer_skipped', { channel, reason: 'not_registered' }); return false; }
    const key = channel + '\n' + options.replyToRef;
    if (answeredRefs.has(key)) { log('channel_answer_skipped', { channel, reason: 'already_answered' }); return true; }
    if (answering.has(key)) { log('channel_answer_skipped', { channel, reason: 'already_answering' }); return false; }
    answering.add(key);
    try {
      for (let attempt = 1; ; attempt++) {
        try {
          await within(entry.send(line, options), limits.sendMs, 'send_timeout');
          answeredRefs.add(key);
          while (answeredRefs.size > limits.answeredRefs) answeredRefs.delete(answeredRefs.values().next().value);
          return true;
        } catch (error) {
          log('channel_answer_failed', { channel, attempt, error: short(error) });
          if (attempt >= limits.answerAttempts) return false;
          await new Promise(resolve => setTimeout(resolve, limits.answerRetryMs * attempt));
        }
      }
    } finally { answering.delete(key); }
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
  let statusQueue = Promise.resolve(), statusTimer, owedTimer;
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
    const message = { channel, senderRef, messageRef, text: body, at };
    const owed1 = owedKey(message);
    // Was this message ALREADY owed when it arrived - by an earlier process (a restart) or by
    // an earlier attempt here? That is what "a line the store still says is owed" means; the
    // write-ahead below puts every message in the store, so asking it afterwards would read
    // "yes" for a message whose line went out long ago.
    const carried = owed.has(owed1);
    // Write-ahead: the owe is on disk BEFORE the host is asked. A stop while a relay is in
    // flight (a start/stop_driver decision runs inside it, up to the host's own 30 s) then
    // still leaves the message for the restart to ask again - the whole point of the store.
    // Every path that finishes this message forgets it; nothing else does.
    //
    // A body of exactly six digits is the ONE message that is never written there: that is
    // the binding code's shape - the one secret the owner sends - and a code can never carry
    // an approval (those are phrases), so nothing durable is owed for it. A lost binding line
    // costs the sender retyping the code; a stored one would put a live code on disk for its
    // ten minutes, which is exactly what the backend's code check exists to prevent and what
    // no writer-side check can cover here (the code has to be live on this path).
    const durable = !CODE.test(trimmed);
    if (durable) {
      handling.add(owed1);
      remember(message);
    }
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
          forget(owed1);
          return { accepted: bound, duplicate: true, ...(bound ? {} : { reason: String(result?.refusal ?? 'refused') }) };
        }
        log(bound ? 'channel_bound' : 'channel_bind_refused', { ...seen, ...(bound ? {} : { reason: result?.refused ?? 'refused' }) });
        if (await answer(channel, bound ? replies.bound : replies.bindRefused, { replyToRef: messageRef, to: senderRef }))
          forget(owed1);
        return bound ? { accepted: true } : { accepted: false, reason: String(result?.refused ?? 'refused') };
      }
      const result = await host.relay({ channel, senderRef, messageRef, text: body, ...(text(at) ? { at } : {}) });
      if (result?.refused) {
        const reason = String(result.refused);
        if (RETRYABLE.has(reason)) {
          // Transient (too many messages this minute): still owed from the write-ahead, so
          // the same message is asked again - by this channel's hold, and by a restart.
          log('channel_relay_retrying', { ...seen, reason });
          return { accepted: false, reason };
        }
        // The host's answer for good: nothing is owed for this message any more.
        forget(owed1);
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
      if (result?.approval_pending === true) {
        // The decision this message carries is still running: this is no answer yet, so the
        // line is owed. Retryable, never terminal - the channel asks again, and a restart
        // re-asks from the owed store.
        remember(message);
        unanswered.add(channel + '\n' + messageRef);
        trimUnanswered();
        log('channel_relay_pending', { ...seen, message_id: result.message_id });
        return { accepted: false, reason: 'approval_pending' };
      }
      // A decision is answered once per channel message (answer() keeps that within the
      // process). A redelivery of a message whose line went out is not answered again; one
      // whose line never went out is: this process got no answer the first time, the host
      // says it decided only now (decided_on_replay), or the owed store still says so - so
      // a line a restart inherited is sent too. Only a successful send clears it.
      const key = channel + '\n' + messageRef, lost = unanswered.has(key);
      if (result?.approval || !result?.duplicate) unanswered.delete(key);
      if (result?.approval && (!result.duplicate || lost || result.decided_on_replay === true || carried)) {
        if (await answer(channel, replies.approval(result.approval), { replyToRef: messageRef, to: senderRef })) forget(owed1);
        else remember(message);
      } else {
        forget(owed1);
      }
      return { accepted: true, ...(result?.duplicate ? { duplicate: true } : {}) };
    } catch (error) {
      // No answer: the delivery is owed durably, so a restart asks again instead of the
      // message existing only in this process's memory (a six-digit one excepted, above).
      if (durable) remember(message);
      log('channel_inbound_failed', { ...seen, error: short(error) });
      unanswered.add(channel + '\n' + messageRef);
      trimUnanswered();
      return { accepted: false, reason: 'unavailable' };
    } finally {
      handling.delete(owed1);
    }
  };
  const trimUnanswered = () => {
    while (unanswered.size > limits.answeredRefs) unanswered.delete(unanswered.values().next().value);
  };
  // The messages a stop (or a lost answer) left owed, asked again in the order they were
  // handed in: the line a decision owes goes out, and a message that was refused for the
  // minute survives the restart that follows. One drain at a time; a channel that is not
  // registered, or a host still unreachable, leaves every entry owed for the next drain.
  let owedQueue = Promise.resolve();
  const drainOwed = () => {
    owedQueue = owedQueue.then(async () => {
      if (!owed.size) return;
      for (const message of [...owed.values()]) {
        if (!owed.has(owedKey(message)) || handling.has(owedKey(message))) continue;
        try { await handle(message); }
        catch (error) { log('channel_owed_failed', { channel: message.channel, error: short(error) }); }
      }
    }).catch(error => log('channel_owed_drain_failed', { error: short(error) }));
    return owedQueue;
  };

  // One mirror at a time, in the order the messages were written: a channel shows them in
  // the order it receives them.
  let queue = Promise.resolve();
  const mirroring = new Set();
  // The delivery row is what keeps a message from being sent again after a restart
  // (sourceOf), so a record that fails is tried again, a bounded number of times, as the
  // same request (the host takes it once). A refusal is the host's answer: not retried.
  const record = async delivery => {
    for (let attempt = 1; ; attempt++) {
      try { return await host.recordDelivery(delivery); }
      catch (failure) {
        if (attempt >= limits.recordAttempts) throw failure;
        log('channel_record_retry', { channel: delivery.channel, message_id: delivery.messageId, attempt, error: short(failure) });
        await new Promise(resolve => setTimeout(resolve, limits.recordRetryMs * 2 ** (attempt - 1)));
      }
    }
  };
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
    // Recorded once, success or failure (the record retried, a failed send never).
    const recorded = await record({ messageId, channel, ...(text(ref) ? { channelMessageRef: ref } : {}), ...(error ? { error } : {}) })
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
      if (!owedTimer) {
        void drainOwed();  // what a stop left owed is claimed now, not on the next restart
        owedTimer = setInterval(() => void drainOwed(), limits.owedMs);
        owedTimer.unref?.();
      }
    },
    /** Every registered channel's status now, reported if it changed (the poll's own step). */
    pollStatus,
    /** Every owed message asked again now (what the owedMs poll does on its own). */
    pollOwed: drainOwed,
    stop: () => {
      clearInterval(statusTimer); statusTimer = undefined;
      clearInterval(owedTimer); owedTimer = undefined;
    },
  };
}
