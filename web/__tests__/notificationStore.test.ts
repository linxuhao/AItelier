/**
 * Tests for web/src/stores/notifications.ts — SSE notification buffer.
 *
 * @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import {
  notificationStore,
  addNotification,
  clearNotifications,
  type NotificationEntry,
} from '../src/stores/notifications';

function makeEntry(overrides: Partial<NotificationEntry> = {}): NotificationEntry {
  return {
    id: '1',
    type: 'test',
    message: 'Test notification',
    timestamp: Date.now(),
    ...overrides,
  };
}

describe('notificationStore', () => {
  beforeEach(() => {
    notificationStore.set([]);
  });

  it('has initial state of empty array', () => {
    expect(get(notificationStore)).toEqual([]);
  });

  it('addNotification prepends entries', () => {
    const entry1 = makeEntry({ id: '1', message: 'First' });
    const entry2 = makeEntry({ id: '2', message: 'Second' });

    addNotification(entry1);
    addNotification(entry2);

    const state = get(notificationStore);
    expect(state).toHaveLength(2);
    expect(state[0].message).toBe('Second'); // most recent first
    expect(state[1].message).toBe('First');
  });

  it('drops oldest entries when buffer exceeds MAX_NOTIFICATIONS', () => {
    // Add 101 entries
    for (let i = 0; i < 101; i++) {
      addNotification(makeEntry({ id: String(i), message: `Entry ${i}` }));
    }

    const state = get(notificationStore);
    expect(state).toHaveLength(100);
    // The oldest entry (id '0') should be dropped
    const ids = state.map((e) => e.id);
    expect(ids).not.toContain('0');
    // The newest entry should be at index 0
    expect(ids[0]).toBe('100');
  });

  it('clearNotifications resets to empty array', () => {
    addNotification(makeEntry());
    addNotification(makeEntry());
    expect(get(notificationStore)).toHaveLength(2);

    clearNotifications();
    expect(get(notificationStore)).toEqual([]);
  });

  it('supports subscribe pattern', () => {
    const values: unknown[] = [];
    const unsub = notificationStore.subscribe((v) => values.push(v));

    expect(values.length).toBe(1);
    expect((values[0] as any[])).toEqual([]);

    addNotification(makeEntry({ id: 'a' }));
    expect(values.length).toBe(2);
    expect((values[1] as any[])).toHaveLength(1);

    unsub();
  });
});


describe('bell unread counter', () => {
  it('increments while the panel is closed and resets on open', async () => {
    const { notificationStore, notifPanelOpen, notifUnread, addNotification, clearNotifications } =
      await import('../src/stores/notifications');
    clearNotifications();
    notifPanelOpen.set(false);

    addNotification({ id: 'u1', type: 'info', message: 'a', timestamp: 1 });
    addNotification({ id: 'u2', type: 'info', message: 'b', timestamp: 2 });
    expect(get(notifUnread)).toBeGreaterThanOrEqual(2);

    notifPanelOpen.set(true);
    expect(get(notifUnread)).toBe(0);

    // While open, arriving events do not accumulate unread
    addNotification({ id: 'u3', type: 'info', message: 'c', timestamp: 3 });
    expect(get(notifUnread)).toBe(0);
  });
});
