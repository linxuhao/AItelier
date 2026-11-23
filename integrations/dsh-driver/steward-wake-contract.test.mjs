/** The steward's wake, built from a REAL State answer (2026-11-22): the host reads the owner's
 *  messages in a wake by `sender_kind === 'user'`, and list_driver_messages' rows carried no
 *  such key, so an owner's file reached the steward as a bare placeholder and no wake ever
 *  counted as a person's message. The fixture is the driver_inbox of one real
 *  wait_for_state_change answer - a director's letter, an owner message relayed from Slack, a
 *  host notice, an owner message from the dashboard - pinned by
 *  tests/unit/test_driver_inbox_sender_kind.py, so the rows here are the rows State sends. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { humanInput, requesterOf, stateWake, withAttachmentLines } from './steward-host.mjs';

const rows = JSON.parse(readFileSync(new URL('./test-fixtures/steward-wake-driver-inbox.json', import.meta.url), 'utf8')).driver_inbox;
const byId = Object.fromEntries(rows.map(row => [row.message_id, row]));
const OWNERS = ['owner-slack', 'owner-dashboard'];
const PDF = { kind: 'file', ref: 'm_0000000000000001', mime: 'application/pdf', size: 2048 };

/** One wake as runtime.mjs builds it: the State answer (resolved deliveries dropped), its
 *  attachment lines, then stateWake's text. `asked` records the message_sources reads. */
async function wake(inbox, asked = []) {
  const result = { events: [], next_after: 0, next_inbox_after: inbox.length, driver_inbox: inbox.filter(m => m.status !== 'resolved'), timed_out: false };
  const request = async (path, body) => {
    asked.push([path, body]);
    return { result: { sources: Object.fromEntries(body.message_ids.map(id => [id, { direction: 'in', channel: 'slack', attachments: [PDF] }])) } };
  };
  const shown = await withAttachmentLines(request, result);
  return { shown, text: stateWake(result, { items: [] }, shown).text };
}

test('the fixture is a real wake: every row says who wrote it, and only the owner\'s two are the user\'s', () => {
  assert.deepEqual(rows.map(row => [row.message_id, row.sender_kind]),
    [['director-letter', 'driver'], ['owner-slack', 'user'], ['host-notice', 'driver'], ['owner-dashboard', 'user']]);
});

test('the owner\'s messages in a real wake get their attachment lines, are a person\'s input and name the requester', async () => {
  const asked = [];
  const { shown, text } = await wake(rows, asked);
  assert.deepEqual(asked, [['/api/state/query/message_sources', { message_ids: OWNERS }]], 'only the owner\'s messages are looked up');
  for (const row of shown.driver_inbox)
    assert.deepEqual(row.attachments, OWNERS.includes(row.message_id) ? ['[文件 m_0000000000000001，application/pdf，2 KB]'] : undefined, row.message_id);
  assert.equal(humanInput(text), true);
  assert.equal(requesterOf(text), 'owner-dashboard', 'the newest owner message of the wake');
});

test('a real wake of letters and notices only is nobody\'s input and names no requester', async () => {
  const asked = [];
  const { shown, text } = await wake([byId['director-letter'], byId['host-notice']], asked);
  assert.deepEqual(asked, [], 'no owner message, no read');
  assert.ok(shown.driver_inbox.every(row => !('attachments' in row)));
  assert.equal(humanInput(text), false);
  assert.equal(requesterOf(text), undefined);
});
