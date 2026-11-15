import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, appendFile, rm } from 'node:fs/promises';
import { openSync, closeSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { zstdCompressSync } from 'node:zlib';
import { zstdFrames, tailHistory, historyPage } from './history.mjs';

const event = seq => ({ type: seq % 2 ? 'assistant/message' : 'step/start', seq, time: 1000 + seq, data: { n: seq } });
const frame = lines => zstdCompressSync(Buffer.from(lines.map(l => JSON.stringify(l)).join('\n') + '\n'));

async function log(t, events, { header = { type: 'session/header', id: 's' }, perFrame = 1 } = {}) {
  const dir = await mkdtemp(join(tmpdir(), 'history-tail-')); t.after(() => rm(dir, { recursive: true, force: true }));
  const file = join(dir, 'session.v4.jsonl.zstd');
  const parts = [frame([header])];
  for (let i = 0; i < events.length; i += perFrame) parts.push(frame(events.slice(i, i + perFrame)));
  await writeFile(file, Buffer.concat(parts));
  return file;
}

test('tail pages equal historyPage over every event, for one and several events per frame', async t => {
  const events = Array.from({ length: 300 }, (_, i) => event(i));
  for (const perFrame of [1, 7]) {
    const file = await log(t, events, { perFrame });
    for (const before_seq of [Number.MAX_SAFE_INTEGER, 299, 150, 13, 1, 0])
      for (const limit of [1, 5, 50, 200])
        assert.deepEqual(tailHistory(file, { before_seq, limit }), historyPage(events, { before_seq, limit }), `perFrame ${perFrame} before ${before_seq} limit ${limit}`);
  }
});

test('a frame the live driver is still appending is ignored, not an error', async t => {
  const events = Array.from({ length: 20 }, (_, i) => event(i));
  const file = await log(t, events);
  const whole = frame([event(20), event(21)]);
  await appendFile(file, whole.subarray(0, whole.length - 5));          // a torn trailing frame
  assert.deepEqual(tailHistory(file, { limit: 5 }), historyPage(events, { limit: 5 }));
  const fd = openSync(file, 'r');
  try { assert.equal(zstdFrames(fd, statSync(file).size).length, 21, 'header + 20 complete frames'); } finally { closeSync(fd); }
});
