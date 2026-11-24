/** Step 24.4b (design-steward 7.8.24 "十", "十四" 24.4b): the steward host's calculate sandbox
 *  (calc.mjs) against a FAKE docker (test-fixtures/fake-docker-calc.mjs: no daemon, no image, no
 *  python; it records every argv and answers from a plan) - the pinned container arguments and
 *  everything they must not have, no model string on a command line and no writable host mount,
 *  the image id check, the exit mapping, the frame's bounds applied by the host, the one slot with
 *  the owner first, the wall clock and its kill, startup cleanup, the self-check and its retry,
 *  the logs; and calcMediaOps (inputs in, outputs stored by magic and audience) over a real media
 *  library. The wrapper itself (docker/calc/run.py) is tested in tests/unit/test_calc_run.py; the
 *  real image in calc.integration.mjs (only with AITELIER_CALC_IT_IMAGE). */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createCalcRunner, calcMediaOps, calcRunArgs, calcHostLabel, outputType, parseFrame, CALC_LIMITS, CALC_LABEL, CALC_USER,
  CALC_OUTPUTS, SELFCHECK_CODE } from './calc.mjs';
import { basicsFlag, mediaOps } from './steward-host.mjs';
import { createMediaLibrary } from '../dsh-director/lib/media.mjs';
import { ooxml } from '../dsh-director/test/fixtures/zip-samples.mjs';

const IMAGE = 'sha256:' + 'cd'.repeat(32);
const FAKE = fileURLToPath(new URL('./test-fixtures/fake-docker-calc.mjs', import.meta.url));
const OK_INSPECT = { code: 0, stdout: IMAGE + '\n' };
const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const SECRET_CODE = 'print("SECRET-CODE-7f3")\n';
const png = (width = 64, height = 64) => {
  const head = Buffer.alloc(64);
  Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex').copy(head, 0);
  head.writeUInt32BE(width, 16); head.writeUInt32BE(height, 20);
  return head;
};
const jpeg = (width = 10, height = 10) => Buffer.concat([Buffer.from('ffd8ffe000104a46494600010100000100010000', 'hex'),
  Buffer.from([0xff, 0xc0, 0x00, 0x11, 0x08, height >> 8, height & 255, width >> 8, width & 255, 0x03]), Buffer.alloc(64)]);
const pdf = () => Buffer.concat([Buffer.from('%PDF-1.7\n'), Buffer.alloc(100)]);

function rig(plan = {}, options = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'calc-'));
  const docker = join(dir, 'docker');
  copyFileSync(FAKE, docker);
  chmodSync(docker, 0o755);
  const files = { log: join(dir, 'calls.jsonl'), plan: join(dir, 'plan.json'), capture: join(dir, 'capture') };
  writeFileSync(files.log, '');
  const setPlan = next => writeFileSync(files.plan, JSON.stringify({ inspect: OK_INSPECT, ...next }));
  setPlan(plan);
  const logs = [];
  let clock = 1_000_000;
  const tmpRoot = join(dir, 'state', 'calc-tmp');
  const runner = createCalcRunner({ image: IMAGE, docker, tmpRoot, log: line => logs.push(line), now: () => clock,
    env: { PATH: process.env.PATH, FAKE_DOCKER_LOG: files.log, FAKE_DOCKER_PLAN: files.plan, FAKE_DOCKER_CAPTURE: files.capture },
    ...options, limits: { ...(options.limits ?? {}) } });
  const calls = () => readFileSync(files.log, 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line));
  const runs = () => calls().filter(args => args[0] === 'run');
  const jobsLeft = () => (existsSync(tmpRoot) ? readdirSync(tmpRoot) : []);
  return { dir, docker, runner, logs, calls, runs, setPlan, files, tmpRoot, jobsLeft, tick: ms => { clock += ms; } };
}

