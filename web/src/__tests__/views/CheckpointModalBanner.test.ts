/**
 * The checkpoint modal must show the user their own rejection feedback.
 *
 * A user who rejected with feedback saw the modal reopen looking identical and
 * concluded the rejection had failed — the "Revised N time(s) / Last feedback"
 * banner read `user_rejection_history.json`, a file with several readers and no
 * writer. `api/meta_routers.py:_read_rejection_rounds` now reads skillflow's
 * real log, and the payload below is the VERBATIM response of a live run
 * (project `greet`, dpe_default_v2 step 2, rejected once) — captured so the
 * client half of that fix is pinned to what the server actually sends.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';
import { showCheckpoint, hideCheckpoint } from '../../stores/checkpoint';
import { t } from '../../lib/i18n.svelte';
import { langStore } from '../../stores/i18n';

import live from './checkpoint_with_rejection.live.json';

const mockApi = vi.hoisted(() => ({
  getCheckpoint: vi.fn(),
  approveCheckpoint: vi.fn(),
  rejectCheckpoint: vi.fn(),
}));

vi.mock('../../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/api')>()),
  ...mockApi,
}));

import CheckpointModal from '../../views/CheckpointModal.svelte';

const FEEDBACK = 'BANNER-PROBE-20260729';

describe('CheckpointModal rejection banner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hideCheckpoint();
    langStore.set('en');
    mockApi.getCheckpoint.mockResolvedValue(live);
  });

  it('renders the round count and the user’s verbatim feedback', async () => {
    const { container } = render(CheckpointModal);
    showCheckpoint('greet', null);

    await waitFor(() => {
      const note = container.querySelector('.cp-revision-note');
      expect(note).not.toBeNull();
      expect(note?.textContent).toContain('Revised 1 time(s)');
      expect(note?.textContent).toContain('Last feedback:');
      expect(note?.textContent).toContain(FEEDBACK);
    });
  });

  it('translates the banner with the rest of the UI', async () => {
    langStore.set('zh-CN');
    const { container } = render(CheckpointModal);
    showCheckpoint('greet', null);

    await waitFor(() => {
      const note = container.querySelector('.cp-revision-note');
      expect(note).not.toBeNull();
      expect(note?.textContent).toContain(t('modal.revisedTimes').replace('{n}', '1'));
      expect(note?.textContent).toContain(t('modal.lastFeedback'));
      expect(note?.textContent).toContain(FEEDBACK);
    });
  });

  // The modal body used to be a cached HTML string built once at load, so t()
  // inside it froze at that moment: switching language re-translated the
  // buttons and left the banner in the old language. The body is derived from
  // the raw payload now, so it must follow the switch — while leaving the
  // user's own words untouched.
  it('follows a language switch made while the modal is open', async () => {
    const { container } = render(CheckpointModal);
    showCheckpoint('greet', null);
    await waitFor(() =>
      expect(container.querySelector('.cp-revision-note')?.textContent).toContain('Revised 1 time(s)'),
    );

    langStore.set('zh-CN');

    await waitFor(() => {
      const note = container.querySelector('.cp-revision-note');
      expect(note?.textContent).toContain('已修订 1 次');
      expect(note?.textContent).not.toContain('Revised 1 time(s)');
      expect(note?.textContent).toContain(FEEDBACK);
    });
  });
});
