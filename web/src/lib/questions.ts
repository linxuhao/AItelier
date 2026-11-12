/** The questions the user still owes an answer to, for the questions rail.
 *
 *  A `question` segment is OPEN until a later message cites it in its
 *  `answers`. The rail lists only open questions, so an info block (or a
 *  question already answered) never appears there: the rail is what keeps a
 *  question from being swept off screen by the thread's newer messages.
 *
 *  Pure on purpose: the rule is the same whatever the component renders, and
 *  it is testable without a DOM.
 */
import type { DriverConversationEntry } from './api';

/** One open question, ready for the rail. `segment_id` plus `message_id` is the
 *  exact pair a reply must cite to close it. */
export interface OpenQuestion {
  message_id: string;
  subject: string;
  created_at: string;
  text: string;
  segment_id: string;
}

/** Every question block no later message cites, oldest first.
 *
 *  A citation names one (message_id, segment_id) pair, so it closes exactly
 *  that question: the other questions of the same message stay open. A reply
 *  that only sets `reply_to_message_id` cites nothing and closes nothing.
 *  Non-question segments are never listed.
 *
 *  Input is the conversation rows as the API returns them (newest-first or
 *  otherwise); the output order is the thread's own `(created_at, message_id)`
 *  order, oldest first, so it never depends on how the rows were delivered.
 */
export function openQuestions(rows: readonly DriverConversationEntry[]): OpenQuestion[] {
  const cited = new Set<string>();
  for (const row of rows) {
    for (const answer of row.answers ?? []) {
      cited.add(answer.message_id + '\u0000' + answer.segment_id);
    }
  }
  const ordered = [...rows].sort((a, b) => {
    if (a.created_at !== b.created_at) return a.created_at < b.created_at ? -1 : 1;
    if (a.message_id !== b.message_id) return a.message_id < b.message_id ? -1 : 1;
    return 0;
  });
  const open: OpenQuestion[] = [];
  for (const row of ordered) {
    for (const segment of row.segments ?? []) {
      if (segment.kind !== 'question' || !segment.id) continue;
      if (cited.has(row.message_id + '\u0000' + segment.id)) continue;
      open.push({
        message_id: row.message_id,
        subject: row.subject,
        created_at: row.created_at,
        text: segment.text,
        segment_id: segment.id,
      });
    }
  }
  return open;
}
