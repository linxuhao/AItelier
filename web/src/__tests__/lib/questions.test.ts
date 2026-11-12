import { describe, it, expect } from 'vitest';
import { openQuestions, readConversationRows, QUESTIONS_MAX_PAGES, QUESTIONS_PAGE_LIMIT } from '../../lib/questions';
import type { DriverConversationEntry } from '../../lib/api';

// The questions rail lists the OPEN questions: every `question` block that no
// later message cites. These pin the rule the owner asked for - a question is
// answered one at a time, an info block never appears, and a reply that does
// not cite a question leaves it open.

const at = (minute: number) => '2026-11-03T10:' + String(minute).padStart(2, '0') + ':00+00:00';

function driverMessage(id: string, minute: number, extra: Partial<DriverConversationEntry> = {}): DriverConversationEntry {
  return {
    message_id: id, thread_id: id, project_id: 'alpha',
    sender_kind: 'driver', sender_driver_id: 'dsh-alpha',
    subject: 'Subject ' + id, body: '', refs: {}, reply_to_message_id: null,
    created_at: at(minute), ...extra,
  };
}

function userReply(id: string, minute: number, extra: Partial<DriverConversationEntry> = {}): DriverConversationEntry {
  return {
    message_id: id, thread_id: 'thread', project_id: 'alpha',
    sender_kind: 'user', sender_driver_id: null,
    subject: 'Message from the user', body: 'answer', refs: {}, reply_to_message_id: null,
    created_at: at(minute), ...extra,
  };
}

describe('the open-questions rail', () => {
  it('lists a question block, with the message, subject, time, text and segment id', () => {
    const rows = [driverMessage('m1', 1, {
      segments: [{ id: 'q1', kind: 'question', text: 'may I restart?' }],
    })];
    expect(openQuestions(rows)).toEqual([{
      message_id: 'm1', subject: 'Subject m1', created_at: at(1),
      text: 'may I restart?', segment_id: 'q1',
    }]);
  });

  it('never lists an info block', () => {
    const rows = [driverMessage('m1', 1, {
      segments: [{ id: 'i1', kind: 'info', text: 'restart is coordinated' }],
    })];
    expect(openQuestions(rows)).toEqual([]);
  });

  it('a citing reply closes exactly that one question and leaves the others of the same message', () => {
    const rows = [
      driverMessage('m1', 1, { segments: [
        { id: 'q1', kind: 'question', text: 'first?' },
        { id: 'i1', kind: 'info', text: 'context' },
        { id: 'q2', kind: 'question', text: 'second?' },
      ] }),
      userReply('r1', 2, { answers: [{ message_id: 'm1', segment_id: 'q1' }] }),
    ];
    expect(openQuestions(rows).map(q => q.segment_id)).toEqual(['q2']);
  });

  it('a reply that only threads on reply_to_message_id, citing nothing, closes nothing', () => {
    const rows = [
      driverMessage('m1', 1, { segments: [{ id: 'q1', kind: 'question', text: 'ok?' }] }),
      userReply('r1', 2, { reply_to_message_id: 'm1' }),
    ];
    expect(openQuestions(rows).map(q => q.segment_id)).toEqual(['q1']);
  });

  it('lists oldest first, whatever order the rows were delivered in', () => {
    const rows = [
      driverMessage('m3', 3, { segments: [{ id: 'q3', kind: 'question', text: 'third' }] }),
      driverMessage('m1', 1, { segments: [{ id: 'q1', kind: 'question', text: 'first' }] }),
      driverMessage('m2', 2, { segments: [{ id: 'q2', kind: 'question', text: 'second' }] }),
    ];
    expect(openQuestions(rows).map(q => q.text)).toEqual(['first', 'second', 'third']);
  });

  it('a question cited by one message does not close a question with the same segment id in another', () => {
    const rows = [
      driverMessage('m1', 1, { segments: [{ id: 'q', kind: 'question', text: 'in m1' }] }),
      driverMessage('m2', 2, { segments: [{ id: 'q', kind: 'question', text: 'in m2' }] }),
      userReply('r1', 3, { answers: [{ message_id: 'm1', segment_id: 'q' }] }),
    ];
    expect(openQuestions(rows).map(q => q.message_id)).toEqual(['m2']);
  });

  it('the order rows arrive in does not change which questions are open', () => {
    const rows = [
      userReply('r1', 3, { answers: [{ message_id: 'm1', segment_id: 'q1' }] }),
      driverMessage('m1', 1, { segments: [{ id: 'q1', kind: 'question', text: 'asked' }] }),
    ];
    expect(openQuestions(rows)).toEqual([]);
  });

  it('a legacy question with no segment id is not listed (nothing can cite it)', () => {
    const rows = [driverMessage('m1', 1, { segments: [{ kind: 'question', text: 'old' }] as never })];
    expect(openQuestions(rows)).toEqual([]);
  });

  it('a message with no segments contributes nothing', () => {
    expect(openQuestions([driverMessage('m1', 1), driverMessage('m2', 2, { segments: null })])).toEqual([]);
  });
});

