/**
 * AItelier Markdown Renderer — safe Markdown-to-HTML conversion.
 *
 * Ported from web/js/utils.js renderMarkdown (IIFE → TypeScript ES module).
 * Uses `marked.parse()` then `DOMPurify.sanitize()` for XSS protection.
 * Falls back to HTML-escaped plain text when either dependency is absent.
 */

import { marked } from 'marked';
import createDOMPurify from 'dompurify';
import { escapeHtml } from './format';

// dompurify's default export is an initialized instance only when a global
// window existed at module-eval time (browser bundle). Under vitest/SSR it
// is an UNBOUND FACTORY — `.sanitize` is undefined, so every render threw
// and silently fell back to escaped plain text. Bind it to the current
// window explicitly; with no DOM at all, renderMarkdown falls back safely.
type Sanitizer = { sanitize(html: string): string };
const DOMPurify: Sanitizer | null =
  typeof (createDOMPurify as unknown as Sanitizer).sanitize === 'function'
    ? (createDOMPurify as unknown as Sanitizer)
    : typeof window !== 'undefined'
      ? (createDOMPurify as unknown as (w: Window) => Sanitizer)(window)
      : null;

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
    return DOMPurify.sanitize(html);
  } catch {
    // Fallback: escape HTML entities
    return escapeHtml(textStr);
  }
}
