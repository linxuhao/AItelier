/** The owner's-language table (lib/owner-text.mjs; overfit review 重-17, 重-18): the 'zh'
 *  lines are exactly what this plugin sent before the table existed - the block below is a
 *  verbatim copy of lib/channels.mjs at a1416299ee (only renamed and un-exported) - the default
 *  is English, and the steward prompt for 'zh' is steward.md byte for byte. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DEFAULT_OWNER_LANGUAGE, MIRROR_CHARS, OWNER_LANGUAGES, OWNER_TEXTS, ownerLanguage, ownerTexts, stewardPromptInLanguage } from '../lib/owner-text.mjs';
import { CHANNEL_LIMITS, CHANNEL_NAMES, alertText, channelReplies, channelText, MORE } from '../lib/channels.mjs';
import { ownerChannelsFor } from '../index.mjs';

// ---- a1416299ee:integrations/dsh-director/lib/channels.mjs, verbatim (lines 47-104, 132, 247-253)
const text = value => typeof value === 'string' && value.length > 0;
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
const cut = (value, max) => { const s = String(value ?? ''); return s.length > max ? s.slice(0, max - 1) + '…' : s; };
const decided = ({ kind, target }) => {
  if (!text(kind)) return null;
  const t = cut(target, 60), known = Object.hasOwn(DECIDED, kind);
  return known ? DECIDED[kind](t) : Array(2).fill(`${cut(kind, 40)}${t ? ' ' + t : ''}`);
};

/** The lines a sender is answered with on the channel: short, the same in both languages,
 *  never an echo of what they wrote. */
const TODAY = Object.freeze({
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
   *  `取消 <code>` for an outward action they asked for; the line is code's, never the model's. */
  confirmation({ code, outcome, refusal }) {
    if (outcome === 'confirmed') return `已确认 ${code}，正在执行。 (Confirmed ${code}; running it.)`;
    if (outcome === 'cancelled') return `已取消 ${code}，不会执行。 (Cancelled ${code}.)`;
    if (outcome === 'expired') return `${code} 已过期，未执行；请重新请求。 (${code} expired; nothing was done.)`;
    if (refusal === 'confirmation_undated') return `${code} 未执行：消息没有时间，不能用来确认。 (${code}: not done - the message carried no time.)`;
    return `${code} 未执行（${refusal ?? 'refused'}）。 (${code}: not done.)`;
  },
});
const TODAY_MORE = '\n…（全文见面板）';
const ALERT_NAMES = Object.freeze({ slack: 'Slack', wechat: '微信' });
function todayAlert({ channel, kind, minutes, detail }, max = 80) {
  const name = ALERT_NAMES[channel] ?? channel, count = Math.max(0, Math.floor(Number(minutes) || 0));
  if (kind === 'up') return `${name}恢复了（断开了 ${count} 分钟）。`;
  const why = text(detail) ? `（${Array.from(detail).slice(0, max).join('')}）` : '';
  return `${name}断开了 ${count} 分钟${why}。断开期间发到${name}的消息收不到；去面板「渠道」页处理。`;
}

// ---- end of the copy

const KINDS = ['create_project', 'start_driver', 'stop_driver', 'archive_project', 'unarchive_project', 'connection_change',
  'capability_route', 'some_future_kind', undefined, ''];
const TARGETS = ['alpha', '', undefined, 'x'.repeat(90), 'draw ← media/generate_image'];
const REFUSALS = ['approval_undated', 'needs_dashboard_confirmation', 'needs_dashboard_approval', 'proposal_not_open', undefined];
/** Every shape a decision's line takes, as the arguments the channel layer passes. */
function approvals() {
  const out = [];
  for (const action of ['approve', 'decline']) {
    for (const kind of KINDS) for (const target of TARGETS) for (const result of [undefined, { status: 'failed' }, { status: 'done' }])
      out.push({ proposal_id: 'p-7', action, outcome: 'executed', kind, target, result });
    for (const refusal of REFUSALS) out.push({ proposal_id: 'p-7', action, outcome: 'refused', refusal });
  }
  return out;
}
const CONFIRMATIONS = [{ code: 'K7Q2A', outcome: 'confirmed' }, { code: 'K7Q2A', outcome: 'cancelled' }, { code: 'K7Q2A', outcome: 'expired' },
  { code: 'K7Q2A', outcome: 'refused', refusal: 'confirmation_undated' }, { code: 'K7Q2A', outcome: 'refused', refusal: 'confirmation_clock_skew' },
  { code: 'K7Q2A', outcome: 'refused' }];
