/** The questions the user still owes an answer to, for the questions rail.
 *
 *  A `question` segment is OPEN until a later message cites it in its
 *  `answers`. The rail lists only open questions, so an info block (or a
 *  question already answered) never appears there: the rail is what keeps a
 *  question from being swept off screen by the thread's newer messages.
 *
 *  Two pieces, and only one of them decides anything:
 *
 *  * `openQuestions` is the RULE, pure on purpose, and the single producer of
 *    "open" anywhere in the system;
 *  * `readConversationRows` is the row source that rule needs. The rail has to
 *    see the WHOLE conversation, not the page the thread happens to have
 *    loaded: a question followed by thirty later info messages is still open,
 *    and it is exactly the one the rail exists to keep in view.
 *
 *  Both are testable without a DOM.
 */
import type { DriverConversation, DriverConversationEntry } from './api';
import { mergeThread } from './conversationThread';

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

/** Read one page of a conversation, newest end first, as the dashboard reads it. */
export type ConversationPageReader =
  (cursor: string | null, limit: number) => Promise<DriverConversation>;

/** One page of the walk. 200 is the route's own maximum (`limit: le=200`), so
 *  this is the fewest requests a full read can take. */
export const QUESTIONS_PAGE_LIMIT = 200;

/** How many pages the walk may read: 2000 rows. That is the bound, and it is
 *  deliberately far past any conversation a person answers by hand - a real
 *  thread of a thousand rows is already extraordinary. The bound exists so a
 *  pathological log cannot make opening the pane an unbounded download; when it
 *  is reached the walk reports `complete: false` and the rail SAYS it may be
 *  missing older questions rather than pretending to be complete. */
export const QUESTIONS_MAX_PAGES = 10;

/** A conversation read for the rail: the rows found, and whether the walk
 *  reached the beginning of the conversation.
 *
 *  `rows` is oldest-first and de-duplicated by `message_id` (a page boundary can
 *  shift while new messages arrive, and a row must never appear twice).
 *
 *  `complete: false` means "these rows are not the whole conversation": the walk
 *  stopped at `maxPages` while the server still had older rows, which is
 *  reported instead of silently truncating, because an older unanswered
 *  question is exactly what the rail exists to keep visible. A read that THROWS
 *  is not caught here either - the caller keeps the rows it already has and
 *  decides how to say they are incomplete.
 *
 *  `readPage` is INJECTED rather than imported so this module stays free of the
 *  HTTP layer (and of any second copy of the open rule): it is a row walk, and
 *  `openQuestions` above remains the only thing that decides what is open.
 */
export async function readConversationRows(
  readPage: ConversationPageReader,
  options: { from?: string | null; pageLimit?: number; maxPages?: number } = {},
): Promise<{ rows: DriverConversationEntry[]; complete: boolean }> {
  const pageLimit = options.pageLimit ?? QUESTIONS_PAGE_LIMIT;
  const maxPages = options.maxPages ?? QUESTIONS_MAX_PAGES;
  let rows: DriverConversationEntry[] = [];
  let cursor = options.from ?? null;
  for (let page = 0; page < maxPages; page++) {
    const result = await readPage(cursor, pageLimit);
    rows = mergeThread(rows, result.messages);
    cursor = result.next_cursor;
    // The cursor is what the next read would use; without one there is nothing
    // older to ask for. `has_more` disagreeing with a present cursor is not a
    // reason to stop - the cursor is the position, so it wins.
    if (!cursor) return { rows, complete: true };
  }
  return { rows, complete: false };
}
