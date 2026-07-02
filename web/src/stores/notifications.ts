import { writable } from 'svelte/store';

export interface NotificationEntry {
  id: string;
  type: string;
  message: string;
  timestamp: number;
  data?: unknown;
}

/** Buffer limit: keep at most 100 most recent entries. */
const MAX_NOTIFICATIONS = 100;

export const notificationStore = writable<NotificationEntry[]>([]);

/** Whether the notification dropdown panel is open (toggled by the bell). */
export const notifPanelOpen = writable(false);

/** Unread counter for the bell badge — resets when the panel opens. */
export const notifUnread = writable(0);

let _panelOpen = false;
notifPanelOpen.subscribe((open) => {
  _panelOpen = open;
  if (open) notifUnread.set(0);
});

/**
 * Add a notification entry to the buffer.
 * New entries are prepended (most-recent-first).
 * If the buffer exceeds MAX_NOTIFICATIONS, the oldest entries are dropped.
 */
export function addNotification(entry: NotificationEntry): void {
  if (!_panelOpen) notifUnread.update((n) => n + 1);
  notificationStore.update(prev => {
    const next = [entry, ...prev];
    if (next.length > MAX_NOTIFICATIONS) {
      return next.slice(0, MAX_NOTIFICATIONS);
    }
    return next;
  });
}

/** Clear all notifications from the buffer. */
export function clearNotifications(): void {
  notificationStore.set([]);
}
