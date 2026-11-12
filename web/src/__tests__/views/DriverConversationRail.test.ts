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
