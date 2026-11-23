/** Group @-turn wake (2026-11-23, iss-0d20ef5dd9a44cd0): the group's kept images were stored
 *  and named by ref, but the wake was one text block and the steward has no image-capable
 *  connection to read them with media_call. Here a fake channel hands a group message to the
 *  REAL setGroupWake handler registerGroupWake installs (index.mjs), with a fake host that can
 *  attach images from its own library, and the steered user message's blocks are the assertion.
 *  No DSH, no backend. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createMediaLibrary } from '../lib/media.mjs';
import { createOwnerChannels, GROUP_WAKE_IMAGE_LIMIT } from '../lib/channels.mjs';
import { registerGroupWake } from '../index.mjs';

const png = () => {
  const head = Buffer.alloc(33);
  Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex').copy(head, 0);
  head.writeUInt32BE(640, 16); head.writeUInt32BE(480, 20);
  return head;
};
const at = n => `2026-11-23T10:0${n}:00+00:00`;

/** One wake world: a real director library behind a fake host hook, and a real ownerChannels
 *  handle whose group inbound path calls the plugin's setGroupWake callback. `attach: false` is
 *  a hook on a text-only route (it renders lines but attaches nothing); `hook: false` is a host
 *  that provides no hook at all - both must keep the wake's single text block. */
function world({ recent = [], attach = true, hook = true } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'group-wake-images-'));
  const library = createMediaLibrary({ root: dir, userId: 'owner' });
  const saved = [], steered = [], recentReads = [];
  const groups = {
    list: async () => ({ owner_refs: { wechat: 'wxid_owner' },
      groups: [{ channel: 'wechat', group_ref: 'r1', label: '老家群', mention_prefix: '@管家', media_keep: 'on' }] }),
    relay: async () => ({ message_id: 'g1', sender_role: 'member', sender_label: '成员甲' }),
    bind: async () => ({ bound: true }), reply: async () => ({}),
    recent: async a => { recentReads.push(a); return { recent: a.groupRef === 'r1' ? recent : [] }; },
    audience: async () => ({}), ownerSpokeSince: async () => false,
    familyNote: async () => ({}), familyShow: async () => ({}),
  };
  const host = {
    groups,
    channels: {
      relay: async () => ({ message_id: 'x' }), bind: async () => ({ bound: true }),
      sourceOf: async () => null, recordDelivery: async () => ({}),
      list: async () => ({ channels: [], default_channel: 'wechat' }), codeCheck: async () => ({ store_text: true }),
    },
    ...(hook ? { wakeMedia: async () => {
      const images = [];
      return { images, line: async (item, { attach: mayAttach = true } = {}) => {
        const row = library.lookup(item?.ref);
        const name = { image: '图片', voice: '语音' }[item?.kind] ?? '文件';
        const meta = `${item?.mime ?? ''}${Number.isInteger(item?.size) ? '，' + item.size + ' B' : ''}`;
        const hint = `[${name} ${item?.ref}${meta ? '，' + meta : ''}；本路线不收图：用 media_call 调 ocr_image 或 vision_critique]`;
        if (!row || !mayAttach || !attach || item.kind !== 'image') return hint;
        const { bytes } = await library.read(item.ref);
        saved.push({ ref: item.ref, bytes });
        images.push({ attachmentId: `att-${images.length + 1}`, mediaType: row.mime });
        return `[图片 ${item.ref}${meta ? '，' + meta : ''}，原图已附]`;
      } };
    } } : {}),
  };
  const channels = createOwnerChannels({ host: host.channels, groups,
    media: { resolve: async ref => library.lookup(ref) }, owedFile: join(dir, 'owed.json') });
  channels.service.register({ name: 'wechat', send: async () => ({ ref: 'out' }),
    status: () => ({ connected: true }), sendGroup: async () => ({ ref: 'g' }) });
  const ctx = { logger: { info() {}, warn() {} }, agents: { withoutInitiator: fn => fn() },
    sessionPersistence: { flush: async () => {} } };
  let n = 0;
  registerGroupWake({ ctx, channels, host, groupTurns: true, agentExists: Promise.resolve(),
    getAgent: () => ({ steer: message => steered.push(message) }),
    createUserMessage: input => ({ ...input, id: 'msg-' + ++n, role: 'user' }) });
  channels.ready();
  const store = async (audience, messageRef) => (await library.ingest(
    { channel: 'wechat', messageRef, index: 0, audience, source: 'in' }, async () => png())).ref;
  const wake = (extra = {}) => channels.service.inbound({ channel: 'wechat', senderRef: 'wxid_m',
    messageRef: 'w' + Math.random(), text: '@管家 看看这几张', at: at(0), conversationRef: 'r1', ...extra });
  const steeredWake = () => { assert.equal(steered.length, 1); return steered[0]; };
  return { library, saved, steered, recentReads, store, wake, steeredWake, stop: () => channels.stop() };
}
const keptEntry = (ref, n) => ({ at: at(n), sender: '妈妈', kept: true,
  attachments: [{ kind: 'image', ref, mime: 'image/png', size: 33 }] });
const lines = message => JSON.parse(message.content[0].text).group_turn;