const ALERTS = [];
for (const channel of ['slack', 'wechat', 'other']) for (const kind of ['down', 'up']) for (const minutes of [0, 12, '7.9', -3, 'x'])
  for (const detail of [undefined, null, '', 'no QR (logged_out)', '长'.repeat(200)]) ALERTS.push({ channel, kind, minutes, detail });
const FIXED = ['bound', 'received', 'bindRefused', 'unknowable', 'body_too_long', 'approval_undated', 'code_sent_to_bound_account', 'rate_limited'];
const CJK = /[　-〿㐀-鿿＀-￯]/;

test('owner language: one setting, by its primary subtag; absent or unknown is English', () => {
  assert.equal(DEFAULT_OWNER_LANGUAGE, 'en');
  assert.deepEqual(OWNER_LANGUAGES, ['en', 'zh']);
  for (const [value, expected] of [[undefined, 'en'], [null, 'en'], ['', 'en'], ['en', 'en'], ['EN-us', 'en'], ['zh', 'zh'], ['ZH', 'zh'],
    ['zh-CN', 'zh'], ['zh_Hans', 'zh'], [' zh ', 'zh'], ['fr', 'en'], ['chinese', 'en'], [42, 'en']])
    assert.equal(ownerLanguage(value), expected, String(value));
  assert.equal(ownerTexts(undefined), OWNER_TEXTS.en);
  assert.equal(ownerTexts('zh-CN'), OWNER_TEXTS.zh);
});

test('zh: every channel line is byte for byte what a1416299ee sent', () => {
  const zh = OWNER_TEXTS.zh.replies;
  for (const key of FIXED) assert.equal(zh[key], TODAY[key], key);
  for (const code of ['unavailable', 'invalid_inbound', 'x']) assert.equal(zh.refused(code), TODAY.refused(code));
  for (const args of approvals()) assert.equal(zh.approval(args), TODAY.approval(args), JSON.stringify(args));
  for (const args of CONFIRMATIONS) assert.equal(zh.confirmation(args), TODAY.confirmation(args), JSON.stringify(args));
  assert.equal(zh.more, TODAY_MORE);
  for (const args of ALERTS) for (const max of [80, 5]) assert.equal(zh.alert(args, max), todayAlert(args, max), JSON.stringify(args));
  // ... and the limit: 600 characters, as CHANNEL_LIMITS had it.
  assert.equal(OWNER_TEXTS.zh.mirrorChars, 600);
  assert.equal(MIRROR_CHARS.zh, CHANNEL_LIMITS.mirrorChars);
  // The literals themselves, once more, as the owner reads them today.
  assert.equal(zh.received, '已收到，正在处理…');
  assert.equal(zh.more, '\n…（全文见面板）');
  assert.equal(zh.alert({ channel: 'wechat', kind: 'down', minutes: 12, detail: 'no QR' }),
    '微信断开了 12 分钟（no QR）。断开期间发到微信的消息收不到；去面板「渠道」页处理。');
});

test('en (the default): the same lines, every one in English only; the library defaults are English', () => {
  const en = OWNER_TEXTS.en.replies, zh = OWNER_TEXTS.zh.replies;
  assert.deepEqual(Object.keys(en).sort(), Object.keys(zh).sort());
  const lines = [...FIXED.map(key => en[key]), en.refused('x'), en.more, ...approvals().map(args => en.approval(args)),
    ...CONFIRMATIONS.map(args => en.confirmation(args)),
    ...ALERTS.filter(args => !String(args.detail ?? '').includes('长')).map(args => en.alert(args))];
  for (const line of lines) assert.ok(text(line) && !CJK.test(line), line);
  assert.equal(en.received, 'Received; working on it…');
  assert.equal(en.alert({ channel: 'wechat', kind: 'up', minutes: 3 }), 'WeChat is back (it was down for 3 minutes).');
  assert.equal(en.alert({ channel: 'slack', kind: 'down', minutes: 10, detail: 'd'.repeat(100) }, 80).match(/\((d+)\)/)[1].length, 80);
  // An English owner reads about twice as many characters for the same message (review 重-18).
  assert.equal(OWNER_TEXTS.en.mirrorChars, 1200);
  // The library's own defaults (a caller that passes no table) are the default language's.
  assert.equal(channelReplies, en);
  assert.equal(MORE, en.more);
  assert.equal(alertText({ channel: 'wechat', kind: 'up', minutes: 1 }), en.alert({ channel: 'wechat', kind: 'up', minutes: 1 }));
  const long = 'An answer sentence that goes on. '.repeat(40);
  const cut = channelText('slack', long, 600);
  assert.ok(cut.endsWith(en.more) && Array.from(cut).length <= 600, cut.slice(-60));
  assert.ok(channelText('slack', long, 1200, zh.more).endsWith(zh.more));
});

