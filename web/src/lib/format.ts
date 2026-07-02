/**
 * AItelier Formatting Utilities — pure formatting functions.
 *
 * Ported from web/js/utils.js (IIFE → TypeScript ES module).
 * All functions are stateless, side-effect-free, and guarded against
 * null/undefined inputs.
 */

// ── Step label map ─────────────────────────────────────────────────

/** Step ID → human-readable label map. */
const _STEP_LABELS: Record<string, string> = {
  '1': 'Researcher',
  '1_review': 'Research Review',
  '2': 'Architect',
  '2_review': 'Architecture Review',
  '3': 'PM',
  '3_review': 'PM Review',
  '5': 'Final Verifier',
  '5_review': 'Final Review',
  '5_test': 'Unit Tests',
  t_plan: 'Task Planner',
  t_plan_review: 'Plan Review',
  t_impl: 'Implementer',
  t_impl_review: 'Impl Review',
  t_verify: 'Verifier',
  t_verify_review: 'Verify Review',
  task_loop: 'Task Loop',
  git_sync_pre: 'Git Sync',
};

// ── Status class map ───────────────────────────────────────────────

const _STATUS_CLASS_MAP: Record<string, string> = {
  completed: 'status-ok',
  running: 'status-warn',
  advancing: 'status-warn',
  failed: 'status-err',
  paused: '',
  planning: '',
  waiting_user_approval: '',
};


// ── Public functions ───────────────────────────────────────────────

/**
 * Convert a timestamp to a human-readable relative time string
 * (e.g. "just now", "2m ago", "1h ago", "3d ago").
 *
 * Accepts either:
 *   - number: Unix epoch seconds
 *   - string: ISO 8601 date string (legacy format)
 *
 * @param value — timestamp (number, string, null, or undefined)
 * @returns relative time string, or "" for invalid input
 */
/**
 * Parse any backend timestamp into Unix epoch SECONDS, or null.
 *
 * The API mixes number epochs, ISO strings, and SQLite "YYYY-MM-DD HH:MM:SS"
 * (UTC, no zone marker) — the last is unparseable by Date in Firefox/Safari
 * and off-by-timezone in Chrome. Doing arithmetic on the raw strings is what
 * made every duration render as NaN.
 */
export function toEpochSeconds(value: unknown): number | null {
  if (value == null || value === '') return null;
  if (typeof value === 'number') {
    if (isNaN(value)) return null;
    return value > 1e12 ? value / 1000 : value; // ms epoch → seconds
  }
  if (typeof value === 'string') {
    let s = value.trim().replace(' ', 'T');
    // SQLite datetimes are UTC without a zone marker — make it explicit.
    if (!/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(s)) s += 'Z';
    const ms = Date.parse(s);
    return isNaN(ms) ? null : ms / 1000;
  }
  return null;
}

export function formatTime(value: number | string | null | undefined): string {
  const epoch = toEpochSeconds(value);
  if (epoch == null) {
    return '';
  }
  const date = new Date(epoch * 1000);
  if (isNaN(date.getTime())) {
    return '';
  }

  const diffSeconds = Math.floor((Date.now() - date.getTime()) / 1000);

  if (diffSeconds < 10) {
    return 'just now';
  }
  if (diffSeconds < 60) {
    return diffSeconds + 's ago';
  }
  if (diffSeconds < 3600) {
    return Math.floor(diffSeconds / 60) + 'm ago';
  }
  if (diffSeconds < 86400) {
    return Math.floor(diffSeconds / 3600) + 'h ago';
  }
  return Math.floor(diffSeconds / 86400) + 'd ago';
}

/**
 * Format a number as a human-readable token count.
 * E.g. 1234 → "1.2k", 3456789 → "3.4M".
 *
 * @param n — token count (number, null, or undefined)
 * @returns formatted string, or "" for invalid input
 */
export function formatTokens(n: number | null | undefined): string {
  if (n == null || isNaN(n)) {
    return '';
  }
  if (n < 1000) {
    return String(n);
  }
  if (n < 1_000_000) {
    const k = n / 1000;
    return (k % 1 === 0 ? k.toFixed(0) : k.toFixed(1).replace(/\.0$/, '')) + 'k';
  }
  const m = n / 1_000_000;
  return (m % 1 === 0 ? m.toFixed(0) : m.toFixed(1).replace(/\.0$/, '')) + 'M';
}

/**
 * Map a pipeline status string to a CSS class name for badge colors.
 *
 * @param status — pipeline status string
 * @returns CSS class name, or "" for unknown/empty status
 */
export function statusClass(status: string | null | undefined): string {
  if (status == null || status === '') {
    return '';
  }
  return _STATUS_CLASS_MAP[status] ?? '';
}

/**
 * Map a prompt-cache hit ratio (0..1) to a colorization CSS class for
 * .cache-inline-badge elements (classes defined globally in app.css).
 *
 * @param hitRatio — cache hit ratio between 0 and 1, or null/undefined
 * @returns CSS class name, or "" when the ratio is unknown
 */
