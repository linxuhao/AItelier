/** Shared helpers for the real-model acceptance scripts (design-steward 3.8.4 items 7, 11,
 *  12; not part of CI). They drive the plugin code of this checkout against the running
 *  aitelier-embed container, always with a throwaway memory root: never the live steward
 *  memory (~/.local/share/dsh-director/memory) or anything under ~/.AItelier. */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { isAbsolute, join, resolve } from 'node:path';
import { execFileSync } from 'node:child_process';

export const URL_ = process.env.EMBED_URL ?? 'http://127.0.0.1:18751';
export const MODEL = 'bge-m3-q8_0';
export const CONTAINER = 'aitelier-embed';
export const USER = 'owner@example.com';
export const STORE = new URL('../../dsh-director/lib/memory/store.mjs', import.meta.url).href;
export const VECTOR = new URL('../../dsh-director/lib/memory/vector.mjs', import.meta.url).href;
export const HOST = new URL('../../dsh-director/lib/host.mjs', import.meta.url).href;

/** argv: <scratch dir> (absolute, fresh or empty; refused under the live memory places). */
export function scratchRoot(arg, name) {
  if (!arg || !isAbsolute(arg)) throw new Error('usage: node <script> <absolute scratch dir> (a temporary memory root goes inside it)');
  const dir = resolve(arg);
  for (const live of [join(homedir(), '.local', 'share'), join(homedir(), '.AItelier')])
    if (dir === live || dir.startsWith(live + '/')) throw new Error(`refused: ${dir} is under ${live} (live data)`);
  const root = join(dir, name);
  if (existsSync(root) && readdirSync(root).length) throw new Error(`refused: ${root} is not empty (use a fresh one)`);
  mkdirSync(root, { recursive: true, mode: 0o700 });
  return { dir, root };
}

export const loadavg = () => readFileSync('/proc/loadavg', 'utf8').trim().split(' ').slice(0, 3).join(' ');
export const memAvailableKb = () => Number(/MemAvailable:\s+(\d+)/.exec(readFileSync('/proc/meminfo', 'utf8'))[1]);
export const now = () => new Date().toISOString();
export const sleep = ms => new Promise(r => setTimeout(r, ms));

/** Nearest-rank percentile. */
export function pct(values, p) {
  if (!values.length) return NaN;
  const s = [...values].sort((a, b) => a - b);
  return s[Math.max(0, Math.ceil((p / 100) * s.length) - 1)];
}
export const summary = ms => ({ n: ms.length, p50: r1(pct(ms, 50)), p95: r1(pct(ms, 95)), max: r1(Math.max(...ms)), min: r1(Math.min(...ms)) });
export const r1 = x => Math.round(x * 10) / 10;

