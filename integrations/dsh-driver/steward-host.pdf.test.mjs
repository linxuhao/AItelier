/** Step 24.2c (design-steward 7.8.12b "六", "七", "十一", "十六 宿主"): the steward host's PDF
 *  reader. Fake reader children (node or sh, written into a test directory and started through a
 *  fake `python` that drops `-I`) stand in for pdf_read.py, so each exit, signal, stream and file
 *  the host must survive is produced on purpose. The last test is a real-subprocess smoke: with
 *  AITELIER_PDF_TEST_PYTHON naming a python that has pypdfium2, the real pdf_read.py renders a
 *  PDF end to end. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createPdfReader, mediaOps, pdfReaderEnv, PDF_READ_SCRIPT, PDF_READER_LIMITS } from './steward-host.mjs';
import { aitelierHost } from './runtime.mjs';
import { measuredEntryForTest } from './dsh-entry.mjs';
import { createMediaLibrary } from '../dsh-director/lib/media.mjs';

const REF = 'm_00000000000000aa';
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3, 4, 5, 6]);
const OTHER_JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe1, 9, 9, 9, 9]);
const PNG = Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/** A test directory: a fake python (drops -I, runs .sh with sh and anything else with node), a
 *  pdf-tmp root, an input file, and a side directory the fake children report into. */
function bench() {
  const dir = mkdtempSync(join(tmpdir(), 'pdf-reader-'));
  const python = join(dir, 'python');
  writeFileSync(python, `#!/bin/sh\n[ "$1" = "-I" ] || exit 99\nshift\ncase "$1" in *.sh) exec /bin/sh "$@";; *) exec ${JSON.stringify(process.execPath)} "$@";; esac\n`);
  chmodSync(python, 0o755);
  const side = join(dir, 'side'); mkdirSync(side);
  const input = join(dir, 'input.pdf'); writeFileSync(input, '%PDF-1.4\n%fake\n');
  return { dir, python, side, input, tmpRoot: join(dir, 'state', 'pdf-tmp') };
}

/** A node reader child. Its self-check writes a valid p1.jpg; otherwise `body` runs with input,
 *  out (the host's directory), opt(name) and emit(object) in scope. */
function nodeChild(b, name, body) {
  const path = join(b.dir, name + '.cjs');
  writeFileSync(path, `const fs = require('fs'), path = require('path'), cp = require('child_process');
const argv = process.argv.slice(2);
const JPEG = Buffer.from(${JSON.stringify([...JPEG])});
const SIDE = ${JSON.stringify(b.side)};
const emit = o => process.stdout.write(JSON.stringify(o) + '\\n');
if (argv[0] === '--selfcheck') {
  fs.writeFileSync(path.join(argv[1], 'p1.jpg'), JPEG);
  emit({ page_count: 1, sandbox: 'none', selfcheck: true, pages: [{ n: 1, text: 'x', chars: 1, truncated: false, no_text_layer: false, image: true }] });
  process.exit(0);
}
const [input, out] = argv; const opt = k => argv[argv.indexOf(k) + 1];
${body}
`);
  return path;
}

/** An sh reader child: the same self-check, then `body`. */
function shChild(b, name, body) {
  const path = join(b.dir, name + '.sh');
  writeFileSync(path, `if [ "$1" = "--selfcheck" ]; then printf '\\377\\330\\377\\340' > "$2/p1.jpg"; echo '{"page_count":1,"sandbox":"none","selfcheck":true,"pages":[{"n":1,"text":"x","image":true}]}'; exit 0; fi\n${body}\n`);
  return path;
}

const page = (n, extra = {}) => ({ n, text: `page ${n}`, chars: 6, truncated: false, no_text_layer: false, image: false, ...extra });
const okBody = (pages = [page(1)]) => `emit({ page_count: 9, sandbox: 'none', pages: ${JSON.stringify(pages)} });`;

function attachmentsStore() {
  const saved = [];
  return { saved, saveImage: async image => { saved.push(image); return { attachmentId: `att-${saved.length}`, name: image.name, mediaType: image.mediaType }; } };
}
function fakeLibrary(rows) {
  return { lookup: ref => rows[ref], read: async ref => (rows[ref] ? { row: rows[ref], bytes: rows[ref].bytes } : undefined) };
}
const pdfRow = b => ({ ref: REF, kind: 'file', mime: 'application/pdf', ext: 'pdf', size: 1234, path: b.input });

