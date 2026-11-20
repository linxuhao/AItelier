/** The owner's chat channels (design-steward 7.2): Slack and WeChat as two more ways for the
 *  owner to talk to the steward. Provided to channel plugins as the cordis service
 *  `ownerChannels`, only when the host declares the `channels` capability (host.d.ts v1.2).
 *
 *  Who decides what: a channel plugin only carries text; the host decides who the owner is
 *  (the binding) and writes what arrives into the owner's conversation; this file connects
 *  the two and nothing more. In particular it never interprets a message - an approval
 *  phrase is matched and executed by the host, in code - and it never sends a channel
 *  anything but the steward's own message to its owner or a one-line answer to the sender
 *  about their own message (B2: a channel is a third party's server). One such line is the
 *  receipt (replies.received): when the host has just taken a NEW owner message into the
 *  conversation, this code - never the model - tells the sender at once that it arrived,
 *  at most once per channel and limits.receiptMs, best effort (see receipt()).
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
  received: '已收到，正在处理…',
  bindRefused: 'Not bound: the code is wrong, expired or already used. 未绑定：验证码错误、已过期或已用过。',
  unknowable: 'Not delivered, and it cannot be retried: please send it again. 未送达，且无法重试：请再发一次。',
  body_too_long: 'Not delivered: the message is too long. 未送达：消息太长。',
  approval_undated: 'Not done: the message carried no time, so it cannot approve or decline; use the dashboard. 未执行：消息没有时间，不能用来批准或拒绝，请在面板上操作。',
  code_sent_to_bound_account: 'That was a binding code; it is now retired - issue a new one on the dashboard. 这是绑定码，已作废，请在面板重新生成。',
  rate_limited: 'Held: too many messages at once; they reach the steward in order, a little later - no need to send them again. 消息太频繁：已留住，稍后按顺序送达管家，不必重发。',
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
 *  here: the message is held and asked again (RETRYABLE). For rate_limited the owner is told
 *  exactly that, once a minute (replies.rate_limited: "Held ... no need to send them again"),
 *  so a burst still on its way is not sent again; that line has its own key, so the
 *  message's real answer (an approval's line) is still owed. Every one of these lines is owed
 *  until it has actually gone out: a send that fails is no line, so the owed store keeps the
 *  reason and the drain says it later (see the refusal path in handleOnce). */
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
 *  owed (the director plugin passes the host's own state dir instead). The file is mode 0600
 *  and it holds a body ONLY once the host has said it may: channels.codeCheck answers with
 *  the rule the relay itself applies (a bound sender, and no live binding code anywhere in
 *  the text, in every rendering the backend folds), so the client never carries a digit rule
 *  of its own. A digit rule cannot do this job - measured both ways against this tree: six
 *  digits in a row refuse an approval whose proposal id carries one (about 38% of them do,
 *  and those lines were lost outright), and \\p{Nd}{6} misses every folded rendering
 *  (superscripts, circled, dingbat: 99 code points) so a live code was written down verbatim.
 *  The fact that a message is owed is written BEFORE any host call, so nothing is lost either
 *  way; the text joins it at the first answer, and a stop before that answer costs the sender
 *  one "send it again" line instead of a live code on disk. */
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
 *  between tries; the owed store is re-asked every owedMs, at most owedMax messages; a decision
 *  line is never sent more than lineMaxAgeMs after the owner wrote it (the earlier of the
 *  channel's time and when it was first owed here): later, the dashboard alone shows it. */
export const CHANNEL_LIMITS = Object.freeze({ sendMs: 30000, readyMs: 120000, bindPerMinute: 5, answeredRefs: 1000, statusMs: 30000,
  statusDetail: 300, recordAttempts: 6, recordRetryMs: 1000, answerAttempts: 2, answerRetryMs: 500, owedMs: 30000, owedMax: 200,
  lineMaxAgeMs: 3600000, receiptMs: 30000, receiptMaxAgeMs: 300000 });

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
  const registered = new Map(), told = new Map(), sending = new Map(), binds = new Map(), lanes = new Map(),
    answeredRefs = new Set();
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
  // The messages whose last answer here was TRANSIENT (unavailable, rate_limited,
  // approval_pending): the channel plugin holds each and hands it in again. A record of one
  // without a text is not a body lost to a restart - the plugin still has it - so the owed
  // poll leaves it to that retry instead of telling the owner to send it again.
  const held = new Set();
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
        // A record without a text is a real one: it says a message is owed whose body was
        // never written down because the host had not judged it (see remember()).
        if (message && text(message.channel) && text(message.senderRef) && text(message.messageRef)
          && (message.text === undefined || typeof message.text === 'string'))
          owed.set(key, message);
      }
    } catch (error) {
      if (error?.code !== 'ENOENT') log('channel_owed_unreadable', { error: short(error) });
    }
  }
  // `judged` says the HOST has allowed this text to be kept: channels.codeCheck said so, or
  // the host answered the relay itself without refusing it for its text (its code check runs
  // before every decision and before the rate limit). Only then is the body written down. No
  // digit rule of this side's own may stand in for that answer: six digits in a row refuse an
  // approval whose proposal id carries one (about 38% of real ids, and their line was lost),
  // and the folded renderings a \\p{Nd} rule misses were written down and then refused by the
  // backend as the live code. Until the host allows it the record keeps the fact of the debt
  // only: a stop in that window costs the sender one "send it again" line.
  // The host's own answer to "may this text be kept" (channels.codeCheck, host.d.ts): the
  // plugin writes the owner's words down before the relay so a timed-out answer is replayed,
  // and must never write a live binding code - and only the host knows which code is live.
  // An answer we do not get is no permission: no codeCheck (an older host) or a refusal -
  // the message stays owed without its text. A codeCheck that is not ANSWERED (a transport
  // failure, a 5xx, a timeout) throws: the message is then not relayed either, and is answered
  // `unavailable` for the plugin to hold and hand in again. A relay without a verdict could
  // commit (and execute an approval) while the record had no text, and a restart could then
  // only tell the owner "send it again" about a message that was taken - and lose its line.
  let codeCheckMissing = false;
  const mayKeepText = async message => {
    if (typeof host.codeCheck !== 'function') {
      if (!codeCheckMissing) { codeCheckMissing = true; log('channel_code_check_absent', {}); }
      return false;
    }
    try {
      const verdict = await host.codeCheck({ channel: message.channel, senderRef: message.senderRef,
        messageRef: message.messageRef, text: message.text });
      return verdict?.store_text === true;
    } catch (error) {
      log('channel_code_check_failed', { channel: message.channel, error: short(error) });
      throw error;
    }
  };
  const remember = (message, { judged = false, refusal, throttled } = {}) => {
    const key = owedKey(message);
    const previous = owed.get(key);
    const body = typeof message.text === 'string' ? message.text : undefined;
    const kept = judged ? body : (typeof previous?.text === 'string' ? previous.text : undefined);
    // What this record owes besides its delivery: the line that says why the host refused this
    // message for good. It is the host's own reason, kept as it came, and it is written before
    // the line's send begins - so a stop inside that send still owes it. What the record holds
    // besides it is the body the host allowed to be kept (`kept` above) and nothing else; that
    // body is never relayed again, because a message refused for good only owes this one line.
    const pending = text(refusal) ? refusal : (text(previous?.refusal) ? previous.refusal : undefined);
    // `throttled`: this record owes NO line of its own - when the host refused the message,
    // another record already owed the channel's one refusal line for that minute-key, so that
    // pending line carries the channel's news and this record keeps the REASON only. Whether a
    // line is owed is a separate fact from what the reason was (`refusal` above), because a
    // stop between the two used to leave a record the drain could only read as a body the host
    // never judged. Absent means the record owes its line, which is also what a record written
    // by an earlier version means: one with a reason owes that line until it goes out; an
    // explicit false clears the mark when the line turns out to be owed after all.
    const noLine = throttled === undefined ? previous?.throttled === true : throttled === true;
    // `owedAt`: when this message was first owed here (kept across a restart) - with the
    // channel's `at`, what dates the line it may owe (lineExpired).
    owed.set(key, { channel: message.channel, senderRef: message.senderRef, messageRef: message.messageRef,
      ...(kept === undefined ? {} : { text: kept }), ...(pending === undefined ? {} : { refusal: pending }),
      ...(noLine ? { throttled: true } : {}),
      ...(text(message.at) ? { at: message.at } : {}),
      owedAt: text(previous?.owedAt) ? previous.owedAt : new Date().toISOString() });
    // Past the bound, a record WITHOUT a text goes first (it can owe no replayable line, only
    // a "send it again" notice; a stranger's message is always one), so a flood never pushes
    // out the owner's owed line. Only a store of owner texts alone loses its oldest. Such a
    // record counts against the bound like any other - the drain now keeps one owed until its
    // notice went out, so it may hold its slot for good; being counted, and being the victim
    // chosen first here, is what keeps it from ever costing the owner a texted line.
    if (owed.size > limits.owedMax) {
      let victim = owed.keys().next().value;
      for (const [candidate, record] of owed) if (typeof record.text !== 'string') { victim = candidate; break; }
      owed.delete(victim);
    }
    owedSave();
    return key;
  };
  // A decision line is never sent more than lineMaxAgeMs after the owner wrote: the earlier
  // of the channel's own time and when it was first owed here (so a skewed channel time
  // cannot keep it). A ring replay of a decision made yesterday, or a line owed through a long
  // outage, is not announced as news - and its record is done (handle), so it is not re-asked
  // every owedMs for good. A message still owed its DELIVERY has no such bound: it is relayed
  // whenever the host takes it (the host dates an approval phrase itself).
  const olderThan = (record, ms) => {
    const born = Math.min(...[record?.at, record?.owedAt].filter(text).map(Date.parse).filter(Number.isFinite));
    return Number.isFinite(born) && Date.now() - born > ms;
  };
  const lineExpired = record => olderThan(record, limits.lineMaxAgeMs);
  const forget = key => { if (owed.delete(key)) owedSave(); };
  // Which minute a refusal line belongs to: the binding-code line has a minute of its own, so
  // no other line ever holds back the news that a code was retired.
  const refusalThrottle = (channel, reason) => reason === 'code_sent_to_bound_account' ? channel + '\ncode' : channel;
  // How many records already owe the channel's one refusal line for that same minute, i.e. how
  // many of the slot this channel has to hold. A refusal that arrives while one is still owed
  // is not owed a line too: the throttle wins, exactly as it does today, so keeping records on
  // a spent minute cannot turn a flood of refused messages into one line each. Cheap by
  // construction: the store is bounded (limits.owedMax) and only a refusal record carries
  // `refusal`. One per minute-key, not per channel: the code line keeps its own minute, so it
  // keeps its own pending slot - a held-back body_too_long must not swallow it.
  // The slot belongs to the SENDER the line would go to, so it is counted for that sender only:
  // a record left by someone else - a stranger's, or one from the account bound before the owner
  // rebound - never holds the bound owner's slot. The drain reads the binding and drops such a
  // record as not_bound, so a slot it held would leave the owner told nothing at all. A record
  // that owes no line of its own (`throttled`) is a reason, not a line, and does not count.
  const pendingRefusals = (key, throttle, senderRef) => {
    let count = 0;
    for (const [other, record] of owed)
      if (other !== key && record.throttled !== true && record.senderRef === senderRef
        && text(record.refusal) && refusalThrottle(record.channel, record.refusal) === throttle) count++;
    return count;
  };
  let ready;
  const started = new Promise(resolve => { ready = resolve; });
  const hostReady = () => within(started, limits.readyMs, 'host_not_ready');
  // A reply goes out on the channel it is about, at most once per channel message (a
  // redelivered message is not answered again, whatever the host answers the second time);
  // a failure to answer is logged and tried a bounded number of times, and then REPORTED -
  // what owes the line keeps it owed instead of counting it sent. Never throws into the
  // caller (the message itself was handled).
  // `key` is what makes it once: the channel message, unless the caller names another (the
  // Held line of a message that will still be delivered must not use up its answer).
  const answer = async (channel, line, options, key = channel + '\n' + options.replyToRef) => {
    const entry = registered.get(channel);
    if (!entry) { log('channel_answer_skipped', { channel, reason: 'not_registered' }); return false; }
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
  // A line to the sender at most once per throttle key and minute, and the minute is spent
  // only by a line that actually went out: a channel whose send is failing right now (a plugin
  // reconnecting, a socket dropped) neither loses this line nor silences the next one in the
  // same minute. The minute is claimed while the send is in flight (two lines never race into
  // one minute) and given back when the send fails, so the caller's next attempt may use it.
  // Answers 'sent', 'failed' (nothing was said, nothing spent), 'held' (this key's line for this
  // minute already went out, so the minute is spent) or 'inflight' (the line that spent it is
  // still being sent, so it may yet fail and give the minute back - a caller that defers to it
  // must be able to tell the two apart, or it loses its line to a minute that was never used).
  const tellThrottled = async (channel, throttle, line, options, key) => {
    const spentAt = told.get(throttle);
    if (Date.now() - (spentAt ?? 0) < 60000) return (sending.get(throttle) ?? 0) > 0 ? 'inflight' : 'held';
    told.set(throttle, Date.now());
    sending.set(throttle, (sending.get(throttle) ?? 0) + 1);
    let sent = false;
    try { sent = await answer(channel, line, options, key); }
    finally {
      const running = (sending.get(throttle) ?? 1) - 1;
      if (running > 0) sending.set(throttle, running); else sending.delete(throttle);
      // The minute goes back only when nothing is left running under it (a send can outlast its
      // minute): then no line was said and none is being said.
      if (!sent && running === 0) {
        if (spentAt === undefined) told.delete(throttle); else told.set(throttle, spentAt);
      }
    }
    return sent ? 'sent' : 'failed';
  };
  // One more binding attempt on `channel` within the minute, or false when it has had its share.
  const bindAllowed = channel => {
    const now = Date.now(), window = binds.get(channel);
    if (!window || now - window.start >= 60000) { binds.set(channel, { start: now, count: 1 }); return true; }
    return ++window.count <= limits.bindPerMinute;
  };
  // The receipt: when the host has just taken a NEW owner message (a relay that is no
  // duplicate and carries no decision - a decision already gets its own line at once), the
  // sender hears at once that it arrived, before the steward's answer, which takes a model turn.
  // It is this code's fixed line, sent straight to the channel: nothing about it reaches the
  // host, so it is no steward message, wakes nothing, answers nothing (the unanswered-owner
  // check and the mirror never see it) and is not remembered. At most one per channel and
  // receiptMs, the window spent by the attempt, so a burst gets one (the window is this
  // process's memory: a restart inside it may let one more through). Only while it is news:
  // a message relayed more than receiptMaxAgeMs after the owner wrote (held through an
  // outage, drained from the owed store) gets none. Best effort: not
  // awaited (the channel's next message is not held up by a slow send), a failure is
  // logged and dropped - no retry, nothing owed.
  const receipts = new Map();
  const receipt = (channel, senderRef, messageRef, record) => {
    const entry = registered.get(channel);
    if (!entry) { log('channel_receipt_skipped', { channel, reason: 'not_registered' }); return; }
    if (olderThan(record, limits.receiptMaxAgeMs)) { log('channel_receipt_skipped', { channel, reason: 'late' }); return; }
    const last = receipts.get(channel);
    if (last !== undefined && Date.now() - last < limits.receiptMs) { log('channel_receipt_skipped', { channel, reason: 'window' }); return; }
    receipts.set(channel, Date.now());
    let sending;
    try { sending = entry.send(replies.received, { replyToRef: messageRef, to: senderRef }); }
    catch (error) { sending = Promise.reject(error); }
    within(Promise.resolve(sending), limits.sendMs, 'send_timeout').then(
      () => log('channel_receipt_sent', { channel }),
      error => log('channel_receipt_failed', { channel, error: short(error) }));
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

  // Never rejects: every failure is an answer. A transient answer marks the message `held`
  // (the plugin hands it in again); any other answer ends that.
  const handle = async message => {
    const result = await handleOnce(message), key = owedKey(message);
    if (result?.accepted === false && RETRYABLE.has(result.reason)) {
      held.add(key);
      while (held.size > limits.answeredRefs) held.delete(held.values().next().value);
    } else held.delete(key);
    return result;
  };
  const handleOnce = async ({ channel, senderRef, messageRef, text: body, at }) => {
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
    // The body itself joins the record only once the host has answered it (remember's
    // `judged`); every path that finishes this message forgets it, and nothing else does.
    handling.add(owed1);
    // The debt is written before ANY host call, and the text follows the host's own verdict.
    remember(message);
    try {
      await hostReady();
      let keep;
      try { keep = await mayKeepText(message); }
      catch { return { accepted: false, reason: 'unavailable' }; }  // no verdict: not relayed (mayKeepText)
      if (keep) remember(message, { judged: true });
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
          // A rate_limited answer means the host DID read this body - its code check runs
          // before the rate limit - so the body may join the record. `unavailable` is this
          // side's own name for an answer that never came, and it joins nothing.
          if (reason === 'rate_limited') remember(message, { judged: true });
          log('channel_relay_retrying', { ...seen, reason });
          // rate_limited only ever answers the bound owner (the backend checks the binding
          // first): told once a minute that the burst is held, so it is not sent again. The
          // line has its own key: the message's own answer is still to come.
          if (reason === 'rate_limited' && Date.now() - (told.get(channel) ?? 0) >= 60000
            && (await binding(channel))?.sender_ref === senderRef) {
            // A Held line the channel could not send spends nothing: this message's next
            // delivery (the plugin holds it, the owed store keeps it) tries the line again,
            // still inside the same minute if the channel is back by then.
            await tellThrottled(channel, channel, replies.rate_limited, { replyToRef: messageRef, to: senderRef },
              channel + '\nheld\n' + messageRef);
          }
          return { accepted: false, reason };
        }
        // The host's answer for good: nothing more is relayed for this message - but the line
        // that says WHY is owed until it has actually gone out. A send failing at this moment
        // (a plugin reconnecting, a socket dropped) would otherwise cost the owner the reason
        // for THIS message and, the minute being spent before the send, silence the next one in
        // the same minute too. So the reason joins the record (its refusal field) BEFORE the
        // send begins - a send can take two 30 s timeouts plus the retry wait, and a stop inside
        // that window must still leave the line owed - and the owed store's drain sends it, to
        // the account still bound then, on a later tick.
        log('channel_relay_refused', { ...seen, reason });
        // Only the bound owner learns why: the binding is read again, not inferred from the code.
        // At most one such line per channel and minute, so a flood is not answered line for line.
        // The binding-code line has a minute of its own: no other line ever holds back the
        // news that a code was retired.
        const throttle = refusalThrottle(channel, reason);
        // A stranger's message owes no line at all (channel_sender_not_bound, or an account not
        // bound now): the record goes. For an owner's message the reason is written FIRST -
        // unconditionally, whatever another line is doing, and before the binding read and the
        // send - so a stop inside that window still leaves a record that says WHY it is owed: the
        // drain sends that line to the account bound then. Written any later, the record could
        // only be read as a message the host never judged, and the restart would tell the owner to
        // send a refused message again (a live binding code among them) or relay a body the host
        // refused for good (see the drain, and the comment this replaced, which claimed a window
        // it did not close). The drain reads the binding itself and drops a record whose sender is
        // not the owner then, which is what keeps a stranger from ever being told.
        if (reason === 'channel_sender_not_bound') forget(owed1);
        else {
          // One pending refusal line per minute-key, for this sender: while the channel already
          // owes one, this message is not owed a line too - the throttle wins (a flood is not
          // answered line for line). The record still carries the reason, marked `throttled`
          // (that it owes no line of its own): the pending line says the channel's news.
          const pending = pendingRefusals(owed1, throttle, senderRef);
          if (pending) log('channel_refusal_throttled', { ...seen, reason });
          remember(message, { refusal: reason, throttled: pending > 0 });
          if ((await binding(channel))?.sender_ref !== senderRef) forget(owed1);
          else {
            const sent = await tellThrottled(channel, throttle, TOLD.has(reason) ? replies[reason] : replies.refused(reason),
              { replyToRef: messageRef, to: senderRef });
            // Forgotten only when the line actually went out, or when it owes no line of its own
            // and its reason is on the record (the pending line carries the channel's news). Not
            // sent because the channel could not (nothing was said and nothing was spent), or
            // because another line spent this minute, leaves the line owed for a later drain tick.
            // The exception is 'inflight': the line it deferred to is still being sent, and that
            // send failing gives the minute back - dropping the record here is how a line was lost
            // to a minute that was never used, so it keeps a line of its own instead. One such
            // kept record per minute-key: a flood is not answered line for line, even inside that
            // window.
            if (sent === 'sent') forget(owed1);
            else if (!pending) log('channel_refusal_owed', { ...seen, reason, outcome: sent });
            else if (sent !== 'inflight' || pending > 1) forget(owed1);
            else {
              // It owes its line after all: the reason stays, the `throttled` mark goes.
              remember(message, { refusal: reason, throttled: false });
              log('channel_refusal_owed', { ...seen, reason, outcome: sent });
            }
          }
        }
        return { accepted: false, reason };
      }
      if (result?.approval_pending === true) {
        // The decision this message carries is still running: this is no answer yet, so the
        // line is owed. Retryable, never terminal - the channel asks again, and a restart
        // re-asks from the owed store.
        remember(message, { judged: true });
        unanswered.add(channel + '\n' + messageRef);
        trimUnanswered();
        log('channel_relay_pending', { ...seen, message_id: result.message_id });
        return { accepted: false, reason: 'approval_pending' };
      }
      // A new owner message the steward will now answer: the receipt (relay accepted means the
      // host found the sender bound to this channel now).
      if (!result?.duplicate && !result?.approval) receipt(channel, senderRef, messageRef, { at, owedAt: owed.get(owed1)?.owedAt });
      // A decision is answered once per channel message (answer() keeps that within the
      // process). A redelivery of a message whose line went out is not answered again; one
      // whose line never went out is: this process got no answer the first time, the host
      // says it decided only now (decided_on_replay), or the owed store still says so - so
      // a line a restart inherited is sent too. Only a successful send clears it.
      const key = channel + '\n' + messageRef, lost = unanswered.has(key);
      if (result?.approval || !result?.duplicate) unanswered.delete(key);
      if (result?.approval && (!result.duplicate || lost || result.decided_on_replay === true || carried)) {
        // A redelivery's line goes only to the account bound NOW (the backend replays a
        // channel message's answer before it checks the binding), and never long after the
        // owner wrote: an unbound account never learns the decision, and a decision made
        // yesterday is not announced again by a ring replay that met one transient failure.
        const expired = result.duplicate && lineExpired({ at, owedAt: owed.get(owed1)?.owedAt });
        if (expired || (result.duplicate && (await binding(channel))?.sender_ref !== senderRef)) {
          log('channel_line_dropped', { ...seen, reason: expired ? 'expired' : 'not_bound' });
          forget(owed1);
        } else if (await answer(channel, replies.approval(result.approval), { replyToRef: messageRef, to: senderRef })) forget(owed1);
        else remember(message, { judged: true });
      } else {
        forget(owed1);
      }
      return { accepted: true, ...(result?.duplicate ? { duplicate: true } : {}) };
    } catch (error) {
      // No answer: the delivery is owed durably, so a restart asks again instead of the
      // message existing only in this process's memory. No answer is no judgement, so the
      // body is not added here (remember keeps one an earlier answer allowed).
      remember(message);
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
        // A refusal line that could not be sent when the message was refused for good: the
        // record holds the host's reason (and, when the host allowed it, the body - that body
        // is never relayed again, so there is nothing to relay, only that same line to send),
        // to the account bound NOW. It keeps the refusal's own minute: a stranger is told
        // nothing, and the record is done only once the line actually went out - a minute
        // another line spent, or a send that failed, leaves it owed for a later tick.
        if (text(message.refusal)) {
          // A record that owes no line of its own (`throttled`): another record already owed the
          // channel's one refusal line for this minute-key when this message was refused, so its
          // reason is on the record only so that a stop inside that send cannot leave a record the
          // drain has to read as a body the host never judged. Its line was never owed - the line
          // already pending carries the channel's news - so there is nothing to send and nothing
          // to hold, and it is done here rather than counting against the store for good.
          if (message.throttled === true) {
            log('channel_owed_dropped', { channel: message.channel, reason: 'throttled' });
            forget(owedKey(message));
            continue;
          }
          let owner;
          try { owner = (await binding(message.channel))?.sender_ref === message.senderRef; }
          catch (error) {
            log('channel_owed_failed', { channel: message.channel, error: short(error) });
            continue;
          }
          if (!owner) {
            log('channel_owed_dropped', { channel: message.channel, reason: 'not_bound' });
            forget(owedKey(message));
            continue;
          }
          const throttle = refusalThrottle(message.channel, message.refusal);
          const sent = await tellThrottled(message.channel, throttle,
            TOLD.has(message.refusal) ? replies[message.refusal] : replies.refused(message.refusal),
            { replyToRef: message.messageRef, to: message.senderRef });
          if (sent === 'sent') forget(owedKey(message));
          else log('channel_owed_held', { channel: message.channel, reason: sent === 'held' ? 'throttled' : 'not_sent' });
          continue;
        }
        // A plugin still holds it and hands it in again: not a body lost to a restart.
        if (typeof message.text !== 'string' && held.has(owedKey(message))) continue;
        if (typeof message.text !== 'string') {
          // The host never judged this body, so there is nothing to ask again: the bound
          // owner is told that it has to be sent again, a stranger is told nothing. A stranger
          // owes no line at all, so that record is done; the owner's is done only once the
          // notice actually went out (answer reports a failed send, tried a bounded number of
          // times, and then says so instead of counting it sent) - the same invariant the
          // with-text branch below keeps. Until then it stays owed and this drain asks again on
          // its own owedMs tick: this drain runs at ready(), and a channel registers LATER
          // (Slack's socket-mode connect, the WeChat bridge's probe), so at the first drain
          // there is often nothing that could send the line at all.
          let owner;
          try { owner = (await binding(message.channel))?.sender_ref === message.senderRef; }
          catch (error) {
            // The binding cannot be read: whether a line is owed here is unknown, and an
            // unknown debt is not a settled one - the record stays for the next drain.
            log('channel_owed_failed', { channel: message.channel, error: short(error) });
            continue;
          }
          if (!owner) {
            log('channel_owed_dropped', { channel: message.channel, reason: 'not_bound' });
            forget(owedKey(message));
          } else if (await answer(message.channel, replies.unknowable, { replyToRef: message.messageRef, to: message.senderRef }))
            forget(owedKey(message));
          else log('channel_owed_held', { channel: message.channel, reason: 'not_sent' });
          continue;
        }
        try { await handle(message); }
        catch (error) { log('channel_owed_failed', { channel: message.channel, error: short(error) }); }
      }
    }).catch(error => log('channel_owed_drain_failed', { error: short(error) }));
    return owedQueue;
  };

  // One mirror at a time, in the order the messages were written: a channel shows them in
  // the order it receives them.
  let queue = Promise.resolve();
  // Messages sent whose delivery row is not recorded: the only thing that keeps them from
  // being sent again in this process. A recorded one leaves it (sourceOf answers for it).
  const mirroring = new Set();
  // stop() ends a record's wait between tries (the try fails as channels_stopped); the wait
  // never keeps the process alive.
  const stopping = new AbortController();
  const pause = ms => new Promise((resolve, reject) => {
    const stopped = () => { clearTimeout(timer); reject(new Error('channels_stopped')); };
    const timer = setTimeout(() => { stopping.signal.removeEventListener('abort', stopped); resolve(); }, ms);
    timer.unref?.();
    if (stopping.signal.aborted) return stopped();
    stopping.signal.addEventListener('abort', stopped, { once: true });
  });
  // The delivery row is what keeps a message from being sent again after a restart
  // (sourceOf), so a record that fails is tried again, a bounded number of times, as the
  // same request (the host takes it once). A refusal is the host's answer: not retried.
  const record = async delivery => {
    for (let attempt = 1; ; attempt++) {
      try { return await host.recordDelivery(delivery); }
      catch (failure) {
        if (attempt >= limits.recordAttempts) throw failure;
        log('channel_record_retry', { channel: delivery.channel, message_id: delivery.messageId, attempt, error: short(failure) });
        await pause(limits.recordRetryMs * 2 ** (attempt - 1));
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
    let recorded, failed = false;
    try { recorded = await record({ messageId, channel, ...(text(ref) ? { channelMessageRef: ref } : {}), ...(error ? { error } : {}) }); }
    catch (failure) { failed = true; log('channel_record_failed', { channel, message_id: messageId, error: short(failure) }); }
    if (recorded?.refused) log('channel_record_refused', { channel, message_id: messageId, reason: String(recorded.refused) });
    else if (!failed) mirroring.delete(messageId);
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
      stopping.abort();
    },
  };
}
