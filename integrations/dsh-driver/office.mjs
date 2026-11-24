/** Step 24.2d (design-steward 7.8.12c "三", "七", "八", "十一"): the steward host's office
 *  converter - the OnlyOffice image oCIS already uses, run as a disposable container per job.
 *
 *  Nothing here talks to a running document server: each job is `docker run --rm` of the PINNED
 *  image id (AITELIER_OFFICE_IMAGE, `sha256:` + 64 hex - never a tag; anything else and the whole
 *  feature is off: office_unavailable no_image_configured), with no network, a read-only root,
 *  no capability, no new privileges, the host's own uid, a 256 MiB /tmp, pid, memory and file-size
 *  bounds, no docker log (`--log-driver none`: a converter's output never lands in docker's
 *  json logs). The job's directory (<state dir>/office-tmp/<random>, 0700) is mounted as
 *  `/job/in` read-only (the task file and the source COPY or the data) and `/job/out` writable;
 *  the font cache (<state dir>/office-fonts/<image id 12>/, made once per image by allfontsgen)
 *  read-only over the converter's own AllFonts.js and font_selection.bin. Every argument is a
 *  constant or the host's random name: no model or user string is ever on a command line or in
 *  the task XML - a document's content is only in /job/in/src.* or the DATA line.
 *
 *  One job at a time per host (queueWaitMs, then busy; a caller's abort while waiting: timeout);
 *  a job past wallMs (or aborted) is killed by its name (`docker kill`, awaited up to killWaitMs
 *  before the slot is let go and the job directory removed) and answered timeout; the job
 *  directory is removed when the job ends, however it ends. Outputs are read only from the
 *  names the host chose, with O_NOFOLLOW and lstat, under the size cap, and their type is
 *  checked (a PDF's magic, a docx's or xlsx's zip directory).
 *  The exit mapping (7.8.12c "八"): the host's own kill -> timeout; any other non-zero exit (137
 *  out of memory included) -> convert_failed; docker itself not starting the job (spawn error,
 *  exit 125-127) -> office_unavailable; exit 0 without a good output -> convert_failed.
 *
 *  start(): removes the containers an earlier process of THIS host left (by its own label
 *  aitelier.office.host=<id of its state dir>; every job also carries aitelier.office=1) and the
 *  office-tmp directory, then the self-check: the image is there, the font cache exists or is
 *  made (into a fresh directory renamed into place, so a cut-off run never leaves half a cache),
 *  and a fixed one-page PDF made from the template reads back (verifyPdf) with its sentence; a
 *  read-back that fails drops the font cache, so the next try makes it again. Until it passes
 *  every job answers office_unavailable; a job tries it again at most once per retryMs. Logs
 *  carry a format, a mime, a size, an elapsed time, a refusal code and the self-check state -
 *  never a title, a name, content or stderr. */
