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
  setStewardCapability: vi.fn(),
  unsetStewardCapability: vi.fn(),
  clearStewardCapabilities: vi.fn(),
  listStewardConnections: vi.fn(),
  stewardConnectionTools: vi.fn(),
  approveStewardProposal: vi.fn(),
  declineStewardProposal: vi.fn(),
  saveStewardConnection: vi.fn(),
  listStewardConfirmations: vi.fn(),
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
// Step 24.4 (design 7.8.24 "六"): the reason and refusal keys are built from a code at run
// time, so no "keys the view uses" scan would find them: every one is written out here.
const REASONS = ['ok', 'fallback_in_use', 'tool_gone', 'tool_not_allowed', 'tool_changed', 'tool_collision',
  'connection_off', 'connection_error', 'see_driver_status', 'missing', 'not_set'];
const ERRORS = ['unknown_capability', 'route_empty', 'too_many_entries', 'entry_not_allowed', 'duplicate_entry',
  'host_tool_not_allowed', 'unknown_connection', 'unknown_tool', 'pin_must_be_private', 'version_conflict',
  // Step 24.4c: the dev-mcp opening's refusal.
  'dev_mcp_not_allowed'];
const VIEW_KEYS = ['steward.capAria', 'steward.capColName', 'steward.capColRoute', 'steward.capNative',
  'steward.capMcp', 'steward.capMissing', 'steward.capEmpty', 'steward.capLoadFailed',
  ...IDS.map(id => 'steward.cap.' + id),
  ...['capColGroup', 'capColStatus', 'capEntryNative', 'capEntryMissing', 'capAddEntry', 'capSave', 'capCancel',
    'capUnset', 'capClearAll', 'capClearAllConfirm', 'capSuggestedProposal', 'capSuggestedDetected', 'capNotSet',
    'capSet', 'capAdopt', 'capReject', 'capNotAllowedHint', 'capConflict', 'capSaveFailed', 'capGroupSeeSteward',
    'capGroupYes', 'capGroupPrivate', 'capStatusLine', 'capPropRoute', 'capPropDescriptions', 'capPropSourceOwner',
    'capPropSourceSearch', 'kind.capability_route', 'connPinnedPrivateOnly'].map(key => 'steward.' + key),
  'steward.capReason.ok', 'steward.capReason.fallback_in_use', 'steward.capReason.tool_gone',
  'steward.capReason.tool_not_allowed', 'steward.capReason.tool_changed', 'steward.capReason.tool_collision',
  'steward.capReason.connection_off', 'steward.capReason.connection_error', 'steward.capReason.see_driver_status',
  'steward.capReason.missing', 'steward.capReason.not_set',
  'steward.capError.unknown_capability', 'steward.capError.route_empty', 'steward.capError.too_many_entries',
  'steward.capError.entry_not_allowed', 'steward.capError.duplicate_entry', 'steward.capError.host_tool_not_allowed',
  'steward.capError.unknown_connection', 'steward.capError.unknown_tool', 'steward.capError.pin_must_be_private',
  'steward.capError.version_conflict', 'steward.capError.dev_mcp_not_allowed'];
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

  it('the written-out reason and refusal keys are exactly the closed code sets', () => {
    expect(VIEW_KEYS.filter(k => k.startsWith('steward.capReason.'))).toEqual(REASONS.map(c => 'steward.capReason.' + c));
    expect(VIEW_KEYS.filter(k => k.startsWith('steward.capError.'))).toEqual(ERRORS.map(c => 'steward.capError.' + c));
    // The zh words are the owner's: 自带, not 内置 / 內建.
    langStore.set('zh-CN'); expect(t('steward.capNative')).toBe('自带'); expect(t('steward.capEntryNative')).toBe('自带');
    langStore.set('zh-TW'); expect(t('steward.capNative')).toBe('自帶');
  });

  it('no language puts reminders into the calendar row', () => {
    const words = /remind|提醒|リマインダー|알림|rappel|Erinnerung|recordatorio/i;
    for (const lang of LANGS) { langStore.set(lang); expect(t('steward.cap.calendar'), lang).not.toMatch(words); }
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


// ---- Step 24.4: the owner's editable route (design 7.8.24 "四", "六") ----------------------
import { ApiError, type StewardCapabilityStatus, type StewardProposal } from '../../lib/api';
import StewardProposalCard from '../../views/StewardProposalCard.svelte';
import StewardConnectionsView from '../../views/StewardConnections.svelte';

const spec = (allows: string[], host_tools: string[] = [], private_only = false) =>
  ({allows, host_tools, private_only}) as StewardCapability['spec'];
const notSet: StewardCapabilityStatus = {dot: 'grey', reasons: ['not_set']};
/** A 24.4 answer: the director's three detection rows plus pdf, draw, video and memory, all unset. */
const routed = (over: Record<string, Partial<StewardCapability>> = {}): StewardCapabilities => ({
  capabilities: ([
    ...payload().capabilities.map(r => ({...r, spec: r.id === 'vision' ? spec(['native', 'missing']) : spec(['conn', 'missing'])})),
    {id: 'pdf', label: 'PDF', provider: 'native', conn_id: null, tool: null, detail: 'pdf_reader', spec: spec(['native', 'conn', 'missing'])},
    {id: 'draw', label: 'Draw', provider: 'missing', conn_id: null, tool: null, detail: null, spec: spec(['conn', 'missing'])},
    {id: 'video', label: 'Video', provider: 'missing', conn_id: null, tool: null, detail: null, spec: spec(['conn', 'missing'])},
    {id: 'memory', label: 'Memory', provider: 'native', conn_id: null, tool: null, detail: 'plugin',
      spec: spec(['native', 'conn'], [], true)},
  ] as StewardCapability[]).map(r => ({setting: null, proposal: null, status: notSet, ...r, ...(over[r.id] ?? {})})),
});
const conns = () => ({connections: [
  {conn_id: 'pub', label: 'Public media', visibility: 'public'},
  {conn_id: 'mine', label: 'My notes', visibility: 'private'},
], effective_tools: [], effective_hash: 'h', restart: {due: [], done: []}}) as never;
const chips = (container: HTMLElement, id: string) =>
  [...(row(container, id)?.querySelectorAll('[data-capability-editor] [data-entry]') ?? [])].map(li =>
    li.textContent?.replace('⋮⋮', '').replace('×', '').replace(/\s+/g, ' ').trim());
const options = (container: HTMLElement, id: string) =>
  [...(row(container, id)?.querySelectorAll('select[data-cap-add] option') ?? [])].map(o => (o as HTMLOptionElement).value);
// As the backend reads a setting back: each entry with its status reason, a connection entry
// with its conn_uid and its connection's visibility.
const pdfSet = {setting: {route: [{kind: 'native', reason: 'see_driver_status'}, {kind: 'conn', conn_id: 'pub',
  raw_name: 'ocr_pdf', conn_uid: 'u-pub', visibility: 'public', reason: 'tool_not_allowed'}],
  version: 3, set_via: 'dashboard'}, status: {dot: 'red', reasons: ['see_driver_status', 'tool_not_allowed']}} as Partial<StewardCapability>;

describe('the route editor (step 24.4)', () => {
  beforeEach(() => {
    api.listStewardConnections.mockResolvedValue(conns());
    api.setStewardCapability.mockResolvedValue({});
    api.unsetStewardCapability.mockResolvedValue({});
    api.clearStewardCapabilities.mockResolvedValue({});
  });

  it('four columns; an unset row shows detection as a grey suggestion, and "use this" only fills the editor', async () => {
    api.listStewardCapabilities.mockResolvedValue(routed());
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(row(view.container, 'memory')).not.toBeNull());
    expect(headers(view.container)).toEqual(['Capability', 'Route', 'In groups', 'Status']);
    // Detection text is the director's route() text, unchanged, in the grey suggestion.
    expect(route(view.container, 'transcribe')).toBe('mcp/tool: agentmedia / transcribe_audio');
    expect(row(view.container, 'transcribe')?.querySelector('[data-capability-suggestion="detected"]')?.textContent)
      .toContain('Detected:');
    expect(route(view.container, 'tts')).toBe('missing (no route)');
    expect(row(view.container, 'tts')?.querySelector('[data-cap-not-set]')?.textContent).toBe(t('steward.capNotSet'));
    expect(row(view.container, 'tts')?.querySelector('[data-cap-adopt]')).toBeNull();
    expect(row(view.container, 'tts')?.querySelector('[data-capability-status]')?.textContent?.trim()).toBe('Not set');
    expect(view.container.querySelector('[data-capability-editor]')).toBeNull();
    expect(view.container.querySelector('[data-cap-clear]')).toBeNull();

    await fireEvent.click(row(view.container, 'transcribe')!.querySelector('[data-cap-adopt]')!);
    expect(chips(view.container, 'transcribe')).toEqual(['agentmedia / transcribe_audio']);
    expect(api.setStewardCapability).not.toHaveBeenCalled();
    await fireEvent.click(row(view.container, 'transcribe')!.querySelector('[data-cap-save]')!);
    await waitFor(() => expect(api.setStewardCapability).toHaveBeenCalledTimes(1));
    expect(api.setStewardCapability.mock.calls[0].slice(0, 3)).toEqual(['transcribe', 0,
      [{kind: 'conn', conn_id: 'agentmedia', raw_name: 'transcribe_audio'}]]);
    // A native detection fills [native].
    await fireEvent.click(row(view.container, 'pdf')!.querySelector('[data-cap-adopt]')!);
    expect(chips(view.container, 'pdf')).toEqual(['Native']);
  });

  it('a set row: entries in order with their reasons and group answers; dragging reorders; save sends the order and the row version', async () => {
    api.listStewardCapabilities.mockResolvedValue(routed({pdf: pdfSet}));
    const onconnect = vi.fn();
    const view = render(StewardCapabilitiesView, {onconnect});
    await waitFor(() => expect(chips(view.container, 'pdf')).toEqual(['Native', 'pub / ocr_pdf Not allowed yet']));
    const pdf = row(view.container, 'pdf')!;
    expect(pdf.querySelector('[data-capability-editor]')?.getAttribute('data-capability-editor')).toBe('pdf');
    expect(pdf.querySelector('[data-entry-reason]')?.getAttribute('data-entry-reason')).toBe('tool_not_allowed');
    expect([...pdf.querySelectorAll('[data-capability-group] div')].map(d => d.textContent)).toEqual(['See the steward', 'Yes']);
    expect(pdf.querySelector('[data-status-dot]')?.getAttribute('data-status-dot')).toBe('red');
    expect(pdf.querySelector('[data-capability-status]')?.textContent).toContain("See the steward's status; Not allowed yet");
    expect(pdf.querySelector('.hint')?.textContent).toBe(t('steward.capNotAllowedHint'));
    expect(view.container.querySelector('[data-cap-clear]')).not.toBeNull();
    // The connection name opens that connection.
    await fireEvent.click(pdf.querySelector('[data-entry-conn="pub"]')!);
    expect(onconnect).toHaveBeenCalledWith('pub');
    // Nothing to save until something changes.
    expect(pdf.querySelector('[data-cap-save]')).toBeNull();

    const items = pdf.querySelectorAll('[data-entry]');
    await fireEvent.dragStart(items[1]);
    await fireEvent.dragOver(items[0]);
    await fireEvent.drop(items[0]);
    expect(chips(view.container, 'pdf')).toEqual(['pub / ocr_pdf Not allowed yet', 'Native']);
    await fireEvent.click(pdf.querySelector('[data-cap-save]')!);
    await waitFor(() => expect(api.setStewardCapability).toHaveBeenCalledTimes(1));
    const [capId, version, sent] = api.setStewardCapability.mock.calls[0];
    expect([capId, version]).toEqual(['pdf', 3]);
    expect(sent.map((e: {kind: string; raw_name?: string}) => e.kind === 'conn' ? e.raw_name : e.kind)).toEqual(['ocr_pdf', 'native']);
    await waitFor(() => expect(api.listStewardCapabilities).toHaveBeenCalledTimes(2));
  });

  it('a private connection entry says "no" for groups', async () => {
    api.listStewardCapabilities.mockResolvedValue(routed({memory: {setting: {route: [{kind: 'native'},
      {kind: 'conn', conn_id: 'mine', raw_name: 'note_search', reason: 'ok'}], version: 1, set_via: 'dashboard'},
      status: {dot: 'green', reasons: ['see_driver_status', 'ok']}}}));
    const view = render(StewardCapabilitiesView);
    // No visibility on the entry: the connection list answers it.
    await waitFor(() => expect([...row(view.container, 'memory')!.querySelectorAll('[data-capability-group] div')]
      .map(d => d.textContent)).toEqual(['See the steward', 'No (private connection)']));
  });

  it('a pin whose connection is gone says so and reaches no group, even when the conn_id is back as public', async () => {
    // c4: the backend reads the re-added `pub` (another conn_uid) as gone: visibility null, tool_gone.
    api.listStewardCapabilities.mockResolvedValue(routed({draw: {setting: {route: [{kind: 'conn', conn_id: 'pub',
      raw_name: 'paint_v2', conn_uid: 'u-old', visibility: null, reason: 'tool_gone'}], version: 2, set_via: 'dashboard'},
      status: {dot: 'red', reasons: ['tool_gone']}, provider: 'mcp', conn_id: 'pub', tool: 'generate_image',
      detail: 'pub: generate_image'}}));
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(chips(view.container, 'draw')).toEqual(['pub / paint_v2 Tool gone']));
    await waitFor(() => expect(api.listStewardConnections).toHaveBeenCalled());
    const draw = row(view.container, 'draw')!;
    expect([...draw.querySelectorAll('[data-capability-group] div')].map(d => d.textContent)).toEqual(['—']);
    expect(draw.querySelector('[data-status-dot]')?.getAttribute('data-status-dot')).toBe('red');
    // The stored route is shown as stored: no rebinding to the detected tool, nothing written.
    // With no entry resolving, detection's grey suggestion comes back beside it (c4); "use
    // this" only fills the editor.
    const suggestion = draw.querySelector('[data-capability-suggestion="detected"]');
    expect(suggestion?.querySelector('[data-capability-route]')?.textContent).toContain('generate_image');
    expect(api.setStewardCapability).not.toHaveBeenCalled();
    await fireEvent.click(suggestion!.querySelector('[data-cap-adopt]')!);
    expect(chips(view.container, 'draw')).toEqual(['pub / generate_image']);
    expect(row(view.container, 'draw')!.querySelector('[data-capability-suggestion]')).toBeNull();
    expect(api.setStewardCapability).not.toHaveBeenCalled();
  });

  it('the picker offers only the allowed kinds: memory lists private connections only, video every connection; any tool name pins (paint_v2 to draw)', async () => {
    api.listStewardCapabilities.mockResolvedValue(routed());
    api.stewardConnectionTools.mockResolvedValue({conn_id: 'pub', instructions: null, last_probe_at: null, last_error: null,
      tools: [{raw_name: 'generate_image', description: 'x'}, {raw_name: 'paint_v2', description: '根据文字描述生成图片'}]});
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(row(view.container, 'memory')).not.toBeNull());
    await fireEvent.click(row(view.container, 'memory')!.querySelector('[data-cap-set]')!);
    await waitFor(() => expect(options(view.container, 'memory')).toEqual(['', 'native', 'conn:mine']));
    await fireEvent.click(row(view.container, 'video')!.querySelector('[data-cap-set]')!);
    await waitFor(() => expect(options(view.container, 'video')).toEqual(['', 'missing', 'conn:pub', 'conn:mine']));
    await fireEvent.click(row(view.container, 'vision')!.querySelector('[data-cap-set]')!);
    expect(options(view.container, 'vision')).toEqual(['', 'native', 'missing']);
    // An empty route cannot be saved.
    expect((row(view.container, 'video')!.querySelector('[data-cap-save]') as HTMLButtonElement).disabled).toBe(true);

    await fireEvent.click(row(view.container, 'draw')!.querySelector('[data-cap-set]')!);
    const select = row(view.container, 'draw')!.querySelector('select[data-cap-add]') as HTMLSelectElement;
    await waitFor(() => expect(options(view.container, 'draw')).toContain('conn:pub'));
    await fireEvent.change(select, {target: {value: 'conn:pub'}});
    await waitFor(() => expect(row(view.container, 'draw')!.querySelector('[data-cap-tool="paint_v2"]')).not.toBeNull());
    expect(api.stewardConnectionTools).toHaveBeenCalledWith('pub');
    await fireEvent.click(row(view.container, 'draw')!.querySelector('[data-cap-tool="paint_v2"]')!);
    expect(chips(view.container, 'draw')).toEqual(['pub / paint_v2']);
    await fireEvent.click(row(view.container, 'draw')!.querySelector('[data-cap-save]')!);
    await waitFor(() => expect(api.setStewardCapability).toHaveBeenCalledTimes(1));
    expect(api.setStewardCapability.mock.calls[0][0]).toBe('draw');
    expect(api.setStewardCapability.mock.calls[0][2]).toEqual([{kind: 'conn', conn_id: 'pub', raw_name: 'paint_v2', visibility: 'public'}]);
  });

  it('a refusal shows its translated code; version_conflict says so and re-reads; an unknown code is quoted', async () => {
    api.listStewardCapabilities.mockResolvedValue(routed({pdf: pdfSet}));
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(chips(view.container, 'pdf')).toHaveLength(2));
    const pdf = () => row(view.container, 'pdf')!;
    const error = () => pdf().querySelector('[data-cap-error]')?.textContent;

    api.setStewardCapability.mockRejectedValueOnce(new ApiError(422, 'unknown_tool: pub has no tool ocr_pdf', null,
      'unknown_tool: pub has no tool ocr_pdf'));
    await fireEvent.click(pdf().querySelector('[data-entry-remove]')!);
    await fireEvent.click(pdf().querySelector('[data-cap-save]')!);
    await waitFor(() => expect(error()).toBe('That connection has no tool of that name.'));
    expect(chips(view.container, 'pdf')).toHaveLength(1);

    api.setStewardCapability.mockRejectedValueOnce(new ApiError(422, 'pin', null, {code: 'pin_must_be_private'}));
    await fireEvent.click(pdf().querySelector('[data-cap-save]')!);
    await waitFor(() => expect(error()).toBe('This tool can only be pinned on a private connection.'));

    api.setStewardCapability.mockRejectedValueOnce(new ApiError(422, 'weird_code: ?', null, 'weird_code: ?'));
    await fireEvent.click(pdf().querySelector('[data-cap-save]')!);
    await waitFor(() => expect(error()).toBe('Not saved: weird_code'));

    const reads = api.listStewardCapabilities.mock.calls.length;
    api.setStewardCapability.mockRejectedValueOnce(new ApiError(409, 'version_conflict', 'version_conflict'));
    await fireEvent.click(pdf().querySelector('[data-cap-save]')!);
    await waitFor(() => expect(error()).toBe(t('steward.capError.version_conflict') + ' ' + t('steward.capConflict')));
    expect(api.listStewardCapabilities.mock.calls.length).toBe(reads + 1);
    // The draft was dropped: the row shows what the backend has now.
    expect(chips(view.container, 'pdf')).toHaveLength(2);
    expect(pdf().querySelector('[data-cap-save]')).toBeNull();

    langStore.set('zh-CN');
    api.setStewardCapability.mockRejectedValueOnce(new ApiError(422, 'duplicate_entry', null, 'duplicate_entry'));
    await fireEvent.click(pdf().querySelector('[data-entry-remove]')!);
    await fireEvent.click(pdf().querySelector('[data-cap-save]')!);
    await waitFor(() => expect(error()).toBe('同一个工具出现了两次。'));
    langStore.set('en');
  });

  it('unset removes one row at its version; clear all asks first, then clears every row', async () => {
    api.listStewardCapabilities.mockResolvedValue(routed({pdf: pdfSet}));
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(row(view.container, 'pdf')?.querySelector('[data-cap-unset]')).toBeTruthy());
    await fireEvent.click(row(view.container, 'pdf')!.querySelector('[data-cap-unset]')!);
    await waitFor(() => expect(api.unsetStewardCapability).toHaveBeenCalledTimes(1));
    expect(api.unsetStewardCapability.mock.calls[0].slice(0, 2)).toEqual(['pdf', 3]);

    await fireEvent.click(view.container.querySelector('[data-cap-clear]')!);
    // ConfirmDialog focuses its first button on the next animation frame: let it run while mounted.
    await new Promise(done => requestAnimationFrame(() => done(null)));
    expect(api.clearStewardCapabilities).not.toHaveBeenCalled();
    expect(view.container.querySelector('#confirm-message')?.textContent).toBe(t('steward.capClearAllConfirm'));
    await fireEvent.click(view.container.querySelector('#confirm-yes')!);
    await waitFor(() => expect(api.clearStewardCapabilities).toHaveBeenCalledTimes(1));
    expect(api.setStewardCapability).not.toHaveBeenCalled();
  });

  it('an open proposal is the suggestion: "use this" approves it at its version, "reject" declines it', async () => {
    const proposal = {proposal_id: 'prop-1', version: 2, reason: 'owner asked',
      route: [{kind: 'conn', conn_id: 'media', raw_name: 'generate_image'}], source: {kind: 'owner_message'}};
    api.listStewardCapabilities.mockResolvedValue(routed({draw: {proposal} as Partial<StewardCapability>}));
    api.approveStewardProposal.mockResolvedValue({status: 'done'});
    api.declineStewardProposal.mockResolvedValue({status: 'declined'});
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(route(view.container, 'draw')).toBe('media / generate_image'));
    const draw = row(view.container, 'draw')!;
    expect(draw.querySelector('[data-capability-suggestion="proposal"]')?.textContent).toContain('Steward suggests:');
    await fireEvent.click(draw.querySelector('[data-cap-adopt-proposal]')!);
    await waitFor(() => expect(api.approveStewardProposal).toHaveBeenCalledWith('prop-1', 2));
    await fireEvent.click(row(view.container, 'draw')!.querySelector('[data-cap-reject-proposal]')!);
    await waitFor(() => expect(api.declineStewardProposal).toHaveBeenCalledWith('prop-1', 2));
    expect(api.setStewardCapability).not.toHaveBeenCalled();
    // A failed approval names its code.
    api.approveStewardProposal.mockResolvedValue({status: 'failed', result: {error: 'route_changed'}});
    await fireEvent.click(row(view.container, 'draw')!.querySelector('[data-cap-adopt-proposal]')!);
    await waitFor(() => expect(row(view.container, 'draw')!.querySelector('[data-cap-error]')?.textContent)
      .toBe('Not saved: route_changed'));
  });

  it('the page-top line shows only the capability lines of the steward status', async () => {
    api.listStewardCapabilities.mockResolvedValue(routed());
    const status = (error: string) => ({total: 1, online: 1, projects: {'aitelier-home': {total: 1, online: 1,
      running_subagents: 0, running_pipelines: 0, managed: {enabled: 1, phase: 'waiting', driver_id: 'dsh-home',
        last_error: error, model: '', role: 'steward'}}}}) as never;
    const view = render(StewardCapabilitiesView, {status: status('upstream 502\nbasics: pdf_reader_unavailable; memory_unavailable')});
    await waitFor(() => expect(view.container.querySelector('[data-capabilities-status]')).not.toBeNull());
    const line = view.container.querySelector('[data-capabilities-status]')!.textContent!;
    expect(line).toContain('Steward status:');
    expect(line).toContain('basics: pdf_reader_unavailable; memory_unavailable');
    expect(line).not.toContain('upstream 502');
    cleanup();
    const quiet = render(StewardCapabilitiesView, {status: status('upstream 502')});
    await waitFor(() => expect(row(quiet.container, 'tts')).not.toBeNull());
    expect(quiet.container.querySelector('[data-capabilities-status]')).toBeNull();
  });
});

