/** Step 24.2 (design-steward 7.8.12, 7.8.12a, 7.8.18 "媒体"): the steward host's media library
 *  (lib/media.mjs), ownerChannels' storeMedia / attachments / sendFile (lib/channels.mjs) with a
 *  fake channel end to end, and media_call / send_media (lib/media-tools.mjs). No DSH. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, statSync, writeFileSync, chmodSync, existsSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createMediaLibrary, sniff, imageSize, covers, placeholder, attachmentLine, MEDIA_LIMITS, REF_PATTERN }
  from '../lib/media.mjs';
import { createOwnerChannels } from '../lib/channels.mjs';
import { registerMediaTools, UrlLedger } from '../lib/media-tools.mjs';
import { GROUP_TOOLS } from '../lib/group-turns.mjs';

const MiB = 1024 * 1024;
const png = (width = 640, height = 480, extra = 0) => {
  const head = Buffer.alloc(33 + extra);
  Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex').copy(head, 0);
  head.writeUInt32BE(width, 16); head.writeUInt32BE(height, 20);
  return head;
};
const jpeg = (width, height) => Buffer.concat([Buffer.from('ffd8ffe000104a46494600010100000100010000', 'hex'),
  Buffer.from([0xff, 0xc0, 0x00, 0x11, 0x08, height >> 8, height & 255, width >> 8, width & 255, 0x03]), Buffer.alloc(64)]);
const pdf = size => Buffer.concat([Buffer.from('%PDF-1.7\n'), Buffer.alloc(size)]);
const wav = () => Buffer.concat([Buffer.from('RIFF\x24\x00\x00\x00WAVEfmt ', 'latin1'), Buffer.alloc(100)]);
const zip = () => Buffer.concat([Buffer.from('504b030414000600', 'hex'), Buffer.alloc(100)]);
const exe = () => Buffer.concat([Buffer.from('MZ'), Buffer.alloc(100)]);
const root = () => mkdtempSync(join(tmpdir(), 'media-'));
/** A source that hands out `total` bytes in `chunk`-sized pieces, counting what was pulled. */
function counted(head, total, chunk = MiB) {
  const state = { pulled: 0 };
  state.open = async () => (async function* () {
    let sent = 0;
    while (sent < total) {
      const size = Math.min(chunk, total - sent);
      const piece = sent === 0 ? Buffer.concat([head, Buffer.alloc(Math.max(0, size - head.length))]) : Buffer.alloc(size);
      sent += piece.length; state.pulled = sent;
      yield piece;
    }
  })();
  return state;
}
/** An ffprobe that answers like `-show_entries format=duration:stream=codec_type:
 *  stream_disposition=attached_pic -of json` would: one audio stream, plus `streams`. */
const fakeProbe = (dir, seconds, streams = []) => {
  const path = join(dir, `ffprobe-${String(seconds).replace(/[^0-9a-z.]/gi, '_')}-${streams.length}-${Math.random().toString(16).slice(2, 8)}`);
  const answer = JSON.stringify({ streams: [{ codec_type: 'audio', disposition: { attached_pic: 0 } }, ...streams], format: { duration: String(seconds) } });
  writeFileSync(path, `#!/bin/sh\ncat <<'EOF'\n${answer}\nEOF\n`); chmodSync(path, 0o755);
  return path;
};
const VIDEO = { codec_type: 'video', disposition: { attached_pic: 0 } };
const COVER = { codec_type: 'video', disposition: { attached_pic: 1 } };
/** The first bytes of an ordinary MP4 video (ftyp isom, as ffmpeg/x264 writes it), an MP4 one
 *  (mp42), a Matroska/WebM one, and an M4A. */
const mp4Video = () => Buffer.concat([Buffer.from('000000206674797069736f6d0000020069736f6d69736f32617663316d703431', 'hex'), Buffer.alloc(100)]);
const mp42 = () => Buffer.concat([Buffer.from('00000018667479706d703432000000006d70343269736f6d', 'hex'), Buffer.alloc(100)]);
const mkv = () => Buffer.concat([Buffer.from('1a45dfa3a34286810142f7810142f2810442f381084282886d6174726f736b61', 'hex'), Buffer.alloc(100)]);
const m4a = () => Buffer.concat([Buffer.from('0000001c667479704d344120000002004d34412069736f6d69736f32', 'hex'), Buffer.alloc(100)]);
const owner = { channel: 'wechat', audience: 'owner', source: 'in' };

// --- the library ------------------------------------------------------------------------

test('library: files 0600 under 0700 directories, random m_ refs, an index row with file, sha256 dedupe', async () => {
  const r = root();
  const lib = createMediaLibrary({ root: r, userId: 'owner', now: () => Date.parse('2026-11-22T10:00:00Z') });
  const a = await lib.ingest({ ...owner, messageRef: 'w1', index: 0 }, async () => png());
  assert.match(a.ref, REF_PATTERN);
  assert.equal(a.kind, 'image'); assert.equal(a.mime, 'image/png');
  const row = lib.rows[0];
  assert.deepEqual(Object.keys(row).sort(), ['at', 'audience', 'channel', 'file', 'index', 'messageRef', 'mime', 'ref', 'sha256', 'size', 'source']);
  assert.equal(row.file, `owner/2026-11/${row.sha256}.png`, 'file is relative to the media root');
  assert.equal(statSync(join(r, row.file)).mode & 0o777, 0o600);
  for (const dir of [join(r, 'owner'), join(r, 'owner', '2026-11')]) assert.equal(statSync(dir).mode & 0o777, 0o700, dir);
  assert.equal(statSync(lib.indexFile).mode & 0o777, 0o600);
  // The same bytes again (another message): a new row and ref, the same file.
  const b = await lib.ingest({ ...owner, messageRef: 'w2', index: 0 }, async () => png());
  assert.notEqual(b.ref, a.ref);
  assert.equal(lib.rows[1].file, row.file);
  assert.equal(readdirSync(join(r, 'owner', '2026-11')).length, 1);
  // The ref is not the hash: it says nothing about the bytes.
  assert.ok(!row.sha256.includes(a.ref.slice(2)));
  // A restart reads the index back.
  const again = createMediaLibrary({ root: r, userId: 'owner' });
  assert.equal(again.lookup(a.ref).path, join(r, row.file));
  assert.equal(again.lookup('m_' + 'f'.repeat(16)), undefined);
});