test('no image configured, or not a pinned id: the feature is off - calc_unavailable no_image_configured, docker never called', async () => {
  for (const image of [undefined, '', 'aitelier-calc:latest', 'python:3.12-slim', 'sha256:' + 'a'.repeat(63), 'sha256:' + 'A'.repeat(64),
    'sha256:' + 'a'.repeat(64) + '\n', ' sha256:' + 'a'.repeat(64), 'sha512:' + 'a'.repeat(64)]) {
    const r = rig({}, { image });
    await r.runner.start();
    assert.deepEqual(await r.runner.run({ code: 'print(1)', inputs: [] }, { priority: 'owner' }),
      { refused: 'calc_unavailable', detail: 'no_image_configured' }, JSON.stringify(image));
    assert.deepEqual(r.calls(), [], JSON.stringify(image));
    assert.equal(r.runner.status().reason, 'no_image_configured');
    assert.ok(r.logs.includes('calc_unavailable: no_image_configured'));
  }
  // Off by configuration is not a failure on the status line; a failed self-check is.
  assert.equal(basicsFlag({ calc: { state: 'unavailable', reason: 'no_image_configured' } }), '');
  assert.equal(basicsFlag({ calc: { state: 'unavailable', reason: 'selfcheck_failed' } }), 'basics: calc_unavailable');
  assert.equal(basicsFlag({ calc: { state: 'pending' } }), '');
  assert.equal(basicsFlag({ office: { state: 'unavailable', reason: 'x' }, calc: { state: 'unavailable', reason: 'docker_unavailable' } }),
    'basics: office_unavailable; calc_unavailable');
});

test('the container: exactly the pinned arguments - no network, nobody, read-only, two bounded tmpfs, only <job>/in mounted, read-only', async () => {
  assert.deepEqual(calcRunArgs({ name: 'aitelier-calc-0011223344556677', inDir: '/s/calc-tmp/j-x/in', image: IMAGE, hostLabel: 'aitelier.calc.host=h' }),
    ['run', '--rm', '--name', 'aitelier-calc-0011223344556677', '--label', 'aitelier.calc=1', '--label', 'aitelier.calc.host=h',
      '--network', 'none', '--log-driver', 'none', '-u', '65534:65534', '--read-only',
      '--tmpfs', '/tmp:rw,size=64m,nr_inodes=1024', '--tmpfs', '/job/out:rw,size=48m,nr_inodes=64,mode=1777',
      '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '64', '--cpus', '1',
      '--memory', '512m', '--memory-swap', '512m', '-e', 'HOME=/tmp', '-v', '/s/calc-tmp/j-x/in:/job/in:ro', '-w', '/job/out', IMAGE]);
  assert.equal(CALC_USER, '65534:65534');
  assert.equal(CALC_LABEL, 'aitelier.calc=1');
  assert.deepEqual([CALC_LIMITS.wallMs, CALC_LIMITS.queueWaitMs, CALC_LIMITS.retryMs, CALC_LIMITS.frameBytes, CALC_LIMITS.stdoutBytes,
    CALC_LIMITS.stderrBytes, CALC_LIMITS.files, CALC_LIMITS.fileBytes], [30000, 30000, 600000, 56 * 1024 * 1024, 16384, 2048, 4, 10 * 1024 * 1024]);

  const r = rig({ run: { frame: { stdout: 'ok\n' } } });
  await r.runner.start();
  const source = join(r.dir, 'SECRET-NAME-家庭开销.csv');
  writeFileSync(source, 'a,b\n1,2\n');
  const ran = await r.runner.run({ code: SECRET_CODE, inputs: [{ path: source, ext: 'csv' }, { path: source, ext: '../x' }] }, { priority: 'owner' });
  assert.equal(ran.result?.stdout, 'ok\n', JSON.stringify(ran));
  const args = r.runs().at(-1);
  const at = flag => args.flatMap((a, i) => (a === flag ? [args[i + 1]] : []));
  // What the arguments must not have, each spelled out (7.8.24 "十" 2).
  for (const banned of ['--privileged', '--device', '--pid', '--ipc', '--uts', '--userns', '--cap-add', '--network=host', '--net', '--add-host',
    '--volumes-from', '--mount', '--entrypoint', '--group-add', '--runtime', '--env-file'])
    assert.ok(!args.some(a => a === banned || a.startsWith(banned + '=')), banned);
  assert.ok(!args.some(a => /unconfined|docker\.sock|seccomp|apparmor/.test(a)), 'no unconfined profile, no docker socket');
  assert.deepEqual(at('--network'), ['none']);
  assert.deepEqual(at('-u'), ['65534:65534']);
  assert.deepEqual(at('--memory'), at('--memory-swap'));
  assert.deepEqual(at('-e'), ['HOME=/tmp']);
  // The one host path: this job's own input directory under calc-tmp, read-only. No writable host mount.
  const mounts = at('-v');
  assert.equal(mounts.length, 1);
  assert.match(mounts[0], /:\/job\/in:ro$/);
  assert.ok(mounts[0].startsWith(join(r.tmpRoot, 'j-')) && mounts[0].split(':')[0].endsWith('/in'), mounts[0]);
  assert.equal(args.filter(a => a.startsWith('/') && !a.startsWith('/job/') && !a.startsWith('/tmp:')).length, 1, 'no other host path');
  assert.equal(args.at(-1), IMAGE, 'the pinned id last: no command after it, the image\'s entrypoint runs');
  // No model or user string on the command line: not the code, not a file's name.
  assert.ok(!args.some(a => a.includes('SECRET')), 'the code and the file names are never on a command line');
  // The program is main.py, the inputs f1.<ext>, f2.<ext> in order (a bad extension: bin); 0755 / 0644 for nobody.
  assert.equal(readFileSync(join(r.files.capture, 'main.py'), 'utf8'), SECRET_CODE);
  assert.equal(readFileSync(join(r.files.capture, 'f1.csv'), 'utf8'), 'a,b\n1,2\n');
  assert.ok(existsSync(join(r.files.capture, 'f2.bin')));
  assert.deepEqual(JSON.parse(readFileSync(join(r.files.capture, 'modes.json'), 'utf8')), { '.': '755', 'main.py': '644', 'f1.csv': '644', 'f2.bin': '644' });
  assert.deepEqual(r.jobsLeft(), [], 'the job directory is removed');
});

