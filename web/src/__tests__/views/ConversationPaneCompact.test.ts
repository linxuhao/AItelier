import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';
import { st } from '../../lib/stateI18n.svelte';
import { PANE_MIN, paneMaxFor } from '../../lib/splitPane';

// ── What this file can and cannot prove ────────────────────────────────────
// The owner asked for a smaller chat: less chrome above and below, so the
// thread gets the room. jsdom has no layout engine (every box is 0×0), so this
// file cannot measure "the thread is 300px tall now". It pins the three things
// that decide that number in a browser:
//
//   1. the thread is the ONE growable box: `flex:1 1 auto`, `min-height:0`,
//      its own overflow-y, and every ancestor up to the pane may shrink, so the
//      thread is whatever the pane has left;
//   2. nothing in that chain carries a fixed pixel height or max-height, i.e.
//      the layout is a share of the row and not a number that only looks right
//      at one viewport;
//   3. the fixed chrome really is one compact row: the project picker, the
//      start/stop icon and both modes share the pane header, there is no second
//      band for the modes and no band at all inside the conversation, and the
//      vertical padding those bands declare is inside a small budget.
//
// Actual proportions, drag feel and single-row wrapping at narrow widths still
// need a browser; the assertions below are the structural half of that.
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
// jsdom reports the AUTHORED value for padding/margin, so a `rem` here is the
// number the stylesheet declares - which is what a compaction claim is about.
function rem(el: Element | null, prop: string): number {
  const value = css(el, prop);
  // jsdom reports '' for a longhand the sheet never declares, which is 0 here.
  if (value === '') return 0;
  const match = /^(-?[\d.]+)(rem|px|em)$/.exec(value);
  if (!match) throw new Error(`${prop} is not a length: ${JSON.stringify(value)}`);
  return match[2] === 'px' ? Number(match[1]) / 16 : Number(match[1]);
}
const verticalChrome = (el: Element | null) =>
  rem(el, 'padding-top') + rem(el, 'padding-bottom');
// Svelte adds its own scoping class to every element; the assertion is about
// the pane's own structure, so ignore those.
const classes = (el: Element) => el.className.split(/\s+/).filter(c => c && !c.startsWith('svelte-'));
function chainBetween(inner: Element, outer: Element): Element[] {
  const chain: Element[] = [];
  for (let node = inner.parentElement; node && node !== outer; node = node.parentElement) chain.push(node);
  return chain;
}

let storage: Map<string, string>;
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en'); storage = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => { storage.set(k, v); },
  });
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'});
  api.stateProjects.mockResolvedValue({projects: [row('hot'), row('cold')], next_after: null});
  // `hot` has a managed driver, `cold` has none: the same select drives both the
  // conversation and the start/stop control, so one picker covers both states.
  api.driverStatus.mockResolvedValue({total: 1, online: 1, projects: {
    hot: {total: 1, online: 1, running_subagents: 1, running_pipelines: 0,
      managed: {enabled: 1, phase: 'running', driver_id: 'dsh-hot', last_error: '',
        updated_at: '2026-06-01T00:00:00+00:00'}},
  }});
  api.dshDriverConversationUnread.mockResolvedValue({projects: {}, total: 0});
  api.dshDriverConversation.mockResolvedValue(page(2));
  api.dshDriverHistory.mockResolvedValue({driver_id: 'dsh-hot', session_id: 's',
    entries: [], has_more: false, next_before_seq: null});
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
  api.messageDshDriver.mockResolvedValue({});
  api.startDshDriver.mockResolvedValue({}); api.stopDshDriver.mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/** Selecting a DAG is the list's job now: click its card. */
async function selectDag(view: any, id: string) {
  const card = [...view.container.querySelectorAll('.project-card')]
    .find((c: any) => (c.textContent ?? '').includes(id)) as HTMLElement | undefined;
  if (!card) throw new Error('no project card for ' + id);
  await fireEvent.click(card);
  await waitFor(() => {
    const link = view.container.querySelector('aside.conversation-pane .pane-project') as HTMLAnchorElement;
    expect(link?.getAttribute('href') ?? '').toContain(id);
  });
}

