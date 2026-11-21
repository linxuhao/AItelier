/** The family group under the WeChat channel (step 23, design-steward 7.7.13): naming a
 *  member calls nameGroupMember with the bridge's member id, and unbinding (which deletes the
 *  records, family memory and family note) asks first. The API is mocked throughout. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  listOwnerGroups: vi.fn(), nameGroupMember: vi.fn(), unbindOwnerGroup: vi.fn(), createGroupCode: vi.fn(),
  clearGroupRecords: vi.fn(), setFamilyNote: vi.fn(), deleteFamilyMemory: vi.fn(),
}));
vi.mock('../../lib/api', async (original) => ({...(await original<object>()), ...api}));
import type { OwnerGroup } from '../../lib/api';
import StewardGroups from '../../views/StewardGroups.svelte';

const group = (): OwnerGroup => ({
  channel: 'wechat', group_ref: 'r1@chatroom', label: '家庭群', mention_prefix: '@管家', bound_at: '2026-11-21T10:00:00Z',
  messages_30d: 2, last_message_at: '2026-11-21T10:05:00Z', members: [{sender_ref: 'wxid_m1', label: '成员甲'} as never],
  messages: [], family_note: null, family_memory: [],
});

describe('StewardGroups', () => {
  beforeEach(() => {
    cleanup(); vi.resetAllMocks(); langStore.set('en');
    api.listOwnerGroups.mockResolvedValue({groups: [group()]});
    api.nameGroupMember.mockResolvedValue({});
    api.unbindOwnerGroup.mockResolvedValue({});
  });

  it('naming a member sends the member id the bridge reported and the new name', async () => {
    const view = render(StewardGroups, {channel: 'wechat', bound: true});
    await waitFor(() => expect(view.container.querySelector('[data-group-bound]')).not.toBeNull());
    await fireEvent.click(view.getByRole('button', {name: 'Records'}));
    const name = await waitFor(() => {
      const el = view.container.querySelector('[data-member-name="wxid_m1"]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue(' 妈妈 ');
    try {
      await fireEvent.click(name);
      await waitFor(() => expect(api.nameGroupMember).toHaveBeenCalledWith('wechat', 'wxid_m1', '妈妈'));
      expect(prompt).toHaveBeenCalledWith('Name', '成员甲');
    } finally { prompt.mockRestore(); }
  });

  it('director 489 (b): a reminder row (kind notice, sender_role null) is the steward\'s notice, never a member nor blank', async () => {
    const rec = (over: Record<string, unknown>) => ({accepted: 1, sender_ref: null, sender_role: null, sender_label: null, original: null,
      withheld_reason: null, reply_to: null, answered_by: null, at: '2026-11-21T10:00:00Z', ...over});
    api.listOwnerGroups.mockResolvedValue({groups: [{...group(), messages: [
      rec({message_id: 'n1', direction: 'out', kind: 'notice', body: '该吃药了', sender_label: '管家通知'}),
      rec({message_id: 'q1', direction: 'in', kind: 'question', body: '几点了', sender_ref: 'wxid_m1', sender_role: 'member', sender_label: '成员甲'}),
      rec({message_id: 'r1', direction: 'out', kind: 'reply', body: '八点'}),
    ]}]});
    langStore.set('zh');
    const view = render(StewardGroups, {channel: 'wechat', bound: true});
    await waitFor(() => expect(view.container.querySelector('[data-group-bound]')).not.toBeNull());
    await fireEvent.click(view.getByRole('button', {name: '查看记录'}));
    const records = await waitFor(() => {
      const els = [...view.container.querySelectorAll('[data-group-record]')] as HTMLElement[];
      expect(els.length).toBe(3);
      return els;
    });
    const [n, q, r] = records.map(el => el.textContent ?? '');
    expect(n).toContain('管家通知:');
    expect(n).toContain('该吃药了');
    expect(n).not.toContain('成员');
    expect(q).toContain('成员甲:');
    expect(r).toContain('→');
    expect(r).not.toContain('管家通知');
    langStore.set('en');
  });

  it('unbinding asks first: declined, nothing is unbound; confirmed, the group is unbound', async () => {
    const view = render(StewardGroups, {channel: 'wechat', bound: true});
    const unbind = await waitFor(() => {
      const el = view.container.querySelector('[data-group-unbind]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    try {
      await fireEvent.click(unbind);
      expect(confirm).toHaveBeenCalledWith('Unbind this group and delete its records, family memory and family note?');
      expect(api.unbindOwnerGroup).not.toHaveBeenCalled();
      confirm.mockReturnValue(true);
      await fireEvent.click(unbind);
      await waitFor(() => expect(api.unbindOwnerGroup).toHaveBeenCalledWith('wechat'));
    } finally { confirm.mockRestore(); }
  });
});
