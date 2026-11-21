/** Plugin step 12 (design-steward 4), the JS side: the steward role in the table, its
 *  closed tool set, the AItelier host started as the steward, the owner's conversation
 *  recorded into memory, the owner-message check, the handoff, the portfolio reads and
 *  the reader for the inbox the wake only injects an excerpt of.
 *  The server-side limits (steward_read_only) are tests/unit's; the real-DSH boot is
 *  runtime.steward.test.mjs. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadRoles, validateRoles, roleOf } from './roles.mjs';
import { roles as aitelierRoles } from './aitelier-roles.mjs';
import { refusal, presentTools, presentSections, defaultDiscoveryTexts } from './tool-discovery.mjs';
import { aitelierHost, INBOX_EXCERPT_CHARS } from './runtime.mjs';
import { conversationTurns, stewardOps, projectSummary, issuesSummary, syncConversation, replyToLetter, unansweredOwnerMessages,
  turnWatcher, forwardFinalText, stewardTexts, INBOX_LIMITS, channelOps, refusalCode } from './steward-host.mjs';
import { PORTFOLIO_TOOLS, registerPortfolioTools } from '../dsh-director/lib/portfolio.mjs';
import { validateHost } from '../dsh-director/lib/host.mjs';
import { CHANNEL_LIMITS } from '../dsh-director/lib/channels.mjs';
import { MemoryStore } from '../dsh-director/lib/memory/store.mjs';

const text = readFileSync(new URL('../dsh-director/roles.json', import.meta.url), 'utf8');
const table = loadRoles();
const variant = edit => { const copy = JSON.parse(text); edit(copy); return copy; };
const exec = name => ({ name, arguments: {} });
/** Register what the plugin registers for a host, without DSH. */
const collectPortfolio = options => {
  const tools = new Map();
  registerPortfolioTools({ tools: { register: t => tools.set(t.name, t) } },
    { defineTool: t => t, jsonOutput: {}, ...options });
  return tools;
};

test('the steward is an optional role: loaded from roles.json, absent is fine, its keys are checked', () => {
  assert.equal(table.steward.prompt, 'steward.md');
  assert.ok(readFileSync(new URL('../dsh-director/steward.md', import.meta.url), 'utf8').length > 500);
  assert.equal(table.steward.presented_only, true);
  assert.doesNotThrow(() => validateRoles(variant(t => { delete t.steward; })), 'a table without a steward is the table before step 12');
  assert.throws(() => validateRoles(variant(t => { t.steward.models = []; })), /unknown key steward\.models/);
  assert.throws(() => validateRoles(variant(t => { delete t.steward.forbidden; })), /missing key steward\.forbidden/);
  assert.throws(() => validateRoles(variant(t => { t.steward.presented_only = 'yes'; })), /steward\.presented_only must be a boolean/);
  assert.throws(() => validateRoles(variant(t => { delete t.director; })), /missing key table\.director/);
  assert.doesNotThrow(() => validateRoles(variant(t => { t.worker.presented_only = false; })), 'presented_only is open to every role');
  assert.equal(roleOf('root', 'root', 'steward'), 'steward');
  assert.equal(roleOf('child', 'root', 'steward'), 'worker');
  assert.equal(roleOf('root', 'root'), 'director', 'the default root role is unchanged');
});

test('presented_only: the steward has exactly its presented tools; everything else is refused, however it is called', () => {
  const steward = aitelierRoles(table).steward;
  assert.deepEqual(steward.presented, [...table.steward.presented, 'send_user_message', 'reply_to_director',
    'schedule_list', 'schedule_set', 'schedule_delete'],
    'the adapter adds only the owner message and the letter reply');
  assert.ok(table.steward.presented.includes('grep_chat_history'), 'the steward recovers exact words after compaction too');
  // The inbox reader is the steward's own and nobody else's.
  assert.ok(table.steward.presented.includes('portfolio_inbox'), 'roles.json presents the inbox reader to the steward');
  assert.equal(refusal(steward, exec('portfolio_inbox')), undefined);
  for (const name of ['bash', 'read', 'write', 'edit', 'grep', 'subagent', 'call_tool', 'search_tools', 'state_read', 'todo_plan',
    'project_work', 'mcp__aitelier__state_graph_write', 'mcp__aitelier__state_graph_read', 'web_fetch', 'some_new_tool'])
    assert.equal(refusal(steward, exec(name)), `${name} is not available to this role`, name);
  for (const name of steward.presented) assert.equal(refusal(steward, exec(name)), undefined, name);
  const registered = ['bash', 'memory_search', 'memory_read', 'portfolio_projects', 'portfolio_inbox', 'handoff', 'send_user_message', 'call_tool'].map(name => ({ name }));
  assert.deepEqual(presentTools(registered, steward).map(t => t.name), ['memory_search', 'memory_read', 'portfolio_projects', 'portfolio_inbox', 'handoff', 'send_user_message']);
  // The director and the worker are untouched by the steward's closed set.
  const effective = aitelierRoles(table);
  assert.equal(refusal(effective.director, exec('some_new_tool')), undefined);
  assert.equal(refusal(effective.worker, exec('some_new_tool')), undefined);
  for (const role of ['director', 'worker']) assert.ok(!effective[role].presented.includes('portfolio_inbox'), role);
});

test('presented_only: the steward is shown no guidance for tools it cannot call', () => {
  // Measured 2026-11-18: the steward's prompt described bash, read, write, edit, glob, grep,
  // jobs, web_search, web_fetch, goals, subagent, search_tools and three MCP servers; its
  // own self-check named them as tools it was told about and could not call.
  const effective = aitelierRoles(table);
  const discovery = defaultDiscoveryTexts.sectionName;
  const names = ['deployment:persona-prefix', 'dsh-director:driver-runtime', 'aitelier:steward-snapshot',
    'tool:bash', 'tool:read', 'tool:write', 'tool:edit', 'tool:jobs', 'tool:web_search', 'tool:web_fetch', 'tool:goal',
    'tool:subagent', 'tool:subagent:depth', 'tool:session-query', 'tool:idle_dormant', 'tool:idle_dormant:detail',
    'mcp-resource-servers', 'mcp:agentmcp', 'mcp:devmcp', 'mcp:zvec', discovery, 'deployment:persona-suffix'];
  const sections = names.map(name => ({ name, text: name }));
  const steward = effective.steward;
  assert.deepEqual(presentSections(sections, steward, discovery, steward.presented).map(s => s.name),
    ['deployment:persona-prefix', 'dsh-director:driver-runtime', 'aitelier:steward-snapshot', 'tool:idle_dormant',
      'tool:idle_dormant:detail', 'deployment:persona-suffix'], 'only its own and the host\'s sections, and the sections of tools it has');
  // Judged against the tools the request actually lists, not the role table: a presented
  // tool missing from this request loses its section too.
  assert.ok(!presentSections(sections, steward, discovery, ['memory_search']).some(s => s.name === 'tool:idle_dormant'));
  // Every other role is shown every section, as before (the same array).
  for (const role of ['director', 'worker'])
    assert.equal(presentSections(sections, effective[role], discovery, ['bash']), sections, role);
  // Per MCP server: a tool of one server brings back that server's section only.
  const one = presentSections(sections, steward, discovery, [...steward.presented, 'mcp__agentmcp__ocr_image', 'search_tools']).map(s => s.name);
  assert.ok(one.includes('mcp:agentmcp') && one.includes(discovery));
  assert.ok(!one.includes('mcp:devmcp') && !one.includes('mcp:zvec') && !one.includes('mcp-resource-servers'));
  assert.ok(presentSections(sections, steward, discovery, ['read_mcp_resource']).some(s => s.name === 'mcp-resource-servers'));
  assert.equal(presentSections(undefined, steward, discovery), undefined, 'an assembly without sections is passed through');
});

const summaryNode = (i, status, extra = {}) => ({ node_key: `d.n${String(i).padStart(4, '0')}`, title: '很长的标题'.repeat(36), status,
  readiness: 'closed', revision: 1, contract_hash: 'x'.repeat(64), dependencies: [], blocked_by: [], hold: null,
  node_hold: { held: 0, reason: '' }, claims: [], next_action: null,
  latest_attempt: { status: 'candidate', updated_at: new Date(Date.UTC(2026, 10, 1) + i * 60000).toISOString(), artifact_ref: 'y'.repeat(64) },
  latest_evidence: { pass: 2 }, open_issue_count: 0, ...extra });
const summaryShape = (openKeys, closedCount = 816) => ({ project: { project_id: 'p' }, counts: { VERIFIED: closedCount }, readiness_counts: {},
  nodes: [...Array.from({ length: closedCount }, (_, i) => summaryNode(i, 'VERIFIED')), ...openKeys.map(i => summaryNode(i, 'CANDIDATE'))],
  issue_work: [summaryNode(5000, 'OPEN', { issue_id: 'iss-1' })], issue_work_counts: { OPEN: 1 }, observed_at: 'now' });
/** Walk every page, reading the overview afresh before each one (`overviewAt(page)`). */
function walk(overviewAt) {
  const seen = [];
  let after;
  for (let page = 0; page < 1000; page++) {
    const summary = projectSummary(overviewAt(page), { openAfter: after });
    assert.ok(summary.open_work.length >= 1 || summary.nodes_shown.open_next_after === null, 'a page moves forward');
    assert.equal(summary.nodes_shown.open_after, after ?? null, 'the cursor asked for is the one reported');
    seen.push(...summary.open_work.map(n => n.node_key));
    if (summary.nodes_shown.open_next_after === null) return seen;
    after = summary.nodes_shown.open_next_after;
  }
  throw new Error('walk did not end');
}

test('portfolio_project is a bounded summary: open work in pages, recent closures always kept, nothing cut silently (reviews 357, 360)', () => {
  // Measured 2026-11-18: aitelier's whole overview was ~1.4 MB (860 nodes, 816 closed), and
  // the harness spilled it to a file the steward has no tool to read.
  for (const openCount of [44, 880]) {
    const keys = Array.from({ length: openCount }, (_, i) => 1000 + i);
    const overview = summaryShape(keys), before = JSON.stringify(overview);
    const first = projectSummary(overview);
    assert.equal(JSON.stringify(overview), before, 'the overview is not changed');
    assert.ok(JSON.stringify(first).length <= 9000, `summary is ${JSON.stringify(first).length} chars`);
    // M1/M2: the recently closed are kept whatever the open count, newest first, with their total.
    assert.deepEqual(first.recently_closed_nodes.map(n => n.node_key), [815, 814, 813, 812, 811, 810, 809, 808].map(i => `d.n${String(i).padStart(4, '0')}`));
    assert.equal(first.nodes_shown.closed_total, 816);
    assert.equal(first.nodes_shown.open_total, openCount + 1); assert.equal(first.nodes_shown.all_total, 816 + openCount + 1);
    const seen = walk(() => overview);
    assert.deepEqual(seen, [...keys.map(i => `d.n${i}`), 'd.n5000'], `${openCount} open: every open item exactly once, in key order`);
  }
  // M5 (review 360): the list is read afresh for each page. An item closed between two reads,
  // or one whose change time moved, must not make the walk skip or repeat any other item.
  const keys = Array.from({ length: 40 }, (_, i) => 1000 + i);
  const firstPage = projectSummary(summaryShape(keys)).open_work.map(n => n.node_key);
  assert.ok(firstPage.length >= 2 && firstPage.length < 41, `page 1 holds ${firstPage.length}`);
  const closedOnPage1 = Number(firstPage[0].slice(3));
  const closing = walk(page => summaryShape(page === 0 ? keys : keys.filter(k => k !== closedOnPage1)));
  assert.deepEqual(closing, [...keys.map(i => `d.n${i}`), 'd.n5000'], 'a page-1 item closing mid-walk skips nothing');
  const laterKey = 1000 + firstPage.length + 2;
  const touched = walk(page => {
    const shape = summaryShape(keys);
    if (page > 0) shape.nodes.find(n => n.node_key === `d.n${laterKey}`).latest_attempt.updated_at = '2026-12-31T00:00:00Z';
    return shape;
  });
  assert.deepEqual(touched, [...keys.map(i => `d.n${i}`), 'd.n5000'], 'an item changing mid-walk is neither repeated nor lost');
  // A cursor past the end is an empty last page that says so; a cursor that is not a key is refused.
  const end = projectSummary(summaryShape(keys), { openAfter: 'd.n9999' });
  assert.deepEqual([end.open_work.length, end.nodes_shown.open_next_after, end.nodes_shown.open_remaining_after_page], [0, null, 0]);
  for (const bad of [3, -1, 3.5, '', {}]) assert.equal(projectSummary(summaryShape(keys), { openAfter: bad }).refused, 'invalid_open_after', String(bad));
  // M4: no field cut silently - titles whole; a long hold reason says it was cut and how long it was;
  // next_action, latest_evidence and who holds a claim are on the line.
  const line = projectSummary({ nodes: [summaryNode(1, 'CANDIDATE', { node_hold: { held: 1, reason: 'r'.repeat(500), updated_at: 'then' },
    blocked_by: Array.from({ length: 12 }, (_, i) => 'b' + i), next_action: 'new_attempt',
    claims: [{ driver_id: 'drv-a', subagent: 'w1', lease_expires_at: 'soon', claim_id: 'c', fence: 3 }] })] }).open_work[0];
  assert.equal(line.title.length, 180, 'the title is the overview\'s, uncut');
  assert.deepEqual([line.held, line.held_since, line.held_reason.length, line.held_reason_truncated, line.held_reason_chars], [true, 'then', 300, true, 500]);
  assert.deepEqual([line.blocked_by.length, line.blocked_by_total], [5, 12]);
  assert.equal(line.next_action, 'new_attempt'); assert.deepEqual(line.latest_evidence, { pass: 2 });
  assert.deepEqual(line.live_claims, [{ driver_id: 'drv-a', subagent: 'w1', lease_expires_at: 'soon' }]);
  // An overview of another shape is not passed through unbounded.
  assert.deepEqual(Object.keys(projectSummary({ nodes: 'x'.repeat(300000) })), ['unrecognised_overview', 'chars']);
  // M3: the open issues are the query's own page, whole, with its cursor while more follow.
  const issues = { issues: Array.from({ length: 15 }, (_, i) => ({ issue_id: `iss-${i}`, kind: 'gap', title: '问题'.repeat(150),
    status: 'open', nodes: [], contradicts_acceptance: [], work_item: null, updated_at: 'x' })), status_counts: { open: 40 }, has_more: true, next_after: 77 };
  const brief = issuesSummary(issues);
  assert.equal(brief.issues.length, 15); assert.equal(brief.issues_next_after, 77); assert.equal(brief.issues[0].title.length, 300);
  assert.deepEqual(Object.keys(brief.issues[0]), ['issue_id', 'kind', 'title', 'updated_at']);
  assert.equal('issues_next_after' in issuesSummary({ ...issues, has_more: false }), false, 'no cursor when nothing follows');
  assert.ok(JSON.stringify(brief).length <= 6000, 'a full page of the longest titles stays small');
  assert.equal(issuesSummary({ error: 'x' }).error, 'x');
});

