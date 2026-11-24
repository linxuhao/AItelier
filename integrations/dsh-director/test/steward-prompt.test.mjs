/** Overfit review 2026-11-24 (重-11, 次-19): the steward's prompt by what its host can do. A host
 *  with every feature gets steward.md unchanged; the built-in local host is told nothing about a
 *  dashboard, chat channels, briefs, groups, handoffs, director letters or connections. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { STEWARD_CUTS, STEWARD_PROMPT_FEATURES, stewardPrompt, stewardPromptFeatures, stewardPromptFor } from '../lib/steward-prompt.mjs';
import { OWNER_TEXTS, stewardPromptInLanguage } from '../lib/owner-text.mjs';
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
  ['with projectsDir only', { projectsDir: '/tmp/projects' }], ['with memory only', { memory: { userId: 'o' } }], ['bare', {}]]) {
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
      assert.match(prompt, /from memory, answer it\./);
      // Review round 2 (nit): memory_forget refuses on this host (no owner message to cite), so the
      // steward is sent to its owner's forget command, not told to cite a message id.
      assert.ok(!prompt.includes('citing the id of that message'), prompt);
      assert.match(prompt, /give them the forget command\nyour context snapshot's owner_commands names/);
    } else for (const word of ['memory_', 'memory entry', 'from memory']) assert.ok(!prompt.includes(word), word + ':\n' + prompt);
    if (options.projectsDir) {
      assert.match(prompt, /read every project \(portfolio_projects, portfolio_project, portfolio_conversation\)/);
      assert.match(prompt, /make a proposal/);
      assert.match(prompt, /when your context snapshot has owner_commands, give them\n {2}the command it names/);
      assert.ok(!prompt.includes('starting or stopping'), 'the local host proposes only a new project');
      assert.match(prompt, /answer from the projects/);
      assert.match(prompt, /a project made\nprogress they care about/);
    } else for (const word of ['portfolio_', 'propose', 'project made', 'project is stuck', 'from the projects', 'which project'])
      assert.ok(!prompt.includes(word), word + ':\n' + prompt);
  });
}

// The two branches composed (overfit 2026-11-24 items 12 and 14): the host's features cut first,
// then the owner's language swaps what is left.
const ZH_PASSAGE = /同意 proposal|拒绝 proposal|今天没有要你处理的事|今早的简报没发成|这个我在群里答不了/;
const EN_PASSAGES = OWNER_TEXTS.en.stewardPrompt;
const ALL = new Set(STEWARD_PROMPT_FEATURES);
const sha = text => createHash('sha256').update(text, 'utf8').digest('hex');

test('composed, every feature: zh is steward.md as at daf492274e byte for byte; en is the language swap alone', () => {
  const zh = stewardPromptFor(md, { features: ALL, language: 'zh' });
  assert.equal(zh, md);
  assert.equal(sha(zh), 'b91a5b14561d39ead1523c6a15d3a6b6dcff6b46bf05924203c30c21e7427680', 'steward.md as at daf492274e');
  const en = stewardPromptFor(md, { features: ALL, language: 'en' });
  assert.equal(en, stewardPromptInLanguage(md, 'en'));
  assert.equal(stewardPromptFor(md, { features: ALL }), en, 'no language: English');
  assert.ok(!ZH_PASSAGE.test(en));
  assert.match(en, /`approve proposal <id>` to approve, `decline proposal <id>` to decline/);
  assert.match(en, /send\nonly "Nothing needs you today\."/);
});

for (const [label, options] of [['with projectsDir and memory', { projectsDir: '/tmp/projects', memory: { userId: 'o' } }], ['bare', {}]]) {
  test(`composed, the local host (${label}): zh and en both drop the host's missing sections and keep the same text`, () => {
    const host = localHost({ root: '/tmp/x', provider: 'p', model: 'm', rootRole: 'steward', ...options });
    const features = stewardPromptFeatures(host.capabilities, { memory: host.config.memory });
    const zh = stewardPromptFor(md, { features, language: 'zh' }), en = stewardPromptFor(md, { features, language: 'en' });
    assert.equal(zh, stewardPrompt(md, features), 'zh: the cuts alone');
    assert.equal(en, stewardPromptInLanguage(stewardPrompt(md, features), 'en'));
    for (const prompt of [zh, en]) {
      for (const word of BACKEND_ONLY) assert.ok(!prompt.includes(word), word);
      for (const [from, to] of EN_PASSAGES) assert.ok(!prompt.includes(from) && !prompt.includes(to), to);
      assert.match(prompt, /Your owner reads what you write here, in this conversation/);
    }
    // Every passage sits inside a channels or groups cut, so nothing is left to swap here.
    assert.equal(en, zh);
  });
}

test('composed, every feature set: no cut fails on a swapped passage and none leaves half of one', () => {
  for (let mask = 0; mask < 1 << STEWARD_PROMPT_FEATURES.length; mask++) {
    const features = new Set(STEWARD_PROMPT_FEATURES.filter((_, i) => mask & (1 << i)));
    const zh = stewardPromptFor(md, { features, language: 'zh' }), en = stewardPromptFor(md, { features, language: 'en' });
    assert.equal(zh, stewardPrompt(md, features), [...features].join());
    assert.equal(en, stewardPromptInLanguage(stewardPrompt(md, features), 'en'), [...features].join());
    assert.ok(!ZH_PASSAGE.test(en), [...features].join());
    // Each passage swapped exactly where its section was kept.
    for (const [from, to] of EN_PASSAGES) assert.equal(en.split(to).length - 1, zh.split(from).length - 1, `${[...features].join()}: ${to}`);
  }
  for (const [, to] of EN_PASSAGES) assert.ok(!md.includes(to), to);
});

// Integration review (nit): the order. On steward.md either order gives the same text, so the order
// is pinned on a text where it shows: a passage split by a section the host lacks is whole, and
// swapped, only once the cut has run.
test('composed: the cuts run first and the language swaps the text they leave', () => {
  const cut = STEWARD_CUTS.find(c => !c.through && !c.else && c.needs.length === 1 && c.needs[0] === 'channels');
  const [zhPassage, enPassage] = EN_PASSAGES.find(([from]) => from.startsWith('only '));
  const half = zhPassage.length >> 1;
  const text = zhPassage.slice(0, half) + cut.from + zhPassage.slice(half);
  const features = new Set(STEWARD_PROMPT_FEATURES.filter(name => name !== 'channels'));
  assert.equal(stewardPromptFor(text, { features, language: 'zh' }), zhPassage);
  assert.equal(stewardPromptFor(text, { features, language: 'en' }), enPassage);
  assert.equal(stewardPromptFor(text, { features: ALL, language: 'en' }), text, 'kept whole, the split passage is no passage');
  // steward.md's own cuts hold no passage, in either language, so a cut never misses its text there.
  for (const { from, through } of STEWARD_CUTS) for (const pair of EN_PASSAGES) for (const part of pair)
    assert.ok(!from.includes(part) && !(through ?? '').includes(part), part);
});
