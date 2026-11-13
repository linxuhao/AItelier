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
import { isAtNewest } from '../../lib/conversationThread';

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
  // jsdom ignores scrollTop assignments on an element it has not laid out, so a
  // test that sets it silently keeps position 0 and "scrolled up" never happens.
  // Own the property outright so both the test and the component read/write the
  // same value.
  let top = 0;
  Object.defineProperty(thread, 'scrollTop', {
    configurable: true, get: () => top, set: (value: number) => { top = value; },
  });
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
    // Served by CURSOR and not by call order: the pane reads page 1 for the
    // thread, then reads the rest of the conversation behind it for the rail, so
    // which call asks for `cursor-1` no longer decides which page it gets.
    const first = page([m2], {has_more: true, next_cursor: 'cursor-1', end_of_history: false});
    const behind = page([m1]);
    api.dshDriverConversation.mockImplementation(
      async (_project: string, cursor?: string | null) => (cursor === 'cursor-1' ? behind : first));
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

  it('clears the badge for a message that arrives while the reader is at the newest end', async () => {
    const onread = vi.fn();
    const view = render(DriverConversation, {projectId: 'alpha', onread});
    await view.findByText('Second report');
    api.markDriverConversationRead.mockClear();
    onread.mockClear();

    // A driver message arrives with the server still reporting it unread.
    api.dshDriverConversation.mockResolvedValue(page([m3, m2, m1],
      {unread_count: 3, read_upto_message_id: null}));
    await vi.advanceTimersByTimeAsync(REFRESH_MS);
    await view.findByText('Fresh report');

    // The reader is in the thread, so the poll marks what is now in front of
    // them and the badge is told to refresh. (The refusals - scrolled up, hidden
    // tab, new message not at the end - are asserted directly on
    // shouldMarkReadOnPoll in the lib test, because jsdom cannot position the
    // component's scroll box: it ignores scrollTop writes and reports 0x0.)
    await waitFor(() => expect(api.markDriverConversationRead).toHaveBeenCalledWith('alpha', 'm3'));
    expect(onread).toHaveBeenCalled();
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

// ── The badge clears while the reader is actually in the thread ──────────────
// The poll used to never advance the read marker ("a timer is not a look"). That
// is right when nobody is looking, and wrong when the reader is sitting at the
// newest end of the thread with the tab visible - the message is on screen in
// front of them. Both halves are asserted here.
describe('a poll marks read only what the reader is actually looking at', () => {
  it('does NOT clear the badge while the tab is hidden', async () => {
    const view = render(DriverConversation, {projectId: 'alpha'});
    await view.findByText('Second report');
    layOutThread(view, 100);
    api.markDriverConversationRead.mockClear();
    tabHidden = true;
    document.dispatchEvent(new Event('visibilitychange'));

    api.dshDriverConversation.mockClear();
    await vi.advanceTimersByTimeAsync(REFRESH_MS * 3);

    expect(api.dshDriverConversation).not.toHaveBeenCalled();
    expect(api.markDriverConversationRead).not.toHaveBeenCalled();
  });
});

// ── One message, one body: no in-message info/question split ────────────────
// The owner's ruling: information and questions are separated, but NOT inside a
// message. A message whose body is info and a question is shown once, as its
// text, in the thread. The questions it asks live in the permanent questions
// box, which later info messages cannot wash away. The at-a-glance `?` mark on
// the asking message's header stays: it is a signpost, not a split.
describe('tagged segments', () => {
  it('marks only the message that asks something, and renders each message body once with no tag blocks', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      entry('q1', 'driver', 'Deploy question', '2026-11-03T12:00:00+00:00',
        {body: 'Heads up.\nmay I restart the host now?',
          segments: [{id: 's1', kind: 'info', text: 'Heads up.'},
            {id: 's2', kind: 'question', text: 'may I restart the host now?'}]}),
      entry('i1', 'driver', 'Just an update', '2026-11-03T11:00:00+00:00',
        {body: 'nothing is in flight',
          segments: [{id: 's3', kind: 'info', text: 'nothing is in flight'}]}),
      entry('p1', 'driver', 'Plain message', '2026-11-03T10:00:00+00:00'),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha'});
    // The subject is shown in the thread AND in the questions box (the question
    // is listed there), so wait for the thread's own message, not the bare text.
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());
    await waitFor(() => expect([...view.container.querySelectorAll('[data-conversation-thread] article.message .subject')]
      .some(n => n.textContent === 'Deploy question')).toBe(true));

    const marks = view.container.querySelectorAll('article.message .asks');
    expect(marks.length, 'only the asking message carries the mark').toBe(1);

    // The segmented message renders its body EXACTLY once - as its text, in a
    // <pre> - and nothing of the split survives inside it: no segment list, no
    // tag labels, no kinded blocks.
    const asking = [...view.container.querySelectorAll('article.message')]
      .find(a => a.querySelector('.subject')?.textContent === 'Deploy question') as HTMLElement;
    expect(asking).toBeTruthy();
    expect(asking.querySelectorAll('pre')).toHaveLength(1);
    expect(asking.querySelector('pre')?.textContent).toBe('Heads up.\nmay I restart the host now?');
    expect(asking.textContent?.split('may I restart the host now?').length - 1).toBe(1);
    expect(asking.querySelector('.segments')).toBeNull();
    expect(asking.querySelector('.segment')).toBeNull();
    expect(asking.querySelector('.seg-tag')).toBeNull();
    expect(asking.querySelector('[data-kind]')).toBeNull();

    // The same holds for every other segmented message in the thread.
    const info = [...view.container.querySelectorAll('article.message')]
      .find(a => a.querySelector('.subject')?.textContent === 'Just an update') as HTMLElement;
    expect(info.querySelectorAll('pre')).toHaveLength(1);
    expect(info.querySelector('.segments, .segment, .seg-tag, [data-kind]')).toBeNull();

    // A segment-less message still renders its body in a <pre>.
    expect(view.getByText('body of Plain message').tagName).toBe('PRE');
    // And nothing in the whole thread is split into tagged blocks.
    expect(view.container.querySelector('[data-conversation-thread] .segments, [data-conversation-thread] .segment, [data-conversation-thread] .seg-tag, [data-conversation-thread] [data-kind]')).toBeNull();
  });

  it('shows a segmented message with an empty body as its segment texts, once, in ONE <pre>', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      entry('e1', 'driver', 'Empty body', '2026-11-03T12:00:00+00:00', {body: '',
        segments: [{id: 's1', kind: 'info', text: 'Heads up.'},
          {id: 's2', kind: 'question', text: 'may I restart the host now?'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());

    const message = [...view.container.querySelectorAll('[data-conversation-thread] article.message')]
      .find(a => a.querySelector('.subject')?.textContent === 'Empty body') as HTMLElement;
    expect(message).toBeTruthy();
    // Both texts, joined by a newline, in exactly one <pre>: the body is empty, so
    // the segments are the message, and each text appears once.
    const pres = message.querySelectorAll('pre');
    expect(pres).toHaveLength(1);
    expect(pres[0].textContent).toBe('Heads up.\nmay I restart the host now?');
    expect(message.textContent?.split('may I restart the host now?').length - 1).toBe(1);
    expect(message.textContent?.split('Heads up.').length - 1).toBe(1);
    // Still no split: no segment list, no tags, no kinded blocks.
    expect(message.querySelector('.segments, .segment, .seg-tag, [data-kind]')).toBeNull();

    // The question is listed in the questions box; the info text is not.
    const box = view.container.querySelector('[data-questions-box]') as HTMLElement;
    await waitFor(() => expect(box.querySelector('[data-question-segment-id="s2"]')).not.toBeNull());
    expect(box.querySelector('[data-question-segment-id="s2"] .question-text')?.textContent).toBe('may I restart the host now?');
    expect(box.textContent).not.toContain('Heads up.');
  });

  // The three cases below pin the branches of the one function that decides what a
  // message shows. Each of them survives the whole suite if its branch is broken,
  // so they are written against the branch, not against the shape of the split.
  it('shows the message BODY when it has one, never its stored segment texts as well', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      entry('b1', 'driver', 'Body wins', '2026-11-03T12:00:00+00:00',
        {body: 'The body is the message.',
          segments: [{id: 's1', kind: 'info', text: 'a stale segment copy'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());

    const message = [...view.container.querySelectorAll('[data-conversation-thread] article.message')]
      .find(a => a.querySelector('.subject')?.textContent === 'Body wins') as HTMLElement;
    expect(message).toBeTruthy();
    expect(message.querySelectorAll('pre')).toHaveLength(1);
    expect(message.querySelector('pre')?.textContent).toBe('The body is the message.');
    // Preferring the segments here would show the same message twice, under two
    // different wordings.
    expect(message.textContent).not.toContain('a stale segment copy');
  });

  it('drops a whitespace-only segment instead of rendering a blank line for it', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      entry('w1', 'driver', 'Whitespace first', '2026-11-03T12:00:00+00:00',
        {body: '',
          segments: [{id: 'w0', kind: 'info', text: '   \n\t'},
            {id: 'q1', kind: 'question', text: 'may I restart the host now?'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());

    const message = [...view.container.querySelectorAll('[data-conversation-thread] article.message')]
      .find(a => a.querySelector('.subject')?.textContent === 'Whitespace first') as HTMLElement;
    expect(message).toBeTruthy();
    expect(message.querySelectorAll('pre')).toHaveLength(1);
    // Exactly the readable text: no leading blank line, no padding around it.
    expect(message.querySelector('pre')?.textContent).toBe('may I restart the host now?');
  });

  it('renders no text block at all for a message with neither a body nor segments', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      entry('n1', 'driver', 'Nothing to say yet', '2026-11-03T12:00:00+00:00', {body: '', segments: []}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());

    const message = [...view.container.querySelectorAll('[data-conversation-thread] article.message')]
      .find(a => a.querySelector('.subject')?.textContent === 'Nothing to say yet') as HTMLElement;
    expect(message).toBeTruthy();
    // An empty <pre> is a visible blank block in the thread; the message has no text,
    // so it must render no text element.
    expect(message.querySelectorAll('pre')).toHaveLength(0);
  });
});
