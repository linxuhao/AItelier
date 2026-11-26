/** How memory_search shows an entry (design-steward 3.8.1), and the date words the BM25
 *  index adds to every entry (3.5). Both read the time through the one owner-time-zone
 *  conversion below, so "the 19th in the evening" is the same day in a line's date, in its
 *  day group and in the index. Only for search: the snapshot's lines (7.3) are not these. */
import { documentTokens } from './bm25.mjs';
import { ownerLanguage } from '../owner-text.mjs';

// The zone is checked once, where the memory opens (memoryTimeZoneProblem in ../host.mjs).
export const DEFAULT_TIME_ZONE = 'UTC';
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const WEEKDAY_FULL = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const WEEKDAY_ZH = ['日', '一', '二', '三', '四', '五', '六'];
const MONTH_FULL = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const MONTH_ABBR = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
// The two forms an `at` comes in: `…+00:00` (often with microseconds, as the host's
// messages carry it) on a conversation entry, `…Z` on a note. Anything else is not a time this renders.
const AT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/;

const formats = new Map();
function format(tz) {
  let f = formats.get(tz);
  if (!f) formats.set(tz, f = new Intl.DateTimeFormat('en-US', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23', weekday: 'short', timeZoneName: 'longOffset' }));
  return f;
}

/** Milliseconds of an `at`, or NaN. */
export function parseAt(at) {
  return typeof at === 'string' && AT.test(at) ? Date.parse(at) : NaN;
}

/** The time memory shows for an entry: when the owner wrote it in a chat app (`channel_at`,
 *  kept from message_sources) when there is a usable one, else `at` (when the backend received
 *  it; still what entries are kept and ordered by). A line's date, its day group and its index
 *  date words all read this one time. */
export function writtenAt({ at, channel_at }) {
  return Number.isNaN(parseAt(channel_at)) ? at : channel_at;
}

/** The owner-time-zone calendar fields of `at`, or undefined when it does not parse. */
export function zoned(at, tz = DEFAULT_TIME_ZONE) {
  const ms = parseAt(at);
  if (Number.isNaN(ms)) return undefined;
  const p = Object.fromEntries(format(tz).formatToParts(new Date(ms)).map(x => [x.type, x.value]));
  const offset = p.timeZoneName === 'GMT' ? '+00:00' : p.timeZoneName.replace(/^GMT/, '');
  return { year: Number(p.year), month: Number(p.month), day: Number(p.day), weekday: WEEKDAYS.indexOf(p.weekday),
    hour: p.hour, minute: p.minute, offset, ms };
}

const pad = n => String(n).padStart(2, '0');
const isoDay = z => `${z.year}-${pad(z.month)}-${pad(z.day)}`;

/** The owner's calendar day of `at` (`YYYY-MM-DD`), or undefined. */
export function dayOf(at, tz) {
  const z = zoned(at, tz);
  return z && isoDay(z);
}

/** The words memory shows the steward, in the owner's language (lib/owner-text.mjs: 'en' or
 *  'zh' by the primary subtag, else 'en'). The 'zh' ones are what memory showed before the
 *  table existed, byte for byte. The index's date words (dateWords) are both languages' always. */
export const MEMORY_WORDS = Object.freeze({
  zh: Object.freeze({ weekday: w => `周${WEEKDAY_ZH[w]}`, role: { owner: '业主', steward: '管家' }, note: '（笔记）',
    channel: c => `（${c}）`, dateOnly: '（日期命中）', see: '→ 见 ', undated: '时间不明', remote: '远端',
    profileLine: n => `[档案 第 ${n} 行]`, grepSource: 'grep · 字面匹配 · 本地记忆日志', bm25Source: 'bm25 · 本地索引',
    bm25NoVectors: 'bm25 · 本地索引 · 向量不可用', vectorSource: model => `bm25+向量 · 本地索引 · ${model}`, chars: '字',
    // The owner's notices ("Memory notices - tell your owner").
    vectorPending: (since, count, file, code, ids) => `本地向量文件自 ${since} 去不掉 ${count} 条已忘掉（或已彻底删除）条目的向量行（${file} · ${code}）；`
      + '原文已删、搜索取不到，但由原文算出的向量行仍在磁盘上；'
      + (code === 'CHECK_FAILED' ? '过滤结果没通过双边检查，文件没动，下次过滤通过后会自动去掉。编号：' : '文件恢复可写后会自动去掉。编号：') + ids,
    vectorDown: (since, url, model, code, left) => `本地向量检索自 ${since} 不可用（${url} · ${model} · ${code}），`
      + `排序检索只用 BM25，可能漏掉意思相近的记录；未嵌入 ${left} 条`,
    purgeRecovered: (purgeId, ids, completed) => `上次彻底删除（${purgeId}，条目 ${ids}）被中断，结果为：${completed ? '已完成' : '已回退，未删除'}` }),
  en: Object.freeze({ weekday: w => WEEKDAYS[w], role: { owner: 'owner', steward: 'steward' }, note: ' (note)',
    channel: c => ` (${c})`, dateOnly: ' (date match)', see: '→ see ', undated: 'time unknown', remote: 'remote',
    profileLine: n => `[profile line ${n}]`, grepSource: 'grep · literal match · local memory log', bm25Source: 'bm25 · local index',
    bm25NoVectors: 'bm25 · local index · vectors unavailable', vectorSource: model => `bm25+vectors · local index · ${model}`, chars: 'chars',
    vectorPending: (since, count, file, code, ids) => `Since ${since} the local vector file still holds the vector rows of ${count} forgotten (or purged) entries (${file} · ${code}); `
      + 'their text is deleted and search cannot find them, but the vectors computed from it are still on disk; '
      + (code === 'CHECK_FAILED' ? 'the filtered file failed its two-sided check, so the file was left as it was; they go once a filter passes. Ids: '
        : 'they go once the file can be written again. Ids: ') + ids,
    vectorDown: (since, url, model, code, left) => `Local vector search has been unavailable since ${since} (${url} · ${model} · ${code}); `
      + `ranked search uses BM25 alone and may miss records close in meaning; ${left} entries not embedded`,
    purgeRecovered: (purgeId, ids, completed) => `The last purge (${purgeId}, entries ${ids}) was interrupted; result: ${completed ? 'completed' : 'rolled back, nothing deleted'}` }),
});
/** The words for a host's language value. */
export const memoryWords = language => MEMORY_WORDS[ownerLanguage(language)];

/** `2026-11-19 Thu 11:25+01:00` (zh: `2026-11-19 周四 11:25+01:00`), or undefined. */
export function stamp(at, tz, language) {
  const z = zoned(at, tz);
  return z && `${isoDay(z)} ${memoryWords(language).weekday(z.weekday)} ${z.hour}:${z.minute}${z.offset}`;
}

/** The date words of `at` (3.8.1): `2026-11-19 2026年11月19日 11月19日 周四 星期四 November
 *  Nov Thursday Thu`. None for a time that does not parse. */
export function dateWords(at, tz) {
  const z = zoned(at, tz);
  if (!z) return [];
  const w = z.weekday, m = z.month - 1;
  return [isoDay(z), `${z.year}年${z.month}月${z.day}日`, `${z.month}月${z.day}日`, `周${WEEKDAY_ZH[w]}`, `星期${WEEKDAY_ZH[w]}`,
    MONTH_FULL[m], MONTH_ABBR[m], WEEKDAY_FULL[w], WEEKDAYS[w]];
}

/** The date words as index tokens: the same tokenizer as the text, so a query meets them. */
export function dateTokens(at, tz) {
  const words = dateWords(at, tz);
  return words.length ? documentTokens(words.join(' ')) : [];
}

/** One search line: `[2026-11-19 Thu 11:25+01:00] owner {t-…-o}: <text>` (zh:
 *  `[2026-11-19 周四 11:25+01:00] 业主 {t-…-o}: <text>`), dated by writtenAt. A note's role says
 *  (note) / （笔记）; an entry with a chat channel names it after the role (`owner (slack)` /
 *  `业主（slack）`); `dateOnly` marks a ranked hit that only its date words matched; `see` replaces
 *  the text with `→ see grep` / `→ 见 grep` (the other answer has the whole entry). A hit with no
 *  local entry (remote) is `remote` / `远端`, without an id. */
export function formatLine({ id, at, channel_at, role, source, channel, text }, tz, { dateOnly = false, see, language } = {}) {
  const words = memoryWords(language);
  const when = stamp(writtenAt({ at, channel_at }), tz, language);
  const head = `[${when ?? words.undated}] ` + (id === undefined
    ? words.remote
    : `${words.role[role] ?? role}${source === 'note' ? words.note : ''}${channel ? words.channel(channel) : ''} {${id}}${dateOnly ? words.dateOnly : ''}`);
  return `${head}: ${see ? words.see + see : text}`;
}

/** A profile hit: `[profile line N] <that line>` (zh: `[档案 第 N 行]`), no date. */
export function formatProfileLine(line, text, language) {
  return `${memoryWords(language).profileLine(line)} ${text}`;
}
