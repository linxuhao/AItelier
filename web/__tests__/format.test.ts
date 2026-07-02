/**
 * Tests for web/src/lib/format.ts — pure formatting utilities.
 *
 * Migrated from web/js/__tests__/utils.test.js (IIFE → ES module import).
 * All functions are pure; no jsdom needed.
 */
import { describe, it, expect, vi } from 'vitest';
import {
  escapeHtml,
  formatTime,
  formatTokens,
  statusClass,
  statusIcon,
  stepLabel,
  truncate,
  debounce,
  formatTaskProgress,
  parseStatus,
} from '../src/lib/format';

// ── escapeHtml ──────────────────────────────────────────────────────

describe('escapeHtml', () => {
  it('escapes all five HTML-significant characters', () => {
    expect(escapeHtml(`<a href="x" on=''>&`)).toBe(
      '&lt;a href=&quot;x&quot; on=&#39;&#39;&gt;&amp;',
    );
  });

  it('neutralises a script-injection payload', () => {
    const out = escapeHtml('<script>alert(1)</script>');
    expect(out).not.toContain('<script>');
    expect(out).toContain('&lt;script&gt;');
  });

  it("returns '' for null/undefined and coerces non-strings", () => {
    expect(escapeHtml(null)).toBe('');
    expect(escapeHtml(undefined)).toBe('');
    expect(escapeHtml(42 as unknown as string)).toBe('42');
  });
});

// ── formatTime ──────────────────────────────────────────────────────

describe('formatTime', () => {
  it('buckets recent ISO timestamps', () => {
    const ago = (s: number) => new Date(Date.now() - s * 1000).toISOString();
    expect(formatTime(ago(1))).toBe('just now');
    expect(formatTime(ago(30))).toBe('30s ago');
    expect(formatTime(ago(120))).toBe('2m ago');
    expect(formatTime(ago(2 * 3600))).toBe('2h ago');
    expect(formatTime(ago(3 * 86400))).toBe('3d ago');
  });

  it('handles epoch integer (seconds)', () => {
    const epochNow = Math.floor(Date.now() / 1000);
    expect(formatTime(epochNow)).toBe('just now');
    expect(formatTime(epochNow - 30)).toBe('30s ago');
    expect(formatTime(epochNow - 120)).toBe('2m ago');
    expect(formatTime(epochNow - 7200)).toBe('2h ago');
    expect(formatTime(epochNow - 259200)).toBe('3d ago');
  });

  it("returns '' for empty or invalid input", () => {
    expect(formatTime(null)).toBe('');
    expect(formatTime('')).toBe('');
    expect(formatTime('not-a-date')).toBe('');
  });
});

// ── formatTokens ────────────────────────────────────────────────────

describe('formatTokens', () => {
  it('handles small numbers verbatim', () => {
    expect(formatTokens(0)).toBe('0');
    expect(formatTokens(1)).toBe('1');
    expect(formatTokens(999)).toBe('999');
  });

  it('formats thousands as k', () => {
    expect(formatTokens(1000)).toBe('1k');
    expect(formatTokens(1234)).toBe('1.2k');
    expect(formatTokens(999999)).toBe('1000k');
  });

  it('formats millions as M', () => {
    expect(formatTokens(1_000_000)).toBe('1M');
    // Rounding (not truncation) is the spec: the thousands case above
    // requires 999999 → '1000k', which only rounding produces.
    expect(formatTokens(3_456_789)).toBe('3.5M');
  });

  it("returns '' for null/undefined/NaN", () => {
    expect(formatTokens(null)).toBe('');
    expect(formatTokens(undefined)).toBe('');
    expect(formatTokens(NaN)).toBe('');
  });
});

// ── statusClass ─────────────────────────────────────────────────────

describe('statusClass', () => {
  it('maps known statuses', () => {
    expect(statusClass('completed')).toBe('status-ok');
    expect(statusClass('running')).toBe('status-warn');
    expect(statusClass('advancing')).toBe('status-warn');
    expect(statusClass('failed')).toBe('status-err');
  });

  it('falls back for unknown/empty', () => {
    expect(statusClass('nonsense')).toBe('');
    expect(statusClass('')).toBe('');
    expect(statusClass(null)).toBe('');
  });
});

