/** Overfit review 2026-11-24 (重-10): the plugin records the root session's own turns into the
 *  owner's memory when its host has no conversation source (no `memory` capability), and only
 *  then: a host that records the turns itself (the backend adapter) gets no second writer. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { MemoryStore } from '../lib/memory/store.mjs';
import { TurnRecorder, turnRecorderFor } from '../lib/memory/turns.mjs';
import { localHost } from '../hosts/local.mjs';

const at = '2026-11-24T10:00:00.000Z';
const owner = (id, text, kind = 'user') => ({ type: 'user/message', time: Date.parse(at), data: { id, role: 'user', source: { kind }, content: [{ type: 'text', text }] } });
const reply = (id, ...content) => ({ type: 'assistant/message', time: Date.parse(at) + 1000, data: { message: { id, role: 'assistant', content } } });
const events = [
  { type: 'turn/start' },
  owner('m-1', 'Remember HERON_4711: the boat is blue.'),
  owner('clock-1', '[system clock] now 2026-11-24T10:00Z', 'dsh-director-clock'),
  reply('a-1', { type: 'tool-call', id: 'c1', name: 'memory_note', arguments: '{}' }),
  reply('a-2', { type: 'text', text: 'Noted: the boat is blue.' }),
  // A wake opens the next turn: neither it nor the reply to it is the owner's conversation.
  { type: 'turn/start' },
  owner('w-1', 'Director event (wake.json): {"wake":1}', 'dsh-director-event'),
  reply('a-3', { type: 'text', text: 'WAKE_REPLY nothing to do' }),
  { type: 'turn/start' },
  owner('m 2/odd', 'And the sail?'),
  reply('a-4', { type: 'text', text: 'White.' }),
];

test('the root\'s owner turns are recorded - the owner\'s words and the replies in that turn, nothing a wake or a clock line brought - once', async t => {
  const root = mkdtempSync(join(tmpdir(), 'dsh-turns-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const store = await MemoryStore.open({ root, userId: 'owner' });
  try {
    const recorder = new TurnRecorder({ store });
    for (const event of events) recorder.observe(event);
    await recorder.settled();
    assert.ok(store.known('t-m-1-o') && store.known('t-m-1-a-2'));
    assert.ok(!store.known('t-m-1-a-1'), 'a reply with no text is not an entry');
    const conversation = [...store.entries.values()].filter(e => e.source === 'conversation');
    assert.deepEqual(conversation.map(e => [e.role, e.text]), [['owner', 'Remember HERON_4711: the boat is blue.'],
      ['steward', 'Noted: the boat is blue.'], ['owner', 'And the sail?'], ['steward', 'White.']]);
    assert.equal(conversation[0].at, at);
    assert.ok(conversation.every(e => !e.text.includes('WAKE_REPLY') && !e.text.includes('system clock')));
    // An id memory cannot take whole is named by its digest, and its turn stays one turn.
    assert.equal(conversation[2].batch, conversation[3].batch);
    assert.match(conversation[2].id, /^t-[0-9a-f]{32}-o$/);
    // memory_search finds what the owner said.
    const found = JSON.stringify(await store.search('HERON_4711', 5));
    assert.ok(found.includes('t-m-1-o') && found.includes('the boat is blue'), found);
    // The same events again (a resumed session) write nothing.
    const before = store.entries.size;
    const again = new TurnRecorder({ store });
    for (const event of events) again.observe(event);
    await again.settled();
    assert.equal(store.entries.size, before);
  } finally { await store.close(); }
});

test('a failed write is logged and the next one still runs', async () => {
  const lines = [], written = [];
  const store = { recordTurn: async (turn, parts) => { if (parts[0].role === 'owner') throw new Error('disk full'); written.push(turn); return { written: 1 }; } };
  const recorder = new TurnRecorder({ store, log: line => lines.push(line) });
  for (const event of events.slice(0, 5)) recorder.observe(event);
  await recorder.settled();
  assert.deepEqual(lines, ['memory turn: disk full']);
  assert.deepEqual(written, ['m-1']);
});

test('who records: the plugin for a host without `memory` (the local host), never for one that records its own turns', () => {
  const store = { recordTurn: async () => ({ written: 0 }) };
  const local = localHost({ root: '/tmp/x', provider: 'p', model: 'm', rootRole: 'steward', memory: { userId: 'o' } });
  assert.ok(turnRecorderFor({ capabilities: local.capabilities, store }) instanceof TurnRecorder);
  // The backend adapter declares `memory` (steward-host.mjs records through syncConversation;
  // dsh-driver/steward.test.mjs checks this against its real capability list).
  assert.equal(turnRecorderFor({ capabilities: [...local.capabilities, 'memory'], store }), undefined);
  assert.equal(turnRecorderFor({ capabilities: local.capabilities, store: undefined }), undefined, 'no open store, nothing to record into');
});