/** One POST /v1/embeddings straight to the server (the request vector.mjs makes). */
export async function embedRaw(input) {
  const t = performance.now();
  const res = await fetch(URL_ + '/v1/embeddings', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ input, model: MODEL }) });
  const body = await res.json();
  const ms = performance.now() - t;
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${JSON.stringify(body).slice(0, 200)}`);
  return { ms, tokens: body.usage?.prompt_tokens, dims: body.data[0].embedding.length };
}

/** Tokens the server counts for `text` (its /tokenize, plus CLS and SEP). */
export async function tokens(text) {
  const res = await fetch(URL_ + '/tokenize', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ content: text }) });
  return (await res.json()).tokens.length + 2;
}

/** The container's cgroup memory.stat (anon, file) and memory.current / memory.peak, bytes. */
export function containerMem() {
  const out = execFileSync('docker', ['exec', CONTAINER, 'cat', '/sys/fs/cgroup/memory.stat', '/sys/fs/cgroup/memory.current', '/sys/fs/cgroup/memory.peak'], { encoding: 'utf8' });
  const lines = out.trim().split('\n');
  const stat = Object.fromEntries(lines.filter(l => l.includes(' ')).map(l => l.split(' ')).map(([k, v]) => [k, Number(v)]));
  const tail = lines.filter(l => !l.includes(' ')).map(Number);
  return { at: now(), anon: stat.anon, file: stat.file, current: tail[0], peak: tail[1] };
}

/** Poll the store's status until `vector` is "ok" (or the deadline). Returns the trace. */
export async function waitVectorOk(store, { timeoutMs = 3600000, every = 1000, onTick } = {}) {
  const started = performance.now(), trace = [];
  let last;
  while (performance.now() - started < timeoutMs) {
    const v = store.statusLine().vector;
    if (v !== last) { trace.push({ ms: Math.round(performance.now() - started), vector: v }); last = v; }
    if (v === 'ok') return { ms: performance.now() - started, trace };
    onTick?.(v);
    await sleep(every);
  }
  throw new Error('vector not ok within ' + timeoutMs + ' ms: ' + last);
}

export function save(dir, name, data) {
  const file = join(dir, name);
  writeFileSync(file, typeof data === 'string' ? data : JSON.stringify(data, null, 2) + '\n');
  return file;
}

/** A seeded PRNG (mulberry32). */
export function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ------------------------------------------------------------------ synthetic text
// Everyday topics that are none of the item-7 targets' meanings (no domains, renewals,
// bookkeeping, phone apps, meetings or early hours).
const ZH = {
  做饭: [['今晚试着做了红烧排骨', '番茄炒蛋多放了一勺糖', '冰箱里的豆腐快过期了', '周末想学包饺子', '买了一口新的铸铁锅'],
    ['味道比上次好很多', '下次要少放点盐', '邻居说可以加点香菜', '火候还是没掌握好', '孩子吃得很开心']],
  植物: [['阳台上的薄荷长得很快', '那盆绿萝的叶子发黄了', '多肉被晒伤了一片', '院子里种的番茄开花了', '朋友送了一盆兰花'],
    ['可能是浇水太多', '打算换一个大一点的花盆', '查了说要放在散射光下', '准备周末施一次肥', '先剪掉坏掉的叶子再看']],
  猫: [['猫今天把杯子推到了地上', '小橘最近掉毛特别厉害', '猫砂盆该换新的了', '带猫去打了第二针疫苗', '猫半夜在客厅跑酷'],
    ['只好把桌面收拾干净', '买了一把新梳子', '换成豆腐猫砂试试', '医生说体重刚好', '吵得楼下来敲门']],
  天气: [['今天风特别大', '外面下了一整天小雨', '降温了十几度', '雾霾有点严重', '难得出了大太阳'],
    ['出门记得带伞', '把厚外套找出来了', '晾的衣服一天都没干', '在家看了一下午书', '去河边走了一圈']],
  读书: [['刚读完一本关于古罗马的历史书', '图书馆借的小说下周到期', '这本诗集的译本很好', '在读一本讲海洋生物的科普', '书架快放不下了'],
    ['作者的叙述很有画面感', '有几章写得有点拖', '打算再买一本同作者的', '摘抄了好几段', '准备送一部分给表弟']],
  出行: [['火车晚点了四十分钟', '自行车的后胎又瘪了', '去山里徒步走了十五公里', '高速上堵了两个小时', '地铁换乘通道在维修'],
    ['只好在站台上看风景', '找了家店补胎', '腿酸了两天', '下次换个时间出发', '绕了一大圈才到']],
};
const EN = {
  cooking: [['Tried a new lentil soup recipe tonight', 'The sourdough starter finally bubbled', 'Burned the garlic bread again', 'Bought a cast iron pan at the flea market', 'Made pancakes with the kids'],
    ['it needed more salt', 'the crust came out perfect', 'the smoke alarm went off', 'it was cheaper than expected', 'everyone asked for seconds']],
  garden: [['The basil on the balcony is thriving', 'Snails ate half the lettuce', 'Planted tulip bulbs along the fence', 'The apple tree needs pruning', 'Repotted the fiddle leaf fig'],
    ['after a week of sun', 'so I put copper tape around the bed', 'before the ground freezes', 'when the leaves fall', 'and it already looks happier']],
  pets: [['The dog chewed through another leash', 'Our cat learned to open the cupboard', 'Took the rabbit to the vet', 'The parrot keeps copying the doorbell', 'Walked the dog along the river'],
    ['so we bought a chain one', 'and stole a bag of treats', 'its teeth were too long', 'which confuses every visitor', 'and met three other spaniels']],
  weather: [['A huge storm rolled in this afternoon', 'It snowed for the first time this year', 'The heat wave finally broke', 'Thick fog covered the valley', 'Strong wind knocked over the bins'],
    ['and the power flickered twice', 'so the kids built a snowman', 'and we opened every window', 'until almost noon', 'and scattered rubbish down the street']],
  books: [['Finished a novel about a lighthouse keeper', 'Started reading a biography of Darwin', 'The poetry collection arrived today', 'Returned the library books late', 'Found an old atlas in the attic'],
    ['and the ending surprised me', 'and the early chapters are slow', 'with lovely illustrations', 'and paid a small fine', 'with borders that no longer exist']],
  travel: [['The train to the coast was delayed', 'Hiked twelve kilometres through the forest', 'Our flight landed in heavy rain', 'Rented bikes to explore the old town', 'Missed the last ferry back'],
    ['so we watched the fields go by', 'and saw two deer near the ridge', 'and the luggage took forever', 'and found a tiny bakery', 'and stayed in a small inn']],
};

function sentences(table, joiner, end) {
  const out = [];
  for (const [, [a, b]] of Object.entries(table)) for (const x of a) for (const y of b) out.push(x + joiner + y + end);
  return out;
}
export const ZH_SENTENCES = sentences(ZH, '，', '。');
export const EN_SENTENCES = sentences(EN, ', ', '.');

/** A text of about `chars` code points from the sentence pools (zh or en). */
export function textOf(rand, chars, lang) {
  const pool = lang === 'zh' ? ZH_SENTENCES : EN_SENTENCES;
  let s = '';
  while (Array.from(s).length < chars) s += (s && lang === 'en' ? ' ' : '') + pool[Math.floor(rand() * pool.length)];
  return Array.from(s).slice(0, chars).join('');
}

/** Write `entries` ({id?, at, role, text}) as turns of `per` messages, then flush. */
export async function writeTurns(store, prefix, entries, per = 25) {
  for (let i = 0; i < entries.length; i += per) {
    const chunk = entries.slice(i, i + per);
    await store.recordTurn(`${prefix}-${i / per}`, chunk.map((e, j) => ({ role: e.role ?? (j % 2 ? 'steward' : 'owner'), text: e.text, at: e.at })));
  }
  await store.flush();
}