test('the steward prompt names the tools it has: both inbox readers where it explains the excerpt', () => {
  const md = readFileSync(new URL('../dsh-director/steward.md', import.meta.url), 'utf8');
  // One direction: every inbox tool the table presents is named in the prompt, so a reader
  // can never be presented to the steward without the words that tell it to use it.
  const inboxTools = table.steward.presented.filter(name => name.includes('inbox'));
  assert.ok(inboxTools.length >= 4, `presented inbox tools: ${inboxTools.join(', ')}`);
  for (const name of inboxTools) assert.ok(md.includes(name), `steward.md never names ${name}`);
  // The paragraph that explains the 240-character excerpt is where a letter is read: naming
  // only the project reader there is the defect this guards (a private letter came back
  // unread, 2026-11-18).
  const paragraph = md.split('\n\n').find(part => part.includes('240 characters'));
  assert.ok(paragraph, 'steward.md still explains the 240-character excerpt');
  for (const name of ['portfolio_inbox', 'private_inbox'])
    assert.ok(paragraph.includes(name), `the excerpt paragraph never names ${name}`);
  // The other direction: the prompt never promises the steward a tool it is not presented.
  // Any name the table gives some role must be in the steward's set for the prompt to say it.
  // Only names built with an underscore: this is prose, and the single-word tools (read,
  // write, edit, grep, bash) are English words here - "read every project" is not a promise
  // of the read tool, and asserting on it would red for the verb.
  const anywhere = new Set(Object.values(table).flatMap(role => Array.isArray(role.presented) ? role.presented : []));
  for (const name of anywhere)
    if (name.includes('_') && new RegExp(`\\b${name}\\b`).test(md))
      assert.ok(table.steward.presented.includes(name), `steward.md names ${name}, which the steward is not presented`);
});

test('the steward prompt holds the daily brief\'s rules (step 25 C5): one short message, its order, counts only, read-only', () => {
  // Design-steward 7.9.3: what a brief carries lives here, not in the reminder row, so a new
  // capability changes this paragraph once and every brief reminder already made follows it.
  const md = readFileSync(new URL('../dsh-director/steward.md', import.meta.url), 'utf8');
  const rule = md.split('\n\n').find(part => part.startsWith('Daily brief:'));
  assert.ok(rule, 'steward.md has the daily-brief paragraph');
  const flat = rule.replace(/\s+/g, ' ');
  // The host's own tool names (send_user_message, schedule_list) never appear in a plugin file
  // (plugin-boundary.test.mjs): the paragraph names them by what they are.
  for (const words of ['ONE message through your message tool', 'at most 500 characters', "today's reminders (your reminder list)",
    'progress or are stuck (portfolio_projects)', 'the number only, never the questions', 'connected tools (calendar, mail) last',
    'send only 今天没有要你处理的事', 'more than 3 hours', '今早的简报没发成（晚了 N 小时）', 'sends no mail, writes no calendar entry',
    'confirms nothing for anyone', 'no weather'])
    assert.ok(flat.includes(words), `the daily-brief paragraph lacks: ${words}`);
  // The order the owner reads it in: reminders, projects, questions, letters.
  const at = words => flat.indexOf(words);
  assert.ok(at('reminder list') < at('portfolio_projects') && at('portfolio_projects') < at('questions wait') && at('questions wait') < at("director's letter"));
  // Within a chat message: 500 leaves room under the 600 a channel shows (CHANNEL_LIMITS.mirrorChars).
  assert.equal(CHANNEL_LIMITS.mirrorChars, 600);
});

test('the steward prompt bounds EVERY message to the owner for a phone chat, not only the unprompted ones', () => {
  const md = readFileSync(new URL('../dsh-director/steward.md', import.meta.url), 'utf8');
  // Owner, 2026-11-20: "管家的回复尽量精简，微信和slack对超长信息的显示都不friendly" - a 1604-character
  // reply with bold section titles went to WeChat. The rule is its own paragraph, not a clause
  // of the "unasked" one, so it binds a reply as much as a report.
  const rule = md.split('\n\n').find(part => part.startsWith('Every message to your owner'));
  assert.ok(rule, 'steward.md has a paragraph for every message to the owner');
  assert.ok(!rule.includes('unasked'), 'the rule is not part of the unprompted-messages paragraph');
  for (const words of ['lead with the answer', 'at most five more short lines', '300 Chinese characters', 'no tables, no headings, no bold',
    'The approval phrase line is never shortened'])
    assert.ok(rule.includes(words), `the rule says: ${words}`);
});

const env = (extra = {}) => ({ AITELIER_DSH_PROJECT: 'aitelier-home', AITELIER_DSH_SESSION: 's-1', AITELIER_DSH_PROVIDER: 'aitelier',
  AITELIER_DSH_MODEL: 'm', AITELIER_DSH_STATE_DIR: '/tmp/x', AITELIER_DSH_ENTRY: '/tmp/dsh.mjs', AITELIER_DRIVER_ID: 'drv-home',
  AITELIER_DSH_EXECUTION: 'host', ...extra });

test('the AItelier host started as the steward: steward root, memory, portfolio and handoff, no full access; a director host is unchanged', () => {
  const steward = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward' }));
  assert.equal(steward.config.rootRole, 'steward');
  assert.deepEqual(steward.config.memory, { userId: 'owner' });
  // Step 21: the owner's time zone reaches memory.timeZone from AITELIER_DSH_MEMORY_TZ.
  assert.deepEqual(aitelierHost(env({ AITELIER_DSH_ROLE: 'steward', AITELIER_DSH_MEMORY_TZ: 'Europe/Paris' })).config.memory,
    { userId: 'owner', timeZone: 'Europe/Paris' });
  assert.equal(aitelierHost(env({ AITELIER_DSH_MEMORY_TZ: 'Europe/Paris' })).config.memory, undefined, 'a director has no memory, zone or not');
  // Step 21: the local vector channel from AITELIER_DSH_MEMORY_VECTOR_URL / _MODEL, only with a url.
  assert.deepEqual(aitelierHost(env({ AITELIER_DSH_ROLE: 'steward', AITELIER_DSH_MEMORY_VECTOR_URL: 'http://127.0.0.1:18751',
    AITELIER_DSH_MEMORY_VECTOR_MODEL: 'bge-m3-q8_0' })).config.memory,
    { userId: 'owner', vector: { url: 'http://127.0.0.1:18751', model: 'bge-m3-q8_0' } });
  assert.deepEqual(aitelierHost(env({ AITELIER_DSH_ROLE: 'steward', AITELIER_DSH_MEMORY_VECTOR_MODEL: 'bge-m3-q8_0' })).config.memory,
    { userId: 'owner' }, 'no url, no vector channel');
  assert.equal(steward.config.permissionPreset, undefined, 'the steward never runs with danger-full-access');
  assert.deepEqual(steward.capabilities.slice(-5), ['memory', 'portfolio', 'handoff', 'proposals', 'channels']);
  assert.deepEqual(Object.keys(steward.channels).sort(), ['bind', 'claimAlert', 'codeCheck', 'list', 'recordAlert', 'recordDelivery', 'relay',
    'reportStatus', 'sourceOf']);
  assert.deepEqual(Object.keys(steward.portfolio).sort(),
    ['acknowledge', 'acknowledgePrivate', 'conversation', 'inbox', 'privateInbox', 'project', 'projects'],
    'the steward\'s own inbox pages are two of the reads this host provides, and acknowledging a delivery is its one write per inbox');
  assert.doesNotThrow(() => validateHost(steward));
  const director = aitelierHost(env());
  assert.equal(director.config.rootRole, undefined);
  assert.equal(director.config.memory, undefined);
  assert.equal(director.config.permissionPreset, 'danger-full-access');
  assert.deepEqual(director.capabilities, ['progress', 'workState', 'heldWork', 'roles', 'install', 'setup', 'texts', 'labels', 'attention']);
  for (const op of ['memory', 'portfolio', 'handoff', 'proposals', 'channels']) assert.equal(director[op], undefined, op);
  assert.doesNotThrow(() => validateHost(director));
  // A root role the table does not have is refused when the host is loaded.
  const rolesFile = join(mkdtempSync(join(tmpdir(), 'roles-')), 'roles.json');
  writeFileSync(rolesFile, JSON.stringify(variant(t => { delete t.steward; })));
  assert.throws(() => validateHost({ ...steward, config: { ...steward.config, rolesFile } }), /rolesFile steward prompt undefined is not next to/);
  assert.throws(() => validateHost({ ...director, config: { ...director.config, rootRole: 'worker' } }), /rootRole must be one of director, steward/);
});

/** A fake State: one conversation (newest first, paged) and the writes it was sent. */
function fakeState(messages, { pageSize } = {}) {
  const sent = [], reads = [];
  const request = async (path, body) => {
    reads.push(path);
    if (path === '/api/state/query/list_user_conversation') {
      if (body.project_id !== 'aitelier-home') return { messages: [], next_cursor: null };
      const newest = [...messages].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
      const start = body.cursor ? Number(body.cursor) : 0, size = Math.min(body.limit, pageSize ?? body.limit);
      const page = newest.slice(start, start + size);
      return { messages: page, next_cursor: start + size < newest.length ? String(start + size) : null };
    }
    if (path === '/api/state/commands/send_director_message') {
      const prior = sent.find(s => s.request_key === body.request_key);
      if (prior && JSON.stringify(prior) !== JSON.stringify(body)) throw new Error('AItelier send_director_message: HTTP 409 {"code":"idempotency_conflict"}');
      sent.push(body); return { message_id: 'dm-1' };
    }
    throw new Error('unexpected ' + path);
  };
  return { request, sent, reads };
}
const owner = (id, at, body) => ({ message_id: id, created_at: at, sender_kind: 'user', sender_driver_id: null, body, segments: [], reply_to_message_id: null });
const mine = (id, at, body, reply) => ({ message_id: id, created_at: at, sender_kind: 'driver', sender_driver_id: 'drv-home', body, segments: [], reply_to_message_id: reply ?? null });