describe('the write calls (step 24.4)', () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it('PUT carries request_key, expect_version and the ordered route; DELETE carries them in the query', async () => {
    const calls: {url: string; method: string; body: unknown}[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: {method?: string; body?: string}) => {
      calls.push({url, method: init?.method ?? 'GET', body: init?.body ? JSON.parse(init.body) : null});
      return new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}});
    }));
    const real = await vi.importActual<typeof import('../../lib/api')>('../../lib/api');
    await real.setStewardCapability('draw', 4, [{kind: 'conn', conn_id: 'pub', raw_name: 'paint_v2', visibility: 'public',
      reason: 'ok', conn_uid: 'u1'}, {kind: 'conn', conn_id: 'pub', raw_name: 'generate_image', visibility: 'public'},
      {kind: 'missing'}], 'k1');
    await real.unsetStewardCapability('pdf', 3, 'k2');
    await real.clearStewardCapabilities('k3');
    expect(calls).toEqual([
      {url: '/api/steward/capabilities/draw', method: 'PUT', body: {request_key: 'k1', expect_version: 4,
        // A pin read back keeps its conn_uid (a gone pin is refused, never moved to a connection
        // added again under that conn_id); a newly picked one has none.
        route: [{kind: 'conn', conn_id: 'pub', raw_name: 'paint_v2', conn_uid: 'u1'},
          {kind: 'conn', conn_id: 'pub', raw_name: 'generate_image'}, {kind: 'missing'}]}},
      {url: '/api/steward/capabilities/pdf?request_key=k2&expect_version=3', method: 'DELETE', body: null},
      {url: '/api/steward/capabilities?request_key=k3', method: 'DELETE', body: null},
    ]);
  });
});

