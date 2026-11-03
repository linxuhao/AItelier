import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';

// ── What this file proves ──────────────────────────────────────────────────
// A message the driver sends while the conversation pane is open used to need
// the Refresh button: the thread was fetched once on mount and never again. The
// pane now re-reads its newest page on a bounded interval, and these tests drive
// that interval for real.
//
// Only `setInterval`/`clearInterval` are faked, so `advanceTimersByTime` is the
// clock the component sees while `findByText`/`waitFor` still run on real time.
// `vi.getTimerCount()` therefore counts exactly the pane's own polling timers.
//
// jsdom has no layout engine (every box is 0×0), so the scroll tests install the
// geometry a real browser would report on the thread element: `scrollHeight`
// follows the number of rendered messages, `clientHeight` is a fixed viewport,
// and `scrollTop` is the reader's position. What cannot be proven here is the
// browser actually honouring the assignment; that needs a browser.
const api = vi.hoisted(() => ({
  dshDriverConversation: vi.fn(), sendDriverConversationMessage: vi.fn(),
  markDriverConversationRead: vi.fn(),
}));
vi.mock('../../lib/api', () => api);
import DriverConversation from '../../views/DriverConversation.svelte';

const REFRESH_MS = 10000;
const entry = (id: string, kind: 'driver' | 'user', subject: string, created: string, extra: any = {}) =>
  ({message_id: id, thread_id: id, project_id: 'alpha', sender_kind: kind,
    sender_driver_id: kind === 'driver' ? 'dsh-alpha' : null, subject, body: 'body of ' + subject,
    refs: {}, reply_to_message_id: null, created_at: created, ...extra});
const page = (messages: any[], extra: any = {}) =>
  ({project_id: 'alpha', messages, has_more: false, next_cursor: null, end_of_history: true,
    matched_total: messages.length, unread_count: 0, read_upto_message_id: null,
    read_upto_created_at: null, ...extra});
const subjects = (view: {container: HTMLElement}) =>
  [...view.container.querySelectorAll('article.message .subject')].map(node => node.textContent ?? '');
const articles = (view: {container: HTMLElement}) => view.container.querySelectorAll('article.message').length;

// The two messages already on screen when the pane opens.
const m1 = entry('m1', 'user', 'Message from the user', '2026-11-03T09:00:00+00:00');
const m2 = entry('m2', 'driver', 'Second report', '2026-11-03T10:00:00+00:00');
// ... and the one the driver writes after it.
const m3 = entry('m3', 'driver', 'Fresh report', '2026-11-03T11:00:00+00:00');

let tabHidden = false;
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  tabHidden = false;
  Object.defineProperty(document, 'hidden', {configurable: true, get: () => tabHidden});
  vi.useFakeTimers({toFake: ['setInterval', 'clearInterval']});
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.dshDriverConversation.mockResolvedValue(page([m2, m1]));
});
afterEach(() => {
  cleanup();
  delete (document as any).hidden;
  vi.useRealTimers();
});

/** Give the thread the geometry a browser would report: 100px per message in a
 *  300px viewport. Returns the element so the test can set the reader's
 *  position before a refresh lands. */
function layOutThread(view: {container: HTMLElement}, clientHeight = 300): HTMLElement {
  const thread = view.container.querySelector('[data-conversation-thread]') as HTMLElement;
  Object.defineProperty(thread, 'scrollHeight', {configurable: true,
    get: () => 100 * articles(view)});
  Object.defineProperty(thread, 'clientHeight', {configurable: true, get: () => clientHeight});
  return thread;
}

describe('new messages arrive while the pane is open', () => {
  it('renders a message the driver sends after the pane opened, with no reload', async () => {
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');
    expect(api.dshDriverConversation).toHaveBeenCalledTimes(1);
    expect(subjects(view)).toEqual(['Message from the user', 'Second report']);

    // The driver writes now. Before the fix the thread was fetched once on mount,
    // so the only way to this message was the page's Refresh button.
    api.dshDriverConversation.mockResolvedValue(page([m3, m2, m1]));
    await vi.advanceTimersByTimeAsync(REFRESH_MS);

    await view.findByText('Fresh report');
    expect(api.dshDriverConversation).toHaveBeenCalledTimes(2);
    expect(subjects(view)).toEqual(['Message from the user', 'Second report', 'Fresh report']);
  });

  it('picks the message up within one bounded cadence, not sooner and not later', async () => {
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');
    api.dshDriverConversation.mockResolvedValue(page([m3, m2, m1]));
    // Nothing is fetched while the timer has not ticked: no busy loop.
    await vi.advanceTimersByTimeAsync(REFRESH_MS - 1);
    expect(api.dshDriverConversation).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await view.findByText('Fresh report');
  });
});

