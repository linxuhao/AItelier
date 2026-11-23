/** The steward's basic-capabilities table (owner 2026-11-23).
 *
 *  The view is a renderer, not a router: the backend's payload decides which row is
 *  native, mcp/tool or missing. These tests render from a payload, then render again
 *  from a DIFFERENT payload with the same shape and watch the route text change with
 *  no edit to the component. A missing row says so explicitly.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  listStewardCapabilities: vi.fn(),
  listOwnerChannels: vi.fn(),
  dshDriverConversation: vi.fn(),
  dshDriverHistory: vi.fn(),
  markDriverConversationRead: vi.fn(),
  stewardMessageSources: vi.fn(),
  listStewardProposals: vi.fn(),
  listStewardSchedules: vi.fn(),
}));
vi.mock('../../lib/api', async (original) => ({...(await original<object>()), ...api}));
import type { StewardCapabilities, StewardCapability } from '../../lib/api';
import StewardCapabilitiesView from '../../views/StewardCapabilities.svelte';
import StewardPane from '../../views/StewardPane.svelte';

const payload = (over: Record<string, Partial<StewardCapability>> = {}): StewardCapabilities => ({
  capabilities: ([
    {id: 'vision', label: 'Vision', provider: 'native', conn_id: null, tool: null, detail: 'model_routes:visionprobe'},
    {id: 'transcribe', label: 'Transcription', provider: 'mcp', conn_id: 'agentmedia', tool: 'transcribe_audio',
      detail: 'agentmedia: transcribe_audio'},
    {id: 'tts', label: 'Speech', provider: 'missing', conn_id: null, tool: null, detail: null},
  ] as StewardCapability[]).map(row => ({...row, ...(over[row.id] ?? {})})),
});
const row = (container: HTMLElement, id: string) =>
  container.querySelector(`[data-capability="${id}"]`) as HTMLElement | null;
const route = (container: HTMLElement, id: string) => row(container, id)?.querySelector('[data-capability-route]')?.textContent?.trim();

beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@example.test'} as never);
  api.listStewardCapabilities.mockResolvedValue(payload());
});
afterEach(() => { cleanup(); });

describe('the capabilities table', () => {
  it('renders one row per capability with the route the payload says it takes', async () => {
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(row(view.container, 'tts')).not.toBeNull());
    expect(view.container.querySelectorAll('[data-capability]').length).toBe(3);
    expect(row(view.container, 'vision')?.dataset.provider).toBe('native');
    expect(route(view.container, 'vision')).toContain('native');
    expect(route(view.container, 'vision')).toContain('model_routes:visionprobe');
    expect(row(view.container, 'transcribe')?.dataset.provider).toBe('mcp');
    expect(route(view.container, 'transcribe')).toContain('mcp/tool');
    expect(route(view.container, 'transcribe')).toContain('agentmedia');
    expect(route(view.container, 'transcribe')).toContain('transcribe_audio');
    expect(route(view.container, 'tts')).toBe('missing (no route)');
    expect(api.listStewardCapabilities).toHaveBeenCalledTimes(1);
  });

  it('a different payload of the same shape renders different routes without editing the view', async () => {
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(route(view.container, 'tts')).toBe('missing (no route)'));
    cleanup();
    // Only providers (and their detail) change; the component is untouched.
    api.listStewardCapabilities.mockResolvedValue(payload({
      vision: {provider: 'mcp', conn_id: 'agentmedia', tool: 'ocr_image', detail: null},
      transcribe: {provider: 'missing', conn_id: null, tool: null, detail: null},
      tts: {provider: 'native', conn_id: null, tool: null, detail: 'route:voiceprobe'},
    }));
    const again = render(StewardCapabilitiesView);
    await waitFor(() => expect(route(again.container, 'tts')).toContain('native'));
    expect(route(again.container, 'vision')).toContain('ocr_image');
    expect(route(again.container, 'vision')).not.toContain('native');
    expect(route(again.container, 'transcribe')).toBe('missing (no route)');
    expect(route(again.container, 'tts')).toContain('route:voiceprobe');
  });

  it('a missing row shows an explicit missing label, never an empty cell', async () => {
    api.listStewardCapabilities.mockResolvedValue(payload({tts: {detail: null, conn_id: null, tool: null}}));
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(row(view.container, 'tts')).not.toBeNull());
    const text = route(view.container, 'tts');
    expect(text?.length).toBeGreaterThan(0);
    expect(text).toMatch(/missing/i);
  });
});

describe('the steward channels pane', () => {
  it('shows the capabilities table beside the channel list', async () => {
    api.listOwnerChannels.mockResolvedValue({channels: [
      {channel: 'slack', bound: false, sender_ref: null, bound_at: null, revoked_at: null, is_default: 0},
      {channel: 'wechat', bound: false, sender_ref: null, bound_at: null, revoked_at: null, is_default: 0},
    ], default_channel: null});
    api.dshDriverConversation.mockResolvedValue({project_id: 'aitelier-home', messages: [], has_more: false,
      next_cursor: null, end_of_history: true, matched_total: 0, unread_count: 0,
      read_upto_message_id: null, read_upto_created_at: null});
    api.dshDriverHistory.mockResolvedValue({messages: [], has_more: false, next_cursor: null});
    api.stewardMessageSources.mockResolvedValue({});
    api.listStewardProposals.mockResolvedValue([]);
    api.listStewardSchedules.mockResolvedValue([]);
    const status = ({total: 1, online: 1, projects: {'aitelier-home': {total: 1, online: 1,
      running_subagents: 0, running_pipelines: 0, managed: {enabled: 1, phase: 'waiting',
        driver_id: 'dsh-home', last_error: '', model: '', role: 'steward'}}}}) as never;
    const view = render(StewardPane, {projectId: 'aitelier-home', status, onanswer: vi.fn()});
    await fireEvent.click(view.getByRole('tab', {name: 'Channels'}));
    await waitFor(() => expect(view.container.querySelector('.steward-channels')).not.toBeNull());
    await waitFor(() => expect(row(view.container, 'transcribe')).not.toBeNull());
    expect(view.container.querySelector('.channels-with-capabilities')).not.toBeNull();
    expect(route(view.container, 'transcribe')).toContain('transcribe_audio');
  });
});
