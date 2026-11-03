import { describe, it, expect } from 'vitest';
import { compareThreadPosition, isAtNewest, mergeThread, NEWEST_SLACK } from '../../lib/conversationThread';

// The thread refresh MERGES the newest page into what is rendered. These are the
// invariants that make that safe: one row per message_id, the server's own
// oldest-first order, and no state churn when a poll finds nothing new.
const entry = (id: string, created: string) => ({
  message_id: id, thread_id: id, project_id: 'alpha',
  sender_kind: 'driver' as const, sender_driver_id: 'dsh-alpha',
  subject: id, body: '', refs: {}, reply_to_message_id: null, created_at: created,
});
const at = (minute: number) => '2026-11-03T10:' + String(minute).padStart(2, '0') + ':00+00:00';
const ids = (rows: {message_id: string}[]) => rows.map(row => row.message_id);

describe('merging the newest page into an open thread', () => {
  it('adds a message that arrived later and keeps oldest-first order', () => {
    const rendered = [entry('m1', at(1)), entry('m2', at(2))];
    // The API pages newest-first; the thread reads oldest-first.
    const page = [entry('m3', at(3)), entry('m2', at(2))];
    expect(ids(mergeThread(rendered, page))).toEqual(['m1', 'm2', 'm3']);
  });

  it('never duplicates a message that was already rendered', () => {
    const rendered = [entry('m1', at(1)), entry('m2', at(2))];
    const page = [entry('m2', at(2)), entry('m1', at(1))];
    expect(ids(mergeThread(rendered, page))).toEqual(['m1', 'm2']);
    // ... even when the page repeats a row inside itself.
    expect(ids(mergeThread(rendered, [entry('m2', at(2)), entry('m2', at(2))]))).toEqual(['m1', 'm2']);
  });

  it('keeps an already-rendered row object, so its DOM node is not rebuilt', () => {
    const first = entry('m1', at(1));
    const rendered = [first, entry('m2', at(2))];
    const merged = mergeThread(rendered, [entry('m2', at(2)), entry('m3', at(3))]);
    expect(merged[0]).toBe(first);
    expect(merged.find(row => row.message_id === 'm2')).toBe(rendered[1]);
  });

  it('returns the same array when the page adds nothing, so a quiet poll touches no state', () => {
    const rendered = [entry('m1', at(1)), entry('m2', at(2))];
    expect(mergeThread(rendered, [entry('m2', at(2)), entry('m1', at(1))])).toBe(rendered);
    expect(mergeThread(rendered, [])).toBe(rendered);
  });

  it('keeps pages loaded further back when the newest page is merged on top of them', () => {
    // The reader clicked "load earlier": m0 is older than the newest page's window.
    const rendered = [entry('m0', at(0)), entry('m1', at(1)), entry('m2', at(2))];
    const merged = mergeThread(rendered, [entry('m4', at(4)), entry('m3', at(3)), entry('m2', at(2))]);
    expect(ids(merged)).toEqual(['m0', 'm1', 'm2', 'm3', 'm4']);
  });

  it('orders ties by message_id, the second half of the server keyset', () => {
    const same = at(5);
    expect(compareThreadPosition(entry('a', same), entry('b', same))).toBeLessThan(0);
    expect(compareThreadPosition(entry('b', same), entry('a', same))).toBeGreaterThan(0);
    expect(compareThreadPosition(entry('a', same), entry('a', same))).toBe(0);
    expect(ids(mergeThread([], [entry('b', same), entry('a', same)]))).toEqual(['a', 'b']);
  });
});

describe('deciding whether the reader is following the newest message', () => {
  const box = (scrollTop: number, scrollHeight = 1000, clientHeight = 300) =>
    ({scrollTop, scrollHeight, clientHeight});

  it('is true only within a few pixels of the bottom', () => {
    expect(isAtNewest(box(700))).toBe(true);                       // exactly at the bottom
    expect(isAtNewest(box(700 - NEWEST_SLACK))).toBe(true);        // rounding slack
    expect(isAtNewest(box(700 - NEWEST_SLACK - 1))).toBe(false);   // reading older messages
    expect(isAtNewest(box(0))).toBe(false);                        // scrolled to the top
  });

  it('treats a box with no geometry as following the thread', () => {
    // jsdom, or a thread that is not laid out yet: nothing is above the reader.
    expect(isAtNewest(box(0, 0, 0))).toBe(true);
    expect(isAtNewest(null)).toBe(true);
    expect(isAtNewest(undefined)).toBe(true);
  });
});
