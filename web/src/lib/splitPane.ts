/** Width policy for the right-hand conversation pane.
 *
 *  The user's chosen width is a preference, so it is stored raw and only
 *  clamped for display: shrinking the window must not rewrite what they chose
 *  on a wide screen. Below `NARROW_BREAKPOINT` the split collapses to one
 *  column and the width stops applying at all.
 */
export const PANE_WIDTH_KEY = 'aitelier_dag_pane_width';
export const PANE_MIN = 280;
export const PANE_DEFAULT = 420;
export const PANE_MAX = 760;
export const NARROW_BREAKPOINT = 900;

/** Never more than 60% of the viewport: the DAG list keeps the room. */
export function paneMaxFor(viewport: number): number {
  if (!Number.isFinite(viewport) || viewport <= 0) return PANE_MAX;
  return Math.max(PANE_MIN, Math.min(PANE_MAX, Math.round(viewport * 0.6)));
}

export function clampPaneWidth(value: number, viewport: number): number {
  const width = Number.isFinite(value) ? Math.round(value) : PANE_DEFAULT;
  return Math.min(paneMaxFor(viewport), Math.max(PANE_MIN, width));
}

export function isNarrow(viewport: number): boolean {
  return Number.isFinite(viewport) && viewport < NARROW_BREAKPOINT;
}

export function readPaneWidth(): number {
  try {
    const raw = localStorage.getItem(PANE_WIDTH_KEY);
    const value = raw === null ? NaN : Number(raw);
    return Number.isFinite(value) ? value : PANE_DEFAULT;
  } catch { return PANE_DEFAULT; }
}

export function writePaneWidth(width: number): void {
  try { localStorage.setItem(PANE_WIDTH_KEY, String(Math.round(width))); } catch { /* preference only */ }
}
