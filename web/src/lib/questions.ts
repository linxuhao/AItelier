/** The questions of a conversation, for the questions rail.
 *
 *  A `question` segment is OPEN until a later message cites it in its
 *  `answers`. The rail keeps every question, open or answered, so the user can
 *  look back at what they answered; an info block never appears there. The rail
 *  is what keeps a question from being swept off screen by the thread's newer
 *  messages.
 *
 *  Two pieces, and only one of them decides anything:
 *
 *  * `railQuestions` is the RULE, pure on purpose, and the single place that
 *    decides which question is open or answered, and by which row;
 *    `openQuestions` is derived from it and decides nothing itself;
 *  * `readConversationRows` is the row source that rule needs. The rail has to
 *    see the WHOLE conversation, not the page the thread happens to have
 *    loaded: a question followed by thirty later info messages is still open,
 *    and it is exactly the one the rail exists to keep in view.
 *
 *  Both are testable without a DOM.
 */
import type { DriverConversation, DriverConversationEntry } from './api';
import { mergeThread } from './conversationThread';

export { railQuestions } from './questionRule';
export type { OpenQuestion, RailQuestion } from './questionRule';
import { railQuestions, type OpenQuestion } from './questionRule';

/** The questions the user still owes an answer to: `railQuestions` with the
 *  answered ones taken out, in the thread's `(created_at, message_id)` order,
 *  oldest first. It is derived from `railQuestions`, so there is one place that
 *  decides "open" and this is only a filter over it.
 *
 *  Every guarantee of `railQuestions` holds here too: the id is required, a
 *  reply that only sets `reply_to_message_id` cites nothing, and a non-question
 *  segment is never listed. What it adds is that a question some reply has
 *  already answered, or its asker withdrew, is not listed.
 */
export function openQuestions(rows: readonly DriverConversationEntry[]): OpenQuestion[] {
  return railQuestions(rows)
    .filter(question => question.answered === null && question.withdrawn === null)
    .map(question => ({
      message_id: question.message_id,
      subject: question.subject,
      created_at: question.created_at,
      text: question.text,
      segment_id: question.segment_id,
    }));
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
 *  `railQuestions` above remains the only thing that decides what is open or
 *  answered.
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
