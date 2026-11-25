/** The family groups under the WeChat channel (step 23, design-steward 7.7.13; step 23.1, 7.7.24:
 *  one row per bound group): naming a member calls nameGroupMember with the group and the bridge's
 *  member id, and unbinding (which deletes the records, family memory and family note) asks first
 *  and names its group; 「添加群」 needs a name and stops at groups_per_channel. The API is mocked. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  listOwnerGroups: vi.fn(), nameGroupMember: vi.fn(), unbindOwnerGroup: vi.fn(), createGroupCode: vi.fn(),
  clearGroupRecords: vi.fn(), setFamilyNote: vi.fn(), deleteFamilyMemory: vi.fn(), setGroupMediaKeep: vi.fn(),
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

  // design/steward-group-media.md section 9 (owner 2026-11-23): the media switch, its four states,
  // why it waits and the last deletion; the switch asks the backend, which decides the state.
  describe('the media switch', () => {
    const keep = (over: Record<string, unknown>) => ({state: 'off', since: null, note: null, attempts: 0, purge_owed: false,
      kept_24h: 0, last_purge: null, ...over});
    const statusOf = async (mediaKeep: Record<string, unknown>) => {
      api.listOwnerGroups.mockResolvedValue({groups: [{...group(), media_keep: mediaKeep}], groups_per_channel: 3});
      langStore.set('zh-CN');
      const view = render(StewardGroups, {channel: 'wechat', bound: true});
      const status = await waitFor(() => {
        const el = view.container.querySelector('[data-media-keep-status]');
        expect(el).not.toBeNull();
        return el as HTMLElement;
      });
      const box = view.container.querySelector('[data-media-keep="r1@chatroom"]') as HTMLInputElement;
      return {view, status: status.textContent?.trim(), box, purge: view.container.querySelector('[data-media-purge]')?.textContent?.trim()};
    };

    it('off: 关, unchecked; clicking turns it on through the backend', async () => {
      api.setGroupMediaKeep.mockResolvedValue({state: 'on_pending'});
      const {view, status, box} = await statusOf(keep({}));
      expect(status).toBe('关');
      expect(box.checked).toBe(false);
      expect(view.container.textContent).toContain('存群里没 @ 管家的图片、语音、文件（存 1 天；关掉即删除已存的）');
      await fireEvent.click(box);
      await waitFor(() => expect(api.setGroupMediaKeep).toHaveBeenCalledWith('wechat', 'r1@chatroom', true));
    });

    it('on_pending: 正在通知群里… with why it waits; on is not shown before the notice went out', async () => {
      const {status, box} = await statusOf(keep({state: 'on_pending', note: 'group_send_budget'}));
      expect(status).toBe('正在通知群里… · 群里本小时发送已满');
      expect(box.checked).toBe(true);
      cleanup();
      expect((await statusOf(keep({state: 'on_pending', note: 'channel_down'}))).status).toBe('正在通知群里… · 渠道未连接');
    });

    it('on: 开 with since when and what it kept today; clicking turns it off', async () => {
      api.setGroupMediaKeep.mockResolvedValue({state: 'off_notice'});
      const {status, box} = await statusOf(keep({state: 'on', since: '2026-11-23T09:41:00Z', kept_24h: 3}));
      expect(status).toMatch(/^开（自 .+）\s·\s今天存了 3 件$/);
      expect(box.checked).toBe(true);
      await fireEvent.click(box);
      await waitFor(() => expect(api.setGroupMediaKeep).toHaveBeenCalledWith('wechat', 'r1@chatroom', false));
    });

    it('off_notice: 已关，正在删除 while the purge is owed, then 正在通知群里 with the last receipt', async () => {
      expect((await statusOf(keep({state: 'off_notice', purge_owed: true}))).status).toBe('已关，正在删除');
      cleanup();
      const purge = {at: '2026-11-23T10:00:00Z', reason: 'off', backend_rows: 4, rows_before: 4, files_unlinked: 3, files_shared: 1, bytes_freed: 99};
      const told = await statusOf(keep({state: 'off_notice', last_purge: purge}));
      expect(told.status).toBe('已关，正在通知群里');
      expect(told.purge).toBe('已删除 4 件（1 件文件另有记录引用，未删）');
      expect(told.box.checked).toBe(false);
    });

    it('a notice that was never sent says so, and the switch reads off', async () => {
      const {status, box} = await statusOf(keep({state: 'off', note: 'notice_not_sent', attempts: 3}));
      expect(status).toBe('关 · 通知没发出去（开关仍是关）');
      expect(box.checked).toBe(false);
    });
  });

  // Step 23, Slack (design-steward 7.7.19): a private channel is a group like a WeChat one, without
  // the media switch (the backend lists it without media_keep), and its code is posted as a mention.
  it('Slack: a bound private channel shows no media switch, and its code help says private channel, invite and mention', async () => {
    const slackGroup: OwnerGroup = {...group(), channel: 'slack', group_ref: 'C0PRIVATE', mention_prefix: '<@U0BOT>'};
    api.listOwnerGroups.mockResolvedValue({groups: [slackGroup], groups_per_channel: 3});
    api.createGroupCode.mockResolvedValue({code: 'G-ABCD-EFGH', expires_at: 'x'});
    const view = render(StewardGroups, {channel: 'slack', bound: true});
    await waitFor(() => expect(view.container.querySelector('[data-group="C0PRIVATE"]')).not.toBeNull());
    expect(view.container.querySelector('[data-media-keep-box]')).toBeNull();
    const input = view.container.querySelector('[data-group-label]') as HTMLInputElement;
    await fireEvent.input(input, {target: {value: '家庭频道'}});
    await fireEvent.click(view.container.querySelector('[data-group-add]') as HTMLElement);
    await waitFor(() => expect(api.createGroupCode).toHaveBeenCalledWith('slack', '家庭频道'));
    const help = await waitFor(() => {
      const text = view.container.querySelector('[data-group-code] p')?.textContent ?? '';
      expect(text).not.toBe('');
      return text;
    });
    expect(help).toContain('private channel');
    expect(help).toContain('/invite @AItelier Steward');
    expect(help).toContain('Slack Connect');
  });

  it('a paused group shows the 已暂停 badge from its projection field, and none when it is null', async () => {
    api.listOwnerGroups.mockResolvedValue({groups: [{...group(), paused: {reason: 'shared', at: '2026-11-24T10:00:00Z'}}],
      groups_per_channel: 3});
    langStore.set('zh-CN');
    const view = render(StewardGroups, {channel: 'wechat', bound: true});
    const badge = await waitFor(() => {
      const el = view.container.querySelector('[data-group-paused]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    expect(badge.textContent?.trim()).toBe('已暂停');
    expect(badge.title).toBe('shared');
    const titleLine = view.container.querySelector('[data-group-bound]')?.textContent ?? '';
    expect(titleLine).toContain('家庭群');
    expect(titleLine).toContain('已暂停');
    cleanup();
    langStore.set('en');
    const english = render(StewardGroups, {channel: 'wechat', bound: true});
    const enBadge = await waitFor(() => {
      const el = english.container.querySelector('[data-group-paused]') as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    expect(enBadge.textContent?.trim()).toBe('Paused');
    cleanup();
    api.listOwnerGroups.mockResolvedValue({groups: [{...group(), paused: null}], groups_per_channel: 3});
    const clear = render(StewardGroups, {channel: 'wechat', bound: true});
    await waitFor(() => expect(clear.container.querySelector('[data-group-bound]')).not.toBeNull());
    expect(clear.container.querySelector('[data-group-paused]')).toBeNull();
    cleanup();
    api.listOwnerGroups.mockResolvedValue({groups: [group()], groups_per_channel: 3});
    const absent = render(StewardGroups, {channel: 'wechat', bound: true});
    await waitFor(() => expect(absent.container.querySelector('[data-group-bound]')).not.toBeNull());
    expect(absent.container.querySelector('[data-group-paused]')).toBeNull();
  });
});
