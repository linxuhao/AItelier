/** Design-steward 3.8.4 item 12 (real model; not CI): 200 warm query embeddings (<= 64
 *  characters) and 1000-character entry embeddings, straight to the server (the request
 *  vector.mjs makes), one at a time; the container's memory.stat after.
 *  Budgets (3.8.2): query p50 <= 300 ms, p95 <= 800 ms; 1000-character entry p95 <= 1.5 s.
 *  Usage: node item12-latency.mjs <absolute scratch dir>  (needs aitelier-embed up) */
import { containerMem, embedRaw, loadavg, now, rng, save, scratchRoot, summary, textOf } from './common.mjs';

const { dir } = scratchRoot(process.argv[2], 'item12-latency');
const rand = rng(12);
const QUERIES_ZH = ['域名续费', '早上会议的偏好', '上次说的牙医是哪天', '猫的疫苗什么时候打', '阳台种的番茄', '火车晚点那次', '我喜欢的书', '周末做什么菜',
  '体检报告的结果', '妈妈的生日礼物', '那家面包店', '去年冬天的旅行', '家里网络很慢', '换工作的想法', '给孩子报的兴趣班', '车子保养'];
const QUERIES_EN = ['expense tracker on mobile', 'dentist appointment next week', 'when is the cat vaccine', 'books I liked last year', 'the hiking trip in October',
  'birthday gift ideas for mom', 'the bakery near the station', 'why was the train late', 'tomato plants on the balcony', 'car service schedule',
  'what did I say about moving', 'kids music lessons', 'the storm last month', 'favourite soup recipe', 'internet is slow at home', 'job change thoughts'];
const queries = Array.from({ length: 200 }, (_, i) => {
  const q = (i % 2 ? QUERIES_EN : QUERIES_ZH)[Math.floor(i / 2) % 16];
  return i < 32 ? q : q + ' ' + (i % 2 ? 'again ' + i : '再问一次' + i); // not only repeats
});
if (queries.some(q => Array.from(q).length > 64)) throw new Error('a query over 64 characters');

const report = { item: '12-latency', started: now(), loadavg_start: loadavg(), mem_before: containerMem() };
for (let i = 0; i < 5; i++) await embedRaw('warm up ' + i); // warm, not counted
const q = [];
for (const text of queries) q.push({ chars: Array.from(text).length, ...(await embedRaw(text)) });
report.loadavg_after_queries = loadavg();
report.query = summary(q.map(x => x.ms));
report.query_tokens = summary(q.map(x => x.tokens));

const long = [];
for (let i = 0; i < 60; i++) {
  const lang = i % 2 ? 'en' : 'zh';
  const text = textOf(rand, 1000, lang);
  long.push({ lang, chars: Array.from(text).length, ...(await embedRaw(text)) });
}
report.loadavg_after_long = loadavg();
report.entry1000 = summary(long.map(x => x.ms));
report.entry1000_zh = summary(long.filter(x => x.lang === 'zh').map(x => x.ms));
report.entry1000_en = summary(long.filter(x => x.lang === 'en').map(x => x.ms));
report.entry1000_tokens = { zh: summary(long.filter(x => x.lang === 'zh').map(x => x.tokens)), en: summary(long.filter(x => x.lang === 'en').map(x => x.tokens)) };
report.dims = [...new Set([...q, ...long].map(x => x.dims))];
report.mem_after = containerMem();
report.verdict = {
  query_p50: report.query.p50 <= 300 ? 'PASS' : 'FAIL',
  query_p95: report.query.p95 <= 800 ? 'PASS' : 'FAIL',
  entry1000_p95: report.entry1000.p95 <= 1500 ? 'PASS' : 'FAIL',
  anon: report.mem_after.anon <= 0.4 * 2 ** 30 ? 'PASS' : 'FAIL',
};
report.raw = { query: q, entry1000: long };
report.finished = now();
report.loadavg_end = loadavg();
save(dir, 'item12-latency.json', report);
console.log(JSON.stringify({ query: report.query, entry1000: report.entry1000, tokens: report.entry1000_tokens, anon: report.mem_after.anon, verdict: report.verdict, load: [report.loadavg_start, report.loadavg_end] }));