test('start: an earlier process\'s labelled containers and job directories go, then the self-check reads 1024 back', async () => {
  const r = rig({ ps: 'abc123def456\n0123456789ab\n--evil\n' });
  mkdirSync(join(r.tmpRoot, 'j-old'), { recursive: true });
  writeFileSync(join(r.tmpRoot, 'j-old', 'main.py'), 'left');
  await r.runner.start();
  const calls = r.calls();
  assert.match(r.runner.label, /^aitelier\.calc\.host=[0-9a-f]{16}$/);
  assert.equal(r.runner.label, calcHostLabel(r.tmpRoot));
  assert.deepEqual(calls[0], ['ps', '-aq', '--filter', `label=${r.runner.label}`]);
  assert.deepEqual(calls[1], ['rm', '-f', 'abc123def456', '0123456789ab']);
  assert.deepEqual(calls[2], ['image', 'inspect', '--format', '{{.Id}}', IMAGE]);
  assert.equal(calls[3][0], 'run');
  assert.deepEqual(r.jobsLeft(), []);
  assert.equal(r.runner.status().state, 'ready');
  assert.ok(r.logs.some(line => line.startsWith('calc_ready ')));
  assert.equal(SELFCHECK_CODE, 'print(2**10)\n');
});

test('the self-check\'s reasons (closed: selfcheck_failed, docker_unavailable), and a failed one is tried again at most once per 10 minutes', async () => {
  const cases = [
    [{ inspect: { code: 1, stderr: 'Error response from daemon: No such image: sha256:cd' } }, 'selfcheck_failed'],
    [{ inspect: { code: 1, stderr: 'Cannot connect to the Docker daemon' } }, 'docker_unavailable'],
    [{ inspect: { code: 0, stdout: 'sha256:' + 'ef'.repeat(32) } }, 'selfcheck_failed'],
    [{ selfcheck: { stdout: '1023\n' } }, 'selfcheck_failed'],
    [{ selfcheck: { code: 125 } }, 'docker_unavailable'],
    [{ selfcheck: { code: 1 } }, 'selfcheck_failed'],
  ];
  for (const [plan, reason] of cases) {
    const r = rig(plan);
    await r.runner.start();
    assert.equal(r.runner.status().reason, reason, JSON.stringify(plan));
    assert.ok(r.logs.includes(`calc_unavailable: ${reason}`));
    assert.equal(basicsFlag({ calc: r.runner.status() }), 'basics: calc_unavailable');
  }
  const missing = rig({}, { docker: '/nonexistent/docker' });
  await missing.runner.start();
  assert.equal(missing.runner.status().reason, 'docker_unavailable');
  const unrooted = rig({}, { tmpRoot: 'relative/calc-tmp' });
  await unrooted.runner.start();
  assert.equal(unrooted.runner.status().reason, 'selfcheck_failed');
  // Retry: within 10 minutes a call answers calc_unavailable without asking docker again.
  const r = rig({ selfcheck: { stdout: 'nope' } });
  await r.runner.start();
  const before = r.calls().length;
  assert.deepEqual(await r.runner.run({ code: 'print(1)' }, { priority: 'owner' }), { refused: 'calc_unavailable', detail: 'selfcheck_failed' });
  assert.equal(r.calls().length, before);
  r.setPlan({ run: { frame: { stdout: '1\n' } } });
  r.tick(CALC_LIMITS.retryMs - 1);
  assert.deepEqual(await r.runner.run({ code: 'print(1)' }, { priority: 'owner' }), { refused: 'calc_unavailable', detail: 'selfcheck_failed' });
  assert.equal(r.calls().length, before);
  r.tick(2);
  const ran = await r.runner.run({ code: 'print(1)' }, { priority: 'owner' });
  assert.equal(ran.result?.stdout, '1\n', JSON.stringify(ran));
  assert.equal(r.runner.status().state, 'ready');
});