describe('a refresh does not disturb the reader', () => {
  it('leaves a reader who scrolled up exactly where they were', async () => {
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');
    const thread = layOutThread(view, 300);   // 200px of content, 300px viewport
    const many = [entry('m5', 'driver', 'Fifth', '2026-11-03T13:00:00+00:00'),
      entry('m4', 'user', 'Fourth', '2026-11-03T12:00:00+00:00'), m3, m2, m1];
    api.dshDriverConversation.mockResolvedValue(page(many));
    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await view.findByText('Fifth');
    expect(articles(view)).toBe(5);
    // 500px of content in a 300px box: park the reader at 120, well above the end.
    thread.scrollTop = 120;
    const parked = thread.scrollTop;

    const newest = [entry('m6', 'driver', 'Sixth', '2026-11-03T14:00:00+00:00'), ...many];
    api.dshDriverConversation.mockResolvedValue(page(newest));
    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await view.findByText('Sixth');

    // The new message was added, and the reader was not pulled to the bottom.
    expect(articles(view)).toBe(6);
    expect(thread.scrollTop).toBe(parked);
    expect(thread.scrollTop).not.toBe(thread.scrollHeight);
  });

  it('follows the newest message for a reader who was already at the bottom', async () => {
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');
    const many = [entry('m5', 'driver', 'Fifth', '2026-11-03T13:00:00+00:00'),
      entry('m4', 'user', 'Fourth', '2026-11-03T12:00:00+00:00'), m3, m2, m1];
    api.dshDriverConversation.mockResolvedValue(page(many));
    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await view.findByText('Fifth');
    const thread = layOutThread(view, 300);
    thread.scrollTop = thread.scrollHeight - 300;   // 200: the bottom
    expect(thread.scrollTop).toBe(200);

    api.dshDriverConversation.mockResolvedValue(page([entry('m6', 'driver', 'Sixth',
      '2026-11-03T14:00:00+00:00'), ...many]));
    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await view.findByText('Sixth');

    // jsdom does not clamp scrollTop the way a browser does; the contract is that
    // the pane ASKS for the newest message, i.e. moves past the old position.
    expect(thread.scrollTop).toBeGreaterThan(200);
    expect(thread.scrollTop).toBe(thread.scrollHeight);
  });

  it('never renders a message twice, and keeps pages loaded further back', async () => {
    api.dshDriverConversation
      .mockResolvedValueOnce(page([m2], {has_more: true, next_cursor: 'cursor-1', end_of_history: false}))
      .mockResolvedValueOnce(page([m1], {has_more: false, next_cursor: null, end_of_history: true}));
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');
    await fireEvent.click(view.getByRole('button', {name: 'Load earlier messages'}));
    await view.findByText('Message from the user');
    expect(subjects(view)).toEqual(['Message from the user', 'Second report']);
    expect(view.container.textContent).toContain('Beginning of the conversation');

    // The newest page repeats the whole thread PLUS a new message. The poll's own
    // page claims there is more history behind it; that belongs to the newest
    // page, not to the older pages this reader already has, so it must not
    // replace their cursor (they would be marched back over pages they hold).
    api.dshDriverConversation.mockResolvedValue(page([m3, m2, m1],
      {has_more: true, next_cursor: 'newest-page-cursor', end_of_history: false}));
    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await view.findByText('Fresh report');

    expect(subjects(view)).toEqual(['Message from the user', 'Second report', 'Fresh report']);
    expect(new Set(subjects(view)).size).toBe(3);
    // The older page survived: the reader is still at the beginning of history,
    // with no "load earlier" affordance pointing back at pages already rendered.
    expect(view.container.textContent).toContain('Beginning of the conversation');
    expect(view.queryByRole('button', {name: 'Load earlier messages'})).toBeNull();
    expect(api.dshDriverConversation).not.toHaveBeenCalledWith('alpha', 'newest-page-cursor');
  });

  it('leaves an automatically refreshed-in message unread', async () => {
    const onread = vi.fn();
    const view = render(DriverConversation, {projectId: 'alpha', onread});
    await view.findByText('Second report');
    expect(view.container.textContent).not.toContain('unread');

    // A driver message arrives with the server still reporting it unread.
    api.dshDriverConversation.mockResolvedValue(page([m3, m2, m1],
      {unread_count: 3, read_upto_message_id: null}));
    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await view.findByText('Fresh report');

    // Shown, but not marked read: the persisted marker may only advance to a
    // message the user was actually looking at, and a timer is not a look.
    expect(api.markDriverConversationRead).not.toHaveBeenCalled();
    expect(onread).not.toHaveBeenCalled();
  });

  it('still advances the marker to what it showed on an explicit visit, never on a timer', async () => {
    const onread = vi.fn();
    const view = render(DriverConversation, {projectId: 'alpha', onread});
    await view.findByText('Second report');
    api.dshDriverConversation.mockResolvedValue(page([m3, m2, m1],
      {unread_count: 1, read_upto_message_id: 'm2'}));

    // The pane has no Refresh button any more, so the explicit visits that may
    // advance the persisted marker are the mount and the user's own send. What
    // the button used to do - read the newest page AND mark what it showed - is
    // still exactly what an explicit visit does; a timer still marks nothing.
    await fireEvent.input(view.getByRole('textbox', {name: 'Write a message'}), {target: {value: 'hello'}});
    await fireEvent.click(view.getByRole('button', {name: 'Send'}));
    await view.findByText('Fresh report');
    await waitFor(() => expect(api.markDriverConversationRead).toHaveBeenCalledWith('alpha', 'm3'));
    expect(onread).toHaveBeenCalled();
  });
});

