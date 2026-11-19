/** Design-steward 3.8.4 item 11 (offline) and the backfill / write parts of item 12 (real
 *  model; not CI), through the plugin's MemoryStore against the running aitelier-embed:
 *   - a non-loopback vector url is refused by the config check and by MemoryStore;
 *   - run.sh exits 78 on a model sha256 mismatch (a copy of run.sh beside an all-zero
 *     model.sha256; nothing is created);
 *   - the image's smoke under --network=none;
 *   - the container's /proc/net/tcp{,6}, sampled every 0.5 s through a 1000-entry backfill,
 *     200 searches and a second backfill with searches and writes: only the listener and
 *     inbound connections from the network's gateway (Docker's DNS listener on 127.0.0.11
 *     is reported apart: every user-defined network has it);
 *   - backfill: 26 entries (about today's 16k characters; budget <= 1 min) and 1000 entries
 *     (throughput, entries/s and tokens/s, and the 100k extrapolation);
 *   - searches during a backfill (report only) and write p95 with vectors on (<= 200 ms),
 *     beside the same with vectors off.
 *  Usage: node item11-backfill.mjs <absolute scratch dir> */
import { execFile as execFileCb } from 'node:child_process';
import { copyFileSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { CONTAINER, HOST, MODEL, STORE, URL_, USER, containerMem, loadavg, now, r1, rng, save, scratchRoot, sleep, summary, textOf, tokens, waitVectorOk, writeTurns } from './common.mjs';

const execFile = promisify(execFileCb);
const { MemoryStore } = await import(STORE);
const { memoryVectorProblem } = await import(HOST);
const { dir, root } = scratchRoot(process.argv[2], 'item11-root');
const rand = rng(11);
const report = { item: '11 + 12 backfill/write', started: now(), loadavg_start: loadavg() };
const here = new URL('..', import.meta.url).pathname;

// ---------------------------------------------------------------- 1. config refuses non-loopback
const urls = ['http://192.168.16.2:8080', 'http://10.0.0.5:18751', 'http://example.com:18751', 'https://127.0.0.1:18751', 'http://0.0.0.0:18751',
  'http://127.0.0.1.nip.io:18751', 'http://user:pw@127.0.0.1:18751', 'http://127.0.0.2:18751', 'http://[::ffff:127.0.0.1]:18751',
  'http://127.0.0.1:18751', 'http://localhost:18751', 'http://[::1]:18751'];
report.config = urls.map(url => ({ url, problem: memoryVectorProblem({ vector: { url, model: MODEL } }) ?? null }));
try {
  await MemoryStore.open({ root: join(root, 'refused'), userId: USER, vector: { url: 'http://192.168.16.2:8080', model: MODEL } });
  report.config_store = 'OPENED (wrong)';
} catch (error) { report.config_store = 'refused: ' + error.message; }
const ACCEPT = new Set(['http://127.0.0.1:18751', 'http://localhost:18751', 'http://[::1]:18751']);
report.config_pass = report.config.every(c => ACCEPT.has(c.url) === (c.problem === null)) && report.config_store.startsWith('refused');

// ---------------------------------------------------------------- 2. run.sh exits 78 on a sha mismatch
const docker = async (...args) => (await execFile('docker', args, { maxBuffer: 1 << 26 })).stdout.trim();
{
  const box = join(root, 'runsh'); mkdirSync(box);
  copyFileSync(join(here, 'run.sh'), join(box, 'run.sh'));
  writeFileSync(join(box, 'model.sha256'), '0'.repeat(64) + '  bge-m3-q8_0.gguf\n');
  const before = { containers: await docker('ps', '-a', '--format', '{{.ID}} {{.Names}}'), networks: await docker('network', 'ls', '--format', '{{.Name}}') };
  const t = performance.now();
  let code = 0, stderr = '';
  try { await execFile('sh', [join(box, 'run.sh')], { env: { ...process.env, EMBED_PORT: '18799' } }); }
  catch (error) { code = error.code; stderr = error.stderr; }
  const after = { containers: await docker('ps', '-a', '--format', '{{.ID}} {{.Names}}'), networks: await docker('network', 'ls', '--format', '{{.Name}}') };
  report.runsh = { exit: code, ms: Math.round(performance.now() - t), stderr: String(stderr).trim(), nothing_created: before.containers === after.containers && before.networks === after.networks };
  report.runsh_pass = code === 78 && report.runsh.nothing_created;
}

// ---------------------------------------------------------------- 3. smoke under --network=none
{
  const r = await execFile('docker', ['run', '--rm', '--network', 'none', '--entrypoint', 'sh', 'aitelier-embed:local', '-c',
    'llama-server --version 2>&1 && llama-server --help 2>&1 | grep -q -- --offline && echo SMOKE_OK']).catch(e => e);
  report.smoke = { exit: r.code ?? 0, out: String(r.stdout ?? '').trim().split('\n').slice(-3) };
  report.smoke_pass = report.smoke.out.includes('SMOKE_OK');
}

// ---------------------------------------------------------------- 4. the connection table sampler
const gateway = await docker('network', 'inspect', 'aitelier-embed', '--format', '{{range .IPAM.Config}}{{.Gateway}}{{end}}');
const containerIp = await docker('inspect', CONTAINER, '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}');
const STATES = { '01': 'ESTABLISHED', '02': 'SYN_SENT', '03': 'SYN_RECV', '04': 'FIN_WAIT1', '05': 'FIN_WAIT2', '06': 'TIME_WAIT', '07': 'CLOSE', '08': 'CLOSE_WAIT', '09': 'LAST_ACK', '0A': 'LISTEN', '0B': 'CLOSING' };
const ip4 = hex => [0, 2, 4, 6].map(i => parseInt(hex.slice(i, i + 2), 16)).reverse().join('.');
function addr(hex) {
  const [h, p] = hex.split(':');
  const ip = h.length === 8 ? ip4(h) : h.length === 32 && h.startsWith('0000000000000000FFFF0000') ? '::ffff:' + ip4(h.slice(24)) : h.length === 32 && /^0+$/.test(h) ? '::' : h;
  return { ip, port: parseInt(p, 16) };
}
const tcp = { samples: 0, failed: 0, kinds: new Map(), unexpected: [], max_conns: 0 };
function classify(family, line) {
  const f = line.trim().split(/\s+/);
  const local = addr(f[1]), remote = addr(f[2]), state = STATES[f[3]] ?? f[3], uid = f[7];
  let kind;
  if (state === 'LISTEN' && local.port === 8080 && (local.ip === '0.0.0.0' || local.ip === '::')) kind = 'listener :8080';
  else if (state === 'LISTEN' && local.ip === '127.0.0.11') kind = 'docker DNS listener 127.0.0.11';
  else if (local.port === 8080 && remote.ip.replace('::ffff:', '') === gateway) kind = `inbound from gateway ${gateway} (${state})`;
  else kind = 'UNEXPECTED';
  const desc = `${family} ${local.ip}:${local.port} -> ${remote.ip}:${remote.port} ${state} uid=${uid}`;
  return { kind, desc };
}
let sampling = true;
const sampler = (async () => {
  while (sampling) {
    try {
      const out = await docker('exec', CONTAINER, 'sh', '-c', 'echo TCP4; cat /proc/net/tcp; echo TCP6; cat /proc/net/tcp6');
      let family = '', conns = 0;
      for (const line of out.split('\n')) {
        if (line === 'TCP4' || line === 'TCP6') { family = line.toLowerCase(); continue; }
        if (!line.includes(':') || line.trim().startsWith('sl')) continue;
        const c = classify(family, line);
        if (c.kind.startsWith('inbound')) conns++;
        tcp.kinds.set(c.kind, (tcp.kinds.get(c.kind) ?? 0) + 1);
        if (c.kind === 'UNEXPECTED' && tcp.unexpected.length < 50) tcp.unexpected.push({ at: now(), line: c.desc });
      }
      tcp.max_conns = Math.max(tcp.max_conns, conns);
      tcp.samples++;
    } catch { tcp.failed++; }
    await sleep(500);
  }
})();

// ---------------------------------------------------------------- helpers
function corpus(n, meanLong, seedText) {
  // Lengths: 30% short (20-150), 40% medium (150-900), 30% long (900-1600): mean about 600,
  // like today's memory (26 entries, about 16k characters).
  const start = Date.now() - 300 * 86400000;
  return Array.from({ length: n }, (_, i) => {
    const r = rand();
    const len = r < 0.3 ? 20 + Math.floor(rand() * 130) : r < 0.7 ? 150 + Math.floor(rand() * 750) : 900 + Math.floor(rand() * meanLong);
    return { text: `${seedText}${i} ` + textOf(rand, len, i % 2 ? 'en' : 'zh'), at: new Date(start + Math.round((i + rand()) * (300 * 86400000 / n))).toISOString() };
  });
}
const vectorOn = { url: URL_, model: MODEL };
async function backfill(name, entries) {
  const r = join(root, name);
  let store = await MemoryStore.open({ root: r, userId: USER });
  await writeTurns(store, name, entries, 25);
  await store.close();
  const load = loadavg();
  const t = performance.now();
  store = await MemoryStore.open({ root: r, userId: USER, vector: vectorOn });
  await store.vector.whenLoaded();
  const loadedMs = performance.now() - t;
  const wait = await waitVectorOk(store, { every: 250 });
  const totalMs = performance.now() - t;
  const embedMs = totalMs - loadedMs;
  let toks = 0;
  for (const e of entries) toks += await tokens(Array.from(e.text).slice(0, 1000).join(''));
  const chars = entries.reduce((s, e) => s + Array.from(e.text).length, 0);
  return { store, root: r, result: { entries: entries.length, chars, embedded_tokens: toks, open_to_ok_ms: Math.round(totalMs), load_ms: Math.round(loadedMs),
    entries_per_s: r1(entries.length / (embedMs / 1000)), tokens_per_s: Math.round(toks / (embedMs / 1000)),
    trace: wait.trace.slice(0, 5).concat(wait.trace.slice(-3)), loadavg_start: load, loadavg_end: loadavg() } };
}

// ---------------------------------------------------------------- 5. backfill: 26 entries (today's size)
{
  const { store, result } = await backfill('today26', corpus(26, 700, 'd'));
  report.backfill26 = result;
  report.backfill26.verdict = result.open_to_ok_ms <= 60000 ? 'PASS' : 'FAIL';
  await store.close();
}

// ---------------------------------------------------------------- 6. backfill: 1000 entries, then 200 searches
const SEARCHES = ['域名续费', 'expense tracker on mobile', '早上会议的偏好', '猫的疫苗', 'train delayed', '阳台的番茄', 'storm power cut', '做饭放盐',
  'library books', '徒步', 'sourdough', '图书馆', 'dog leash', '降温', 'flea market pan', '诗集'];
let big;
{
  const b = await backfill('backfill1000', corpus(1000, 700, 'b'));
  big = b;
  report.backfill1000 = b.result;
  report.backfill1000.extrapolate_100k_h = r1(100000 / b.result.entries_per_s / 3600);
  report.mem_after_backfill = containerMem();
  const ms = [];
  const load = loadavg();
  for (let i = 0; i < 200; i++) {
    const t = performance.now();
    const r = await b.store.search(SEARCHES[i % SEARCHES.length] + (i >= 16 ? ' ' + i : ''), 10);
    ms.push(performance.now() - t);
    if (r.ranked.vector !== 'ok') report.search_not_ok = (report.search_not_ok ?? 0) + 1;
  }
  report.search1000 = { ...summary(ms), loadavg_start: load, loadavg_end: loadavg() };
  await b.store.close();
}

// ---------------------------------------------------------------- 7. a backfill again, with searches and writes during it
{
  rmSync(join(big.root, USER + '.vectors.jsonl'));
  const load = loadavg();
  const store = await MemoryStore.open({ root: big.root, userId: USER, vector: vectorOn });
  await store.vector.whenLoaded();
  const searches = [], notes = [];
  let backlogSeen = 0;
  const searching = (async () => {
    for (let i = 0; i < 50; i++) {
      const t = performance.now();
      const r = await store.search(SEARCHES[i % SEARCHES.length], 10);
      searches.push(performance.now() - t);
      if (String(r.ranked.vector).startsWith('backlog')) backlogSeen++;
    }
  })();
  for (let i = 0; i < 100; i++) {
    const t = performance.now();
    await store.note(`写入测试 ${i}: ` + textOf(rand, 60 + Math.floor(rand() * 400), i % 2 ? 'en' : 'zh'));
    const onDisk = performance.now() - t;
    await store.flush();
    notes.push({ on_disk_ms: onDisk, committed_ms: performance.now() - t });
    await sleep(100);
  }
  await searching;
  const writeP95 = [...store.writeMs].sort((a, b) => a - b)[Math.floor(store.writeMs.length * 0.95)];
  report.during_backfill = {
    searches: { ...summary(searches), with_backlog_status: backlogSeen },
    writes_on_disk: summary(notes.map(n => n.on_disk_ms)), writes_committed: summary(notes.map(n => n.committed_ms)),
    store_write_p95_ms: r1(writeP95), store_over_budget: store.statusLine().over_budget ?? [], vector_at_end: store.statusLine().vector,
    loadavg_start: load, loadavg_end: loadavg() };
  report.during_backfill.verdict = writeP95 <= 200 ? 'PASS' : 'FAIL';
  await waitVectorOk(store, { every: 500 });
  await store.close();
}
sampling = false;
await sampler;
report.tcp = { gateway, container_ip: containerIp, samples: tcp.samples, failed: tcp.failed, max_inbound_at_once: tcp.max_conns,
  kinds: Object.fromEntries(tcp.kinds), unexpected: tcp.unexpected };
report.tcp_pass = tcp.samples > 0 && !tcp.kinds.has('UNEXPECTED');

// ---------------------------------------------------------------- 8. writes with vectors off (the same store), for comparison
{
  const load = loadavg();
  const store = await MemoryStore.open({ root: big.root, userId: USER });
  for (let i = 0; i < 100; i++) {
    await store.note(`对照写入 ${i}: ` + textOf(rand, 60 + Math.floor(rand() * 400), i % 2 ? 'en' : 'zh'));
    await store.flush();
    await sleep(100);
  }
  const p95 = [...store.writeMs].sort((a, b) => a - b)[Math.floor(store.writeMs.length * 0.95)];
  report.writes_vector_off = { store_write_p95_ms: r1(p95), loadavg_start: load, loadavg_end: loadavg() };
  await store.close();
}

report.mem_end = containerMem();
report.finished = now();
report.loadavg_end = loadavg();
save(dir, 'item11-backfill.json', report);
console.log(JSON.stringify({ config: report.config_pass, runsh: report.runsh, smoke: report.smoke_pass, tcp: report.tcp, b26: report.backfill26, b1000: report.backfill1000,
  search1000: report.search1000, during: report.during_backfill, off: report.writes_vector_off }, null, 1));
