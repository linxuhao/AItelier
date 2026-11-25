/** Step 24.4b (design-steward 7.8.24 "十四" 24.4b 集成测试): calculate's sandbox against the REAL
 *  calc image and docker, run by the director in the deploy window on the steward's host:
 *
 *    AITELIER_CALC_IT_IMAGE=sha256:... node --test calc.integration.mjs
 *
 *  This file is named so that `node --test` discovery does NOT find it, and it is deliberately
 *  absent from the npm test script: it needs a real docker daemon and a prebuilt image, which the
 *  suite's own containers do not have, so under discovery it could only ever appear as eight
 *  skipped tests. The repo's gate over this suite (tests/integration/test_driver_node_suite_runs.py)
 *  counts a skip as RED - it requires every test the runner reports to have passed - so an arm
 *  that is discovered but cannot run would red the python suite on every tree. Invoke it
 *  explicitly, as above. Without AITELIER_CALC_IT_IMAGE its eight tests skip and it exits 0, so a
 *  bare run is safe anywhere; calc.test.mjs covers the host's side with a fake docker. Nothing is
 *  downloaded: every job is a disposable container of the pinned image, labelled with this run's
 *  own host label, so a steward host on the same daemon is never touched. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, statfsSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createCalcRunner } from './calc.mjs';

const image = process.env.AITELIER_CALC_IT_IMAGE;
const skip = image ? false : 'set AITELIER_CALC_IT_IMAGE=sha256:... (the built calc image) to run against real docker';
const work = image ? mkdtempSync(join(tmpdir(), 'calc-it-')) : undefined;
const logs = [];
let runner;
const run = async (code, inputs = []) => {
  if (!runner) {
    // The wrapper answers at its own CHILD_SECONDS=25 bound; the driver's default 30 s wall
    // backstop leaves only 5 s of slack, which a loaded host can eat, turning the wrapper's
    // answer into {refused: "timeout"}. Lift only the backstop well past the wrapper so the
    // timed_out assertion below measures the wrapper's real answer, not the driver's kill.
    runner = createCalcRunner({ image, tmpRoot: join(work, 'calc-tmp'), log: line => logs.push(line),
      limits: { wallMs: 60000 } });
    await runner.start();
    assert.equal(runner.status().state, 'ready', logs.join('\n'));
  }
  return runner.run({ code, inputs }, { priority: 'owner' });
};
const freeBytes = () => { const s = statfsSync(work); return s.bavail * s.bsize; };

test.after(() => { if (work) rmSync(work, { recursive: true, force: true }); });

test('the self-check passes and a plain program prints', { skip }, async () => {
  const answer = await run('print(sum(range(101)))');
  assert.equal(answer.result?.exit_code, 0, JSON.stringify(answer));
  assert.equal(answer.result.stdout.trim(), '5050');
});

test('no network: a connection to 1.1.1.1:53 fails', { skip }, async () => {
  const answer = await run('import socket\ns = socket.socket()\ns.settimeout(3)\ntry:\n    s.connect(("1.1.1.1", 53)); print("CONNECTED")\nexcept OSError as e:\n    print("refused", type(e).__name__)');
  assert.match(answer.result.stdout, /^refused /, answer.result.stdout);
});

test('/job/in is read-only, the root is read-only, the user is nobody', { skip }, async () => {
  const answer = await run([
    'import os',
    'for p in ("/job/in/x", "/usr/x", "/opt/calc/x"):',
    '    try:',
    '        open(p, "w").write("x"); print("WROTE", p)',
    '    except OSError as e:',
    '        print("denied", p)',
    'print("uid", os.getuid(), os.getgid())',
  ].join('\n'));
  assert.ok(!answer.result.stdout.includes('WROTE'), answer.result.stdout);
  assert.match(answer.result.stdout, /uid 65534 65534/);
});

test('the tmpfs bounds: 10,000 files or 2 GiB end in ENOSPC, and the host disk does not grow', { skip }, async () => {
  const before = freeBytes();
  const files = await run('import errno\nn = 0\ntry:\n    for i in range(10000):\n        open(f"f{i}", "w").close(); n += 1\nexcept OSError as e:\n    print("stopped", n, errno.errorcode[e.errno])');
  assert.match(files.result.stdout, /stopped \d+ ENOSPC/, files.result.stdout);
  const big = await run('import errno\ntry:\n    with open("big", "wb") as f:\n        for i in range(2048): f.write(b"x" * 1048576)\nexcept OSError as e:\n    print("stopped", errno.errorcode[e.errno])');
  assert.match(big.result.stdout, /stopped ENOSPC/, big.result.stdout);
  assert.ok(before - freeBytes() < 64 * 1024 * 1024, 'the host disk did not take the writes');
});

test('/job/out: a symlink and a FIFO never come back; a real file does', { skip }, async () => {
  const answer = await run('import os\nos.symlink("/etc/passwd", "link.png")\nos.mkfifo("pipe.png")\nopen("ok.png", "wb").write(bytes.fromhex("89504e470d0a1a0a") + bytes(64))');
  assert.equal(answer.result.files.length, 1);
  assert.equal(answer.result.files[0].bytes.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  assert.equal(answer.result.skipped, 2);
});

test('printing 1 GiB returns its first 16 KiB, stdout_truncated', { skip }, async () => {
  const answer = await run('import sys\nfor i in range(1024): sys.stdout.write("y" * 1048576)');
  assert.equal(Buffer.byteLength(answer.result.stdout), 16384);
  assert.equal(answer.result.stdout_truncated, true);
});

test('the libraries are there: numpy, sympy, pandas and a matplotlib chart', { skip }, async () => {
  const input = join(work, 'week.csv');
  writeFileSync(input, 'day,spent\nMon,12\nTue,30\nWed,8\n');
  const answer = await run([
    'import numpy, sympy, pandas',
    'import matplotlib.pyplot as plt',
    'df = pandas.read_csv("/job/in/f1.csv")',
    'plt.bar(df.day, df.spent); plt.savefig("week.png")',
    'print(int(numpy.sum(df.spent)), sympy.factorint(1024))',
  ].join('\n'), [{ path: input, ext: 'csv' }]);
  assert.equal(answer.result.exit_code, 0, answer.result.stderr_tail);
  assert.match(answer.result.stdout, /^50 \{2: 10\}/);
  assert.equal(answer.result.files.length, 1);
  assert.equal(answer.result.files[0].bytes.subarray(0, 4).toString('latin1'), '\x89PNG');
});

test('the wall clock: a program that never ends is timed out by the wrapper inside 30 s', { skip }, async () => {
  const answer = await run('while True: pass');
  assert.equal(answer.result?.timed_out, true, JSON.stringify(answer));
});
