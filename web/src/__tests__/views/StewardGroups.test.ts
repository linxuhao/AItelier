/** The family groups under the WeChat channel (step 23, design-steward 7.7.13; step 23.1, 7.7.24:
 *  one row per bound group): naming a member calls nameGroupMember with the group and the bridge's
 *  member id, and unbinding (which deletes the records, family memory and family note) asks first
 *  and names its group; 「添加群」 needs a name and stops at groups_per_channel. The API is mocked. */
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
    api.listOwnerGroups.mockResolvedValue({groups: [group()], groups_per_channel: 3});
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
      await waitFor(() => expect(api.nameGroupMember).toHaveBeenCalledWith('wechat', 'r1@chatroom', 'wxid_m1', '妈妈'));
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
    ]}], groups_per_channel: 3});
    langStore.set('zh-CN');
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
      expect(confirm).toHaveBeenCalledWith('Unbind «家庭群» and delete its records, family memory and family note? Its lists stay on the dashboard; your other groups are untouched.');
      expect(api.unbindOwnerGroup).not.toHaveBeenCalled();
      confirm.mockReturnValue(true);
      await fireEvent.click(unbind);
      await waitFor(() => expect(api.unbindOwnerGroup).toHaveBeenCalledWith('wechat', 'r1@chatroom'));
    } finally { confirm.mockRestore(); }
  });

  it('step 23.1: one row per bound group, each action naming its own group', async () => {
    const second = {...group(), group_ref: 'r2@chatroom', label: '老家群', members: [{sender_ref: 'wxid_m2', label: '成员甲'} as never]};
    api.listOwnerGroups.mockResolvedValue({groups: [group(), second], groups_per_channel: 3});
    api.clearGroupRecords.mockResolvedValue({});
    const view = render(StewardGroups, {channel: 'wechat', bound: true});
    await waitFor(() => expect(view.container.querySelectorAll('[data-group-bound]').length).toBe(2));
    expect([...view.container.querySelectorAll('[data-group-bound]')].map(e => e.textContent)).toEqual([
      expect.stringContaining('家庭群'), expect.stringContaining('老家群')]);
    await fireEvent.click(view.container.querySelector('[data-group-clear="r2@chatroom"]') as HTMLElement);
    await waitFor(() => expect(api.clearGroupRecords).toHaveBeenCalledWith('wechat', 'r2@chatroom'));
    // Opening B's records asks for B's detail only.
    await fireEvent.click(view.container.querySelector('[data-group-records="r2@chatroom"]') as HTMLElement);
    await waitFor(() => expect(api.listOwnerGroups).toHaveBeenLastCalledWith('wechat', 'r2@chatroom'));
    // The add form is there while fewer than the cap are bound, and needs a name.
    expect(view.container.querySelector('[data-group-add]')).not.toBeNull();
    expect((view.container.querySelector('[data-group-add]') as HTMLButtonElement).disabled).toBe(true);
  });

  it('step 23.1: 「添加群」 sends the name; at the cap it is gone; a refusal reads in words', async () => {
    api.createGroupCode.mockResolvedValue({code: 'G-ABCD-EFGH', expires_at: 'x'});
    const view = render(StewardGroups, {channel: 'wechat', bound: true});
    const input = await waitFor(() => view.container.querySelector('[data-group-label]') as HTMLInputElement);
    await fireEvent.input(input, {target: {value: ' 老家群 '}});
    await fireEvent.click(view.container.querySelector('[data-group-add]') as HTMLElement);
    await waitFor(() => expect(api.createGroupCode).toHaveBeenCalledWith('wechat', '老家群'));
    await waitFor(() => expect(view.container.querySelector('[data-group-code]')?.textContent).toContain('G-ABCD-EFGH'));
    cleanup();
    api.createGroupCode.mockRejectedValue(new Error('409: group_label_taken: a bound group already has this name; choose another'));
    const again = render(StewardGroups, {channel: 'wechat', bound: true});
    const name = await waitFor(() => again.container.querySelector('[data-group-label]') as HTMLInputElement);
    await fireEvent.input(name, {target: {value: '家庭群'}});
    await fireEvent.click(again.container.querySelector('[data-group-add]') as HTMLElement);
    await waitFor(() => expect(again.getByRole('alert').textContent).toBe('A bound group already has this name'));
    cleanup();
    const three = ['r1', 'r2', 'r3'].map((ref, i) => ({...group(), group_ref: ref + '@chatroom', label: '群' + i}));
    api.listOwnerGroups.mockResolvedValue({groups: three, groups_per_channel: 3});
    const full = render(StewardGroups, {channel: 'wechat', bound: true});
    await waitFor(() => expect(full.container.querySelector('[data-groups-full]')?.textContent).toContain('3 groups are bound'));
    expect(full.container.querySelector('[data-group-add]')).toBeNull();
  });
});