async function mountPane() {
  const view = render(StateProjects);
  await view.findByRole('heading', {name: 'hot'});
  const pane = view.container.querySelector('aside.conversation-pane') as HTMLElement;
  // Until driver status lands both rows are idle, so which one the pane picks
  // first is not fixed: select the DAG under test explicitly, on its card.
  await selectDag(view, 'hot');
  await waitFor(() => expect(view.container.querySelectorAll('article.message').length).toBe(2));
  // `hot` is managed: wait until the toggle knows it before asserting its name.
  await waitFor(() => expect(view.getByRole('button', {name: 'Stop driver'})).toBeTruthy());
  const header = pane.querySelector('.pane-header') as HTMLElement;
  // The header used to hold the picker; it now holds the link into the DAG the
  // list selected, which is what "which DAG is this pane about" reads as.
  const dagLink = pane.querySelector('.pane-project') as HTMLAnchorElement;
  const thread = pane.querySelector('[data-conversation-thread]') as HTMLElement;
  const composer = pane.querySelector('[data-conversation-composer]') as HTMLElement;
  return {view, pane, header, thread, composer, dagLink};
}

describe('the vertical budget goes to the thread', () => {
  it('gives the thread the remainder of the pane and keeps every box above it shrinkable', async () => {
    const {pane, thread} = await mountPane();

    // The thread is the one box that grows, and it may shrink below its content
    // (min-height:0) so the pane's height - not the messages - decides its size.
    expect(css(thread, 'flex-grow')).toBe('1');
    expect(Number(css(thread, 'flex-shrink'))).toBe(1);
    expect(css(thread, 'min-height')).toBe('0');
    expect(css(thread, 'overflow-y')).toBe('auto');

    // Every box between the thread and the pane is a clipping flex column that
    // can shrink: otherwise the bounded height stops at that box and the thread
    // would grow past the pane again.
    const chain = chainBetween(thread, pane);
    expect(chain.length).toBeGreaterThan(0);
    for (const el of chain) {
      expect(css(el, 'display')).toBe('flex');
      expect(css(el, 'flex-direction')).toBe('column');
      expect(css(el, 'min-height')).toBe('0');
      expect(['hidden', 'clip']).toContain(css(el, 'overflow-y'));
    }

    // No magic number: the pane's height is its share of the row and nothing in
    // the chain pins a pixel height that would only fit one viewport.
    expect(css(pane, 'height')).toBe('100%');
    for (const el of [pane, ...chain, thread]) {
      expect(css(el, 'height')).not.toMatch(/^\d+(\.\d+)?px$/);
      expect(css(el, 'max-height')).not.toMatch(/^\d+(\.\d+)?px$/);
      expect(css(el, 'min-height')).not.toMatch(/^\d+(\.\d+)?px$/);
    }
  });

  it('spends one compact header row on all the pane controls, and no band inside the conversation', async () => {
    const {view, pane, header, thread, composer} = await mountPane();

    // The pane is handle + ONE control row + the body. The modes used to be a
    // second band; they are now part of the header, so the tabs, the DAG link
    // and the driver toggle share a single row.
    expect([...pane.children].map(classes).flat()).toEqual(['resize-handle', 'pane-header', 'pane-body']);
    expect(header.contains(view.getByLabelText('Open this State DAG'))).toBe(true);
    // The project picker is gone from the header - the list selects, the header
    // says which DAG this is and links into it.
    expect(header.querySelector('select')).toBeNull();
    expect(header.querySelector('.pane-project')?.getAttribute('href')).toContain('hot');
    expect(header.contains(view.getByRole('button', {name: 'Stop driver'}))).toBe(true);
    expect(header.querySelector('[role="tablist"]')).toBeTruthy();
    expect(view.getByRole('tab', {name: 'Conversation'}).closest('.pane-header')).toBe(header);
    expect(view.getByRole('tab', {name: 'Run history'}).closest('.pane-header')).toBe(header);
    expect(header.querySelectorAll('[role="tablist"]')).toHaveLength(1);

    // ... and nothing else sits above the thread: no second header band for the
    // conversation title, no modes band, no size row.
    const section = thread.parentElement as HTMLElement;
    expect(classes(section)).toContain('conversation');
    // The section's bands are the thread, then the permanent questions box, then
    // the composer. The box is a band BETWEEN the thread and the composer, never
    // inside the thread, so it is the one band allowed here; nothing above the
    // thread is added.
    expect([...section.children]).toEqual([thread, section.querySelector('[data-questions-box]'), composer]);
    expect(section.children[0]).toBe(thread);
    expect(section.children[section.children.length - 1]).toBe(composer);
    expect(pane.querySelector('.conversation-head')).toBeNull();

    // The chrome that remains is small: header + body padding + composer's own
    // separator inside a 2rem budget, versus ~4rem of bands before (header,
    // modes band, conversation title band, body padding, composer padding).
    const budget = verticalChrome(header) + verticalChrome(pane.querySelector('.pane-body'))
      + verticalChrome(composer) + rem(composer, 'margin-top');
    expect(verticalChrome(header)).toBeLessThanOrEqual(0.35 * 2);
    expect(verticalChrome(pane.querySelector('.pane-body'))).toBeLessThanOrEqual(0.35 * 2);
    expect(verticalChrome(composer)).toBeLessThanOrEqual(0.35 * 2);
    expect(budget).toBeLessThanOrEqual(2);
  });
});