test('library: a root is required and the writer\'s subdirectory is a plain name', () => {
  assert.throws(() => createMediaLibrary({ userId: 'owner' }), /root is required/);
  assert.throws(() => createMediaLibrary({ root: root(), userId: '../x' }), /bad userId/);
});

test('library: idempotent by (channel, messageRef, index) without calling open; a channel\'s 21st inbound file an hour is refused before open', async () => {
  const lib = createMediaLibrary({ root: root() });
  let opened = 0;
  const open = async () => { opened += 1; return png(10 + opened, 10); };
  const first = await lib.ingest({ ...owner, messageRef: 'm', index: 0 }, open);
  const replay = await lib.ingest({ ...owner, messageRef: 'm', index: 0 }, open);
  assert.equal(replay.ref, first.ref); assert.equal(replay.duplicate, true); assert.equal(opened, 1);
  for (let i = 1; i < MEDIA_LIMITS.inbound_per_channel_hour; i += 1) assert.ok((await lib.ingest({ ...owner, messageRef: 'm', index: i }, open)).ref);
  const before = opened;
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'm2', index: 0 }, open), { refused: 'too_large' });
  assert.equal(opened, before, 'the 21st was not fetched');
  // Another channel and generated files are not that channel's count.
  assert.ok((await lib.ingest({ channel: 'slack', audience: 'owner', source: 'in', messageRef: 's', index: 0 }, open)).ref);
  assert.ok((await lib.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, open)).ref);
});

test('library limits: counted as they come and cut at the bound (a declared size is never read)', async () => {
  const lib = createMediaLibrary({ root: root() });
  const image = counted(png(), 100 * MiB);
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'i', index: 0 }, image.open), { refused: 'too_large' });
  assert.ok(image.pulled <= 20 * MiB + MiB, `pulled ${image.pulled}`);
  const file = counted(pdf(0), 100 * MiB);
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'f', index: 0 }, file.open), { refused: 'too_large' });
  assert.ok(file.pulled <= 25 * MiB + MiB);
  assert.ok((await lib.ingest({ ...owner, messageRef: 'f2', index: 0 }, async () => pdf(24 * MiB))).ref, 'a 24 MiB PDF is a file');
  // A 24 MiB PNG is an image (20 MiB), whatever a name says.
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'f3', index: 0 }, async () => png(100, 100, 24 * MiB)), { refused: 'too_large' });
  assert.equal(lib.rows.length, 1, 'nothing over a bound was stored');
  assert.deepEqual(readdirSync(lib.home).filter(n => n.startsWith('.incoming')), [], 'no partial file is left');
});

test('library limits: the type is the first bytes\' (zip, .docm, an executable renamed .png: type)', async () => {
  const lib = createMediaLibrary({ root: root() });
  for (const [ref, bytes] of [['z', zip()], ['d', zip()], ['e', exe()], ['t', Buffer.from('just text, not media at all - and long enough to sniff past sixty-four bytes')]])
    assert.deepEqual(await lib.ingest({ ...owner, messageRef: ref, index: 0 }, async () => bytes), { refused: 'type' }, ref);
  assert.equal(lib.rows.length, 0);
  assert.equal(sniff(jpeg(10, 10))?.mime, 'image/jpeg');
  assert.equal(sniff(wav())?.mime, 'audio/wav');
});

test('library limits: an image over 40 MP is too_large, read from its header; an unreadable header is unsupported', async () => {
  const lib = createMediaLibrary({ root: root() });
  assert.deepEqual(imageSize(png(8000, 6000), 'image/png'), { width: 8000, height: 6000 });
  assert.deepEqual(imageSize(jpeg(7000, 6000), 'image/jpeg'), { width: 7000, height: 6000 });
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'p', index: 0 }, async () => png(8000, 6000)), { refused: 'too_large' });
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'j', index: 0 }, async () => jpeg(7000, 6000)), { refused: 'too_large' });
  assert.ok((await lib.ingest({ ...owner, messageRef: 'ok', index: 0 }, async () => jpeg(4000, 3000))).ref);
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'z', index: 0 }, async () => png(0, 0)), { refused: 'unsupported' });
});

test('library limits: a voice\'s length is measured with ffprobe (configurable); no ffprobe is unsupported (fail closed)', async () => {
  const dir = root();
  const missing = createMediaLibrary({ root: dir, ffprobe: join(dir, 'no-such-ffprobe') });
  assert.deepEqual(await missing.ingest({ ...owner, messageRef: 'v', index: 0 }, async () => wav()), { refused: 'unsupported' });
  const short = createMediaLibrary({ root: root(), ffprobe: fakeProbe(dir, '23.4') });
  const voice = await short.ingest({ ...owner, messageRef: 'v', index: 0 }, async () => wav());
  assert.equal(voice.kind, 'voice'); assert.equal(voice.durationMs, 23400);
  assert.equal(short.rows[0].durationMs, 23400);
  const long = createMediaLibrary({ root: root(), ffprobe: fakeProbe(dir, '130.0') });
  assert.deepEqual(await long.ingest({ ...owner, messageRef: 'v', index: 0 }, async () => wav()), { refused: 'too_large' });
  const broken = createMediaLibrary({ root: root(), ffprobe: fakeProbe(dir, 'N/A') });
  assert.deepEqual(await broken.ingest({ ...owner, messageRef: 'v', index: 0 }, async () => wav()), { refused: 'unsupported' });
});

test('library limits: a container holding a video stream is type, whatever its first bytes (isom, mp42, webm/matroska, ogg); cover art is not video', async () => {
  const dir = root();
  // The first bytes alone take these for audio (the table is shared with audio-only files) ...
  for (const bytes of [mp4Video(), mp42(), mkv(), m4a()]) assert.equal(sniff(bytes)?.kind, 'voice');
  // ... so ffprobe's streams decide: a video stream is refused `type`, inbound or generated.
  const video = createMediaLibrary({ root: root(), ffprobe: fakeProbe(dir, '12.0', [VIDEO]) });
  for (const [i, bytes] of [mp4Video(), mp42(), mkv(), Buffer.concat([Buffer.from('4f67675300020000', 'hex'), Buffer.alloc(100)])].entries())
    assert.deepEqual(await video.ingest({ ...owner, messageRef: 'v', index: i }, async () => bytes), { refused: 'type' }, String(i));
  assert.deepEqual(await video.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => mp4Video()), { refused: 'type' });
  assert.equal(video.rows.length, 0, 'no video was stored');
  assert.deepEqual(readdirSync(video.home).filter(n => n.startsWith('.incoming')), [], 'no partial file is left');
  // Audio in the same containers, or audio with an attached cover picture, is a voice.
  const audio = createMediaLibrary({ root: root(), ffprobe: fakeProbe(dir, '12.0') });
  assert.equal((await audio.ingest({ ...owner, messageRef: 'a', index: 0 }, async () => m4a())).kind, 'voice');
  assert.equal((await audio.ingest({ ...owner, messageRef: 'a', index: 1 }, async () => mkv())).kind, 'voice');
  const cover = createMediaLibrary({ root: root(), ffprobe: fakeProbe(dir, '12.0', [COVER]) });
  assert.equal((await cover.ingest({ ...owner, messageRef: 'c', index: 0 }, async () => mp4Video())).kind, 'voice');
});