/** A reader over one fake child; every spawn is counted and its arguments kept. */
function reader(b, script, options = {}) {
  const { limits, now, attachments = attachmentsStore() } = options;
  const python = 'python' in options ? options.python : b.python, tmpRoot = 'tmpRoot' in options ? options.tmpRoot : b.tmpRoot;
  const logs = [], spawns = [];
  const r = createPdfReader({ python, script, tmpRoot, attachments: () => attachments, log: line => logs.push(line), now, limits,
    spawn: (command, args, options) => { spawns.push({ command, args, options }); return spawn(command, args, options); } });
  return { r, logs, spawns, attachments, library: fakeLibrary({ [REF]: pdfRow(b) }) };
}
const read = (h, options, signal) => h.r.readDocument(h.library, REF, options, signal);
const leftovers = b => (existsSync(b.tmpRoot) ? readdirSync(b.tmpRoot) : []);
/** Gone, or a zombie waiting for its reaper: either way it runs no more. */
const notRunning = pid => { try { return readFileSync(`/proc/${pid}/stat`, 'utf8').split(') ')[1]?.[0] === 'Z'; } catch { return true; } };

test('the limits are the design\'s (7.8.12b "六"), and the script is the plugin\'s pdf_read.py', () => {
  assert.deepEqual({ ...PDF_READER_LIMITS }, { wallMs: 30000, queueWaitMs: 30000, retryMs: 600000, stdoutBytes: 262144, stderrBytes: 4096,
    imageBytes: 4194304, charsPage: 4000, charsCall: 8000, pagesWithImages: 4, pagesTextOnly: 30 });
  assert.ok(PDF_READ_SCRIPT.endsWith('/integrations/dsh-director/pdf_read.py') && existsSync(PDF_READ_SCRIPT), PDF_READ_SCRIPT);
  assert.deepEqual(pdfReaderEnv('/t/x'), { PATH: '/usr/bin:/bin', LC_ALL: 'C.UTF-8', HOME: '/t/x' });
});

test('spawn: python -I script, its own process group, stdin ignored, cwd and HOME the call\'s fresh 0700 directory, env exactly PATH/LC_ALL/HOME; the directory is gone after', async () => {
  const b = bench();
  const script = nodeChild(b, 'env', `fs.writeFileSync(path.join(SIDE, 'seen.json'), JSON.stringify({ env: process.env, cwd: process.cwd(),
  mode: fs.statSync(process.cwd()).mode & 0o777, argv }));
${okBody()}`);
  process.env.AITELIER_LEAK_PROBE = 'host-secret';
  try {
    const h = reader(b, script);
    const answer = await read(h, { pages: '2-3', images: false });
    assert.equal(answer.kind, 'pdf', JSON.stringify(answer));
    const call = h.spawns.at(-1);
    assert.equal(call.command, b.python);
    assert.deepEqual(call.args, ['-I', script, b.input, call.options.cwd, '--pages', '2-3', '--images', '0', '--chars-page', '4000', '--chars-call', '8000']);
    assert.deepEqual(call.options.env, { PATH: '/usr/bin:/bin', LC_ALL: 'C.UTF-8', HOME: call.options.cwd });
    assert.equal(call.options.detached, true);
    assert.deepEqual(call.options.stdio, ['ignore', 'pipe', 'pipe']);
    assert.ok(call.options.cwd.startsWith(b.tmpRoot + '/'), call.options.cwd);
    const seen = JSON.parse(readFileSync(join(b.side, 'seen.json'), 'utf8'));
    assert.deepEqual(Object.keys(seen.env).filter(k => k.startsWith('AITELIER_')), [], 'no AITELIER_* key reaches the child');
    assert.equal(seen.env.HOME, call.options.cwd); assert.equal(seen.env.PATH, '/usr/bin:/bin'); assert.equal(seen.env.LC_ALL, 'C.UTF-8');
    assert.equal(seen.cwd, call.options.cwd); assert.equal(seen.mode, 0o700);
    assert.ok(!existsSync(call.options.cwd), 'the call\'s directory is removed');
    assert.deepEqual(leftovers(b), []);
    assert.equal(statSync(b.tmpRoot).mode & 0o777, 0o700);
  } finally { delete process.env.AITELIER_LEAK_PROBE; }
});

