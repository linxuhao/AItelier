/** Plugin steps 12-13: the steward's portfolio, handoff and proposal tools - each exists
 *  only when the host provides it, and each passes the call to the host unchanged. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PORTFOLIO_TOOLS, registerPortfolioTools } from '../lib/portfolio.mjs';
import { PROPOSAL_KINDS, registerProposalTools } from '../lib/proposals.mjs';

const collect = (register, options) => {
  const tools = new Map();
  register({ tools: { register: t => tools.set(t.name, t) } }, { defineTool: t => t, jsonOutput: {}, ...options });
  return tools;
};
const exec = { signal: AbortSignal.timeout(5000) };

test('portfolio and handoff: registered for what the host provides, the call passed through', async () => {
  const calls = [];
  const portfolio = { projects: async () => (calls.push(['projects']), 'P'), project: async id => (calls.push(['project', id]), 'O'),
    conversation: async (id, cursor) => (calls.push(['conversation', id, cursor]), 'C') };
  const handoff = async h => (calls.push(['handoff', h]), 'H');
  assert.deepEqual([...collect(registerPortfolioTools, { portfolio }).keys()], PORTFOLIO_TOOLS);
  assert.deepEqual([...collect(registerPortfolioTools, { handoff }).keys()], ['handoff']);
  const tools = collect(registerPortfolioTools, { portfolio, handoff });
  assert.equal(await tools.get('portfolio_projects').execute({}, exec), 'P');
  assert.equal(await tools.get('portfolio_project').execute({ project_id: 'w' }, exec), 'O');
  assert.equal(await tools.get('portfolio_conversation').execute({ project_id: 'w', cursor: 'c' }, exec), 'C');
  assert.equal(await tools.get('handoff').execute({ project_id: 'w', text: 't', cites_user_message_id: 'm' }, exec), 'H');
  assert.deepEqual(calls, [['projects'], ['project', 'w'], ['conversation', 'w', 'c'],
    ['handoff', { projectId: 'w', text: 't', citesUserMessageId: 'm' }]]);
});

test('proposals: propose passes kind, payload, reason (and supersedes); an unknown kind never reaches the host', async () => {
  const calls = [];
  const proposals = { propose: async p => (calls.push(['propose', p]), { proposal_id: 'p-1' }), withdraw: async w => (calls.push(['withdraw', w]), { withdrawn: true }) };
  const tools = collect(registerProposalTools, { proposals });
  assert.deepEqual([...tools.keys()], ['propose', 'withdraw_proposal']);
  assert.deepEqual(PROPOSAL_KINDS, ['create_project', 'start_driver', 'stop_driver']);
  assert.deepEqual(await tools.get('propose').execute({ kind: 'deploy', payload: {}, reason: 'r' }, exec), { refused: 'unknown_kind', kinds: PROPOSAL_KINDS });
  assert.equal(calls.length, 0);
  await tools.get('propose').execute({ kind: 'stop_driver', payload: { project_id: 'w' }, reason: 'r' }, exec);
  await tools.get('propose').execute({ kind: 'stop_driver', payload: { project_id: 'w' }, reason: 'r2', supersedes: 'p-1' }, exec);
  await tools.get('withdraw_proposal').execute({ id: 'p-1', reason: 'gone' }, exec);
  assert.deepEqual(calls, [['propose', { kind: 'stop_driver', payload: { project_id: 'w' }, reason: 'r' }],
    ['propose', { kind: 'stop_driver', payload: { project_id: 'w' }, reason: 'r2', supersedes: 'p-1' }],
    ['withdraw', { id: 'p-1', reason: 'gone' }]]);
});
