import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';
const api = vi.hoisted(() => ({ driverStatus: vi.fn(), startDshDriver: vi.fn(), stopDshDriver: vi.fn() }));
vi.mock('../../lib/api', () => api);
import DriverStatus from '../../views/DriverStatus.svelte';
const projects: any[] = [{ project_id: 'alpha', title: 'Alpha' }, { project_id: 'beta', title: 'Beta' }];
const status = () => ({total: 4, online: 2, projects: {
  alpha: {total: 2, online: 1, running_subagents: 3, running_pipelines: 2,
    managed: {enabled: 1, phase: 'waiting', driver_id: 'dsh-alpha', last_error: ''}},
  beta: {total: 1, online: 0, running_subagents: 0, running_pipelines: 0},
}});
beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'});
  api.driverStatus.mockResolvedValue(status());
  api.startDshDriver.mockResolvedValue({}); api.stopDshDriver.mockResolvedValue({});
});
afterEach(() => {cleanup(); vi.useRealTimers();});
describe('dashboard driver status', () => {
  it('shows global counts, per DAG workers and direct start/stop without opening a DAG', async () => {
    const view = render(DriverStatus, {projects, projectId: 'alpha'});
    await view.findByText('Total: 4 · Online: 2');
    expect(view.getByText('alpha: 1/2 Online · 3 subagents · 2 pipelines')).toBeTruthy();
    await fireEvent.click(view.container.querySelector('summary')!);
    expect(view.getByText('Alpha')).toBeTruthy(); expect(view.getByText('Beta')).toBeTruthy();
    await fireEvent.click(view.getByRole('button', {name: 'Stop driver'}));
    await waitFor(() => expect(api.stopDshDriver).toHaveBeenCalledWith('alpha'));
    await waitFor(() => expect(view.getByRole('button', {name: 'Start driver'}).hasAttribute('disabled')).toBe(false));
    await fireEvent.click(view.getByRole('button', {name: 'Start driver'}));
    await waitFor(() => expect(api.startDshDriver).toHaveBeenCalledWith('beta'));
  });
  it('refreshes running worker counts on the dashboard', async () => {
    vi.useFakeTimers();
    const view = render(DriverStatus, {projects, projectId: 'alpha'});
    await vi.advanceTimersByTimeAsync(1);
    expect(view.container.textContent).toContain('3 subagents');
    const next = status(); next.projects.alpha.running_subagents = 0; next.projects.alpha.running_pipelines = 1;
    api.driverStatus.mockResolvedValue(next);
    await vi.advanceTimersByTimeAsync(10000);
    expect(view.container.textContent).toContain('0 subagents · 1 pipelines');
  });
  it('keeps runtime details private and exposes real failures', async () => {
    authStore.set({canWrite: false, permissionResolved: true, email: null});
    const privateView = render(DriverStatus, {projects});
    expect(privateView.container.textContent).toBe(''); expect(api.driverStatus).not.toHaveBeenCalled();
    cleanup(); authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'});
    api.driverStatus.mockRejectedValue(new Error('runtime unavailable'));
    const view = render(DriverStatus, {projects});
    await view.findByRole('alert');
    expect(view.getByRole('alert').textContent).toBe('runtime unavailable');
  });
});