test('library: a refusal is the attachment\'s answer for good - a replay (an hour later, after a restart) answers it again without open; unavailable is not kept', async () => {
  let clock = Date.parse('2026-11-22T10:00:00Z');
  const r = root();
  const limits = { ...MEDIA_LIMITS, inbound_per_channel_hour: 1 };
  const lib = createMediaLibrary({ root: r, limits, now: () => clock });
  let opened = 0;
  const open = async () => { opened += 1; return png(10, 10 + opened); };
  assert.ok((await lib.ingest({ ...owner, messageRef: 'a', index: 0 }, open)).ref);
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'b', index: 0 }, open), { refused: 'too_large' }, 'over the hourly limit');
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'b', index: 1 }, async () => zip()), { refused: 'too_large' }, 'over the hourly limit');
  clock += 3601 * 1000;
  const before = opened;
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'b', index: 0 }, open), { refused: 'too_large', duplicate: true },
    'the hour has passed, and the replay still gets the first answer');
  assert.equal(opened, before, 'open() was not called');
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 't', index: 0 }, async () => zip()), { refused: 'type' });
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'u', index: 0 }, async () => { throw new Error('ECONNRESET'); }), { refused: 'unavailable' });
  // A restart reads the refusals back with the rows; a transient failure was never kept.
  const again = createMediaLibrary({ root: r, limits, now: () => clock });
  assert.equal(again.rows.length, 1, 'refusals are not rows');
  assert.deepEqual(await again.ingest({ ...owner, messageRef: 't', index: 0 }, async () => { throw new Error('not called'); }), { refused: 'type', duplicate: true });
  assert.deepEqual(await again.ingest({ ...owner, messageRef: 'b', index: 0 }, async () => { throw new Error('not called'); }), { refused: 'too_large', duplicate: true });
  assert.ok((await again.ingest({ ...owner, messageRef: 'u', index: 0 }, open)).ref, 'unavailable is asked again');
  // Kept as long as the files are (retention), then gone with the index's other old lines.
  clock += (MEDIA_LIMITS.retention_days + 1) * 24 * 3600 * 1000;
  again.cleanup();
  assert.ok(!readFileSync(again.indexFile, 'utf8').includes('"refused"'));
});

test('library: a source that stalls is cut by its deadline (unavailable, transient), and the ingests behind it still complete', async () => {
  const lib = createMediaLibrary({ root: root(), deadlines: { firstByteMs: 200, totalMs: 400 } });
  let signalled;
  const stalled = async ({ signal }) => { signalled = signal; return (async function* () { yield png(); await new Promise(() => {}); })(); };
  const silent = async () => (async function* () { await new Promise(() => {}); })();
  const hanging = () => new Promise(() => {});
  const started = Date.now();
  const a = lib.ingest({ ...owner, messageRef: 'a', index: 0 }, stalled);
  const b = lib.ingest({ channel: 'slack', audience: 'owner', source: 'in', messageRef: 'b', index: 0 }, async () => png(7, 7));
  const c = lib.ingest({ ...owner, messageRef: 'c', index: 0 }, silent);
  const d = lib.ingest({ ...owner, messageRef: 'd', index: 0 }, hanging);
  const e = lib.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => png(9, 9));
  assert.deepEqual(await a, { refused: 'unavailable' }, 'the stalled body: the total deadline');
  assert.equal(signalled.aborted, true, 'open() was told to stop');
  assert.ok((await b).ref, 'the next ingest completes');
  assert.deepEqual(await c, { refused: 'unavailable' }, 'no first byte');
  assert.deepEqual(await d, { refused: 'unavailable' }, 'open() itself never answers');
  assert.ok((await e).ref);
  assert.ok(Date.now() - started < 5000, `${Date.now() - started} ms`);
  // A deadline is transient: the replay fetches again.
  assert.ok((await lib.ingest({ ...owner, messageRef: 'a', index: 0 }, async () => png(3, 3))).ref);
  // A caller that gives up stops waiting for the lane, and its turn fetches nothing.
  const slow = createMediaLibrary({ root: root(), deadlines: { firstByteMs: 300, totalMs: 300 } });
  const first = slow.ingest({ ...owner, messageRef: 'x', index: 0 }, silent);
  const controller = new AbortController();
  let fetched = false;
  const queued = slow.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => { fetched = true; return png(); }, { signal: controller.signal });
  controller.abort();
  assert.deepEqual(await queued, { refused: 'unavailable' });
  await first;
  await slow.ingest({ channel: 'generated', audience: 'owner', source: 'generated' }, async () => png(4, 4));
  assert.equal(fetched, false);
});

test('library: a generated file (send_media) is held to the outbound limit, not the inbound per-kind ones', async () => {
  const dir = root();
  const lib = createMediaLibrary({ root: root(), ffprobe: fakeProbe(dir, '130.0') });
  const generated = { channel: 'generated', audience: 'owner', source: 'generated' };
  assert.ok((await lib.ingest(generated, async () => png(100, 100, 22 * MiB))).ref, 'a 22 MiB generated image');
  assert.equal((await lib.ingest(generated, async () => wav())).durationMs, 130000, 'a generated voice over 2 minutes');
  assert.deepEqual(await lib.ingest(generated, async () => pdf(26 * MiB)), { refused: 'too_large' }, 'over 25 MiB');
  // The same bytes inbound are still held to the inbound limits (also when deduplicated).
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'i', index: 0 }, async () => png(100, 100, 22 * MiB)), { refused: 'too_large' });
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'v', index: 0 }, async () => wav()), { refused: 'too_large' });
});

