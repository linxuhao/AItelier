import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';
const api = vi.hoisted(() => ({ dshDriverConversation: vi.fn(), sendDriverConversationMessage: vi.fn(), markDriverConversationRead: vi.fn() }));
vi.mock('../../lib/api', () => api);
import DriverConversation from '../../views/DriverConversation.svelte';

// The questions box: a PERMANENT, foldable SIBLING of the thread's scroll box,
// listing the open questions (each with its own reply control, which binds the
// composer to that one question and sends `answers` with the reply) and the
// answered ones in a separate group, with no reply control. A message is never
// split into info and question blocks; the questions live only here.

const driverMsg = (id: string, subject: string, created: string, extra: any = {}) =>
  ({message_id: id, thread_id: id, project_id: 'alpha', sender_kind: 'driver',
    sender_driver_id: 'dsh-alpha', subject, body: '', refs: {}, reply_to_message_id: null,
    created_at: created, ...extra});
const userMsg = (id: string, created: string, extra: any = {}) =>
  ({message_id: id, thread_id: id, project_id: 'alpha', sender_kind: 'user',
    sender_driver_id: null, subject: 'Message from the user', body: 'ok', refs: {},
    reply_to_message_id: null, created_at: created, ...extra});
const page = (messages: any[]) =>
  ({project_id: 'alpha', messages, has_more: false, next_cursor: null, end_of_history: true,
    matched_total: messages.length, unread_count: 0, read_upto_message_id: null,
    read_upto_created_at: null});
const at = (minute: number) => new Date(Date.UTC(2026, 10, 3, 10, minute, 0)).toISOString();

/** The conversation as the route serves it: newest-first, keyset-paged by
 *  `cursor` (which names the previous page's last row) and honouring `limit`.
 *
 *  The component reads it twice - the thread's own page, then the rail's walk
 *  behind that page - so a fixture has to be served page by page rather than as
 *  one lump, or the test would prove nothing about reading past the first page.
 */
function serveConversation(newestFirst: any[], pageSize?: number) {
  api.dshDriverConversation.mockImplementation(async (_project: string, cursor?: string | null, limit = 30) => {
    const size = Math.min(pageSize ?? limit, limit);
    const start = cursor ? newestFirst.findIndex(row => row.message_id === cursor) + 1 : 0;
    const slice = newestFirst.slice(start, start + size);
    const more = start + size < newestFirst.length;
    return {
      ...page(slice),
      has_more: more, next_cursor: more ? slice[slice.length - 1].message_id : null,
      end_of_history: !more,
    };
  });
}

/** A conversation whose oldest message asks a question and whose 40 newer
 *  messages are info: the newest 30 rows - all the thread loads - do not
 *  contain the question. */
function washedAwayConversation() {
  const asked = driverMsg('m0', 'Report', at(0),
    {segments: [{id: 'q1', kind: 'question', text: 'may I restart the host?'}]});
  const infos = Array.from({length: 40}, (_, i) => driverMsg('i' + i, 'Info ' + i, at(i + 1),
    {segments: [{id: 'i' + i, kind: 'info', text: 'nothing to report ' + i}]}));
  return {asked, infos, newestFirst: [...infos].reverse().concat([asked])};
}

beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  api.markDriverConversationRead.mockResolvedValue({});
  api.sendDriverConversationMessage.mockResolvedValue({});
});
afterEach(() => cleanup());

