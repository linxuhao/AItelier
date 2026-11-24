/** Step 24.4b (design-steward 7.8.24 "十"): the steward host's calculate sandbox - the pinned
 *  calc image (docker/calc: python 3.12 with numpy, sympy, pandas, matplotlib) run as a
 *  disposable container per call.
 *
 *  Each job is `docker run --rm` of the PINNED image id (AITELIER_CALC_IMAGE, `sha256:` + 64 hex
 *  - never a tag; anything else and the whole feature is off: calc_unavailable
 *  no_image_configured) with no network, no docker log, uid 65534 (never the host's own uid: it is
 *  in the docker group), a read-only root, two small tmpfs (/tmp and /job/out, counted in the
 *  memory bound), no capability, no new privileges, pid, cpu and memory bounds, HOME=/tmp. The
 *  ONLY host path in the container is the job's input directory (<state dir>/calc-tmp/<random>/in,
 *  0755, its files 0644), read-only at /job/in: the model's program as main.py and the input
 *  files renamed f1.<ext>, f2.<ext>, ... in the order given (their names never go in). There is
 *  no writable host mount: what the program saves in /job/out comes back only inside the one
 *  JSON frame the image's fixed wrapper (/opt/calc/run.py, the entrypoint) writes to stdout. Every
 *  argument is a constant, the host's random name or its own directory: no model string is ever on
 *  a command line.
 *
 *  The host trusts nothing of the frame (the program runs as the wrapper's uid and could forge
 *  one): its stdout is read up to frameBytes (past it: `docker kill`, output_too_large, nothing
 *  kept), parsed, and every bound applied again here - the stdout's and stderr's sizes, at most
 *  `files` files of at most fileBytes each (more, or a larger one: output_too_large). Nothing is
 *  unpacked or parsed here beyond the JSON; a file's type is its magic, checked when it is stored
 *  (calcMediaOps).
 *
 *  One job at a time per host, its own slot (the office converter has another); a call that waits
 *  longer than queueWaitMs answers busy; an owner's call is queued before every group call
 *  already waiting. A job past wallMs (or aborted) is killed by its name (`docker kill`, awaited
 *  up to killWaitMs) and answered timeout. The job directory is removed however the job ends.
 *  The exit mapping (7.8.24 "十" 3): the host's own kill -> timeout; docker not starting the job
 *  (spawn error, exit 125-127) -> calc_unavailable docker_unavailable; any other exit, 137 out of
 *  memory included, is a result: the program's exit code and stderr tail are what the model reads.
 *
 *  start(): removes the containers an earlier process of THIS host left (its own label
 *  aitelier.calc.host=<id of its tmp dir>; every job also carries aitelier.calc=1) and the
 *  calc-tmp directory, then the self-check: the image is there (`docker image inspect`) and
 *  `print(2**10)` reads back 1024. Until it passes every job answers calc_unavailable; a job tries
 *  it again at most once per retryMs. Logs carry an elapsed time, an exit code, a refusal code,
 *  a count and a size of the output files, and the self-check state - never the code, stdout,
 *  stderr or a file name. */
