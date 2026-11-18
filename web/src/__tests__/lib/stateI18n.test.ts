// The three language blocks are one dictionary indexed by key: a key that
// exists in one block and not in another does not fail loudly anywhere — the
// page falls back to English and renders one untranslated sentence inside a
// Chinese or French view. That is how `workingNotes` reached production: it was
// in `en` only, and no gate compared the three blocks.
//
// So the durable assertion here is the SET equality, not the presence of any
// particular key: it fails the moment the next key is added to one block alone.
import { describe, expect, it } from 'vitest';
import { en, fr, st, zh } from '../../lib/stateI18n.svelte';

const BLOCKS = { en, zh, fr } as const;

describe('stateI18n blocks', () => {
  it('have identical key sets in all three languages', () => {
    const keys = (b: Record<string, string>) => Object.keys(b).sort();
    const [enKeys, zhKeys, frKeys] = [keys(en), keys(zh), keys(fr)];

    // Report the asymmetry by name so a failure says "you forgot to translate
    // `x` in `zh`", not "arrays differ".
    const missing: Record<string, string[]> = {};
    const enSet = new Set(enKeys);
    const zhSet = new Set(zhKeys);
    const frSet = new Set(frKeys);
    const only = (a: Set<string>, b: Set<string>, c: Set<string>) =>
      [...a].filter(k => !b.has(k) || !c.has(k));
    if (only(enSet, zhSet, frSet).length) missing.en = only(enSet, zhSet, frSet);
    if (only(zhSet, enSet, frSet).length) missing.zh = only(zhSet, enSet, frSet);
    if (only(frSet, enSet, zhSet).length) missing.fr = only(frSet, enSet, zhSet);

    expect(missing, `keys missing outside ${JSON.stringify(missing)}`).toEqual({});
    expect(zhKeys).toEqual(enKeys);
    expect(frKeys).toEqual(enKeys);
  });

  it('never ship an empty translation', () => {
    for (const [lang, block] of Object.entries(BLOCKS)) {
      const empty = Object.entries(block)
        .filter(([, value]) => typeof value !== 'string' || value.trim() === '')
        .map(([key]) => key);
      expect(empty, `${lang} has empty values`).toEqual([]);
    }
  });

  it('translate `workingNotes` instead of falling back to English', () => {
    // The regression this test was written for: `workingNotes` existed in `en`
    // only, so a zh/fr page rendered the English sentence through st()'s
    // fallback and nothing went red.
    for (const [lang, block] of Object.entries(BLOCKS)) {
      expect(block.workingNotes, `${lang}.workingNotes`).toBeTruthy();
    }
    expect(zh.workingNotes).not.toBe(en.workingNotes);
    expect(fr.workingNotes).not.toBe(en.workingNotes);
    expect(st('workingNotes')).toBeTruthy();
  });

  it('fall back to the key itself for a key no block has', () => {
    // st()'s last resort (`?? key`) is the reason a missing key is invisible in
    // the UI; pin it so a change of that behaviour is a decision, not a drift.
    expect(st('__no_such_key_1118__')).toBe('__no_such_key_1118__');
  });
});
