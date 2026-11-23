/** Every tool registrar of the steward and the director, run through the PINNED DSH defineTool.
 *
 * 2026-11-22: the steward never booted after the 24.2 deploy - media_call declared
 * `additionalProperties` as a schema object, the pinned defineTool accepts only true/false there
 * and threw UNSUPPORTED_SCHEMA at boot, while every unit test passed definitions through
 * `defineTool: t => t`. This file hands each registrar the real defineTool with stub
 * dependencies (registration reads none of them) and checks every tool it registers is accepted.
 *
 * Not reached here (they register only inside a booted session's setup / the adapter's
 * install, which need the real DSH context); real-DSH boot tests register them through the
 * same pinned defineTool, so a refused schema fails these tests:
 * - the inline tools of dsh-director/index.mjs (todo_*, watch_process, ask_owner,
 *   close_question, idle_dormant, grep_chat_history): runtime.steward.test.mjs and
 *   runtime.smoke.test.mjs;
 * - the adapter's own (runtime.mjs: state_read, project_work, schedule_*, reply_to_director,
 *   relay_answer, send_user_message): runtime.channels.test.mjs, runtime.group.test.mjs and
 *   runtime.smoke.test.mjs
 *   (runtime.steward.test.mjs does not install them all);
 * - dsh-director/hosts/local.mjs (work_update, note_write): runtime.local-host.test.mjs, which
 *   boots the director with that host and no adapter. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { measuredEntryForTest } from './dsh-entry.mjs';
import { registerMemoryTools } from '../dsh-director/lib/memory/tools.mjs';
import { registerChannelTools } from '../dsh-director/lib/channel-tools.mjs';
import { registerGroupTools } from '../dsh-director/lib/group-tools.mjs';
import { registerConfirmTools } from '../dsh-director/lib/confirm.mjs';
import { registerConnectionTools } from '../dsh-director/lib/connections.mjs';
import { registerProposalTools } from '../dsh-director/lib/proposals.mjs';
import { registerPortfolioTools } from '../dsh-director/lib/portfolio.mjs';
import { installToolDiscovery } from '../dsh-director/lib/tool-discovery.mjs';
import { registerMediaTools } from '../dsh-director/lib/media-tools.mjs';

const jsonOutput = { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] };
const nothing = async () => ({});
const role = { presented: [], forbidden: [] };

test('every tool registrar of the steward and the director is accepted by the pinned DSH defineTool', async t => {
  const entry = await measuredEntryForTest(t);
  if (!entry) return;
  const { defineTool } = await import(createRequire(entry).resolve('@deepseek-ai/dsh-tools'));
  const registered = new Map();
  // Each registrar gets this defineTool: a refused schema throws here, naming the registrar and the tool.
  const scopeOf = registrar => ({ tools: { register: tool => { registered.set(tool.name, registrar); } } });
  const checked = registrar => definition => {
    try { return defineTool(definition); }
    catch (error) { throw new Error(`${registrar}: ${definition?.name}: ${error?.message ?? error}`); }
  };
  const ctx = { tools: { register: tool => { registered.set(tool.name, 'tool-discovery'); }, guard() {}, schemas: () => [] },
    on() {}, systemPrompt: { section() {} } };
  const groups = { familyNote: nothing, familyShow: nothing, listShow: nothing, listAdd: nothing, listCheck: nothing,
    listRemove: nothing, reminderSet: nothing };
  const turns = { open: () => undefined, audience: () => ({ kind: 'owner' }) };
  const catalog = { rootSession: 's', audience: () => ({ kind: 'owner' }), catalog: () => new Set(), outward: () => false };

  registerMemoryTools(scopeOf('memory'), { defineTool: checked('memory'), jsonOutput, memory: { store: {} }, ownerMessage: nothing });
  registerChannelTools(scopeOf('channel-tools'), { defineTool: checked('channel-tools'), jsonOutput, login: nothing });
  registerGroupTools(scopeOf('group-tools'), { defineTool: checked('group-tools'), jsonOutput, groups, turns });
  registerConfirmTools(scopeOf('confirm'), { ctx, defineTool: checked('confirm'), jsonOutput, HarnessError: Error,
    confirm: { create: nothing, deliver: nothing, get: nothing, cancel: nothing }, catalog });
  registerConnectionTools(scopeOf('connections'), { defineTool: checked('connections'), jsonOutput,
    manage: { list: nothing, tools: nothing, refresh: nothing, propose: nothing }, catalog, host: {} });
  registerProposalTools(scopeOf('proposals'), { defineTool: checked('proposals'), jsonOutput, proposals: { propose: nothing, withdraw: nothing } });
  registerPortfolioTools(scopeOf('portfolio'), { defineTool: checked('portfolio'), jsonOutput,
    portfolio: { projects: nothing, project: nothing, conversation: nothing, inbox: nothing, privateInbox: nothing,
      acknowledge: nothing, acknowledgePrivate: nothing }, handoff: nothing });
  for (const rootRole of ['director', 'steward'])
    installToolDiscovery(ctx, { defineTool: checked('tool-discovery'), HarnessError: Error, rootSession: 's', rootRole,
      roles: { director: role, steward: { ...role, catalog: 'host' }, worker: role }, catalog: () => new Set() });
  registerMediaTools(scopeOf('media'), { ctx, defineTool: checked('media'), jsonOutput, HarnessError: Error,
    media: {}, catalog, turns, channels: undefined, ledger: {} });

  const by = registrar => [...registered].filter(([, r]) => r === registrar).map(([name]) => name).sort();
  assert.deepEqual(by('memory'), ['memory_forget', 'memory_note', 'memory_profile_update', 'memory_read', 'memory_search']);
  assert.deepEqual(by('channel-tools'), ['request_wechat_qr']);
  assert.deepEqual(by('group-tools'), ['family_memory_note', 'family_memory_show', 'group_reminder_set', 'list_add', 'list_check',
    'list_remove', 'list_show']);
  assert.deepEqual(by('confirm'), ['request_confirmation', 'run_confirmed']);
  assert.deepEqual(by('connections'), ['connection_refresh', 'connection_tools', 'connections_list', 'propose_connection_change']);
  assert.deepEqual(by('proposals'), ['propose', 'withdraw_proposal']);
  assert.deepEqual(by('portfolio'), ['handoff', 'portfolio_conversation', 'portfolio_inbox', 'portfolio_inbox_acknowledge', 'portfolio_project',
    'portfolio_projects', 'private_inbox', 'private_inbox_acknowledge']);
  assert.deepEqual(by('tool-discovery'), ['call_tool', 'search_tools']);
  assert.deepEqual(by('media'), ['media_call', 'send_media']);
});
