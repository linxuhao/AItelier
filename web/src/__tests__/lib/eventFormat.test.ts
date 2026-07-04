import { describe, it, expect } from 'vitest';
import { formatEvent } from '../../lib/eventFormat';

describe('formatEvent', () => {
  it('renders a human step label for step completion', () => {
    const f = formatEvent({ type: 'step_completed', _step_id: 't_impl' });
    expect(f).toEqual({
      icon: '✓', text: 'Implementer completed', detail: '', severity: 'success',
    });
  });

  it('previews written files and keeps the full list as detail', () => {
    const f = formatEvent({ type: 'files_written', files: ['a.ts', 'b.ts', 'c.ts', 'd.ts'] });
    expect(f?.text).toBe('Wrote a.ts, b.ts, c.ts, …');
    expect(f?.detail).toBe('a.ts, b.ts, c.ts, d.ts');
    expect(f?.severity).toBe('info');
  });

  it('drops a files_written event with no files', () => {
    expect(formatEvent({ type: 'files_written', files: [] })).toBeNull();
  });

  it('surfaces error text and marks failures as error severity', () => {
    const f = formatEvent({ type: 'step_failed', _step_id: '2', error: 'boom' });
    expect(f?.text).toBe('Architect: boom');
    expect(f?.severity).toBe('error');
  });

  it('labels a reached checkpoint as awaiting review (warning)', () => {
    const f = formatEvent({ type: 'checkpoint_reached', label: 'Plan' });
    expect(f).toEqual({ icon: '⏸', text: 'Plan — awaiting review', detail: '', severity: 'warning' });
  });

  it('respects agent_message level', () => {
    expect(formatEvent({ type: 'agent_message', content: 'hi', level: 'milestone' })?.icon).toBe('★');
    expect(formatEvent({ type: 'agent_message', content: 'careful', level: 'warning' })?.severity).toBe('warning');
    expect(formatEvent({ type: 'agent_message', content: '' })).toBeNull();
  });

  it('shows a minimal label for an unknown but step-scoped event', () => {
    const f = formatEvent({ type: 'weird_event', _step_id: 't_verify' });
    expect(f).toEqual({ icon: '·', text: 'Verifier', detail: '', severity: 'info' });
  });

  it('drops an unknown event with no step context', () => {
    expect(formatEvent({ type: 'sse_connected' })).toBeNull();
  });
});