test('wall clock: the whole process group is SIGKILLed (a grandchild sleeper too), the directory is removed, the answer is timeout', async () => {
  const b = bench();
  const script = nodeChild(b, 'hang', `const sleeper = cp.spawn('/bin/sleep', ['1000'], { stdio: 'ignore' });
fs.writeFileSync(path.join(SIDE, 'sleeper'), String(sleeper.pid));
setInterval(() => {}, 1000);`);
  const h = reader(b, script, { limits: { wallMs: 1500 } });
  const started = Date.now();
  assert.deepEqual(await read(h, {}), { refused: 'timeout' });
  assert.ok(Date.now() - started >= 1400, 'answered at the wall clock');
  const sleeper = Number(readFileSync(join(b.side, 'sleeper'), 'utf8'));
  for (let i = 0; i < 40 && !notRunning(sleeper); i++) await sleep(50);
  assert.ok(notRunning(sleeper), `grandchild ${sleeper} still runs`);
  assert.deepEqual(leftovers(b), []);
  assert.ok(h.logs.some(l => l.startsWith('pdf_read ') && JSON.parse(l.slice(9)).refused === 'timeout'), h.logs.join('\n'));
});

test('exit mapping: SIGXCPU -> timeout; a SIGKILL the host did not send, SIGSEGV, SIGABRT, MemoryError -> broken; a valid {error} passes through', async () => {
  const b = bench();
  const cases = [
    ['kill -XCPU $$', { refused: 'timeout' }],
    ['kill -KILL $$', { refused: 'broken' }],
    ['kill -SEGV $$', { refused: 'broken' }],
    ['kill -ABRT $$', { refused: 'broken' }],
    ['echo "Traceback: MemoryError" >&2; exit 1', { refused: 'broken' }],
    ['echo \'{"error":"encrypted","sandbox":"none"}\'; exit 2', { refused: 'encrypted' }],
    ['echo \'{"error":"unsupported_security"}\'; exit 2', { refused: 'unsupported_security' }],
    ['echo \'{"error":"page_out_of_range","page_count":3}\'; exit 2', { refused: 'page_out_of_range', page_count: 3 }],
    ['echo \'{"error":"pwned"}\'; exit 2', { refused: 'broken' }],
    ['echo \'{"error":"encrypted"}\'; exit 0', { refused: 'broken' }],
    ['echo not json; exit 0', { refused: 'broken' }],
    ['echo \'{"page_count":1,"pages":[]}\'; exit 1', { refused: 'broken' }],
    ['echo \'{"page_count":1,"pages":[]}\'; echo \'{"page_count":1,"pages":[]}\'; exit 0', { refused: 'broken' }],
    ['exit 3', { refused: 'broken' }],
  ];
  for (const [i, [body, expected]] of cases.entries()) {
    const h = reader(b, shChild(b, `exit-${i}`, body));
    assert.deepEqual(await read(h, {}), expected, body);
  }
  assert.deepEqual(leftovers(b), []);
});

test('stderr: 1 MiB does not stall the child, and none of it reaches the log', async () => {
  const b = bench();
  const script = nodeChild(b, 'noisy', `process.stderr.write(Buffer.alloc(1024 * 1024, 'STDERR-SECRET-'));
${okBody([page(1, { text: 'TEXT-SECRET' })])}`);
  const h = reader(b, script, { limits: { wallMs: 10000 } });
  const answer = await read(h, {});
  assert.equal(answer.kind, 'pdf', JSON.stringify(answer));
  assert.equal(answer.pages[0].text, 'TEXT-SECRET');
  const all = h.logs.join('\n');
  assert.ok(!all.includes('STDERR') && !all.includes('SECRET') && !all.includes(REF), all);
});

test('the log holds only mime, size, page count, the pages chosen, elapsed time, a refusal code and the sandbox state', async () => {
  const b = bench();
  const h = reader(b, nodeChild(b, 'logged', okBody([page(1, { text: 'TEXT-SECRET' })])));
  await read(h, { pages: '1' });
  const reads = h.logs.filter(l => l.startsWith('pdf_read ')).map(l => JSON.parse(l.slice(9)));
  assert.equal(reads.length, 1, h.logs.join('\n'));
  assert.deepEqual(reads[0], { mime: 'application/pdf', size: 1234, pages: '1-1', images: true, elapsed_ms: reads[0].elapsed_ms, page_count: 9, sandbox: 'none' });
  assert.ok(h.logs.some(l => l.startsWith('pdf_reader_ready ')), 'the self-check is logged with its sandbox state');
  assert.ok(!h.logs.join('\n').includes('SECRET') && !h.logs.join('\n').includes(b.input), h.logs.join('\n'));
});

