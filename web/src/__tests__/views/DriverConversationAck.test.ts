import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';
const api = vi.hoisted(() => ({ dshDriverConversation: vi.fn(), sendDriverConversationMessage: vi.fn(), markDriverConversationRead: vi.fn() }));
vi.mock('../../lib/api', () => api);
import DriverConversation from '../../views/DriverConversation.svelte';

// The steward's receipt (owner 2026-11-18: "收到消息先ack，再处理/回复正式内容"): one status
// line under the owner's newest message while nothing has answered it, from the driver's
// phase - no model step, nothing written to the conversation.
const entry = (id: string, kind: 'driver' | 'user', created: string) =>
  ({message_id: id, thread_id: id, project_id: 'house', sender_kind: kind,
    sender_driver_id: kind === 'driver' ? 'dsh-house' : null, subject: 's-' + id, body: 'b-' + id, refs: {},
    reply_to_message_id: null, created_at: created});
const page = (messages: any[]) =>
  ({project_id: 'house', messages, has_more: false, next_cursor: null, end_of_history: true,
    matched_total: messages.length, unread_count: 0, read_upto_message_id: null, read_upto_created_at: null});
const ownerLast = [entry('m2', 'user', '2026-11-18T10:00:00+00:00'), entry('m1', 'driver', '2026-11-18T09:00:00+00:00')];
const ackOf = (view: ReturnType<typeof render>) => view.container.querySelector('[data-conversation-ack]');

beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('zh');
  api.markDriverConversationRead.mockResolvedValue({});
  api.dshDriverConversation.mockResolvedValue(page(ownerLast));
});
afterEach(() => cleanup());

describe('the steward receipt under an unanswered owner message', () => {
  it.each([
    ['running', '已收到，正在处理…'],
    ['waiting', '已送达，马上处理…'],
    ['dormant', '已送达，马上处理…'],
    ['starting', '已送达，马上处理…'],
    ['failed', '没有在运行：消息已保存，启动后会回复。'],
    [undefined, '没有在运行：消息已保存，启动后会回复。'],
  ])('phase %s says %s', async (phase, text) => {
    const view = render(DriverConversation, {projectId: 'house', ack: {phase}});
    await view.findByText('b-m2');
    expect(ackOf(view)?.textContent).toBe(text);
    expect(ackOf(view)?.getAttribute('role')).toBe('status');
    // It sits after the last message, not inside it.
    expect(ackOf(view)?.previousElementSibling?.getAttribute('data-message-id')).toBe('m2');
  });

  it('is gone once a reply follows the owner message', async () => {
    api.dshDriverConversation.mockResolvedValue(page([entry('m3', 'driver', '2026-11-18T10:01:00+00:00'), ...ownerLast]));
    const view = render(DriverConversation, {projectId: 'house', ack: {phase: 'running'}});
    await view.findByText('b-m3');
    expect(ackOf(view)).toBeNull();
  });

  it('a host that does not ask for it (a director pane) shows none', async () => {
    const view = render(DriverConversation, {projectId: 'house'});
    await view.findByText('b-m2');
    expect(ackOf(view)).toBeNull();
  });
});
