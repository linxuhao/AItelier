// Step 24.2c R-M3 (design-steward 7.8.12b "八" 4, "十六" 适配器): the steward's root session keeps
// at most image_retain's already-seen images and asks DSH to offload the oldest; an image the
// model has not seen yet (after the last assistant message) is never offloaded (review NEW-1).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { installHostModel, offloadedImagePrefixCount, offloadedImagePlaceholder, REQUEST_IMAGE_MAX_BYTES } from './model.mjs';
import { loadRoles } from './roles.mjs';
import { measuredEntryForTest } from './dsh-entry.mjs';

const STEWARD_RETAIN = loadRoles().steward.image_retain;
const ONE_REQUEST_IMAGE_B64 = 5592408;
const BOTH = { root: ['text', 'image'], child: ['text', 'image'] };

/** The DSH shapes the adapter uses, for the tests that do not load DSH itself. */
class FakeLlmError extends Error {
  constructor(message, code, options) {
    super(message);
    this.code = code;
    this.failure = { message, code, ...(options?.offloadImages === undefined ? {} : { offloadImages: options.offloadImages }) };
  }
}
const fakeProject = (messages, placeholder) => messages.map(message => (message.content.some(b => b.type === 'image' && b.offloaded === true)
  ? { ...message, content: message.content.map(b => (b.type === 'image' && b.offloaded === true ? { type: 'text', text: placeholder(b.attachment) } : b)) }
  : message));
const FAKE_LLM = { LlmAdapter: class {}, LlmError: FakeLlmError, projectOffloadedImages: fakeProject };

function harness({ sizes = {}, retain = STEWARD_RETAIN, routes = BOTH, llm = FAKE_LLM, rootReadsFiles } = {}) {
  const reads = [], sent = [], sentBlocks = new Set();
  const attachments = { async readImageRequest(ref, target) {
    assert.equal(target.maxBytes, REQUEST_IMAGE_MAX_BYTES, 'the request image cap is the exported constant');
    reads.push(ref.attachmentId);
    return { mediaType: 'image/jpeg', data: new Uint8Array(sizes[ref.attachmentId] ?? 30) };
  } };
  let adapter;
  installHostModel({ llm: { registerAdapter(_names, value) { adapter = value; } }, get: () => attachments }, llm,
    { project: 'alpha', session: 'root', routeInput: async () => routes, ...(retain ? { imageRetain: retain } : {}),
      ...(rootReadsFiles === undefined ? {} : { rootReadsFiles }),
      async request(_path, body) { sent.push(body); return { text: 'ok', tool_calls: [], truncated: false, usage: {} }; } });
  return { adapter, reads, sent, sentBlocks };
}

const img = (id, name) => ({ type: 'image', attachment: { attachmentId: id, ...(name ? { name } : {}) } });
const user = (...content) => ({ role: 'user', content });
const tool = (id, ...content) => ({ role: 'tool', toolCallId: id, content });
const assistant = (...ids) => ({ role: 'assistant', content: ids.length
  ? ids.map(id => ({ type: 'tool-call', id, name: 'read_document', arguments: '{}' })) : [{ type: 'text', text: 'done' }] });
const text = value => ({ type: 'text', text: value });

/** One request over the session's events: the folded ones (a group turn folded out of an owner
 *  turn, 7.7.6a) stay on the surface but are not sent. Blocks are copied, so the adapter sees
 *  the current offload marks and cannot keep a reference. */
const requestOf = (events, sessionId = 'root') => ({ provider: 'aitelier', model: 'driver', sessionId, system: 'rules',
  messages: events.filter(e => !e.folded).map(({ folded: _f, ...e }) => ({ ...e, content: e.content.map(b => ({ ...b })) })) });

async function send(h, events, sessionId) {
  try {
    for await (const _ of h.adapter.stream(requestOf(events, sessionId))) { /* drain */ }
    return { body: h.sent.at(-1) };
  } catch (error) {
    if (error?.code !== 'IMAGE_OFFLOAD_REQUIRED') throw error;
    return { offload: error.failure.offloadImages, error };
  }
}

/** DSH dsh-compaction-image-offload `offloadOldestImages`, simulated on the events: the oldest
 *  `count` not yet offloaded images of user and tool messages, folded ones included. */
