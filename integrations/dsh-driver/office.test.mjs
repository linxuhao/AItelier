/** Step 24.2d (design-steward 7.8.12c "三", "七", "八", "十六" 宿主): the steward host's office
 *  converter (office.mjs) against a FAKE docker (test-fixtures/fake-docker.mjs: no daemon, no
 *  image, no OnlyOffice; it records every argv and answers from a plan) - the fixed container
 *  arguments and nothing of the user's on a command line, the exit mapping, the output checks,
 *  the one slot, the wall clock and its kill, startup cleanup, the self-check and its retry,
 *  the logs; and officeMediaOps (make and store, the read routes) over a real media library. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createOfficeRunner, officeMediaOps, OFFICE_IMAGE_LAYOUT, OFFICE_LIMITS, SELFCHECK_TEXT } from './office.mjs';
import { createMediaLibrary } from '../dsh-director/lib/media.mjs';
import { ooxml } from '../dsh-director/test/fixtures/zip-samples.mjs';

const IMAGE = 'sha256:' + 'ab'.repeat(32);
const FAKE = fileURLToPath(new URL('./test-fixtures/fake-docker.mjs', import.meta.url));
const OK_INSPECT = { code: 0, stdout: IMAGE + '\n' };
const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const LETTER = { format: 'pdf', title: '给物业的信-标题', blocks: [{ t: 'p', runs: [{ text: '楼道灯坏了-正文秘密' }] }] };

function rig(plan = {}, options = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'office-'));
  const docker = join(dir, 'docker');
  copyFileSync(FAKE, docker);
  chmodSync(docker, 0o755);
  const files = { log: join(dir, 'calls.jsonl'), plan: join(dir, 'plan.json'), capture: join(dir, 'capture') };
  writeFileSync(files.log, '');
  const setPlan = next => writeFileSync(files.plan, JSON.stringify({ inspect: OK_INSPECT, run: { write: 'pdf' }, ...next }));
  setPlan(plan);
  const logs = [];
  let clock = 1_000_000;
  const samples = join(dir, 'samples');
  mkdirSync(samples);
  for (const ext of ['docx', 'xlsx']) writeFileSync(join(samples, `s.${ext}`), ooxml(ext));
  const runner = createOfficeRunner({ image: IMAGE, docker, tmpRoot: join(dir, 'state', 'office-tmp'), fontsRoot: join(dir, 'state', 'office-fonts'),
    verifyPdf: async path => readFileSync(path, 'utf8'), log: line => logs.push(line), now: () => clock, uid: 1234, gid: 5678,
    env: { PATH: process.env.PATH, FAKE_DOCKER_LOG: files.log, FAKE_DOCKER_PLAN: files.plan, FAKE_DOCKER_CAPTURE: files.capture },
    ...options, limits: { ...(options.limits ?? {}) } });
  const calls = () => readFileSync(files.log, 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line));
  const runs = () => calls().filter(args => args[0] === 'run');
  const jobsLeft = () => (existsSync(join(dir, 'state', 'office-tmp')) ? readdirSync(join(dir, 'state', 'office-tmp')) : []);
  return { dir, docker, runner, logs, calls, runs, setPlan, files, samples, jobsLeft, tick: ms => { clock += ms; } };
}

test('no image configured: the feature is off - every job office_unavailable, docker never called', async () => {
  for (const image of [undefined, '', 'onlyoffice/documentserver:latest', 'sha256:' + 'a'.repeat(63), 'sha256:' + 'A'.repeat(64)]) {
    const r = rig({}, { image });
    await r.runner.start();
    assert.deepEqual(await r.runner.generate(LETTER), { refused: 'office_unavailable' }, String(image));
    assert.deepEqual(r.calls(), [], String(image));
    assert.deepEqual(r.runner.status().reason, 'no_image_configured');
  }
});

test('start: an earlier process\'s labelled containers and job directories go, then the self-check makes fonts and reads a PDF back', async () => {
  const r = rig({ ps: 'abc123def456\n0123456789ab\n--evil\n' });
  mkdirSync(join(r.dir, 'state', 'office-tmp', 'j-old'), { recursive: true });
  writeFileSync(join(r.dir, 'state', 'office-tmp', 'j-old', 'x'), 'left');
  await r.runner.start();
  const calls = r.calls();
  assert.deepEqual(calls[0], ['ps', '-aq', '--filter', 'label=aitelier.office=1']);
  assert.deepEqual(calls[1], ['rm', '-f', 'abc123def456', '0123456789ab']);
  assert.deepEqual(calls[2], ['image', 'inspect', '--format', '{{.Id}}', IMAGE]);
  assert.equal(r.jobsLeft().length, 0, 'the old job directory went');
  assert.equal(r.runner.status().state, 'ready');
  assert.ok(r.logs.some(line => line.startsWith('office_ready ')));
  const [fonts, check] = r.runs();
  assert.equal(fonts[fonts.indexOf('--entrypoint') + 1], OFFICE_IMAGE_LAYOUT.allfontsgen);
  const fontsDir = join(r.dir, 'state', 'office-fonts', 'abababababab');
  assert.ok(fonts.includes(`${fontsDir}:/job/out:rw`));
  assert.ok(existsSync(join(fontsDir, 'AllFonts.js')) && existsSync(join(fontsDir, 'font_selection.bin')));
  assert.equal(check[check.indexOf('--entrypoint') + 1], OFFICE_IMAGE_LAYOUT.docbuilder);
  assert.ok(readFileSync(join(r.files.capture, 'build.docbuilder'), 'utf8').includes(SELFCHECK_TEXT));
  // A second process with the same font cache does not make the fonts again.
  const again = rig({}, { fontsRoot: join(r.dir, 'state', 'office-fonts') });
  await again.runner.start();
  assert.equal(again.runs().filter(args => args.includes(OFFICE_IMAGE_LAYOUT.allfontsgen)).length, 0);
  assert.equal(again.runner.status().state, 'ready');
});

test('the self-check\'s reasons, and a failed one is tried again by a job at most once per retryMs', async () => {
  const cases = [
    [{ inspect: { code: 1, stderr: 'Error response from daemon: No such image: sha256:ab' } }, 'image_missing'],
    [{ inspect: { code: 1, stderr: 'Cannot connect to the Docker daemon' } }, 'docker_unavailable'],
    [{ inspect: { code: 0, stdout: 'sha256:' + 'cd'.repeat(32) } }, 'image_missing'],
    [{ fonts: { code: 1 } }, 'fonts_failed'],
    [{ run: { write: 'junk' } }, 'selfcheck_failed'],
    [{ run: { write: 'none' } }, 'selfcheck_failed'],
  ];
  for (const [plan, reason] of cases) {
    const r = rig(plan);
    await r.runner.start();
    assert.equal(r.runner.status().reason, reason, JSON.stringify(plan));
    assert.ok(r.logs.includes(`office_unavailable: ${reason}`));
  }
  const missing = rig({}, { docker: '/nonexistent/docker' });
  await missing.runner.start();
  assert.equal(missing.runner.status().reason, 'docker_unavailable');
  // The read-back must hold the sentence.
  const wrong = rig({}, { verifyPdf: async () => 'something else' });
  await wrong.runner.start();
  assert.equal(wrong.runner.status().reason, 'selfcheck_failed');
  // Retry: within retryMs a job answers office_unavailable without asking docker again.
  const r = rig({ inspect: { code: 1, stderr: 'No such image' } });
  await r.runner.start();
  const before = r.calls().length;
  assert.deepEqual(await r.runner.generate(LETTER), { refused: 'office_unavailable' });
  assert.equal(r.calls().length, before);
  r.setPlan({});
  r.tick(OFFICE_LIMITS.retryMs - 1);
  assert.deepEqual(await r.runner.generate(LETTER), { refused: 'office_unavailable' });
  r.tick(2);
  const made = await r.runner.generate(LETTER);
  assert.ok(made.bytes, JSON.stringify(made));
  assert.equal(r.runner.status().state, 'ready');
});

test('a job\'s container: fixed hardening, the pinned id, read-only in/ and fonts, and no user string on any command line', async () => {
  const r = rig();
  await r.runner.start();
  const made = await r.runner.generate(LETTER);
  assert.equal(made.mime, 'application/pdf');
  assert.equal(made.bytes.subarray(0, 5).toString(), '%PDF-');
  const args = r.runs().at(-1);
  const pairs = (flag, value) => args.some((a, i) => a === flag && args[i + 1] === value);
  for (const [flag, value] of [['--network', 'none'], ['--log-driver', 'none'], ['-u', '1234:5678'], ['--cap-drop', 'ALL'],
    ['--security-opt', 'no-new-privileges'], ['--pids-limit', '128'], ['--cpus', '2'], ['--memory', '1g'], ['--memory-swap', '1g'],
    ['--ulimit', 'fsize=67108864'], ['--tmpfs', '/tmp:rw,size=256m,mode=1777'], ['--label', 'aitelier.office=1'],
    ['--entrypoint', OFFICE_IMAGE_LAYOUT.docbuilder]])
    assert.ok(pairs(flag, value), `${flag} ${value}`);
  assert.ok(args.includes('--rm') && args.includes('--read-only'));
  assert.match(args[args.indexOf('--name') + 1], /^aitelier-office-[0-9a-f]{16}$/);
  assert.equal(args.at(-2), IMAGE);
  assert.equal(args.at(-1), '/job/in/build.docbuilder');
  const mounts = args.filter((a, i) => args[i - 1] === '-v');
  assert.equal(mounts.length, 4);
  assert.match(mounts[0], /\/office-tmp\/j-[^/]+\/in:\/job\/in:ro$/);
  assert.match(mounts[1], /\/office-tmp\/j-[^/]+\/out:\/job\/out:rw$/);
  assert.ok(mounts[2].endsWith(`/office-fonts/abababababab/AllFonts.js:${OFFICE_IMAGE_LAYOUT.bin}/AllFonts.js:ro`));
  assert.ok(mounts[3].endsWith(`/office-fonts/abababababab/font_selection.bin:${OFFICE_IMAGE_LAYOUT.bin}/font_selection.bin:ro`));
  const everything = JSON.stringify(r.calls());
  for (const secret of ['给物业的信', '标题', '楼道灯', '正文秘密']) assert.ok(!everything.includes(secret), secret);
  // The script: the DATA line, the template, then the host's own save lines.
  const script = readFileSync(join(r.files.capture, 'build.docbuilder'), 'utf8').split('\n');
  assert.match(script[0], /^var DATA = \{"format":"pdf","title":"给物业的信-标题","font":"WenQuanYi Zen Hei","blocks":/);
  assert.ok(script.includes('builder.CreateFile("docx");'));
  assert.deepEqual(script.slice(-3), ['builder.SaveFile("pdf", "/job/out/out.pdf");', 'builder.CloseFile();', '']);
  assert.deepEqual(r.jobsLeft(), [], 'the job directory is removed');
  // A docx and an xlsx are checked by their zip directory: the right one passes, another type is convert_failed.
  r.setPlan({ run: { write: 'copy', from: join(r.samples, 's.docx') } });
  assert.equal((await r.runner.generate({ ...LETTER, format: 'docx' })).mime, DOCX);
  assert.deepEqual(await r.runner.generate({ format: 'xlsx', title: 't', sheet: 'S', rows: [[{ s: 'a' }]], widths: [6] }), { refused: 'convert_failed' });
  r.setPlan({ run: { write: 'copy', from: join(r.samples, 's.xlsx') } });
  assert.equal((await r.runner.generate({ format: 'xlsx', title: 't', sheet: 'S', rows: [[{ s: 'a' }]], widths: [6] })).mime, XLSX);
  assert.ok(readFileSync(join(r.files.capture, 'build.docbuilder'), 'utf8').includes('builder.CreateFile("xlsx");'));
  // An unknown format never reaches docker.
  const count = r.calls().length;
  assert.deepEqual(await r.runner.generate({ ...LETTER, format: 'pptx' }), { refused: 'convert_failed' });
  assert.equal(r.calls().length, count);
});

test('the exit mapping and the output checks; the job directory goes whatever happened', async () => {
  const r = rig({}, { limits: { outputBytes: 1024 * 1024 } });
  await r.runner.start();
  for (const [run, refused] of [
    [{ code: 137, write: 'pdf' }, 'convert_failed'], [{ code: 1, write: 'pdf' }, 'convert_failed'], [{ code: 80 }, 'convert_failed'],
    [{ code: 125 }, 'office_unavailable'], [{ code: 127 }, 'office_unavailable'],
    [{ write: 'none' }, 'convert_failed'], [{ write: 'symlink' }, 'convert_failed'], [{ write: 'big' }, 'convert_failed'], [{ write: 'junk' }, 'convert_failed'],
  ]) {
    r.setPlan({ run });
    assert.deepEqual(await r.runner.generate(LETTER), { refused }, JSON.stringify(run));
    assert.deepEqual(r.jobsLeft(), [], JSON.stringify(run));
  }
  // Something else the container left in out/ is ignored and removed with it.
  r.setPlan({ run: { write: 'pdf', extra: true } });
  assert.ok((await r.runner.generate(LETTER)).bytes);
  assert.deepEqual(r.jobsLeft(), []);
});

test('the wall clock: the host kills the container by its name and answers timeout; one slot, a waiter past queueWaitMs is busy', async () => {
  const r = rig({}, { limits: { wallMs: 400, queueWaitMs: 150 } });
  await r.runner.start();
  r.setPlan({ run: { sleepMs: 5000, write: 'pdf' } });
  const started = Date.now();
  const [slow, waiting] = await Promise.all([r.runner.generate(LETTER), new Promise(resolve => setTimeout(resolve, 50)).then(() => r.runner.generate(LETTER))]);
  assert.deepEqual(slow, { refused: 'timeout' });
  assert.deepEqual(waiting, { refused: 'busy' });
  assert.ok(Date.now() - started < 4000, 'the call did not wait for the container');
  const name = r.runs().at(-1)[r.runs().at(-1).indexOf('--name') + 1];
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.ok(r.calls().some(args => args[0] === 'kill' && args[1] === name), 'docker kill <name>');
  assert.deepEqual(r.jobsLeft(), []);
  // An abort from the caller is the same kill.
  r.setPlan({ run: { sleepMs: 5000, write: 'pdf' } });
  const controller = new AbortController();
  setTimeout(() => controller.abort(), 100);
  assert.deepEqual(await r.runner.generate(LETTER, controller.signal), { refused: 'timeout' });
});

test('reading: a docx goes through x2t to a PDF the reader reads in the job directory; an xlsx is dumped to text', async () => {
  const r = rig({}, { limits: { stdoutBytes: 4096 } });
  await r.runner.start();
  const docx = { mime: DOCX, size: 10, path: join(r.samples, 's.docx') };
  let seen;
  const answer = await r.runner.withPdf(docx, async path => { seen = readFileSync(path); return { kind: 'pdf', page_count: 1, pages: [] }; });
  assert.deepEqual(answer, { kind: 'pdf', page_count: 1, pages: [] });
  assert.equal(seen.subarray(0, 5).toString(), '%PDF-');
  const args = r.runs().at(-1);
  assert.equal(args[args.indexOf('--entrypoint') + 1], OFFICE_IMAGE_LAYOUT.x2t);
  assert.equal(args.at(-1), '/job/in/task.xml');
  const task = readFileSync(join(r.files.capture, 'task.xml'), 'utf8');
  assert.match(task, /<m_sFileFrom>\/job\/in\/src\.docx<\/m_sFileFrom>/);
  assert.match(task, /<m_nFormatTo>513<\/m_nFormatTo>/);
  assert.deepEqual(readFileSync(join(r.files.capture, 'src.docx')), ooxml('docx'), 'a copy of the source, not a mount of the library');
  assert.deepEqual(r.jobsLeft(), []);
  r.setPlan({ run: { code: 137 } });
  assert.deepEqual(await r.runner.withPdf(docx, async () => assert.fail('never read')), { refused: 'convert_failed' });
  assert.deepEqual(await r.runner.withPdf({ ...docx, mime: 'application/pdf' }, async () => assert.fail('never')), { refused: 'not_a_document' });

  const xlsx = { mime: XLSX, size: 10, path: join(r.samples, 's.xlsx') };
  const dump = { sheet_count: 1, sheets: [{ n: 1, name: 'S', rows_total: 1, cols_total: 1, first_row: 1, rows: [['a']] }] };
  r.setPlan({ run: { stdout: `some noise\nOFFICE_XLSX ${JSON.stringify(dump)}\n` } });
  const read = await r.runner.readSheet(xlsx, { first: 1, last: 3, rowFirst: 1, rows: 300, cols: 30, cellChars: 200, chars: 8000 });
  assert.deepEqual(read, { result: dump });
  const script = readFileSync(join(r.files.capture, 'dump.docbuilder'), 'utf8').split('\n');
  assert.equal(script[0], 'var DATA = {"first":1,"last":3,"row_first":1,"rows":300,"cols":30,"cell_chars":200,"chars":8000};');
  assert.equal(script[1], 'builder.OpenFile("/job/in/src.xlsx");');
  for (const [plan, refused] of [[{ stdout: 'no line' }, 'convert_failed'], [{ stdout: 'OFFICE_XLSX {broken' }, 'convert_failed'],
    [{ stdout: 'x'.repeat(8192) }, 'convert_failed'], [{ code: 1, stdout: `OFFICE_XLSX ${JSON.stringify(dump)}\n` }, 'convert_failed']]) {
    r.setPlan({ run: plan });
    assert.deepEqual(await r.runner.readSheet(xlsx, { first: 1, last: 1, rowFirst: 1, rows: 1, cols: 1, cellChars: 1, chars: 1 }), { refused }, JSON.stringify(plan).slice(0, 60));
  }
  assert.deepEqual(r.jobsLeft(), []);
});

test('logs: an op, a mime or format, a size, a time and a code - never a title, content, a path or stderr', async () => {
  const r = rig({ run: { write: 'pdf', stderr: 'STDERR-SECRET' } });
  await r.runner.start();
  await r.runner.generate(LETTER);
  r.setPlan({ run: { code: 1, stderr: 'STDERR-SECRET' } });
  await r.runner.generate(LETTER);
  await r.runner.withPdf({ mime: DOCX, size: 10, path: join(r.samples, 's.docx') }, async () => ({}));
  const text = r.logs.join('\n');
  for (const secret of ['给物业', '楼道灯', 'STDERR-SECRET', r.dir, 's.docx']) assert.ok(!text.includes(secret), secret);
  assert.ok(r.logs.some(line => line.startsWith('office_job ') && JSON.parse(line.slice(11)).refused === 'convert_failed'));
  assert.deepEqual(Object.keys(JSON.parse(r.logs.find(line => line.startsWith('office_job ')).slice(11))).sort(), ['elapsed_ms', 'format', 'op', 'size']);
});

test('officeMediaOps: a made file is stored as the turn\'s with its title as its name; a file of the wrong type is taken back', async () => {
  const library = createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'office-lib-')), userId: 'owner' });
  const pdf = Buffer.concat([Buffer.from('%PDF-1.7\n'), Buffer.alloc(50)]);
  let next = pdf;
  const office = { generate: async () => ({ bytes: next }), withPdf: async (row, use) => use('/converted.pdf'), readSheet: async () => ({ result: { sheet_count: 2, sheets: [] } }) };
  const reads = [];
  const reader = { readDocument: async (lib, ref, options) => { reads.push([lib.lookup(ref), ref, options]); return { kind: 'pdf', page_count: 1, pages: [] }; } };
  const ops = officeMediaOps({ library, reader, office });
  const made = await ops.makeDocument({ format: 'pdf', title: '信' }, 'group:wechat:r1');
  const row = library.lookup(made.ref);
  assert.deepEqual([row.audience, row.source, row.channel, row.name, row.mime], ['group:wechat:r1', 'generated', 'generated', '信.pdf', 'application/pdf']);
  next = ooxml('xlsx');
  assert.deepEqual(await ops.makeDocument({ format: 'docx', title: '信' }, 'owner'), { refused: 'convert_failed' });
  assert.equal(library.rows.length, 1, 'the wrong file is withdrawn');
  assert.deepEqual(await ops.makeDocument({ format: 'pptx', title: '信' }, 'owner'), { refused: 'office_unavailable' });
  office.generate = async () => ({ refused: 'busy' });
  assert.deepEqual(await ops.makeDocument({ format: 'pdf', title: '信' }, 'owner'), { refused: 'busy' });
  // Reads: a PDF straight to the reader, a docx as its converted PDF under the same ref, an xlsx dumped.
  const put = async bytes => (await library.ingest({ channel: 'wechat', messageRef: String(Math.random()), index: 0, audience: 'owner', source: 'in' }, async () => bytes)).ref;
  const pdfRef = await put(Buffer.concat([pdf, Buffer.from('x')])), docxRef = await put(ooxml('docx')), xlsxRef = await put(ooxml('xlsx'));
  await ops.readDocument(pdfRef, { pages: '1' });
  assert.equal(reads.at(-1)[0].path, library.lookup(pdfRef).path);
  await ops.readDocument(docxRef, { pages: '1' });
  assert.deepEqual([reads.at(-1)[0].path, reads.at(-1)[0].mime, reads.at(-1)[1]], ['/converted.pdf', 'application/pdf', docxRef]);
  assert.deepEqual(await ops.readDocument(xlsxRef, { sheets: '1-3', rows: '1-300', chars: 8000 }), { kind: 'xlsx', sheet_count: 2, sheets: [] });
  assert.deepEqual(await ops.readDocument(xlsxRef, { sheets: '1-4' }), { refused: 'invalid_request' });
  assert.deepEqual(await ops.readDocument('m_' + '0'.repeat(16), {}), { refused: 'unknown_ref' });
  // No converter: an office ref is office_unavailable, a PDF still read.
  const pdfOnly = officeMediaOps({ library, reader });
  assert.deepEqual(await pdfOnly.readDocument(docxRef, {}), { refused: 'office_unavailable' });
  assert.equal((await pdfOnly.readDocument(pdfRef, {})).kind, 'pdf');
});