test('one queue per host: a second concurrent call waits, and answers busy after the queue wait', async () => {
  const b = bench();
  const h = reader(b, nodeChild(b, 'slow', `setTimeout(() => { ${okBody()} }, 1500);`), { limits: { queueWaitMs: 300 } });
  const started = Date.now();
  const first = read(h, {});
  await sleep(50);
  const second = await read(h, {});
  const waited = Date.now() - started;
  assert.deepEqual(second, { refused: 'busy' });
  assert.ok(waited >= 300 && waited < 1400, `busy after the queue wait, before the first finished (${waited} ms)`);
  assert.equal((await first).kind, 'pdf');
  assert.equal(h.spawns.length, 2, 'the self-check and the first call; the busy call started nothing');
  assert.equal((await read(h, {})).kind, 'pdf', 'the queue moves on after a busy answer');
});

test('stdout over 256 KiB is broken, even when it is one valid JSON line', async () => {
  const b = bench();
  for (const [name, body] of [['flood', `process.stdout.write(Buffer.alloc(300 * 1024, 'x'));`],
    ['bigjson', okBody([page(1, { text: 'y'.repeat(300 * 1024) })])]]) {
    const h = reader(b, nodeChild(b, name, body));
    assert.deepEqual(await read(h, {}), { refused: 'broken' }, name);
  }
  // Just under the cap is fine (the host trusts the size it measured, not the child's word).
  const h = reader(b, nodeChild(b, 'under', okBody([page(1, { text: 'y'.repeat(200 * 1024) })])));
  assert.equal((await read(h, {})).kind, 'pdf');
});

test('p<n>.jpg that is a symlink, a directory, oversize or not JPEG is never saved; a reported path is ignored', async () => {
  const b = bench();
  const outside = join(b.side, 'outside.jpg'); writeFileSync(outside, OTHER_JPEG);
  const script = nodeChild(b, 'files', `
fs.symlinkSync(${JSON.stringify(outside)}, path.join(out, 'p1.jpg'));
fs.mkdirSync(path.join(out, 'p2.jpg'));
fs.writeFileSync(path.join(out, 'p3.jpg'), Buffer.concat([JPEG, Buffer.alloc(5000)]));
fs.writeFileSync(path.join(out, 'p4.jpg'), Buffer.from(${JSON.stringify([...PNG])}));
${okBody([1, 2, 3, 4].map(n => page(n, { image: true, path: outside })))}`);
  const h = reader(b, script, { limits: { imageBytes: 4096 } });
  const answer = await read(h, { pages: '1-4' });
  assert.equal(answer.kind, 'pdf', JSON.stringify(answer));
  assert.deepEqual(h.attachments.saved, [], 'nothing reached saveImage');
  assert.deepEqual(answer.pages.map(p => [p.n, p.image, p.image_error]), [1, 2, 3, 4].map(n => [n, undefined, 'broken']));
  assert.ok(!JSON.stringify(answer).includes('path'), 'no path field reaches the plugin');
});

test('a valid page image is saved as <ref>#p<n>.jpg from the host\'s own file name, whatever path the child reports', async () => {
  const b = bench();
  const outside = join(b.side, 'outside.jpg'); writeFileSync(outside, OTHER_JPEG);
  const script = nodeChild(b, 'good', `fs.writeFileSync(path.join(out, 'p2.jpg'), JPEG);
${okBody([page(2, { image: true, path: outside, width: 10, height: 20 })])}`);
  const h = reader(b, script);
  const answer = await read(h, { pages: '2' });
  assert.equal(h.attachments.saved.length, 1);
  const [saved] = h.attachments.saved;
  assert.deepEqual({ ...saved, data: Buffer.from(saved.data) }, { data: JPEG, mediaType: 'image/jpeg', name: `${REF}#p2.jpg` });
  assert.deepEqual(answer.pages[0].image, { attachmentId: 'att-1', name: `${REF}#p2.jpg`, mediaType: 'image/jpeg' });
  assert.ok(!JSON.stringify(answer).includes('"path"'));
  // images:false: a reported image is not read at all.
  const text = reader(b, script);
  const plain = await read(text, { pages: '2', images: false });
  assert.deepEqual(text.attachments.saved, []); assert.equal(plain.pages[0].image, undefined);
  // An attachment store that refuses the image: the page says so, nothing is thrown.
  const refusing = reader(b, script, { attachments: { saveImage: async () => { throw new Error('IMAGE_TOO_MANY_PIXELS'); } } });
  assert.equal((await read(refusing, { pages: '2' })).pages[0].image_error, 'not_saved');
});

