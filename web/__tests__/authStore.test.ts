/**
 * Tests for web/src/stores/auth.ts — authentication state store.
 *
 * Verifies Svelte writable store contract and setAuth() helper.
 *
 * @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { authStore, setAuth } from '../src/stores/auth';

describe('authStore', () => {
  beforeEach(() => {
    // Reset to initial state
    authStore.set({ canWrite: false, email: null, permissionResolved: false });
  });

  it('has correct initial state', () => {
    const state = get(authStore);
    expect(state.canWrite).toBe(false);
    expect(state.email).toBeNull();
    expect(state.permissionResolved).toBe(false);
  });

  it('updates all fields via setAuth', () => {
    setAuth({ canWrite: true, email: 'user@example.com' });
    const state = get(authStore);
    expect(state.canWrite).toBe(true);
    expect(state.email).toBe('user@example.com');
    expect(state.permissionResolved).toBe(true); // auto-set by setAuth
  });

  it('preserves existing fields on partial update', () => {
    setAuth({ canWrite: true, email: 'a@b.com' });
    // Partial update: only change email
    setAuth({ email: 'new@b.com' });
    const state = get(authStore);
    expect(state.canWrite).toBe(true); // preserved
    expect(state.email).toBe('new@b.com'); // updated
    expect(state.permissionResolved).toBe(true);
  });

  it('supports subscribe pattern (Svelte store contract)', () => {
    const values: unknown[] = [];
    const unsub = authStore.subscribe((v) => values.push(v));

    expect(values.length).toBe(1); // initial value emitted immediately
    expect((values[0] as any).canWrite).toBe(false);

    setAuth({ canWrite: true });
    expect(values.length).toBe(2);
    expect((values[1] as any).canWrite).toBe(true);

    unsub();
  });
});