test('conversation turns: an owner message opens a turn, the steward\'s replies join it, other senders stay out', () => {
  const turns = conversationTurns([
    mine('s2', '2026-11-17T10:03Z', 'second reply'),
    owner('o1', '2026-11-17T10:00Z', '想做个记账工具'),
    mine('s1', '2026-11-17T10:01Z', '记下了', 'o1'),
    { ...mine('x1', '2026-11-17T10:02Z', 'someone else'), sender_driver_id: 'other' },
    mine('s0', '2026-11-17T09:00Z', 'before any owner message'),
    owner('o2', '2026-11-17T11:00Z', 'next'),
    mine('s3', '2026-11-17T11:05Z', 'late reply to the first', 'o1'),
  ], 'drv-home');
  assert.deepEqual([...turns].map(([turn, parts]) => [turn, parts.map(p => `${p.role}:${p.part}`)]), [
    ['s0', ['steward:s0']],
    ['o1', ['owner:o', 'steward:s1', 'steward:s2', 'steward:s3']],
    ['o2', ['owner:o']],
  ]);
});

test('sync: the conversation lands in memory as turns, once; a reply recorded later joins its turn; forgotten stays forgotten', async () => {
  const store = await MemoryStore.open({ root: mkdtempSync(join(tmpdir(), 'steward-mem-')), userId: 'owner' });
  const conversation = [owner('o1', '2026-11-17T10:00:00Z', '想做个记账工具')];
  const state = fakeState(conversation);
  const sync = () => syncConversation({ request: state.request, project: 'aitelier-home', me: 'drv-home', memory: store });
  assert.deepEqual(await sync(), { written: 1 });
  conversation.push(mine('s1', '2026-11-17T10:01:00Z', '记下了，先列需求', 'o1'));
  assert.deepEqual(await sync(), { written: 1 }, 'only the reply is new');
  assert.deepEqual(await sync(), { written: 0 }, 'a replay writes nothing');
  const read = await store.read(['t-o1-o'], { withTurn: true });
  assert.deepEqual(read.entries.map(e => [e.id, e.role, e.text, e.context ?? false]),
    [['t-o1-o', 'owner', '想做个记账工具', false], ['t-o1-s1', 'steward', '记下了，先列需求', true]]);
  await store.forget(['t-o1-o'], { reason: 'asked', by: 'owner' });
  assert.deepEqual(await sync(), { written: 0 }, 'a forgotten message is not written back by the next sync');
  assert.equal((await store.search('记账')).grep.hits.length, 0);
  await store.close();
});

test('sync pages back only until it meets an owner message memory already has', async () => {
  const store = await MemoryStore.open({ root: mkdtempSync(join(tmpdir(), 'steward-mem-')), userId: 'owner' });
  const conversation = Array.from({ length: 5 }, (_, i) => owner('o' + i, `2026-11-17T10:0${i}:00Z`, 'msg ' + i));
  const state = fakeState(conversation, { pageSize: 2 });
  const sync = () => syncConversation({ request: state.request, project: 'aitelier-home', me: 'drv-home', memory: store });
  assert.deepEqual(await sync(), { written: 5 }, 'a first sync walks every page');
  conversation.push(owner('o5', '2026-11-17T10:05:00Z', 'msg 5'));
  state.reads.length = 0;
  assert.deepEqual(await sync(), { written: 1 });
  assert.equal(state.reads.length, 1, 'the newest page already holds a known owner message: one read');
  await store.close();
});

test('owner-message check and handoff: only the owner\'s own message, never to the steward\'s project, one handoff per message and project', async () => {
  const state = fakeState([owner('o1', '2026-11-17T10:00:00Z', 'tell wuxia to pause the save rows'), mine('s1', '2026-11-17T10:01:00Z', 'ok', 'o1')]);
  const ops = stewardOps({ request: state.request, project: 'aitelier-home', me: 'drv-home' });
  assert.equal(await ops.memory.ownerMessage('o1'), true);
  assert.equal(await ops.memory.ownerMessage('s1'), false, 'the steward\'s own message is not the owner\'s');
  assert.equal(await ops.memory.ownerMessage('nope'), false);
  assert.equal((await ops.handoff({ projectId: 'wuxia', text: 'pause', citesUserMessageId: 's1' })).refused, 'not_an_owner_message');
  assert.equal((await ops.handoff({ projectId: 'aitelier-home', text: 'x', citesUserMessageId: 'o1' })).refused, 'invalid_project');
  assert.equal(state.sent.length, 0, 'a refused handoff sends nothing');
  const done = await ops.handoff({ projectId: 'wuxia', text: 'Your owner asks you to pause the save rows.', citesUserMessageId: 'o1' });
  assert.equal(done.handed_off, true);
  assert.deepEqual({ ...state.sent[0], body: undefined }, { sender_project_id: 'aitelier-home', director_identity: 'drv-home',
    target_project_id: 'wuxia', request_key: 'handoff:o1:wuxia', subject: 'From your owner, via the steward', body: undefined });
  assert.match(state.sent[0].body, /^Your owner asks you to pause the save rows\.\n\nYour owner wrote \(message o1, .*\):\n> tell wuxia to pause the save rows$/);
  assert.equal((await ops.handoff({ projectId: 'wuxia', text: 'Your owner asks you to pause the save rows.', citesUserMessageId: 'o1' })).handed_off, true, 'a retry is a replay');
  assert.equal((await ops.handoff({ projectId: 'wuxia', text: 'other words', citesUserMessageId: 'o1' })).refused, 'already_handed_off');
});

test('portfolio: every other project with its director and open questions; one project; one conversation page', async () => {
  const calls = [];
  const request = async (path, body) => {
    calls.push([path, body]);
    if (path === '/api/state/query/list_projects') return { result: [{ project_id: 'aitelier-home', title: 'Home' }, { project_id: 'wuxia', title: 'Wuxia' }, { project_id: 'idle', title: 'Idle' }] };
    if (path === '/api/dsh-drivers') return { projects: { wuxia: { online: 1, managed: { phase: 'dormant' } } } };
    if (path === '/api/state/query/list_user_questions') return body.project_id === 'wuxia'
      ? { questions: [{ segment_id: 'q1' }], has_more: false } : { questions: [{}, {}], has_more: true };
    return { result: { path, body } };
  };
  const ops = stewardOps({ request, project: 'aitelier-home', me: 'drv-home' });
  assert.deepEqual(await ops.portfolio.projects(), { projects: [
    { project_id: 'wuxia', title: 'Wuxia', director: { phase: 'dormant', online: 1 }, open_questions: 1 },
    { project_id: 'idle', title: 'Idle', director: null, open_questions: '2+' }] });
  const one = await ops.portfolio.project('wuxia');
  assert.deepEqual(Object.keys(one), ['project_id', 'overview', 'open_issues', 'open_questions']);
  // The issue cursor reaches the query; the open-work cursor is the summary's own.
  calls.length = 0;
  const paged = await ops.portfolio.project('wuxia', undefined, { openAfter: 'd.n0003', issuesAfter: 41 });
  const issueCall = calls.find(([path]) => path === '/api/state/query/list_issues');
  assert.equal(issueCall[1].after, 41); assert.equal(issueCall[1].limit, 15);
  assert.equal(calls.find(([path]) => path === '/api/state/query/list_user_questions')[1].limit, 20);
  // M6 (review 360): the open-work cursor reaches the summary - read back from the answer.
  // (The fake overview here is not a node list, so build one that is.)
  const withNodes = stewardOps({ project: 'aitelier-home', me: 'drv-home', request: async (path, body) =>
    path === '/api/state/query/project_overview' ? { result: summaryShape([1, 2, 3, 4, 5], 2) } : request(path, body) });
  const second = await withNodes.portfolio.project('wuxia', undefined, { openAfter: 'd.n0003' });
  assert.equal(second.overview.nodes_shown.open_after, 'd.n0003');
  assert.deepEqual(second.overview.open_work.map(n => n.node_key), ['d.n0004', 'd.n0005', 'd.n5000']);
  assert.equal((await withNodes.portfolio.project('wuxia', undefined, { issuesAfter: 'x' })).refused, 'invalid_issues_after');
  void paged;
  assert.deepEqual((await ops.portfolio.conversation('wuxia', 'c-1')).body, { project_id: 'wuxia', limit: 30, cursor: 'c-1' });
  assert.ok(calls.every(([path]) => path.startsWith('/api/state/query/') || path === '/api/dsh-drivers'), 'reads only');
});

/** A fake State for this steward's own inbox: deliveries stored whole, paged by
 *  delivery_seq the way the inbox query pages them, and every call remembered. */
function fakeInbox(bodies) {
  const calls = [];
  const deliveries = bodies.map((body, index) => ({
    message: { message_id: `m-${index + 1}`, thread_id: `thr-${index + 1}`, sender_project_id: 'wuxia',
      subject: `subject ${index + 1}`, body, created_at: `2026-11-17T10:${String(index).padStart(2, '0')}:00Z` },
    delivery: { delivery_id: `dlv-${index + 1}`, delivery_seq: index + 1, status: 'unread', version: 1 },
    acked_by_me: false }));
  const request = async (path, body) => {
    calls.push([path, body]);
    if (path !== '/api/state/query/list_director_messages') throw new Error('the inbox reader called ' + path);
    if (body.project_id !== 'aitelier-home') throw new Error('the inbox reader read another project: ' + body.project_id);
    const left = deliveries.filter(d => d.delivery.delivery_seq > body.after), page = left.slice(0, body.limit);
    return { result: { project_id: body.project_id, items: page, matched_total: left.length,
      has_more: left.length > page.length, next_after: page.length ? page.at(-1).delivery.delivery_seq : body.after } };
  };
  return { request, calls, deliveries };
}

/** The inbox readers exactly as the steward is given them: the delivered registered
 *  tools over the delivered adapter operations. */
function stewardInboxTool(request) {
  const tools = new Map();
  registerPortfolioTools({ tools: { register: tool => tools.set(tool.name, tool) } },
    { defineTool: tool => tool, jsonOutput: {}, portfolio: stewardOps({ request, project: 'aitelier-home', me: 'drv-home' }).portfolio });
  assert.deepEqual([...tools.keys()],
    [...PORTFOLIO_TOOLS, 'private_inbox', 'portfolio_inbox_acknowledge', 'private_inbox_acknowledge'],
    'the steward host registers the four portfolio reads, its two inbox readers and one acknowledgement per inbox');
  return tools.get('portfolio_inbox');
}
const toolCall = { signal: AbortSignal.timeout(5000) };

test('the inbox reader (step 12 follow-up): a body longer than the injected excerpt comes back whole, equal to the stored body', async () => {
  const stored = '0123456789abcdef'.repeat(117).slice(0, 1863);
  assert.equal(stored.length, 1863);
  assert.ok(stored.length > INBOX_EXCERPT_CHARS, 'longer than the excerpt the wake injects');
  const state = fakeInbox([stored, 'tiny', 'also tiny']);
  const tool = stewardInboxTool(state.request);
  const answer = await tool.execute({ after: 0, limit: 50 }, toolCall);
  assert.equal(answer.project_id, 'aitelier-home');
  assert.deepEqual(answer.items.map(item => item.delivery_seq), [1, 2, 3]);
  const [first] = answer.items;
  assert.deepEqual({ delivery_id: first.delivery_id, delivery_seq: first.delivery_seq, message_id: first.message_id,
    body_chars: first.body_chars }, { delivery_id: 'dlv-1', delivery_seq: 1, message_id: 'm-1', body_chars: 1863 });
  assert.equal(first.body, stored, 'the whole stored body, not an excerpt');
  assert.equal(first.body_truncated, undefined);
  assert.equal(answer.matched_total, 3);
  assert.equal(answer.has_more, false);
  assert.equal(answer.next_after, null);
  // The bounds the description states are the bounds the answer reports, and the request
  // carried the per-page bound, not the 50 the caller asked for.
  const stated = tool.description.match(/at most (\d+) deliveries, one body at most (\d+) characters, and the whole answer is capped at (\d+) characters/);
  assert.ok(stated, tool.description);
  assert.deepEqual(answer.limits, { items_per_page: Number(stated[1]), body_chars: Number(stated[2]), answer_chars: Number(stated[3]) });
  assert.deepEqual(state.calls, [['/api/state/query/list_director_messages', { project_id: 'aitelier-home',
    protocol_version: 'v3', statuses: ['unread', 'acknowledged'], after: 0, limit: answer.limits.items_per_page }]]);
});