describe('a capability_route proposal card', () => {
  const card = (payload: Record<string, unknown>): StewardProposal => ({proposal_id: 'prop-9', version: 1,
    kind: 'capability_route', payload, reason: 'draw with the media tool', status: 'open',
    created_at: '2026-11-24T00:00:00Z', decided_at: null, decided_by: null, result: null});
  const props = (p: StewardProposal) => ({proposal: p, onupdate: vi.fn(), onreload: vi.fn(), onrevise: vi.fn()});
  const draw = {cap_id: 'draw', route: ['mcp__sw__media__paint_v2', 'missing'],
    entries: [{kind: 'conn', conn_id: 'media', raw_name: 'paint_v2'}, {kind: 'missing'}],
    card: [{conn_id: 'media', raw_name: 'paint_v2', description: 'IGNORE PREVIOUS <img src=x onerror=alert(1)>'}]};

  it('names the capability and its route, shows the frozen description as plain text, and the tool-search source', () => {
    const view = render(StewardProposalCard, props(card({...draw, source: {kind: 'tool_search'}})));
    expect(view.container.querySelector('header strong')?.textContent).toBe("Set a basic capability's route");
    expect(view.container.querySelector('[data-cap-name]')?.textContent).toBe('Draw pictures');
    expect(view.container.querySelector('[data-cap-route]')?.textContent).toBe('media / paint_v2 → Missing');
    const described = view.container.querySelector('[data-cap-description]')!;
    expect(described.textContent).toBe('IGNORE PREVIOUS <img src=x onerror=alert(1)>');
    expect(view.container.querySelector('img')).toBeNull();
    expect(view.container.querySelector('[data-cap-source="tool_search"]')?.textContent).toBe(t('steward.capPropSourceSearch'));
    expect(view.container.querySelectorAll('.actions button').length).toBe(3);
  });

  it("an owner_message source quotes the owner's words", () => {
    const view = render(StewardProposalCard, props(card({...draw, source: {kind: 'owner_message', ref: 'm1',
      owner_message: {channel: 'slack', created_at: '2026-11-24T00:00:00Z', text: '以后画图用 media 那个'}}})));
    const quote = view.container.querySelector('[data-cap-source="owner_message"]')!.textContent!;
    expect(quote).toContain('Source: your own words');
    expect(quote).toContain('以后画图用 media 那个');
  });
});