test('the host cuts text again to the per-page and per-call caps; pages outside the asked range or repeated are dropped', async () => {
  const b = bench();
  const long = 'z'.repeat(5000);
  const script = nodeChild(b, 'long', okBody([page(3, { text: long, chars: 5000 }), page(1, { text: long, chars: 5000 }),
    page(2, { text: long, chars: 5000 }), page(1, { text: 'again' }), page(9, { text: 'outside' })]));
  const h = reader(b, script);
  const answer = await read(h, { pages: '1-3', images: false });
  assert.deepEqual(answer.pages.map(p => [p.n, p.text.length, p.chars, p.truncated]), [[1, 4000, 5000, true], [2, 4000, 5000, true], [3, 0, 5000, true]]);
  assert.equal(answer.page_count, 9);
});

test('ref, kind and pages are checked before any child: unknown_ref, not_a_document, invalid_request, too_many_pages', async () => {
  const b = bench();
  const h = reader(b, nodeChild(b, 'never', okBody()));
  h.library = fakeLibrary({ [REF]: pdfRow(b), m_00000000000000bb: { ref: 'm_00000000000000bb', kind: 'voice', mime: 'audio/ogg', ext: 'ogg', size: 9, path: b.input },
    m_00000000000000cc: { ref: 'm_00000000000000cc', kind: 'file', mime: 'application/zip', ext: 'zip', size: 9, path: b.input } });
  assert.deepEqual(await h.r.readDocument(h.library, 'm_0000000000000099', {}), { refused: 'unknown_ref' });
  assert.deepEqual(await h.r.readDocument(h.library, 'm_00000000000000bb', {}), { refused: 'not_a_document' });
  assert.deepEqual(await h.r.readDocument(h.library, 'm_00000000000000cc', {}), { refused: 'not_a_document' });
  for (const pages of ['0', '3-2', 'a', '1-2-3', '', ' 1', 2]) assert.deepEqual(await read(h, { pages }), { refused: 'invalid_request' }, String(pages));
  assert.deepEqual(await read(h, { pages: '1-5' }), { refused: 'too_many_pages' });
  assert.deepEqual(await read(h, { pages: '1-3', maxPages: 2 }), { refused: 'too_many_pages' });
  assert.deepEqual(await read(h, { pages: '1-31', images: false }), { refused: 'too_many_pages' });
  assert.equal(h.spawns.length, 0, 'no child for a refused request');
  const defaults = reader(b, nodeChild(b, 'argv', `fs.writeFileSync(path.join(SIDE, 'pages-' + opt('--images')), opt('--pages'));\n${okBody()}`));
  await read(defaults, {}); await read(defaults, { images: false }); await read(defaults, { images: false, pages: '1-30' });
  assert.equal(readFileSync(join(b.side, 'pages-1'), 'utf8'), '1-4');
  assert.equal(readFileSync(join(b.side, 'pages-0'), 'utf8'), '1-30');
  await read(defaults, { maxPages: 2 });
  assert.equal(readFileSync(join(b.side, 'pages-1'), 'utf8'), '1-2');
});

test('an image ref: the library bytes are saved as one image named <ref>.<ext>, with no child and no python', async () => {
  const b = bench();
  const h = reader(b, nodeChild(b, 'never', okBody()), { python: undefined });
  const ref = 'm_00000000000000dd';
  h.library = fakeLibrary({ [ref]: { ref, kind: 'image', mime: 'image/png', ext: 'png', size: PNG.length, path: '/nowhere', bytes: PNG } });
  const answer = await h.r.readDocument(h.library, ref, {});
  assert.equal(answer.kind, 'image'); assert.equal(answer.page_count, 1);
  assert.deepEqual(answer.pages[0].image, { attachmentId: 'att-1', name: `${ref}.png`, mediaType: 'image/png' });
  assert.deepEqual(Buffer.from(h.attachments.saved[0].data), PNG);
  assert.equal(h.spawns.length, 0);
  const refusing = reader(b, nodeChild(b, 'never2', okBody()), { attachments: { saveImage: async () => { throw new Error('IMAGE_DIMENSION_TOO_LARGE'); } } });
  refusing.library = h.library;
  assert.deepEqual(await refusing.r.readDocument(h.library, ref, {}), { refused: 'broken' });
});