test('inbox bounds: a body past the per-body bound carries its true body_chars and a truncation flag; a page past the item bound carries matched_total, has_more and the next cursor', async () => {
  const enormous = 'y'.repeat(9000);
  const state = fakeInbox([enormous, ...Array.from({ length: 11 }, (_, i) => 'small ' + i)]);
  const tool = stewardInboxTool(state.request);
  const first = await tool.execute({ after: 0, limit: 50 }, toolCall);
  assert.equal(first.items.length, 5, 'the item bound holds, though 12 are waiting');
  const [cut] = first.items;
  assert.equal(cut.body_chars, 9000, 'the true size of the stored body');
  assert.equal(cut.body_truncated, true);
  assert.equal(cut.body, enormous.slice(0, first.limits.body_chars));
  assert.equal(first.matched_total, 12);
  assert.equal(first.has_more, true);
  assert.equal(first.next_after, 5);
  const second = await tool.execute({ after: first.next_after, limit: 50 }, toolCall);
  assert.deepEqual(second.items.map(item => item.delivery_seq), [6, 7, 8, 9, 10]);
  assert.equal(second.matched_total, 7);
  assert.equal(second.has_more, true);
  assert.equal(second.next_after, 10);
  const last = await tool.execute({ after: second.next_after, limit: 50 }, toolCall);
  assert.deepEqual(last.items.map(item => item.delivery_seq), [11, 12]);
  assert.equal(last.has_more, false);
  assert.equal(last.next_after, null);
  assert.ok(state.calls.every(([path]) => path === '/api/state/query/list_director_messages'), 'read-only: one query, no acknowledgement, no write');
});

test('the inbox answer itself is bounded: a page of long bodies is cut and counted, and its cursor still moves', async () => {
  const state = fakeInbox(Array.from({ length: 5 }, () => 'z'.repeat(9000)));
  const tool = stewardInboxTool(state.request);
  const answer = await tool.execute({ after: 0, limit: 5 }, toolCall);
  assert.ok(JSON.stringify(answer).length <= answer.limits.answer_chars, 'one call cannot flood the steward\'s context');
  assert.ok(answer.items.length >= 1 && answer.items.length < 5, 'fewer than the page asked for, not more than the cap');
  assert.equal(answer.items_omitted, 5 - answer.items.length);
  assert.equal(answer.has_more, true);
  assert.equal(answer.next_after, answer.items.at(-1).delivery_seq, 'the cursor resumes after the last delivery it showed');
});

/** A fake State holding deliveries addressed to a driver exactly as
 *  `list_driver_messages` returns them: FLAT rows (the delivery's columns plus the
 *  message's readable ones), paged by the delivery seq, `truncated` when more wait. It
 *  hands over every row it holds, including one addressed to another driver, so the
 *  reader's own narrowing is what has to keep that one out - and it records every call,
 *  so a write can be seen. */
function privateDeliveries(rows) {
  const calls = [];
  const request = async (path, body) => {
    calls.push([path, body]);
    if (path !== '/api/state/query/list_driver_messages') throw new Error('the private inbox reader called ' + path);
    const mine = rows.filter(row => row.seq > body.after).sort((a, b) => a.seq - b.seq);
    const page = mine.slice(0, body.limit);
    return { result: { driver_id: body.driver_id, messages: page, truncated: mine.length > page.length,
      next_after: page.length ? page.at(-1).seq : body.after } };
  };
  return { request, calls, rows };
}
const privateRow = (seq, over = {}) => ({ delivery_id: `dlv-${seq}`, message_id: `m-${seq}`, seq,
  target_driver_id: 'drv-home', sender_driver_id: 'drv-plat', subject: `subject ${seq}`, body: `body ${seq}`,
  created_at: `2026-11-17T10:${String(seq).padStart(2, '0')}:00Z`, status: 'unread', version: 1, ...over });

/** The private inbox reader exactly as the steward is given it: the delivered registered
 *  tool over the delivered adapter operations. */
function stewardPrivateInboxTool(request) {
  const tools = collectPortfolio({ portfolio: stewardOps({ request, project: 'aitelier-home', me: 'drv-home' }).portfolio });
  const tool = tools.get('private_inbox');
  assert.ok(tool, 'the steward host registers the private inbox reader');
  return tool;
}

test('the private inbox reader: a letter longer than the injected excerpt comes back whole, byte for byte, with its true body_chars', async () => {
  const stored = '0123456789abcdef'.repeat(117).slice(0, 1863);
  assert.equal(stored.length, 1863);
  assert.ok(stored.length > INBOX_EXCERPT_CHARS, 'longer than the excerpt a wake injects');
  const state = privateDeliveries([privateRow(1, { body: stored }), privateRow(2, { body: 'tiny' })]);
  const tool = stewardPrivateInboxTool(state.request);
  const answer = await tool.execute({}, toolCall);
  assert.equal(answer.driver_id, 'drv-home', 'the answer names the box it read');
  assert.deepEqual(answer.items.map(item => item.delivery_seq), [1, 2], 'oldest delivery first');
  const [first, second] = answer.items;
  assert.equal(first.body, stored, 'the whole stored letter, not the 240-character excerpt');
  assert.equal(first.body_chars, 1863, 'its true length, not the excerpt length');
  assert.equal(first.body_truncated, undefined);
  assert.deepEqual({ delivery_id: first.delivery_id, message_id: first.message_id, subject: first.subject,
    sender_driver_id: first.sender_driver_id, created_at: first.created_at, status: first.status, version: first.version },
  { delivery_id: 'dlv-1', message_id: 'm-1', subject: 'subject 1', sender_driver_id: 'drv-plat',
    created_at: '2026-11-17T10:01:00Z', status: 'unread', version: 1 }, 'the delivery identity and its sender');
  assert.equal(second.body, 'tiny', 'a short letter is unchanged');
  assert.equal(second.body_chars, 4);
  assert.equal(second.body_truncated, undefined);
  assert.equal(answer.has_more, false);
  assert.equal(answer.next_after, null);
  // The bounds the description states are the bounds the answer reports, and they are the
  // project reader's own constant - not a second set of numbers.
  const stated = tool.description.match(/at most (\d+) deliveries, one body at most (\d+) characters, and the whole answer is capped at (\d+) characters/);
  assert.ok(stated, tool.description);
  assert.deepEqual(answer.limits, { items_per_page: Number(stated[1]), body_chars: Number(stated[2]), answer_chars: Number(stated[3]) });
  assert.deepEqual(answer.limits, INBOX_LIMITS, 'the same INBOX_LIMITS as the project inbox reader');
});

test('the private inbox reader names the chat channel an owner message came from, and nothing for a dashboard one', async () => {
  const state = privateDeliveries([privateRow(1, { sender_driver_id: null, source_channel: 'slack' }),
    privateRow(2, { sender_driver_id: null, source_channel: 'wechat' }), privateRow(3, { sender_driver_id: null })]);
  const answer = await stewardPrivateInboxTool(state.request).execute({}, toolCall);
  assert.deepEqual(answer.items.map(item => item.source_channel), ['slack', 'wechat', undefined]);
  assert.equal('source_channel' in answer.items[2], false, 'a dashboard message carries no source key');
});

test('the private inbox reader is the steward\'s own box: a letter addressed to another driver is not in the page, one query, no write', async () => {
  const state = privateDeliveries([privateRow(1),
    privateRow(2, { target_driver_id: 'drv-plat', delivery_id: 'dlv-other', message_id: 'm-other', sender_driver_id: 'drv-x' }),
    privateRow(3)]);
  const tool = stewardPrivateInboxTool(state.request);
  const answer = await tool.execute({ after: 0, limit: 5 }, toolCall);
  assert.deepEqual(answer.items.map(item => item.delivery_id), ['dlv-1', 'dlv-3'],
    'only the deliveries addressed to this steward');
  assert.equal(answer.items.some(item => item.delivery_id === 'dlv-other'), false);
  assert.deepEqual(state.calls, [['/api/state/query/list_driver_messages', { driver_id: 'drv-home', after: 0, limit: 5 }]],
    'exactly ONE query, naming the steward\'s own driver id, and no write of any kind');
});

test('the private inbox reader pages by delivery seq: 7 long letters come back 5 then 2, nothing repeated and nothing dropped', async () => {
  // Longer than the wake excerpt, but a page of five still fits the answer bound: this
  // witnesses the PER-PAGE cut, not the answer cap (which has its own test below).
  const letter = n => `letter ${n}: `.padEnd(1200, String(n % 10));
  const rows = Array.from({ length: 7 }, (_, i) => privateRow(i + 1, { body: letter(i + 1) }));
  const state = privateDeliveries(rows);
  const tool = stewardPrivateInboxTool(state.request);
  const first = await tool.execute({}, toolCall);
  assert.equal(first.items.length, 5, 'the per-page bound holds, though 7 are waiting');
  assert.deepEqual(first.items.map(item => item.delivery_seq), [1, 2, 3, 4, 5]);
  assert.ok(first.items.every(item => item.body === letter(item.delivery_seq)), 'every body whole');
  assert.equal(first.has_more, true);
  assert.equal(first.next_after, 5);
  assert.equal(first.items_omitted, 0, 'the page bound cut nothing: the cursor resumes');
  const second = await tool.execute({ after: first.next_after }, toolCall);
  assert.deepEqual(second.items.map(item => item.delivery_seq), [6, 7]);
  assert.equal(second.has_more, false);
  assert.equal(second.next_after, null);
  const read = [...first.items, ...second.items].map(item => item.delivery_id);
  assert.deepEqual(read, rows.map(row => row.delivery_id), 'nothing repeated, nothing dropped');
  assert.equal(new Set(read).size, 7);
  assert.deepEqual(state.calls.map(([, body]) => [body.driver_id, body.after, body.limit]),
    [['drv-home', 0, 5], ['drv-home', 5, 5]], 'the second page starts where the first said it would');
});

test('the private inbox reader: a body past the per-body bound is clipped and says its true body_chars', async () => {
  const enormous = 'y'.repeat(9000);
  const state = privateDeliveries([privateRow(1, { body: enormous })]);
  const tool = stewardPrivateInboxTool(state.request);
  const answer = await tool.execute({}, toolCall);
  const [cut] = answer.items;
  assert.equal(cut.body_chars, 9000, 'the true size of the stored body');
  assert.equal(cut.body_truncated, true);
  assert.equal(cut.body.length, answer.limits.body_chars);
  assert.equal(cut.body, enormous.slice(0, answer.limits.body_chars));
});

test('the private inbox answer itself is bounded: a page of long letters is cut and counted, and its cursor still moves', async () => {
  const state = privateDeliveries(Array.from({ length: 5 }, (_, i) => privateRow(i + 1, { body: 'z'.repeat(9000) })));
  const tool = stewardPrivateInboxTool(state.request);
  const answer = await tool.execute({}, toolCall);
  assert.ok(JSON.stringify(answer).length <= answer.limits.answer_chars, 'one call cannot flood the steward\'s context');
  assert.ok(answer.items.length >= 1 && answer.items.length < 5, 'fewer than the page asked for, not more than the cap');
  assert.equal(answer.items_omitted, 5 - answer.items.length);
  assert.equal(answer.has_more, true);
  assert.equal(answer.next_after, answer.items.at(-1).delivery_seq, 'the cursor resumes after the last delivery it showed');
  assert.ok(state.calls.every(([path]) => path === '/api/state/query/list_driver_messages'), 'read-only: one query, no acknowledgement, no write');
});

test('the private inbox reader is presented to the steward, and the steward\'s own texts name it where they say where to read', async () => {
  const presented = new Set(aitelierRoles(table).steward.presented);
  assert.ok(presented.has('private_inbox'), 'roles.json presents the private inbox reader to the steward');
  assert.equal(refusal(aitelierRoles(table).steward, exec('private_inbox')), undefined);
  const registered = ['private_inbox', 'portfolio_inbox', 'bash'].map(name => ({ name }));
  assert.deepEqual(presentTools(registered, aitelierRoles(table).steward).map(t => t.name), ['private_inbox', 'portfolio_inbox']);
  for (const role of ['director', 'worker'])
    assert.ok(!aitelierRoles(table)[role].presented.includes('private_inbox'), role);
  // The reader, not only the acknowledgement: a private letter must be readable whole.
  for (const [key, text] of [['continuationQuestion', stewardTexts.continuationQuestion],
    ['reviewInstruction', stewardTexts.reviewInstruction]]) {
    assert.match(text, /private_inbox(?!_)/, key + ' names the private inbox reader');
    assert.match(text, /portfolio_inbox(?!_)/, key + ' names the project inbox reader');
  }
});

