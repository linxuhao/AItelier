/** Step 23 (design-steward 7.7.6, 7.7.15): the adapter's side of the family group - in a group
 *  turn every State request a TOOL makes carries X-AItelier-Read-As (and the host's own never
 *  does), the public portfolio branch reads only what an anonymous reader may, and the group
 *  operations map onto the step 23 commands. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { stewardOps, groupOps, publicProjectSummary, listWhoShown, boundGroupsSection } from './steward-host.mjs';
import { readFileSync } from 'node:fs';

function recorder(answers = {}) {
  const calls = [];
  const request = async (path, body, _signal, options) => {
    calls.push({ path, body, readAs: options?.readAs });
    const answer = answers[path];
    if (answer instanceof Error) throw answer;
    return typeof answer === 'function' ? answer(body) : answer ?? { result: {} };
  };
  return { calls, request };
}

test('a group turn\'s portfolio reads only the public catalog and overview: no drivers, no questions, no claims', async () => {
  const overview = { project: { project_id: 'wuxia-myth' }, counts: {}, nodes: [{ node_key: 'a', title: 't', status: 'READY',
    claims: [{ driver_id: 'drv-1' }], latest_evidence: { x: 1 }, hold: { held: true, reason: 'internal words' } }] };
  const { calls, request } = recorder({
    '/api/state/query/project_catalog': { result: { projects: [{ project_id: 'wuxia-myth', title: '武虾', node_count: 9, verified_count: 3, source: { repo_path: '/x' } },
      { project_id: 'aitelier-home', title: 'home' }] } },
    '/api/state/query/project_overview': body => body.project_id === 'wuxia-myth' ? { result: overview }
      : Promise.reject(Object.assign(new Error('AItelier: HTTP 403 "This State DAG record is not available."'), { status: 403 })),
    '/api/state/query/list_issues': { result: { issues: [] } } });
  const ops = stewardOps({ request, project: 'aitelier-home', me: 'drv-home', groupTurn: () => true });
  assert.deepEqual(await ops.portfolio.projects(), { projects: [{ project_id: 'wuxia-myth', title: '武虾', node_count: 9, verified_count: 3 }] });
  const shown = await ops.portfolio.project('wuxia-myth');
  const line = shown.overview.open_work[0];
  assert.ok(!('live_claims' in line) && !('latest_evidence' in line) && !('held_reason' in line), JSON.stringify(line));
  assert.ok(!('open_questions' in shown));
  assert.deepEqual(await ops.portfolio.project('aitelier'), { refused: 'not_found' });
  assert.ok(!calls.some(c => c.path === '/api/dsh-drivers' || c.path.includes('list_user_questions')), JSON.stringify(calls.map(c => c.path)));
  assert.deepEqual(publicProjectSummary({ open_work: [{ node_key: 'a', live_claims: [], held_reason: 'r' }] }), { open_work: [{ node_key: 'a' }] });
});

test('the group operations are the host\'s own requests; only the family memory pair goes through the tools\' request', async () => {
  const host = recorder({ '/api/state/commands/relay_group_message': { result: { message_id: 'g1', sender_role: 'member' } },
    '/api/state/commands/bind_owner_group': Object.assign(new Error('AItelier: HTTP 422 "channel_sender_not_bound"'), { status: 422 }) });
  const tool = recorder();
  const ops = groupOps({ request: host.request, toolRequest: (p, b, s) => tool.request(p, b, s, { readAs: 'public' }), project: 'aitelier-home', me: 'drv' });
  assert.deepEqual(await ops.relay({ channel: 'wechat', groupRef: 'r1', senderRef: 'm', messageRef: 'x', text: 'hi', at: 't' }),
    { message_id: 'g1', sender_role: 'member' });
  assert.deepEqual(await ops.bind({ channel: 'wechat', groupRef: 'r1', senderRef: 'm', messageRef: 'x', prefix: '@管家', code: 'G-1' }),
    { refused: 'channel_sender_not_bound' });
  await ops.reply({ questionId: 'g1', alsoAnswers: ['g0'], body: 'answer', turn: '3' });
  await ops.audience({ turn: '3', audience: 'group', group: { channel: 'wechat', group_ref: 'r1' }, senderRefs: ['m'], questionIds: ['g0', 'g1'] });
  await ops.familyNote('周六去外婆家', undefined, { turn: '3' });
  await ops.familyShow(undefined, { turn: '3' });
  assert.ok(host.calls.every(c => c.readAs === undefined));
  assert.deepEqual(host.calls.map(c => c.path.split('/').at(-1)), ['relay_group_message', 'bind_owner_group', 'post_group_reply', 'set_steward_turn_audience']);
  // Step 23.1 (7.7.7): the reply names its turn and the audience the questions that turn took.
  assert.deepEqual(host.calls[2].body, { request_key: 'group-reply:g1', question_id: 'g1', body: 'answer', turn: '3', also_answers: ['g0'] });
  assert.deepEqual(host.calls[3].body, { turn: '3', audience: 'group', group: { channel: 'wechat', group_ref: 'r1' }, sender_refs: ['m'],
    question_ids: ['g0', 'g1'] });
  assert.deepEqual(tool.calls.map(c => [c.path.split('/').at(-1), c.readAs]), [['note_family_memory', 'public'], ['list_family_memory', 'public']]);
  // Both name the turn they run in (director 477 N1: the backend wants the recorded one).
  assert.deepEqual(tool.calls.map(c => c.body.turn), ['3', '3']);
});

test('step 25 C4: the four list operations go through the tools\' request, naming the turn they run in', async () => {
  const host = recorder();
  const tool = recorder();
  const ops = groupOps({ request: host.request, toolRequest: (p, b, s) => tool.request(p, b, s, { readAs: 'public' }), project: 'aitelier-home', me: 'drv' });
  await ops.listShow({ list: 'shopping' }, undefined, { turn: '7' });
  await ops.listAdd({ list: 'shopping', items: ['牛奶', '鸡蛋'] }, undefined, { turn: '7' });
  await ops.listCheck({ itemIds: ['li-1'], done: true }, undefined, { turn: '7' });
  await ops.listRemove({ itemIds: ['li-2'] }, undefined, { turn: '8' });
  assert.deepEqual(host.calls, []);
  assert.deepEqual(tool.calls.map(c => [c.path.split('/').at(-1), c.readAs, c.body.turn]), [
    ['list_list_items', 'public', '7'], ['add_list_items', 'public', '7'], ['check_list_items', 'public', '7'],
    ['remove_list_items', 'public', undefined]]);
  assert.deepEqual({ ...tool.calls[1].body, request_key: undefined }, { request_key: undefined, list: 'shopping', items: ['牛奶', '鸡蛋'], turn: '7' });
  assert.deepEqual({ ...tool.calls[2].body, request_key: undefined }, { request_key: undefined, item_ids: ['li-1'], done: true, turn: '7' });
  // Each call its own key: the same item can be added again after it was removed.
  await ops.listAdd({ list: 'shopping', items: ['牛奶', '鸡蛋'] }, undefined, { turn: '7' });
  assert.notEqual(tool.calls[1].body.request_key, tool.calls.at(-1).body.request_key);
  // A refusal is an answer the model reads, never a thrown tool error.
  const refusing = groupOps({ request: host.request, toolRequest: async () => { throw new Error('AItelier: HTTP 422 "withheld: private_project"'); } });
  assert.equal((await refusing.listAdd({ list: 'todo', items: ['x'] }, undefined, { turn: '1' })).refused, 'state_refused');
});

test('director 489 (a): an item of an unbound group reads to the model as 曾由一位成员添加 / 勾掉, never blank, never a throw', async () => {
  const gone = { item_id: 'li-1', text: '牛奶', added_by: null, added_by_role: 'member', done: true, done_by: null };
  const open = { item_id: 'li-2', text: '鸡蛋', added_by: null, added_by_role: 'member', done: false, done_by: null };
  const named = { item_id: 'li-3', text: '面包', added_by: '妈妈', added_by_role: 'member', done: true, done_by: '业主' };
  const tool = recorder({
    '/api/state/query/list_list_items': { result: { lists: { shopping: [gone, open, named], todo: [] } } },
    '/api/state/commands/add_list_items': { result: { items: [{ ...open, added: false }] } },
    '/api/state/commands/check_list_items': { result: { items: [gone], unknown: ['li-x'] } } });
  const ops = groupOps({ request: recorder().request, toolRequest: tool.request });
  const shown = await ops.listShow({}, undefined, { turn: '1' });
  assert.deepEqual(shown.lists.shopping.map(i => [i.text, i.added_by, i.done_by]), [
    ['牛奶', '曾由一位成员添加', '曾由一位成员勾掉'], ['鸡蛋', '曾由一位成员添加', null], ['面包', '妈妈', '业主']]);
  assert.deepEqual(shown.lists.todo, []);
  assert.equal((await ops.listAdd({ list: 'shopping', items: ['鸡蛋'] }, undefined, { turn: '1' })).items[0].added_by, '曾由一位成员添加');
  const checked = await ops.listCheck({ itemIds: ['li-1', 'li-x'] }, undefined, { turn: '1' });
  assert.deepEqual([checked.items[0].added_by, checked.items[0].done_by, checked.unknown], ['曾由一位成员添加', '曾由一位成员勾掉', ['li-x']]);
  // Odd answers pass through untouched.
  assert.deepEqual([listWhoShown(undefined), listWhoShown({ refused: 'withheld' }), listWhoShown({ items: null })],
    [undefined, { refused: 'withheld' }, { items: null }]);
});

test('director 505: checked items past the bound read to the model as 另有 N 条已勾掉的未显示, never a silent cut', async () => {
  const done = n => Array.from({ length: n }, (_, i) => ({ item_id: `li-${i}`, text: `x${i}`, added_by: '妈妈', done: true, done_by: '业主' }));
  const tool = recorder({ '/api/state/query/list_list_items': { result: { lists: { shopping: done(100), todo: done(3) },
    done_total: { shopping: 150, todo: 3 }, done_more: { shopping: 50, todo: 0 } } } });
  const shown = await groupOps({ request: recorder().request, toolRequest: tool.request }).listShow({}, undefined, { turn: '1' });
  assert.equal(shown.lists.shopping.length, 100);
  assert.deepEqual([shown.done_total, shown.done_more], [{ shopping: 150, todo: 3 }, { shopping: 50, todo: 0 }]);
  assert.deepEqual(shown.done_not_shown, { shopping: '另有 50 条已勾掉的未显示' });
  // Everything shown: no marker.
  assert.equal('done_not_shown' in listWhoShown({ lists: { todo: done(3) }, done_total: { todo: 3 }, done_more: { todo: 0 } }), false);
});

test('the runtime: in a group turn every tool request carries the header, the host\'s requests never; in an owner turn none', async () => {
  const { aitelierHost } = await import('./runtime.mjs');
  const { calls, request } = recorder({ '/api/state/query/project_catalog': { result: { projects: [] } },
    '/api/state/query/list_projects': { result: [] }, '/api/dsh-drivers': { projects: {} },
    '/api/state/query/list_owner_groups': { result: { groups: [] } },
    '/api/state/query/list_user_questions': { result: { questions: [{ segment_id: 'q1', message_id: 'm1', text: 'may I?' }] } } });
  const host = aitelierHost({ AITELIER_DSH_ROLE: 'steward', AITELIER_DSH_PROJECT: 'aitelier-home', AITELIER_DRIVER_ID: 'drv',
    AITELIER_DSH_SESSION: 's', AITELIER_DSH_PROVIDER: 'p', AITELIER_DSH_MODEL: 'm', AITELIER_DSH_STATE_DIR: '/tmp', AITELIER_DSH_ENTRY: '/x' },
  { request });
  assert.ok(host.capabilities.includes('groups'));
  // Before install the audience is unknown: a tool's request reads as anonymous.
  await host.portfolio.projects();
  await host.groups.list();
  assert.deepEqual(calls.map(c => [c.path, c.readAs]), [['/api/state/query/project_catalog', 'public'], ['/api/state/query/list_owner_groups', undefined]]);
  // ask_owner and close_question are tools too (7.7.5 second layer): a guard slip still
  // reaches State as an anonymous reader, refused public_view_read_only.
  calls.length = 0;
  await host.attention.ask({ id: 'q1', text: 'may I?' });
  await host.attention.close({ id: 'q1', reason: 'done' });
  await host.attention.list();
  assert.deepEqual(calls.map(c => c.path.split('/').at(-1)), ['send_user_message', 'list_user_questions', 'withdraw_user_question', 'list_user_questions']);
  assert.deepEqual(calls.filter(c => c.readAs !== 'public').map(c => c.path), []);
});

test('after a group turn the adapter\'s own writes wait for the owner too; the guard refuses its tools in a group turn', async () => {
  const { GroupTurns, groupTurnRefusal, pausedRefusal } = await import('../dsh-director/lib/group-turns.mjs');
  const { GROUP_LIMITS } = await import('../dsh-director/lib/channels.mjs');
  const { mkdtempSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const ops = groupOps({ request: async () => ({}) });
  assert.deepEqual(ops.pausedTools, ['schedule_set', 'reply_to_director']);
  const turns = new GroupTurns({ stateDir: mkdtempSync(join(tmpdir(), 'adapter-pause-')), limits: GROUP_LIMITS, pausedTools: ops.pausedTools });
  const msg = kind => ({ id: kind, role: 'user', content: [], source: kind === 'g' ? { kind: 'dsh-director-group', channel: 'wechat', group_ref: 'r1', question_ids: ['q'] } : { kind: 'e' } });
  turns.split([msg('g')], 1);
  for (const name of ['send_user_message', 'reply_to_director', 'schedule_list', 'schedule_set', 'schedule_delete', 'relay_answer'])
    assert.equal(turns.guard(name), groupTurnRefusal, name);
  turns.pause();
  turns.split([msg('e')], 1);
  assert.equal(turns.guard('schedule_set'), pausedRefusal);
  assert.equal(turns.guard('reply_to_director'), pausedRefusal);
  assert.equal(turns.guard('send_user_message'), undefined);
});

test('step 23.1 (7.9.4, 7.9.5): an owner turn\'s list operations name the group; a member\'s reminder goes through the tools\' request with its turn', async () => {
  const host = recorder();
  const tool = recorder({ '/api/state/commands/create_group_reminder': { result: { schedule_id: 'sch-1', created_via: 'group' } } });
  const ops = groupOps({ request: host.request, toolRequest: (p, b, s) => tool.request(p, b, s, { readAs: 'public' }), project: 'aitelier-home', me: 'drv' });
  await ops.listShow({ group: '老家群' });
  await ops.listAdd({ list: 'todo', items: ['交水费'], group: '老家群' });
  await ops.listCheck({ itemIds: ['li-1'], group: '老家群' });
  await ops.listRemove({ itemIds: ['li-1'], group: '老家群' });
  assert.deepEqual(tool.calls.map(c => c.body.group), ['老家群', '老家群', '老家群', '老家群']);
  // Without a group nothing names one (the backend picks the one bound group, or group_required).
  await ops.listShow({});
  assert.equal('group' in tool.calls.at(-1).body, false);
  const made = await ops.reminderSet({ at: '2027-01-02T07:00', text: '明早带伞' }, undefined, { turn: '9' });
  assert.deepEqual(made, { schedule_id: 'sch-1', created_via: 'group' });
  const call = tool.calls.at(-1);
  assert.deepEqual([call.path.split('/').at(-1), call.readAs, call.body.turn, call.body.at, call.body.text],
    ['create_group_reminder', 'public', '9', '2027-01-02T07:00', '明早带伞']);
  assert.match(call.body.request_key, /^group-reminder:9:/);
  assert.deepEqual(host.calls, []);
});

test('step 23.1 (7.8.9 step 5): run_confirmed\'s read names the turn it runs in', async () => {
  const { confirmOps } = await import('./steward-host.mjs');
  const { calls, request } = recorder({ '/api/state/commands/get_steward_confirmation': { result: { status: 'confirmed', group: '家庭群', confirmed_by: '妈妈' } } });
  const got = await confirmOps(request, { project: 'aitelier-home' }).get('K7Q2A', undefined, { turn: '12' });
  assert.deepEqual(calls.map(c => c.body), [{ code: 'K7Q2A', turn: '12' }]);
  assert.equal(got.group, '家庭群');
  // Director 602 defect 8: no open turn is sent as none (the backend's read-only read), never as ''.
  await confirmOps(request, { project: 'aitelier-home' }).get('K7Q2A', undefined, {});
  await confirmOps(request, { project: 'aitelier-home' }).get('K7Q2A');
  assert.deepEqual(calls.slice(1).map(c => c.body), [{ code: 'K7Q2A' }, { code: 'K7Q2A' }]);
});

test('director 602 defect 4: the snapshot says 群名暂不可用 when the group names cannot be read; schedule_set does not lean on it', async () => {
  const names = await boundGroupsSection(recorder({ '/api/state/query/list_owner_groups': { result: { groups: [{ label: '家庭群' }, { label: '老家群' }] } } }).request);
  assert.equal(names, '\n\nYour owner\'s bound family groups (by name):\n["家庭群","老家群"]');
  assert.equal(await boundGroupsSection(recorder({ '/api/state/query/list_owner_groups': { result: { groups: [] } } }).request), '');
  const failed = await boundGroupsSection(recorder({ '/api/state/query/list_owner_groups': new Error('AItelier: HTTP 503') }).request);
  assert.match(failed, /bound family groups \(by name\): 群名暂不可用/);
  // The snapshot is built from it, and schedule_set's description sends the model to the refusal, not the snapshot.
  const runtime = readFileSync(new URL('./runtime.mjs', import.meta.url), 'utf8');
  assert.ok(runtime.includes('await boundGroupsSection(request)'));
  const description = runtime.match(/name: 'schedule_set',\s*description: '((?:[^'\\]|\\.)*)'/)?.[1] ?? '';
  assert.ok(description.includes('a group_required refusal names every bound group') && !description.includes('snapshot'), description);
});


test('design/steward-group-media.md: relay carries kept only when true; the notice, the purge receipt and the expiry are the host\'s own requests', async () => {
  const host = recorder({ '/api/state/commands/record_group_media_purge':
    Object.assign(new Error('AItelier: HTTP 422 "purge_incomplete: kept rows are left; purge again"'), { status: 422 }) });
  const ops = groupOps({ request: host.request, project: 'aitelier-home', me: 'drv' });
  await ops.relay({ channel: 'wechat', groupRef: 'r1', senderRef: 'm', messageRef: 'x', text: '[图片]', kept: true,
    attachments: [{ kind: 'image', ref: 'm_0123456789abcdef' }] });
  await ops.relay({ channel: 'wechat', groupRef: 'r1', senderRef: 'm', messageRef: 'y', text: 'hi' });
  assert.equal(host.calls[0].body.kept, true);
  assert.ok(!('kept' in host.calls[1].body), 'a mention\'s relay is the request it always was');
  await ops.mediaNotice({ channel: 'wechat', groupRef: 'r1', epoch: 'E1', notice: 'on', phase: 'check', channelDown: true });
  await ops.mediaNotice({ channel: 'wechat', groupRef: 'r1', epoch: 'E1', notice: 'on', phase: 'check' });
  await ops.mediaNotice({ channel: 'wechat', groupRef: 'r1', epoch: 'E1', notice: 'on', phase: 'sent' });
  await ops.mediaNotice({ channel: 'wechat', groupRef: 'r1', epoch: 'E1', notice: 'on', phase: 'failed', attempts: 2 });
  const [down, check, sent, failed] = host.calls.slice(2, 6).map(c => c.body);
  assert.match(down.request_key, /^group-media-notice-check:/);
  assert.notEqual(down.request_key, check.request_key, 'a check is a dry run: a fresh key each time');
  assert.deepEqual({ ...down, request_key: undefined }, { request_key: undefined, channel: 'wechat', group_ref: 'r1', epoch: 'E1',
    notice: 'on', phase: 'check', channel_down: true });
  assert.ok(!('channel_down' in check));
  assert.match(sent.request_key, /^group-media-notice:wechat:[0-9a-f]{16}:E1:sent$/, 'one commit key per epoch: a retry is State\'s replay');
  assert.match(failed.request_key, /^group-media-notice:wechat:[0-9a-f]{16}:E1:failed:2$/);
  assert.ok(!sent.request_key.includes('r1'), 'the room is digested in the key');
  const receipt = { rows_before: 2, rows_after: 0, files_unlinked: 1, files_shared: 1, bytes_freed: 33 };
  assert.deepEqual(await ops.recordPurge({ channel: 'wechat', groupRef: 'r1', epoch: 'E2', receipt }), { refused: 'purge_incomplete' });
  assert.deepEqual(host.calls[6].body.receipt, receipt);
  assert.match(host.calls[6].body.request_key, /^group-media-purge:wechat:[0-9a-f]{16}:E2:[0-9a-f]{16}$/);
  await ops.expireMedia();
  assert.deepEqual(host.calls.map(c => c.path.split('/').at(-1)), ['relay_group_message', 'relay_group_message', 'group_media_notice',
    'group_media_notice', 'group_media_notice', 'group_media_notice', 'record_group_media_purge', 'expire_group_media']);
  assert.deepEqual(host.calls.at(-1).body, {});
  assert.ok(host.calls.every(c => c.readAs === undefined), 'never with the public-read header');
});
