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
import { t } from '../../lib/i18n.svelte';

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

// The backend's ids (api/steward_routers.py CAPABILITIES). Translation is not one of
// them: the owner ruled it is not a basic capability (2026-11-23).
const IDS = ['vision', 'ocr', 'pdf', 'office_read', 'office_write', 'transcribe', 'tts', 'draw', 'music',
  'sfx', 'video', 'search', 'fetch', 'email', 'calendar', 'cloud_files', 'memory', 'code'];
const LANGS = ['en', 'zh-CN', 'zh-TW', 'ja', 'ko', 'fr', 'de', 'es'];
const VIEW_KEYS = ['steward.capAria', 'steward.capColName', 'steward.capColRoute', 'steward.capNative',
  'steward.capMcp', 'steward.capMissing', 'steward.capEmpty', 'steward.capLoadFailed',
  ...IDS.map(id => 'steward.cap.' + id)];
const headers = (container: HTMLElement) =>
  [...container.querySelectorAll('thead th')].map(th => th.textContent?.trim());
const label = (container: HTMLElement, id: string) =>
  row(container, id)?.querySelector('[data-capability-label]')?.textContent?.trim();

describe('the capabilities table speaks the chosen language', () => {
  afterEach(() => { langStore.set('en'); });

  it('every key the view uses exists in all 8 languages, translated rather than copied', () => {
    // t() falls back to English, then to the key: a key missing from one language
    // renders English there, so "differs from en" is what proves the key is present.
    const english: Record<string, string> = {};
    langStore.set('en');
    for (const key of VIEW_KEYS) {
      english[key] = t(key);
      expect(english[key], key).not.toBe(key);
    }
    const copied: string[] = [];
    for (const lang of LANGS.slice(1)) {
      langStore.set(lang);
      for (const key of VIEW_KEYS) if (t(key) === english[key]) copied.push(`${lang} ${key}`);
    }
    expect(copied).toEqual([]);
  });

  it('the calendar row names the calendar alone (owner 2026-11-23: reminders are the steward\'s own)', () => {
    const want: Record<string, string> = {en: 'Calendar', 'zh-CN': '日历', 'zh-TW': '行事曆', ja: 'カレンダー',
      ko: '캘린더', fr: 'Agenda', de: 'Kalender', es: 'Calendario'};
    for (const lang of LANGS) {
      langStore.set(lang);
      expect(t('steward.cap.calendar'), lang).toBe(want[lang]);
    }
  });

  it('headers, aria-label, names and routes come from the i18n keys in en and zh-CN', async () => {
    api.listStewardCapabilities.mockResolvedValue(payload({vision: {label: '看图 (Vision)'}}));
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(row(view.container, 'tts')).not.toBeNull());
    const section = view.container.querySelector('.steward-capabilities') as HTMLElement;
    expect(headers(view.container)).toEqual(['Capability', 'Route']);
    expect(section.getAttribute('aria-label')).toBe('Basic capabilities');
    expect(label(view.container, 'vision')).toBe('Understand images');
    expect(label(view.container, 'transcribe')).toBe('Speech to text');
    expect(label(view.container, 'tts')).toBe('Send voice messages');

    langStore.set('zh-CN');
    await waitFor(() => expect(headers(view.container)).toEqual(['能力', '提供方式']));
    expect(section.getAttribute('aria-label')).toBe('基础能力');
    // The name is the key's, never the backend's mixed label.
    expect(label(view.container, 'vision')).toBe('看图');
    expect(label(view.container, 'transcribe')).toBe('语音转文字');
    expect(label(view.container, 'tts')).toBe('发语音条');
    expect(route(view.container, 'vision')).toBe('自带 — model_routes:visionprobe');
    expect(route(view.container, 'transcribe')).toBe('MCP 工具：agentmedia / transcribe_audio');
    expect(route(view.container, 'tts')).toBe('缺失（无提供方式）');
  });

  it('an id with no key falls back to the backend label', async () => {
    api.listStewardCapabilities.mockResolvedValue({capabilities: [
      ...payload().capabilities,
      {id: 'telepathy', label: '读心 (Telepathy)', provider: 'missing', conn_id: null, tool: null, detail: null},
    ]});
    langStore.set('zh-CN');
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(row(view.container, 'telepathy')).not.toBeNull());
    expect(label(view.container, 'telepathy')).toBe('读心 (Telepathy)');
    expect(label(view.container, 'vision')).toBe('看图');
  });

  it('empty and failed answers are translated too', async () => {
    langStore.set('zh-CN');
    api.listStewardCapabilities.mockResolvedValue({capabilities: []});
    const empty = render(StewardCapabilitiesView);
    await waitFor(() => expect(empty.container.querySelector('[data-capabilities-empty]')?.textContent)
      .toBe('没有列出任何能力。'));
    cleanup();
    api.listStewardCapabilities.mockRejectedValue(new Error('HTTP 503'));
    const failed = render(StewardCapabilitiesView);
    await waitFor(() => expect(failed.getByRole('alert').textContent).toBe('读取能力列表失败：HTTP 503'));
    expect(failed.container.querySelector('[data-capabilities-empty]')).toBeNull();
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