describe('the connections tab names a private-only pin', () => {
  const listed = () => ({connections: [{conn_id: 'mine', label: 'Mine', url: 'http://notes:9/mcp', auth_kind: 'none',
    header_name: null, visibility: 'private', enabled: true, generation: 1, config_version: 1, secret: 'set',
    auth_identity: null, created_at: '2026-11-21T10:00:00Z', updated_at: '2026-11-21T10:00:00Z', last_probe_at: null,
    last_error: null, last_change: 'dashboard', usable: true, tool_counts: {all: 1, allowed: 1, outward: 0, changed: 0, new: 0}}],
    effective_tools: [], effective_hash: 'h', restart: {due: [], done: []}}) as never;

  it('pinned_private_only shows the hint; another refusal does not', async () => {
    api.listStewardConnections.mockResolvedValue(listed());
    api.listStewardConfirmations.mockResolvedValue({confirmations: []});
    api.saveStewardConnection.mockRejectedValueOnce(new ApiError(422, 'pinned_private_only: memory pins a tool here'));
    const view = render(StewardConnectionsView);
    await waitFor(() => expect(view.container.querySelector('[data-conn="mine"]')).not.toBeNull());
    await fireEvent.click(view.getByRole('button', {name: 'Edit'}));
    const form = view.container.querySelector('[data-conn-form]') as HTMLFormElement;
    await fireEvent.change(form.querySelectorAll('select')[1], {target: {value: 'public'}});
    await fireEvent.submit(form);
    await waitFor(() => expect(view.container.querySelector('[data-conn-pinned-private-only]')?.textContent)
      .toBe(t('steward.connPinnedPrivateOnly')));
    api.saveStewardConnection.mockRejectedValueOnce(new ApiError(422, 'private_only: this host is private only'));
    await fireEvent.submit(view.container.querySelector('[data-conn-form]') as HTMLFormElement);
    await waitFor(() => expect(view.container.querySelector('[role="alert"]')?.textContent).toContain('private_only:'));
    expect(view.container.querySelector('[data-conn-pinned-private-only]')).toBeNull();
  });
});