describe('controls are consolidated, not deleted', () => {
  it('has no size buttons left, and the separator still resizes by drag and by keyboard', async () => {
    const {view, pane} = await mountPane();
    // The three buttons duplicated a border that is already draggable.
    expect(view.queryByRole('button', {name: 'Expand'})).toBeNull();
    expect(view.queryByRole('button', {name: 'Shrink'})).toBeNull();
    expect(view.queryByRole('button', {name: 'Restore'})).toBeNull();
    // Their strings are gone with them, in all three languages.
    expect(st('conversationPaneExpand')).toBe('conversationPaneExpand');
    expect(st('conversationPaneShrink')).toBe('conversationPaneShrink');
    expect(st('conversationPaneRestore')).toBe('conversationPaneRestore');
    expect(st('conversationPaneSize')).toBe('conversationPaneSize');

    // The capability those buttons stood for - the pane's width is the user's -
    // survives on the handle itself.
    const handle = view.getByRole('separator', {name: 'Resize conversation pane'});
    expect(pane.style.width).toBe('420px');
    await fireEvent.mouseDown(handle, {clientX: 800});
    await fireEvent.mouseMove(window, {clientX: 700});
    await fireEvent.mouseUp(window, {clientX: 700});
    await waitFor(() => expect(pane.style.width).toBe('520px'));
    expect(handle.getAttribute('aria-valuenow')).toBe('520');
    expect(storage.get('aitelier_dag_pane_width')).toBe('520');

    // ... and from the keyboard, which is the accessible half of the same
    // capability and was never covered by a button.
    await fireEvent.keyDown(handle, {key: 'ArrowRight'});
    await waitFor(() => expect(pane.style.width).toBe('488px'));
    await fireEvent.keyDown(handle, {key: 'ArrowLeft'});
    await waitFor(() => expect(pane.style.width).toBe('520px'));
    await fireEvent.keyDown(handle, {key: 'Home'});
    await waitFor(() => expect(pane.style.width).toBe(PANE_MIN + 'px'));
    await fireEvent.keyDown(handle, {key: 'End'});
    await waitFor(() => expect(pane.style.width).toBe(paneMaxFor(window.innerWidth) + 'px'));
  });

  it('starts and stops the selected DAG from an icon-only button with an accessible name', async () => {
    const {view} = await mountPane();
    const stop = view.getByRole('button', {name: 'Stop driver'});
    // Icon-only: no visible text, but a name for assistive tech and a hint.
    expect(stop.textContent?.trim()).toBe('');
    expect(stop.querySelector('svg')).toBeTruthy();
    expect(stop.getAttribute('title')).toBe('Start or stop this project’s driver');
    await fireEvent.click(stop);
    await waitFor(() => expect(api.stopDshDriver).toHaveBeenCalledWith('hot'));

    // The control acts on whatever the list selects - not on a fixed project.
    await selectDag(view, 'cold');
    await waitFor(() => expect(api.dshDriverConversation).toHaveBeenCalledWith('cold'));
    const start = await waitFor(() => view.getByRole('button', {name: 'Start driver'}));
    expect(view.queryByRole('button', {name: 'Stop driver'})).toBeNull();
    await fireEvent.click(start);
    await waitFor(() => expect(api.startDshDriver).toHaveBeenCalledWith('cold'));
  });

  it('drops the refresh and close buttons without dropping what they did', async () => {
    const {view, composer, thread} = await mountPane();
    // Neither button is in the pane any more. The close button was already dead
    // here: the pane never passed an `onclose`, so it closed nothing.
    expect(view.queryByRole('button', {name: 'Refresh'})).toBeNull();
    expect(view.queryByRole('button', {name: 'Close messages'})).toBeNull();
    expect(st('driverMessagesRefresh')).toBe('driverMessagesRefresh');

    // What Refresh was for - "show me what the driver just wrote" - is the
    // thread's own polling: a message that arrives while the pane is open shows
    // up without any control being pressed, and still leaves the marker alone.
    api.dshDriverConversation.mockResolvedValue(page(3));
    document.dispatchEvent(new Event('visibilitychange'));
    await view.findByText('Subject 3');
    expect(thread.contains(view.getByText('Subject 3'))).toBe(true);
    expect(api.markDriverConversationRead).not.toHaveBeenCalled();
    // The composer is still outside the scroller, so the extra row could not
    // have pushed Send anywhere.
    expect(thread.contains(composer)).toBe(false);
  });
});