describe('the questions box', () => {
  it('is permanent: with no question at all it still renders, named, and says there are none', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Info only', '2026-11-03T10:00:00+00:00', {segments: [{id: 'i1', kind: 'info', text: 'context'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await view.findByText('Info only');
    await waitFor(() => expect(view.container.querySelector('[data-questions-empty]')).not.toBeNull());
    const box = view.container.querySelector('[data-questions-box]') as HTMLElement;
    expect(box).not.toBeNull();
    expect(box.tagName).toBe('DETAILS');
    // An accessible name, and the empty-state text in the box.
    expect(box.getAttribute('aria-label')).toBe('Questions');
    expect(view.getByRole('group', {name: 'Questions'})).toBe(box);
    expect(box.textContent).toContain('No open questions.');
    // No question is listed, and the info stays out of the box.
    expect(view.container.querySelectorAll('[data-question-segment-id]')).toHaveLength(0);
    expect(view.container.querySelectorAll('[data-answer-message-id]')).toHaveLength(0);
    expect(box.textContent).not.toContain('context');
  });

  it('is open by default, so the open count and the questions are visible without a click', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [{id: 'q1', kind: 'question', text: 'ok?'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-question-segment-id="q1"]')).not.toBeNull());
    const box = view.container.querySelector('[data-questions-box]') as HTMLDetailsElement;
    expect(box.open).toBe(true);
  });

  it('lists open questions with a reply control, and answered ones in the answered group without one', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: 'may I restart?'},
        {id: 'i1', kind: 'info', text: 'context'},
        {id: 'q2', kind: 'question', text: 'which node first?'}]}),
      userMsg('u1', '2026-11-03T11:00:00+00:00', {answers: [{message_id: 'm1', segment_id: 'q1'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());
    const rail = view.container.querySelector('[data-questions-box]') as HTMLElement;
    expect(rail).not.toBeNull();
    // q2 is open and keeps its reply control; q1 is answered and stays in the
    // box's answered group, with no reply control. The info block is never listed.
    const open = rail.querySelectorAll('.questions-list:not(.questions-list-answered) [data-question-segment-id]');
    expect([...open].map(i => i.getAttribute('data-question-segment-id'))).toEqual(['q2']);
    const answered = rail.querySelectorAll('.questions-list-answered [data-question-segment-id]');
    expect([...answered].map(i => i.getAttribute('data-question-segment-id'))).toEqual(['q1']);
    expect(rail.textContent).not.toContain('context');
    const button = rail.querySelector('[data-answer-message-id="m1"][data-answer-segment-id="q2"]');
    expect(button).not.toBeNull();
    expect(rail.querySelector('[data-answer-message-id="m1"][data-answer-segment-id="q1"]')).toBeNull();
  });

  it('is a sibling of the thread scroll box, never inside it', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [{id: 'q1', kind: 'question', text: 'ok?'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());
    const box = view.container.querySelector('[data-questions-box]') as HTMLElement;
    const thread = view.container.querySelector('[data-conversation-thread]') as HTMLElement;
    expect(box).not.toBeNull();
    // The DOM relationship itself: the box is not inside the thread's scroll box,
    // and does not contain it - they are siblings under the same band.
    expect(thread.closest('[data-questions-box]')).toBeNull();
    expect(thread.contains(box)).toBe(false);
    expect(box.contains(thread)).toBe(false);
    expect(box.parentElement).toBe(thread.parentElement);
  });

  it('its own body scrolls on its own and never on the thread', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [{id: 'q1', kind: 'question', text: 'ok?'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());
    const body = view.container.querySelector('[data-questions-box] .questions-body') as HTMLElement;
    const style = getComputedStyle(body);
    expect(style.overflowY).toBe('auto');
    // The bound itself, by value: the rule is `.questions-body{max-height:9rem}`.
    // A bare truthiness check passed even with the bound removed (`none`).
    expect(style.maxHeight).toBe('9rem');
    // The box's own rules, not the thread's: the thread keeps its own scroll.
    const threadStyle = getComputedStyle(view.container.querySelector('[data-conversation-thread]') as HTMLElement);
    expect(threadStyle.overflowY).toBe('auto');
  });

  it('a rail reply binds the composer to that question and sends its citation', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: 'may I restart?'},
        {id: 'q2', kind: 'question', text: 'which node first?'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());
    const control = view.container.querySelector('[data-answer-message-id="m1"][data-answer-segment-id="q2"]') as HTMLElement;
    await fireEvent.click(control);
    const bound = view.container.querySelector('[data-answering]');
    expect(bound?.getAttribute('data-answer-segment-id')).toBe('q2');
    const textarea = view.getByLabelText('Write a message') as HTMLTextAreaElement;
    await fireEvent.input(textarea, {target: {value: 'node a first'}});
    await fireEvent.submit(view.container.querySelector('[data-conversation-composer]') as HTMLFormElement);
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalled());
    const [, , options] = api.sendDriverConversationMessage.mock.calls[0];
    expect(options.answers).toEqual([{message_id: 'm1', segment_id: 'q2'}]);
  });

  it('an ordinary send carries no answers, so it is the same request as before', async () => {
    api.dshDriverConversation.mockResolvedValue(page([driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00')]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());
    const textarea = view.getByLabelText('Write a message') as HTMLTextAreaElement;
    await fireEvent.input(textarea, {target: {value: 'hello'}});
    await fireEvent.submit(view.container.querySelector('[data-conversation-composer]') as HTMLFormElement);
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalled());
    const [, , options] = api.sendDriverConversationMessage.mock.calls[0];
    expect('answers' in options).toBe(false);
  });
});