test('proposals (step 13): propose records one open proposal under a digest key; withdraw names the proposal; a refusal is an answer', async () => {
  const calls = [];
  const request = async (path, body) => {
    calls.push([path, body]);
    if (body.proposal_id === 'p-gone') throw new Error('AItelier withdraw_steward_proposal: HTTP 409 {"code":"proposal_not_open"}');
    return path.endsWith('create_steward_proposal') ? { result: { proposal_id: 'p-1', version: 1, status: 'open' } } : { result: { withdrawn: true } };
  };
  const { proposals } = stewardOps({ request, project: 'aitelier-home', me: 'drv-home' });
  const proposal = { kind: 'start_driver', payload: { project_id: 'wuxia' }, reason: 'your owner wants wuxia running again' };
  assert.deepEqual(await proposals.propose(proposal), { proposal_id: 'p-1', version: 1, status: 'open' });
  await proposals.propose(proposal);
  assert.equal(calls[0][1].request_key, calls[1][1].request_key, 'the same proposal is the same request: a replay, not a second card');
  assert.match(calls[0][1].request_key, /^proposal:[0-9a-f]{40}$/);
  assert.deepEqual({ ...calls[0][1], request_key: undefined }, { request_key: undefined, ...proposal });
  await proposals.propose({ ...proposal, reason: 'other words' });
  assert.notEqual(calls[2][1].request_key, calls[0][1].request_key);
  assert.deepEqual(await proposals.withdraw({ id: 'p-1', reason: 'owner changed their mind' }), { withdrawn: true });
  assert.deepEqual(calls[3], ['/api/state/commands/withdraw_steward_proposal', { request_key: 'withdraw-proposal:p-1', proposal_id: 'p-1', reason: 'owner changed their mind' }]);
  assert.equal((await proposals.withdraw({ id: 'p-gone', reason: 'x' })).refused, 'state_refused');
  assert.ok(calls.every(([path]) => /\/(create|withdraw)_steward_proposal$/.test(path)), 'nothing but the two steward writes');
});