test('library: the audiences are owner, a group and (a director\'s subdirectory, 24.2b) a project; anything else is invalid', async () => {
  const lib = createMediaLibrary({ root: root(), userId: 'd-drv1' });
  const stored = await lib.ingest({ channel: 'director', audience: 'project:p-1', source: 'in' }, async () => png());
  assert.match(stored.ref, REF_PATTERN);
  assert.equal(lib.lookup(stored.ref).audience, 'project:p-1');
  assert.equal(covers({ kind: 'group', channel: 'wechat', group_ref: 'r1' }, lib.lookup(stored.ref)), false, 'a group turn never covers it');
  for (const audience of ['project:', 'group:', 'everyone', '', undefined])
    assert.deepEqual(await lib.ingest({ channel: 'director', audience, source: 'in' }, async () => png()), { refused: 'invalid_media' }, String(audience));
});

test('library: open() refusals pass on (gone, too_large); any other failure is unavailable (transient)', async () => {
  const lib = createMediaLibrary({ root: root() });
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'g', index: 0 }, async () => { throw Object.assign(new Error('410'), { refused: 'gone' }); }), { refused: 'gone' });
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'g', index: 1 }, async () => { throw Object.assign(new Error('413'), { refused: 'too_large' }); }), { refused: 'too_large' });
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'g', index: 2 }, async () => { throw new Error('ECONNRESET'); }), { refused: 'unavailable' });
  assert.deepEqual(await lib.ingest({ ...owner, messageRef: 'g', index: 3 }, async () => (async function* () { yield png(); throw new Error('cut'); })()), { refused: 'unavailable' });
});

test('library: 30 days kept, then the row and (once no row points at it) the file; the total bound evicts oldest first; protected refs stay', async () => {
  let now = Date.parse('2026-10-01T00:00:00Z');
  const r = root();
  const lib = createMediaLibrary({ root: r, now: () => now, limits: { ...MEDIA_LIMITS, total_bytes: 3000 } });
  const old = await lib.ingest({ ...owner, messageRef: 'a', index: 0 }, async () => png(1, 1, 1000));
  now += 24 * 3600 * 1000;
  const kept = await lib.ingest({ ...owner, messageRef: 'b', index: 0 }, async () => png(2, 2, 1000));
  const oldFile = lib.lookup(old.ref).path;
  now = Date.parse('2026-11-01T12:00:00Z');   // 31 days after the first, 30 after the second
  lib.setProtected(() => [kept.ref]);
  const { removed } = lib.cleanup();
  assert.equal(removed, 1);
  assert.equal(lib.lookup(old.ref), undefined); assert.ok(!existsSync(oldFile));
  assert.ok(lib.lookup(kept.ref), 'a protected ref outlives its 30 days');
  // Over the total: the oldest unprotected go first.
  const c = await lib.ingest({ ...owner, messageRef: 'c', index: 0 }, async () => png(3, 3, 1000));
  now += 1000;
  const d = await lib.ingest({ ...owner, messageRef: 'd', index: 0 }, async () => png(4, 4, 1000));
  assert.ok(lib.totalBytes() <= 3000);
  assert.ok(lib.lookup(kept.ref) && lib.lookup(d.ref));
  assert.equal(lib.lookup(c.ref), undefined, 'the oldest unprotected file went');
  // The index file says the same after a restart.
  assert.deepEqual(createMediaLibrary({ root: r }).rows.map(x => x.ref).sort(), [kept.ref, d.ref].sort());
  assert.equal(lib.clear().removed, 2);
  assert.ok(!existsSync(lib.home));
});

test('library: logs carry a mime and a size, never a name or a byte', async () => {
  const lines = [];
  const lib = createMediaLibrary({ root: root(), log: (event, fields) => lines.push([event, fields]) });
  await lib.ingest({ ...owner, messageRef: 'x', index: 0 }, async () => png());
  await lib.ingest({ ...owner, messageRef: 'y', index: 0 }, async () => zip());
  for (const [, fields] of lines) for (const key of Object.keys(fields)) assert.ok(['mime', 'size', 'reason', 'rows', 'error'].includes(key), key);
  assert.deepEqual(lines[0], ['media_stored', { mime: 'image/png', size: 33 }]);
});

test('audience: an owner turn covers every ref, a group turn only its own group\'s, anything unreadable nothing', () => {
  const ownerRow = { audience: 'owner' }, groupRow = { audience: 'group:wechat:r1' };
  assert.ok(covers({ kind: 'owner' }, ownerRow) && covers({ kind: 'owner' }, groupRow));
  assert.ok(covers({ kind: 'group', channel: 'wechat', group_ref: 'r1' }, groupRow));
  assert.ok(!covers({ kind: 'group', channel: 'wechat', group_ref: 'r1' }, ownerRow));
  assert.ok(!covers({ kind: 'group', channel: 'wechat', group_ref: 'r2' }, groupRow));
  assert.ok(!covers({ kind: 'group', unknown: true }, groupRow));
  assert.ok(!covers(undefined, ownerRow));
});

test('placeholders and wake lines', () => {
  assert.equal(placeholder({ kind: 'image', ref: 'm_1' }), '[图片]');
  assert.equal(placeholder({ kind: 'voice', ref: 'm_1', durationMs: 23000 }), '[语音 0:23]');
  assert.equal(placeholder({ kind: 'file', ref: 'm_1' }), '[文件]');
  assert.equal(placeholder({ kind: 'file', refused: 'too_large' }), '[文件 过大未收]');
  assert.equal(placeholder({ kind: 'file', refused: 'type' }), '[文件 类型不收]');
  assert.equal(placeholder({ kind: 'file', refused: 'unsupported' }), '[文件 未收：渠道不支持]');
  assert.equal(placeholder({ kind: 'image', refused: 'gone' }), '[图片 未收：已取不到]');
  const ref = 'm_3f9a000000000000';
  assert.equal(attachmentLine({ kind: 'voice', ref, durationMs: 23000 }), `[语音 ${ref}，0:23]`);
  assert.equal(attachmentLine({ kind: 'image', ref, mime: 'image/jpeg', size: 1258291 }), `[图片 ${ref}，image/jpeg，1.2 MB]`);
  assert.equal(attachmentLine({ kind: 'image', refused: 'too_large' }), '[图片 过大未收]');
});

// --- ownerChannels: storeMedia, attachments, sendFile, with a fake channel ------------------