describe('the composer is one row', () => {
  it('puts Send beside a single-line input that still expands', async () => {
    const {view, composer} = await mountPane();
    const input = view.getByRole('textbox', {name: 'Write a message'}) as HTMLTextAreaElement;
    const send = view.getByRole('button', {name: 'Send'}) as HTMLButtonElement;

    // pico ships `button[type=submit],select,textarea{width:100%}`. Left alone,
    // that makes Send a full-width block and the textarea's base size the whole
    // row, so Send would always wrap onto a line of its own. The two selectors
    // that beat it are read out of the CSSOM rather than from the source: jsdom
    // resolves the cascade in document order and ignores specificity, so it
    // cannot model pico's sheet against a component rule, but the browser only
    // needs the declarations to be there with the higher specificity.
    const rules = [...document.styleSheets].flatMap(sheet => {
      try { return [...sheet.cssRules] as CSSStyleRule[]; } catch { return []; }
    });
    const ruleFor = (selectorPart: string) => rules.find(rule =>
      rule.selectorText?.includes('composer-row') && rule.selectorText.includes(selectorPart));
    // `.composer-row button[type="submit"]` is (0,2,1) against pico's (0,1,1).
    expect(ruleFor('button[type="submit"]')?.style.width).toBe('auto');
    // `.composer-row textarea` is (0,1,1) against pico's (0,0,1), and its base
    // size is 6rem rather than the whole row.
    expect(ruleFor('textarea')?.style.width).toBe('auto');
    expect(css(send, 'width')).toBe('auto');
    expect(css(input, 'width')).toBe('auto');
    expect(css(input, 'flex-basis')).toBe('6rem');

    // One line by default, and still the user's to grow: the resize handle is
    // the same affordance the two-row box had.
    expect(input.getAttribute('rows')).toBe('1');
    expect(css(input, 'resize')).toBe('vertical');

    // Send is in the same flex row, and the row is the composer's only row: the
    // input gives up width (min-width:0) instead of pushing Send out, and the
    // row wraps rather than overflowing at a narrow width.
    expect(send.parentElement).toBe(input.parentElement);
    const rowEl = input.parentElement as HTMLElement;
    expect(composer.contains(rowEl)).toBe(true);
    expect(css(rowEl, 'display')).toBe('flex');
    expect(css(rowEl, 'flex-direction')).toBe('row');
    expect(css(rowEl, 'flex-wrap')).toBe('wrap');
    expect(css(input, 'flex-grow')).toBe('1');
    expect(Number(css(input, 'flex-shrink'))).toBe(1);
    expect(css(input, 'min-width')).toBe('0');
    expect(css(send, 'flex-shrink')).toBe('0');

    // The visible "Write a message" label is gone; the accessible name it gave
    // the box is not, so the field is still announced and still findable.
    expect(composer.querySelector('label')).toBeNull();
    expect(view.container.textContent).not.toContain('Write a message');
    expect(input.getAttribute('aria-label')).toBe('Write a message');

    // It still sends, and still clears afterwards.
    await fireEvent.input(input, {target: {value: 'hello'}});
    await fireEvent.click(send);
    await waitFor(() => expect(api.sendDriverConversationMessage)
      .toHaveBeenCalledWith('hot', 'hello', {replyToMessageId: undefined, targetDriverId: 'dsh-hot'}));
    await waitFor(() => expect(input.value).toBe(''));
  });

  it('stays one row, reachable and sendable when the pane collapses to one column', async () => {
    const {view, pane, thread, composer} = await mountPane();
    window.innerWidth = 600;
    try {
      window.dispatchEvent(new Event('resize'));
      await waitFor(() => expect(pane.dataset.narrow).toBe('true'));
      // No fixed width, no separator, but the same one-row composer.
      expect(pane.style.width).toBe('');
      expect(view.queryByRole('separator', {name: 'Resize conversation pane'})).toBeNull();
      expect(css(thread, 'overflow-y')).toBe('auto');
      const input = view.getByRole('textbox', {name: 'Write a message'}) as HTMLTextAreaElement;
      const send = view.getByRole('button', {name: 'Send'}) as HTMLButtonElement;
      expect(send.parentElement).toBe(input.parentElement);
      expect(css(input.parentElement, 'flex-wrap')).toBe('wrap');
      expect(css(input, 'min-width')).toBe('0');
      // Still outside the scroller, so it cannot scroll away with the messages.
      expect(thread.contains(composer)).toBe(false);
      await fireEvent.input(input, {target: {value: 'narrow hello'}});
      await fireEvent.click(send);
      await waitFor(() => expect(api.sendDriverConversationMessage)
        .toHaveBeenCalledWith('hot', 'narrow hello', {replyToMessageId: undefined, targetDriverId: 'dsh-hot'}));
    } finally { window.innerWidth = 1024; }
  });
});

