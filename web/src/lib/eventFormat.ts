/**
 * SSE pipeline-event formatter.
 *
 * Maps a raw SSE event to a { icon, text, detail, severity } notification, or
 * null to skip it. Restores the detail the old (pre-Svelte) frontend showed —
 * human step names, error text, checkpoint labels, agent messages, and
 * written-file lists — instead of the bare event-type string. Mirrors the CLI
 * notification panel (cli/tui/notifications.py).
 */

import { stepLabel } from './format';

export type Severity = 'error' | 'warning' | 'success' | 'info';

export interface FormattedEvent {
  icon: string;
  text: string;
  detail: string;
  severity: Severity;
}

function _truncate(s: unknown, n: number): string {
  const str = typeof s === 'string' ? s : '';
  return str.length > n ? str.slice(0, n) + '…' : str;
}

/**
 * Format a raw SSE event object into a panel entry, or null to drop it.
 */
export function formatEvent(event: Record<string, unknown>): FormattedEvent | null {
  const type = (event.type as string) || '';
  const stepId = (event._step_id as string) || (event.step_id as string) || (event.step as string) || '';
  const step = stepLabel(stepId);
  const files = Array.isArray(event.files) ? (event.files as string[]) : [];
  const filePreview = files.length
    ? files.slice(0, 3).join(', ') + (files.length > 3 ? ', …' : '')
    : '';
  const label = (event.label as string) || 'Checkpoint';

  switch (type) {
    case 'run_started':
    case 'pipeline_started':
      return { icon: '▶', text: 'Pipeline started', detail: '', severity: 'info' };
    case 'step_claimed':
    case 'step_start':
      return { icon: '▶', text: step || 'step', detail: '', severity: 'info' };
    case 'step_completed':
    case 'step_end':
      return { icon: '✓', text: (step || 'step') + ' completed', detail: '', severity: 'success' };
    case 'step_done':
      return {
        icon: '✓',
        text: (step || 'step') + (filePreview ? ' → ' + filePreview : ''),
        detail: files.join(', '),
        severity: 'success',
      };
    case 'files_written':
      return filePreview
        ? { icon: '✎', text: 'Wrote ' + filePreview, detail: files.join(', '), severity: 'info' }
        : null;
    case 'step_timeout':
      return { icon: '⏰', text: (step || 'step') + ' timed out', detail: '', severity: 'warning' };
    case 'step_failed': {
      const err = _truncate(event.error ?? event.reason, 100);
      return {
        icon: '✗',
        text: (step || 'step') + (err ? ': ' + err : ' failed'),
        detail: (event.error as string) || '',
        severity: 'error',
      };
    }
    case 'checkpoint_reached':
    case 'checkpoint_paused':
      return { icon: '⏸', text: label + ' — awaiting review', detail: '', severity: 'warning' };
    case 'checkpoint_resolved':
    case 'checkpoint_approved':
      return { icon: '✓', text: label + ' ' + ((event.action as string) || 'approved'), detail: '', severity: 'success' };
    case 'checkpoint_rejected':
    case 'step_checkpoint_rejected':
      return { icon: '↺', text: label + ' rejected — redo', detail: '', severity: 'warning' };
    case 'agent_message': {
      const content = _truncate(event.content, 140);
      if (!content) return null;
      const lvl = (event.level as string) || 'info';
      const icon = lvl === 'milestone' ? '★' : lvl === 'warning' ? '⚠' : 'ℹ';
      const severity: Severity = lvl === 'warning' ? 'warning' : 'info';
      return { icon, text: content, detail: (event.content as string) || '', severity };
    }
    case 'project_completed':
      return { icon: '✓', text: 'Project completed', detail: '', severity: 'success' };
    case 'project_failed':
    case 'run_failed': {
      const reason = _truncate(event.reason ?? event.error, 100);
      return {
        icon: '✗',
        text: 'Project failed' + (reason ? ': ' + reason : ''),
        detail: (event.reason as string) || '',
        severity: 'error',
      };
    }
    default:
      // Unknown step-scoped event — show a minimal line rather than dropping it.
      if (stepId) return { icon: '·', text: step || stepId, detail: '', severity: 'info' };
      return null;
  }
}