function channelWorld({ bound = 'wxid_owner', senderBound, owedFile, now, mediaKinds = ['image', 'file'], sendFile = true } = {}) {
  const dir = root();
  const library = createMediaLibrary({ root: join(dir, 'media'), ...(now ? { now } : {}) });
  const media = { ingest: (m, o) => library.ingest(m, o), resolve: async ref => library.lookup(ref), read: ref => library.read(ref),
    setProtected: fn => library.setProtected(fn) };
  const calls = { relay: [], senderBound: [], sent: [], files: [] };
  const sources = new Map();
  const host = {
    relay: async message => { calls.relay.push(message); const id = `msg-${calls.relay.length}`; sources.set(id, { direction: 'in', channel: message.channel, sender_ref: message.senderRef, channel_message_ref: message.messageRef }); return { message_id: id, duplicate: false, approval: null }; },
    bind: async () => ({ bound: false }),
    sourceOf: async id => sources.get(id) ?? null,
    recordDelivery: async () => ({}),
    list: async () => ({ channels: [{ channel: 'wechat', bound: true, sender_ref: bound, revoked_at: null }], default_channel: 'wechat' }),
    codeCheck: async () => ({ store_text: true }),
    senderBound: senderBound ?? (async ({ senderRef }) => { calls.senderBound.push(senderRef); return { bound: senderRef === bound }; }),
  };
  const handle = createOwnerChannels({ host, media, ...(owedFile ? { owedFile } : {}) });
  const unregister = handle.service.register({ name: 'wechat', send: async (text, options) => { calls.sent.push([text, options]); return { ref: 'out' }; },
    status: () => ({ connected: true }),
    ...(sendFile ? { sendFile: async file => { calls.files.push(file); return { ref: 'file-out' }; }, mediaKinds } : {}) });
  handle.ready();
  return { dir, library, media, calls, host, handle, sources, unregister, service: handle.service };
}

test('storeMedia: (1) the binding first - a stranger\'s open() is never called; unreachable is unavailable; (2) a stored key answers without open; (3) then the bytes', async () => {
  const w = channelWorld();
  let opened = 0;
  const open = async () => { opened += 1; return png(); };
  assert.deepEqual(await w.service.storeMedia({ channel: 'wechat', senderRef: 'wxid_stranger', messageRef: 's1', index: 0, kind: 'file' },
    async () => { opened += 1; return pdf(25 * MiB); }), { refused: 'channel_sender_not_bound' });
  assert.equal(opened, 0, 'not one byte of a stranger\'s was fetched');
  assert.deepEqual(w.library.rows, []);
  const stored = await w.service.storeMedia({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'o1', index: 0, kind: 'image' }, open);
  assert.match(stored.ref, REF_PATTERN);
  const replay = await w.service.storeMedia({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'o1', index: 0, kind: 'image' }, open);
  assert.equal(replay.ref, stored.ref); assert.equal(opened, 1, 'a replay does not fetch again');
  const down = channelWorld({ senderBound: async () => { throw new Error('ECONNREFUSED'); } });
  let touched = false;
  assert.deepEqual(await down.service.storeMedia({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'o1', index: 0, kind: 'image' },
    async () => { touched = true; return png(); }), { refused: 'unavailable' });
  assert.equal(touched, false);
  assert.deepEqual(await w.service.storeMedia({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'o1', index: -1, kind: 'image' }, open), { refused: 'invalid_media' });
  assert.deepEqual(await w.service.storeMedia({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'g', index: 0, kind: 'image', conversationRef: 'room' }, open),
    { refused: 'unsupported' }, 'no group media in 24.2');
});

test('storeMedia without a media library (the local host) or without the host\'s binding read is unsupported', async () => {
  const host = { relay: async () => ({}), bind: async () => ({}), sourceOf: async () => null, recordDelivery: async () => ({}), list: async () => ({ channels: [] }) };
  const handle = createOwnerChannels({ host });
  handle.ready();
  assert.deepEqual(await handle.service.storeMedia({ channel: 'wechat', senderRef: 'a', messageRef: 'b', index: 0, kind: 'image' }, async () => png()),
    { refused: 'unsupported' });
});

test('inbound with attachments: the closed set, placeholders written here, the metadata (never a name) to the host', async () => {
  const w = channelWorld();
  const { ref } = await w.service.storeMedia({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'm1', index: 0, kind: 'image' }, async () => png());
  const answer = await w.service.inbound({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'm1', text: '', at: new Date().toISOString(),
    attachments: [{ kind: 'image', ref, name: 'IMG_1.jpg', size: 999999999 }, { kind: 'file', refused: 'type', name: 'a.docm' }] });
  assert.deepEqual(answer, { accepted: true });
  const relayed = w.calls.relay.at(-1);
  assert.equal(relayed.text, '[图片]\n[文件 类型不收]', 'the placeholder body for a message that is only attachments');
  assert.deepEqual(relayed.attachments, [{ kind: 'image', ref, mime: 'image/png', size: 33 }, { kind: 'file', refused: 'type' }],
    'the library\'s own size, not the channel\'s; no name');
  // With text, the lines follow it.
  const { ref: r2 } = await w.service.storeMedia({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'm2', index: 0, kind: 'image' }, async () => png(5, 5));
  await w.service.inbound({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'm2', text: '看这个', attachments: [{ kind: 'image', ref: r2 }] });
  assert.equal(w.calls.relay.at(-1).text, '看这个\n[图片]');
  // Without attachments a message is relayed exactly as before (no attachments key).
  await w.service.inbound({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'm3', text: '你好' });
  assert.ok(!('attachments' in w.calls.relay.at(-1)));
});

test('inbound: anything outside the closed set refuses the message and the host is not asked', async () => {
  const w = channelWorld();
  const { ref } = await w.service.storeMedia({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'm1', index: 0, kind: 'image' }, async () => png());
  const bad = [
    [{ kind: 'image', ref, refused: 'type' }], [{ kind: 'image' }], [{ kind: 'image', refused: 'channel_sender_not_bound' }],
    [{ kind: 'image', ref: 'media-123' }], [{ kind: 'image', ref: 'm_' + 'a'.repeat(16) }], [{ kind: 'video', ref }],
    'not-a-list', Array(11).fill({ kind: 'image', refused: 'gone' }),
  ];
  for (const attachments of bad) {
    const answer = await w.service.inbound({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'x', text: '', attachments });
    assert.deepEqual(answer, { accepted: false, reason: 'invalid_inbound' }, JSON.stringify(attachments));
  }
  assert.equal(w.calls.relay.length, 0);
});