function offloadOldest(events, count) {
  const marked = [];
  for (const event of events) {
    if (count === 0) break;
    if (event.role !== 'user' && event.role !== 'tool') continue;
    for (const block of event.content) {
      if (count === 0) break;
      if (block.type === 'image' && block.offloaded !== true) { block.offloaded = true; marked.push(block); count -= 1; }
    }
  }
  return marked;
}

/** The image blocks of the request after its last assistant message: the latest input. */
function latestImages(events) {
  const sentEvents = events.filter(e => !e.folded);
  const last = sentEvents.findLastIndex(e => e.role === 'assistant');
  return sentEvents.slice(last + 1).flatMap(e => e.content.filter(b => b.type === 'image'));
}
function seenImages(events) {
  const sentEvents = events.filter(e => !e.folded);
  const last = sentEvents.findLastIndex(e => e.role === 'assistant');
  return sentEvents.slice(0, Math.max(0, last)).filter(e => e.role === 'user' || e.role === 'tool')
    .flatMap(e => e.content.filter(b => b.type === 'image' && b.offloaded !== true));
}

/** Send until the adapter stops asking, applying each offload as DSH would and checking at
 *  every round: n never exceeds the seen images, no latest-input image is marked, and every
 *  marked image the request carries was already sent once (`sentBlocks`). */
async function run(h, events, { sentBlocks = h.sentBlocks, sessionId } = {}) {
  const offloads = [], seenAtThrow = [];
  const imageCount = events.reduce((sum, e) => sum + e.content.filter(b => b.type === 'image').length, 0);
  for (let round = 0; round <= imageCount + 1; round += 1) {
    const latest = latestImages(events), seen = seenImages(events);
    const result = await send(h, events, sessionId);
    if (result.body) {
      for (const e of events) if (!e.folded) for (const b of e.content) if (b.type === 'image' && b.offloaded !== true) sentBlocks.add(b);
      return { offloads, seenAtThrow, body: result.body };
    }
    assert.ok(result.offload > 0 && result.offload <= seen.length, `n ${result.offload} is within the ${seen.length} seen images`);
    offloads.push(result.offload);
    seenAtThrow.push(seen.length);
    const marked = offloadOldest(events, result.offload);
    for (const block of marked) {
      assert.ok(!latest.includes(block), `an image of the latest input was offloaded: ${block.attachment.attachmentId}`);
      const folded = events.some(e => e.folded && e.content.includes(block));
      if (!folded) assert.ok(sentBlocks.has(block), `an image offloaded before the model saw it: ${block.attachment.attachmentId}`);
    }
  }
  assert.fail('the offload retries did not converge');
}

const imageUrls = body => body.messages.flatMap(m => (Array.isArray(m.content) ? m.content.filter(p => p.type === 'image_url') : []));
const bodyText = body => body.messages.map(m => (Array.isArray(m.content) ? m.content.filter(p => p.type === 'text').map(p => p.text).join('\n') : m.content)).join('\n');
const offloadedIds = events => events.flatMap(e => e.content.filter(b => b.type === 'image' && b.offloaded === true).map(b => b.attachment.attachmentId));

const PDF = 'm_8c1d00000000aaaa', PHOTO = 'm_0123456789abcdef';
const page = n => img(`sha256:p${n}`, `${PDF}#p${n}.jpg`);

test('NEW-1 case 1: a photo plus 4 pages - no offload at the wake or the tool result, then 4 (the photo and pages 1-3)', async () => {
  const h = harness();
  const events = [user(text('wake'), img('sha256:photo', `${PHOTO}.jpg`))];
  let step = await run(h, events);
  assert.deepEqual(step.offloads, [], 'the wake request does not offload');
  assert.equal(imageUrls(step.body).length, 1);
  events.push(assistant('c1'), tool('c1', text('[PDF …]'), page(1), page(2), page(3), page(4)));
  step = await run(h, events);
  assert.deepEqual(step.offloads, [], 'the tool-result request does not offload: 1 seen, 4 latest');
  assert.equal(imageUrls(step.body).length, 5, 'the model gets all 5 at once');
  events.push(assistant('c2'), tool('c2', text('sent')));
  step = await run(h, events);
  assert.deepEqual(step.offloads, [4]);
  assert.deepEqual(offloadedIds(events), ['sha256:photo', 'sha256:p1', 'sha256:p2', 'sha256:p3']);
  assert.equal(imageUrls(step.body).length, 1, 'page 4 stays');
  const sentText = bodyText(step.body);
  assert.ok(sentText.includes(`[图片 ${PHOTO} 已移出上下文；要再看请 read_document(ref=${PHOTO})]`));
  assert.ok(sentText.includes(`[第 3 页图已移出上下文（文字仍在上面）；要再看请 read_document(ref=${PDF}, pages=3)]`));
});