export function cacheBadgeClass(hitRatio: number | null | undefined): string {
  if (hitRatio == null || isNaN(hitRatio)) {
    return '';
  }
  if (hitRatio >= 0.7) return 'cache-badge-high';
  if (hitRatio >= 0.3) return 'cache-badge-mid';
  return 'cache-badge-low';
}

/**
 * Map a step ID to a human-readable label.
 *
 * @param id — step ID (e.g. "t_impl", "3")
 * @returns human-readable label, or the ID itself if unknown, or "" for null
 */
export function stepLabel(id: string | null | undefined): string {
  if (id == null) {
    return '';
  }
  return _STEP_LABELS[id] ?? id;
}

/**
 * Escape special HTML characters in a string, preventing XSS.
 * Replaces: & < > " '
 *
 * @param str — raw text (string, null, or undefined)
 * @returns HTML-escaped string, or "" for null/undefined
 */
export function escapeHtml(str: string | null | undefined): string {
  if (str == null) {
    return '';
  }
  const entityMap: Record<string, string> = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  };
  return String(str).replace(/[&<>"']/g, (m) => entityMap[m]);
}

/**
 * Map a pipeline status string to a Unicode icon character.
 *
 * @param status — pipeline status string
 * @returns Unicode icon character, or "?" for unknown/empty status
 */
export function statusIcon(status: string | null | undefined): string {
  if (status == null || status === '') {
    return '\u003F'; // ?
  }

  const map: Record<string, string> = {
    completed: '\u2713',             // ✓
    running: '\u25B6',               // ▶
    advancing: '\u25B6',             // ▶
    failed: '\u2717',                // ✗
    paused: '\u2717',                // ✗
    pending: '\u25CB',               // ○
    planning: '\u25CB',              // ○
    waiting_user_approval: '\u23F8', // ⏸
    checkpoint: '\u23F8',            // ⏸
  };

  return map[status] ?? '\u003F'; // ?
}

/**
 * Truncate a string to a maximum length, appending an ellipsis ("…")
 * if the original text exceeds the limit.
 *
 * @param text — input text (string, null, or undefined)
 * @param maxLen — maximum allowed length (non-negative)
 * @returns truncated string, or "" for null/undefined
 */
export function truncate(text: string | null | undefined, maxLen: number): string {
  if (text == null) {
    return '';
  }
  const safeMax = Math.max(0, maxLen);
  const str = String(text);
  if (str.length <= safeMax) {
    return str;
  }
  return str.slice(0, safeMax) + '\u2026'; // …
}

/**
 * Create a debounced version of a function.
 * The function is called after `ms` milliseconds have elapsed since
 * the last invocation (standard "trailing" debounce pattern).
 *
 * @param fn — function to debounce
 * @param ms — debounce delay in milliseconds
 * @returns debounced function (preserves `this` context)
 */
export function debounce<T extends (...args: any[]) => any>(
  fn: T,
  ms: number,
): (...args: Parameters<T>) => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return function (this: unknown, ...args: Parameters<T>) {
    if (timer !== null) {
      clearTimeout(timer);
    }
    timer = setTimeout(() => {
      fn.apply(this, args);
      timer = null;
    }, ms);
  };
}

/**
 * Format task progress summary string for a project.
 * Produces strings like "3/5 ▶1" or "2/2" or "-" for zero tasks.
 *
 * @param project — project object with task_count, completed_count, running_count, failed_count
 * @returns formatted progress string
 */
export function formatTaskProgress(project: Record<string, unknown>): string {
  const total = (project.task_count as number) || 0;
  const completed = (project.completed_count as number) || 0;
  const running = (project.running_count as number) || 0;
  const failed = (project.failed_count as number) || 0;

  if (total === 0) return '-';

  const parts: string[] = [];
  parts.push(completed + '/' + total);
  if (running > 0) parts.push('\u25B6' + running);
  if (failed > 0) parts.push('\u2717' + failed);
  return parts.join(' ');
}

/**
 * Parse a compound status string into display-friendly parts.
 * Handles formats like "running:t_impl", "checkpoint:Review", "completed", null, etc.
 *
 * @param status — raw status string or null/undefined
 * @returns object with text, className (CSS class name), and icon (Unicode character)
 */
export function parseStatus(status: string | null | undefined): { text: string; className: string; icon: string } {
  if (!status) return { text: '', className: '', icon: '\u003F' };

  const colonIdx = status.indexOf(':');
  const baseStatus = colonIdx >= 0 ? status.slice(0, colonIdx) : status;
  const suffix = colonIdx >= 0 ? status.slice(colonIdx + 1) : '';

  const icon = statusIcon(baseStatus);
  const className = statusClass(baseStatus);

  let text = '';
  if (baseStatus === 'running' && suffix) {
    text = stepLabel(suffix) || suffix;
  } else if (baseStatus === 'checkpoint' && suffix) {
    text = suffix;
  } else if (baseStatus === 'failed' && suffix) {
    text = suffix;
  } else {
    text = baseStatus;
  }

  return { text, className, icon };
}
