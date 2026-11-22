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
const fakeProbe = (dir, seconds) => {
  const path = join(dir, `ffprobe-${String(seconds).replace(/[^0-9a-z.]/gi, '_')}`);
  writeFileSync(path, `#!/bin/sh\necho ${seconds}\n`); chmodSync(path, 0o755);
  return path;
};
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

function channelWorld({ bound = 'wxid_owner', senderBound, owedFile, now } = {}) {
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
    status: () => ({ connected: true }), sendFile: async file => { calls.files.push(file); return { ref: 'file-out' }; }, mediaKinds: ['image', 'file'] });
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

function toolWorld({ audience = { kind: 'owner' }, requester = 'msg-1', channels } = {}) {
  const w = channelWorld();
  const turn = { audience, turn: 't1' };
  const executed = [], fetched = [], posted = [];
  let out;
  const ledger = new UrlLedger();
  const catalog = { rootSession: 'root', requester,
    catalog: () => new Set([OCR, DRAW, SEND]), outward: name => name === SEND };
  const media = { ...w.media,
    fetch: async request => { fetched.push(request); return w.library.ingest({ channel: 'generated', audience: request.audience, source: 'generated' }, async () => png(64, 64)); },
    postGroup: async request => { posted.push(request); return out?.postAnswer ?? { posted: true, message_id: 'gm1', channel: 'wechat', group_ref: 'r1', ...(request.caption ? { caption: request.caption } : {}) }; } };
  const tools = new Map();
  const ctx = { tools: { execute: async request => { executed.push(request); return { value: { text: 'OCR: 你好' }, content: [{ type: 'text', text: 'OCR: 你好' }] }; } } };
  class HarnessError extends Error {}
  registerMediaTools({ tools: { register: t => tools.set(t.name, t) } }, { ctx, defineTool: t => t, jsonOutput: {}, HarnessError,
    media, catalog, turns: { audience: () => turn.audience, open: () => turn }, channels: channels ?? w.handle, ledger });
  const run = (name, args, session = 'root') => tools.get(name).execute(args, { agent: { session: { id: session } }, callId: 'c1', rootCallId: 'r1', token: 'tok' });
  out = { ...w, media, turn, executed, fetched, posted, ledger, tools, run };
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
