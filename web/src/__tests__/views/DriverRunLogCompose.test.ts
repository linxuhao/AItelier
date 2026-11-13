import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor, screen } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';
import { st } from '../../lib/stateI18n.svelte';

// ── What this file can and cannot prove ────────────────────────────────────
// The run-log half of the conversation pane used to carry a second message
// composer with a VISIBLE label ("给 driver 发消息") and, after a send, a
// completion sentence ("已发送到 driver 收件箱"). The owner asked for that
// label and that sentence to go away, and both are gone from the component -
// but "the strings are not in the source" is a claim about an edit, not about
// what a reader sees.
//
// jsdom has no layout engine, so this file cannot measure "the composer is now
// one row tall". What it CAN measure is the rendered DOM: the box carries an
// accessible name instead of a visible label, nothing in the form prints the
// label text, and no completion sentence appears after a successful send. It
// also measures that the behaviour the strings were attached to survived the
// slimming: one API call with the typed text, the box cleared, the button dead
// while the box is empty, and a failure still printed as an alert.
const api = vi.hoisted(() => ({
  dshDriverHistory: vi.fn(), messageDshDriver: vi.fn(),
}));
vi.mock('../../lib/api', () => api);
import DriverRunLog from '../../views/DriverRunLog.svelte';

const page = () => ({
  driver_id: 'dsh-1', session_id: 'sessions/0',
  entries: [], has_more: false, next_before_seq: null,
});

function mount() {
  return render(DriverRunLog, {
    projectId: 'hot', projectTitle: 'Hot', driverId: 'dsh-1', driverEnabled: false,
  });
}
// Located by its position in the section, NOT by the new marker: a locator that
// only exists in the delivered markup would make every arm fail at the selector
// on the base instead of at the claim under test.
const form = () => document.querySelector('section.history form') as HTMLFormElement;
const box = () => form().querySelector('textarea') as HTMLTextAreaElement;
const submitButton = () => form().querySelector('button[type="submit"]') as HTMLButtonElement;

beforeEach(() => {
  langStore.set('zh');
  api.dshDriverHistory.mockReset();
  api.messageDshDriver.mockReset();
  api.dshDriverHistory.mockResolvedValue(page());
});
afterEach(() => cleanup());

describe('the run log composer after the slimming', () => {
  it('names the box for assistive tech instead of printing a visible label', async () => {
    mount();
    await waitFor(() => expect(api.dshDriverHistory).toHaveBeenCalled());

    expect(form()).not.toBeNull();
    expect(box()).not.toBeNull();
    // The accessible name lives on the control itself.
    expect(box().getAttribute('aria-label')).toBe(st('driverMessage'));
    // One line by default: the reader can still drag it taller.
    expect(box().getAttribute('rows')).toBe('1');
    // The label ELEMENT is what used to print the string on screen.
    expect(form().querySelector('label')).toBeNull();
    // And nothing in the form renders that text.
    expect(form().textContent).not.toContain(st('driverMessage'));
    // Last, so a base-arm red lands on the claims above it rather than here:
    // the marker is what the built-bundle check greps for.
    expect(form().hasAttribute('data-run-log-compose')).toBe(true);
  });

  it('sends once, clears the box, and prints no completion sentence', async () => {
    api.messageDshDriver.mockResolvedValue(undefined);
    mount();
    await waitFor(() => expect(api.dshDriverHistory).toHaveBeenCalled());

    await fireEvent.input(box(), { target: { value: 'run finished?' } });
    await fireEvent.submit(form());

    await waitFor(() => expect(api.messageDshDriver).toHaveBeenCalledTimes(1));
    expect(api.messageDshDriver).toHaveBeenCalledWith('hot', 'dsh-1', 'run finished?');
    await waitFor(() => expect(box().value).toBe(''));
    // The sentence the owner asked to remove must not come back as feedback.
    expect(form().textContent).not.toContain(st('driverMessageSent'));
    expect(screen.queryByText(st('driverMessageSent'))).toBeNull();
  });

  it('keeps the submit button dead while the box is empty', async () => {
    mount();
    await waitFor(() => expect(api.dshDriverHistory).toHaveBeenCalled());
    expect(submitButton().disabled).toBe(true);
    await fireEvent.input(box(), { target: { value: '   ' } });
    expect(submitButton().disabled).toBe(true);
    await fireEvent.input(box(), { target: { value: 'x' } });
    expect(submitButton().disabled).toBe(false);
  });

  it('still prints a failed send, because a silent failure is not a feature', async () => {
    api.messageDshDriver.mockRejectedValue(new Error('inbox unavailable'));
    mount();
    await waitFor(() => expect(api.dshDriverHistory).toHaveBeenCalled());

    await fireEvent.input(box(), { target: { value: 'anyone there' } });
    await fireEvent.submit(form());

    const alert = await waitFor(() => {
      const node = form().querySelector('[role="alert"]');
      expect(node).not.toBeNull();
      return node as HTMLElement;
    });
    expect(alert.textContent).toContain('inbox unavailable');
    // A failure does not clear the text: the reader can retry it as written.
    expect(box().value).toBe('anyone there');
  });
});