test('a failed self-check: logged as pdf_reader_unavailable: <reason>; calls answer pdf_reader_unavailable, retry at most once per 10 minutes, and read after it passes', async () => {
  const b = bench();
  let clock = 0;
  const script = join(b.dir, 'flaky.cjs');
  writeFileSync(script, 'process.exit(1);');
  const h = reader(b, script, { now: () => clock });
  await h.r.start();
  assert.ok(h.logs.includes('pdf_reader_unavailable: broken'), h.logs.join('\n'));
  assert.equal(h.spawns.length, 1);
  clock = 9 * 60 * 1000;
  assert.deepEqual(await read(h, {}), { refused: 'pdf_reader_unavailable' });
  assert.equal(h.spawns.length, 1, 'no retry within 10 minutes');
  clock = 10 * 60 * 1000;
  assert.deepEqual(await read(h, {}), { refused: 'pdf_reader_unavailable' });
  assert.equal(h.spawns.length, 2, 'retried once after 10 minutes');
  writeFileSync(script, readFileSync(nodeChild(b, 'fixed', okBody())));
  clock = 15 * 60 * 1000;
  assert.deepEqual(await read(h, {}), { refused: 'pdf_reader_unavailable' });
  assert.equal(h.spawns.length, 2, 'the clock runs from the last check');
  clock = 20 * 60 * 1000;
  assert.equal((await read(h, {})).kind, 'pdf');
  assert.equal(h.spawns.length, 4, 'the self-check, then the read');
  assert.equal(h.r.status().state, 'ready');
});

test('self-check reasons: no python, no state dir, a python that cannot start, selfcheck_failed, no page image', async () => {
  const b = bench();
  const reasons = async options => { const h = reader(b, options.script ?? nodeChild(b, 'ok', okBody()), options); await h.r.start(); return h.logs.filter(l => l.startsWith('pdf_reader_')); };
  assert.deepEqual(await reasons({ python: undefined }), ['pdf_reader_unavailable: no_python']);
  assert.deepEqual(await reasons({ tmpRoot: undefined }), ['pdf_reader_unavailable: no_state_dir']);
  assert.deepEqual(await reasons({ tmpRoot: 'relative/pdf-tmp' }), ['pdf_reader_unavailable: no_state_dir']);
  assert.deepEqual(await reasons({ python: join(b.dir, 'no-such-python') }), ['pdf_reader_unavailable: spawn_failed']);
  const failed = join(b.dir, 'sc-failed.cjs');
  writeFileSync(failed, 'process.stdout.write(JSON.stringify({ error: "selfcheck_failed", sandbox: "none", selfcheck: false }) + "\\n"); process.exit(2);');
  assert.deepEqual(await reasons({ script: failed }), ['pdf_reader_unavailable: selfcheck_failed']);
  const noImage = join(b.dir, 'sc-noimage.cjs');
  writeFileSync(noImage, 'process.stdout.write(JSON.stringify({ page_count: 1, sandbox: "none", selfcheck: true, pages: [{ n: 1, image: true }] }) + "\\n");');
  assert.deepEqual(await reasons({ script: noImage }), ['pdf_reader_unavailable: no_page_image']);
  const unavailable = reader(b, nodeChild(b, 'ok2', okBody()), { python: undefined });
  assert.deepEqual(await read(unavailable, {}), { refused: 'pdf_reader_unavailable' });
});

test('start clears pdf-tmp/ before the self-check', async () => {
  const b = bench();
  mkdirSync(join(b.tmpRoot, 'r-stale'), { recursive: true });
  writeFileSync(join(b.tmpRoot, 'r-stale', 'p1.jpg'), JPEG);
  writeFileSync(join(b.tmpRoot, 'loose'), 'x');
  const h = reader(b, nodeChild(b, 'ok', okBody()));
  await h.r.start();
  assert.equal(h.r.status().state, 'ready', h.logs.join('\n'));
  assert.deepEqual(leftovers(b), [], 'nothing an earlier process left');
  assert.equal(statSync(b.tmpRoot).mode & 0o777, 0o700);
});

