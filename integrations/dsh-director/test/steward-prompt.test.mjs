/** Overfit review 2026-11-24 (重-11, 次-19): the steward's prompt by what its host can do. A host
 *  with every feature gets steward.md unchanged; the built-in local host is told nothing about a
 *  dashboard, chat channels, briefs, groups, handoffs, director letters or connections. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { STEWARD_CUTS, STEWARD_PROMPT_FEATURES, stewardPrompt, stewardPromptFeatures } from '../lib/steward-prompt.mjs';
import { localHost } from '../hosts/local.mjs';

const md = readFileSync(new URL('../steward.md', import.meta.url), 'utf8');
const occurrences = (text, part) => text.split(part).length - 1;

test('every cut names text steward.md has exactly once, and its end comes after its start', () => {
  for (const cut of STEWARD_CUTS) {
    assert.equal(occurrences(md, cut.from), 1, JSON.stringify(cut.from));
    if (cut.through) assert.ok(md.indexOf(cut.through, md.indexOf(cut.from) + cut.from.length) > 0, JSON.stringify(cut.through));
    for (const name of cut.needs) assert.ok(STEWARD_PROMPT_FEATURES.includes(name), name);
  }
});

test('a host with every feature gets steward.md byte for byte; the host\'s own `memory` op is not the memory feature', () => {
  const all = ['memory', 'portfolio', 'handoff', 'proposals', 'channels', 'groups', 'catalog'];
  assert.equal(stewardPrompt(md, stewardPromptFeatures(all, { memory: { userId: 'o' } })), md);
  assert.equal(stewardPrompt(md, new Set(STEWARD_PROMPT_FEATURES)), md);
  assert.ok(!stewardPromptFeatures(all, {}).has('memory'), 'no config.memory: no memory tools, whatever the host declares');
  assert.notEqual(stewardPrompt(md, stewardPromptFeatures(all, {})), md);
  // A prompt file of the host's own, without these passages, is left as it is.
  assert.equal(stewardPrompt('Be kind.\n', new Set()), 'Be kind.\n');
});

const BACKEND_ONLY = ['dashboard', 'Slack', 'WeChat', 'slack', 'wechat', 'Daily brief', 'family', 'Group', 'group turn',
  'handoff', 'portfolio_inbox', 'private_inbox', 'letter', 'request_wechat_qr', 'search_tools', 'call_tool', 'Q12', '同意 proposal',
  'message tool', 'relay', 'reminder', 'connection', 'send_media', 'read_document', 'make_document', 'calculate', 'basic capabilities',
  'channel_at'];

for (const [label, options] of [['with projectsDir and memory', { projectsDir: '/tmp/projects', memory: { userId: 'o' } }],
  ['bare', {}]]) {
  test(`the local host's steward (${label}) is told only what it can do`, () => {
    const host = localHost({ root: '/tmp/x', provider: 'p', model: 'm', rootRole: 'steward', ...options });
    const prompt = stewardPrompt(md, stewardPromptFeatures(host.capabilities, { memory: host.config.memory }));
    for (const word of BACKEND_ONLY) assert.ok(!prompt.includes(word), `the standalone steward is told about ${word}:\n${prompt}`);
    assert.match(prompt, /^You are your owner's steward/);
    assert.match(prompt, /grep_chat_history\(pattern\) searches your own complete conversation log/);
    assert.match(prompt, /Your owner reads what you write here, in this conversation/);
    assert.match(prompt, /When nothing is waiting, call idle_dormant\.\n$/);
    assert.ok(!/\n{3}/.test(prompt) && !/ \n/.test(prompt), 'no paragraph left torn: ' + JSON.stringify(prompt));
    if (options.memory) {
      assert.match(prompt, /Each\nconversation turn is recorded automatically/);
      assert.match(prompt, /and keep what matters in their memory/);
    } else assert.ok(!prompt.includes('memory_'), prompt);
    if (options.projectsDir) {
      assert.match(prompt, /read every project \(portfolio_projects, portfolio_project, portfolio_conversation\)/);
      assert.match(prompt, /make a proposal/);
      assert.match(prompt, /when your context snapshot has owner_commands, give them\n {2}the command it names/);
      assert.ok(!prompt.includes('starting or stopping'), 'the local host proposes only a new project');
    } else for (const word of ['portfolio_', 'propose']) assert.ok(!prompt.includes(word), word);
  });
}
