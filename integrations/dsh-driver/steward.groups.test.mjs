/** Step 23 (design-steward 7.7.6, 7.7.15): the adapter's side of the family group - in a group
 *  turn every State request a TOOL makes carries X-AItelier-Read-As (and the host's own never
 *  does), the public portfolio branch reads only what an anonymous reader may, and the group
 *  operations map onto the step 23 commands. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { stewardOps, groupOps, publicProjectSummary } from './steward-host.mjs';

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
  await ops.reply({ questionId: 'g1', alsoAnswers: ['g0'], body: 'answer' });
  await ops.audience({ turn: '3', audience: 'group', group: { channel: 'wechat', group_ref: 'r1' }, senderRefs: ['m'] });
  await ops.familyNote('周六去外婆家');
  await ops.familyShow();
  assert.ok(host.calls.every(c => c.readAs === undefined));
  assert.deepEqual(host.calls.map(c => c.path.split('/').at(-1)), ['relay_group_message', 'bind_owner_group', 'post_group_reply', 'set_steward_turn_audience']);
  assert.deepEqual(host.calls[2].body, { request_key: 'group-reply:g1', question_id: 'g1', body: 'answer', also_answers: ['g0'] });
  assert.deepEqual(tool.calls.map(c => [c.path.split('/').at(-1), c.readAs]), [['note_family_memory', 'public'], ['list_family_memory', 'public']]);
});

test('the runtime: in a group turn every tool request carries the header, the host\'s requests never; in an owner turn none', async () => {
  const { aitelierHost } = await import('./runtime.mjs');
  const { calls, request } = recorder({ '/api/state/query/project_catalog': { result: { projects: [] } },
    '/api/state/query/list_projects': { result: [] }, '/api/dsh-drivers': { projects: {} },
    '/api/state/query/list_owner_groups': { result: { groups: [] } } });
  const host = aitelierHost({ AITELIER_DSH_ROLE: 'steward', AITELIER_DSH_PROJECT: 'aitelier-home', AITELIER_DRIVER_ID: 'drv',
    AITELIER_DSH_SESSION: 's', AITELIER_DSH_PROVIDER: 'p', AITELIER_DSH_MODEL: 'm', AITELIER_DSH_STATE_DIR: '/tmp', AITELIER_DSH_ENTRY: '/x' },
  { request });
  assert.ok(host.capabilities.includes('groups'));
  // Before install the audience is unknown: a tool's request reads as anonymous.
  await host.portfolio.projects();
  await host.groups.list();
  assert.deepEqual(calls.map(c => [c.path, c.readAs]), [['/api/state/query/project_catalog', 'public'], ['/api/state/query/list_owner_groups', undefined]]);
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
