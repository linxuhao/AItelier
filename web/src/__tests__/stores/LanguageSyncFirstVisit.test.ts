/**
 * A reader with no identity must not send a per-user write on page load.
 *
 * `syncInitialLang()` runs on every first visit and went through `setLang()`,
 * which POSTs /api/settings/user/language. For an anonymous reader the backend
 * answers 403 write_denied_not_authenticated, so every anonymous visit to the
 * dashboard made one refused mutating request (measured on the served bundle at
 * 935558b710: 13 requests, 1 mutating, and it was the only non-2xx — that POST).
 *
 * A session that has an identity, including a READ-ONLY one, can still store
 * the preference (lib/api.ts setUserLang), and that path has to keep working:
 * deleting the sync would satisfy the read-only test while silently losing a
 * real feature. So both directions are pinned here, and neither one is asserted
 * by mocking the store's own decision — the fetch side effect is what is read.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { authStore } from '../../stores/auth';
import { langStore, setLang, syncInitialLang } from '../../stores/i18n';

const LS_KEY = 'aitelier_lang';

interface Call { url: string; method: string; body: string }

const calls: Call[] = [];

function anonymous(): void {
  authStore.set({ canWrite: false, email: null, permissionResolved: true, lang: null,
                  signinUrl: '', authError: null });
}

function withIdentity(email = 'reader@example.test', canWrite = false): void {
  authStore.set({ canWrite, email, permissionResolved: true, lang: null,
                  signinUrl: '', authError: null });
}

beforeEach(() => {
  calls.length = 0;
  localStorage.clear();
  // Recording spy: every request this module could make lands in `calls`.
  vi.stubGlobal('fetch', vi.fn(async (input: unknown, init: Record<string, unknown> = {}) => {
    calls.push({
      url: String(input),
      method: String(init.method ?? 'GET').toUpperCase(),
      body: String(init.body ?? ''),
    });
    return { ok: true, status: 200, json: async () => ({ lang: 'x' }) };
  }));
});

describe('first-visit language sync', () => {
  it('sends no request at all when nobody is signed in', async () => {
    anonymous();
    await syncInitialLang();

    expect(calls).toEqual([]);
    // Nothing is persisted either: the UI already shows the browser language,
    // and a later sign-in must still get its one chance to record it.
    expect(localStorage.getItem(LS_KEY)).toBeNull();
    expect(get(langStore)).toBe((navigator.language || 'en').slice(0, 10));
  });

  it('still stores the language once when an identity is resolved (read-only included)', async () => {
    withIdentity('reader@example.test', false);
    await syncInitialLang();

    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toBe('/api/settings/user/language');
    expect(JSON.parse(calls[0].body).lang).toBe((navigator.language || 'en').slice(0, 10));
    expect(localStorage.getItem(LS_KEY)).toBe((navigator.language || 'en').slice(0, 10));
  });
});

describe('an explicit language switch', () => {
  it('is applied and remembered without an identity, and sends nothing', async () => {
    anonymous();
    await setLang('ja');

    expect(calls).toEqual([]);
    expect(get(langStore)).toBe('ja');
    expect(localStorage.getItem(LS_KEY)).toBe('ja');
  });

  it('still reaches the backend when an identity is resolved', async () => {
    withIdentity();
    await setLang('ja');

    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toBe('/api/settings/user/language');
    expect(JSON.parse(calls[0].body).lang).toBe('ja');
  });
});
