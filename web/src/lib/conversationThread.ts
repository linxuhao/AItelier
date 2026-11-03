/** Merge and scroll policy for the driver conversation thread.
 *
 *  The thread is read WHILE the driver keeps writing, so the pane re-reads the
 *  newest page on a timer. That refresh is a MERGE and never a replacement: the
 *  reader may have paged further back, and a message already on screen must not
 *  appear twice just because its page was fetched again. Keeping the policy here
 *  (instead of inside the component) is what makes it testable without a DOM.
 */
import type { DriverConversationEntry } from './api';

/** Ascending (oldest-first) thread order.
 *
 *  `created_at` then `message_id` is the exact total order the server pages by -
 *  `ORDER BY created_at DESC, message_id DESC`, reversed for display - so a
 *  merged thread and the server's own order can never disagree. `message_id` is
 *  unique, so the pair is a strict total order and the result is deterministic
 *  even when two rows share a timestamp.
 */
export function compareThreadPosition(a: DriverConversationEntry, b: DriverConversationEntry): number {
  if (a.created_at !== b.created_at) return a.created_at < b.created_at ? -1 : 1;
  if (a.message_id !== b.message_id) return a.message_id < b.message_id ? -1 : 1;
  return 0;
}

/** The newest page merged into what is already rendered, oldest-first.
 *
 *  Identity is `message_id`: an entry already in the thread keeps its own object
 *  (and, because the list is keyed by id, its DOM node) and is never duplicated,
 *  whichever page it arrives on. When the page adds nothing the SAME array is
 *  returned, so a poll that finds no news touches neither state nor the DOM.
 */
export function mergeThread(current: DriverConversationEntry[],
                            incoming: readonly DriverConversationEntry[]): DriverConversationEntry[] {
  const seen = new Set<string>();
  const merged: DriverConversationEntry[] = [];
  for (const message of [...current, ...incoming]) {
    if (!message || seen.has(message.message_id)) continue;
    seen.add(message.message_id);
    merged.push(message);
  }
  // No new id (and no duplicate to clean up): keep the existing array identity.
  if (merged.length === current.length) return current;
  return merged.sort(compareThreadPosition);
}

/** How close to the newest message still counts as "following the thread".
 *  A few pixels of slack absorb sub-pixel rounding and a scroll that landed one
 *  row short. Anything further up is a reader who deliberately left the bottom
 *  and must not be pulled back by a refresh.
 */
export const NEWEST_SLACK = 24;

export interface ScrollBox { scrollTop: number; clientHeight: number; scrollHeight: number }

/** Is the scroll box parked at (or within `slack` of) its newest message?
 *  A box without measurable geometry - jsdom, or a thread that is not laid out
 *  yet - answers `true`: with nothing to read above, following the newest
 *  message is the only sensible behaviour.
 */
export function isAtNewest(box: ScrollBox | null | undefined, slack = NEWEST_SLACK): boolean {
  if (!box) return true;
  const distance = box.scrollHeight - box.scrollTop - box.clientHeight;
  return !Number.isFinite(distance) || distance <= slack;
}

/** Should a background poll advance the persisted read marker?
 *
 *  The rule this encodes: a poll may only mark read what the reader could
 *  actually be reading right now. That needs all three of:
 *
 *  * the tab is visible - a hidden tab has seen nothing;
 *  * the thread is parked at its newest end - a reader scrolled up has not
 *    reached what sits below;
 *  * the newest rendered message IS the newest message on the server - if the
 *    merge did not put the new message at the end, the reader cannot be looking
 *    at it.
 *
 *  Extracted as a pure function so all three refusals can be asserted directly;
 *  the component's scroll geometry cannot be controlled reliably in jsdom.
 */
export function shouldMarkReadOnPoll(input: {
  hidden: boolean | undefined;
  atNewest: boolean | null | undefined;
  newestRenderedId: string | null | undefined;
  newestOnServerId: string | null | undefined;
}): boolean {
  if (input.hidden) return false;
  if (input.atNewest === false) return false;
  if (!input.newestRenderedId || !input.newestOnServerId) return false;
  return input.newestRenderedId === input.newestOnServerId;
}
