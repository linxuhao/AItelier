import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';

// ── What this file can and cannot prove ────────────────────────────────────
// jsdom has no layout engine: every box is 0×0 and scrollHeight is always 0, so
// a test cannot measure "the send button is on screen". What it CAN do is pin
// the box chain that makes the composer reachable in a real browser:
//
//   * the pane's height is definite and the pane itself never scrolls,
//   * the thread is a shrinkable (min-height:0) child with its own overflow-y,
//   * the composer is NOT inside any scrolling box, so its position depends on
//     the pane's box and never on how many messages the thread holds,
//   * appending messages adds nodes only inside the thread.
//
// Those are exactly the properties the old `.pane-body{overflow:auto}` chain
// violated: the composer sat at the end of the scrolling content, the pane was
// taller than the scrollport its page gave it (`100vh - 1rem` inside a container
// of `100vh - appbar`), and reaching Send meant scrolling the page first.
// Actual scroll feel, sticky behaviour and handle hit area still need a browser.
const api = vi.hoisted(() => ({
  stateProjects: vi.fn(), driverStatus: vi.fn(),
  dshDriverConversationUnread: vi.fn(), dshDriverConversation: vi.fn(),
  dshDriverHistory: vi.fn(), sendDriverConversationMessage: vi.fn(),
  markDriverConversationRead: vi.fn(), messageDshDriver: vi.fn(),
  startDshDriver: vi.fn(), stopDshDriver: vi.fn(),
}));
vi.mock('../../lib/api', () => api);
import StateProjects from '../../views/StateProjects.svelte';

const row = (id: string) => ({
  project_id: id, title: id, source_project_id: null, node_count: 1, verified_count: 0,
  source: {repo_path: null}, policy: {dispatch: 'active', revision: 0, reason: ''},
  created_at: '2026-01-01T00:00:00+00:00',
});
const entry = (n: number) => ({
  message_id: 'm' + n, thread_id: 'thread', project_id: 'hot',
  sender_kind: n % 2 ? 'user' : 'driver', sender_driver_id: n % 2 ? null : 'dsh-hot',
  subject: 'Subject ' + n, body: 'Body of message ' + n, refs: {},
  reply_to_message_id: null, created_at: new Date(Date.UTC(2026, 0, 1, 0, n)).toISOString(),
});
const page = (count: number) => ({
  project_id: 'hot',
  messages: Array.from({length: count}, (_, i) => entry(count - i)),
  has_more: false, next_cursor: null, end_of_history: true,
  matched_total: count, unread_count: 0,
  read_upto_message_id: null, read_upto_created_at: null,
});

const css = (el: Element | null, prop: string) =>
  el ? getComputedStyle(el as HTMLElement).getPropertyValue(prop).trim() : '<missing>';
// jsdom keeps `overflow` as written: a shorthand rule reports the shorthand, a
// longhand rule reports the longhand. Read whichever is populated so the helper
// sees the OLD `.pane-body{overflow:auto}` just as clearly as the new
// `overflow-y:auto`.
const overflowY = (el: Element | null) => css(el, 'overflow-y') || css(el, 'overflow');
function scrollingAncestors(el: Element, stopAt: Element): string[] {
  const found: string[] = [];
  for (let node = el.parentElement; node && node !== stopAt; node = node.parentElement) {
    if (['auto', 'scroll', 'overlay'].includes(overflowY(node))) found.push(node.className || node.tagName);
  }
  return found;
}
function chainBetween(inner: Element, outer: Element): Element[] {
  const chain: Element[] = [];
  for (let node = inner.parentElement; node && node !== outer; node = node.parentElement) chain.push(node);
  return chain;
}
function outsideThread(thread: Element): number {
  const pane = thread.closest('.conversation-pane')!;
  return [...pane.querySelectorAll('*')].filter(el => el !== thread && !thread.contains(el)).length;
}

let storage: Map<string, string>;
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en'); storage = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => { storage.set(k, v); },
  });
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'});
  api.stateProjects.mockResolvedValue({projects: [row('hot')], next_after: null});
  api.driverStatus.mockResolvedValue({total: 1, online: 1, projects: {
    hot: {total: 1, online: 1, running_subagents: 1, running_pipelines: 0,
      managed: {enabled: 1, phase: 'running', driver_id: 'dsh-hot', last_error: '',
        updated_at: '2026-06-01T00:00:00+00:00'}},
  }});
  api.dshDriverConversationUnread.mockResolvedValue({projects: {}, total: 0});
  api.dshDriverConversation.mockResolvedValue(page(5));
  api.dshDriverHistory.mockResolvedValue({driver_id: 'dsh-hot', session_id: 's',
    entries: [], has_more: false, next_before_seq: null});
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.messageDshDriver.mockResolvedValue({});
  api.startDshDriver.mockResolvedValue({}); api.stopDshDriver.mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function mountSplit() {
  const view = render(StateProjects);
  await view.findByRole('heading', {name: 'hot'});
  await waitFor(() => expect(view.container.querySelectorAll('article.message').length).toBe(5));
  const pane = view.container.querySelector('aside.conversation-pane') as HTMLElement;
  // The named hooks are the contract; the fallbacks only exist so a run against
  // the old structure reports the real defect (the composer sitting inside the
  // scrolling body) instead of a null dereference.
  const thread = (pane.querySelector('[data-conversation-thread]')
    ?? pane.querySelector('section.conversation')) as HTMLElement;
  const composer = (pane.querySelector('[data-conversation-composer]')
    ?? pane.querySelector('section.conversation form')) as HTMLElement;
  return {view, pane, thread, composer};
}