test('NEW-1 case 2: 5 photos - the first request sends all 5, the next step offloads 4', async () => {
  const h = harness();
  const photos = [1, 2, 3, 4, 5].map(n => img(`sha256:ph${n}`, `m_00000000000000${n}0.png`));
  const events = [user(text('look'), ...photos)];
  let step = await run(h, events);
  assert.deepEqual(step.offloads, []);
  assert.equal(imageUrls(step.body).length, 5);
  events.push(assistant(), user(text('and this one'), img('sha256:ph6', 'm_0000000000000060.png')));
  step = await run(h, events);
  assert.deepEqual(step.offloads, [4]);
  assert.deepEqual(offloadedIds(events), ['sha256:ph1', 'sha256:ph2', 'sha256:ph3', 'sha256:ph4']);
  assert.equal(imageUrls(step.body).length, 2, 'photo 5 (seen) and photo 6 (latest)');
});

test('NEW-1 case 3: read "1-3" then "4-7" - 7 images visible, then pages 1-4 go', async () => {
  const h = harness();
  const events = [user(text(`[文件 ${PDF}，application/pdf，1.2 MB；用 read_document 读]`))];
  await run(h, events);
  events.push(assistant('r1'), tool('r1', text('1-3'), page(1), page(2), page(3)));
  let step = await run(h, events);
  assert.deepEqual(step.offloads, []);
  events.push(assistant('r2'), tool('r2', text('4-7'), page(4), page(5), page(6), page(7)));
  step = await run(h, events);
  assert.deepEqual(step.offloads, [], '3 seen are within 4');
  assert.equal(imageUrls(step.body).length, 7);
  events.push(assistant('r3'), tool('r3', text('ok')));
  step = await run(h, events);
  assert.deepEqual(step.offloads, [4]);
  assert.deepEqual(offloadedIds(events), ['sha256:p1', 'sha256:p2', 'sha256:p3', 'sha256:p4']);
  assert.equal(imageUrls(step.body).length, 3);
});

test('NEW-1 case 4: a 2.5 MB photo and a cap-size image are kept once seen; bytes offload only over 6 MiB, oldest first', async () => {
  const sizes = { 'sha256:big': 2.5 * 1024 * 1024, 'sha256:cap': REQUEST_IMAGE_MAX_BYTES };
  assert.equal(Math.ceil(REQUEST_IMAGE_MAX_BYTES / 3) * 4, ONE_REQUEST_IMAGE_B64);
  for (const id of ['sha256:big', 'sha256:cap']) {
    const h = harness({ sizes });
    const events = [user(text('photo'), img(id, `${PHOTO}.jpg`))];
    assert.deepEqual((await run(h, events)).offloads, [], `${id}: the first request`);
    events.push(assistant(), user(text('more?')));
    const step = await run(h, events);
    assert.deepEqual(step.offloads, [], `${id}: the step after it was seen (one image is under 6 MiB)`);
    assert.equal(imageUrls(step.body)[0].image_url.url.length - 'data:image/jpeg;base64,'.length, Math.ceil(sizes[id] / 3) * 4,
      'measured on the data actually sent');
  }
  const h = harness({ sizes });
  const events = [user(text('photo'), img('sha256:big', `${PHOTO}.jpg`))];
  await run(h, events);
  events.push(assistant(), user(text('and'), img('sha256:cap', 'm_00000000000000ca.jpg')));
  let step = await run(h, events);
  assert.deepEqual(step.offloads, [], 'the cap image is the latest input; 3.3 MiB seen');
  events.push(assistant(), user(text('so?')));
  step = await run(h, events);
  assert.ok(3495254 + ONE_REQUEST_IMAGE_B64 > 6291456);
  assert.deepEqual(step.offloads, [1], 'over 6 MiB seen: one byte-based offload');
  assert.deepEqual(offloadedIds(events), ['sha256:big'], 'the oldest goes');
});