test('the wall clock: past it the container is killed by its name (awaited), timeout, the job directory removed', async () => {
  const r = rig({ run: { sleepMs: 5000 }, kill: { sleepMs: 100 } }, { limits: { wallMs: 300 } });
  await r.runner.start();
  const ran = await r.runner.run({ code: 'while True: pass' }, { priority: 'owner' });
  assert.deepEqual(ran, { refused: 'timeout' });
  const calls = r.calls();
  const name = r.runs().at(-1)[r.runs().at(-1).indexOf('--name') + 1];
  assert.ok(name.startsWith('aitelier-calc-'));
  assert.deepEqual(calls.filter(c => c[0] === 'kill'), [['kill', name]]);
  assert.deepEqual(calls.at(-1), ['kill-done', name], 'the answer waited for the kill');
  assert.deepEqual(r.jobsLeft(), []);
  // A caller's abort is the same kill.
  const s = rig({ run: { sleepMs: 5000 } });
  await s.runner.start();
  const controller = new AbortController();
  setTimeout(() => controller.abort(), 200);
  assert.deepEqual(await s.runner.run({ code: 'x' }, { priority: 'owner' }, controller.signal), { refused: 'timeout' });
  assert.equal(s.calls().filter(c => c[0] === 'kill').length, 1);
});

