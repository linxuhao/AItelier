import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({ stateIssues: vi.fn(), stateIssue: vi.fn(), setUserLang: vi.fn() }));
vi.mock('../../lib/api', () => api);
import StateIssues from '../../views/StateIssues.svelte';

const summary = (over = {}) => ({issue_id: 'iss-1', project_id: 'game', kind: 'defect', title: 'input dropped',
  status: 'open', version: 1, nodes: [{node_key: 'growth.progress', status: 'VERIFIED'}],
  contradicts_acceptance: ['growth.progress'], created_at: '2026-09-16T10:00:00Z', updated_at: '2026-09-16T10:00:00Z', ...over});

beforeEach(() => { cleanup(); vi.resetAllMocks(); langStore.set('en'); });

describe('State issues view', () => {
  it('lists open issues, flags defects on accepted goals and links to the node', async () => {
    api.stateIssues.mockResolvedValue({issues: [summary()], status_counts: {open: 1, absorbed: 2}, has_more: false, next_after: 1});
    const onselect = vi.fn();
    const view = render(StateIssues, {projectId: 'game', onselect});
    await waitFor(() => expect(view.getByText('input dropped')).toBeTruthy());
    expect(api.stateIssues).toHaveBeenCalledWith('game', true);
    expect(view.container.querySelector('.issue.contradicts')).toBeTruthy();
    expect(view.getByText('Open only · 1')).toBeTruthy();
    expect(view.getByText('All · 3')).toBeTruthy();
    await fireEvent.click(view.getByRole('button', {name: 'growth.progress · VERIFIED'}));
    expect(onselect).toHaveBeenCalledWith('growth.progress');
  });

  it('switches to all issues and shows body and resolution on demand', async () => {
    api.stateIssues.mockResolvedValue({issues: [summary({status: 'absorbed', contradicts_acceptance: []})], status_counts: {absorbed: 1}, has_more: false, next_after: 1});
    api.stateIssue.mockResolvedValue({...summary({status: 'absorbed', contradicts_acceptance: []}), body: 'measured detail', source: 'delivery-7',
      resolution: {resolution: 'absorbed', reason: 'criterion c2', director_identity: 'wuxia', node_key: 'growth.progress', node_revision: 3},
      reported_by: {actor: 'a@x', director_identity: 'wuxia'}});
    const view = render(StateIssues, {projectId: 'game', onselect: vi.fn()});
    await waitFor(() => expect(view.getByText('input dropped')).toBeTruthy());
    await fireEvent.click(view.getByRole('button', {name: /^All/}));
    await waitFor(() => expect(api.stateIssues).toHaveBeenLastCalledWith('game', false));
    expect(view.container.querySelector('.issue.contradicts')).toBeNull();
    await fireEvent.click(view.getByRole('button', {name: 'Details'}));
    await waitFor(() => expect(view.getByText('measured detail')).toBeTruthy());
    expect(view.getByRole('button', {name: 'growth.progress r3'})).toBeTruthy();
  });
});
