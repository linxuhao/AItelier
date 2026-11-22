/** Step 24.2 (design-steward 7.8.12, 7.8.17 "适配器"): the AItelier steward host's media side -
 *  the `media` capability exists only with a media root (AITELIER_MEDIA_ROOT, given by the DSH
 *  host), the gateway fetch stores what /fetch answered as `generated` under the turn's
 *  audience, the group's send_media record, the relays carry attachment metadata only when
 *  there is some, storeMedia's binding read, and a wake shows attachments one line each. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { aitelierHost } from './runtime.mjs';
import { channelOps, groupOps, mediaOps, mediaRootFrom, stateWake, withAttachmentLines } from './steward-host.mjs';
import { createMediaLibrary } from '../dsh-director/lib/media.mjs';
import { validateHost } from '../dsh-director/lib/host.mjs';

const env = (extra = {}) => ({ AITELIER_DSH_PROJECT: 'aitelier-home', AITELIER_DSH_SESSION: 's-1', AITELIER_DSH_PROVIDER: 'aitelier',
  AITELIER_DSH_MODEL: 'm', AITELIER_DSH_STATE_DIR: '/tmp/x', AITELIER_DSH_ENTRY: '/tmp/dsh.mjs', AITELIER_DRIVER_ID: 'drv-home',
  AITELIER_DSH_EXECUTION: 'host', ...extra });
const PNG = Buffer.concat([Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex'), Buffer.from([0, 0, 0, 64, 0, 0, 0, 64]), Buffer.alloc(40)]);

test('the media capability: a steward with a media root has it (and validates); without a root, or a director, has none', () => {
  const root = mkdtempSync(join(tmpdir(), 'steward-media-'));
  const steward = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward', AITELIER_MEDIA_ROOT: root }));
  assert.deepEqual(steward.capabilities.slice(-8), ['memory', 'portfolio', 'handoff', 'proposals', 'channels', 'groups', 'catalog', 'media']);
  for (const op of ['ingest', 'resolve', 'read', 'fetch', 'postGroup', 'setProtected']) assert.equal(typeof steward.media[op], 'function', op);
  assert.equal(typeof steward.channels.senderBound, 'function');
  assert.doesNotThrow(() => validateHost(steward));
  const bare = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward' }));
  assert.ok(!bare.capabilities.includes('media')); assert.equal(bare.media, undefined);
  const director = aitelierHost(env({ AITELIER_MEDIA_ROOT: root }));
  assert.ok(!director.capabilities.includes('media'), 'the director gets the root (24.2b uses it), not the steward\'s tools');
});

test('the media root comes from AITELIER_MEDIA_ROOT (absolute) or there is none', () => {
  assert.equal(mediaRootFrom({ AITELIER_MEDIA_ROOT: '/srv/media' }), '/srv/media');
  assert.equal(mediaRootFrom({ AITELIER_MEDIA_ROOT: 'relative' }), undefined);
  assert.equal(mediaRootFrom({}), undefined);
});

test('fetch: what the gateway answered is stored as generated under the turn\'s audience; a refusal is passed on', async () => {
  const library = createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'steward-media-')) });
  const asked = [];
  let answer = () => new Response(PNG, { status: 200, headers: { 'content-type': 'image/png' } });
  const ops = mediaOps({ request: async () => ({}), library, gatewayFetch: async (connId, url) => { asked.push([connId, url]); return answer(); } });
  const stored = await ops.fetch({ connId: 'media', url: 'http://mcp_server:9003/files/a.png', audience: 'group:wechat:r1' });
  assert.match(stored.ref, /^m_[0-9a-f]{16}$/);
  assert.deepEqual(asked, [['media', 'http://mcp_server:9003/files/a.png']]);
  const row = await ops.resolve(stored.ref);
  assert.equal(row.source, 'generated'); assert.equal(row.audience, 'group:wechat:r1'); assert.equal(row.channel, 'generated');
  assert.deepEqual(readFileSync(row.path), PNG);
  answer = () => new Response(JSON.stringify({ detail: 'url_not_seen' }), { status: 403 });
  assert.deepEqual(await ops.fetch({ connId: 'media', url: 'http://x/y', audience: 'owner' }), { refused: 'url_not_seen' });
  answer = () => { throw new Error('ECONNREFUSED'); };
  assert.equal((await ops.fetch({ connId: 'media', url: 'http://x/y', audience: 'owner' })).refused, 'fetch_failed');
});

test('postGroup: post_group_media with the turn, the kind and the caption; a State refusal is {refused}', async () => {
  const sent = [];
  const ops = mediaOps({ library: createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'steward-media-')) }), gatewayFetch: async () => {},
    request: async (path, body) => { sent.push([path, body]); return { result: { posted: true, channel: 'wechat', group_ref: 'r1' } }; } });
  assert.deepEqual(await ops.postGroup({ turn: 't7', kind: 'image', caption: '猫' }), { posted: true, channel: 'wechat', group_ref: 'r1' });
  assert.equal(sent[0][0], '/api/state/commands/post_group_media');
  assert.equal(sent[0][1].turn, 't7'); assert.equal(sent[0][1].kind, 'image'); assert.equal(sent[0][1].caption, '猫');
  assert.match(sent[0][1].request_key, /^group-media:t7:/);
  const refusing = mediaOps({ library: createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'steward-media-')) }), gatewayFetch: async () => {},
    request: async () => { throw new Error('AItelier /x: HTTP 422 "group_send_budget"'); } });
  assert.equal((await refusing.postGroup({ turn: 't7', kind: 'image' })).refused, 'group_send_budget');
});

test('the relays carry attachments only when there are some; storeMedia\'s binding read', async () => {
  const sent = [];
  const request = async (path, body) => { sent.push([path, body]); return { result: { message_id: 'm1', bound: true } }; };
  const channels = channelOps(request), groups = groupOps({ request, project: 'aitelier-home', me: 'drv' });
  await channels.relay({ channel: 'wechat', senderRef: 'w', messageRef: 'r', text: '你好' });
  assert.ok(!('attachments' in sent.at(-1)[1]), 'a message without files is the request it always was');
  const files = [{ kind: 'image', ref: 'm_0123456789abcdef', mime: 'image/png', size: 9 }];
  await channels.relay({ channel: 'wechat', senderRef: 'w', messageRef: 'r2', text: '[图片]', attachments: files });
  assert.deepEqual(sent.at(-1)[1].attachments, files);
  await groups.relay({ channel: 'wechat', groupRef: 'g', senderRef: 'w', messageRef: 'r3', text: '[图片]', attachments: files });
  assert.deepEqual(sent.at(-1)[1].attachments, files);
  assert.deepEqual(await channels.senderBound({ channel: 'wechat', senderRef: 'w' }), { message_id: 'm1', bound: true });
  assert.deepEqual(sent.at(-1), ['/api/state/query/owner_channel_sender_bound', { channel: 'wechat', sender_ref: 'w' }]);
});

test('a wake shows each owner message\'s attachments one line each, from the source rows', async () => {
  const ref = 'm_3f9a000000000000';
  const request = async (path, body) => {
    assert.equal(path, '/api/state/query/message_sources');
    assert.deepEqual(body.message_ids, ['u1', 'u2']);
    return { result: { sources: { u1: { channel: 'wechat', attachments: [{ kind: 'voice', ref, durationMs: 23000 },
      { kind: 'image', refused: 'too_large' }] }, u2: { channel: 'wechat' } } } };
  };
  const result = { events: [], driver_inbox: [{ message_id: 'u1', sender_kind: 'user', body: '[语音 0:23]\n[图片 过大未收]' },
    { message_id: 'u2', sender_kind: 'user', body: 'hi' }, { message_id: 'd1', sender_kind: 'driver', body: 'x' }] };
  const shown = await withAttachmentLines(request, result);
  assert.deepEqual(shown.driver_inbox[0].attachments, [`[语音 ${ref}，0:23]`, '[图片 过大未收]']);
  assert.ok(!('attachments' in shown.driver_inbox[1]) && !('attachments' in shown.driver_inbox[2]));
  assert.equal(await withAttachmentLines(async () => { throw new Error('down'); }, result), result, 'a failed read leaves the wake as it was');
  const none = { events: [{ event_type: 'x' }] };
  assert.equal(await withAttachmentLines(async () => { throw new Error('not called'); }, none), none);
});

test('a wake\'s id is the digest of the batch as State answered it: with or without the attachment lines, a replay is the same wake', async () => {
  const result = { events: [{ event_type: 'user_message_received' }], driver_inbox: [{ message_id: 'u1', sender_kind: 'user', body: '[语音 0:23]' }] };
  const inbox = { items: [] };
  const request = async () => ({ result: { sources: { u1: { attachments: [{ kind: 'voice', ref: 'm_3f9a000000000000', durationMs: 23000 }] } } } });
  const read = stateWake(result, inbox, await withAttachmentLines(request, result));
  const failed = stateWake(result, inbox, await withAttachmentLines(async () => { throw new Error('down'); }, result));
  assert.ok(read.text.includes('m_3f9a000000000000') && !failed.text.includes('m_3f9a000000000000'));
  assert.equal(read.id, failed.id, 'the read succeeding once and failing once is still one wake');
  assert.match(read.id, /^state:[0-9a-f]{40}$/);
  // Without a media library the wake is the one it always was (text and id).
  const plain = stateWake(result, inbox);
  assert.equal(plain.text, 'AItelier runtime event:\n' + JSON.stringify({ ...result, project_inbox: inbox }));
  assert.equal(plain.id, read.id);
  // A batch that grew is a new wake.
  assert.notEqual(stateWake({ ...result, events: [...result.events, { event_type: 'x' }] }, inbox).id, plain.id);
});