test('one at a time: a second call waits, busy past the queue wait; an owner\'s call goes before the groups\' already waiting', async () => {
  const r = rig({ run: { sleepMs: 400, frame: { stdout: 'x' } } }, { limits: { queueWaitMs: 150 } });
  await r.runner.start();
  const first = r.runner.run({ code: 'a' }, { priority: 'group' });
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.deepEqual(await r.runner.run({ code: 'b' }, { priority: 'owner' }), { refused: 'busy' });
  assert.ok((await first).result);
  // A waiter that aborts gives its place up: timeout.
  const q = rig({ run: { sleepMs: 300, frame: {} } });
  await q.runner.start();
  const held = q.runner.run({ code: 'a' }, { priority: 'group' });
  await new Promise(resolve => setTimeout(resolve, 50));
  const controller = new AbortController();
  const waiting = q.runner.run({ code: 'b' }, { priority: 'owner' }, controller.signal);
  controller.abort();
  assert.deepEqual(await waiting, { refused: 'timeout' });
  await held;

  // Order: a group call holds the slot; groups A and B wait; then an owner's call arrives and goes first.
  const o = rig({ run: { sleepMs: 150, frame: {} } });
  await o.runner.start();
  const order = [];
  const call = (label, priority) => o.runner.run({ code: label }, { priority }).then(answer => { order.push(label); return answer; });
  const all = [call('g0', 'group')];
  await new Promise(resolve => setTimeout(resolve, 30));
  all.push(call('gA', 'group'), call('gB', 'group'));
  await new Promise(resolve => setTimeout(resolve, 30));
  all.push(call('o1', 'owner'), call('o2', 'owner'));
  const answers = await Promise.all(all);
  assert.ok(answers.every(answer => answer.result), JSON.stringify(answers));
  assert.deepEqual(order, ['g0', 'o1', 'o2', 'gA', 'gB']);
});

test('the frame is bounded by the host: stdout past 56 MiB killed, more than 4 files or a file over 10 MiB output_too_large, stdout and stderr cut', async () => {
  // A stream past the frame bound: killed at once, nothing kept (the bound lowered to keep the test small).
  const r = rig({ run: { flood: 300000 } }, { limits: { frameBytes: 100000 } });
  await r.runner.start();
  assert.deepEqual(await r.runner.run({ code: 'print("x" * 10**9)' }, { priority: 'owner' }), { refused: 'output_too_large' });
  assert.equal(r.calls().filter(c => c[0] === 'kill').length, 1);
  assert.deepEqual(r.jobsLeft(), []);

  const five = { files: Array.from({ length: 5 }, (_, i) => ({ name: `p${i}.png`, hex: '89504e470d0a1a0a' })) };
  assert.deepEqual(parseFrame(Buffer.from(JSON.stringify({ v: 1, exit_code: 0, timed_out: false, stdout: '', stderr_tail: '',
    files: five.files.map(f => ({ name: f.name, data: Buffer.from(f.hex, 'hex').toString('base64') })) }))), { tooLarge: true });
  const f = rig({ run: { frame: five } });
  await f.runner.start();
  assert.deepEqual(await f.runner.run({ code: 'x' }, { priority: 'owner' }), { refused: 'output_too_large' });
  const big = rig({ run: { frame: { files: [{ name: 'a.png', size: 2048 }] } } }, { limits: { fileBytes: 1024 } });
  await big.runner.start();
  assert.deepEqual(await big.runner.run({ code: 'x' }, { priority: 'owner' }), { refused: 'output_too_large' });

  const long = rig({ run: { frame: { exit_code: 1, stdout: '数'.repeat(10000), stdout_truncated: false, stderr_tail: 'E'.repeat(5000) + 'Traceback END',
    files: [{ name: 'a.png', hex: '89504e470d0a1a0a' }] } } });
  await long.runner.start();
  const ran = await long.runner.run({ code: 'x' }, { priority: 'owner' });
  assert.equal(ran.result.exit_code, 1);
  assert.equal(ran.result.stdout, '数'.repeat(5461), 'cut at a character boundary within 16 KiB');
  assert.equal(ran.result.stdout_truncated, true);
  assert.equal(Buffer.byteLength(ran.result.stderr_tail), 2048);
  assert.ok(ran.result.stderr_tail.endsWith('Traceback END'));
  assert.equal(ran.result.files.length, 1);
  // Not a frame: nothing taken from it.
  for (const junk of ['', 'not json', '[]', '{"exit_code": "0"}', JSON.stringify({ exit_code: 0, timed_out: false, stdout: '', stderr_tail: '', files: [{}] })])
    assert.equal(parseFrame(Buffer.from(junk)), undefined, junk);
});

