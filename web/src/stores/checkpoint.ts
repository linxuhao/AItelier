import { writable } from 'svelte/store';

export interface CheckpointState {
  visible: boolean;
  runId: string | null;
  feedback: string;
  checkpointData: Record<string, unknown> | null;
}

export const checkpointStore = writable<CheckpointState>({
  visible: false,
  runId: null,
  feedback: '',
  checkpointData: null,
});

/**
 * Show the checkpoint approval dialog with the given run and data.
 * Uses .set() (full replacement) to ensure stale state is fully reset.
 */
export function showCheckpoint(runId: string, data: Record<string, unknown> | null): void {
  checkpointStore.set({
    visible: true,
    runId,
    feedback: '',
    checkpointData: data,
  });
}

/** Hide the checkpoint dialog and reset all state. */
export function hideCheckpoint(): void {
  checkpointStore.update(prev => ({
    ...prev,
    visible: false,
    runId: null,
    feedback: '',
    checkpointData: null,
  }));
}

/** Update the feedback text as the user types. */
export function setFeedback(text: string): void {
  checkpointStore.update(prev => ({ ...prev, feedback: text }));
}