test('the approval phrase line is kept whole in either language: the Chinese phrase and its English alias', () => {
  const body = 'Long. '.repeat(200) + '\n`approve proposal prop-1`\n**decline proposal prop-1**\n同意 proposal prop-1';
  const sent = channelText('wechat', body, 300);
  assert.ok(sent.endsWith('\napprove proposal prop-1\ndecline proposal prop-1\n同意 proposal prop-1' + MORE), sent.slice(-120));
  // Not a phrase: a capitalised alias is an ordinary line (the host decides none of these).
  assert.ok(!channelText('wechat', 'Long. '.repeat(200) + '\nApprove proposal prop-1', 300).includes('Approve proposal prop-1'));
});

const STEWARD_MD = new URL('../steward.md', import.meta.url);
test('steward prompt: zh is steward.md byte for byte (as at daf492274e); en names the English phrases and brief lines', () => {
  const md = readFileSync(STEWARD_MD, 'utf8');
  assert.equal(createHash('sha256').update(stewardPromptInLanguage(md, 'zh')).digest('hex'),
    'b91a5b14561d39ead1523c6a15d3a6b6dcff6b46bf05924203c30c21e7427680', 'steward.md as at daf492274e');
  assert.equal(stewardPromptInLanguage(md, 'zh'), md);
  // Each passage as often as steward.md has it today: the withheld sentence three times (group
  // turns, list_add, request_confirmation), every other once.
  const times = { '「这个我在群里答不了。」': 3 };
  for (const [from] of OWNER_TEXTS.en.stewardPrompt) assert.equal(md.split(from).length - 1, times[from] ?? 1, `in steward.md: ${from}`);
  const en = stewardPromptInLanguage(md, undefined);
  assert.equal(en, stewardPromptInLanguage(md, 'en'));
  assert.ok(!/同意 proposal|拒绝 proposal|今天没有要你处理的事|今早的简报没发成|这个我在群里答不了/.test(en));
  // A host that withholds a group reply posts these same words (its own test ties the two).
  assert.equal(en.split('exactly "I cannot answer that in this group."').length - 1, 2);
  assert.equal(en.split('exactly\n"I cannot answer that in this group."').length - 1, 1);
  assert.match(en, /`approve proposal <id>` to approve, `decline proposal <id>` to decline/);
  assert.match(en, /the exact reply approve proposal <id>;/);
  assert.match(en, /send\nonly "Nothing needs you today\."/);
});

/** index.mjs's ownerChannelsFor with a host that says (or does not say) the owner's language. */
async function receiptFor(config) {
  const sent = [];
  const host = { capabilities: ['channels'], config: { stateDir: mkdtempSync(join(tmpdir(), 'owner-text-')), ...config },
    channels: {
      async list() { return { default_channel: 'slack', channels: CHANNEL_NAMES.map(channel => ({ channel, bound: channel === 'slack',
        sender_ref: channel === 'slack' ? 'U-OWNER' : null, bound_at: null, revoked_at: null, is_default: channel === 'slack' })) }; },
      async codeCheck() { return { store_text: true }; },
      async relay() { return { message_id: 'm-in', duplicate: false, approval: null }; },
      async bind() { return { bound: false }; }, async sourceOf() { return null; }, async recordDelivery() { return { recorded: true }; },
    } };
  const channels = ownerChannelsFor({ logger: { info() {} } }, host);
  channels.ready();
  channels.service.register({ name: 'slack', status: () => ({ connected: true }), async send(line) { sent.push(line); return { ref: 'r' }; } });
  await channels.service.inbound({ channel: 'slack', senderRef: 'U-OWNER', messageRef: 'ts-1', text: 'hello', at: new Date().toISOString() });
  await new Promise(resolve => setTimeout(resolve, 20));
  channels.stop();
  return sent;
}

test('index.mjs hands the host\'s language to the channel layer: zh gets today\'s receipt, no setting gets English', async () => {
  assert.deepEqual(await receiptFor({ ownerLanguage: 'zh' }), ['已收到，正在处理…']);
  assert.deepEqual(await receiptFor({}), ['Received; working on it…']);
  assert.deepEqual(await receiptFor({ ownerLanguage: 'en' }), ['Received; working on it…']);
});
