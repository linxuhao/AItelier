import { writable } from 'svelte/store';

export interface AuthState {
  canWrite: boolean;
  email: string | null;
  permissionResolved: boolean;
  lang: string | null;
  /** Where to sign in, as the DEPLOYMENT declares it. Empty string when this
   *  deployment has no sign-in route — the UI must then offer none, rather
   *  than a link to an Access application that may not exist. */
  signinUrl: string;
  /** Set when a credential was presented and refused, which is a different
   *  state from having none: the reader did everything right and is still a
   *  reader, and only the server can tell them so. */
  authError: string | null;
}

/** Default: fail-closed (canWrite=false) until /api/me resolves. */
export const authStore = writable<AuthState>({
  canWrite: false,
  email: null,
  permissionResolved: false,
  lang: null,
  signinUrl: '',
  authError: null,
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
