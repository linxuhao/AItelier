/**
 * An unknown hash route used to render nothing at all: App.svelte had no
 * catch-all, so the retired '#/chat' (and any mistyped link) showed a blank
 * page. Unknown routes now land on the default view, and are replaced rather
 * than pushed so Back does not bounce through them.
 *
 * The real App and the real svelte-spa-router run; only the views (stubbed to a
 * marker) and the network/SSE edges are replaced.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, cleanup } from '@testing-library/svelte';

vi.mock('../../lib/api', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  whoami: vi.fn().mockResolvedValue({can_write: false, email: null}),
  getCheckpoint: vi.fn().mockResolvedValue({}),
  getUserLang: vi.fn().mockResolvedValue({lang: 'en'}),
  setUserLang: vi.fn().mockResolvedValue({}),
}));
vi.mock('../../lib/sse', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  connect: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
}));
vi.mock('../../stores/i18n', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  syncInitialLang: vi.fn(),
}));
// The chrome around the router renders nothing; the default view is a marker.
const stub = vi.hoisted(() => async () => ({
  default: (await import('../fixtures/RouteStub.svelte')).default,
}));
const empty = vi.hoisted(() => async () => ({
  default: (await import('../fixtures/EmptyStub.svelte')).default,
}));
vi.mock('../../views/AppBar.svelte', empty);
vi.mock('../../views/NotificationPanel.svelte', empty);
vi.mock('../../views/CheckpointModal.svelte', empty);
vi.mock('../../views/ConfirmDialog.svelte', empty);
vi.mock('../../views/ProjectDashboard.svelte', stub);

import App from '../../App.svelte';

function routed(container: HTMLElement): HTMLElement | null {
  return container.querySelector('#view-container [data-testid="route-stub"]');
}

describe('App routing fallback', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => cleanup());

  it('sends the retired #/chat route to the default view', async () => {
    window.location.hash = '#/chat';
    const depth = window.history.length;
    const {container} = render(App);
    await waitFor(() => expect(window.location.hash).toBe('#/'));
    await waitFor(() => expect(routed(container)).not.toBeNull());
    expect(window.history.length).toBe(depth);
  });

  it('sends any unknown route to the default view', async () => {
    window.location.hash = '#/no/such/view';
    const {container} = render(App);
    await waitFor(() => expect(window.location.hash).toBe('#/'));
    await waitFor(() => expect(routed(container)).not.toBeNull());
  });

  it('leaves a known route alone', async () => {
    window.location.hash = '#/projects';
    const {container} = render(App);
    await waitFor(() => expect(routed(container)).not.toBeNull());
    expect(window.location.hash).toBe('#/projects');
  });
});
