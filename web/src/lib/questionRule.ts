/** The RULE of the questions rail: which question is open, answered or withdrawn.
 *
 *  Kept apart from questions.ts, with type-only imports, so the same file runs
 *  in Node (type stripping) as well as in the dashboard: the backend's tests run
 *  it on the rows the server builds and check it agrees with the server's rule
 *  (core/driver_questions.py) - tests/unit/test_driver_question_withdraw.py.
 */
import type { DriverConversationEntry } from './api';

/** The thread's own order: `(created_at, message_id)`, oldest first. */
function compareThread(
  a: { created_at: string; message_id: string },
  b: { created_at: string; message_id: string },
): number {
  if (a.created_at !== b.created_at) return a.created_at < b.created_at ? -1 : 1;
  if (a.message_id !== b.message_id) return a.message_id < b.message_id ? -1 : 1;
  return 0;
}

/** One open question, ready for the rail. `segment_id` plus `message_id` is the
 *  exact pair a reply must cite to close it. */
export interface OpenQuestion {
  message_id: string;
  subject: string;
  created_at: string;
  text: string;
  segment_id: string;
}

/** One question the rail lists, open or answered. `answered` names the CITING
 *  row - the reply whose `answers` cites this question - and is null while no
 *  row cites it. `text` on `answered` is that row's body, kept as the user wrote
 *  it. */
export interface RailQuestion {
  message_id: string;
  subject: string;
  created_at: string;
  text: string;
  segment_id: string;
  answered: { message_id: string; created_at: string; text: string } | null;
  /** The asking driver's withdrawal (`withdraw_user_question`): the row whose
   *  `withdraws` cites this question, with its reason; null when none does. An
   *  answer wins over a withdrawal, so an answered question carries null here. */
  withdrawn: { message_id: string; created_at: string; reason: string } | null;
}


/** Every question block that carries a segment id, answered or not, for the
 *  questions rail.
 *
 *  The id is required, not a detail: a citation names one
 *  (message_id, segment_id) pair, so it closes exactly that question and the
 *  other questions of the same message stay open or answered on their own. A
 *  question block written BEFORE segment ids existed therefore cannot be cited,
 *  and this function skips it rather than list a question no reply could name.
 *  (Such rows do not exist in this deployment: every stored conversation row
 *  with segments was written after ids shipped, measured 0 rows on 2026-11-12.)
 *  A reply that only sets `reply_to_message_id` cites nothing, so it answers
 *  nothing. Non-question segments are never listed.
 *
 *  A question the asking driver withdrew (a row whose `withdraws` cites it) is
 *  neither open nor answered unless a reply answers it anyway - the answer wins.
 *
 *  Order: the OPEN questions first, in the thread's `(created_at,
 *  message_id)` order, oldest first; then the answered ones, ordered by the
 *  answering row's `(created_at, message_id)` DESCENDING - the most recently
 *  answered first; then the withdrawn ones, most recent withdrawal first. If several rows cite the same question, the latest citing
 *  row wins. Input order never changes the output.
 */
export function railQuestions(rows: readonly DriverConversationEntry[]): RailQuestion[] {
  const ordered = [...rows].sort(compareThread);
  // Ascending, so a later citing row overwrites an earlier one: the latest wins.
  const citedBy = new Map<string, { message_id: string; created_at: string; text: string }>();
  // The FIRST withdrawal of a question is the one that took it back.
  const withdrawnBy = new Map<string, { message_id: string; created_at: string; reason: string }>();
  for (const row of ordered) {
    for (const answer of row.answers ?? []) {
      citedBy.set(answer.message_id + '\u0000' + answer.segment_id, {
        message_id: row.message_id,
        created_at: row.created_at,
        text: row.body,
      });
    }
    for (const withdrawal of row.withdraws ?? []) {
      const key = withdrawal.message_id + '\u0000' + withdrawal.segment_id;
      if (!withdrawnBy.has(key)) withdrawnBy.set(key, { message_id: row.message_id, created_at: row.created_at, reason: withdrawal.reason });
    }
  }
  const open: RailQuestion[] = [];
  const answered: RailQuestion[] = [];
  const withdrawn: RailQuestion[] = [];
  for (const row of ordered) {
    for (const segment of row.segments ?? []) {
      if (segment.kind !== 'question' || !segment.id) continue;
      const citing = citedBy.get(row.message_id + '\u0000' + segment.id) ?? null;
      const taken = citing ? null : withdrawnBy.get(row.message_id + '\u0000' + segment.id) ?? null;
      const question: RailQuestion = {
        message_id: row.message_id,
        subject: row.subject,
        created_at: row.created_at,
        text: segment.text,
        segment_id: segment.id,
        answered: citing,
        withdrawn: taken,
      };
      (citing ? answered : taken ? withdrawn : open).push(question);
    }
  }
  // Stable sort: questions answered by the same row keep their thread order.
  answered.sort((a, b) => compareThread(b.answered!, a.answered!));
  withdrawn.sort((a, b) => compareThread(b.withdrawn!, a.withdrawn!));
  return [...open, ...answered, ...withdrawn];
}
