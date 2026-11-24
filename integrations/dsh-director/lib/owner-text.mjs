/** The fixed text this plugin itself sends an owner, keyed by the owner's language (overfit
 *  review 重-18, 次-18's limit): the channel layer's lines (lib/channels.mjs: the receipt, the
 *  binding answers, a decision's line, the truncation tail and the limit it marks, the
 *  channel-down alert) and the reply phrases the steward's prompt names.
 *
 *  The language is HostConfig.ownerLanguage, given by the host from the one setting its own
 *  fixed text follows: 'en' or 'zh' by its primary subtag; absent or anything else, 'en'. index.mjs
 *  hands the table to the channel layer (createOwnerChannels' replies and limits) and the
 *  steward's prompt (stewardPrompt).
 *
 *  The 'zh' entries are what this plugin sent before the table existed, byte for byte (the
 *  bilingual lines included; test/owner-text.test.mjs holds the copies), and steward.md is
 *  its 'zh' prompt as it stands. Never a model's words: every line here is code's. */

export const OWNER_LANGUAGES = Object.freeze(['en', 'zh']);
export const DEFAULT_OWNER_LANGUAGE = 'en';

/** 'en' or 'zh' from a host's value: its primary subtag, case-insensitively; else the default. */
export function ownerLanguage(value) {
  const primary = String(value ?? '').trim().toLowerCase().replace(/_/g, '-').split('-')[0];
  return OWNER_LANGUAGES.includes(primary) ? primary : DEFAULT_OWNER_LANGUAGE;
}

/** A steward message reaches a chat channel cut to at most this many characters (code points):
 *  600 fits a Chinese phone screen's worth, an English one needs about twice as many for the
 *  same reading. A host that packs its own messages for a channel packs them to these. */
export const MIRROR_CHARS = Object.freeze({ en: 1200, zh: 600 });

const text = value => typeof value === 'string' && value.length > 0;
const cut = (value, max) => { const s = String(value ?? ''); return s.length > max ? s.slice(0, max - 1) + '…' : s; };

/** What an executed decision was about, in both languages: the host's own record of the
 *  proposal (its kind and target), never the message. An unknown kind reads as itself; a
 *  long target or kind is cut, so the line stays one short line. */
const DECIDED = Object.freeze({
  create_project: t => [`create project ${t}`, `创建 ${t} 项目`],
  start_driver: t => [`start director of ${t}`, `启动 ${t} 项目的导演`],
  stop_driver: t => [`stop director of ${t}`, `停止 ${t} 项目的导演`],
  archive_project: t => [`archive project ${t}`, `归档 ${t} 项目`],
  unarchive_project: t => [`unarchive project ${t}`, `取消归档 ${t} 项目`],
  connection_change: t => [`change the steward's connection ${t}`, `修改管家的连接 ${t}`],
  // Step 24.4 (design-steward 7.8.24 "五" 3): the target is the route the host resolved (cap_id ← entries).
  capability_route: t => [`basic capability ${t}`, `基础能力 ${t}`],
});
const decided = ({ kind, target }) => {
  if (!text(kind)) return null;
  const t = cut(target, 60), known = Object.hasOwn(DECIDED, kind);
  return known ? DECIDED[kind](t) : Array(2).fill(`${cut(kind, 40)}${t ? ' ' + t : ''}`);
};
const count = minutes => Math.max(0, Math.floor(Number(minutes) || 0));
const detailOf = (detail, max) => Array.from(detail).slice(0, max).join('');

/** The 'zh' lines: short, the same in both languages where they were (the bilingual ones),
 *  never an echo of what the sender wrote. */
const ZH_REPLIES = Object.freeze({
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
    // Step 24.1c (design-steward 7.8.10a): a change that widens a connection's permissions has
    // no chat approval - its card is on the dashboard (there is no box to tick).
    if (refusal === 'needs_dashboard_approval') return `proposal ${id}: not done - approve it on the dashboard card. 未执行：这条要在面板的卡片上批准。`;
    return `proposal ${id}: not done (${refusal ?? 'refused'}). 未执行（${refusal ?? 'refused'}）。`;
  },
  /** Step 24.1b (design-steward 7.8.9): the host decided the sender's `确认 <code>` /
   *  `取消 <code>` (or `confirm <code>` / `cancel <code>`) for an outward action they asked
   *  for; the line is code's, never the model's. */
  confirmation({ code, outcome, refusal }) {
    if (outcome === 'confirmed') return `已确认 ${code}，正在执行。 (Confirmed ${code}; running it.)`;
    if (outcome === 'cancelled') return `已取消 ${code}，不会执行。 (Cancelled ${code}.)`;
    if (outcome === 'expired') return `${code} 已过期，未执行；请重新请求。 (${code} expired; nothing was done.)`;
    if (refusal === 'confirmation_undated') return `${code} 未执行：消息没有时间，不能用来确认。 (${code}: not done - the message carried no time.)`;
    return `${code} 未执行（${refusal ?? 'refused'}）。 (${code}: not done.)`;
  },
  /** What follows a steward message cut short on a channel (lib/channels.mjs channelText). */
  more: '\n…（全文见面板）',
  /** The fixed text of a channel-down alert (design-steward 7.9.2): which channel, for how many
   *  minutes (the host's count) and the channel's own detail line (cut to `max` characters) - or
   *  that it is back. Nothing else: no QR, no account. */
  alert({ channel, kind, minutes, detail }, max = 80) {
    const name = { slack: 'Slack', wechat: '微信' }[channel] ?? channel, n = count(minutes);
    if (kind === 'up') return `${name}恢复了（断开了 ${n} 分钟）。`;
    const why = text(detail) ? `（${detailOf(detail, max)}）` : '';
    return `${name}断开了 ${n} 分钟${why}。断开期间发到${name}的消息收不到；去面板「渠道」页处理。`;
  },
});