test('the exit mapping: docker not starting is calc_unavailable; any other exit, 137 included, is the program\'s result', async () => {
  for (const code of [125, 126, 127]) {
    const r = rig({ run: { code, stderr: 'docker: Error response from daemon' } });
    await r.runner.start();
    assert.deepEqual(await r.runner.run({ code: 'x' }, { priority: 'owner' }), { refused: 'calc_unavailable', detail: 'docker_unavailable' }, String(code));
  }
  const oom = rig({ run: { code: 137, stdout: '', stderr: 'Killed' } });
  await oom.runner.start();
  assert.deepEqual(await oom.runner.run({ code: 'x' }, { priority: 'owner' }),
    { result: { exit_code: 137, timed_out: false, stdout: '', stdout_truncated: false, stderr_tail: 'Killed', files: [], skipped: 0 } });
  const failed = rig({ run: { frame: { exit_code: 1, stderr_tail: 'ZeroDivisionError: division by zero\n' } } });
  await failed.runner.start();
  const answer = await failed.runner.run({ code: '1/0' }, { priority: 'owner' });
  assert.equal(answer.result.exit_code, 1);
  assert.match(answer.result.stderr_tail, /ZeroDivisionError/);
  const timedOut = rig({ run: { frame: { exit_code: -9, timed_out: true } } });
  await timedOut.runner.start();
  assert.equal((await timedOut.runner.run({ code: 'x' }, { priority: 'owner' })).result.timed_out, true);
});

test('logs: an elapsed time, an exit code, a refusal, a count and a size - never the code, stdout, stderr or a file name', async () => {
  const r = rig({ run: { frame: { exit_code: 3, stdout: 'SECRET-STDOUT', stderr_tail: 'SECRET-STDERR', files: [{ name: 'SECRET-FILE.png', hex: '89504e470d0a1a0a' }] } } });
  await r.runner.start();
  await r.runner.run({ code: SECRET_CODE }, { priority: 'group' });
  r.setPlan({ run: { code: 126 } });
  await r.runner.run({ code: SECRET_CODE }, { priority: 'owner' });
  const jobs = r.logs.filter(line => line.startsWith('calc_job '));
  assert.equal(jobs.length, 2);
  assert.deepEqual(Object.keys(JSON.parse(jobs[0].slice(9))).sort(), ['bytes', 'elapsed_ms', 'exit_code', 'files', 'priority', 'timed_out']);
  assert.deepEqual(JSON.parse(jobs[0].slice(9)), { priority: 'group', elapsed_ms: 0, exit_code: 3, timed_out: false, files: 1, bytes: 8 });
  assert.deepEqual(JSON.parse(jobs[1].slice(9)), { priority: 'owner', elapsed_ms: 0, refused: 'calc_unavailable', detail: 'docker_unavailable' });
  assert.ok(!r.logs.join('\n').includes('SECRET'), r.logs.join('\n'));
});

