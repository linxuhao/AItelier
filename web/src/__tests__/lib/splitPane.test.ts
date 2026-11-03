import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { clampPaneWidth, isNarrow, paneMaxFor, readPaneWidth, writePaneWidth,
  NARROW_BREAKPOINT, PANE_DEFAULT, PANE_MIN, PANE_MAX } from '../../lib/splitPane';

describe('conversation pane width policy', () => {
  it('clamps to the minimum, the default and 60% of the viewport', () => {
    expect(clampPaneWidth(10, 1400)).toBe(PANE_MIN);
    expect(clampPaneWidth(420, 1400)).toBe(PANE_DEFAULT);
    expect(paneMaxFor(1400)).toBe(PANE_MAX);
    expect(paneMaxFor(1024)).toBe(614);
    expect(clampPaneWidth(900, 1024)).toBe(614);
    expect(paneMaxFor(300)).toBe(PANE_MIN);
    expect(clampPaneWidth(Number.NaN, 1024)).toBe(PANE_DEFAULT);
  });

  it('collapses below the narrow breakpoint only', () => {
    expect(isNarrow(NARROW_BREAKPOINT - 1)).toBe(true);
    expect(isNarrow(NARROW_BREAKPOINT)).toBe(false);
    expect(isNarrow(NARROW_BREAKPOINT + 1)).toBe(false);
  });

  describe('persistence', () => {
    let storage: Map<string, string>;
    beforeEach(() => {
      storage = new Map();
      vi.stubGlobal('localStorage', {
        getItem: (k: string) => storage.get(k) ?? null,
        setItem: (k: string, v: string) => { storage.set(k, v); },
      });
    });
    afterEach(() => vi.unstubAllGlobals());

    it('round-trips the chosen width and defaults when nothing is stored', () => {
      expect(readPaneWidth()).toBe(PANE_DEFAULT);
      writePaneWidth(517.6);
      expect(storage.get('aitelier_dag_pane_width')).toBe('518');
      expect(readPaneWidth()).toBe(518);
    });

    it('falls back to the default rather than a broken width', () => {
      storage.set('aitelier_dag_pane_width', 'not-a-number');
      expect(readPaneWidth()).toBe(PANE_DEFAULT);
    });
  });
});