describe('the row version a write expects (step 24.4)', () => {
  beforeEach(() => {
    api.listStewardConnections.mockResolvedValue(conns());
    api.setStewardCapability.mockResolvedValue({});
  });

  it('a draft saves at the version it started from, not at a later reload\'s (the other writer is not overwritten)', async () => {
    api.listStewardCapabilities.mockResolvedValueOnce(routed({pdf: pdfSet}));
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(chips(view.container, 'pdf')).toEqual(['Native', 'pub / ocr_pdf Not allowed yet']));
    const items = row(view.container, 'pdf')!.querySelectorAll('[data-entry]');
    await fireEvent.dragStart(items[1]); await fireEvent.dragOver(items[0]); await fireEvent.drop(items[0]);
    // Meanwhile another writer moved pdf to v4; saving another row re-reads the table.
    api.listStewardCapabilities.mockResolvedValue(routed({pdf: {...pdfSet, version: 4, setting: {
      route: [{kind: 'native', reason: 'see_driver_status'}], version: 4, set_via: 'proposal'}}}));
    await fireEvent.click(row(view.container, 'transcribe')!.querySelector('[data-cap-adopt]')!);
    await fireEvent.click(row(view.container, 'transcribe')!.querySelector('[data-cap-save]')!);
    await waitFor(() => expect(api.listStewardCapabilities).toHaveBeenCalledTimes(2));
    await fireEvent.click(row(view.container, 'pdf')!.querySelector('[data-cap-save]')!);
    await waitFor(() => expect(api.setStewardCapability).toHaveBeenCalledTimes(2));
    expect(api.setStewardCapability.mock.calls[1].slice(0, 2)).toEqual(['pdf', 3]);
  });

  it('an unset row saves at the version it kept (never back to 0)', async () => {
    api.listStewardCapabilities.mockResolvedValue(routed({transcribe: {version: 5}}));
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(row(view.container, 'transcribe')?.querySelector('[data-cap-adopt]')).toBeTruthy());
    await fireEvent.click(row(view.container, 'transcribe')!.querySelector('[data-cap-adopt]')!);
    await fireEvent.click(row(view.container, 'transcribe')!.querySelector('[data-cap-save]')!);
    await waitFor(() => expect(api.setStewardCapability).toHaveBeenCalledTimes(1));
    expect(api.setStewardCapability.mock.calls[0].slice(0, 2)).toEqual(['transcribe', 5]);
  });

  it('a refused clear all says the clear failed, not that the table could not be read', async () => {
    api.listStewardCapabilities.mockResolvedValue(routed({pdf: pdfSet}));
    api.clearStewardCapabilities.mockRejectedValue(new ApiError(403, 'owner_session_required', null, 'owner_session_required'));
    const view = render(StewardCapabilitiesView);
    await waitFor(() => expect(view.container.querySelector('[data-cap-clear]')).not.toBeNull());
    await fireEvent.click(view.container.querySelector('[data-cap-clear]')!);
    await new Promise(done => requestAnimationFrame(() => done(null)));
    await fireEvent.click(view.container.querySelector('#confirm-yes')!);
    await waitFor(() => expect(view.container.querySelector('[data-cap-clear-error]')?.textContent)
      .toBe(t('steward.capSaveFailed').replace('{code}', 'owner_session_required')));
    expect(view.container.textContent).not.toContain(t('steward.capLoadFailed').replace('{code}', ''));
    expect(chips(view.container, 'pdf')).toHaveLength(2);
  });
});