// ── statusIcon ──────────────────────────────────────────────────────

describe('statusIcon', () => {
  it('maps known statuses to icons', () => {
    expect(statusIcon('completed')).toBe('✓');
    expect(statusIcon('running')).toBe('▶');
    expect(statusIcon('failed')).toBe('✗');
    expect(statusIcon('waiting_user_approval')).toBe('⏸');
  });

  it('falls back for unknown/null', () => {
    expect(statusIcon('nonsense')).toBe('?');
    expect(statusIcon(null)).toBe('?');
    expect(statusIcon('')).toBe('?');
  });
});

// ── stepLabel ───────────────────────────────────────────────────────

describe('stepLabel', () => {
  it('maps known step IDs', () => {
    expect(stepLabel('t_impl')).toBe('Implementer');
    expect(stepLabel('1')).toBe('Researcher');
    expect(stepLabel('3')).toBe('PM');
  });

  it('returns ID itself for unknown', () => {
    expect(stepLabel('xyz')).toBe('xyz');
  });

  it("returns '' for null", () => {
    expect(stepLabel(null)).toBe('');
    expect(stepLabel(undefined)).toBe('');
  });
});

// ── truncate ────────────────────────────────────────────────────────

describe('truncate', () => {
  it('leaves short strings untouched', () => {
    expect(truncate('hello', 10)).toBe('hello');
    expect(truncate('hello', 5)).toBe('hello'); // boundary: len == max
  });

  it('appends an ellipsis when over the limit', () => {
    expect(truncate('hello world', 5)).toBe('hello…');
  });

  it('handles null and negative/zero max', () => {
    expect(truncate(null, 5)).toBe('');
    expect(truncate('abc', 0)).toBe('…');
    expect(truncate('abc', -3)).toBe('…');
  });
});

// ── debounce ────────────────────────────────────────────────────────

describe('debounce', () => {
  it('collapses rapid calls into one trailing invocation', () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const d = debounce(fn, 100);
    d();
    d();
    d();
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(100);
    expect(fn).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it('preserves args and this-context of the last call', () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const ctx = { tag: 'ctx' };
    const d = debounce(fn, 50);
    d.call(ctx, 'a');
    d.call(ctx, 'b');
    vi.advanceTimersByTime(50);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn.mock.calls[0]).toEqual(['b']);
    expect(fn.mock.instances[0]).toBe(ctx);
    vi.useRealTimers();
  });
});

// ── formatTaskProgress ──────────────────────────────────────────────

describe('formatTaskProgress', () => {
  it("returns '-' for zero tasks", () => {
    expect(formatTaskProgress({})).toBe('-');
    expect(formatTaskProgress({ task_count: 0 })).toBe('-');
  });

  it('shows completed/total', () => {
    expect(
      formatTaskProgress({ task_count: 5, completed_count: 3 }),
    ).toBe('3/5');
  });

  it('includes running indicator', () => {
    expect(
      formatTaskProgress({
        task_count: 5,
        completed_count: 2,
        running_count: 1,
      }),
    ).toBe('2/5 ▶1');
  });

  it('includes failed indicator', () => {
    expect(
      formatTaskProgress({
        task_count: 5,
        completed_count: 2,
        running_count: 1,
        failed_count: 1,
      }),
    ).toBe('2/5 ▶1 ✗1');
  });
});

// ── parseStatus ─────────────────────────────────────────────────────

describe('parseStatus', () => {
  it('parses compound status with step suffix', () => {
    const r = parseStatus('running:t_impl');
    expect(r.text).toBe('Implementer');
    expect(r.className).toBe('status-warn');
    expect(r.icon).toBe('▶');
  });

  it('parses checkpoint status', () => {
    const r = parseStatus('checkpoint:Review');
    expect(r.text).toBe('Review');
    expect(r.className).toBe('');
    expect(r.icon).toBe('⏸');
  });

  it('handles plain status', () => {
    const r = parseStatus('completed');
    expect(r.text).toBe('completed');
    expect(r.className).toBe('status-ok');
    expect(r.icon).toBe('✓');
  });

  it("returns defaults for null/empty", () => {
    const r = parseStatus(null);
    expect(r.text).toBe('');
    expect(r.className).toBe('');
    expect(r.icon).toBe('?');
  });
});
