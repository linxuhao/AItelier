#!/usr/bin/env node
// Step 24.4b: a fake `docker` for calc.test.mjs - no daemon, no image, no python. Every call
// appends its argv (JSON, one line) to $FAKE_DOCKER_LOG and answers from the plan in
// $FAKE_DOCKER_PLAN (JSON, read on every call so a test can change it between calls):
//   inspect: {code, stdout, stderr}      `docker image inspect ...`
//   ps: "id id"                          `docker ps -aq --filter ...`
//   kill: {sleepMs}                      `docker kill <name>`: answers after sleepMs, then logs ["kill-done", name]
//   run: {code, sleepMs, stdout, stderr, frame, flood}   a job (`docker run`):
//     the self-check's program (print(2**10)) answers a frame with stdout "1024\n" unless
//     `selfcheck` is set in the plan ({stdout, code}); any other program answers `frame` (an
//     object: written as JSON; a file's `text` or `hex` becomes its base64 data, `size` that many
//     bytes of "a"), or `stdout` verbatim, or `flood` bytes of "x" in 64 KiB chunks.
// $FAKE_DOCKER_CAPTURE, when set, gets a copy of /job/in and `modes.json` (each entry's mode).
import { appendFileSync, cpSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const args = process.argv.slice(2);
appendFileSync(process.env.FAKE_DOCKER_LOG, JSON.stringify(args) + '\n');
const plan = JSON.parse(readFileSync(process.env.FAKE_DOCKER_PLAN, 'utf8'));
const answer = ({ code = 0, stdout = '', stderr = '' } = {}) => {
  if (stdout) process.stdout.write(stdout);
  if (stderr) process.stderr.write(stderr);
  process.exitCode = code;
};
const encode = frame => JSON.stringify({ v: 1, exit_code: 0, timed_out: false, stdout: '', stdout_truncated: false, stderr_tail: '', skipped: 0,
  ...frame, files: (frame.files ?? []).map(file => ({ name: file.name ?? 'out.bin',
    data: file.data ?? Buffer.from(file.hex !== undefined ? Buffer.from(file.hex, 'hex') : file.size !== undefined ? Buffer.alloc(file.size, 'a') : file.text ?? '').toString('base64') })) });

if (args[0] === 'image') answer(plan.inspect);
else if (args[0] === 'ps') answer({ stdout: plan.ps ?? '' });
else if (args[0] === 'rm') answer();
else if (args[0] === 'kill') {
  setTimeout(() => { appendFileSync(process.env.FAKE_DOCKER_LOG, JSON.stringify(['kill-done', args[1]]) + '\n'); answer(); }, plan.kill?.sleepMs ?? 0);
}
else if (args[0] === 'run') {
  const mount = args.find((a, k) => args[k - 1] === '-v' && a.split(':')[1] === '/job/in');
  const input = mount?.split(':')[0];
  const program = input ? readFileSync(join(input, 'main.py'), 'utf8') : '';
  if (process.env.FAKE_DOCKER_CAPTURE && input && program !== 'print(2**10)\n') {
    cpSync(input, process.env.FAKE_DOCKER_CAPTURE, { recursive: true });
    const modes = { '.': (statSync(input).mode & 0o777).toString(8) };
    for (const name of readdirSync(input)) modes[name] = (statSync(join(input, name)).mode & 0o777).toString(8);
    writeFileSync(join(process.env.FAKE_DOCKER_CAPTURE, 'modes.json'), JSON.stringify(modes));
  }
  const step = plan.run ?? {};
  const finish = () => {
    if (program === 'print(2**10)\n') {
      const check = plan.selfcheck ?? {};
      answer({ code: check.code ?? 0, stdout: encode({ stdout: check.stdout ?? '1024\n' }) });
      return;
    }
    if (step.flood) {
      const chunk = Buffer.alloc(65536, 'x');
      let left = step.flood;
      const pump = () => {
        while (left > 0) {
          const n = Math.min(left, chunk.length);
          left -= n;
          if (!process.stdout.write(chunk.subarray(0, n))) { process.stdout.once('drain', pump); return; }
        }
        answer({ code: step.code ?? 0 });
      };
      pump();
      return;
    }
    answer({ code: step.code ?? 0, stderr: step.stderr ?? '',
      stdout: step.frame ? encode(step.frame) : step.stdout ?? encode({}) });
  };
  // The self-check answers at once whatever the plan's run does.
  if (step.sleepMs && program !== 'print(2**10)\n') setTimeout(finish, step.sleepMs); else finish();
} else answer({ code: 1, stderr: 'fake docker: unknown command' });