test('calcMediaOps: the refs\' files go in; outputs are stored by magic as the turn\'s - an owner keeps png, jpg, pdf, xlsx, a group png only', async () => {
  const library = createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'calc-media-')), userId: 'owner' });
  const input = await library.ingest({ channel: 'wechat', source: 'in', audience: 'owner', messageRef: 'm1', index: 0 }, async () => pdf());
  const outputs = [{ name: 'chart.png', data: png().toString('base64') }, { name: 'photo.jpg', data: jpeg().toString('base64') },
    { name: 'report.pdf', data: pdf().toString('base64') }, { name: 'table.xlsx', data: ooxml('xlsx').toString('base64') }];
  const r = rig({ run: { stdout: JSON.stringify({ v: 1, exit_code: 0, timed_out: false, stdout: '42\n', stdout_truncated: false, stderr_tail: '', files: outputs, skipped: 1 }) } });
  await r.runner.start();
  const ops = calcMediaOps({ library, calc: r.runner });
  const owner = await ops.calculate({ code: 'print(42)', refs: [input.ref] }, 'owner');
  assert.equal(owner.exit_code, 0);
  assert.equal(owner.stdout, '42\n');
  assert.deepEqual(owner.files.map(f => f.mime), ['image/png', 'image/jpeg', 'application/pdf', XLSX]);
  assert.equal(owner.files_skipped, 1, 'the wrapper\'s own skipped count is passed on');
  for (const [i, file] of owner.files.entries()) {
    const row = library.lookup(file.ref);
    assert.deepEqual([row.audience, row.source, row.name], ['owner', 'generated', `calc-${i + 1}.${['png', 'jpg', 'pdf', 'xlsx'][i]}`]);
    assert.equal(file.bytes, row.size);
  }
  // The input went in as f1.pdf.
  assert.equal(readFileSync(join(r.files.capture, 'f1.pdf')).subarray(0, 5).toString(), '%PDF-');

  const group = await ops.calculate({ code: 'x', refs: [] }, 'group:wechat:r1');
  assert.deepEqual(group.files.map(f => f.mime), ['image/png'], 'a group keeps png only');
  assert.equal(group.files_skipped, 4);
  assert.equal(library.lookup(group.files[0].ref).audience, 'group:wechat:r1');
  // The turn's audience is the queue priority (design 十四: an owner's call before the groups').
  assert.deepEqual(r.logs.filter(line => line.startsWith('calc_job ')).map(line => JSON.parse(line.slice(9)).priority), ['owner', 'group']);

  // Bad magic, a docx where an xlsx would be, svg, csv text: never stored.
  r.setPlan({ run: { frame: { files: [{ name: 'a.png', text: '<svg/>' }, { name: 'b.csv', text: 'a,b\n1,2\n' }, { name: 'c.xlsx', data: ooxml('docx').toString('base64') },
    { name: 'd.png', hex: '00112233' }] } } });
  const bad = await ops.calculate({ code: 'x', refs: [] }, 'owner');
  assert.deepEqual(bad.files, []);
  assert.equal(bad.files_skipped, 4);

  // An unknown ref, the host's own refusals passed on.
  assert.deepEqual(await ops.calculate({ code: 'x', refs: ['m_0000000000000000'] }, 'owner'), { refused: 'unknown_ref' });
  r.setPlan({ run: { code: 125 } });
  assert.deepEqual(await ops.calculate({ code: 'x', refs: [] }, 'owner'), { refused: 'calc_unavailable', detail: 'docker_unavailable' });
  // A stream past the bound: refused before anything is stored.
  const flood = rig({ run: { flood: 300000 } }, { limits: { frameBytes: 100000 } });
  await flood.runner.start();
  assert.deepEqual(await calcMediaOps({ library, calc: flood.runner }).calculate({ code: 'x', refs: [] }, 'owner'), { refused: 'output_too_large' });

  // outputType: magic only, by audience.
  assert.equal(outputType(png(), 'group:wechat:r1'), 'image/png');
  assert.equal(outputType(pdf(), 'group:wechat:r1'), undefined);
  assert.equal(outputType(Buffer.from('PK\x03\x04', 'latin1'), 'owner'), XLSX);
  assert.deepEqual(Object.values(CALC_OUTPUTS).map(t => t.ext), ['png', 'jpg', 'pdf', 'xlsx']);
  // mediaOps carries calculate only with a runner.
  assert.equal(typeof mediaOps({ request: async () => ({}), library, gatewayFetch: async () => ({}), calc: r.runner }).calculate, 'function');
  assert.equal(mediaOps({ request: async () => ({}), library, gatewayFetch: async () => ({}) }).calculate, undefined);
});