/** Seeded PRNG (mulberry32): the property test is the same run every time. */
function prng(seed) {
  return () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}

test('property: random sessions never offload an unseen image, n never exceeds the seen images, retries converge', async () => {
  const random = prng(20261123);
  const pick = n => Math.floor(random() * n);
  let throws = 0, byteRounds = 0;
  for (let run_ = 0; run_ < 120; run_ += 1) {
    const sizes = {};
    let next = 0;
    const image = () => {
      const id = `sha256:r${run_}-${next++}`;
      sizes[id] = random() < 0.15 ? 1048576 + pick(REQUEST_IMAGE_MAX_BYTES - 1048576) : 1 + pick(200000);
      const kind = pick(3);
      return img(id, kind === 0 ? `${PDF}#p${next}.jpg` : kind === 1 ? `m_${String(next).padStart(16, '0')}.png` : undefined);
    };
    const images = max => Array.from({ length: pick(max + 1) }, image);
    const h = harness({ sizes });
    const events = [];
    for (let step = 0; step < 8; step += 1) {
      const action = pick(4);
      if (action === 0) events.push(user(text('wake'), ...images(5)));
      else if (action === 1) events.push({ ...user(text('group turn'), ...images(3)), folded: true }, user(text('wake')));
      else if (action === 2) {
        const ids = Array.from({ length: 1 + pick(3) }, (_, i) => `c${step}-${i}`);
        events.push(assistant(...ids), ...ids.map(id => tool(id, text('result'), ...images(4))));
      } else events.push(assistant(), user(text('next'), ...images(2)));
      const result = await run(h, events);
      throws += result.offloads.length;
      byteRounds += result.seenAtThrow.filter(count => count <= STEWARD_RETAIN.max_images).length;
      const unoffloaded = events.filter(e => !e.folded).flatMap(e => e.content.filter(b => b.type === 'image' && b.offloaded !== true));
      assert.equal(imageUrls(result.body).length, unoffloaded.length, 'every image not offloaded is sent, nothing else');
      const offloaded = new Set(offloadedIds(events));
      const lastReads = h.reads.slice(h.reads.length - unoffloaded.length);
      assert.ok(lastReads.every(id => !offloaded.has(id)), 'an offloaded image is never read');
    }
  }
  assert.ok(throws > 30, `the generator exercised offloads (${throws})`);
  assert.ok(byteRounds > 0, `and byte-based ones (${byteRounds})`);
});

test('the three placeholders are fixed text from the attachment name, byte-identical across generations', async () => {
  assert.equal(offloadedImagePlaceholder({ attachmentId: 'sha256:x', name: `${PDF}#p12.jpg` }),
    `[第 12 页图已移出上下文（文字仍在上面）；要再看请 read_document(ref=${PDF}, pages=12)]`);
  for (const ext of ['png', 'jpg', 'jpeg', 'gif', 'webp']) {
    assert.equal(offloadedImagePlaceholder({ attachmentId: 'sha256:x', name: `${PHOTO}.${ext}` }),
      `[图片 ${PHOTO} 已移出上下文；要再看请 read_document(ref=${PHOTO})]`);
  }
  assert.equal(offloadedImagePlaceholder({ attachmentId: 'sha256:x', name: 'chart.png' }), '[一张图已移出上下文（chart.png），不能再看]');
  assert.equal(offloadedImagePlaceholder({ attachmentId: 'sha256:x', name: `${PDF}#p1.png` }), `[一张图已移出上下文（${PDF}#p1.png），不能再看]`);
  assert.equal(offloadedImagePlaceholder({ attachmentId: 'sha256:x' }), '[一张图已移出上下文（sha256:x），不能再看]');
  const events = () => [user(text('a'), { ...img('sha256:1', `${PDF}#p2.jpg`), offloaded: true }, { ...img('sha256:2', `${PHOTO}.webp`), offloaded: true },
    { ...img('sha256:3', 'tool-output.png'), offloaded: true }, img('sha256:4', 'kept.png')), assistant(), user(text('b'))];
  const h = harness();
  const first = await run(h, events()), second = await run(h, events());
  assert.equal(JSON.stringify(first.body), JSON.stringify(second.body), 'two generations, the same bytes');
  assert.equal(first.body.messages[1].content[0].text, ['a', `[第 2 页图已移出上下文（文字仍在上面）；要再看请 read_document(ref=${PDF}, pages=2)]`,
    `[图片 ${PHOTO} 已移出上下文；要再看请 read_document(ref=${PHOTO})]`, '[一张图已移出上下文（tool-output.png），不能再看]'].join('\n'));
});