test('inbound: a group message takes only its own group\'s refs - an owner\'s file never becomes a group attachment', async () => {
  const w = channelWorld();
  const { ref: ownerRef } = await w.service.storeMedia({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'm1', index: 0, kind: 'image' }, async () => png());
  const { ref: otherGroup } = await w.library.ingest({ channel: 'wechat', audience: 'group:wechat:room-2', source: 'in' }, async () => png(6, 6));
  const { ref: ownGroup } = await w.library.ingest({ channel: 'wechat', audience: 'group:wechat:room-1', source: 'in' }, async () => png(8, 8));
  const group = attachments => w.service.inbound({ channel: 'wechat', senderRef: 'wxid_member', messageRef: 'g1', text: '',
    conversationRef: 'room-1', attachments });
  assert.deepEqual(await group([{ kind: 'image', ref: ownerRef }]), { accepted: false, reason: 'invalid_inbound' });
  assert.deepEqual(await group([{ kind: 'image', ref: otherGroup }]), { accepted: false, reason: 'invalid_inbound' });
  // Its own group's ref, and a refused item, pass the check (this world has no groups service).
  assert.deepEqual(await group([{ kind: 'image', ref: ownGroup }]), { accepted: false, reason: 'no_groups' });
  assert.deepEqual(await group([{ kind: 'image', refused: 'unsupported' }]), { accepted: false, reason: 'no_groups' });
  assert.equal(w.calls.relay.length, 0);
});

test('inbound: a held message keeps its refs in the owed store, and the library does not clean them', async () => {
  const dir = root();
  const owedFile = join(dir, 'owed.json');
  const clock = { now: Date.now() };
  const w = channelWorld({ owedFile, now: () => clock.now });
  w.host.relay = async () => { throw new Error('backend down'); };
  const { ref } = await w.service.storeMedia({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'h1', index: 0, kind: 'image' }, async () => png());
  const answer = await w.service.inbound({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'h1', text: '', attachments: [{ kind: 'image', ref }] });
  assert.deepEqual(answer, { accepted: false, reason: 'unavailable' });
  const owed = Object.values(JSON.parse(readFileSync(owedFile, 'utf8')));
  assert.deepEqual(owed[0].attachments, [{ kind: 'image', ref, mime: 'image/png', size: 33 }]);
  // Even far past 30 days, the owed ref is kept: ownerChannels protects what it still owes.
  clock.now += 40 * 24 * 3600 * 1000;
  assert.equal(w.library.cleanup().removed, 0);
  assert.ok(w.library.lookup(ref));
  // Once the relay commits, the record goes and the ref is an ordinary one again.
  w.host.relay = async () => ({ message_id: 'm-late', duplicate: false, approval: null });
  await w.handle.pollOwed();
  assert.equal(w.library.cleanup().removed, 1);
});

test('register: sendFile must be a function and mediaKinds a subset of image, voice, file', () => {
  const handle = createOwnerChannels({ host: { relay() {}, bind() {}, sourceOf() {}, recordDelivery() {}, list() {} } });
  const base = { name: 'slack', send: async () => ({}), status: () => ({ connected: true }) };
  assert.throws(() => handle.service.register({ ...base, sendFile: 'no' }), /sendFile, when given, must be a function/);
  assert.throws(() => handle.service.register({ ...base, sendFile: async () => ({}), mediaKinds: ['video'] }), /mediaKinds/);
  assert.doesNotThrow(() => handle.service.register({ ...base, sendFile: async () => ({}), mediaKinds: ['image'] }));
  assert.equal(handle.canSendFile('slack', 'image'), true);
  assert.equal(handle.canSendFile('slack', 'voice'), false);
});

test('sendFile target: a dashboard message is dashboard_no_media; a channel without the kind is channel_no_media; else the bound account', async () => {
  const w = channelWorld();
  assert.deepEqual(await w.handle.ownerMediaTarget('dash-msg'), { refused: 'dashboard_no_media' });
  await w.service.inbound({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'q1', text: '画一只猫' });
  assert.deepEqual(await w.handle.ownerMediaTarget('msg-1'), { channel: 'wechat', to: 'wxid_owner', replyToRef: 'q1' });
  assert.deepEqual(await w.handle.sendFile('wechat', { kind: 'voice', mime: 'audio/wav', name: 'x.wav', path: '/x' }), { refused: 'channel_no_media' });
  w.unregister();
  assert.deepEqual(await w.handle.ownerMediaTarget('msg-1'), { refused: 'channel_not_connected' });
});

// --- media_call and send_media ----------------------------------------------------------------

const OCR = 'mcp__sw__media__ocr_image', DRAW = 'mcp__sw__media__generate_image', SEND = 'mcp__sw__mailpub__send_message';

function toolWorld({ audience = { kind: 'owner' }, requester = 'msg-1', channels, mediaKinds, sendFile } = {}) {
  const w = channelWorld({ ...(mediaKinds ? { mediaKinds } : {}), ...(sendFile === false ? { sendFile } : {}) });
  const turn = { audience, turn: 't1' };
  const executed = [], fetched = [], posted = [], checked = [];
  let out;
  const ledger = new UrlLedger();
  const catalog = { rootSession: 'root', requester,
    catalog: () => new Set([OCR, DRAW, SEND]), outward: name => name === SEND };
  const media = { ...w.media,
    fetch: async request => { fetched.push(request); return w.library.ingest({ channel: 'generated', audience: request.audience, source: 'generated' }, async () => png(64, 64)); },
    postGroup: async request => {
      if (request.checkOnly) { checked.push(request); return out?.postAnswer?.refused ? out.postAnswer : { may_post: true }; }
      posted.push(request); return out?.postAnswer ?? { posted: true, message_id: 'gm1', channel: 'wechat', group_ref: 'r1', ...(request.caption ? { caption: request.caption } : {}) }; } };
  const tools = new Map();
  const ctx = { tools: { execute: async request => { executed.push(request); return { value: { text: 'OCR: 你好' }, content: [{ type: 'text', text: 'OCR: 你好' }] }; } } };
  class HarnessError extends Error {}
  registerMediaTools({ tools: { register: t => tools.set(t.name, t) } }, { ctx, defineTool: t => t, jsonOutput: {}, HarnessError,
    media, catalog, turns: { audience: () => turn.audience, open: () => turn }, channels: channels ?? w.handle, ledger });
  const run = (name, args, session = 'root') => tools.get(name).execute(args, { agent: { session: { id: session } }, callId: 'c1', rootCallId: 'r1', token: 'tok' });
  out = { ...w, media, turn, executed, fetched, posted, checked, ledger, tools, run };
  return out;
}

