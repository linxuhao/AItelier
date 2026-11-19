/** Design-steward 3.8.4 item 12, memory (real model; not CI): the host's MemAvailable with
 *  the aitelier-embed container stopped and started again (three readings each, 10 s
 *  apart), and the container's memory.stat anon once warm. It stops and starts ONLY the
 *  aitelier-embed container, and leaves it running.
 *  Budgets (3.8.2): MemAvailable before - after <= 1.0 GB; anon <= 0.4 GB.
 *  The host runs other work: MemAvailable moves by gigabytes on its own (see the readings).
 *  Usage: node item12-memory.mjs <absolute scratch dir> */
import { execFile as execFileCb } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { promisify } from 'node:util';
import { CONTAINER, URL_, containerMem, embedRaw, loadavg, memAvailableKb, now, r1, rng, save, scratchRoot, sleep, textOf } from './common.mjs';

const execFile = promisify(execFileCb);
const { dir } = scratchRoot(process.argv[2], 'item12-memory');
const docker = async (...args) => (await execFile('docker', args)).stdout.trim();
const meminfo = () => Object.fromEntries(readFileSync('/proc/meminfo', 'utf8').trim().split('\n').map(l => l.split(/:\s+/)).filter(([k]) =>
  ['MemTotal', 'MemFree', 'MemAvailable', 'Cached', 'SwapTotal', 'SwapFree', 'AnonPages', 'Mapped'].includes(k)).map(([k, v]) => [k, Number(v.split(' ')[0])]));
async function readings(label) {
  const out = [];
  for (let i = 0; i < 3; i++) {
    if (i) await sleep(10000);
    out.push({ label, at: now(), mem_available_kb: memAvailableKb(), loadavg: loadavg(), meminfo: meminfo() });
  }
  return out;
}
const mean = rs => rs.reduce((s, r) => s + r.mem_available_kb, 0) / rs.length;
const gb = kb => Math.round(kb / 1024 / 1024 * 1000) / 1000;

const report = { item: '12-memory', started: now(), loadavg_start: loadavg() };
report.running_initial = await readings('running (initial)');
report.mem_running_initial = containerMem();
const t0 = performance.now();
await docker('stop', CONTAINER);
report.stop_ms = Math.round(performance.now() - t0);
await sleep(5000);
report.stopped = await readings('stopped');
const t1 = performance.now();
await docker('start', CONTAINER);
let healthMs;
for (;;) {
  try { const r = await fetch(URL_ + '/health'); if (r.ok) { healthMs = Math.round(performance.now() - t1); break; } } catch {}
  await sleep(100);
}
report.start_to_health_ms = healthMs;
const rand = rng(1212);
report.first_after_start = await embedRaw('早上会议的偏好');
for (let i = 0; i < 3; i++) await embedRaw('warm ' + i);
for (let i = 0; i < 2; i++) await embedRaw(textOf(rand, 1000, 'zh'));
await sleep(5000);
report.started_again = await readings('running (after start + warm)');
report.mem_warm = containerMem();
report.state = await docker('inspect', CONTAINER, '--format', '{{.State.Status}} restarts={{.RestartCount}} oom={{.State.OOMKilled}} started={{.State.StartedAt}}');

const before = mean(report.stopped), after = mean(report.started_again);
report.mem_available = {
  stopped_gb: report.stopped.map(r => gb(r.mem_available_kb)), running_gb: report.started_again.map(r => gb(r.mem_available_kb)),
  initial_running_gb: report.running_initial.map(r => gb(r.mem_available_kb)),
  delta_gb: gb(before - after), delta_vs_initial_gb: gb(before - mean(report.running_initial)),
  spread_stopped_gb: gb(Math.max(...report.stopped.map(r => r.mem_available_kb)) - Math.min(...report.stopped.map(r => r.mem_available_kb))),
  spread_running_gb: gb(Math.max(...report.started_again.map(r => r.mem_available_kb)) - Math.min(...report.started_again.map(r => r.mem_available_kb))),
};
report.verdict = { mem_available: report.mem_available.delta_gb <= 1.0 ? 'PASS' : 'FAIL', anon: report.mem_warm.anon <= 0.4 * 2 ** 30 ? 'PASS' : 'FAIL' };
report.anon_gb = r1(report.mem_warm.anon / 2 ** 30 * 100) / 100;
report.finished = now();
report.loadavg_end = loadavg();
save(dir, 'item12-memory.json', report);
console.log(JSON.stringify({ mem_available: report.mem_available, anon: report.mem_warm, verdict: report.verdict, start_to_health_ms: healthMs, first_after_start_ms: report.first_after_start.ms, state: report.state }, null, 1));
