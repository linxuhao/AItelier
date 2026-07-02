/**
 * Tests for web/src/stores/checkpoint.ts — checkpoint approval dialog state.
 *
 * @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import {
  checkpointStore,
  showCheckpoint,
  hideCheckpoint,
  setFeedback,
} from '../src/stores/checkpoint';

describe('checkpointStore', () => {
  beforeEach(() => {
    // Reset to initial state
    checkpointStore.set({
      visible: false,
      runId: null,
      feedback: '',
      checkpointData: null,
    });
  });

  it('has correct initial state', () => {
    const state = get(checkpointStore);
    expect(state.visible).toBe(false);
    expect(state.runId).toBeNull();
    expect(state.feedback).toBe('');
    expect(state.checkpointData).toBeNull();
  });

  it('showCheckpoint sets visible with runId and data', () => {
    const data = { step_output: { file: 'content' } };
    showCheckpoint('run-123', data);

    const state = get(checkpointStore);
    expect(state.visible).toBe(true);
    expect(state.runId).toBe('run-123');
    expect(state.checkpointData).toEqual(data);
    expect(state.feedback).toBe(''); // reset
  });

  it('showCheckpoint accepts null data', () => {
    showCheckpoint('run-456', null);

    const state = get(checkpointStore);
    expect(state.visible).toBe(true);
    expect(state.runId).toBe('run-456');
    expect(state.checkpointData).toBeNull();
  });

  it('hideCheckpoint resets to initial state', () => {
    showCheckpoint('run-123', { label: 'Review' });
    setFeedback('Looks good');

    hideCheckpoint();

    const state = get(checkpointStore);
    expect(state.visible).toBe(false);
    expect(state.runId).toBeNull();
    expect(state.feedback).toBe('');
    expect(state.checkpointData).toBeNull();
  });

  it('setFeedback updates feedback text', () => {
    setFeedback('Approve this checkpoint');
    expect(get(checkpointStore).feedback).toBe('Approve this checkpoint');

    setFeedback('Updated feedback');
    expect(get(checkpointStore).feedback).toBe('Updated feedback');
  });

  it('preserves other fields when setting feedback', () => {
    showCheckpoint('run-789', { label: 'Test' });
    setFeedback('Looks good');

    const state = get(checkpointStore);
    expect(state.runId).toBe('run-789');
    expect(state.checkpointData).toEqual({ label: 'Test' });
    expect(state.visible).toBe(true);
    expect(state.feedback).toBe('Looks good');
  });

  it('supports subscribe pattern', () => {
    const values: unknown[] = [];
    const unsub = checkpointStore.subscribe((v) => values.push(v));

    expect(values.length).toBe(1);
    expect((values[0] as any).visible).toBe(false);

    showCheckpoint('run-1', null);
    expect(values.length).toBe(2);
    expect((values[1] as any).visible).toBe(true);
    expect((values[1] as any).runId).toBe('run-1');

    unsub();
  });
});