test('chat channels (step 19): each plugin call is one State request; the request key is the channel message, so a redelivery is a replay', async () => {
  const calls = [];
  let answer = {};
  const request = async (path, body) => { calls.push([path, body]); if (answer instanceof Error) throw answer; return answer; };
  const channels = channelOps(request);
  answer = { result: { message_id: 'm-1', duplicate: false, approval: null } };
  assert.deepEqual(await channels.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-1', text: '同意 proposal p-1' }),
    { message_id: 'm-1', duplicate: false, approval: null });
  await channels.relay({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-1', text: '同意 proposal p-1' });
  await channels.relay({ channel: 'wechat', senderRef: 'U1', messageRef: 'ts-1', text: 'x', at: '2026-11-18T10:00:00.000Z' });
  assert.equal(calls[2][1].channel_at, '2026-11-18T10:00:00.000Z', "the channel's own time dates an approval phrase");
  assert.ok(!('channel_at' in calls[0][1]), 'no time from the channel, none sent');
  assert.equal(calls[0][0], '/api/state/commands/relay_owner_message');
  assert.deepEqual({ ...calls[0][1], request_key: undefined }, { request_key: undefined, channel: 'slack', sender_ref: 'U1',
    channel_message_ref: 'ts-1', body: '同意 proposal p-1' });
  assert.equal(calls[1][1].request_key, calls[0][1].request_key);
  assert.notEqual(calls[2][1].request_key, calls[0][1].request_key, 'the same ref on another channel is another message');
  await channels.relay({ channel: 'slack', senderRef: 'U-OTHER', messageRef: 'ts-1', text: '同意 proposal p-1' });
  assert.notEqual(calls.pop()[1].request_key, calls[0][1].request_key,
    "another sender's message with the same ref is its own request, never a replay of the owner's");
  answer = { bound: true, channel: 'wechat', sender_ref: 'wx' };
  assert.deepEqual(await channels.bind({ channel: 'wechat', senderRef: 'wx', code: '123456', messageRef: 'w-1' }), answer);
  assert.deepEqual({ ...calls[3][1], request_key: undefined }, { request_key: undefined, channel: 'wechat', sender_ref: 'wx', code: '123456',
    channel_message_ref: 'w-1' }, 'the channel message goes along: the host answers a redelivered binding message once');
  assert.ok(!calls[3][1].request_key.includes('123456'), 'the code is not in the request key');
  answer = { result: { recorded: true } };
  await channels.recordDelivery({ messageId: 'm-9', channel: 'slack', channelMessageRef: 'ts-out' });
  await channels.recordDelivery({ messageId: 'm-10', channel: 'wechat', error: 'socket closed' });
  assert.deepEqual(calls.slice(4).map(([path, body]) => [path, body]), [
    ['/api/state/commands/record_channel_delivery', { request_key: 'delivery:m-9:slack', message_id: 'm-9', channel: 'slack', channel_message_ref: 'ts-out' }],
    ['/api/state/commands/record_channel_delivery', { request_key: 'delivery:m-10:wechat', message_id: 'm-10', channel: 'wechat', error: 'socket closed' }]]);
  answer = { sources: { 'm-1': { direction: 'in', channel: 'slack', sender_ref: 'U1', channel_message_ref: 'ts-1', at: 't', error: null } } };
  assert.equal((await channels.sourceOf('m-1')).channel, 'slack');
  assert.equal(await channels.sourceOf('m-dashboard'), null, 'no row is the dashboard');
  assert.deepEqual(calls.at(-1), ['/api/state/query/message_sources', { message_ids: ['m-dashboard'] }]);
  answer = { result: { channels: [], default_channel: null } };
  assert.deepEqual(await channels.list(), { channels: [], default_channel: null });
  assert.deepEqual(calls.at(-1), ['/api/state/query/list_owner_channels', {}]);
});

test('chat channels: the store-text question is the relay command in its precheck mode, and the request key is never the relay\'s', async () => {
  const calls = [];
  let answer = { result: { store_text: true } };
  const request = async (path, body) => { calls.push([path, body]); if (answer instanceof Error) throw answer; return answer; };
  const channels = channelOps(request);
  assert.deepEqual(await channels.codeCheck({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-1', text: '\u540c\u610f proposal p-1' }),
    { store_text: true });
  assert.equal(calls[0][0], '/api/state/commands/relay_owner_message');
  assert.deepEqual({ ...calls[0][1], request_key: undefined }, { request_key: undefined, precheck: true, channel: 'slack',
    sender_ref: 'U1', channel_message_ref: 'ts-1', body: '\u540c\u610f proposal p-1' });
  assert.ok(calls[0][1].request_key.startsWith('relay-check:'), 'its own key: a check is never the relay\'s replay');
  // The host's verdict is passed through as it came: the plugin decides what to keep.
  answer = { result: { store_text: false } };
  assert.deepEqual(await channels.codeCheck({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-2', text: '\u2077\u00b9\u00b9\u2078\u00b3\u00b3' }),
    { store_text: false });
  // A refusal is the host's answer, not an outage; an outage is thrown for the plugin to read
  // as no permission.
  answer = Object.assign(new Error('AItelier /api/state/commands/relay_owner_message: HTTP 422 "channel_sender_not_bound: U1"'), { status: 422 });
  assert.deepEqual(await channels.codeCheck({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-3', text: 'hi' }),
    { refused: 'channel_sender_not_bound' });
  answer = Object.assign(new Error('AItelier /api/state/commands/relay_owner_message: HTTP 503 "down"'), { status: 503 });
  await assert.rejects(channels.codeCheck({ channel: 'slack', senderRef: 'U1', messageRef: 'ts-4', text: 'hi' }), /HTTP 503/);
});

test('chat channels: a 4xx is the channel refusal it names; a 5xx or a dropped connection is an error, not a refusal', async () => {
  const failing = (status, detail) => Object.assign(new Error(`AItelier /api/state/commands/x: HTTP ${status} ${detail}`), { status });
  for (const [error, code] of [[failing(422, '"rate_limited: 6 per minute"'), 'rate_limited'], [failing(422, '"body_too_long"'), 'body_too_long'],
    [failing(409, '{"code":"idempotency_conflict"}'), 'idempotency_conflict'], [failing(403, '"steward_read_only: no"'), 'steward_read_only'],
    [failing(500, '"internal"'), undefined], [new TypeError('fetch failed'), undefined], [failing(422, '"Not A Code"'), undefined]])
    assert.equal(refusalCode(error), code, error.message);
  let thrown = failing(422, '"channel_sender_not_bound: U9 is not bound"');
  const channels = channelOps(async () => { throw thrown; });
  assert.deepEqual(await channels.relay({ channel: 'slack', senderRef: 'U9', messageRef: 'x', text: 'hi' }), { refused: 'channel_sender_not_bound' });
  // A body that is not valid Unicode text (a lone surrogate) is refused, not an outage: as an
  // error the WeChat plugin would retry it for as long as it is answered so, holding the lane behind it.
  thrown = failing(422, '"invalid_body: body is not valid Unicode text"');
  assert.deepEqual(await channels.relay({ channel: 'slack', senderRef: 'U9', messageRef: 'x', text: 'hi \ud800' }), { refused: 'invalid_body' });
  // An approval phrase without the channel's time, and a live binding code pasted by the
  // bound owner, are the sender's own refusals: answered, never retried as an outage.
  for (const code of ['approval_undated', 'code_sent_to_bound_account']) {
    thrown = failing(422, `"${code}: refused"`);
    assert.deepEqual(await channels.relay({ channel: 'slack', senderRef: 'U9', messageRef: 'x', text: '同意 proposal p-1' }), { refused: code });
  }
  thrown = failing(422, '"code_expired"');
  assert.deepEqual(await channels.bind({ channel: 'slack', senderRef: 'U9', code: '111111', messageRef: 'y' }), { refused: 'code_expired' });
  thrown = failing(422, '"code_retired"');
  assert.deepEqual(await channels.bind({ channel: 'slack', senderRef: 'U9', code: '111111', messageRef: 'z' }), { refused: 'code_retired' },
    'a retired code is a refusal the stranger is answered once for, not an outage (a WeChat message would be held and retried)');
  // A 4xx that names no code - a validation 422 whose detail is a list (a sender_ref over 200
  // characters) - is deterministic: refused invalid_request, never 'unavailable' (which the
  // WeChat plugin retries for as long as it is answered so, holding the lane behind that one message).
  for (const detail of [`"[{'field': 'sender_ref', 'error': 'String should have at most 200 characters'}]"`,
    '[{"type":"string_too_long","loc":["body","channel_message_ref"],"msg":"too long"}]', '"Not A Code"', ''])
    for (const call of [() => channels.relay({ channel: 'slack', senderRef: 'U'.repeat(201), messageRef: 'x', text: 'hi' }),
      () => channels.bind({ channel: 'slack', senderRef: 'U'.repeat(201), code: '111111', messageRef: 'y' }),
      () => channels.recordDelivery({ messageId: 'm-1', channel: 'slack', channelMessageRef: 'r'.repeat(201) })]) {
      thrown = failing(422, detail);
      assert.deepEqual(await call(), { refused: 'invalid_request' }, detail);
    }
  thrown = failing(400, '');
  assert.deepEqual(await channels.relay({ channel: 'slack', senderRef: 'U9', messageRef: 'x', text: 'hi' }), { refused: 'invalid_request' });
  for (const status of [401, 403]) {
    thrown = failing(status, '"Not authenticated"');
    await assert.rejects(channels.relay({ channel: 'slack', senderRef: 'U9', messageRef: 'x', text: 'hi' }), new RegExp(`HTTP ${status}`),
      'an auth failure without a code is still a failure, never a refusal');
  }
  // Only a 400 or 422 is the request's own fault: any other codeless 4xx (a wrong route, a
  // timeout, a conflict, a body the proxy took as too large, a busy server) is an error the
  // channel tries again, never a final refusal of the owner's message.
  for (const status of [404, 405, 408, 409, 413, 429])
    for (const detail of ['"Not Found"', '']) {
      thrown = failing(status, detail);
      await assert.rejects(channels.relay({ channel: 'slack', senderRef: 'U9', messageRef: 'x', text: 'hi' }), new RegExp(`HTTP ${status}`),
        `a codeless ${status} is not invalid_request`);
      await assert.rejects(channels.recordDelivery({ messageId: 'm-1', channel: 'slack' }), new RegExp(`HTTP ${status}`));
    }
  thrown = failing(503, '"busy"');
  await assert.rejects(channels.relay({ channel: 'slack', senderRef: 'U9', messageRef: 'x', text: 'hi' }), /HTTP 503/);
  // A 4xx that is not a channel refusal (a misconfigured token, say) is a failure to log, never hidden as one.
  thrown = failing(403, '"steward_read_only: no"');
  await assert.rejects(channels.relay({ channel: 'slack', senderRef: 'U9', messageRef: 'x', text: 'hi' }), /HTTP 403/);
  thrown = failing(401, '"unauthorized: bad token"');
  await assert.rejects(channels.recordDelivery({ messageId: 'm-1', channel: 'slack' }), /HTTP 401/);
  thrown = failing(404, '"unknown_message: not yours"');
  assert.deepEqual(await channels.recordDelivery({ messageId: 'm-1', channel: 'slack' }), { refused: 'unknown_message' });
  // A 200 that carries a code is a refusal too.
  assert.deepEqual(await channelOps(async () => ({ result: { code: 'invalid_code' } })).bind({ channel: 'slack', senderRef: 'U', code: '1', messageRef: 'z' }),
    { refused: 'invalid_code' });
});

test('chat channels: a status change is one report_channel_status, each under its own key; its refusals are answers', async () => {
  const calls = [];
  let answer = { result: { recorded: true, channel: 'wechat', connected: false, at: 't' } };
  const request = async (path, body) => { calls.push([path, body]); if (answer instanceof Error) throw answer; return answer; };
  const channels = channelOps(request);
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M4 4.5h7"/></svg>';
  assert.deepEqual(await channels.reportStatus({ channel: 'wechat', connected: false, detail: 'scan needed', qrSvg: svg }), answer.result);
  await new Promise(resolve => setTimeout(resolve, 2));
  await channels.reportStatus({ channel: 'wechat', connected: true });
  await new Promise(resolve => setTimeout(resolve, 2));
  await channels.reportStatus({ channel: 'wechat', connected: false, detail: 'scan needed', qrSvg: svg });
  assert.deepEqual(calls.map(([path, { request_key, ...body }]) => [path, body]), [
    ['/api/state/commands/report_channel_status', { channel: 'wechat', connected: false, detail: 'scan needed', qr_svg: svg }],
    ['/api/state/commands/report_channel_status', { channel: 'wechat', connected: true }],
    ['/api/state/commands/report_channel_status', { channel: 'wechat', connected: false, detail: 'scan needed', qr_svg: svg }]]);
  assert.equal(new Set(calls.map(([, body]) => body.request_key)).size, 3, 'a status that comes back is a new report, not a replay');
  assert.ok(calls.every(([, body]) => body.request_key.length <= 320 && !body.request_key.includes('svg')));
  answer = Object.assign(new Error('AItelier /api/state/commands/report_channel_status: HTTP 422 "qr_svg_refused: no"'), { status: 422 });
  assert.deepEqual(await channels.reportStatus({ channel: 'wechat', connected: false, qrSvg: '<svg onload="x"></svg>' }), { refused: 'qr_svg_refused' });
  answer = Object.assign(new Error('AItelier /api/state/commands/report_channel_status: HTTP 503'), { status: 503 });
  await assert.rejects(channels.reportStatus({ channel: 'slack', connected: true }), /HTTP 503/);
});

test('channel-down alert (step 25 C12): one claim and one record per outage and attempt, under keys a restart derives the same way', async () => {
  const calls = [];
  let answer = { result: { channel: 'wechat', kind: 'down', down_since: 'D', detail: 'x', via: 'slack', attempt: 1 } };
  const request = async (path, body) => { calls.push([path, body]); if (answer instanceof Error) throw answer; return answer; };
  const channels = channelOps(request);
  assert.deepEqual(await channels.claimAlert({ channel: 'wechat', kind: 'down', downSince: '2026-11-21T10:00:00+00:00', attempt: 1 }), answer.result);
  answer = { result: { held: 'no_channel' } };
  assert.deepEqual(await channels.claimAlert({ channel: 'wechat', kind: 'up', downSince: '2026-11-21T10:00:00+00:00', attempt: 1 }), { held: 'no_channel' });
  answer = { result: {} };
  assert.deepEqual(await channels.claimAlert({ channel: 'wechat', kind: 'down', downSince: '2026-11-21T10:00:00+00:00', attempt: 2 }), {});
  answer = { result: { recorded: true, channel: 'wechat', kind: 'down' } };
  await channels.recordAlert({ channel: 'wechat', kind: 'down', downSince: '2026-11-21T10:00:00+00:00', via: 'slack', attempt: 1 });
  await channels.recordAlert({ channel: 'wechat', kind: 'down', downSince: '2026-11-21T10:00:00+00:00', via: 'slack', attempt: 2,
    error: '409 recipient_unknown ' + 'e'.repeat(400) });
  assert.deepEqual(calls, [
    ['/api/state/commands/claim_channel_alert', { request_key: 'channel-alert:wechat:down:2026-11-21T10:00:00+00:00:1', channel: 'wechat', kind: 'down',
      down_since: '2026-11-21T10:00:00+00:00' }],
    ['/api/state/commands/claim_channel_alert', { request_key: 'channel-alert:wechat:up:2026-11-21T10:00:00+00:00:1', channel: 'wechat', kind: 'up',
      down_since: '2026-11-21T10:00:00+00:00' }],
    ['/api/state/commands/claim_channel_alert', { request_key: 'channel-alert:wechat:down:2026-11-21T10:00:00+00:00:2', channel: 'wechat', kind: 'down',
      down_since: '2026-11-21T10:00:00+00:00' }],
    ['/api/state/commands/record_channel_alert', { request_key: 'channel-alert-result:wechat:down:2026-11-21T10:00:00+00:00:1', channel: 'wechat',
      kind: 'down', down_since: '2026-11-21T10:00:00+00:00', via: 'slack', ok: true }],
    ['/api/state/commands/record_channel_alert', { request_key: 'channel-alert-result:wechat:down:2026-11-21T10:00:00+00:00:2', channel: 'wechat',
      kind: 'down', down_since: '2026-11-21T10:00:00+00:00', via: 'slack', ok: false, error: ('409 recipient_unknown ' + 'e'.repeat(400)).slice(0, 300) }]]);
  answer = Object.assign(new Error('AItelier /api/state/commands/claim_channel_alert: HTTP 403 "steward_role_required"'), { status: 403 });
  await assert.rejects(channels.claimAlert({ channel: 'wechat', kind: 'down', downSince: 'D', attempt: 1 }), /HTTP 403/);
});

test('owner memory requests (step 14): each kind runs against the store as the owner; results go back; an unwritten result runs again', async () => {
  const { runOwnerMemoryRequest, processMemoryRequests, MEMORY_REQUEST_EVENTS } = await import('./memory-requests.mjs');
  const store = await MemoryStore.open({ root: mkdtempSync(join(tmpdir(), 'steward-mem-')), userId: 'owner' });
  await store.recordTurn('o1', [{ role: 'owner', part: 'o', text: 'secret account 4242' }, { role: 'owner', part: 'x', text: 'keep this' }]);
  const run = (kind, payload) => runOwnerMemoryRequest(store, { kind, payload });
  const profile = await run('profile_read');
  assert.equal(profile.text, '');
  const updated = await run('profile_update', { text: 'likes short replies', expected_version: profile.version });
  assert.equal(updated.updated, true);
  assert.equal((await run('profile_update', { text: 'again', expected_version: profile.version })).refused, 'version_conflict', 'a repeat with the old version is refused');
  assert.deepEqual((await run('forget', { ids: ['t-o1-o'] })).forgotten, ['t-o1-o']);
  assert.deepEqual((await run('forgotten_list')).forgotten.map(r => [r.id, r.by]), [['t-o1-o', 'owner']]);
  assert.deepEqual((await run('restore', { id: 't-o1-o' })).restored, 't-o1-o');
  assert.equal((await run('purge', { ids: ['t-o1-o'] })).purged, true);
  assert.deepEqual((await store.read(['t-o1-o'])).not_found, ['t-o1-o']);
  assert.equal((await run('deploy')).refused, 'unknown_kind');
  assert.deepEqual(await runOwnerMemoryRequest(undefined, { kind: 'profile_read' }), { refused: 'memory_unavailable' });
  // The loop: every pending row runs and is completed; a completion that fails stays pending.
  const completed = [];
  let failOnce = true;
  const request = async (path, body) => {
    if (path.endsWith('list_steward_memory_requests')) return { result: { requests: [{ request_id: 'r1', kind: 'forgotten_list' }, { request_id: 'r2', kind: 'profile_read' }] } };
    if (body.request_id === 'r2' && failOnce) { failOnce = false; throw new Error('HTTP 503'); }
    completed.push(body); return { result: { completed: true } };
  };
  const logged = [];
  assert.deepEqual(await processMemoryRequests({ request, memory: store, log: line => logged.push(line) }), { pending: 2, done: 1 });
  assert.match(logged[0], /memory request r2: result not written/);
  assert.deepEqual(await processMemoryRequests({ request, memory: store }), { pending: 2, done: 2 });
  assert.deepEqual(completed.map(c => [c.request_key, c.request_id]), [['memory-request:r1', 'r1'], ['memory-request:r1', 'r1'], ['memory-request:r2', 'r2']]);
  assert.equal(completed.at(-1).result.text, 'likes short replies');
  assert.ok(MEMORY_REQUEST_EVENTS.has('steward_memory_request_created'));
  await store.close();
});

test('inbox acknowledgement: the registered tool writes ONE acknowledge for the delivery the wake showed, and a retry is the same request', async () => {
  const calls = [];
  const request = async (path, body) => {
    calls.push([path, body]);
    if (path !== '/api/state/commands/acknowledge_director_message') throw new Error('unexpected ' + path);
    if (body.delivery_id === 'd-gone') {
      // The director-messaging contract's refusal envelope, not an HTTP error.
      return { schema: 'aitelier.director-messaging.v3', code: 'unknown_delivery',
        detail: { message: 'unknown_delivery' } };
    }
    return { schema: 'aitelier.director-messaging.v3', result: {
      delivery: { delivery_id: body.delivery_id, status: 'resolved', version: body.expected_version + 1 },
      replayed: calls.length > 1 } };
  };
  const ops = stewardOps({ request, project: 'aitelier-home', me: 'drv-home' });
  const tools = collectPortfolio({ portfolio: ops.portfolio });
  assert.ok(tools.has('portfolio_inbox_acknowledge'), 'registered when the host acknowledges');
  const tool = tools.get('portfolio_inbox_acknowledge');
  const first = await tool.execute({ delivery_id: 'd-1', version: 1 }, exec);
  assert.deepEqual(first.delivery, { delivery_id: 'd-1', status: 'resolved', version: 2 });
  assert.equal(calls.length, 1, 'one acknowledgement is one write');
  assert.deepEqual(calls[0], ['/api/state/commands/acknowledge_director_message', {
    project_id: 'aitelier-home', protocol_version: 'v3', delivery_id: 'd-1', expected_version: 1,
    request_key: 'steward-ack:d-1:1' }], 'the wake\'s delivery identity and version, its own project only');
  // Same delivery and version = same request key: State replays it, no second write.
  assert.equal((await tool.execute({ delivery_id: 'd-1', version: 1 }, exec)).replayed, true);
  assert.equal(calls[1][1].request_key, calls[0][1].request_key);
  assert.ok(calls.every(([path]) => path === '/api/state/commands/acknowledge_director_message'), 'nothing else was written');
  // An unknown delivery is a named refusal, never a silent success.
  const unknown = await tool.execute({ delivery_id: 'd-gone', version: 1 }, exec);
  assert.deepEqual({ refused: unknown.refused, code: unknown.code }, { refused: 'state_refused', code: 'unknown_delivery' });
  // What it cannot identify writes nothing at all.
  const before = calls.length;
  assert.equal((await tool.execute({ delivery_id: '', version: 1 }, exec)).refused, 'invalid_delivery');
  assert.equal((await tool.execute({ delivery_id: 'd-1', version: 0 }, exec)).refused, 'invalid_version');
  assert.equal(calls.length, before, 'a refused call writes nothing');
  // A host that does not offer the acknowledgement registers no such tool.
  const readOnly = collectPortfolio({ portfolio: { projects: async () => ({}), project: async () => ({}),
    conversation: async () => ({}) } });
  assert.equal(readOnly.has('portfolio_inbox_acknowledge'), false);
});

/** A fake State holding the PRIVATE deliveries addressed to this steward, exactly the page
 *  `list_driver_messages` returns for it (a delivery with no sender is a system notice and is
 *  still addressed to this driver), and recording every write. `refuse` makes the write answer
 *  the way the real server answers a refused request: a 200 whose inner result carries a code
 *  and a message, never a thrown error. */
function privateInbox({ refuse = null } = {}) {
  const writes = [], reads = [];
  const deliveries = [
    { delivery_id: 'd-priv', version: 3, status: 'unread', subject: 'ping', sender_driver_id: 'drv-plat', delivery_seq: 1 },
    { delivery_id: 'd-notice', version: 1, status: 'unread', subject: 'lease notice', sender_driver_id: null, delivery_seq: 2 },
  ];
  const request = async (path, body) => {
    if (path === '/api/state/query/list_driver_messages') {
      reads.push(body);
      if (body.driver_id !== 'drv-home') throw new Error('the private inbox read another driver: ' + body.driver_id);
      return { result: { driver_id: body.driver_id, messages: deliveries, truncated: false, next_after: 2 } };
    }
    if (path !== '/api/state/commands/acknowledge_driver_message') throw new Error('unexpected ' + path);
    writes.push(body);
    if (refuse) return { result: { schema: 'aitelier.driver-inbox.v1', code: refuse,
      detail: { message: refuse + ': the transition refused this delivery' } } };
    return { result: { delivery_id: body.delivery_id, status: 'acknowledged', version: body.expected_version + 1 } };
  };
  return { request, writes, reads, base: { request, project: 'aitelier-home', me: 'drv-home' } };
}

test('private inbox acknowledgement (iss-cef568c3d62e4962): ONE acknowledge for the private letter, at the version State holds', async () => {
  const { writes, reads, base } = privateInbox();
  const ops = stewardOps(base);
  const tools = collectPortfolio({ portfolio: ops.portfolio });
  assert.ok(tools.has('private_inbox_acknowledge'), 'registered when the host acknowledges its private inbox');
  const tool = tools.get('private_inbox_acknowledge');
  const answer = await tool.execute({ delivery_id: 'd-priv' }, exec);
  assert.deepEqual({ acknowledged: answer.acknowledged, inbox: answer.inbox, delivery_id: answer.delivery_id,
    status: answer.status, version: answer.version },
  { acknowledged: true, inbox: 'private', delivery_id: 'd-priv', status: 'acknowledged', version: 4 },
  'the settled delivery comes back as the answer');
  assert.equal(writes.length, 1, 'one acknowledgement is one write');
  assert.deepEqual(writes[0], { delivery_id: 'd-priv', expected_version: 3,
    request_key: 'steward-ack-private:d-priv:3' }, 'the looked-up version, and a key derived from the delivery');
  // The version the caller was shown, when it is the one State holds, is the same request.
  assert.equal((await tool.execute({ delivery_id: 'd-priv', version: 3 }, exec)).acknowledged, true);
  assert.deepEqual(writes[1], writes[0], 'same delivery and version = same request: State replays it');
  // A system notice has no sender and is still a delivery addressed to this steward.
  assert.equal((await tool.execute({ delivery_id: 'd-notice' }, exec)).status, 'acknowledged');
  assert.equal(writes[2].expected_version, 1);
  // A version the delivery has moved past is refused rather than silently overridden.
  const before = writes.length;
  const stale = await tool.execute({ delivery_id: 'd-priv', version: 2 }, exec);
  assert.deepEqual({ refused: stale.refused, current_version: stale.current_version },
    { refused: 'version_conflict', current_version: 3 });
  // What it cannot identify writes nothing at all.
  assert.equal((await tool.execute({ delivery_id: '', version: 1 }, exec)).refused, 'invalid_delivery');
  assert.equal((await tool.execute({ delivery_id: 'd-priv', version: 0 }, exec)).refused, 'invalid_version');
  assert.equal(writes.length, before, 'a refused call writes nothing');
  assert.ok(reads.every(read => read.driver_id === 'drv-home'), 'only the steward\'s own inbox is ever read');
});

test('private inbox acknowledgement: a delivery not addressed to the steward is unknown_delivery, and no write is made', async () => {
  const { writes, reads, base } = privateInbox();
  const ops = stewardOps(base);
  assert.equal((await ops.portfolio.acknowledgePrivate({ deliveryId: 'd-other' })).refused, 'unknown_delivery');
  assert.equal((await ops.portfolio.acknowledgePrivate({ deliveryId: 'd-other', version: 1 })).refused, 'unknown_delivery');
  assert.equal(writes.length, 0, 'the lookup is the steward\'s own inbox: nothing was written');
  assert.ok(reads.length >= 1 && reads.every(read => read.driver_id === 'drv-home'));
});

test('private inbox acknowledgement: a 200 whose inner result carries a code is a refusal naming it, never a success', async () => {
  const { writes, base } = privateInbox({ refuse: 'unknown_delivery' });
  const ops = stewardOps(base);
  const answer = await ops.portfolio.acknowledgePrivate({ deliveryId: 'd-priv', version: 3 });
  assert.equal(writes.length, 1, 'the write was attempted');
  assert.equal(answer.acknowledged, undefined, 'a refusal must not claim the letter was settled');
  assert.deepEqual({ refused: answer.refused, code: answer.code }, { refused: 'state_refused', code: 'unknown_delivery' });
  assert.match(String(answer.reason), /unknown_delivery/, 'the server\'s own message is carried, so the steward can act on it');
});

test('private inbox acknowledgement: registered only when the host offers it; acknowledgement only, and the steward is told about it', () => {
  const { base } = privateInbox();
  const ops = stewardOps(base);
  const tools = collectPortfolio({ portfolio: ops.portfolio });
  const tool = tools.get('private_inbox_acknowledge');
  assert.ok(tool, 'registered when the op is provided');
  assert.deepEqual(tool.parameters.delivery_id, { type: 'string', required: true });
  assert.equal(tool.parameters.version.type, 'integer');
  assert.equal(tool.parameters.version.required, undefined, 'the version is optional: the host looks it up');
  // The description says what it is for and what it is not.
  assert.match(tool.description, /ONE delivery addressed to your own private inbox/);
  assert.match(tool.description, /wake re-injects every unread private delivery/);
  assert.match(tool.description, /stops the same letter coming back/);
  assert.match(tool.description, /acknowledgement only: no resolve, no other driver, no break-glass/);
  assert.equal(/resolve_driver_message|resolve_director_message/.test(tool.description), false,
    'it names no resolve action, only the refusal of resolving');
  // A host that does not offer the private acknowledgement registers no such tool; the two
  // inbox writes are registered independently of each other.
  const noPrivate = collectPortfolio({ portfolio: { ...ops.portfolio, acknowledgePrivate: undefined } });
  assert.equal(noPrivate.has('private_inbox_acknowledge'), false);
  assert.ok(noPrivate.has('portfolio_inbox_acknowledge'), 'the project-inbox write is unaffected');
  const noProject = collectPortfolio({ portfolio: { ...ops.portfolio, acknowledge: undefined } });
  assert.equal(noProject.has('portfolio_inbox_acknowledge'), false);
  assert.ok(noProject.has('private_inbox_acknowledge'), 'the private-inbox write is unaffected');
  // The steward's own wording names it, next to the project letter's acknowledgement.
  assert.match(stewardTexts.continuationQuestion,
    /portfolio_inbox_acknowledge \(a project letter\) or private_inbox_acknowledge \(a letter in your private inbox\)/);
});

/** A fake State holding one letter in each inbox and recording every write.
 *  `refuse` makes the write answer the way the real server answers a refused request: a
 *  200 whose inner result carries a code and a message (never a thrown error). */
function letters({ refuse = null } = {}) {
  const writes = [];
  const request = async (path, body) => {
    if (path.endsWith('/list_director_messages')) return { result: { items: [{ message: { message_id: 'm1', subject: 'Steward boot',
      sender_project_id: 'aitelier' }, delivery: { delivery_id: 'd-proj', version: 2, status: 'unread', delivery_seq: 1 } }], has_more: false } };
    if (path.endsWith('/list_driver_messages')) return { driver_id: 'drv-home', truncated: false, next_after: 1,
      messages: [{ delivery_id: 'd-priv', version: 1, status: 'unread', subject: 'ping', sender_driver_id: 'drv-plat' }] };
    writes.push([path.split('/').at(-1), body]);
    if (refuse) return { result: { schema: 'aitelier.director-messaging.v3', code: refuse,
      detail: { message: 'target_project_id / broadcast / reply_to_delivery_id: choose exactly one destination; replies omit target_project_id' } } };
    return { result: { ok: true } };
  };
  return { request, writes, base: { request, project: 'aitelier-home', me: 'drv-home' } };
}

test('letters (owner 2026-11-17): a reply goes back to the sender in either inbox, threaded on the cited delivery', async () => {
  const { writes, base } = letters();
  assert.deepEqual(await replyToLetter({ ...base, inbox: 'project', deliveryId: 'd-proj', text: '收到，已读完整封信' }),
    { replied: true, inbox: 'project', to: 'aitelier', ok: true });
  assert.deepEqual(writes[0][0], 'send_director_message');
  // ONE destination: the cited delivery. Sending target_project_id as well is refused by
  // the real server with invalid_request (measured 2026-11-17, iss-8ba3f8ce4b254704), so
  // the assertion is that the key is absent, not that it names the same sender.
  assert.equal('target_project_id' in writes[0][1], false, 'a reply omits target_project_id');
  assert.deepEqual({ ...writes[0][1], request_key: undefined }, { protocol_version: 'v3', sender_project_id: 'aitelier-home',
    director_identity: 'drv-home', request_key: undefined, subject: 'Re: Steward boot', body: '收到，已读完整封信',
    reply_to_delivery_id: 'd-proj' });
  await replyToLetter({ ...base, inbox: 'private', deliveryId: 'd-priv', text: 'noted' });
  assert.deepEqual(writes[1], ['send_driver_message', { request_key: writes[1][1].request_key, subject: 'Re: ping', body: 'noted',
    target_driver_id: 'drv-plat', reply_to_delivery_id: 'd-priv' }]);
  assert.match(writes[1][1].request_key, /^reply:d-priv:[0-9a-f]{16}$/);
  // Refused before anything is written: an unknown letter, another inbox name, no text.
  assert.equal((await replyToLetter({ ...base, inbox: 'private', deliveryId: 'nope', text: 'x' })).refused, 'unknown_delivery');
  assert.equal((await replyToLetter({ ...base, inbox: 'email', deliveryId: 'd-proj', text: 'x' })).refused, 'unknown_inbox');
  assert.equal((await replyToLetter({ ...base, inbox: 'project', deliveryId: 'd-proj', text: ' ' })).refused, 'empty_text');
  assert.equal(writes.length, 2);
});

test('a refused reply is not reported as sent: a 200 with a code inside the result is a refusal in either inbox', async () => {
  for (const [inbox, deliveryId, command] of [['project', 'd-proj', 'send_director_message'],
                                             ['private', 'd-priv', 'send_driver_message']]) {
    const { writes, base } = letters({ refuse: 'invalid_request' });
    const answer = await replyToLetter({ ...base, inbox, deliveryId, text: '收到' });
    assert.equal(writes.length, 1, `${inbox}: the write was attempted`);
    assert.equal(writes[0][0], command);
    assert.equal(answer.replied, undefined, `${inbox}: a refusal must not claim a sent reply`);
    assert.equal(answer.refused, 'invalid_request', `${inbox}: the refusal names the server's code`);
    assert.match(String(answer.reason), /choose exactly one destination|one destination/,
      `${inbox}: the server's own message is carried, so the steward can act on it`);
  }
});

test('every owner message gets an answer: the unanswered ones; the last words of a finished turn go to the owner; dormancy is held', async () => {
  const conversation = [owner('o1', '2026-11-17T10:00:00Z', 'first'), mine('s1', '2026-11-17T10:01:00Z', 'answer'),
    owner('o2', '2026-11-17T10:02:00Z', 'second'), owner('o3', '2026-11-17T10:03:00Z', 'third')];
  const sent = [];
  let conflict = false;
  const request = async (path, body) => {
    if (path.endsWith('/list_user_conversation')) return { messages: conversation, next_cursor: null };
    if (conflict) throw new Error('AItelier send_user_message: HTTP 409 {"code":"idempotency_conflict"}');
    sent.push(body); return { result: { message_id: 'r1' } };
  };
  const base = { request, project: 'aitelier-home', me: 'drv-home' };
  assert.deepEqual((await unansweredOwnerMessages(base)).map(m => m.message_id), ['o2', 'o3']);
  // A turn: what it was given, tool-call text first, the final words last; only a completed turn forwards.
  const said = [];
  const watch = turnWatcher((text, input) => said.push([text, input]));
  const assistant = text => ({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text }] } } });
  const given = text => ({ type: 'user/message', data: { message: { content: [{ type: 'text', text }] } } });
  for (const e of [{ type: 'turn/start' }, given('AItelier runtime event: {"driver_inbox":[{"message_id":"o3"}]}'), assistant('I will look first'),
    assistant('Both done: the ledger is set up.'), { type: 'turn/end', data: { reason: { kind: 'completed' } } }]) watch(e);
  for (const e of [{ type: 'turn/start' }, assistant('half a thought'), { type: 'turn/end', data: { reason: { kind: 'interrupted' } } }]) watch(e);
  assert.equal(said.length, 1);
  assert.equal(said[0][0], 'Both done: the ledger is set up.');
  // F3a: a turn that was not given the waiting owner message (a director's letter woke it) sends nothing.
  assert.deepEqual(await forwardFinalText({ ...base, text: 'about the director letter', input: 'Pending project inbox: d-1' }), { forwarded: false });
  assert.equal(sent.length, 0);
  const forwarded = await forwardFinalText({ ...base, text: said[0][0], input: said[0][1] });
  assert.deepEqual(forwarded, { forwarded: true, reply_to: 'o3', message_id: 'r1' });
  assert.deepEqual(sent[0], { request_key: 'auto-reply:o3', project_id: 'aitelier-home', subject: 'Re: ',
    body: 'Both done: the ledger is set up.', reply_to_message_id: 'o3' }, 'one key per owner message, whatever the text');
  // F3c: a second automatic reply to the same message (other words, after a lost answer) is refused, not sent twice.
  conflict = true;
  assert.deepEqual(await forwardFinalText({ ...base, text: 'other words', input: 'o3' }), { forwarded: false, already_replied: 'o3' });
  conflict = false;
  conversation.push(mine('s2', '2026-11-17T10:04:00Z', 'Both done', 'o3'));
  assert.deepEqual(await forwardFinalText({ ...base, text: 'more', input: 'o2 o3' }), { forwarded: false }, 'nobody waiting: nothing is sent');
  assert.equal(sent.length, 1);
  // The steward host's held work is exactly the unanswered owner messages (idle_dormant refuses on it).
  conversation.push(owner('o4', '2026-11-17T10:05:00Z', 'fourth'));
  const host = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward' }), { request });
  assert.deepEqual(await host.heldWork(), [{ node_key: 'owner message o4' }]);
  assert.equal(host.texts.heldWork, stewardTexts.heldWork);
  assert.equal(host.texts.tools.idle_dormant, stewardTexts.idleDormant);
  assert.notEqual(aitelierHost(env(), { request }).texts.heldWork, stewardTexts.heldWork, 'a director keeps its wording');
});

test('iss-4a503609 (a)(b): the steward\'s texts name only what it has; the director\'s are byte-for-byte today\'s', async () => {
  const directorTexts = await import('./aitelier-texts.mjs');
  const steward = aitelierHost(env({ AITELIER_DSH_ROLE: 'steward' })).texts, director = aitelierHost(env()).texts;
  // The director is unchanged: the module's own constants, byte for byte.
  assert.equal(director, directorTexts.texts);
  const effective = aitelierRoles(table);
  const presented = new Set(effective.steward.presented);
  const anyTool = new Set(['director', 'worker', 'steward'].flatMap(role => [...effective[role].presented, ...effective[role].forbidden])
    .concat(['state_read', 'list_user_questions', 'project_work', 'watch_process', 'todo_plan']));
  const lines = { continuationQuestion: steward.continuationQuestion, reviewInstruction: steward.reviewInstruction,
    askTheUser: steward.askTheUser, heldWork: steward.heldWork, ...Object.fromEntries(['idle_dormant', 'ask_owner', 'close_question']
      .map(name => ['tools.' + name, steward.tools[name]])) };
  for (const [key, text] of Object.entries(lines)) {
    assert.equal(typeof text, 'string', key);
    for (const phrase of ['ready work items', 'ready frontier nodes', 'open issues', 'worker capacity', 'watch_process', 'State claim',
      'list_user_questions', 'Director only', 'todo plan', 'live child'])
      assert.ok(!text.includes(phrase), `${key} names "${phrase}"`);
    const named = (text.match(/\b[a-z]+(?:_[a-z]+)+\b/g) ?? []).filter(word => anyTool.has(word));
    assert.deepEqual(named.filter(word => !presented.has(word)), [], `${key} names a tool the steward does not have`);
  }
  // It asks the steward for what it can do, and the table has to present every one of them:
  // `named` above is filtered by `anyTool`, which is itself built from that table, so a tool
  // the table loses drops out of the comparison and goes quiet instead of red (measured
  // 2026-11-18: deleting private_inbox_acknowledge from roles.json reds nothing without this).
  for (const tool of ['send_user_message', 'reply_to_director', 'portfolio_inbox', 'private_inbox',
    'portfolio_inbox_acknowledge', 'private_inbox_acknowledge', 'handoff', 'ask_owner']) {
    assert.ok(steward.continuationQuestion.includes(tool) || steward.askTheUser.includes(tool), tool);
    assert.ok(presented.has(tool), 'roles.json and the adapter present ' + tool);
  }
  assert.match(steward.tools.idle_dormant, /Nothing is expected of you until your owner or a director writes/);
  assert.notEqual(director.tools.idle_dormant, steward.tools.idle_dormant);
});

test('step 17 layer 2: open threads from data - the unanswered owner messages and the last 7 days of handoffs, answered or not', async () => {
  const stateDir = mkdtempSync(join(tmpdir(), 'steward-state-'));
  const conversation = [owner('o1', '2026-11-18T10:00:00Z', 'tell wuxia to pause'), mine('s1', '2026-11-18T10:01:00Z', 'ok', 'o1'),
    owner('o2', '2026-11-18T10:05:00Z', 'and the ledger?')];
  let replies = [];
  const request = async (path, body) => {
    if (path.endsWith('/list_user_conversation')) return { messages: conversation, next_cursor: null };
    if (path.endsWith('/send_director_message')) return { result: { message: { message_id: 'hm1' }, deliveries: [{ delivery_id: 'hd1' }] } };
    if (path.endsWith('/list_director_messages')) return { result: { items: replies, has_more: false } };
    throw new Error('unexpected ' + path);
  };
  const ops = stewardOps({ request, project: 'aitelier-home', me: 'drv-home', stateDir });
  assert.equal((await ops.handoff({ projectId: 'wuxia', text: 'pause please', citesUserMessageId: 'o1' })).handed_off, true);
  const { openThreads } = await import('./steward-host.mjs');
  const now = Date.parse('2026-11-18T12:00:00Z');
  let threads = await openThreads({ request, project: 'aitelier-home', me: 'drv-home', stateDir, now: Date.now() });
  assert.deepEqual(threads.unanswered_owner_messages.map(m => m.message_id), ['o2']);
  assert.deepEqual(threads.handoffs_last_7_days.map(h => [h.project_id, h.cites_user_message_id, h.answered]), [['wuxia', 'o1', false]]);
  replies = [{ message: { message_id: 'r1', reply_to_delivery_id: 'hd1', sender_project_id: 'wuxia' }, delivery: { delivery_id: 'x' } }];
  threads = await openThreads({ request, project: 'aitelier-home', me: 'drv-home', stateDir, now: Date.now() });
  assert.equal(threads.handoffs_last_7_days[0].answered, true, 'the project wrote back citing that handoff');
  // Older than 7 days: gone from the view.
  threads = await openThreads({ request, project: 'aitelier-home', me: 'drv-home', stateDir, now: Date.now() + 8 * 24 * 3600e3 });
  assert.deepEqual(threads.handoffs_last_7_days, []);
  void now;
});

test('open threads name the chat channel each unanswered owner message came from, and nothing for a dashboard one', async () => {
  const conversation = [owner('o1', '2026-11-20T12:48:00Z', '测试'), owner('o2', '2026-11-20T12:49:00Z', 'from wechat'),
    owner('o3', '2026-11-20T12:50:00Z', 'from the dashboard')];
  const asked = [];
  let sourcesFail = false;
  const request = async (path, body) => {
    if (path.endsWith('/list_user_conversation')) return { messages: conversation, next_cursor: null };
    if (path.endsWith('/message_sources')) {
      asked.push(body.message_ids);
      if (sourcesFail) throw new Error('state down');
      return { result: { sources: { o1: { direction: 'in', channel: 'slack', sender_ref: 'U0OWNER', channel_message_ref: 'ts-1' },
        o2: { direction: 'in', channel: 'wechat', sender_ref: 'wxid', channel_message_ref: 'wx-1' } } } };
    }
    throw new Error('unexpected ' + path);
  };
  const { openThreads } = await import('./steward-host.mjs');
  const stateDir = mkdtempSync(join(tmpdir(), 'steward-state-'));
  let threads = await openThreads({ request, project: 'aitelier-home', me: 'drv-home', stateDir });
  assert.deepEqual(asked, [['o1', 'o2', 'o3']], 'one sources read for the whole list');
  assert.deepEqual(threads.unanswered_owner_messages.map(m => m.source_channel), ['slack', 'wechat', undefined]);
  assert.equal('source_channel' in threads.unanswered_owner_messages[2], false, 'a dashboard message carries no source key');
  assert.doesNotMatch(JSON.stringify(threads), /U0OWNER|ts-1|wxid|wx-1/, 'only the channel name, never the account or message ref');
  sourcesFail = true;
  threads = await openThreads({ request, project: 'aitelier-home', me: 'drv-home', stateDir });
  assert.deepEqual(threads.unanswered_owner_messages.map(m => m.message_id), ['o1', 'o2', 'o3'], 'a failed sources read keeps the list');
});

test('review 340 (a): every tool the adapter adds to the steward\'s presented list is one runtime.mjs registers', () => {
  const effective = aitelierRoles(table);
  const added = effective.steward.presented.filter(name => !table.steward.presented.includes(name));
  const source = readFileSync(new URL('./runtime.mjs', import.meta.url), 'utf8');
  const registered = new Set([...source.matchAll(/defineTool\(\{ name: '([a-z_]+)'/g)].map(m => m[1]));
  assert.ok(added.length >= 5, JSON.stringify(added));
  assert.deepEqual(added.filter(name => !registered.has(name)), [], 'presented to the steward but never registered');
});

test('owner channel login requests (step 19): each pending request asks the channel once and completes with {requested} or a refusal code, never more', async () => {
  const { processChannelLoginRequests, CHANNEL_LOGIN_EVENTS } = await import('./channel-login-requests.mjs');
  assert.ok(CHANNEL_LOGIN_EVENTS.has('owner_channel_login_requested'));
  const completed = [], asked = [];
  const answers = [{ requested: true, qr: 'https://login.weixin.qq.com/l/SECRET', qrSvg: '<svg/>' },
    { refused: 'rate_limited', retry_after_s: 80, qr: 'SECRET' }, { refused: 'Not A Code <svg>' }];
  let failComplete = false;
  const request = async (path, body) => {
    if (path.endsWith('list_owner_channel_login_requests')) return { result: { requests: [
      { request_id: 'q1', channel: 'wechat' }, { request_id: 'q2', channel: 'wechat' }, { request_id: 'q3', channel: 'wechat' }] } };
    if (failComplete) throw new Error('HTTP 503');
    completed.push(body); return { result: { status: 'done' } };
  };
  const login = async channel => { asked.push(channel); return answers.shift(); };
  assert.deepEqual(await processChannelLoginRequests({ request, login }), { pending: 3, done: 3 });
  assert.deepEqual(asked, ['wechat', 'wechat', 'wechat']);
  assert.deepEqual(completed, [
    { request_key: 'channel-login:q1', request_id: 'q1', result: { requested: true } },
    { request_key: 'channel-login:q2', request_id: 'q2', result: { refused: 'rate_limited', retry_after_s: 80 } },
    { request_key: 'channel-login:q3', request_id: 'q3', result: { refused: 'refused' } }]);
  assert.ok(!JSON.stringify(completed).includes('SECRET') && !JSON.stringify(completed).includes('svg'));
  // A login that throws is an `unavailable` answer; a completion that fails is logged, not thrown.
  failComplete = true;
  const logged = [];
  const out = await processChannelLoginRequests({ request, login: async () => { throw new Error('boom'); }, log: line => logged.push(line) });
  assert.deepEqual(out, { pending: 3, done: 0 });
  assert.ok(logged.some(line => /q1: boom/.test(line)) && logged.some(line => /q1: result not written/.test(line)));
});
