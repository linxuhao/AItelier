import { writable } from 'svelte/store';

export interface AuthState {
  canWrite: boolean;
  email: string | null;
  permissionResolved: boolean;
  lang: string | null;
}

/** Default: fail-closed (canWrite=false) until /api/me resolves. */
export const authStore = writable<AuthState>({
  canWrite: false,
  email: null,
  permissionResolved: false,
  lang: null,
});

/**
 * Update auth state from an /api/me response (or fallback).
 * Accepts optional partial data so callers can supply either the full
 * response or just specific fields (e.g. on error fallback).
 * Always sets permissionResolved:true so the app un-gates write affordances.
 */
export function setAuth(data: Partial<AuthState>): void {
  authStore.update(prev => ({ ...prev, ...data, permissionResolved: true }));
}