test('c1: a group @-turn attaches its own kept images and the message\'s own image; other rooms\' refs never attach', async t => {
  const recent = [];
  const w = world({ recent });
  t.after(w.stop);
  const k1 = await w.store('group:wechat:r1', 'k1');
  const k2 = await w.store('group:wechat:r1', 'k2');
  const mine = await w.store('group:wechat:r1', 'mine');
  // Held by this host, but not in this group's wake: an owner's file and another room's kept file.
  const ownerRef = await w.store('owner', 'owner');
  const otherRef = await w.store('group:wechat:r2', 'other');
  recent.push(keptEntry(k1, 1), keptEntry(k2, 2));
  w.recentReads.length = 0;
  await w.wake({ attachments: [{ kind: 'image', ref: mine, mime: 'image/png', size: 33 }] });
  const message = w.steeredWake();
  assert.deepEqual(w.recentReads, [{ channel: 'wechat', groupRef: 'r1' }], 'only this wake\'s room is read');
  assert.equal(message.content[0].type, 'text');
  assert.deepEqual(message.content.slice(1).map(block => block.type), ['image', 'image', 'image']);
  const shown = lines(message);
  assert.equal(shown.group, '老家群');
  const keptLines = shown.recent.filter(e => e.kept).flatMap(e => e.attachments);
  assert.equal(keptLines.length, 2);
  assert.ok(keptLines.some(line => line.includes(k1) && line.includes('原图已附')));
  assert.ok(keptLines.some(line => line.includes(k2) && line.includes('原图已附')));
  assert.match(shown.messages[0].attachments[0], /原图已附/, 'the @-ed image is attached too');
  assert.deepEqual(w.saved.map(item => item.ref).sort(), [k1, k2, mine].sort());
  assert.ok(!w.saved.some(item => item.ref === ownerRef || item.ref === otherRef));
  assert.ok(!message.content[0].text.includes(ownerRef) && !message.content[0].text.includes(otherRef));
  // The bridge would not even hand those refs to this room: both are refused before a wake.
  for (const ref of [ownerRef, otherRef]) {
    assert.deepEqual(await w.wake({ attachments: [{ kind: 'image', ref, mime: 'image/png', size: 33 }] }),
      { accepted: false, reason: 'invalid_inbound' });
  }
  assert.equal(w.steered.length, 1, 'a refused ref produces no extra wake');
});

test('c1: a host that attaches nothing keeps one text block and the media_call lines; no hook is today\'s wake', async t => {
  const pair = { at: at(1), sender: '爸爸', q: '问一句', a: '答一句' };
  const makeWorld = options => {
    const w = world(options);
    t.after(w.stop);
    return w;
  };
  const blindRecent = [];
  const blind = makeWorld({ attach: false, recent: blindRecent });
  const blindCurrent = await blind.store('group:wechat:r1', 'blind-current');
  blindRecent.push(pair, keptEntry('m_1111111111111111', 2));
  await blind.wake({ attachments: [{ kind: 'image', ref: blindCurrent, mime: 'image/png', size: 33 }] });
  const blindMessage = blind.steeredWake();
  assert.deepEqual(blindMessage.content.map(block => block.type), ['text']);
  const blindShown = lines(blindMessage);
  assert.deepEqual(blindShown.recent[0], pair, 'an entry without attachments is untouched');
  for (const line of [...blindShown.recent.flatMap(e => e.attachments ?? []), ...blindShown.messages[0].attachments])
    assert.match(line, /media_call/);
  assert.deepEqual(blind.saved, []);

  // Without the hook the renderer is attachmentLine: no hint, no block, byte-for-byte today.
  const plain = makeWorld({ hook: false, recent: [keptEntry('m_3333333333333333', 3)] });
  await plain.wake();
  const plainMessage = plain.steeredWake();
  assert.deepEqual(plainMessage.content.map(block => block.type), ['text']);
  const plainLine = lines(plainMessage).recent[0].attachments[0];
  assert.deepEqual(plainLine, '[图片 m_3333333333333333，image/png，33 B]');
});

test('c2: bound+1 kept images attach only the newest `bound`, the rest keep their hint line', async t => {
  assert.equal(GROUP_WAKE_IMAGE_LIMIT, 4);
  const recent = [];
  const w = world({ recent });
  t.after(w.stop);
  for (let i = 0; i <= GROUP_WAKE_IMAGE_LIMIT; i++) {
    const ref = await w.store('group:wechat:r1', 'k' + i);
    // Backend recent is oldest first; the last entry is the newest.
    recent.push(keptEntry(ref, i + 1));
  }
  await w.wake();
  const message = w.steeredWake();
  assert.equal(message.content[0].type, 'text');
  const blocks = message.content.slice(1);
  assert.equal(blocks.length, GROUP_WAKE_IMAGE_LIMIT);
  assert.ok(blocks.every(block => block.type === 'image'));
  assert.deepEqual(w.saved.map(item => item.ref).sort(), recent.slice(1).map(e => e.attachments[0].ref).sort(), 'the oldest of bound+1 is left out');
  const shownLines = lines(message).recent.map(entry => entry.attachments[0]);
  assert.ok(shownLines[0].includes(recent[0].attachments[0].ref) && shownLines[0].includes('media_call') && !shownLines[0].includes('原图已附'));
  for (const line of shownLines.slice(1)) assert.match(line, /原图已附/);
});
