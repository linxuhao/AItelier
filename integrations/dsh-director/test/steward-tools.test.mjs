/** Plugin steps 12-13: the steward's portfolio (including its own inbox page), handoff and
 *  proposal tools - each exists only when the host provides it, and each passes the call to
 *  the host unchanged. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PORTFOLIO_TOOLS, registerPortfolioTools } from '../lib/portfolio.mjs';
import { PROPOSAL_KINDS, registerProposalTools } from '../lib/proposals.mjs';
import { registerChannelTools } from '../lib/channel-tools.mjs';
import { readFileSync } from 'node:fs';

const collect = (register, options) => {
  const tools = new Map();
  register({ tools: { register: t => tools.set(t.name, t) } }, { defineTool: t => t, jsonOutput: {}, ...options });
  return tools;
};
const exec = { signal: AbortSignal.timeout(5000) };

test('portfolio, the inbox reader and handoff: registered for what the host provides, the call passed through', async () => {
  const calls = [];
  const portfolio = { projects: async () => (calls.push(['projects']), 'P'), project: async (id, _signal, page) => (calls.push(['project', id, page]), 'O'),
    conversation: async (id, cursor) => (calls.push(['conversation', id, cursor]), 'C'),
    inbox: async page => (calls.push(['inbox', page]), 'I') };
  const handoff = async h => (calls.push(['handoff', h]), 'H');
  assert.deepEqual([...collect(registerPortfolioTools, { portfolio }).keys()], PORTFOLIO_TOOLS);
  // The inbox reader is the one portfolio tool a host may leave out; the rest stay.
  const withoutInbox = { ...portfolio };
  delete withoutInbox.inbox;
  assert.deepEqual([...collect(registerPortfolioTools, { portfolio: withoutInbox }).keys()],
    PORTFOLIO_TOOLS.filter(name => name !== 'portfolio_inbox'));
  assert.deepEqual([...collect(registerPortfolioTools, { handoff }).keys()], ['handoff']);
  const tools = collect(registerPortfolioTools, { portfolio, handoff });
  assert.equal(await tools.get('portfolio_projects').execute({}, exec), 'P');
  assert.equal(await tools.get('portfolio_project').execute({ project_id: 'w' }, exec), 'O');
  // The page cursors are passed to the host as given (review 360: nothing witnessed it).
  assert.equal(await tools.get('portfolio_project').execute({ project_id: 'w', open_after: 'k', issues_after: 9 }, exec), 'O');
  assert.equal(await tools.get('portfolio_conversation').execute({ project_id: 'w', cursor: 'c' }, exec), 'C');
  assert.equal(await tools.get('portfolio_inbox').execute({ after: 2, limit: 3 }, exec), 'I');
  assert.equal(await tools.get('handoff').execute({ project_id: 'w', text: 't', cites_user_message_id: 'm' }, exec), 'H');
  assert.deepEqual(calls, [['projects'], ['project', 'w', { openAfter: undefined, issuesAfter: undefined }],
    ['project', 'w', { openAfter: 'k', issuesAfter: 9 }], ['conversation', 'w', 'c'], ['inbox', { after: 2, limit: 3 }],
    ['handoff', { projectId: 'w', text: 't', citesUserMessageId: 'm' }]]);
  // Its bounds are in the description, where the model reads them, not only in the answer.
  const description = tools.get('portfolio_inbox').description;
  for (const bound of ['5 deliveries', '8000 characters', '24000 characters']) assert.ok(description.includes(bound), bound);
  assert.match(description, /Read-only/);
});

test('the private inbox reader: registered only for a host that provides it, its bounds in the description, the call passed through', async () => {
  const calls = [];
  const portfolio = { privateInbox: async page => (calls.push(['privateInbox', page]), 'PI') };
  // A host whose portfolio does not read the private inbox gets no such tool - and saying
  // so is not an error, whatever else the portfolio does provide.
  const without = collect(registerPortfolioTools, { portfolio: {} });
  assert.equal(without.has('private_inbox'), false);
  const partial = collect(registerPortfolioTools, { portfolio: { inbox: async () => 'I' } });
  assert.equal(partial.has('private_inbox'), false, 'the project reader does not imply the private one');
  assert.ok(partial.has('portfolio_inbox'));
  assert.doesNotThrow(() => collect(registerPortfolioTools, { portfolio }));
  const tools = collect(registerPortfolioTools, { portfolio });
  assert.deepEqual([...tools.keys()].filter(name => name !== 'private_inbox'),
    PORTFOLIO_TOOLS.filter(name => name !== 'portfolio_inbox'), 'the project reads are the rest of it, nothing invented');
  // The documented parameters: two optional integers.
  const tool = tools.get('private_inbox');
  assert.deepEqual(Object.keys(tool.parameters), ['after', 'limit']);
  for (const name of ['after', 'limit']) {
    assert.equal(tool.parameters[name].type, 'integer', name);
    assert.equal(tool.parameters[name].required, undefined, `${name} is optional: the host states the bounds`);
  }
  assert.equal(await tool.execute({ after: 5, limit: 2 }, exec), 'PI');
  assert.equal(await tool.execute({}, exec), 'PI');
  assert.deepEqual(calls, [['privateInbox', { after: 5, limit: 2 }], ['privateInbox', { after: undefined, limit: undefined }]],
    'the call reaches the host unchanged');
  // Its bounds are in the description, where the model reads them, not only in the answer.
  const description = tool.description;
  for (const bound of ['5 deliveries', '8000 characters', '24000 characters']) assert.ok(description.includes(bound), bound);
  assert.match(description, /Read-only/);
});

test('proposals: propose passes kind, payload, reason (and supersedes); an unknown kind never reaches the host', async () => {
  const calls = [];
  const proposals = { propose: async p => (calls.push(['propose', p]), { proposal_id: 'p-1' }), withdraw: async w => (calls.push(['withdraw', w]), { withdrawn: true }) };
  const tools = collect(registerProposalTools, { proposals });
  assert.deepEqual([...tools.keys()], ['propose', 'withdraw_proposal']);
  // Owner 2026-11-18: archive / unarchive are proposals too (the owner approves, the backend executes).
  assert.deepEqual(PROPOSAL_KINDS, ['create_project', 'start_driver', 'stop_driver', 'archive_project', 'unarchive_project']);
  const description = tools.get('propose').description;
  for (const kind of ['archive_project {project_id, reason?}', 'unarchive_project {project_id, reason?}']) assert.ok(description.includes(kind), kind);
  assert.deepEqual(await tools.get('propose').execute({ kind: 'deploy', payload: {}, reason: 'r' }, exec), { refused: 'unknown_kind', kinds: PROPOSAL_KINDS });
  assert.equal(calls.length, 0);
  await tools.get('propose').execute({ kind: 'archive_project', payload: { project_id: 'w', reason: 'done' }, reason: 'r' }, exec);
  await tools.get('propose').execute({ kind: 'stop_driver', payload: { project_id: 'w' }, reason: 'r' }, exec);
  await tools.get('propose').execute({ kind: 'stop_driver', payload: { project_id: 'w' }, reason: 'r2', supersedes: 'p-1' }, exec);
  await tools.get('withdraw_proposal').execute({ id: 'p-1', reason: 'gone' }, exec);
  assert.deepEqual(calls, [['propose', { kind: 'archive_project', payload: { project_id: 'w', reason: 'done' }, reason: 'r' }],
    ['propose', { kind: 'stop_driver', payload: { project_id: 'w' }, reason: 'r' }],
    ['propose', { kind: 'stop_driver', payload: { project_id: 'w' }, reason: 'r2', supersedes: 'p-1' }],
    ['withdraw', { id: 'p-1', reason: 'gone' }]]);
});

test('proposals, step 15: a host that names its kinds (the local host: create_project) is offered only those', async () => {
  const calls = [];
  const proposals = { kinds: ['create_project'], propose: async p => (calls.push(p), { proposal_id: 'p-1' }), withdraw: async () => ({}) };
  const propose = collect(registerProposalTools, { proposals }).get('propose');
  assert.deepEqual(propose.parameters.kind.enum, ['create_project']);
  assert.match(propose.description, /kind is one of create_project\. payload: create_project \{project_id, title, goal, seed_nodes/);
  assert.doesNotMatch(propose.description, /start_driver|stop_driver|archive_project/);
  assert.deepEqual(await propose.execute({ kind: 'start_driver', payload: { project_id: 'w' }, reason: 'r' }, exec), { refused: 'unknown_kind', kinds: ['create_project'] });
  assert.equal(calls.length, 0);
  await propose.execute({ kind: 'create_project', payload: { project_id: 'w', title: 't', goal: 'g' }, reason: 'r' }, exec);
  assert.equal(calls.length, 1);
});

test('request_wechat_qr asks the WeChat channel for a login and answers only {requested} or {refused, retry_after_s}: never a QR', async () => {
  const asked = [];
  let answer;
  const tools = collect(registerChannelTools, { login: async channel => { asked.push(channel); return answer; } });
  assert.deepEqual([...tools.keys()], ['request_wechat_qr']);
  const tool = tools.get('request_wechat_qr');
  assert.deepEqual(tool.parameters, {});
  answer = { requested: true, qr: 'https://login.weixin.qq.com/l/SECRET', qrSvg: '<svg/>', qr_svg: '<svg/>' };
  assert.deepEqual(await tool.execute({}, exec), { requested: true });
  answer = { refused: 'rate_limited', retry_after_s: 75, qr: 'SECRET' };
  assert.deepEqual(await tool.execute({}, exec), { refused: 'rate_limited', retry_after_s: 75 });
  answer = { refused: 'qr_waiting', qr_svg: '<svg/>' };
  assert.deepEqual(await tool.execute({}, exec), { refused: 'qr_waiting' });
  answer = undefined;
  assert.deepEqual(await tool.execute({}, exec), { refused: 'refused' });
  assert.deepEqual(asked, ['wechat', 'wechat', 'wechat', 'wechat']);
  // The model is told where the QR is (the dashboard) and that it never goes over chat.
  assert.match(tool.description, /ONLY on the dashboard's Channels tab/);
  assert.match(tool.description, /never send, describe or promise a QR over chat/);
  // The steward is presented it (roles.json), and its prompt says the same.
  const roles = JSON.parse(readFileSync(new URL('../roles.json', import.meta.url), 'utf8'));
  assert.ok(roles.steward.presented.includes('request_wechat_qr'));
  assert.ok(!roles.director.presented.includes('request_wechat_qr') && !roles.worker.presented.includes('request_wechat_qr'));
  assert.match(readFileSync(new URL('../steward.md', import.meta.url), 'utf8'), /request_wechat_qr[\s\S]*only on the dashboard's Channels tab/);
});