// ── One control with two states, not two buttons ────────────────────────────
// The owner asked for the mode pair to behave like a pressed/raised toggle. The
// assertions below pin the parts a test can decide: a single shared pill, the
// chosen segment marked active and aria-selected, the other neither, and the
// marking following the click. The literal pixels are a browser's job.
describe('the mode pair is one control with two states', () => {
  it('marks exactly one segment as the pressed state and moves it with the click', async () => {
    const {view} = await mountPane();
    const group = view.container.querySelector('[data-modes="segmented"]') as HTMLElement;
    expect(group, 'the two modes must share one pill').toBeTruthy();
    // Exactly the two modes live inside that one pill.
    expect(group.querySelectorAll('[role="tab"]').length).toBe(2);

    const conversation = view.getByRole('tab', {name: 'Conversation'}) as HTMLElement;
    const run = view.getByRole('tab', {name: 'Run history'}) as HTMLElement;
    expect(group.contains(conversation) && group.contains(run)).toBe(true);

    // Conversation is the default: it is the raised one.
    expect(conversation.classList.contains('active')).toBe(true);
    expect(conversation.getAttribute('aria-selected')).toBe('true');
    expect(run.classList.contains('active')).toBe(false);
    expect(run.getAttribute('aria-selected')).toBe('false');

    await fireEvent.click(run);
    await waitFor(() => expect(run.classList.contains('active')).toBe(true));
    expect(run.getAttribute('aria-selected')).toBe('true');
    expect(conversation.classList.contains('active')).toBe(false);
    expect(conversation.getAttribute('aria-selected')).toBe('false');
  });
});
