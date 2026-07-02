import { writable } from 'svelte/store';

export interface ConnectionState {
  connectionOk: boolean;
  reconnectAttempt: number;
}

export const connectionStore = writable<ConnectionState>({
  connectionOk: true,
  reconnectAttempt: 0,
});

/** Mark connection as established and reset reconnect attempt counter. */
export function setConnected(): void {
  connectionStore.set({ connectionOk: true, reconnectAttempt: 0 });
}

/** Mark connection as lost (does not reset reconnect counter). */
export function setDisconnected(): void {
  connectionStore.update(prev => ({ ...prev, connectionOk: false }));
}

/** Increment reconnect attempt counter (called by connection monitoring). */
export function incrementReconnect(): void {
  connectionStore.update(prev => ({ ...prev, reconnectAttempt: prev.reconnectAttempt + 1 }));
}