// ── answered questions stay on the rail, marked as answered ─────────────────
// The rail keeps every question so the reader can look back at what they
// answered. An answered question is shown with the answer and has NO reply
// control: the reply control exists only for an open question.
describe('the rail keeps answered questions, marked as answered', () => {
  it('a conversation whose only question is answered still renders the rail, with the answered group and no reply control', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: 'may I restart?'}]}),
      userMsg('u1', '2026-11-03T11:00:00+00:00', {body: 'yes, after the sweep',
        answers: [{message_id: 'm1', segment_id: 'q1'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    // Wait for the answered group itself: the box exists before the walk's rows
    // arrive, and this test reads the group, not the box.
    await waitFor(() => expect(view.container.querySelector('[data-questions-answered]')).not.toBeNull());
    const rail = view.container.querySelector('[data-questions-box]') as HTMLElement;

    // The answered group is present, with its title and the one question.
    const title = rail.querySelector('[data-questions-answered]');
    expect(title).not.toBeNull();
    expect(title?.textContent).toContain('Answered');
    expect(title?.textContent).toContain('(1)');
    const item = rail.querySelector('.questions-list-answered [data-question-segment-id="q1"]') as HTMLElement;
    expect(item).not.toBeNull();
    expect(item.classList.contains('question-item-answered')).toBe(true);
    expect(item.getAttribute('data-question-message-id')).toBe('m1');
    expect(item.getAttribute('data-question-answered-by')).toBe('u1');
    // It names the question, and the answer the user gave.
    expect(item.textContent).toContain('may I restart?');
    expect(item.textContent).toContain('You answered: ');
    expect(item.querySelector('.question-answer-text')?.textContent).toBe('yes, after the sweep');
    // No reply control on an answered question. The open count is in the box's
    // SUMMARY (0 open here), so the body carries only the answered-group title.
    expect(rail.querySelector('[data-answer-message-id]')).toBeNull();
    expect(rail.querySelector('[data-answer-segment-id="q1"]')).toBeNull();
    expect(rail.querySelector('summary')?.textContent).toContain('Open questions (0)');
    expect(rail.querySelectorAll('.questions-title')).toHaveLength(1);
  });

  it('an open question renders above the answered group with its reply control, and the answered one has none', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: 'answered one?'}]}),
      driverMsg('m2', 'Report 2', '2026-11-03T10:30:00+00:00', {segments: [
        {id: 'q2', kind: 'question', text: 'still open?'}]}),
      userMsg('u1', '2026-11-03T11:00:00+00:00', {body: 'yes',
        answers: [{message_id: 'm1', segment_id: 'q1'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-questions-answered]')).not.toBeNull());
    const rail = view.container.querySelector('[data-questions-box]') as HTMLElement;

    // The open count counts only the open question, and lives in the SUMMARY.
    expect(rail.querySelector('summary')?.textContent).toContain('Open questions (1)');

    // The open question is above the answered group, and carries its reply control.
    const openItem = rail.querySelector('[data-question-segment-id="q2"]') as HTMLElement;
    const answeredTitle = rail.querySelector('[data-questions-answered]') as HTMLElement;
    expect(openItem.compareDocumentPosition(answeredTitle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const reply = rail.querySelector('[data-answer-message-id="m2"][data-answer-segment-id="q2"]');
    expect(reply).not.toBeNull();
    expect(openItem.classList.contains('question-item-answered')).toBe(false);

    // The answered one is in the answered group, with no reply control.
    const answeredItem = rail.querySelector('.questions-list-answered [data-question-segment-id="q1"]') as HTMLElement;
    expect(answeredItem).not.toBeNull();
    expect(answeredItem.querySelector('[data-answer-message-id]')).toBeNull();
    expect(answeredItem.getAttribute('data-question-answered-by')).toBe('u1');
    expect(rail.querySelector('[data-answer-message-id="m1"]')).toBeNull();
  });

  it('a conversation with no question at all and a complete walk keeps the box, saying there are none', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Plain', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'i1', kind: 'info', text: 'just context'}]}),
      userMsg('u1', '2026-11-03T11:00:00+00:00', {body: 'thanks'}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await view.findByText('Plain');
    // Inverted from the old "renders no rail": the box is permanent now, and an
    // empty conversation says so instead of disappearing.
    await waitFor(() => expect(view.container.querySelector('[data-questions-empty]')).not.toBeNull());
    expect(view.container.querySelector('[data-questions-box]')).not.toBeNull();
    expect(view.container.querySelector('[data-questions-box] [data-questions-empty]')?.textContent).toContain('No open questions.');
    expect(view.container.querySelector('[data-questions-answered]')).toBeNull();
    expect(view.container.querySelectorAll('[data-answer-message-id]')).toHaveLength(0);
  });
});

// ── the rail does not depend on the thread's page ───────────────────────────
// The rail used to be derived from `messages`: the thread's LOADED page. On a
// fresh mount that page is the newest 30 rows, so a question followed by 30 or
// more later info messages dropped off the rail until the user clicked "load
// earlier" - the "washed away by info" failure moved from scrolling to paging.
// These tests mount exactly that conversation, served page by page.
describe('the rail reads the whole conversation, not the thread page', () => {
  it('lists a question that 30 or more later info messages pushed out of the loaded page', async () => {
    serveConversation(washedAwayConversation().newestFirst);
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});

    // Where this test starts: the thread's page is the newest 30 rows, and the
    // question's message is not one of them. A rail built from the thread could
    // not show it - that is the bug.
    await waitFor(() => expect(
      view.container.querySelectorAll('[data-conversation-thread] article.message')).toHaveLength(30));
    const thread = view.container.querySelector('[data-conversation-thread]') as HTMLElement;
    expect(thread.textContent).not.toContain('may I restart the host?');
    expect(thread.textContent).toContain('Info 39');

    // The rail lists it anyway, because it read the rest of the conversation.
    await waitFor(() => expect(view.container.querySelector('[data-question-segment-id="q1"]')).not.toBeNull());
    const rail = view.container.querySelector('[data-questions-box]') as HTMLElement;
    expect(rail).not.toBeNull();
    expect(rail.textContent).toContain('may I restart the host?');
    expect([...rail.querySelectorAll('[data-question-segment-id]')]
      .map(item => item.getAttribute('data-question-segment-id'))).toEqual(['q1']);
    expect(rail.querySelector('[data-answer-message-id="m0"][data-answer-segment-id="q1"]')).not.toBeNull();
    // It got there by reading BEHIND the thread's page, at the route's largest page.
    expect(api.dshDriverConversation.mock.calls.some(
      ([, cursor, limit]) => cursor === 'i10' && limit === 200)).toBe(true);
    // It reached the beginning of the conversation, so it claims nothing less.
    expect(rail.querySelector('[data-questions-incomplete]')).toBeNull();
  });

  it('still answers a question the thread page no longer holds', async () => {
    serveConversation(washedAwayConversation().newestFirst);
    const view = render(DriverConversation, {projectId: 'alpha'});
    const control = await view.findByLabelText('Answer this question: may I restart the host?');
    await fireEvent.click(control);
    expect(view.container.querySelector('[data-answering]')?.getAttribute('data-answer-segment-id')).toBe('q1');

    await fireEvent.input(view.getByLabelText('Write a message'), {target: {value: 'yes, go ahead'}});
    await fireEvent.submit(view.container.querySelector('[data-conversation-composer]') as HTMLFormElement);
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalled());
    const [, , options] = api.sendDriverConversationMessage.mock.calls[0];
    // The citation is what closes the question; the message it belongs to is not
    // on the thread, so there is no reply link to set - and none is invented.
    expect(options.answers).toEqual([{message_id: 'm0', segment_id: 'q1'}]);
    expect(options.replyToMessageId).toBeUndefined();
  });

  it('says the list may be missing older questions when it stopped short, instead of pretending', async () => {
    // Every page claims more history behind it, so only the walk's own page
    // bound can stop it. The rail must report that rather than look complete.
    let served = 0;
    api.dshDriverConversation.mockImplementation(async () => {
      served += 1;
      const row = driverMsg('m' + served, 'Report ' + served, at(served),
        {segments: [{id: 'q' + served, kind: 'question', text: 'question ' + served}]});
      return {...page([row]), has_more: true, next_cursor: 'c' + served, end_of_history: false};
    });
    const view = render(DriverConversation, {projectId: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-questions-incomplete]')).not.toBeNull());
    const note = view.container.querySelector('[data-questions-incomplete]') as HTMLElement;
    expect(note.textContent).toContain('Older questions may be missing');
    // The questions it did read are still listed: an incomplete list is not an empty one.
    expect(view.container.querySelectorAll('[data-question-segment-id]').length).toBeGreaterThan(0);
  });

  it('warns that older questions may be missing even when the short list holds no question at all', async () => {
    // Round 2's hole: the warning lived inside `{#if questions.length}`, so a
    // walk that failed before finding ANY question drew nothing - the reader saw
    // no rail, no warning, and an older question stayed invisible and unsignalled.
    const infos = Array.from({length: 40}, (_, i) => driverMsg('i' + i, 'Info ' + i, at(i + 1),
      {segments: [{id: 'i' + i, kind: 'info', text: 'nothing to report ' + i}]}));
    const newestFirst = [...infos].reverse();
    api.dshDriverConversation.mockImplementation(async (_project: string, cursor?: string | null, limit = 30) => {
      const start = cursor ? newestFirst.findIndex(row => row.message_id === cursor) + 1 : 0;
      if (start > 0) throw new Error('the walk died on the second page');
      const slice = newestFirst.slice(start, start + limit);
      const more = start + slice.length < newestFirst.length;
      return {...page(slice), has_more: more, next_cursor: more ? slice[slice.length - 1].message_id : null,
        end_of_history: !more};
    });
    const view = render(DriverConversation, {projectId: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-questions-incomplete]')).not.toBeNull());
    const note = view.container.querySelector('[data-questions-incomplete]') as HTMLElement;
    expect(note.textContent).toContain('Older questions may be missing');
    // No question was read, so the rail is warning only - and it still says so.
    expect(view.container.querySelectorAll('[data-question-segment-id]')).toHaveLength(0);
    expect(view.container.querySelectorAll('[data-answer-message-id]')).toHaveLength(0);
  });

  it('walks again after a failed walk, so one transient error does not hide the question for the life of the pane', async () => {
    const {asked, newestFirst} = washedAwayConversation();
    let walkFailures = 1;
    api.dshDriverConversation.mockImplementation(async (_project: string, cursor?: string | null, limit = 30) => {
      const start = cursor ? newestFirst.findIndex(row => row.message_id === cursor) + 1 : 0;
      if (start > 0 && walkFailures > 0) { walkFailures -= 1; throw new Error('transient'); }
      const slice = newestFirst.slice(start, start + limit);
      const more = start + slice.length < newestFirst.length;
      return {...page(slice), has_more: more, next_cursor: more ? slice[slice.length - 1].message_id : null,
        end_of_history: !more};
    });
    const view = render(DriverConversation, {projectId: 'alpha'});
    // The first walk died before reaching the oldest row, so the question the
    // conversation starts with is missing and the rail says the list is short.
    await waitFor(() => expect(view.container.querySelector('[data-questions-incomplete]')).not.toBeNull());
    expect(view.container.querySelectorAll('[data-question-segment-id]')).toHaveLength(0);
    // A poll that succeeds is the retry: the seed was released by the failure.
    document.dispatchEvent(new Event('visibilitychange'));
    await waitFor(() => expect(view.container.querySelectorAll('[data-question-segment-id]')).toHaveLength(1));
    expect((view.container.querySelector('[data-question-segment-id]') as HTMLElement)
      .getAttribute('data-question-message-id')).toBe(asked.message_id);
    // And the warning is withdrawn, because this walk reached the end.
    expect(view.container.querySelector('[data-questions-incomplete]')).toBeNull();
  });
});