test('mediaOps: readDocument goes to the reader with the library; takesImages is wakeTakesImages over the route input', async () => {
  const b = bench();
  const h = reader(b, nodeChild(b, 'ok', okBody()));
  const ops = mediaOps({ request: async () => ({}), library: h.library, gatewayFetch: async () => {}, reader: h.r,
    routeInput: async () => ({ root: ['text', 'image'], child: ['text', 'image'] }) });
  assert.equal((await ops.readDocument(REF, { pages: '1' })).kind, 'pdf');
  assert.equal(await ops.takesImages(), true);
  for (const routes of [{ root: ['text', 'image'], child: ['text'] }, { root: ['text', 'image'], child: ['text', 'image'], measured: false }]) {
    assert.equal(await mediaOps({ library: h.library, routeInput: async () => routes }).takesImages(), false, JSON.stringify(routes));
  }
  const bare = mediaOps({ request: async () => ({}), library: h.library, gatewayFetch: async () => {} });
  assert.equal(bare.readDocument, undefined); assert.equal(bare.takesImages, undefined);
});

test('runtime: the steward\'s media has readDocument (AITELIER_MEDIA_PYTHON, <state dir>/pdf-tmp) and takesImages; a director\'s has neither', async () => {
  const b = bench();
  const root = join(b.dir, 'media'), stateDir = join(b.dir, 'state');
  const env = { AITELIER_DSH_PROJECT: 'aitelier-home', AITELIER_DSH_SESSION: 's-1', AITELIER_DSH_PROVIDER: 'aitelier', AITELIER_DSH_MODEL: 'm',
    AITELIER_DSH_STATE_DIR: stateDir, AITELIER_DSH_ENTRY: '/tmp/dsh.mjs', AITELIER_DRIVER_ID: 'drv-home', AITELIER_DSH_EXECUTION: 'host',
    AITELIER_MEDIA_ROOT: root, AITELIER_MEDIA_PYTHON: b.python };
  const steward = aitelierHost({ ...env, AITELIER_DSH_ROLE: 'steward' });
  assert.equal(typeof steward.media.readDocument, 'function'); assert.equal(typeof steward.media.takesImages, 'function');
  const pdf = Buffer.from('%PDF-1.4\n%runtime\n');
  const library = createMediaLibrary({ root, userId: 'owner' });
  const { ref } = await library.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => pdf);
  // The host's own library re-reads the index; the real pdf_read.py fails on these bytes - the
  // point is that the host's python and pdf-tmp are the ones the environment named.
  const fresh = aitelierHost({ ...env, AITELIER_DSH_ROLE: 'steward' });
  const answer = await fresh.media.readDocument(ref, { pages: '1' });
  assert.ok(['broken', 'pdf_reader_unavailable'].includes(answer.refused), JSON.stringify(answer));
  assert.ok(existsSync(join(stateDir, 'pdf-tmp')), 'the reader works under <state dir>/pdf-tmp');
  assert.deepEqual(readdirSync(join(stateDir, 'pdf-tmp')), [], 'and leaves nothing there');
  assert.equal(await steward.media.takesImages(), false, 'no readable route input: no page images (fail closed)');
  const director = aitelierHost(env);
  assert.equal(director.media.readDocument, undefined); assert.equal(director.media.takesImages, undefined);
});