const EN_REPLIES = Object.freeze({
  bound: 'Bound: this account now talks to the steward.',
  received: 'Received; working on it…',
  bindRefused: 'Not bound: the code is wrong, expired or already used.',
  unknowable: 'Not delivered, and it cannot be retried: please send it again.',
  body_too_long: 'Not delivered: the message is too long.',
  approval_undated: 'Not done: the message carried no time, so it cannot approve or decline; use the dashboard.',
  code_sent_to_bound_account: 'That was a binding code; it is now retired - issue a new one on the dashboard.',
  rate_limited: 'Held: too many messages at once; they reach the steward in order, a little later - no need to send them again.',
  refused: code => `Not delivered (${code}); the dashboard has the conversation.`,
  approval({ proposal_id: id, action, outcome, refusal, kind, target, result }) {
    if (outcome === 'executed') {
      const [en] = decided({ kind, target }) ?? [];
      if (action === 'decline') return en ? `proposal ${id}: declined - ${en}.` : `proposal ${id}: declined.`;
      if (result?.status === 'failed') return en ? `proposal ${id}: approved, but it failed - ${en}.` : `proposal ${id}: approved, but it failed.`;
      return en ? `proposal ${id}: approved and done - ${en}.` : `proposal ${id}: approved and done.`;
    }
    if (refusal === 'approval_undated') return `proposal ${id}: not done - the message carried no time, so it cannot decide; use the dashboard.`;
    if (refusal === 'needs_dashboard_confirmation') return `proposal ${id}: not done - it needs your confirmation on the dashboard.`;
    if (refusal === 'needs_dashboard_approval') return `proposal ${id}: not done - approve it on the dashboard card.`;
    return `proposal ${id}: not done (${refusal ?? 'refused'}).`;
  },
  confirmation({ code, outcome, refusal }) {
    if (outcome === 'confirmed') return `Confirmed ${code}; running it.`;
    if (outcome === 'cancelled') return `Cancelled ${code}; it will not run.`;
    if (outcome === 'expired') return `${code} expired; nothing was done - please ask again.`;
    if (refusal === 'confirmation_undated') return `${code}: not done - the message carried no time, so it cannot confirm.`;
    return `${code}: not done (${refusal ?? 'refused'}).`;
  },
  more: '\n… (full text on the dashboard)',
  alert({ channel, kind, minutes, detail }, max = 80) {
    const name = { slack: 'Slack', wechat: 'WeChat' }[channel] ?? channel, n = count(minutes);
    if (kind === 'up') return `${name} is back (it was down for ${n} minutes).`;
    const why = text(detail) ? ` (${detailOf(detail, max)})` : '';
    return `${name} has been down for ${n} minutes${why}. Messages sent to ${name} meanwhile do not arrive; see the Channels page on the dashboard.`;
  },
});

/** The steward's prompt in the owner's language: steward.md is written for a 'zh' owner (the
 *  reply phrases and the brief's two fixed lines it has the steward send); for another language
 *  each of these exact passages (each occurs once in steward.md, which a test checks) is
 *  replaced. The host decides a reply phrase in either language; the prompt names the owner's. */
const EN_STEWARD_PROMPT = Object.freeze([
  ['`同意 proposal <id>` to approve, `拒绝 proposal <id>` to decline', '`approve proposal <id>` to approve, `decline proposal <id>` to decline'],
  ['the exact reply 同意 proposal <id>;', 'the exact reply approve proposal <id>;'],
  ['only 今天没有要你处理的事.', 'only "Nothing needs you today."'],
  ['今早的简报没发成（晚了 N 小时）', '"This morning\'s brief did not go out (N hours late)"'],
].map(pair => Object.freeze(pair)));

export const OWNER_TEXTS = Object.freeze({
  zh: Object.freeze({ replies: ZH_REPLIES, mirrorChars: MIRROR_CHARS.zh, stewardPrompt: Object.freeze([]) }),
  en: Object.freeze({ replies: EN_REPLIES, mirrorChars: MIRROR_CHARS.en, stewardPrompt: EN_STEWARD_PROMPT }),
});

/** The table for a host's language value (see ownerLanguage). */
export const ownerTexts = language => OWNER_TEXTS[ownerLanguage(language)];

/** `prompt` with the language's passages replaced ('zh': unchanged, byte for byte). */
export function stewardPrompt(prompt, language) {
  return ownerTexts(language).stewardPrompt.reduce((out, [from, to]) => out.split(from).join(to), prompt);
}
