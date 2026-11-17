import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  createStateProject: vi.fn(), sendDriverConversationMessage: vi.fn(), startDshDriver: vi.fn(),
}));
vi.mock('../../lib/api', () => api);

import NewStateProject from '../../views/NewStateProject.svelte';

async function openForm(oncreated = vi.fn()) {
  const view = render(NewStateProject, { oncreated });
  await fireEvent.click(view.getByRole('button', { name: 'New project' }));
  const form = view.getByRole('form', { name: 'New project' }) as HTMLFormElement;
  const field = (name: string) => form.querySelector(`[name="${name}"]`) as HTMLInputElement;
  return { view, form, field, oncreated };
}

describe('NewStateProject (plugin step 10)', () => {
  beforeEach(() => { langStore.set('en'); vi.resetAllMocks(); });
  afterEach(() => cleanup());

  it('creates the project, sends the goal as the first message, then starts the driver - in that order', async () => {
    const order: string[] = [];
    api.createStateProject.mockImplementation(async () => { order.push('create'); });
    api.sendDriverConversationMessage.mockImplementation(async () => { order.push('goal'); });
    api.startDshDriver.mockImplementation(async () => { order.push('start'); });
    const { view, form, field, oncreated } = await openForm();
    await fireEvent.input(field('project_id'), { target: { value: 'ledger' } });
    await fireEvent.input(field('title'), { target: { value: 'Ledger' } });
    await fireEvent.input(field('goal'), { target: { value: 'A local-first ledger.' } });
    await fireEvent.input(field('source_project_id'), { target: { value: 'ledger-src' } });
    await fireEvent.submit(form);
    await waitFor(() => expect(oncreated).toHaveBeenCalledWith('ledger'));
    expect(order).toEqual(['create', 'goal', 'start']);
    expect(api.createStateProject).toHaveBeenCalledWith('ledger', 'Ledger', 'ledger-src');
    expect(api.sendDriverConversationMessage).toHaveBeenCalledWith('ledger', 'A local-first ledger.');
    expect(api.startDshDriver).toHaveBeenCalledWith('ledger');
    expect(view.getByRole('status').textContent).toBe('Project created.');
  });

  it('skips the goal when empty and the driver when unticked; no source sends none', async () => {
    api.createStateProject.mockResolvedValue({});
    const { form, field, oncreated } = await openForm();
    await fireEvent.input(field('project_id'), { target: { value: 'quiet' } });
    await fireEvent.input(field('title'), { target: { value: 'Quiet' } });
    await fireEvent.click(field('start_driver'));
    await fireEvent.submit(form);
    await waitFor(() => expect(oncreated).toHaveBeenCalledWith('quiet'));
    expect(api.createStateProject).toHaveBeenCalledWith('quiet', 'Quiet', undefined);
    expect(api.sendDriverConversationMessage).not.toHaveBeenCalled();
    expect(api.startDshDriver).not.toHaveBeenCalled();
  });

  it('refuses an invalid id or an empty title before calling the backend', async () => {
    const { view, form, field } = await openForm();
    await fireEvent.input(field('project_id'), { target: { value: '-bad id' } });
    await fireEvent.input(field('title'), { target: { value: 'X' } });
    await fireEvent.submit(form);
    expect(view.getByRole('alert').textContent).toContain('must be 1-128');
    await fireEvent.input(field('project_id'), { target: { value: 'good' } });
    await fireEvent.input(field('title'), { target: { value: '   ' } });
    await fireEvent.submit(form);
    expect(view.getByRole('alert').textContent).toBe('A title is required.');
    expect(api.createStateProject).not.toHaveBeenCalled();
  });

  it('shows the backend refusal verbatim and creates nothing further', async () => {
    api.createStateProject.mockRejectedValue(new Error('project identity already exists with different settings'));
    const { view, form, field, oncreated } = await openForm();
    await fireEvent.input(field('project_id'), { target: { value: 'taken' } });
    await fireEvent.input(field('title'), { target: { value: 'Taken' } });
    await fireEvent.input(field('goal'), { target: { value: 'g' } });
    await fireEvent.submit(form);
    await waitFor(() => expect(view.getByRole('alert').textContent).toBe('project identity already exists with different settings'));
    expect(api.sendDriverConversationMessage).not.toHaveBeenCalled();
    expect(api.startDshDriver).not.toHaveBeenCalled();
    expect(oncreated).not.toHaveBeenCalled();
  });

  it('a created project stays created when a later step fails, and says which step failed', async () => {
    api.createStateProject.mockResolvedValue({});
    api.sendDriverConversationMessage.mockRejectedValue(new Error('user_identity_required'));
    api.startDshDriver.mockRejectedValue(new Error('driver identity must be enabled'));
    const { view, form, field, oncreated } = await openForm();
    await fireEvent.input(field('project_id'), { target: { value: 'half' } });
    await fireEvent.input(field('title'), { target: { value: 'Half' } });
    await fireEvent.input(field('goal'), { target: { value: 'g' } });
    await fireEvent.submit(form);
    await waitFor(() => expect(oncreated).toHaveBeenCalledWith('half'));
    const alert = view.getByRole('alert').textContent ?? '';
    expect(alert).toContain('Project created, but:');
    expect(alert).toContain('the goal was not sent: user_identity_required');
    expect(alert).toContain('the driver did not start: driver identity must be enabled');
  });

  it('tells the owner the project is private', async () => {
    const { view } = await openForm();
    expect(view.container.textContent).toContain('it is not published');
  });
});
