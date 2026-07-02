/**
 * Tests for web/src/stores/connection.ts — connection state store.
 *
 * @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import {
  connectionStore,
  setConnected,
  setDisconnected,
  incrementReconnect,
} from '../src/stores/connection';

describe('connectionStore', () => {
  beforeEach(() => {
    // Reset to initial state
    connectionStore.set({ connectionOk: true, reconnectAttempt: 0 });
  });

  it('has correct initial state', () => {
    const state = get(connectionStore);
    expect(state.connectionOk).toBe(true);
    expect(state.reconnectAttempt).toBe(0);
  });

  it('setConnected resets to connected state', () => {
    connectionStore.set({ connectionOk: false, reconnectAttempt: 3 });
    setConnected();
    const state = get(connectionStore);
    expect(state.connectionOk).toBe(true);
    expect(state.reconnectAttempt).toBe(0);
  });

  it('setDisconnected marks connection as lost', () => {
    setDisconnected();
    const state = get(connectionStore);
    expect(state.connectionOk).toBe(false);
  });

  it('setDisconnected preserves reconnectAttempt', () => {
    connectionStore.set({ connectionOk: true, reconnectAttempt: 5 });
    setDisconnected();
    const state = get(connectionStore);
    expect(state.connectionOk).toBe(false);
    expect(state.reconnectAttempt).toBe(5); // preserved
  });

  it('incrementReconnect increments the counter', () => {
    incrementReconnect();
    expect(get(connectionStore).reconnectAttempt).toBe(1);
    incrementReconnect();
    expect(get(connectionStore).reconnectAttempt).toBe(2);
  });

  it('supports subscribe pattern (Svelte store contract)', () => {
    const values: unknown[] = [];
    const unsub = connectionStore.subscribe((v) => values.push(v));

    expect(values.length).toBe(1);

    setDisconnected();
    expect(values.length).toBe(2);
    expect((values[1] as any).connectionOk).toBe(false);

    unsub();
  });
});