import { randomBytes } from 'node:crypto';
import { chmod, copyFile, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { spawn as spawnChild } from 'node:child_process';
import { isAbsolute, join } from 'node:path';
import { createDockerRun, dockerEnv, dockerHostLabel } from './office.mjs';

const MiB = 1024 * 1024;
export const CALC_LIMITS = Object.freeze({
  wallMs: 30000, // one container's wall clock; then `docker kill` by its name
  killWaitMs: 15000, // how long that kill is waited for before the slot is let go
  queueWaitMs: 30000, // one container per host; a call that waits longer answers busy
  retryMs: 10 * 60 * 1000, // a failed self-check is tried again by a job at most this often
  frameBytes: 56 * MiB, // the container's stdout (the frame); past it: killed, output_too_large
  stdoutBytes: 16 * 1024, stderrBytes: 2 * 1024, // the program's stdout head and stderr tail
  files: 4, fileBytes: 10 * MiB, // output files a call
  cpus: '1', memory: '512m', pids: 64,
  tmpfs: '/tmp:rw,size=64m,nr_inodes=1024', outTmpfs: '/job/out:rw,size=48m,nr_inodes=64,mode=1777',
});
/** The container's user: nobody, never the host's own (in the docker group: root once out). */
export const CALC_USER = '65534:65534';
export const CALC_LABEL = 'aitelier.calc=1';
/** The label naming one host's jobs: its calc-tmp, hashed (startup removes only these). */
export const calcHostLabel = tmpRoot => dockerHostLabel('calc', tmpRoot);
export const CALC_NAME_PREFIX = 'aitelier-calc-';
export const SELFCHECK_CODE = 'print(2**10)\n';
const IMAGE_ID = /^sha256:[0-9a-f]{64}$/;
const EXT = /^[a-z0-9]{1,8}$/;

/** The docker run arguments of one job: constants, the host's random name, the job's own input
 *  directory and the pinned image. Exported for the tests that pin them. */
export function calcRunArgs({ name, inDir, image, hostLabel, limits = CALC_LIMITS }) {
  return ['run', '--rm', '--name', name, '--label', CALC_LABEL, '--label', hostLabel,
    '--network', 'none', '--log-driver', 'none', '-u', CALC_USER, '--read-only',
    '--tmpfs', limits.tmpfs, '--tmpfs', limits.outTmpfs, '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
    '--pids-limit', String(limits.pids), '--cpus', limits.cpus, '--memory', limits.memory, '--memory-swap', limits.memory,
    '-e', 'HOME=/tmp', '-v', `${inDir}:/job/in:ro`, '-w', '/job/out', image];
}

/** A string's UTF-8 cut to its first (headBytes) or last (tailBytes) `limit` bytes, at a character
 *  boundary (a cut character is left out whole). */
const headBytes = (text, limit) => {
  const b = Buffer.from(text, 'utf8');
  if (b.length <= limit) return text;
  let end = limit;
  while (end > 0 && (b[end] & 0xc0) === 0x80) end -= 1;
  return b.subarray(0, end).toString('utf8');
};
const tailBytes = (text, limit) => {
  const b = Buffer.from(text, 'utf8');
  if (b.length <= limit) return text;
  let start = b.length - limit;
  while (start < b.length && (b[start] & 0xc0) === 0x80) start += 1;
  return b.subarray(start).toString('utf8');
};

/** A frame read back (the wrapper's, or one the program forged): {result} with every bound
 *  applied, {tooLarge: true}, or undefined when it is not a frame. */
export function parseFrame(bytes, limits = CALC_LIMITS) {
  let frame;
  try { frame = JSON.parse(bytes.toString('utf8')); } catch { return undefined; }
  if (!frame || typeof frame !== 'object' || !Number.isSafeInteger(frame.exit_code) || typeof frame.timed_out !== 'boolean'
    || typeof frame.stdout !== 'string' || typeof frame.stderr_tail !== 'string' || !Array.isArray(frame.files)) return undefined;
  if (frame.files.length > limits.files) return { tooLarge: true };
  const files = [];
  for (const file of frame.files) {
    if (!file || typeof file.data !== 'string') return undefined;
    // base64 is 4 characters per 3 bytes: a string that long cannot hold a file within the bound.
    if (file.data.length > Math.ceil(limits.fileBytes / 3) * 4 + 4) return { tooLarge: true };
    const data = Buffer.from(file.data, 'base64');
    if (data.length > limits.fileBytes) return { tooLarge: true };
    files.push({ bytes: data });
  }
  const cut = Buffer.byteLength(frame.stdout, 'utf8') > limits.stdoutBytes;
  return { result: { exit_code: frame.exit_code, timed_out: frame.timed_out,
    stdout: headBytes(frame.stdout, limits.stdoutBytes),
    stdout_truncated: cut || frame.stdout_truncated === true,
    stderr_tail: tailBytes(frame.stderr_tail, limits.stderrBytes), files,
    skipped: Number.isSafeInteger(frame.skipped) && frame.skipped > 0 ? frame.skipped : 0 } };
}

/** @param image the pinned image id (`sha256:` + 64 hex) @param docker the docker command
 *  @param tmpRoot <state dir>/calc-tmp @param log (line) => void. */
export function createCalcRunner({ image, docker = 'docker', tmpRoot, log = () => {}, now = Date.now,
  limits: overrides, spawn = spawnChild, env = dockerEnv(), random = n => randomBytes(n) } = {}) {
  const limits = { ...CALC_LIMITS, ...(overrides ?? {}) };
  const rooted = typeof tmpRoot === 'string' && isAbsolute(tmpRoot);
  const pinned = typeof image === 'string' && IMAGE_ID.test(image);
  const hostLabel = calcHostLabel(tmpRoot);
  const dockerRun = createDockerRun({ spawn, docker, env, limits, overflowBytes: limits.frameBytes });
  let status = { state: 'pending', checkedAt: -Infinity };

  // The one slot and its queue: owners' waiters first (in their order), then the groups'.
  let held = false;
  const waiting = [];
  const release = () => { const next = waiting.shift(); if (next) next.grant(); else held = false; };
  /** {release} or {refused}: busy past queueWaitMs, timeout when the caller aborted while waiting. */
  function acquire(priority, signal) {
    return new Promise(resolve => {
      let released = false;
      const once = () => { if (!released) { released = true; release(); } };
      if (signal?.aborted) { resolve({ refused: 'timeout' }); return; }
      if (!held && !waiting.length) { held = true; resolve({ release: once }); return; }
      let timer;
      const leave = refused => {
        const at = waiting.indexOf(entry);
        if (at >= 0) waiting.splice(at, 1);
        clearTimeout(timer);
        signal?.removeEventListener?.('abort', onAbort);
        resolve({ refused });
      };
      const onAbort = () => leave('timeout');
      const entry = { priority, grant: () => { clearTimeout(timer); signal?.removeEventListener?.('abort', onAbort); resolve({ release: once }); } };
      const at = priority === 'owner' ? waiting.findIndex(other => other.priority !== 'owner') : -1;
      if (at < 0) waiting.push(entry); else waiting.splice(at, 0, entry);
      timer = setTimeout(() => leave('busy'), limits.queueWaitMs);
      signal?.addEventListener?.('abort', onAbort, { once: true });
    });
  }

  /** One container over a fresh input directory: {result} or {refused, detail?}. */
  async function job({ code, inputs = [] }, signal) {
    let dir;
    try {
      await mkdir(tmpRoot, { recursive: true, mode: 0o700 });
      await chmod(tmpRoot, 0o700);
      dir = await mkdtemp(join(tmpRoot, 'j-'));
      await chmod(dir, 0o700);
      const inDir = join(dir, 'in');
      // Readable by the container's nobody; nothing of it is writable there (mounted :ro).
      await mkdir(inDir, { mode: 0o755 });
      await chmod(inDir, 0o755);
      await writeFile(join(inDir, 'main.py'), code, { mode: 0o644 });
      await chmod(join(inDir, 'main.py'), 0o644);
      for (const [i, input] of inputs.entries()) {
        const target = join(inDir, `f${i + 1}.${EXT.test(input.ext ?? '') ? input.ext : 'bin'}`);
        await copyFile(input.path, target);
        await chmod(target, 0o644);
      }
      const name = CALC_NAME_PREFIX + Buffer.from(random(8)).toString('hex');
      const run = await dockerRun(calcRunArgs({ name, inDir, image, hostLabel, limits }),
        { signal, wallMs: limits.wallMs, name, keepStdout: true });
      if (run.spawnError) return { refused: 'calc_unavailable', detail: 'docker_unavailable' };
      if (run.overflow) return { refused: 'output_too_large' };
      if (run.killedByUs) return { refused: 'timeout' };
      if (run.code === 125 || run.code === 126 || run.code === 127) return { refused: 'calc_unavailable', detail: 'docker_unavailable' };
      const parsed = run.code === 0 && !run.signal ? parseFrame(run.stdout, limits) : undefined;
      if (parsed?.tooLarge) return { refused: 'output_too_large' };
      if (parsed) return parsed;
      // No frame: the container itself ended (137: out of memory). A result, not a tool failure.
      return { result: { exit_code: Number.isSafeInteger(run.code) ? run.code : 137, timed_out: false, stdout: '', stdout_truncated: false,
        stderr_tail: tailBytes(run.stderrTail.toString('utf8'), limits.stderrBytes), files: [], skipped: 0 } };
    } finally { if (dir) await rm(dir, { recursive: true, force: true }).catch(() => {}); }
  }

  async function selfcheck() {
    const started = now();
    let reason;
    try {
      if (!pinned) reason = 'no_image_configured';
      else if (!rooted) reason = 'selfcheck_failed';
      else {
        const inspect = await dockerRun(['image', 'inspect', '--format', '{{.Id}}', image], { wallMs: limits.wallMs, keepStdout: true });
        if (inspect.spawnError || inspect.killedByUs) reason = 'docker_unavailable';
        else if (inspect.code !== 0) reason = /no such (image|object)/i.test(inspect.stderrTail.toString('utf8')) ? 'selfcheck_failed' : 'docker_unavailable';
        else if (inspect.stdout.toString('utf8').trim() !== image) reason = 'selfcheck_failed';
        else {
          const ran = await job({ code: SELFCHECK_CODE });
          if (ran.detail === 'docker_unavailable') reason = 'docker_unavailable';
          else if (ran.result?.exit_code !== 0 || ran.result.stdout.trim() !== '1024') reason = 'selfcheck_failed';
        }
      }
    } catch { reason = 'selfcheck_failed'; }
    status = { state: reason ? 'unavailable' : 'ready', reason, checkedAt: now() };
    if (reason) log(`calc_unavailable: ${reason}`);
    else log('calc_ready ' + JSON.stringify({ elapsed_ms: now() - started }));
  }

  async function available() {
    if (status.state === 'ready') return true;
    if (status.state === 'unavailable' && now() - status.checkedAt < limits.retryMs) return false;
    await selfcheck();
    return status.state === 'ready';
  }

  /** One call: {code, inputs: [{path, ext}]} with `priority` 'owner' or 'group' -> {result:
   *  {exit_code, timed_out, stdout, stdout_truncated, stderr_tail, files: [{bytes}], skipped}} or
   *  {refused, detail?}. One log line. */
  async function run(request, { priority = 'group' } = {}, signal) {
    const started = now();
    const done = answer => {
      const files = answer.result?.files ?? [];
      log('calc_job ' + JSON.stringify({ priority, elapsed_ms: now() - started,
        ...(answer.refused ? { refused: answer.refused, ...(answer.detail ? { detail: answer.detail } : {}) }
          : { exit_code: answer.result.exit_code, timed_out: answer.result.timed_out, files: files.length,
            bytes: files.reduce((sum, file) => sum + file.bytes.length, 0) }) }));
      return answer;
    };
    if (!pinned) return done({ refused: 'calc_unavailable', detail: 'no_image_configured' });
    if (typeof request?.code !== 'string' || !Array.isArray(request.inputs ?? [])) return done({ refused: 'invalid_request' });
    const slot = await acquire(priority === 'owner' ? 'owner' : 'group', signal);
    if (slot.refused) return done({ refused: slot.refused });
    try {
      if (!(await available())) return done({ refused: 'calc_unavailable', detail: status.reason ?? 'selfcheck_failed' });
      return done(await job(request, signal));
    } catch { return done({ refused: 'calc_unavailable', detail: 'docker_unavailable' }); } finally { slot.release(); }
  }

  /** Startup: an earlier process's containers (by label) and job directories go, then the
   *  self-check. Nothing at all when no image is configured (the feature is off). */
  async function start() {
    if (!pinned) { status = { state: 'unavailable', reason: 'no_image_configured', checkedAt: now() }; log('calc_unavailable: no_image_configured'); return; }
    if (rooted) await rm(tmpRoot, { recursive: true, force: true }).catch(() => {});
    const left = await dockerRun(['ps', '-aq', '--filter', `label=${hostLabel}`], { wallMs: limits.wallMs, keepStdout: true });
    const ids = left.code === 0 ? left.stdout.toString('utf8').split(/\s+/).filter(id => /^[0-9a-f]{12,64}$/.test(id)) : [];
    if (ids.length) await dockerRun(['rm', '-f', ...ids], { wallMs: limits.wallMs });
    const slot = await acquire('owner');
    if (!slot.release) return;
    try { await selfcheck(); } finally { slot.release(); }
  }

  return { run, start, status: () => ({ ...status }), label: hostLabel };
}

const OOXML_SHEET = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
/** The output types a call keeps (7.8.24 "十" 1), by the turn's audience, each with the magic it
 *  is checked by here (nothing is unpacked: an xlsx is only `PK\x03\x04` here; the media library's
 *  own check of its zip directory decides when it is stored) and the extension it is stored by. */
export const CALC_OUTPUTS = Object.freeze({
  'image/png': { ext: 'png', magic: Buffer.from('89504e470d0a1a0a', 'hex'), group: true },
  'image/jpeg': { ext: 'jpg', magic: Buffer.from('ffd8ff', 'hex'), group: false },
  'application/pdf': { ext: 'pdf', magic: Buffer.from('%PDF-', 'latin1'), group: false },
  [OOXML_SHEET]: { ext: 'xlsx', magic: Buffer.from('504b0304', 'hex'), group: false },
});
/** The type an output's first bytes say, of those the audience keeps; else undefined. */
export function outputType(bytes, audience) {
  for (const [mime, type] of Object.entries(CALC_OUTPUTS))
    if ((audience === 'owner' || type.group) && bytes.length >= type.magic.length && bytes.subarray(0, type.magic.length).equals(type.magic)) return mime;
  return undefined;
}

/** The host's media op for calculate (mediaOps spreads it in): the refs' library files as the
 *  inputs, the runner, and every output of a kept type stored as `audience`'s (source generated,
 *  named calc-<n>.<ext>: the program's own names are never kept). An output of another type, or
 *  one the library refuses or types otherwise, is not stored and only counted in files_skipped. */
export function calcMediaOps({ library, calc }) {
  async function calculate({ code, refs = [] }, audience, signal) {
    const rows = refs.map(ref => library?.lookup?.(ref));
    if (rows.some(row => !row?.path)) return { refused: 'unknown_ref' };
    const ran = await calc.run({ code, inputs: rows.map(row => ({ path: row.path, ext: row.ext })) },
      { priority: audience === 'owner' ? 'owner' : 'group' }, signal);
    if (!ran.result) return { refused: ran.refused ?? 'calc_unavailable', ...(ran.detail ? { detail: ran.detail } : {}) };
    const files = [];
    let skipped = ran.result.skipped;
    for (const [i, file] of ran.result.files.entries()) {
      const mime = outputType(file.bytes, audience);
      if (!mime) { skipped += 1; continue; }
      let stored;
      try {
        stored = await library.ingest({ channel: 'generated', source: 'generated', audience, name: `calc-${i + 1}.${CALC_OUTPUTS[mime].ext}` },
          () => file.bytes, { signal });
      } catch { stored = undefined; }
      if (!stored?.ref) { skipped += 1; continue; }
      if (stored.mime !== mime) { try { library.withdraw(stored.ref); } catch { /* cleaned in 30 days */ } skipped += 1; continue; }
      files.push({ ref: stored.ref, mime: stored.mime, bytes: stored.size });
    }
    const { exit_code, timed_out, stdout, stdout_truncated, stderr_tail } = ran.result;
    return { exit_code, timed_out, stdout, stdout_truncated, stderr_tail, files, ...(skipped ? { files_skipped: skipped } : {}) };
  }
  return { calculate };
}