describe('the conversation pane owns its own scrolling', () => {
  it('pins the composer outside the thread scroller and never scrolls the pane itself', async () => {
    const {pane, thread, composer} = await mountSplit();

    // The composer is a non-shrinking sibling AFTER the scroller, not inside it.
    expect(composer).toBeTruthy();
    expect(thread.contains(composer)).toBe(false);
    expect(getComputedStyle(composer).flexShrink).toBe('0');
    expect(composer.parentElement).toBe(thread.parentElement);
    expect(thread.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Nothing scrolls between the composer and the pane: its position is fully
    // determined by the pane's box, so no amount of thread content can push it
    // out of the pane. Before this change the whole `.pane-body` scrolled and
    // the composer was its last child, i.e. inside the scroller.
    expect(scrollingAncestors(composer, pane)).toEqual([]);

    // The pane is a bounded flex column that clips: it can never be the thing
    // the user has to scroll, and its height cannot follow the thread.
    expect(css(pane, 'display')).toBe('flex');
    expect(css(pane, 'flex-direction')).toBe('column');
    expect(overflowY(pane)).toBe('hidden');
    expect(css(pane, 'height')).not.toBe('auto');
    expect(css(pane, 'max-height')).not.toBe('');

    // The thread is the one scroll box: shrinkable, bounded, its own overflow.
    expect(pane.querySelector('[data-conversation-thread]')).toBeTruthy();
    expect(overflowY(thread)).toBe('auto');
    expect(css(thread, 'min-height')).toBe('0');
    expect(css(thread, 'flex-grow')).toBe('1');
    expect(Number(css(thread, 'flex-shrink'))).toBe(1);

    // Every box between the thread and the pane may shrink (min-height:0) and
    // is a flex column, otherwise the bounded height stops here and the thread
    // grows past the pane.
    const chain = chainBetween(thread, pane);
    expect(chain.length).toBeGreaterThan(0);
    for (const el of chain) {
      expect(css(el, 'min-height')).toBe('0');
      expect(css(el, 'display')).toBe('flex');
      expect(['hidden', 'clip']).toContain(overflowY(el));
    }

    // And the thread really is where the messages are.
    const messages = [...pane.querySelectorAll('article.message')];
    expect(messages).toHaveLength(5);
    for (const message of messages) expect(thread.contains(message)).toBe(true);
  });

  it('reaches the composer with the thread scrolled to its newest message, without window scroll', async () => {
    const {pane, thread, composer} = await mountSplit();
    const pageScroll = vi.spyOn(window, 'scrollTo');

    // Scrolling "the panel" only means anything if the panel is the scroll box.
    expect(overflowY(thread)).toBe('auto');
    expect(scrollingAncestors(composer, pane)).toEqual([]);
    thread.scrollTop = thread.scrollHeight;
    expect(thread.scrollTop).toBe(thread.scrollHeight);
    expect(window.scrollY).toBe(0);
    expect(pageScroll).not.toHaveBeenCalled();

    // The send control is outside that scroller and still in the pane, so it is
    // reachable at any thread scroll position, including the newest one.
    expect(thread.contains(composer)).toBe(false);
    expect(composer.isConnected).toBe(true);
    const send = composer.querySelector('button[type="submit"]') as HTMLButtonElement;
    expect(send).toBeTruthy();
    expect(thread.contains(send)).toBe(false);
    await fireEvent.input(composer.querySelector('textarea')!, {target: {value: 'hello'}});
    await waitFor(() => expect(send.disabled).toBe(false));
    await fireEvent.click(send);
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalledWith('hot', 'hello',
      {replyToMessageId: undefined, targetDriverId: 'dsh-hot'}));
    pageScroll.mockRestore();
  });

  it('keeps every appended message inside the thread instead of growing the pane or the page', async () => {
    const {view, pane, thread} = await mountSplit();
    // Containment only bounds the page if the container is the scroll box.
    expect(overflowY(thread)).toBe('auto');
    expect(css(thread, 'min-height')).toBe('0');
    const boxBefore = ['height', 'max-height', 'min-height', 'display', 'flex-direction', 'overflow-y']
      .map(prop => prop + '=' + css(pane, prop));
    const outsideBefore = outsideThread(thread);
    const documentBefore = document.documentElement.scrollHeight;

    api.dshDriverConversation.mockResolvedValue(page(60));
    await fireEvent.click(view.getByRole('button', {name: 'Refresh'}));
    await waitFor(() => expect(view.container.querySelectorAll('article.message').length).toBe(60));

    // All the new content landed in the scroller; the pane's box did not change
    // and no element outside the scroller was added, so the page cannot grow.
    for (const message of view.container.querySelectorAll('article.message')) {
      expect(thread.contains(message)).toBe(true);
    }
    expect(outsideThread(thread)).toBe(outsideBefore);
    expect(['height', 'max-height', 'min-height', 'display', 'flex-direction', 'overflow-y']
      .map(prop => prop + '=' + css(pane, prop))).toEqual(boxBefore);
    // jsdom reports 0 for every scrollHeight; the containment above is the
    // load-bearing half, this keeps the page-height claim explicit.
    expect(document.documentElement.scrollHeight).toBeLessThanOrEqual(documentBefore);
    expect(window.scrollY).toBe(0);
  });
});