// ── the row source the rail needs ───────────────────────────────────────────
// `openQuestions` is only as good as the rows it is given. The rail therefore
// reads the WHOLE conversation, and this is the walk that does it: bounded,
// de-duplicating, and honest about whether it reached the beginning.
describe('reading the whole conversation for the rail', () => {
  const conversationPage = (messages: DriverConversationEntry[], next: string | null) => ({
    project_id: 'alpha', messages, has_more: next !== null, next_cursor: next,
    end_of_history: next === null, matched_total: messages.length, unread_count: 0,
    read_upto_message_id: null, read_upto_created_at: null,
  });

  /** A conversation served newest-first and keyset-paged, exactly as the route
   *  does it: `cursor` names the last row of the previous page. */
  function server(newestFirst: DriverConversationEntry[], pageSize = 2) {
    const calls: {cursor: string | null; limit: number}[] = [];
    const readPage = async (cursor: string | null, limit: number) => {
      calls.push({cursor, limit});
      const size = Math.min(pageSize, limit);
      const start = cursor ? newestFirst.findIndex(row => row.message_id === cursor) + 1 : 0;
      const slice = newestFirst.slice(start, start + size);
      const more = start + size < newestFirst.length;
      return conversationPage(slice, more ? slice[slice.length - 1].message_id : null);
    };
    return {readPage, calls};
  }

  it('walks past the first page, and lists a question thirty later messages pushed off it', async () => {
    // The bug this exists for: the thread's first page holds the newest rows
    // only, and the question is at the oldest end.
    const asked = driverMessage('q', 0, { segments: [{ id: 'q1', kind: 'question', text: 'may I restart?' }] });
    const infos = Array.from({length: 40}, (_, i) => driverMessage('i' + i, i + 1,
      { segments: [{ id: 'i' + i, kind: 'info', text: 'nothing to report ' + i }] }));
    const newestFirst = [...infos].reverse().concat([asked]);
    const {readPage, calls} = server(newestFirst, 20);

    const {rows, complete} = await readConversationRows(readPage);

    expect(complete).toBe(true);
    expect(rows).toHaveLength(41);
    // Oldest first, de-duplicated, every row exactly once.
    expect(rows[0].message_id).toBe('q');
    expect(new Set(rows.map(row => row.message_id)).size).toBe(41);
    expect(openQuestions(rows).map(question => question.segment_id)).toEqual(['q1']);
    // It really did page back, and it asked for the largest page the route takes.
    expect(calls.length).toBeGreaterThan(1);
    expect(calls.every(call => call.limit === QUESTIONS_PAGE_LIMIT)).toBe(true);
    expect(calls[0].cursor).toBeNull();
  });

  it('starts where it is told to, so rows the thread already read are not read twice', async () => {
    const rows = [driverMessage('m3', 3), driverMessage('m2', 2), driverMessage('m1', 1)];
    const {readPage, calls} = server(rows, 5);
    const result = await readConversationRows(readPage, {from: 'm2'});
    expect(result.complete).toBe(true);
    expect(result.rows.map(row => row.message_id)).toEqual(['m1']);
    expect(calls[0].cursor).toBe('m2');
  });

  it('does not duplicate a row that arrives on two pages', async () => {
    // A keyset cursor is stable, but a page boundary can repeat a row when new
    // messages land; identity is `message_id`, so it is merged, not doubled.
    let page = 0;
    const readPage = async () => {
      page += 1;
      return page === 1
        ? conversationPage([driverMessage('m2', 2), driverMessage('m1', 1)], 'm1')
        : conversationPage([driverMessage('m1', 1)], null);
    };
    const result = await readConversationRows(readPage);
    expect(result.complete).toBe(true);
    expect(result.rows.map(row => row.message_id)).toEqual(['m1', 'm2']);
  });

  it('stops at the page bound and reports the list as INCOMPLETE instead of truncating silently', async () => {
    // Every page claims more history, so only the bound can stop the walk.
    let page = 0;
    const readPage = async () => {
      page += 1;
      return conversationPage([driverMessage('m' + page, page)], 'cursor-' + page);
    };
    const result = await readConversationRows(readPage);
    expect(result.complete).toBe(false);
    expect(page).toBe(QUESTIONS_MAX_PAGES);
    expect(result.rows).toHaveLength(QUESTIONS_MAX_PAGES);
  });

  it('honours an explicit page bound, so the bound is a parameter and not a hidden constant', async () => {
    const readPage = async (cursor: string | null) =>
      conversationPage([driverMessage('m' + (cursor ?? '0'), 1)], 'next-' + (cursor ?? '0'));
    const result = await readConversationRows(readPage, {maxPages: 3});
    expect(result.complete).toBe(false);
    expect(result.rows).toHaveLength(3);
  });
});
