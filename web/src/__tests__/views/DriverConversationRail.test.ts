import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';
const api = vi.hoisted(() => ({ dshDriverConversation: vi.fn(), sendDriverConversationMessage: vi.fn(), markDriverConversationRead: vi.fn() }));
vi.mock('../../lib/api', () => api);
import DriverConversation from '../../views/DriverConversation.svelte';

// The questions rail: a SIBLING of the thread's scroll box, listing only the
// open questions, with its own reply control per question that binds the
// composer to that one question and sends `answers` with the reply.

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

describe('the questions rail', () => {
  it('is absent when no question is open, so it takes no width or height', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Info only', '2026-11-03T10:00:00+00:00', {segments: [{id: 'i1', kind: 'info', text: 'context'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await view.findByText('Info only');
    expect(view.container.querySelector('[data-questions-rail]')).toBeNull();
    expect(view.container.querySelectorAll('[data-answer-message-id]')).toHaveLength(0);
  });

  it('lists only open questions, each with its own reply control', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [
        {id: 'q1', kind: 'question', text: 'may I restart?'},
        {id: 'i1', kind: 'info', text: 'context'},
        {id: 'q2', kind: 'question', text: 'which node first?'}]}),
      userMsg('u1', '2026-11-03T11:00:00+00:00', {answers: [{message_id: 'm1', segment_id: 'q1'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());
    const rail = view.container.querySelector('[data-questions-rail]') as HTMLElement;
    expect(rail).not.toBeNull();
    // q1 is answered, so only q2 is open; the info block is never on the rail.
    const items = rail.querySelectorAll('[data-question-segment-id]');
    expect([...items].map(i => i.getAttribute('data-question-segment-id'))).toEqual(['q2']);
    expect(rail.textContent).not.toContain('context');
    const button = rail.querySelector('[data-answer-message-id="m1"][data-answer-segment-id="q2"]');
    expect(button).not.toBeNull();
  });

  it('is a sibling of the thread scroll box, never inside it', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [{id: 'q1', kind: 'question', text: 'ok?'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());
    const rail = view.container.querySelector('[data-questions-rail]') as HTMLElement;
    const thread = view.container.querySelector('[data-conversation-thread]') as HTMLElement;
    expect(rail).not.toBeNull();
    expect(thread.contains(rail)).toBe(false);
    expect(rail.contains(thread)).toBe(false);
    expect(rail.parentElement).toBe(thread.parentElement);
  });

  it('its own style scrolls on its own and never on the thread', async () => {
    api.dshDriverConversation.mockResolvedValue(page([
      driverMsg('m1', 'Report', '2026-11-03T10:00:00+00:00', {segments: [{id: 'q1', kind: 'question', text: 'ok?'}]}),
    ]));
    const view = render(DriverConversation, {projectId: 'alpha', driverId: 'dsh-alpha'});
    await waitFor(() => expect(view.container.querySelector('[data-conversation-thread] article.message')).not.toBeNull());
    const rail = view.container.querySelector('[data-questions-rail]') as HTMLElement;
    const style = getComputedStyle(rail);
    expect(style.overflowY).toBe('auto');
    expect(style.maxHeight).toBeTruthy();
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
    await waitFor(() => expect(view.container.querySelector('[data-questions-rail]')).not.toBeNull());
    const rail = view.container.querySelector('[data-questions-rail]') as HTMLElement;
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