test('runtime install (real pinned DSH entry): the steward wipes <state dir>/pdf-tmp and runs the self-check, logged as pdf_reader_unavailable: <reason>', async (t) => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const b = bench();
  const stateDir = join(b.dir, 'state'), token = join(b.dir, 'token');
  mkdirSync(join(stateDir, 'pdf-tmp', 'r-stale'), { recursive: true });
  writeFileSync(join(stateDir, 'pdf-tmp', 'r-stale', 'p1.jpg'), JPEG);
  writeFileSync(token, 'fixture-only', { mode: 0o600 });
  const host = aitelierHost({ AITELIER_DSH_PROJECT: 'aitelier-home', AITELIER_DSH_SESSION: 's-1', AITELIER_DSH_PROVIDER: 'aitelier',
    AITELIER_DSH_MODEL: 'm', AITELIER_DSH_STATE_DIR: stateDir, AITELIER_DSH_ENTRY: entry, AITELIER_DRIVER_ID: 'drv-home',
    AITELIER_DSH_EXECUTION: 'host', AITELIER_DRIVER_TOKEN_FILE: token, AITELIER_DSH_ROLE: 'steward',
    AITELIER_MEDIA_ROOT: join(b.dir, 'media'), AITELIER_MEDIA_PYTHON: join(b.dir, 'no-such-python') });
  const logs = [];
  const ctx = { logger: { info: line => logs.push(line), warn: line => logs.push(line), error: line => logs.push(line) }, get: () => undefined };
  // Only the start of install() matters here; a fake ctx makes the rest of it throw.
  await host.install({ ctx, defineTool: () => {}, llm: {}, roles: {} }).catch(() => {});
  for (let i = 0; i < 100 && !logs.some(l => l.startsWith('pdf_reader_')); i++) await sleep(50);
  assert.ok(logs.includes('pdf_reader_unavailable: spawn_failed'), logs.join('\n'));
  assert.deepEqual(readdirSync(join(stateDir, 'pdf-tmp')), [], 'what an earlier process left is gone');
});

/** A small PDF with one line of Helvetica text per page, from bytes (the selfcheck's shape). */
function smokePdf(lines) {
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>',
    `<< /Type /Pages /Kids [${lines.map((_, i) => `${3 + 2 * i} 0 R`).join(' ')}] /Count ${lines.length} >>`];
  const font = 3 + 2 * lines.length;
  lines.forEach((line, i) => {
    const content = `BT /F1 24 Tf 72 720 Td (${line}) Tj ET\n0 0 1 rg 72 600 200 60 re f\n`;
    objects.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 ${font} 0 R >> >> /Contents ${4 + 2 * i} 0 R >>`);
    objects.push(`<< /Length ${content.length} >>\nstream\n${content}endstream`);
  });
  objects.push('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>');
  let out = '%PDF-1.4\n';
  const offsets = objects.map((object, i) => { const at = out.length; out += `${i + 1} 0 obj\n${object}\nendobj\n`; return at; });
  const xref = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n` + offsets.map(o => `${String(o).padStart(10, '0')} 00000 n \n`).join('');
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, 'latin1');
}

test('real subprocess: the real pdf_read.py under AITELIER_PDF_TEST_PYTHON passes its self-check and renders a PDF end to end', async (t) => {
  const python = process.env.AITELIER_PDF_TEST_PYTHON;
  if (!python) {
    t.skip('AITELIER_PDF_TEST_PYTHON is not set: no python with pypdfium2 here (aitelier:latest has none until rebuilt); run this file under ctest-st.sh with the /tmp/pv venv');
    return;
  }
  const b = bench();
  writeFileSync(b.input, smokePdf(['smoke page one 4721', 'smoke page two 4722', 'smoke page three 4723']));
  const logs = [], attachments = attachmentsStore();
  const r = createPdfReader({ python, tmpRoot: b.tmpRoot, attachments: () => attachments, log: line => logs.push(line) });
  await r.start();
  assert.equal(r.status().state, 'ready', logs.join('\n'));
  const answer = await r.readDocument(fakeLibrary({ [REF]: pdfRow(b) }), REF, { pages: '2-3' });
  assert.equal(answer.kind, 'pdf', JSON.stringify(answer) + '\n' + logs.join('\n'));
  assert.equal(answer.page_count, 3);
  assert.deepEqual(answer.pages.map(p => [p.n, p.text.trim(), p.no_text_layer, p.image?.name]),
    [[2, 'smoke page two 4722', false, `${REF}#p2.jpg`], [3, 'smoke page three 4723', false, `${REF}#p3.jpg`]]);
  assert.equal(attachments.saved.length, 2);
  for (const saved of attachments.saved) {
    assert.equal(saved.mediaType, 'image/jpeg');
    assert.deepEqual([...saved.data.subarray(0, 3)], [0xff, 0xd8, 0xff]);
  }
  assert.ok(['landlock', 'landlock_fs', 'none'].includes(answer.sandbox), answer.sandbox);
  assert.deepEqual(await r.readDocument(fakeLibrary({ [REF]: pdfRow(b) }), REF, { pages: '4' }), { refused: 'page_out_of_range', page_count: 3 });
  assert.deepEqual(leftovers(b), []);
  console.log(`# pdf reader smoke: sandbox=${answer.sandbox}; ${logs.join(' | ')}`);
});
