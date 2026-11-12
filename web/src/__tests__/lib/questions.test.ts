import { describe, it, expect } from 'vitest';
import { openQuestions } from '../../lib/questions';
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
