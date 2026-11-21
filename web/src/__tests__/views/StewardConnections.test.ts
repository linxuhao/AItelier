/** The steward's own MCP connections on the dashboard (step 24.1a, design 7.8.10).
 *
 *  - each connection shows its name, id, address, auth state (never a secret),
 *    visibility, enabled, tool counts and where its last change came from;
 *  - "Read tools" probes, then lists every tool with its description as plain text and
 *    the five ticks; a changed tool is marked and comes back unticked; Save sends the
 *    ticks of every tool with the definition hash it showed (the server refuses to allow
 *    a definition that changed since); the daily cap can be typed and cleared;
 *  - the add form saves the connection, and a header value only through the secret call;
 *  - a pending fallback restart shows its line and the "Restart steward" button.
 *  The API is mocked throughout. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { authStore } from '../../stores/auth';
import { langStore } from '../../stores/i18n';

const api = vi.hoisted(() => ({
  listStewardConnections: vi.fn(), saveStewardConnection: vi.fn(), deleteStewardConnection: vi.fn(),
  stewardConnectionTools: vi.fn(), setStewardConnectionTools: vi.fn(), setStewardConnectionSecret: vi.fn(),
  probeStewardConnection: vi.fn(), startDshDriver: vi.fn(), stopDshDriver: vi.fn(),
  listStewardConfirmations: vi.fn(), startStewardOAuth: vi.fn(), confirmStewardIdentity: vi.fn(),
  signOutStewardOAuth: vi.fn(), setStewardOAuthClient: vi.fn(),
}));
vi.mock('../../lib/api', async (original) => ({...(await original<object>()), ...api}));
import type { StewardConnectionTools, StewardConnections } from '../../lib/api';
import StewardConnectionsView from '../../views/StewardConnections.svelte';

const listed = (over: Partial<StewardConnections> = {}): StewardConnections => ({
  connections: [{conn_id: 'media', label: 'Media', url: 'http://mcp_server:9003/mcp', auth_kind: 'none', header_name: null,
    visibility: 'public', enabled: true, generation: 1, config_version: 3, secret: 'set', auth_identity: null,
    created_at: '2026-11-21T10:00:00Z', updated_at: '2026-11-21T10:00:00Z', last_probe_at: '2026-11-21T10:00:00Z',
    last_error: null, last_change: 'dashboard', usable: true,
    tool_counts: {all: 2, allowed: 1, outward: 0, changed: 1, new: 0}}],
  effective_tools: ['mcp__sw__media__ocr_image'], effective_hash: 'h', restart: {due: [], done: []}, ...over,
});
const tools = (): StewardConnectionTools => ({conn_id: 'media', instructions: '<b>upload is REST</b>', last_probe_at: null, last_error: null,
  tools: [
    {raw_name: 'ocr_image', gw_name: 'ocr_image', name: 'mcp__sw__media__ocr_image', description: '<img src=x onerror=alert(1)>',
      input_schema: {}, annotations: {readOnlyHint: true}, schema_sha256: 'a', allowed: true, outward: false, egress: true,
      blocked_args: ['image_url'], daily_cap: null, state: 'allowed'},
    {raw_name: 'web_search', gw_name: 'web_search', name: 'mcp__sw__media__web_search', description: 'Search', input_schema: {},
      annotations: null, schema_sha256: 'b', allowed: true, outward: false, egress: true, blocked_args: [], daily_cap: 5,
      state: 'changed'},
  ]});

beforeEach(() => {
  cleanup(); vi.resetAllMocks(); langStore.set('en');
  authStore.set({canWrite: true, permissionResolved: true, email: 'owner@local'} as never);
  api.listStewardConnections.mockResolvedValue(listed());
  api.probeStewardConnection.mockResolvedValue(tools());
  api.stewardConnectionTools.mockResolvedValue(tools());
  api.setStewardConnectionTools.mockImplementation(async () => tools());
  api.listStewardConfirmations.mockResolvedValue({confirmations: []});
});
afterEach(() => cleanup());

const conn = (container: HTMLElement) => container.querySelector('[data-conn="media"]') as HTMLElement;

describe('the connections tab', () => {
  it('lists a connection with its counts and no secret', async () => {
    const view = render(StewardConnectionsView);
    await waitFor(() => expect(conn(view.container)).not.toBeNull());
    const text = conn(view.container).textContent ?? '';
    expect(text).toContain('Media');
    expect(text).toContain('http://mcp_server:9003/mcp');
    expect(conn(view.container).querySelector('[data-conn-counts]')?.textContent).toContain('1 allowed / 0 outward / 2 tools');
    expect(text).toContain('1 changed, check again');
    expect(view.container.querySelector('[data-restart-pending]')).toBeNull();
  });

  it('reads the tools, shows descriptions as plain text, unticks a changed one and saves every tick', async () => {
    const view = render(StewardConnectionsView);
    await waitFor(() => expect(conn(view.container)).not.toBeNull());
    await fireEvent.click(conn(view.container).querySelector('[data-conn-probe]')!);
    expect(api.probeStewardConnection).toHaveBeenCalledWith('media');
    await waitFor(() => expect(view.container.querySelector('[data-tool="ocr_image"]')).not.toBeNull());
    const ocr = view.container.querySelector('[data-tool="ocr_image"]') as HTMLElement;
    expect(ocr.querySelector('img')).toBeNull();
    expect(ocr.textContent).toContain('<img src=x onerror=alert(1)>');
    expect(view.container.querySelector('b')).toBeNull();
    const changed = view.container.querySelector('[data-tool="web_search"]') as HTMLElement;
    expect(changed.getAttribute('data-state')).toBe('changed');
    expect((changed.querySelector('[data-tick="allowed"]') as HTMLInputElement).checked).toBe(false);
    await fireEvent.click(ocr.querySelector('[data-tick="outward"]')!);
    await fireEvent.click(view.container.querySelector('[data-conn-save-tools]')!);
    await waitFor(() => expect(api.setStewardConnectionTools).toHaveBeenCalledTimes(1));
    expect(api.setStewardConnectionTools.mock.calls[0]).toEqual(['media', [
      {raw_name: 'ocr_image', allowed: true, outward: true, egress: true, blocked_args: ['image_url'], daily_cap: null,
        schema_sha256: 'a'},
      {raw_name: 'web_search', allowed: false, outward: false, egress: true, blocked_args: [], daily_cap: 5,
        schema_sha256: 'b'},
    ]]);
  });

  it('a typed daily cap is saved as a number and a cleared one as none; the tab stays usable', async () => {
    const view = render(StewardConnectionsView);
    await waitFor(() => expect(conn(view.container)).not.toBeNull());
    await fireEvent.click(conn(view.container).querySelector('[data-conn-probe]')!);
    await waitFor(() => expect(view.container.querySelector('[data-tool="ocr_image"]')).not.toBeNull());
    const capOf = (name: string) => view.container.querySelector(`[data-tool="${name}"] [data-tick="cap"]`) as HTMLInputElement;
    await fireEvent.input(capOf('ocr_image'), {target: {value: '7'}});
    await fireEvent.input(capOf('web_search'), {target: {value: ''}});
    const save = view.container.querySelector('[data-conn-save-tools]') as HTMLButtonElement;
    await fireEvent.click(save);
    await waitFor(() => expect(api.setStewardConnectionTools).toHaveBeenCalledTimes(1));
    const sent = api.setStewardConnectionTools.mock.calls[0][1] as {raw_name: string; daily_cap: number | null}[];
    expect(sent.map(c => [c.raw_name, c.daily_cap])).toEqual([['ocr_image', 7], ['web_search', null]]);
    await waitFor(() => expect(save.disabled).toBe(false));
  });

  it('adds a header connection: the value goes only through the secret call', async () => {
    api.saveStewardConnection.mockResolvedValue({});
    api.setStewardConnectionSecret.mockResolvedValue({secret: 'set'});
    const view = render(StewardConnectionsView);
    await waitFor(() => expect(view.container.querySelector('[data-conn-add]')).not.toBeNull());
    await fireEvent.click(view.container.querySelector('[data-conn-add]')!);
    const form = view.container.querySelector('[data-conn-form]') as HTMLFormElement;
    const inputs = form.querySelectorAll('input');
    await fireEvent.input(inputs[0], {target: {value: 'docs'}});
    await fireEvent.input(inputs[1], {target: {value: 'Docs'}});
    await fireEvent.input(inputs[2], {target: {value: 'https://docs.example/mcp'}});
    await fireEvent.change(form.querySelector('select')!, {target: {value: 'header'}});
    await waitFor(() => expect(form.querySelector('input[type="password"]')).not.toBeNull());
    await fireEvent.input(form.querySelector('input[type="password"]')!, {target: {value: 'Bearer s3cret'}});
    await fireEvent.submit(form);
    await waitFor(() => expect(api.setStewardConnectionSecret).toHaveBeenCalledWith('docs', 'Bearer s3cret'));
    const saved = api.saveStewardConnection.mock.calls[0][0];
    expect(saved).toEqual({conn_id: 'docs', label: 'Docs', url: 'https://docs.example/mcp', auth_kind: 'header',
      header_name: 'Authorization', visibility: 'private', enabled: false});
    expect(JSON.stringify(saved)).not.toContain('s3cret');
  });

  it('a pending fallback restart shows its line and restarts the steward on request', async () => {
    api.listStewardConnections.mockResolvedValue(listed({restart: {due: [{driver_id: 'd', reason: 'tools_stream_down',
      due_at: '2026-11-21T10:01:30Z', created_at: '2026-11-21T10:00:00Z'}], done: []}}));
    api.stopDshDriver.mockResolvedValue({});
    api.startDshDriver.mockResolvedValue({});
    const view = render(StewardConnectionsView);
    await waitFor(() => expect(view.container.querySelector('[data-restart-pending]')).not.toBeNull());
    await fireEvent.click(view.container.querySelector('[data-restart-pending] button')!);
    await waitFor(() => expect(api.startDshDriver).toHaveBeenCalledWith('aitelier-home'));
    expect(api.stopDshDriver).toHaveBeenCalledWith('aitelier-home');
  });
});

// Step 24.1b (design 7.8.6, 7.8.9, 7.8.10): OAuth with the account check, and the
// confirmations of outward actions with the text that went out.
const oauthConn = (over: Record<string, unknown> = {}) => listed({connections: [{...listed().connections[0], conn_id: 'mailpub',
  label: 'Family mail', url: 'https://mail.example/mcp', auth_kind: 'oauth', enabled: false, usable: false,
  auth_identity: 'family@example.com', identity_confirmed: false, ...over}]});

describe('an oauth connection', () => {
  it('authorizes in a new tab, shows the bound account and asks to confirm it', async () => {
    api.listStewardConnections.mockResolvedValue(oauthConn());
    api.startStewardOAuth.mockResolvedValue({conn_id: 'mailpub', authorize_url: 'https://auth.example/authorize?x=1'});
    api.confirmStewardIdentity.mockResolvedValue({});
    const opened = vi.spyOn(window, 'open').mockImplementation(() => null);
    const view = render(StewardConnectionsView);
    await waitFor(() => expect(view.container.querySelector('[data-conn-oauth]')).not.toBeNull());
    const box = view.container.querySelector('[data-conn-oauth]') as HTMLElement;
    expect(box.querySelector('[data-conn-identity]')?.textContent).toBe('Authorized as family@example.com');
    expect(box.textContent).toContain('Confirm the account before enabling the connection');
    await fireEvent.click(box.querySelector('[data-conn-authorize]')!);
    await waitFor(() => expect(opened).toHaveBeenCalledWith('https://auth.example/authorize?x=1', '_blank', 'noopener'));
    await fireEvent.click(box.querySelector('[data-conn-confirm-identity]')!);
    await waitFor(() => expect(api.confirmStewardIdentity).toHaveBeenCalledWith('mailpub', 'family@example.com'));
    opened.mockRestore();
  });

  it('a confirmed account has no confirm button; the form offers OAuth and a client id', async () => {
    api.listStewardConnections.mockResolvedValue(oauthConn({identity_confirmed: true, enabled: true, usable: true}));
    api.saveStewardConnection.mockResolvedValue({});
    api.setStewardOAuthClient.mockResolvedValue({});
    const view = render(StewardConnectionsView);
    await waitFor(() => expect(view.container.querySelector('[data-conn-oauth]')).not.toBeNull());
    expect(view.container.querySelector('[data-conn-confirm-identity]')).toBeNull();
    await fireEvent.click(view.container.querySelector('[data-conn-add]')!);
    const form = view.container.querySelector('[data-conn-form]') as HTMLFormElement;
    const select = form.querySelector('select') as HTMLSelectElement;
    expect([...select.options].map(o => o.value)).toEqual(['none', 'header', 'oauth']);
    await fireEvent.change(select, {target: {value: 'oauth'}});
    await waitFor(() => expect(form.querySelector('[data-oauth-client-id]')).not.toBeNull());
  });

  it('lists the confirmations with the text that went out, as plain text', async () => {
    api.listStewardConfirmations.mockResolvedValue({confirmations: [{code: 'K7Q2A', conn_id: 'mailpub', raw_name: 'send_message',
      gw_name: 'send_message', requester_kind: 'owner', channel: 'slack', status: 'done', created_at: '2026-11-21T10:00:00Z',
      confirmed_at: null, consumed_at: null, result: {}, message_text: '请确认（K7Q2A）<b>x</b>'}]});
    const view = render(StewardConnectionsView);
    await waitFor(() => expect(view.container.querySelector('[data-confirmation="K7Q2A"]')).not.toBeNull());
    const item = view.container.querySelector('[data-confirmation="K7Q2A"]') as HTMLElement;
    expect(item.textContent).toContain('请确认（K7Q2A）<b>x</b>');
    expect(item.querySelector('b')).toBeNull();
  });
});
