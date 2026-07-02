/**
 * Tests for web/src/lib/markdown.ts — safe Markdown-to-HTML conversion.
 *
 * Tests both the normal path (marked + DOMPurify) and the fallback path
 * (escapeHtml when deps are missing or throw).
 *
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { renderMarkdown } from '../src/lib/markdown';

// ── Null/empty handling ────────────────────────────────────────────

describe('renderMarkdown null/empty input', () => {
  it("returns '' for null", () => {
    expect(renderMarkdown(null)).toBe('');
  });

  it("returns '' for undefined", () => {
    expect(renderMarkdown(undefined)).toBe('');
  });

  it("returns '' for empty string", () => {
    expect(renderMarkdown('')).toBe('');
  });
});

// ── Normal rendering path (marked + DOMPurify available) ────────────

describe('renderMarkdown with marked + DOMPurify', () => {
  it('renders simple markdown as HTML', () => {
    const result = renderMarkdown('**bold**');
    expect(result).toContain('<strong>bold</strong>');
  });

  it('renders inline code', () => {
    const result = renderMarkdown('Use `code` here');
    expect(result).toContain('<code>code</code>');
  });

  it('sanitises dangerous HTML in markdown', () => {
    // Marked passes through raw HTML by default, DOMPurify sanitises it
    const result = renderMarkdown('<img src=x onerror=alert(1)>');
    // DOMPurify strips the onerror attribute
    expect(result).not.toContain('onerror');
    expect(result).not.toContain('alert(1)');
    // The <img> tag may be kept if DOMPurify considers src=x safe, but onerror must be gone
  });

  it('renders paragraphs', () => {
    const result = renderMarkdown('Hello\n\nWorld');
    expect(result).toContain('<p>');
  });
});

// ── Fallback path (marked throws) ───────────────────────────────────

describe('renderMarkdown fallback when marked throws', () => {
  it('falls back to HTML-escaping when marked.parse throws', async () => {
    // NOT vi.mock: it hoists file-wide and broke every normal-path test
    // above (all of them silently exercised the fallback). Spy on the real
    // module for this one test and restore.
    const { marked } = await import('marked');
    const spy = vi.spyOn(marked, 'parse').mockImplementation(() => {
      throw new Error('mock error');
    });
    try {
      const out = renderMarkdown('<script>alert(1)</script>');
      expect(out).not.toContain('<script>');
      expect(out).toContain('&lt;script&gt;');
    } finally {
      spy.mockRestore();
    }
  });
});

// ── Edge cases ──────────────────────────────────────────────────────

describe('renderMarkdown edge cases', () => {
  it('handles plain text', () => {
    const result = renderMarkdown('Just some plain text');
    expect(result).toContain('Just some plain text');
  });

  it('handles text with HTML entities that should be escaped', () => {
    const result = renderMarkdown('x < y && y > z');
    // The < and > should be handled safely
    expect(result).not.toContain('< y');
  });
});