test('offloaded blocks are neither read nor sent, with or without image_retain (the latent fix)', async () => {
  for (const retain of [STEWARD_RETAIN, null]) {
    const h = harness({ retain });
    const events = [user(text('a'), { ...img('sha256:gone', `${PHOTO}.png`), offloaded: true }, img('sha256:here'))];
    const { body } = await run(h, events);
    assert.deepEqual(h.reads, ['sha256:here'], 'the offloaded image is not read');
    assert.equal(imageUrls(body).length, 1, 'and not sent');
  }
});

test('children, text-only routes and roles without image_retain never offload', async () => {
  const many = () => [user(text('x'), ...Array.from({ length: 9 }, (_, i) => img(`sha256:m${i}`))), assistant(), user(text('y'))];
  for (const [label, h, sessionId] of [['a steward child', harness(), 'child-1'], ['no image_retain', harness({ retain: null }), 'root']]) {
    const events = many();
    const step = await run(h, events, { sessionId });
    assert.deepEqual(step.offloads, [], label);
    assert.equal(imageUrls(step.body).length, 9, label);
  }
  const textOnly = harness({ routes: { root: ['text'], child: ['text'] } });
  const step = await run(textOnly, many());
  assert.deepEqual(step.offloads, []);
  assert.deepEqual(textOnly.reads, [], 'a text-only route reads no image');
});