test('media_call: the host fills the base64 in, the call goes through ctx.tools.execute as a nested call, and the base64 never comes back', async () => {
  const t = toolWorld();
  const { ref } = await t.library.ingest({ ...owner, messageRef: 'p', index: 0 }, async () => png());
  const args = { name: OCR, arguments: { lang: 'zh' }, media: { image_base64: ref } };
  const answer = await t.run('media_call', args);
  assert.deepEqual(answer, { text: 'OCR: 你好' });
  const call = t.executed[0];
  assert.equal(call.name, OCR); assert.equal(call.parent, 'tok', 'nested: the guard does not count it again');
  assert.equal(call.arguments.lang, 'zh');
  assert.equal(call.arguments.image_base64, png().toString('base64'));
  assert.deepEqual(args.media, { image_base64: ref }, 'the model\'s own arguments carry the ref only');
  assert.ok(!JSON.stringify(answer).includes(png().toString('base64').slice(0, 20)));
  assert.deepEqual(t.tools.get('media_call').projectContent({ token: 'tok' }), [{ type: 'text', text: 'OCR: 你好' }]);
});

test('media_call refusals: not this turn\'s tool, an outward tool, an unknown ref, a group turn using an owner ref, another session', async () => {
  const t = toolWorld({ audience: { kind: 'group', channel: 'wechat', group_ref: 'r1' } });
  const mine = await t.library.ingest({ ...owner, messageRef: 'p', index: 0 }, async () => png());
  const theirs = await t.library.ingest({ channel: 'generated', audience: 'group:wechat:r1', source: 'generated' }, async () => png(9, 9));
  assert.equal((await t.run('media_call', { name: 'mcp__sw__mailme__read', media: { image_base64: theirs.ref } })).refused, 'not_visible_in_this_turn');
  assert.equal((await t.run('media_call', { name: SEND, media: { attachment: theirs.ref } })).refused, 'outward_media_not_supported');
  assert.equal((await t.run('media_call', { name: OCR, media: { image_base64: 'm_' + '0'.repeat(16) } })).refused, 'unknown_ref');
  assert.equal((await t.run('media_call', { name: OCR, media: { image_base64: mine.ref } })).refused, 'not_visible_in_this_turn', 'a group turn cannot use the owner\'s ref');
  assert.equal((await t.run('media_call', { name: OCR, media: {} })).refused, 'invalid_media');
  assert.equal((await t.run('media_call', { name: OCR, media: { image_base64: theirs.ref } }, 'child')).refused, 'not_available');
  assert.equal(t.executed.length, 0);
  assert.deepEqual(await t.run('media_call', { name: OCR, media: { image_base64: theirs.ref } }), { text: 'OCR: 你好' }, 'its own group\'s ref');
});

test('send_media(url): only a url a connection tool returned in this turn, fetched through that connection, sent to this turn\'s requester', async () => {
  const t = toolWorld();
  await t.service.inbound({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'q1', text: '画一只猫' });
  const url = 'http://mcp_server:9003/files/cat.png?sig=1&exp=2';
  assert.equal((await t.run('send_media', { url })).refused, 'url_not_seen');
  assert.equal(t.fetched.length, 0);
  t.ledger.record(DRAW, { value: { url }, content: [{ type: 'text', text: `done: ${url}\n` }] });
  t.ledger.record('search_tools', { value: { url: 'http://elsewhere/x.png' } });
  assert.equal(t.ledger.lookup('http://elsewhere/x.png'), undefined, 'only connection tools\' results count');
  const sent = await t.run('send_media', { url, caption: '一只猫' });
  assert.equal(sent.sent, true);
  assert.deepEqual(t.fetched, [{ connId: 'media', url, audience: 'owner' }]);
  const file = t.calls.files.at(-1);
  assert.equal(file.to, 'wxid_owner'); assert.equal(file.replyToRef, 'q1'); assert.equal(file.kind, 'image');
  assert.equal(file.caption, '一只猫');
  assert.deepEqual(readFileSync(file.path), png(64, 64));
  assert.equal(t.library.rows.at(-1).source, 'generated');
  t.ledger.reset();
  assert.equal((await t.run('send_media', { url })).refused, 'url_not_seen', 'a new turn forgets the last turn\'s urls');
});

test('send_media(url) in an owner turn: a channel that cannot send the file is refused before anything is fetched or stored', async () => {
  // Review F3: the owner's channel is judged with the kind before the fetch - no sendFile or no
  // kinds at all refuses any url; a kind its name says (cat.png) that the channel lacks refuses it.
  const url = 'http://mcp_server:9003/files/cat.png?sig=1', unnamed = 'http://mcp_server:9003/files/7f3a?sig=1';
  for (const shape of [{ sendFile: false }, { mediaKinds: [] }, { mediaKinds: ['file'] }]) {
    const t = toolWorld(shape);
    await t.service.inbound({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'q1', text: '画一只猫' });
    t.ledger.record(DRAW, { value: { url, unnamed } });
    assert.deepEqual(await t.run('send_media', { url }), { refused: 'channel_no_media' }, JSON.stringify(shape));
    if (!shape.mediaKinds?.length) assert.deepEqual(await t.run('send_media', { url: unnamed }), { refused: 'channel_no_media' });
    assert.equal(t.fetched.length, 0, `nothing fetched: ${JSON.stringify(shape)}`);
    assert.equal(t.library.rows.length, 0, 'nothing stored');
    assert.equal(t.calls.files.length, 0);
  }
  // A url whose name says nothing, on a file-only channel: its kind is known once fetched -
  // fetched and stored, then refused without sending.
  const t = toolWorld({ mediaKinds: ['file'] });
  await t.service.inbound({ channel: 'wechat', senderRef: 'wxid_owner', messageRef: 'q1', text: '画一只猫' });
  t.ledger.record(DRAW, { value: { unnamed } });
  assert.deepEqual(await t.run('send_media', { url: unnamed }), { refused: 'channel_no_media' });
  assert.equal(t.fetched.length, 1);
  assert.equal(t.calls.files.length, 0, 'not sent');
  assert.deepEqual(await t.handle.ownerMediaTarget('msg-1', 'image'), { refused: 'channel_no_media' }, 'the kind branch is live');
});

