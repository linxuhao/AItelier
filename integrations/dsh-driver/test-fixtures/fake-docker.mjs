#!/usr/bin/env node
// Step 24.2d: a fake `docker` for office.test.mjs - no daemon, no image, no OnlyOffice. Every call
// appends its argv (JSON, one line) to $FAKE_DOCKER_LOG and answers from the plan in
// $FAKE_DOCKER_PLAN (JSON, read on every call so a test can change it between calls):
//   inspect: {code, stdout, stderr}         `docker image inspect ...`
//   ps: "id id"                             `docker ps -aq --filter ...`
//   fonts: {code, write: true}              a run whose entrypoint is allfontsgen
//   run: {code, sleepMs, write, from, stdout, stderr}  any other run (docbuilder, x2t)
// A run's output goes to the file the job names: docbuilder's `builder.SaveFile("...", "/job/out/<name>")`
// line, x2t's <m_sFileTo>; `write` is "pdf" (%PDF- and the job's own script or task, so its text
// is in it), "copy" (the bytes of `from`), "symlink" (a link to /etc/hostname), "big" (2 MiB),
// "junk" (bytes with no magic) or "none". $FAKE_DOCKER_CAPTURE, when set, gets a copy of /job/in.
import { appendFileSync, copyFileSync, cpSync, readFileSync, readdirSync, symlinkSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const args = process.argv.slice(2);
appendFileSync(process.env.FAKE_DOCKER_LOG, JSON.stringify(args) + '\n');
const plan = JSON.parse(readFileSync(process.env.FAKE_DOCKER_PLAN, 'utf8'));
const answer = ({ code = 0, stdout = '', stderr = '' } = {}) => {
  if (stdout) process.stdout.write(stdout);
  if (stderr) process.stderr.write(stderr);
  process.exitCode = code;
};

if (args[0] === 'image') answer(plan.inspect);
else if (args[0] === 'ps') answer({ stdout: plan.ps ?? '' });
else if (args[0] === 'rm' || args[0] === 'kill') answer();
else if (args[0] === 'run') {
  const mount = target => { const i = args.findIndex((a, k) => args[k - 1] === '-v' && a.split(':')[1] === target); return i < 0 ? undefined : args[i].split(':')[0]; };
  const out = mount('/job/out'), input = mount('/job/in');
  const entrypoint = args[args.indexOf('--entrypoint') + 1];
  if (entrypoint.endsWith('allfontsgen')) {
    const step = plan.fonts ?? {};
    if (step.write !== false && !step.code) {
      writeFileSync(join(out, 'AllFonts.js'), 'window["__fonts_files"] = [];');
      writeFileSync(join(out, 'font_selection.bin'), Buffer.alloc(16));
    }
    answer(step);
  } else {
    const step = plan.run ?? {};
    if (process.env.FAKE_DOCKER_CAPTURE && input) cpSync(input, process.env.FAKE_DOCKER_CAPTURE, { recursive: true });
    const task = readFileSync(join(input, args.at(-1).replace('/job/in/', '')), 'utf8');
    const name = (/builder\.SaveFile\("\w+", "\/job\/out\/([\w.]+)"\)/.exec(task) ?? /<m_sFileTo>\/job\/out\/([\w.]+)</.exec(task))?.[1];
    const finish = () => {
      const target = name ? join(out, name) : undefined;
      if (target && step.write === 'pdf') writeFileSync(target, Buffer.concat([Buffer.from('%PDF-1.7\n'), Buffer.from(task)]));
      else if (target && step.write === 'copy') copyFileSync(step.from, target);
      else if (target && step.write === 'symlink') symlinkSync('/etc/hostname', target);
      else if (target && step.write === 'big') writeFileSync(target, Buffer.concat([Buffer.from('%PDF-1.7\n'), Buffer.alloc(2 * 1024 * 1024)]));
      else if (target && step.write === 'junk') writeFileSync(target, Buffer.alloc(100, 7));
      if (step.extra) writeFileSync(join(out, 'extra.bin'), 'x');
      void readdirSync(out);
      answer(step);
    };
    if (step.sleepMs) setTimeout(finish, step.sleepMs); else finish();
  }
} else answer({ code: 1, stderr: 'fake docker: unknown command' });
