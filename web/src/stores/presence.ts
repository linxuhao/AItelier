// Who is watching right now — a projection of the singleton SSE stream,
// beside connectionStore/notifications which project the same stream.
//
// A store, not per-view state: the badge lives in the AppBar (visible on
// every page), and per-view copies were exactly how the seed-fetch and the
// event handler would have drifted apart on the second page that wanted it.
import { writable } from 'svelte/store';
import { on } from '../lib/sse';
import { getConnections } from '../lib/api';

export interface PresenceState {
  total: number;
  authenticated: number;
}

export const presenceStore = writable<PresenceState | null>(null);

let sawEvent = false;

on('presence', (ev: Record<string, unknown>) => {
  sawEvent = true;
  presenceStore.set({
    total: Number(ev.total ?? 0),
    authenticated: Number(ev.authenticated ?? 0),
  });
});

// Seed once: the SSE stream only speaks on connect/disconnect, so a page
// opened into a quiet deployment would otherwise show nothing for hours.
// The seed must never clobber a FRESHER event that raced ahead of it.
// Through the typed wrapper, not raw fetch: it carries the 10s timeout and
// the one-shot retry — a transient blip at page load would otherwise leave
// the badge unseeded for hours on a quiet deployment.
getConnections()
  .then((j) => {
    if (j && !sawEvent) {
      presenceStore.set({ total: j.total, authenticated: j.authenticated });
    }
  })
  .catch(() => {});