// ── the box folds, and the fold survives the pane's own refresh ─────────────
// The box is a native <details>: the summary is the fold control, the open count
// is always in it, and collapsed it shows one summary line with no question item.
// Its toggle belongs to the mounted pane, so a new message (the pane's own poll)
// must not reset it.
//
// jsdom does NOT toggle `details.open` when a summary is clicked (a browser
// does). So the fold is driven the way a browser drives it: set `open`, then fire
// the `toggle` event the browser fires after any change. `bind:open` listens to
// that event, so this exercises the component's real path, not a stand-in.
function toggleFold(box: HTMLDetailsElement, open: boolean) {
  box.open = open;
  box.dispatchEvent(new Event('toggle'));
}
describe('the questions box folds', () => {
  it('collapses and expands on the summary, keeps the open count in the summary, and hides the items while collapsed', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: 'may I restart?'},
        {id: 'q2', kind: 'question', text: 'which node first?'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-question-segment-id="q2"]')).not.toBeNull());
    const box = view.container.querySelector('[data-questions-box]') as HTMLDetailsElement;
    const summary = box.querySelector('summary') as HTMLElement;
    expect(summary.textContent).toContain('Open questions (2)');
    expect(box.open).toBe(true);

    // Collapse: the open count stays in the summary and no question item renders.
    toggleFold(box, false);
    await waitFor(() => expect(view.container.querySelectorAll('[data-question-segment-id]')).toHaveLength(0));
    expect(box.open).toBe(false);
    expect(summary.textContent).toContain('Open questions (2)');
    expect(view.container.querySelectorAll('[data-answer-message-id]')).toHaveLength(0);

    // Expand again: the items are back, each with its reply control.
    toggleFold(box, true);
    await waitFor(() => expect(view.container.querySelectorAll('[data-question-segment-id]')).toHaveLength(2));
    expect(box.open).toBe(true);
    expect(view.container.querySelectorAll('[data-question-segment-id]')).toHaveLength(2);
    expect(view.container.querySelector('[data-answer-message-id="m1"][data-answer-segment-id="q1"]')).not.toBeNull();
  });

  it('keeps the fold after a new message arrives on the pane\u2019s own poll', async () => {
    const newest = [driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
      {id: 'q1', kind: 'question', text: 'may I restart?'}]})];
    api.dshDriverConversation.mockResolvedValue(page(newest));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-question-segment-id="q1"]')).not.toBeNull());
    const box = view.container.querySelector('[data-questions-box]') as HTMLDetailsElement;

    // The reader folds the box away.
    toggleFold(box, false);
    await waitFor(() => expect(box.open).toBe(false));

    // The driver sends a new message; the pane's own newest read (the poll) picks
    // it up. The box must stay collapsed: a refresh does not reopen what the
    // reader closed.
    const later = driverMsg('m2', 'Report 2', '2026-11-03T11:00:00+00:00', {segments: [
      {id: 'q2', kind: 'question', text: 'a second question?'}]});
    api.dshDriverConversation.mockResolvedValue(page([later, ...newest]));
    // Drive the pane's own newest read: a visibility catch-up is the same read the
    // interval performs. A collapsed box renders no items, so the arrival is seen
    // in the summary count, which is derived from the same rows.
    document.dispatchEvent(new Event('visibilitychange'));
    await waitFor(() => expect(box.querySelector('summary')?.textContent).toContain('Open questions (2)'));
    expect(box.open).toBe(false);
    expect(view.container.querySelectorAll('[data-question-segment-id]')).toHaveLength(0);
  });

  it('shows the incomplete marker in the SUMMARY while the box is collapsed, and the full sentence only when expanded', async () => {
    // Every page claims more history and hands back no cursor: the walk stops
    // short, so `railIncomplete` is true.
    let served = 0;
    api.dshDriverConversation.mockImplementation(async () => {
      served += 1;
      const row = driverMsg('m' + served, 'Report ' + served, at(served),
        {segments: [{id: 'q' + served, kind: 'question', text: 'question ' + served}]});
      return {...page([row]), has_more: true, next_cursor: null, end_of_history: false};
    });
    const view = render(DriverConversation, {projectId: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-questions-incomplete-summary]')).not.toBeNull());
    const box = view.container.querySelector('[data-questions-box]') as HTMLDetailsElement;
    const summary = box.querySelector('summary') as HTMLElement;
    // Collapsed: the caveat is still visible on the summary line.
    toggleFold(box, false);
    await waitFor(() => expect(box.open).toBe(false));
    expect(summary.querySelector('[data-questions-incomplete-summary]')?.textContent).toContain('may be incomplete');
    // The full sentence is not in the collapsed body (there is no body).
    expect(view.container.querySelector('[data-questions-incomplete]')).toBeNull();
    // Expanded: the full sentence is back in the body, and the summary marker stays.
    toggleFold(box, true);
    await waitFor(() => expect(view.container.querySelector('[data-questions-incomplete]')).not.toBeNull());
    expect(box.open).toBe(true);
    expect(view.container.querySelector('[data-questions-incomplete]')?.textContent).toContain('Older questions may be missing');
    expect(summary.querySelector('[data-questions-incomplete-summary]')).not.toBeNull();
  });

  it('lists a question beyond the thread page while the box is open, and answers it from there', async () => {
    // The end-to-end property that must not regress with the fold: a question
    // 40 messages back is listed (read behind the thread's first page), its reply
    // control binds exactly that (message_id, segment_id), and the sent body
    // cites exactly that one question.
    const {asked, newestFirst} = washedAwayConversation();
    serveConversation(newestFirst);
    const view = render(DriverConversation, {projectId: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-question-segment-id="q1"]')).not.toBeNull());
    expect(view.container.querySelector('[data-conversation-thread]')?.textContent).not.toContain('may I restart the host?');
    const control = view.container.querySelector('[data-answer-message-id="m0"][data-answer-segment-id="q1"]') as HTMLElement;
    expect(control).not.toBeNull();
    await fireEvent.click(control);
    await fireEvent.input(view.getByLabelText('Write a message'), {target: {value: 'yes, go ahead'}});
    await fireEvent.submit(view.container.querySelector('[data-conversation-composer]') as HTMLFormElement);
    await waitFor(() => expect(api.sendDriverConversationMessage).toHaveBeenCalled());
    const [, , options] = api.sendDriverConversationMessage.mock.calls[0];
    expect(options.answers).toEqual([{message_id: asked.message_id, segment_id: 'q1'}]);
  });

  it('shows an answered question with the answer and no reply control while the box is open', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: 'may I restart?'}]}),
      userMsg('u1', '2026-11-03T11:00:00+00:00', {body: 'yes, after the sweep',
        answers: [{message_id: 'm1', segment_id: 'q1'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-questions-answered]')).not.toBeNull());
    const item = view.container.querySelector('.questions-list-answered [data-question-segment-id="q1"]') as HTMLElement;
    expect(item.textContent).toContain('yes, after the sweep');
    expect(item.querySelector('[data-answer-message-id]')).toBeNull();
    expect(view.container.querySelector('[data-questions-box] [data-answer-message-id]')).toBeNull();
  });
});