describe('polling is bounded and failure-tolerant', () => {
  it('keeps exactly one timer, ticks once per cadence, and stops on teardown', async () => {
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');
    expect(api.dshDriverConversation).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(1);

    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledTimes(2));
    expect(vi.getTimerCount()).toBe(1);

    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledTimes(3));
    expect(vi.getTimerCount()).toBe(1);   // never a second interval stacked on top

    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(3 * REFRESH_MS);
    expect(api.dshDriverConversation).toHaveBeenCalledTimes(3);
  });

  it('does not stack a timer when visibility changes repeatedly', async () => {
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');

    // Two "visible" events in a row: the second must replace the timer, not add
    // a second one, or every tick would fetch twice.
    document.dispatchEvent(new Event('visibilitychange'));
    document.dispatchEvent(new Event('visibilitychange'));
    expect(vi.getTimerCount()).toBe(1);
    const catchUp = api.dshDriverConversation.mock.calls.length;

    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    // Exactly one interval fired: one more fetch, not one per started timer.
    expect(api.dshDriverConversation.mock.calls.length).toBe(catchUp + 1);
    expect(vi.getTimerCount()).toBe(1);
  });

  it('polls nothing while the tab is hidden and catches up when it is shown', async () => {
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');

    tabHidden = true;
    document.dispatchEvent(new Event('visibilitychange'));
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(5 * REFRESH_MS);
    expect(api.dshDriverConversation).toHaveBeenCalledTimes(1);

    // Back to a visible tab: an immediate catch-up, not one whole tick of silence.
    api.dshDriverConversation.mockResolvedValue(page([m3, m2, m1]));
    tabHidden = false;
    document.dispatchEvent(new Event('visibilitychange'));
    await view.findByText('Fresh report');
    expect(api.dshDriverConversation).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(1);

    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledTimes(3));
  });

  it('keeps the thread on screen when a refresh fails', async () => {
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');
    const before = subjects(view);

    api.dshDriverConversation.mockRejectedValue(new Error('network down'));
    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledTimes(2));

    expect(subjects(view)).toEqual(before);
    expect(articles(view)).toBe(2);
    expect(view.getByText('Second report')).toBeTruthy();
    // A background failure is not an alert the reader has to dismiss...
    expect(view.queryByRole('alert')).toBeNull();
    // ... and the next tick still tries again.
    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledTimes(3));
    expect(subjects(view)).toEqual(before);
  });

  it('recovers a thread whose first load failed', async () => {
    api.dshDriverConversation.mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValue(page([m2, m1]));
    const view = render(DriverConversation, {projectId: 'alpha'});
    await waitFor(() => expect(view.getByRole('alert')).toBeTruthy());
    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await view.findByText('Second report');
    expect(articles(view)).toBe(2);
    // The failure alert described the empty thread; the thread is no longer empty.
    expect(view.queryByRole('alert')).toBeNull();
  });
});