import { createHash, randomBytes } from 'node:crypto';
import { constants as fsConstants } from 'node:fs';
import { chmod, lstat, mkdir, mkdtemp, open as openFile, readFile, readdir, rename, rm, writeFile } from 'node:fs/promises';
import { spawn as spawnChild } from 'node:child_process';
import { isAbsolute, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { identify } from '../dsh-director/lib/media.mjs';
import { OFFICE_FONT, OFFICE_FORMATS, dataLine } from '../dsh-director/lib/office-blocks.mjs';

export const OFFICE_LIMITS = Object.freeze({
  wallMs: 60000, // one container's wall clock; then `docker kill` by its name
  killWaitMs: 15000, // how long that kill is waited for before the slot is let go
  queueWaitMs: 30000, // one container per host; a job that waits longer answers busy
  retryMs: 10 * 60 * 1000, // a failed self-check is tried again by a job at most this often
  stdoutBytes: 256 * 1024, stderrBytes: 4096, // stdout over the cap is convert_failed; stderr keeps its tail
  outputBytes: 25 * 1024 * 1024, // a made document (the library's outbound_bytes)
  pdfBytes: 64 * 1024 * 1024, // a converted PDF (the container's fsize)
  cpus: '2', memory: '1g', pids: 128, tmpfs: 'size=256m,mode=1777', fsize: 64 * 1024 * 1024,
  // An xlsx read (7.8.12c "七"): sheets and rows a call, columns and characters a cell, characters a call.
  readSheets: 3, readRows: 300, readCols: 30, readCellChars: 200, readChars: 8000,
});
/** Where the image keeps what a job runs (OnlyOffice Docs 10). The self-check runs only part of
 *  it: `docker image inspect` of the pinned id, allfontsgen (over coreFonts) only when this image's
 *  font cache is not there yet, and docbuilder making one PDF from the document template, which
 *  the PDF reader then reads back. It never runs x2t (a docx or pptx read; `themes` is named only
 *  in x2t's task) or the sheet and dump templates (an xlsx made or read). */
export const OFFICE_IMAGE_LAYOUT = Object.freeze({
  bin: '/var/www/onlyoffice/documentserver/server/FileConverter/bin',
  x2t: '/var/www/onlyoffice/documentserver/server/FileConverter/bin/x2t',
  docbuilder: '/var/www/onlyoffice/documentserver/server/FileConverter/bin/docbuilder',
  allfontsgen: '/var/www/onlyoffice/documentserver/server/tools/allfontsgen',
  coreFonts: '/var/www/onlyoffice/documentserver/core-fonts',
  themes: '/var/www/onlyoffice/documentserver/sdkjs/slide/themes',
});
export const OFFICE_LABEL = 'aitelier.office=1';
/** The label naming one host's jobs: its state dir's office-tmp, hashed (startup removes only these). */
export const dockerHostLabel = (kind, tmpRoot) => `aitelier.${kind}.host=${createHash('sha256').update(String(tmpRoot)).digest('hex').slice(0, 16)}`;
export const officeHostLabel = tmpRoot => dockerHostLabel('office', tmpRoot);
export const OFFICE_NAME_PREFIX = 'aitelier-office-';
const IMAGE_ID = /^sha256:[0-9a-f]{64}$/;
const TEMPLATES = Object.freeze({
  document: fileURLToPath(new URL('../dsh-director/office/document.js', import.meta.url)),
  sheet: fileURLToPath(new URL('../dsh-director/office/sheet.js', import.meta.url)),
  dump: fileURLToPath(new URL('../dsh-director/office/dump.js', import.meta.url)),
});
/** The host's own lines after a template: docbuilder takes its commands only as whole lines. */
const SAVE_LINES = Object.freeze({
  pdf: 'builder.SaveFile("pdf", "/job/out/out.pdf");\nbuilder.CloseFile();\n',
  docx: 'builder.SaveFile("docx", "/job/out/out.docx");\nbuilder.CloseFile();\n',
  xlsx: 'builder.SaveFile("xlsx", "/job/out/out.xlsx");\nbuilder.CloseFile();\n',
});
/** The source formats a read converts (7.8.12c "五"): docx and pptx to a PDF, an xlsx dumped. */
const READ_EXT = Object.freeze({
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
});
const DUMP_PREFIX = 'OFFICE_XLSX ';
/** The self-check's sentence: made into a one-page PDF and read back, character for character
 *  (letters only: a PDF reader may give punctuation in another width). */
export const SELFCHECK_TEXT = '管家文档自检一页中文';
/** Whether a read-back holds the sentence: compared NFKC-normalized, without white space or
 *  punctuation (a reader may split the line, space the characters or widen a colon). */
export const selfcheckRead = text => typeof text === 'string'
  && text.normalize('NFKC').replace(/[\s\p{P}]+/gu, '').includes(SELFCHECK_TEXT);

/** The x2t task: every path the host's own; 513 is PDF. */
const x2tTask = ext => `<?xml version="1.0" encoding="utf-8"?>
<TaskQueueDataConvert xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
<m_sFileFrom>/job/in/src.${ext}</m_sFileFrom>
<m_sFileTo>/job/out/out.pdf</m_sFileTo>
<m_nFormatTo>513</m_nFormatTo>
<m_sAllFontsPath>${OFFICE_IMAGE_LAYOUT.bin}/AllFonts.js</m_sAllFontsPath>
<m_sFontDir>/usr/share/fonts</m_sFontDir>
<m_sThemeDir>${OFFICE_IMAGE_LAYOUT.themes}</m_sThemeDir>
<m_sTempDir>/tmp</m_sTempDir>
<m_bIsNoBase64>true</m_bIsNoBase64>
</TaskQueueDataConvert>
`;

/** The docker client's environment: what it needs to reach its daemon, nothing of the host's
 *  own (its driver token among them). */
export function dockerEnv(env = process.env) {
  const out = { PATH: env.PATH ?? '/usr/bin:/bin' };
  for (const key of ['HOME', 'DOCKER_HOST', 'DOCKER_CONFIG', 'DOCKER_CONTEXT', 'XDG_RUNTIME_DIR'])
    if (typeof env[key] === 'string') out[key] = env[key];
  return out;
}

/** The one docker command runner both sandboxes share (office here, calc.mjs): `limits.killWaitMs` and
 *  `limits.stderrBytes` bound the kill wait and the stderr tail; stdout past `overflowBytes` kills the
 *  container and answers overflow (nothing kept). */
export function createDockerRun({ spawn, docker, env, limits, overflowBytes }) {
  /** One docker command: {code, signal, stdout, overflow, spawnError, killedByUs, stderrTail}. */
  function dockerRun(args, { signal, wallMs, name, keepStdout = false } = {}) {
    return new Promise(resolve => {
      let child, done = false, killedByUs = false, overflow = false, outBytes = 0, killed;
      const out = [];
      let errTail = Buffer.alloc(0);
      try { child = spawn(docker, args, { env, stdio: ['ignore', 'pipe', 'pipe'] }); }
      catch { resolve({ spawnError: true }); return; }
      const kill = () => {
        if (killedByUs) return;
        killedByUs = true;
        // The container is killed by its name (the client's own death would not stop it); the
        // answer waits for that kill (bounded), so the next job never overlaps this one.
        if (name) {
          killed = new Promise(settle => {
            const bound = setTimeout(settle, limits.killWaitMs);
            const end = () => { clearTimeout(bound); settle(); };
            try { spawn(docker, ['kill', name], { env, stdio: 'ignore' }).on('error', end).on('close', end); } catch { end(); }
          });
        }
        try { child.kill('SIGKILL'); } catch { /* gone */ }
      };
      const wall = wallMs ? setTimeout(kill, wallMs) : undefined;
      signal?.addEventListener?.('abort', kill, { once: true });
      const finish = fields => {
        if (done) return;
        done = true;
        clearTimeout(wall);
        signal?.removeEventListener?.('abort', kill);
        const answer = { ...fields, killedByUs, overflow, stdout: Buffer.concat(out), stderrTail: errTail };
        if (killed) killed.then(() => resolve(answer)); else resolve(answer);
      };
      child.on('error', () => finish({ spawnError: true }));
      child.stdout.on('data', chunk => {
        if (!keepStdout || overflow) return;
        outBytes += chunk.length;
        if (outBytes > overflowBytes) { overflow = true; out.length = 0; kill(); return; }
        out.push(chunk);
      });
      child.stderr.on('data', chunk => {
        const joined = Buffer.concat([errTail, chunk]);
        errTail = Buffer.from(joined.subarray(Math.max(0, joined.length - limits.stderrBytes)));
      });
      child.on('close', (code, sig) => finish({ code, signal: sig }));
      if (signal?.aborted) kill();
    });
  }
  return dockerRun;
}

/** @param image the pinned image id (`sha256:` + 64 hex) @param docker the docker command
 *  @param tmpRoot <state dir>/office-tmp @param fontsRoot <state dir>/office-fonts
 *  @param verifyPdf (path, signal) => the text of the PDF's first page, or undefined (the PDF
 *  reader: the self-check's read-back) @param log (line) => void. */
export function createOfficeRunner({ image, docker = 'docker', tmpRoot, fontsRoot, verifyPdf, log = () => {}, now = Date.now,
  limits: overrides, spawn = spawnChild, env = dockerEnv(), uid = process.getuid?.(), gid = process.getgid?.(),
  random = n => randomBytes(n) } = {}) {
  const limits = { ...OFFICE_LIMITS, ...(overrides ?? {}) };
  const rooted = typeof tmpRoot === 'string' && isAbsolute(tmpRoot) && typeof fontsRoot === 'string' && isAbsolute(fontsRoot);
  const pinned = typeof image === 'string' && IMAGE_ID.test(image);
  const fontsDir = pinned && rooted ? join(fontsRoot, image.slice(7, 19)) : undefined;
  const hostLabel = officeHostLabel(tmpRoot);
  const dockerRun = createDockerRun({ spawn, docker, env, limits, overflowBytes: limits.stdoutBytes });
  let tail = Promise.resolve();
  let status = { state: 'pending', checkedAt: -Infinity };

  /** The one slot: {release}, or {refused} - busy past queueWaitMs, timeout when the caller
   *  aborted while waiting (its place in the queue is given up either way). */
  async function acquire(signal) {
    const previous = tail;
    let release, timer, onAbort;
    const mine = new Promise(resolve => { release = resolve; });
    tail = previous.then(() => mine);
    const turn = await Promise.race([previous.then(() => 'turn'),
      new Promise(resolve => { timer = setTimeout(() => resolve('busy'), limits.queueWaitMs); }),
      new Promise(resolve => {
        onAbort = () => resolve('timeout');
        if (signal?.aborted) onAbort(); else signal?.addEventListener?.('abort', onAbort, { once: true });
      })]);
    clearTimeout(timer);
    signal?.removeEventListener?.('abort', onAbort);
    if (turn !== 'turn') { release(); return { refused: turn }; }
    return { release };
  }

  /** The bytes of `<dir>/out/<file>` - a name the host chose - or undefined: opened with
   *  O_NOFOLLOW, a regular file (lstat, and the same inode once open), at most `cap`. */
  async function output(dir, file, cap) {
    const path = join(dir, 'out', file);
    let handle;
    try {
      const before = await lstat(path);
      if (!before.isFile() || before.size > cap) return undefined;
      handle = await openFile(path, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_NONBLOCK);
      const opened = await handle.stat();
      if (!opened.isFile() || opened.ino !== before.ino || opened.dev !== before.dev || opened.size > cap) return undefined;
      const data = await handle.readFile();
      return data.length > cap ? undefined : data;
    } catch { return undefined; } finally { await handle?.close().catch(() => {}); }
  }

  async function freshDir() {
    await mkdir(tmpRoot, { recursive: true, mode: 0o700 });
    await chmod(tmpRoot, 0o700);
    const dir = await mkdtemp(join(tmpRoot, 'j-'));
    await chmod(dir, 0o700);
    await mkdir(join(dir, 'in'), { mode: 0o700 });
    await mkdir(join(dir, 'out'), { mode: 0o700 });
    return dir;
  }

  /** The fixed `docker run` arguments of one job (the only variable parts: the host's random
   *  name, its own directories, the pinned id and the entrypoint). */
  function runArgs(name, dir, entrypoint, task, { fonts = true, outDir } = {}) {
    const B = OFFICE_IMAGE_LAYOUT.bin;
    return ['run', '--rm', '--name', name, '--label', OFFICE_LABEL, '--label', hostLabel, '--network', 'none', '--log-driver', 'none',
      '-u', `${uid}:${gid}`, '--read-only', '--tmpfs', `/tmp:rw,${limits.tmpfs}`, '--cap-drop', 'ALL',
      '--security-opt', 'no-new-privileges', '--pids-limit', String(limits.pids), '--cpus', limits.cpus,
      '--memory', limits.memory, '--memory-swap', limits.memory, '--ulimit', `fsize=${limits.fsize}`,
      '-e', `LD_LIBRARY_PATH=${B}`,
      ...(dir ? ['-v', `${join(dir, 'in')}:/job/in:ro`] : []), '-v', `${outDir ?? join(dir, 'out')}:/job/out:rw`,
      ...(fonts ? ['-v', `${join(fontsDir, 'AllFonts.js')}:${B}/AllFonts.js:ro`,
        '-v', `${join(fontsDir, 'font_selection.bin')}:${B}/font_selection.bin:ro`] : []),
      '--entrypoint', entrypoint, image, ...task];
  }
  const freshName = () => OFFICE_NAME_PREFIX + Buffer.from(random(8)).toString('hex');

  /** The exit mapping (7.8.12c "八"): undefined when the container exited 0, else a refusal. */
  function exitRefusal(run) {
    if (run.spawnError) return 'office_unavailable';
    if (run.killedByUs) return run.overflow ? 'convert_failed' : 'timeout';
    if (run.code === 125 || run.code === 126 || run.code === 127) return 'office_unavailable';
    if (run.code !== 0 || run.signal) return 'convert_failed';
    return undefined;
  }

  /** One job in a fresh directory: `inputs` {name: bytes} into in/, then the container; answers
   *  {run, dir} for the caller to read out/ before `finish()` removes it. */
  async function job(inputs, entrypoint, task, signal, { keepStdout = false } = {}) {
    const dir = await freshDir();
    for (const [file, bytes] of Object.entries(inputs)) await writeFile(join(dir, 'in', file), bytes, { mode: 0o600 });
    const name = freshName();
    const run = await dockerRun(runArgs(name, dir, entrypoint, task), { signal, wallMs: limits.wallMs, name, keepStdout });
    return { run, dir };
  }

  /** The docbuilder script of one document: the DATA line, the template, the host's save lines. */
  async function documentScript(spec) {
    const sheet = spec.format === 'xlsx';
    const data = sheet ? { format: 'xlsx', title: spec.title, font: OFFICE_FONT, sheet: spec.sheet, rows: spec.rows, widths: spec.widths }
      : { format: spec.format, title: spec.title, font: OFFICE_FONT, blocks: spec.blocks };
    const template = await readFile(sheet ? TEMPLATES.sheet : TEMPLATES.document, 'utf8');
    return `${dataLine(data)}\n${template}\n${SAVE_LINES[spec.format]}`;
  }

  /** A made file's type check: the PDF's magic, a docx's or xlsx's zip directory. */
  const madeRight = (bytes, format) => (format === 'pdf' ? bytes.subarray(0, 5).toString('latin1') === '%PDF-'
    : identify(bytes)?.mime === OFFICE_FORMATS[format].mime);

  /** Make one document (spec from the plugin: {format, title, blocks} or {format: 'xlsx', title,
   *  sheet, rows, widths}) without the slot: {bytes} or {refused}. */
  async function make(spec, signal) {
    let dir;
    try {
      const { run, dir: made } = await job({ 'build.docbuilder': await documentScript(spec) }, OFFICE_IMAGE_LAYOUT.docbuilder,
        ['/job/in/build.docbuilder'], signal);
      dir = made;
      const refused = exitRefusal(run);
      if (refused) return { refused };
      const bytes = await output(dir, `out.${spec.format}`, limits.outputBytes);
      if (!bytes || !madeRight(bytes, spec.format)) return { refused: 'convert_failed' };
      return { bytes };
    } catch { return { refused: 'convert_failed' }; } finally { if (dir) await rm(dir, { recursive: true, force: true }).catch(() => {}); }
  }

  /** The font cache of this image: there, or made now by allfontsgen into a fresh directory
   *  beside it that is renamed into place only once allfontsgen exited 0 with both files - so a
   *  run cut off half-way never leaves a cache that looks whole. */
  async function fonts() {
    const there = async dir => {
      for (const file of ['AllFonts.js', 'font_selection.bin']) {
        try { if (!(await lstat(join(dir, file))).isFile()) return false; } catch { return false; }
      }
      return true;
    };
    if (await there(fontsDir)) return true;
    await mkdir(fontsRoot, { recursive: true, mode: 0o700 });
    const fresh = await mkdtemp(join(fontsRoot, '.new-'));
    try {
      await chmod(fresh, 0o700);
      const L = OFFICE_IMAGE_LAYOUT;
      const name = freshName();
      const run = await dockerRun(runArgs(name, undefined, L.allfontsgen,
        [`--input=${L.coreFonts}`, '--allfonts=/job/out/AllFonts.js', '--selection=/job/out/font_selection.bin', '--use-system=true'],
        { fonts: false, outDir: fresh }), { wallMs: limits.wallMs, name });
      if (exitRefusal(run) || !(await there(fresh))) return false;
      await rm(fontsDir, { recursive: true, force: true });
      await rename(fresh, fontsDir);
      return there(fontsDir);
    } finally { await rm(fresh, { recursive: true, force: true }).catch(() => {}); }
  }

  async function selfcheck() {
    const started = now();
    let reason;
    try {
      if (!pinned) reason = 'no_image_configured';
      else if (!rooted) reason = 'no_state_dir';
      else {
        const inspect = await dockerRun(['image', 'inspect', '--format', '{{.Id}}', image], { wallMs: limits.wallMs, keepStdout: true });
        if (inspect.spawnError || inspect.killedByUs) reason = 'docker_unavailable';
        else if (inspect.code !== 0) reason = /no such (image|object)/i.test(inspect.stderrTail.toString('utf8')) ? 'image_missing' : 'docker_unavailable';
        else if (inspect.stdout.toString('utf8').trim() !== image) reason = 'image_missing';
        else if (!(await fonts())) reason = 'fonts_failed';
        else {
          const made = await make({ format: 'pdf', title: 'selfcheck', blocks: [{ t: 'p', runs: [{ text: SELFCHECK_TEXT }] }] });
          if (!made.bytes) reason = 'selfcheck_failed';
          else {
            const dir = await freshDir();
            try {
              await writeFile(join(dir, 'out', 'check.pdf'), made.bytes, { mode: 0o600 });
              const text = typeof verifyPdf === 'function' ? await verifyPdf(join(dir, 'out', 'check.pdf')) : undefined;
              if (!selfcheckRead(text)) reason = 'selfcheck_failed';
            } finally { await rm(dir, { recursive: true, force: true }).catch(() => {}); }
          }
        }
      }
    } catch { reason = 'selfcheck_failed'; }
    // A PDF that was not made or read right may be a damaged font cache: made again next time.
    if (reason === 'selfcheck_failed' && fontsDir) await rm(fontsDir, { recursive: true, force: true }).catch(() => {});
    status = { state: reason ? 'unavailable' : 'ready', reason, checkedAt: now() };
    if (reason) log(`office_unavailable: ${reason}`);
    else log('office_ready ' + JSON.stringify({ elapsed_ms: now() - started }));
  }

  async function available() {
    if (status.state === 'ready') return true;
    if (status.state === 'unavailable' && now() - status.checkedAt < limits.retryMs) return false;
    await selfcheck();
    return status.state === 'ready';
  }

  /** Every job: the slot, then the self-check, then `work()`; one log line. */
  async function slot(fields, work, signal) {
    const started = now();
    const done = answer => {
      log('office_job ' + JSON.stringify({ ...fields, elapsed_ms: now() - started,
        ...(answer.refused ? { refused: answer.refused } : answer.bytes ? { size: answer.bytes.length } : {}) }));
      return answer;
    };
    if (!pinned) return done({ refused: 'office_unavailable' });
    const { release, refused } = await acquire(signal);
    if (refused) return done({ refused });
    try {
      if (!(await available())) return done({ refused: 'office_unavailable' });
      return done(await work());
    } catch { return done({ refused: 'convert_failed' }); } finally { release(); }
  }

  /** make_document's host half: {bytes, mime} or {refused}. */
  function generate(spec, signal) {
    if (!spec || !Object.hasOwn(SAVE_LINES, spec.format)) return Promise.resolve({ refused: 'convert_failed' });
    return slot({ op: 'make', format: spec.format }, async () => {
      const made = await make(spec, signal);
      return made.bytes ? { bytes: made.bytes, mime: OFFICE_FORMATS[spec.format].mime } : made;
    }, signal);
  }

  /** A docx or pptx row converted to a PDF: `use(path)` runs on it inside the job's directory
   *  (removed afterwards) and its answer is this call's; or {refused}. The slot is let go before
   *  `use` runs (the PDF reader has its own). */
  async function withPdf(row, use, signal) {
    const ext = READ_EXT[row?.mime];
    if (!ext || ext === 'xlsx') return { refused: 'not_a_document' };
    let dir, refused;
    const converted = await slot({ op: 'pdf', mime: row.mime, size: row.size }, async () => {
      const source = await readFile(row.path);
      const { run, dir: made } = await job({ [`src.${ext}`]: source, 'task.xml': x2tTask(ext) }, OFFICE_IMAGE_LAYOUT.x2t, ['/job/in/task.xml'], signal);
      dir = made;
      const failed = exitRefusal(run);
      if (failed) return { refused: failed };
      const bytes = await output(dir, 'out.pdf', limits.pdfBytes);
      if (!bytes || bytes.subarray(0, 5).toString('latin1') !== '%PDF-') return { refused: 'convert_failed' };
      return {};
    }, signal);
    try {
      refused = converted.refused;
      if (refused) return { refused };
      return await use(join(dir, 'out', 'out.pdf'));
    } finally { if (dir) await rm(dir, { recursive: true, force: true }).catch(() => {}); }
  }

  /** An xlsx row's sheets as text (dump.js): {result: {sheet_count, sheets}} or {refused}. */
  function readSheet(row, { first, last, rowFirst, rows, cols, cellChars, chars }, signal) {
    if (READ_EXT[row?.mime] !== 'xlsx') return Promise.resolve({ refused: 'not_a_document' });
    return slot({ op: 'sheet', mime: row.mime, size: row.size }, async () => {
      let dir;
      try {
        const script = `${dataLine({ first, last, row_first: rowFirst, rows, cols, cell_chars: cellChars, chars })}\n`
          + `builder.OpenFile("/job/in/src.xlsx");\n${await readFile(TEMPLATES.dump, 'utf8')}\nbuilder.CloseFile();\n`;
        const { run, dir: made } = await job({ 'src.xlsx': await readFile(row.path), 'dump.docbuilder': script }, OFFICE_IMAGE_LAYOUT.docbuilder,
          ['/job/in/dump.docbuilder'], signal, { keepStdout: true });
        dir = made;
        const failed = exitRefusal(run);
        if (failed) return { refused: failed };
        const line = run.stdout.toString('utf8').split('\n').find(text => text.startsWith(DUMP_PREFIX));
        let parsed;
        try { parsed = line ? JSON.parse(line.slice(DUMP_PREFIX.length)) : undefined; } catch { parsed = undefined; }
        if (!parsed || !Number.isSafeInteger(parsed.sheet_count) || !Array.isArray(parsed.sheets)) return { refused: 'convert_failed' };
        return { result: parsed };
      } finally { if (dir) await rm(dir, { recursive: true, force: true }).catch(() => {}); }
    }, signal);
  }

  /** Startup: an earlier process's containers (by label) and job directories go, then the
   *  self-check. Nothing at all when no image is configured (the feature is off). */
  async function start() {
    if (!pinned) { status = { state: 'unavailable', reason: 'no_image_configured', checkedAt: now() }; log('office_unavailable: no_image_configured'); return; }
    if (rooted) {
      await rm(tmpRoot, { recursive: true, force: true }).catch(() => {});
      // A font cache an earlier process was making when it stopped.
      for (const entry of await readdir(fontsRoot).catch(() => []))
        if (entry.startsWith('.new-')) await rm(join(fontsRoot, entry), { recursive: true, force: true }).catch(() => {});
    }
    // Only this host's own (another host or the acceptance script may share the docker daemon).
    const left = await dockerRun(['ps', '-aq', '--filter', `label=${hostLabel}`], { wallMs: limits.wallMs, keepStdout: true });
    const ids = left.code === 0 ? left.stdout.toString('utf8').split(/\s+/).filter(id => /^[0-9a-f]{12,64}$/.test(id)) : [];
    if (ids.length) await dockerRun(['rm', '-f', ...ids], { wallMs: limits.wallMs });
    const { release } = await acquire();
    if (!release) return;
    try { await selfcheck(); } finally { release(); }
  }

  return { generate, withPdf, readSheet, start, status: () => ({ ...status }), label: hostLabel };
}

const PAGES_SPEC = /^([1-9]\d{0,5})(?:-([1-9]\d{0,5}))?$/;
const range = (text, fallback) => {
  const match = typeof text === 'string' ? PAGES_SPEC.exec(text) : null;
  if (!match) return fallback;
  const first = Number(match[1]), last = match[2] === undefined ? first : Number(match[2]);
  return last < first ? undefined : [first, last];
};

/** The host's media ops for office documents (mediaOps spreads them in): readDocument for every
 *  ref - a PDF or an image straight to the PDF `reader`, a docx or pptx to the reader once
 *  `office` converted it (its PDF never stored: removed with the job's directory), an xlsx
 *  through `office.readSheet` - and makeDocument (make, check, store as `audience`'s). */
export function officeMediaOps({ library, reader, office }) {
  async function readDocument(ref, options = {}, signal) {
    const row = library?.lookup?.(ref);
    if (!row) return { refused: 'unknown_ref' };
    const ext = READ_EXT[row.mime];
    if (!ext) return reader ? reader.readDocument(library, ref, options, signal) : { refused: 'pdf_reader_unavailable' };
    if (!office) return { refused: 'office_unavailable' };
    if (ext === 'xlsx') {
      const L = OFFICE_LIMITS;
      const sheets = range(options.sheets, [1, L.readSheets]), rows = range(options.rows, [1, L.readRows]);
      if (!sheets || !rows || sheets[1] - sheets[0] + 1 > L.readSheets || rows[1] - rows[0] + 1 > L.readRows) return { refused: 'invalid_request' };
      const chars = Number.isSafeInteger(options.chars) && options.chars > 0 ? Math.min(options.chars, L.readChars) : L.readChars;
      const read = await office.readSheet(row, { first: sheets[0], last: sheets[1], rowFirst: rows[0], rows: rows[1] - rows[0] + 1,
        cols: L.readCols, cellChars: L.readCellChars, chars }, signal);
      return read.result ? { kind: 'xlsx', sheet_count: read.result.sheet_count, sheets: read.result.sheets } : { refused: read.refused ?? 'convert_failed' };
    }
    if (!reader) return { refused: 'pdf_reader_unavailable' };
    // The converted PDF stands in for the row: same ref (page images are named by it), its own path.
    return office.withPdf(row, path => reader.readDocument({ lookup: () => ({ ...row, mime: 'application/pdf', kind: 'file', ext: 'pdf', path }) },
      ref, options, signal), signal);
  }

  async function makeDocument(spec, audience, signal) {
    const format = OFFICE_FORMATS[spec?.format];
    if (!office || !format || !Object.hasOwn(OFFICE_FORMATS, spec.format)) return { refused: 'office_unavailable' };
    const made = await office.generate(spec, signal);
    if (!made?.bytes) return { refused: made?.refused ?? 'convert_failed' };
    const stored = await library.ingest({ channel: 'generated', source: 'generated', audience, name: `${spec.title}.${format.ext}` },
      () => made.bytes, { signal });
    if (!stored?.ref) return { refused: 'store_failed' };
    if (stored.mime !== format.mime) { try { library.withdraw(stored.ref); } catch { /* cleaned in 30 days */ } return { refused: 'convert_failed' }; }
    return { ref: stored.ref, mime: stored.mime, size: stored.size };
  }

  return { readDocument, makeDocument };
}