test('install refuses a byte cap under one request image; the roles.json value passes', () => {
  const install = retain => installHostModel({ llm: { registerAdapter() {} } }, FAKE_LLM, { project: 'a', session: 'root', imageRetain: retain, async request() {} });
  assert.ok(STEWARD_RETAIN.max_base64_bytes >= ONE_REQUEST_IMAGE_B64, 'roles.json steward image_retain holds one request image');
  assert.doesNotThrow(() => install(STEWARD_RETAIN));
  assert.doesNotThrow(() => install({ ...STEWARD_RETAIN, max_base64_bytes: ONE_REQUEST_IMAGE_B64 }));
  assert.throws(() => install({ ...STEWARD_RETAIN, max_base64_bytes: ONE_REQUEST_IMAGE_B64 - 1 }), /under one request image \(5592408/);
  assert.throws(() => installHostModel({ llm: { registerAdapter() {} } }, { LlmAdapter: class {} },
    { project: 'a', session: 'root', imageRetain: STEWARD_RETAIN, async request() {} }), /needs DSH LlmError/);
  assert.doesNotThrow(() => installHostModel({ llm: { registerAdapter() {} } }, { LlmAdapter: class {} }, { project: 'a', session: 'root', async request() {} }),
    'no image_retain needs nothing more than before');
});

test('the copied rounding at its byte boundaries (pinned values, measured on DSH 0.2.1-rc.5)', () => {
  assert.equal(offloadedImagePrefixCount([4, 4, 4], { maxBytes: 10, byteQuantum: 4 }), 2, 'a quantum over 1 keeps removing at an exact landing');
  assert.equal(offloadedImagePrefixCount([4, 4, 4], { maxBytes: 8, byteQuantum: 1 }), 1, 'a quantum of 1 stops there');
  assert.equal(offloadedImagePrefixCount([4, 4, 4, 4, 4], { maxImages: 4, countQuantum: 4 }), 4);
  assert.equal(offloadedImagePrefixCount([4, 4], {}), 0, 'no budget, nothing');
});

test('alignment with the pinned DSH: the copied rounding equals requiredImageOffload, and the adapter throws a real LlmError', async t => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const llm = await import(createRequire(entry).resolve('@deepseek-ai/dsh-llm'));
  // The exact boundaries random lengths rarely hit: removed bytes landing exactly on the byte
  // quantum (DSH keeps removing when the quantum is over 1, stops when it is 1).
  for (const [raw, budget, expected] of [[[3, 3, 3], { maxBytes: 10, byteQuantum: 4 }, 2], [[3, 3, 3], { maxBytes: 8, byteQuantum: 1 }, 1],
    [[3, 3, 3, 3, 3], { maxImages: 4, countQuantum: 4, maxBytes: 100, byteQuantum: 1 }, 4]]) {
    const messages = [{ role: 'user', content: raw.map((bytes, k) => ({ type: 'image', attachment: { attachmentId: `b${k}`, bytes } })) }];
    assert.equal(llm.requiredImageOffload(messages, { ...budget, representation: 'base64' }, block => block.attachment.bytes), expected, JSON.stringify(budget));
    assert.equal(offloadedImagePrefixCount(raw.map(bytes => Math.ceil(bytes / 3) * 4), budget), expected, JSON.stringify(budget));
  }
  const random = prng(7);
  const pick = n => Math.floor(random() * n);
  for (let i = 0; i < 2000; i += 1) {
    const raw = Array.from({ length: pick(12) }, () => (random() < 0.3 ? pick(REQUEST_IMAGE_MAX_BYTES) : pick(300000)));
    const budget = { maxImages: 1 + pick(8), countQuantum: 1 + pick(4), maxBytes: 1 + pick(8 * 1048576), byteQuantum: random() < 0.2 ? 1 : 1 + pick(1048576) };
    const messages = [{ role: 'user', content: raw.map((bytes, k) => ({ type: 'image', attachment: { attachmentId: `a${k}`, bytes } })) }];
    const theirs = llm.requiredImageOffload(messages, { ...budget, representation: 'base64' }, block => block.attachment.bytes);
    assert.equal(offloadedImagePrefixCount(raw.map(bytes => Math.ceil(bytes / 3) * 4), budget), theirs, JSON.stringify({ raw, budget }));
  }
  // End to end with the real LlmError and projectOffloadedImages: every image seen, so the adapter's
  // n is DSH's own count on the same request bytes.
  for (let i = 0; i < 40; i += 1) {
    const sizes = {};
    const blocks = Array.from({ length: 1 + pick(10) }, (_, k) => {
      sizes[`sha256:e${i}-${k}`] = random() < 0.3 ? pick(REQUEST_IMAGE_MAX_BYTES) + 1 : pick(100000) + 1;
      return img(`sha256:e${i}-${k}`, `m_${String(k).padStart(16, '0')}.png`);
    });
    const h = harness({ sizes, llm });
    const events = [user(text('x'), ...blocks), assistant(), user(text('y'))];
    const result = await send(h, events);
    const theirs = llm.requiredImageOffload(requestOf(events).messages, { representation: 'base64', maxImages: STEWARD_RETAIN.max_images,
      countQuantum: STEWARD_RETAIN.count_quantum, maxBytes: STEWARD_RETAIN.max_base64_bytes, byteQuantum: STEWARD_RETAIN.byte_quantum },
    block => sizes[block.attachment.attachmentId]);
    if (theirs === 0) { assert.ok(result.body, 'DSH would send it: so does the adapter'); continue; }
    assert.ok(result.error instanceof llm.LlmError, 'a real DSH LlmError');
    assert.equal(result.error.code, llm.IMAGE_OFFLOAD_REQUIRED_CODE);
    assert.equal(result.offload, theirs);
  }
  // The real projection turns DSH's offloaded marks into the adapter's placeholder.
  const h = harness({ llm });
  const { body } = await run(h, [user(text('a'), { ...img('sha256:1', `${PDF}#p2.jpg`), offloaded: true })]);
  assert.equal(body.messages[1].content, `a\n[第 2 页图已移出上下文（文字仍在上面）；要再看请 read_document(ref=${PDF}, pages=2)]`);
});
