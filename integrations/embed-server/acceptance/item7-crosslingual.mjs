/** Design-steward 3.8.4 item 7 (real model; not CI): three target records among 300
 *  synthetic distractors (half Chinese, half English; five lexical look-alikes per target).
 *  Vector on: every target in ranked's top 3. BM25 only: none in the top 3.
 *  Usage: node item7-crosslingual.mjs <absolute scratch dir>  (needs aitelier-embed up) */
import { EN_SENTENCES, MODEL, STORE, URL_, USER, ZH_SENTENCES, loadavg, now, rng, save, scratchRoot, waitVectorOk, writeTurns } from './common.mjs';

const { MemoryStore } = await import(STORE);
const { dir, root } = scratchRoot(process.argv[2], 'item7-root');
const rand = rng(7);

const TARGETS = [
  { name: 'domain', text: 'Remind me to renew the domain before December', query: '域名续费', lang: 'en' },
  { name: 'tracker', text: '下周想把记账小工具做成手机能用的', query: 'expense tracker on mobile', lang: 'zh' },
  { name: 'meetings', text: '我不喜欢太早开会', query: '早上会议的偏好', lang: 'zh' },
];
// Five per target: a query word on its own, in a sentence that means something else.
const LOOKALIKES = [
  ['公司内网的域名解析今天切到了新的服务器', '他在论文里专门讨论了「域名」这个词的翻译', '健身房的会员卡下个月自动续费，记得看一下扣款', '视频网站的会员续费价格又涨了', '刚收到手机话费续费成功的短信'],
  ['The travel expense report for the Lyon trip is due on Friday.', 'My fitness tracker battery died again during the run.', 'The parcel tracker says the package is stuck in customs.',
    'Mobile reception at the cabin is terrible past the second bridge.', 'The mobile library van comes to the village every other Tuesday.'],
  ['会议室的投影仪又连不上笔记本了', '早上的面包店七点开门，可颂很快卖完', '猫对罐头口味的偏好很固定，只吃鸡肉的', '年度会议的场地定在了城东的酒店', '早上跑步的时候膝盖有点疼'],
];

const shuffle = a => { for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(rand() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; };
const zhLook = LOOKALIKES[0].length + LOOKALIKES[2].length, enLook = LOOKALIKES[1].length;
const generic = [...shuffle([...ZH_SENTENCES]).slice(0, 150 - zhLook), ...shuffle([...EN_SENTENCES]).slice(0, 150 - enLook)];
const distractors = [...LOOKALIKES.flat(), ...generic];
if (distractors.length !== 300) throw new Error('want 300 distractors, have ' + distractors.length);
const all = shuffle([...distractors.map(text => ({ text })), ...TARGETS.map(t => ({ text: t.text, target: t.name }))]);
// Spread over the last 60 days, in time order.
const start = Date.now() - 60 * 86400000;
all.forEach((e, i) => { e.at = new Date(start + Math.round((i + rand()) * (60 * 86400000 / all.length))).toISOString(); });
const PER = 25;
all.forEach((e, i) => { e.id = `t-c7-${Math.floor(i / PER)}-${i % PER}`; });
const idOf = Object.fromEntries(all.filter(e => e.target).map(e => [e.target, e.id]));

const report = { item: 7, started: now(), loadavg_start: loadavg(), url: URL_, model: MODEL, corpus: {
  entries: all.length, targets: TARGETS.map(t => ({ ...t, id: idOf[t.name] })),
  zh: all.filter(e => /\p{Script=Han}/u.test(e.text)).length, en: all.filter(e => !/\p{Script=Han}/u.test(e.text)).length } };

async function ranks(store, label) {
  const out = [];
  for (const t of TARGETS) {
    const r = await store.search(t.query, 10);
    const ids = (r.ranked.hits ?? []).map(h => h.id);
    const bm = store.index.search(t.query, 1000).map(h => h.id);
    const vec = store.vector ? await store.vector.rank(t.query, 1000) : undefined;
    const pos = (list, id) => { const i = list ? list.indexOf(id) : -1; return i < 0 ? null : i + 1; };
    out.push({ query: t.query, target: t.name, ranked_rank: pos(ids, idOf[t.name]), in_top3: ids.slice(0, 3).includes(idOf[t.name]),
      bm25_rank: pos(bm, idOf[t.name]), bm25_matches: bm.length, vector_rank: vec ? pos(vec, idOf[t.name]) : undefined,
      ranked_source: r.ranked.source, ranked_vector: r.ranked.vector,
      // Why each of the first five is there: its rank in each channel (RRF uses the first C = 50).
      top5: (r.ranked.hits ?? []).slice(0, 5).map(h => ({ id: h.id, score: h.score, bm25_rank: pos(bm, h.id), vector_rank: vec ? pos(vec, h.id) : undefined,
        text: all.find(e => e.id === h.id)?.text.slice(0, 40), target: all.find(e => e.id === h.id)?.target })) });
  }
  console.log(label, JSON.stringify(out.map(o => [o.target, o.ranked_rank, o.in_top3])));
  return out;
}

// 1. Vector on: write, let the worker embed everything, search.
let store = await MemoryStore.open({ root, userId: USER, timeZone: 'Europe/Paris', vector: { url: URL_, model: MODEL } });
await writeTurns(store, 'c7', all.map(({ text, at }) => ({ text, at })), PER);
report.embed_wait = await waitVectorOk(store, { timeoutMs: 600000 });
report.vector_on = await ranks(store, 'vector on');
await store.close();
// 2. BM25 only: the same root without vector config.
store = await MemoryStore.open({ root, userId: USER, timeZone: 'Europe/Paris' });
report.bm25_only = await ranks(store, 'bm25 only');
await store.close();

report.pass = report.vector_on.every(r => r.in_top3) && report.bm25_only.every(r => !r.in_top3);
report.finished = now();
report.loadavg_end = loadavg();
console.log(JSON.stringify({ pass: report.pass }));
save(dir, 'item7.json', report);
process.exitCode = report.pass ? 0 : 1;