test('send_media: a dashboard turn is dashboard_no_media; no requester is no_requester; exactly one of ref and url', async () => {
  const t = toolWorld({ requester: 'dash-msg' });
  const { ref } = await t.library.ingest({ ...owner, messageRef: 'p', index: 0 }, async () => png());
  assert.equal((await t.run('send_media', { ref })).refused, 'dashboard_no_media');
  assert.equal((await t.run('send_media', { ref, url: 'http://x/y' })).refused, 'invalid_request');
  assert.equal((await t.run('send_media', {})).refused, 'invalid_request');
  const none = toolWorld({ requester: null });
  assert.equal((await none.run('send_media', { ref })).refused, 'no_requester');
  assert.equal(t.calls.files.length + none.calls.files.length, 0);
});

test('send_media in a group turn: only this group, through the backend\'s budget, and never the owner\'s ref', async () => {
  const t = toolWorld({ audience: { kind: 'group', channel: 'wechat', group_ref: 'r1' } });
  const mine = await t.library.ingest({ ...owner, messageRef: 'p', index: 0 }, async () => png());
  assert.equal((await t.run('send_media', { ref: mine.ref })).refused, 'not_visible_in_this_turn');
  const theirs = await t.library.ingest({ channel: 'generated', audience: 'group:wechat:r1', source: 'generated' }, async () => png(7, 7));
  const sent = await t.run('send_media', { ref: theirs.ref, caption: '猫' });
  assert.equal(sent.sent, true);
  assert.deepEqual(t.posted, [{ turn: 't1', kind: 'image', caption: '猫' }]);
  const file = t.calls.files.at(-1);
  assert.equal(file.groupRef, 'r1'); assert.equal(file.to, undefined); assert.equal(file.caption, '猫');
  t.postAnswer = { refused: 'group_send_budget' };
  assert.deepEqual(await t.run('send_media', { ref: theirs.ref }), { refused: 'group_send_budget' });
  assert.equal(t.calls.files.length, 1, 'nothing goes out over the budget');
  // A kind the group's channel cannot send is refused before the budget is touched.
  const voice = createMediaLibrary({ root: root(), ffprobe: fakeProbe(root(), '3') });
  const v = await voice.ingest({ channel: 'generated', audience: 'group:wechat:r1', source: 'generated' }, async () => wav());
  t.media.resolve = async ref => voice.lookup(ref);
  const before = t.posted.length;
  const refused = await t.run('send_media', { ref: v.ref });
  assert.equal(refused.refused, 'channel_no_media');
  assert.equal(t.posted.length, before);
});

test('send_media(url) in a group turn: a kind the channel cannot send and an exhausted budget refuse before anything is fetched or stored', async () => {
  // Review F2: the turn is judged first - the channel's kinds (a url's guessed from its name),
  // then the backend's budget and caption check as a dry run - and only then is the url fetched.
  const group = { kind: 'group', channel: 'wechat', group_ref: 'r1' };
  const url = 'http://mcp_server:9003/files/cat.png?sig=1';
  const fileOnly = toolWorld({ audience: group, mediaKinds: ['file'] });
  fileOnly.ledger.record(DRAW, { value: { url } });
  assert.deepEqual(await fileOnly.run('send_media', { url }), { refused: 'channel_no_media' });
  assert.equal(fileOnly.fetched.length, 0, 'nothing fetched');
  assert.equal(fileOnly.library.rows.length, 0, 'nothing stored');
  assert.equal(fileOnly.checked.length + fileOnly.posted.length, 0, 'the backend is not asked either');
  const spent = toolWorld({ audience: group });
  spent.ledger.record(DRAW, { value: { url } });
  spent.postAnswer = { refused: 'group_send_budget' };
  assert.deepEqual(await spent.run('send_media', { url, caption: '猫' }), { refused: 'group_send_budget' });
  assert.equal(spent.fetched.length, 0, 'nothing fetched');
  assert.equal(spent.library.rows.length, 0, 'nothing stored');
  assert.deepEqual(spent.checked, [{ turn: 't1', kind: 'image', caption: '猫', checkOnly: true }]);
  assert.equal(spent.posted.length, 0, 'the dry run is all the backend was asked');
  // Allowed: checked, fetched, then posted for real (the authoritative record) and sent.
  spent.postAnswer = undefined;
  assert.equal((await spent.run('send_media', { url })).sent, true);
  assert.equal(spent.fetched.length, 1);
  assert.deepEqual(spent.posted, [{ turn: 't1', kind: 'image', caption: undefined }]);
});

test('GROUP_TOOLS holds media_call and send_media (7.8.8)', () => {
  assert.ok(GROUP_TOOLS.has('media_call') && GROUP_TOOLS.has('send_media'));
});

test('a fake channel end to end: storeMedia for each file, then inbound with the refs; the steward\'s file goes back through sendFile', async () => {
  // A channel plugin as 24.2 wants one (WeChat's, once its bridge can fetch media): it reports
  // a message with two files, stores each BEFORE inbound, writes `refused` itself for what it
  // knows it cannot take, and registers sendFile for what it can send.
  const t = toolWorld();
  const incoming = { from: 'wxid_owner', ref: 'wx-77', text: '', files: [
    { kind: 'image', bytes: jpeg(800, 600) }, { kind: 'file', declaredSize: 30 * MiB } ] };
  const attachments = [];
  for (const [index, f] of incoming.files.entries()) {
    if (f.declaredSize > 25 * MiB) { attachments.push({ kind: f.kind, refused: 'too_large' }); continue; }
    const stored = await t.service.storeMedia({ channel: 'wechat', senderRef: incoming.from, messageRef: incoming.ref, index, kind: f.kind },
      async () => f.bytes);
    attachments.push(stored.ref ? { kind: f.kind, ref: stored.ref } : { kind: f.kind, refused: stored.refused });
  }
  assert.deepEqual(await t.service.inbound({ channel: 'wechat', senderRef: incoming.from, messageRef: incoming.ref, text: incoming.text, attachments }),
    { accepted: true });
  const relayed = t.calls.relay.at(-1);
  assert.equal(relayed.text, '[图片]\n[文件 过大未收]');
  assert.equal(relayed.attachments[0].mime, 'image/jpeg');
  // The steward reads the photo and sends a generated picture back into this chat.
  const ref = relayed.attachments[0].ref;
  assert.deepEqual(await t.run('media_call', { name: OCR, media: { image_base64: ref } }), { text: 'OCR: 你好' });
  assert.equal(t.executed[0].arguments.image_base64, jpeg(800, 600).toString('base64'));
  const sent = await t.run('send_media', { ref });
  assert.equal(sent.sent, true);
  assert.deepEqual(readFileSync(t.calls.files.at(-1).path), jpeg(800, 600));
  assert.equal(t.calls.files.at(-1).replyToRef, 'wx-77');
});
