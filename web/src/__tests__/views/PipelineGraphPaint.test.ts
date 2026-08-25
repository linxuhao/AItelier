/**
 * The graph must never paint from a CSS variable that does not exist.
 *
 * Pico's classless build (the one app.css loads) ships no `--pico-color-<hue>-<n>`
 * scale at all, so every reference to one in this codebase has only ever been
 * resolved by its fallback. In `background` that is survivable: an undefined var
 * makes the declaration invalid at computed-value time and the property falls
 * back to its INITIAL value -- transparent -- so a missed fallback is merely
 * invisible, which is why nobody noticed. In SVG it is not survivable: `fill`'s
 * initial value is BLACK. Two rules here shipped without a fallback and painted
 * every tool and addon node solid black over its own label.
 *
 * This reads the component source rather than the rendered DOM because jsdom
 * does not resolve custom properties, so a render test cannot see the bug.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// vitest runs from the web/ root (vitest.config.js), and jsdom's import.meta.url
// is not a file: URL, so resolve from cwd rather than from this module.
const SOURCE = readFileSync(
  resolve(process.cwd(), 'src/views/PipelineGraph.svelte'), 'utf-8');
const STYLE = SOURCE.slice(SOURCE.lastIndexOf('<style>'));

/** Every `var(--x)` in a paint declaration, with whether it has a fallback. */
function paintVars(): Array<{ prop: string; name: string; hasFallback: boolean }> {
  const out: Array<{ prop: string; name: string; hasFallback: boolean }> = [];
  const decl = /(fill|stroke|background|color|border-color)\s*:\s*([^;}]+)[;}]/g;
  let m: RegExpExecArray | null;
  while ((m = decl.exec(STYLE)) !== null) {
    const inner = /var\(\s*(--[\w-]+)\s*(,)?/g;
    let v: RegExpExecArray | null;
    while ((v = inner.exec(m[2])) !== null) {
      out.push({ prop: m[1], name: v[1], hasFallback: v[2] === ',' });
    }
  }
  return out;
}

describe('PipelineGraph paint declarations', () => {
  it('never references the pico colour scale, which this build does not ship', () => {
    const phantom = paintVars().filter((v) => /^--pico-color-\w+-\d+$/.test(v.name));
    expect(phantom.map((v) => `${v.prop}: ${v.name}`)).toEqual([]);
  });

  it('gives every paint variable a fallback', () => {
    // A local `--g-*` token is defined in this same block, so it always
    // resolves; anything inherited from outside needs a literal to fall back to.
    const bare = paintVars().filter(
      (v) => !v.hasFallback && !v.name.startsWith('--g-'),
    );
    expect(bare.map((v) => `${v.prop}: ${v.name}`)).toEqual([]);
  });

  it('defines every --g- token it paints with', () => {
    const defined = new Set(
      [...STYLE.matchAll(/^\s*(--g-[\w-]+)\s*:/gm)].map((m) => m[1]),
    );
    const used = new Set(paintVars().filter((v) => v.name.startsWith('--g-'))
      .map((v) => v.name));
    expect([...used].filter((n) => !defined.has(n))).toEqual([]);
  });

  it('scopes the palette to a wrapper the whole component lives inside', () => {
    // Tokens defined on `.pg` only reach the SVG if `.pg` actually wraps it.
    expect(STYLE).toMatch(/\.pg\s*\{/);
    expect(SOURCE).toContain('<div class="pg">');
  });
});
