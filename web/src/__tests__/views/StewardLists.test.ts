/** The family lists on the dashboard (step 25 C4, design-steward 7.9.5): both lists with who
 *  added and checked each item; the owner adds (several at once), checks, removes and clears the
 *  checked items. The API is mocked throughout. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  listFamilyLists: vi.fn(), addFamilyListItems: vi.fn(), checkFamilyListItems: vi.fn(), removeFamilyListItem: vi.fn(),
  clearDoneFamilyListItems: vi.fn(),
}));
vi.mock('../../lib/api', async (original) => ({...(await original<object>()), ...api}));
import type { FamilyListItem } from '../../lib/api';
import StewardLists from '../../views/StewardLists.svelte';

const item = (id: string, text: string, over: Partial<FamilyListItem> = {}): FamilyListItem => ({
  item_id: id, list: 'shopping', text, added_by: '妈妈', added_by_role: 'member', added_via: 'group',
  added_at: '2026-11-21T10:00:00Z', done: false, done_at: null, done_by: null, ...over,
});

describe('StewardLists', () => {
  beforeEach(() => {
    cleanup(); vi.resetAllMocks(); langStore.set('en');
    api.listFamilyLists.mockResolvedValue({lists: {shopping: [item('li-1', '牛奶'),
      item('li-2', '鸡蛋', {done: true, done_at: '2026-11-21T11:00:00Z', done_by: '管家'})], todo: []}});
    for (const fn of [api.addFamilyListItems, api.checkFamilyListItems, api.removeFamilyListItem, api.clearDoneFamilyListItems])
      fn.mockResolvedValue({});
  });

  it('shows both lists with who added and who checked each item', async () => {
    const view = render(StewardLists);
    await view.findByText('牛奶');
    const milk = view.container.querySelector('[data-item="li-1"]') as HTMLElement;
    expect(milk.querySelector('[data-item-who]')?.textContent).toContain('added by 妈妈');
    const eggs = view.container.querySelector('[data-item="li-2"]') as HTMLElement;
    expect(eggs.classList.contains('done')).toBe(true);
    expect(eggs.querySelector('[data-item-who]')?.textContent).toContain('checked by 管家');
    expect((view.container.querySelector('[data-list="todo"]') as HTMLElement).textContent).toContain('Nothing on this list.');
  });

  it('director 489 (a): an item of an unbound group (added_by / done_by null) reads 曾由一位成员添加, never blank, never a throw', async () => {
    api.listFamilyLists.mockResolvedValue({lists: {shopping: [item('li-1', '牛奶', {added_by: null}),
      item('li-2', '鸡蛋', {added_by: null, done: true, done_at: '2026-11-21T11:00:00Z', done_by: null})], todo: []}});
    langStore.set('zh-CN');
    const view = render(StewardLists);
    await view.findByText('牛奶');
    const milk = (view.container.querySelector('[data-item="li-1"]') as HTMLElement).querySelector('[data-item-who]')?.textContent ?? '';
    expect(milk).toContain('曾由一位成员添加');
    expect(milk).not.toContain('null');
    const eggs = (view.container.querySelector('[data-item="li-2"]') as HTMLElement).querySelector('[data-item-who]')?.textContent ?? '';
    expect(eggs).toContain('曾由一位成员添加');
    expect(eggs).toContain('曾由一位成员勾掉');
    expect(eggs).not.toContain('null');
    langStore.set('en');
  });

  it('director 505: 150 checked within 30 days, 100 shown - the rest is said (另有 50 条已勾掉的未显示), never silently dropped', async () => {
    const done = Array.from({length: 100}, (_, n) => item('d-' + n, '物品' + n, {done: true, done_at: '2026-11-21T11:00:00Z', done_by: '管家'}));
    api.listFamilyLists.mockResolvedValue({lists: {shopping: done, todo: []}, done_total: {shopping: 150, todo: 0},
      done_more: {shopping: 50, todo: 0}});
    langStore.set('zh-CN');
    const view = render(StewardLists);
    await view.findByText('物品0');
    const shopping = view.container.querySelector('[data-list="shopping"]') as HTMLElement;
    expect(shopping.querySelectorAll('[data-item]').length).toBe(100);
    expect(shopping.querySelector('[data-list-more]')?.textContent).toBe('另有 50 条已勾掉的未显示');
    expect((view.container.querySelector('[data-list="todo"]') as HTMLElement).querySelector('[data-list-more]')).toBeNull();
    langStore.set('en');
    await waitFor(() => expect(shopping.querySelector('[data-list-more]')?.textContent).toBe('50 more checked items not shown'));
  });

  it('director 505: an answer without the counts (an older backend) shows no marker and no throw', async () => {
    const view = render(StewardLists);
    await view.findByText('牛奶');
    expect(view.container.querySelector('[data-list-more]')).toBeNull();
  });

  it('review F4: more than 10 items at once is refused with a message, nothing is cut and the draft is kept', async () => {
    const view = render(StewardLists);
    await view.findByText('牛奶');
    const input = view.container.querySelector('[data-list-input="shopping"]') as HTMLInputElement;
    const eleven = Array.from({length: 11}, (_, i) => 'x' + i).join('，');
    await fireEvent.input(input, {target: {value: eleven}});
    await fireEvent.submit(input.closest('form') as HTMLFormElement);
    await waitFor(() => expect(view.getByRole('alert').textContent).toBe('At most 10 items at a time: nothing was added'));
    expect(api.addFamilyListItems).not.toHaveBeenCalled();
    expect(input.value).toBe(eleven);
  });

  it('adds several items at once, checks, removes and clears the checked ones', async () => {
    const view = render(StewardLists);
    await view.findByText('牛奶');
    const input = view.container.querySelector('[data-list-input="todo"]') as HTMLInputElement;
    await fireEvent.input(input, {target: {value: '交水费，修水龙头'}});
    await fireEvent.submit(input.closest('form') as HTMLFormElement);
    await waitFor(() => expect(api.addFamilyListItems).toHaveBeenCalledWith('todo', ['交水费', '修水龙头']));
    // One action at a time: the next control is usable once the list was read again.
    const ready = async (selector: string) => {
      const el = await waitFor(() => {
        const found = view.container.querySelector(selector) as HTMLButtonElement;
        expect(found?.disabled).toBe(false);
        return found;
      });
      await fireEvent.click(el);
    };
    await ready('[data-item-check="li-1"]');
    await waitFor(() => expect(api.checkFamilyListItems).toHaveBeenCalledWith(['li-1'], true));
    await ready('[data-item-remove="li-2"]');
    await waitFor(() => expect(api.removeFamilyListItem).toHaveBeenCalledWith('li-2'));
    await ready('[data-list-clear="shopping"]');
    await waitFor(() => expect(api.clearDoneFamilyListItems).toHaveBeenCalledWith('shopping'));
  });

  it('a refused add (withheld) is shown', async () => {
    api.addFamilyListItems.mockRejectedValue(new Error('withheld: private_project'));
    const view = render(StewardLists);
    await view.findByText('牛奶');
    const input = view.container.querySelector('[data-list-input="shopping"]') as HTMLInputElement;
    await fireEvent.input(input, {target: {value: '家庭账本'}});
    await fireEvent.submit(input.closest('form') as HTMLFormElement);
    await waitFor(() => expect(view.getByRole('alert').textContent).toContain('withheld'));
  });
});
