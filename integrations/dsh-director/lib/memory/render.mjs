/** How memory_search shows an entry (design-steward 3.8.1), and the date words the BM25
 *  index adds to every entry (3.5). Both read the time through the one owner-time-zone
 *  conversion below, so "the 19th in the evening" is the same day in a line's date, in its
 *  day group and in the index. Only for search: the snapshot's lines (7.3) are not these. */
import { documentTokens } from './bm25.mjs';

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

/** `2026-11-19 周四 11:25+01:00`, or undefined. */
export function stamp(at, tz) {
  const z = zoned(at, tz);
  return z && `${isoDay(z)} 周${WEEKDAY_ZH[z.weekday]} ${z.hour}:${z.minute}${z.offset}`;
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

const ROLE = { owner: '业主', steward: '管家' };

/** One search line: `[2026-11-19 周四 11:25+01:00] 业主 {t-…-o}: <text>`, dated by writtenAt. A note's role says
 *  （笔记）; an entry with a chat channel names it after the role (`业主（slack）`); `dateOnly`
 *  marks a ranked hit that only its date words matched; `see` replaces the text with
 *  `→ 见 grep` / `→ 见 ranked` (the other answer has the whole entry). A hit with no local
 *  entry (remote) is `远端`, without an id. */
export function formatLine({ id, at, channel_at, role, source, channel, text }, tz, { dateOnly = false, see } = {}) {
  const when = stamp(writtenAt({ at, channel_at }), tz);
  const head = `[${when ?? '时间不明'}] ` + (id === undefined
    ? '远端'
    : `${ROLE[role] ?? role}${source === 'note' ? '（笔记）' : ''}${channel ? `（${channel}）` : ''} {${id}}${dateOnly ? '（日期命中）' : ''}`);
  return `${head}: ${see ? '→ 见 ' + see : text}`;
}

/** A profile hit: `[档案 第 N 行] <that line>`, no date. */
export function formatProfileLine(line, text) {
  return `[档案 第 ${line} 行] ${text}`;
}
