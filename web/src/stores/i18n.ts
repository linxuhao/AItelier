/**
 * Persistent i18n store: lang from localStorage, fallback navigator.language.
 * Also syncs to the backend via POST /api/settings/user/language.
 */
import { writable, get } from 'svelte/store';
import { authStore } from './auth';
import { setUserLang } from '../lib/api';

const LS_KEY = 'aitelier_lang';

function browserLang(): string {
  if (typeof navigator !== 'undefined' && navigator.language) {
    return navigator.language.slice(0, 10); // e.g. "zh-CN", "en-US"
  }
  return 'en';
}

function initialLang(): string {
  // Guard the METHOD, not just the object: jsdom (and some embedded webviews)
  // expose a partial `localStorage` whose getItem isn't a function, so a bare
  // `typeof localStorage !== 'undefined'` check still throws here — at module
  // load, via `langStore = writable(initialLang())`, taking down every view
  // that imports i18n. try/catch keeps a hostile storage from breaking boot.
  try {
    if (typeof localStorage !== 'undefined'
        && typeof localStorage.getItem === 'function') {
      const stored = localStorage.getItem(LS_KEY);
      if (stored) return stored;
    }
  } catch {
    /* storage unavailable/partial — fall back to the browser language */
  }
  return browserLang();
}

export const langStore = writable<string>(initialLang());

/** Set language locally + persist to localStorage + sync to backend. */
export async function setLang(lang: string): Promise<void> {
  lang = lang.slice(0, 10);
  langStore.set(lang);
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(LS_KEY, lang);
  }
  authStore.update(s => ({ ...s, lang }));
  // Sync to backend (fire-and-forget — no-op if offline/unauthenticated)
  try {
    await setUserLang(lang);
  } catch {
    // silent — localStorage is authoritative for the FE
  }
}

/** Sync browser language to backend on first visit, if none stored. */
export async function syncInitialLang(): Promise<void> {
  const lang = get(langStore);
  const $auth = get(authStore);
  // If we already have a stored lang or backend lang, skip
  if (typeof localStorage !== 'undefined' && localStorage.getItem(LS_KEY)) return;
  if ($auth.lang) return;
  // First visit: persist browser lang
  await setLang(lang);
}
