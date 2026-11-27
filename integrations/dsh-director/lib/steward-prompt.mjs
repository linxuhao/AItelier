/** The steward's prompt, by what its host can do (overfit review 2026-11-24, 重-11 and 次-19).
 *  steward.md is written for the fullest host (the backend adapter): a dashboard, Slack and
 *  WeChat, briefs, family groups, directors to hand off to and letters from them, a catalog of
 *  connections. A host without one of those must not be told about it, so each passage that
 *  needs one is cut here when the host lacks it, and some are replaced by a line that says
 *  what that host does instead. A host with every feature gets steward.md byte for byte.
 *
 *  The features are the host's capabilities (host.d.ts) - `portfolio`, `handoff`,
 *  `proposals`, `channels` (the owner reaches the steward through the host: its message tool,
 *  chat channels, pushed questions, reminders and the dashboard), `groups`, `catalog` - and
 *  `memory`, which here means the owner's memory is configured (config.memory: the memory
 *  tools exist), not the host's `memory` op; that op is `ownerMessage` here (the host can show
 *  which message its owner wrote, so memory_forget can cite one). A director with project letters
 *  exists only where the steward can hand off to one, so the inbox and letter passages need `handoff`;
 *  so do the questions a host pushes to the owner's chat (they are directors' questions) and the
 *  relay tool that answers them. `dashboard` is the owner's place on the host - the dashboard with
 *  the whole conversation, proposal cards, the channels' status and login QR, and the host's
 *  reminders and daily brief: every host has it unless its config says ownerDashboard: false (the
 *  built-in local host, whose owner is in its DSH, with commands instead). Without it the channel
 *  passages say what that host does instead.
 *
 *  Each cut runs from the first `from` through the first `through` after it (just `from`
 *  without one). A cut whose text is not in the prompt (removed by an earlier, wider cut, or a
 *  host's own prompt file) changes nothing; the shipped steward.md has every cut's text once
 *  (test/steward-prompt.test.mjs).
 *
 *  stewardPromptFor composes this with the owner's language (lib/owner-text.mjs). */
import { stewardPromptInLanguage } from './owner-text.mjs';

export const STEWARD_PROMPT_FEATURES = Object.freeze(['portfolio', 'handoff', 'proposals', 'channels', 'groups', 'catalog', 'memory', 'ownerMessage',
  'dashboard']);

/** What a host with chat channels and no dashboard (the built-in local host) tells its steward
 *  instead of the dashboard's passages: how a reply reaches the owner, and the channels. */
const localReply = () => 'Your owner reads what you write here, in this conversation, and on the chat channels they bound.\n'
  + 'A message from a channel names it (`source_channel`) and its message_id: answer it with message_owner,\n'
  + 'that id as reply_to_message_id, one clear message per reply. Your owner already sees that you received it and\n'
  + 'are working on it; when an answer needs more than two or three lookups, first send one short line saying what\n'
  + 'you are looking at, then the answer. If it is still unanswered when your turn ends, the last thing you said is\n'
  + 'sent there as your reply. A message you start yourself goes with message_owner, without\n'
  + 'reply_to_message_id, to your owner\'s default channel when they chose one. A message without\n'
  + '`source_channel` was typed here: answer it here. End every turn with words meant for your owner, never\n'
  + 'notes to yourself.\n';
const localChannels = f => 'Chat channels: your owner binds their chat accounts (Slack, WeChat) with the /bind command;\n'
  + 'a message from there wakes you as an owner message that names its channel (`source_channel`:\n'
  + '`slack` or `wechat`); one typed here has none.' + (f.has('memory') ? ' Your memory records it with its channel.' : '')
  + ' What goes to a channel says the same as what you would say\n'
  + 'here: never host paths, internal ids, credentials or state dumps - a chat service is someone else\'s server.\n'
  + 'A channel can be disconnected; this conversation always has the whole exchange, so never promise your\n'
  + 'owner that a channel will get something.\n'
  + 'WeChat login: when your owner wants to connect WeChat (again), call request_wechat_qr once. The QR is\n'
  + 'written to a file on this machine - never in chat, never from you - so tell your owner to type /channels,\n'
  + 'which names that file, and scan it with the WeChat account to connect (it is valid for about 2 minutes); if\n'
  + 'it is refused, say why in plain words (a QR is already waiting, WeChat is already connected, or try again in\n'
  + 'a few minutes).\n';

const canDo = f => 'What you can do: ' + [
  f.has('portfolio') && 'read every project (portfolio_projects, portfolio_project, portfolio_conversation)',
  f.has('handoff') && 'read your inboxes whole (portfolio_inbox for a project letter, private_inbox for a letter in your private inbox)',
  'talk with your owner' + (f.has('memory') ? ' and keep what matters in their memory' : ''),
  f.has('handoff') && 'pass your owner\'s words to a project\'s director (handoff)',
  f.has('proposals') && 'propose actions for your owner to approve (propose / withdraw_proposal)',
].filter(Boolean).join('; ') + '. Your tools are the ones listed'
  + (f.has('catalog') ? ' plus your connection tools; when a "basic capabilities" table follows, it says exactly which jobs you can do and with which tool.' : ', and no others.') + '\n'
  // Without handoff no director hears the steward; it told a stranger's owner "the project's
  // director picks up the work" and "I'll pass your choice along" (stranger re-test 2026-11-26).
  + (f.has('handoff') ? '' : 'No director hears you on this host: you cannot pass anything to one, and a project your\nowner approves waits until your owner starts its director.\n');

export const STEWARD_CUTS = Object.freeze([
  { needs: ['portfolio', 'handoff', 'memory', 'proposals', 'catalog'], from: 'What you can do: read every project',
    through: 'it says exactly which jobs you can do and with which tool.\n', else: canDo },
  { needs: ['handoff'], from: 'Inbox: you have two.', through: 'has_more with next_after for the page after this one.\n\n' },
  { needs: ['handoff', 'proposals'], from: ' Starting or stopping a director, creating a', through: 'approves.',
    else: f => f.has('proposals') ? ' What your host can do for your owner happens only through a proposal your\nowner approves (propose names the kinds).' : '' },
  { needs: ['channels', 'handoff'], from: 'Relaying is not answering:', through: 'combine an answer.\n' },
  { needs: ['portfolio', 'memory'], from: '- If it is a question you can answer from the projects or from memory', through: 'which memory entry).\n',
    else: f => f.has('portfolio') ? '- If it is a question you can answer from the projects, answer it.\n  Say which project the answer comes from.\n'
      : f.has('memory') ? '- If it is a question you can answer from memory, answer it.\n  Say which memory entry the answer comes from.\n'
      : '- If it is a question you can answer, answer it.\n' },
  { needs: ['handoff'], from: '- If it is something a project\'s director must act on', through: 'relay it.\n' },
  { needs: ['proposals'], from: '- If it needs an action only your owner may take', through: '  decides.\n' },
  { needs: ['handoff'], from: '(a new project, starting or stopping\n  a director)', else: () => '(a new project)' },
  { needs: ['channels'], from: '  When you ask your owner to decide it, write the exact phrase', through: 'never approve or decline anything yourself.\n' },
  // Within the passage just kept (channels, no dashboard): the owner decides here with a command too.
  { needs: ['dashboard'], from: ' as\n  well as decide on the dashboard. Only that exact line, sent by your owner, decides;',
    else: () => ',\n  or decide here with the command your context snapshot\'s owner_commands names. Only that exact\n  line or that command, sent by your owner, decides;' },
  { needs: ['channels', 'dashboard'], from: '  If your context snapshot has owner_commands', through: '  decides.\n',
    else: f => f.has('channels') ? '' : '  Tell your owner how to decide it: when your context snapshot has owner_commands, give them\n  the command it names; only your owner decides.\n' },
  { needs: ['channels', 'handoff'], from: '- Questions a director asked your owner stay theirs', through: 'do not remind them yourself.\n' },
  { needs: ['memory'], from: 'Memory: your owner\'s profile is in your context snapshot', through: 'citing the id of that message.\n' },
  { needs: ['ownerMessage'], from: 'Forget only when your owner asked, citing the id of that message.',
    else: () => 'Never forget an entry yourself: memory_forget refuses on this host,\nwhich cannot show which message your owner wrote; when your owner asks, give them the forget command\nyour context snapshot\'s owner_commands names.' },
  { needs: ['handoff'], from: ', director letters\nand wakes', else: () => ' and wakes' },
  { needs: ['handoff'], from: ', a director\'s letter or a wake', else: () => ', or a wake' },
  { needs: ['groups'], from: ' Group turns are never in it.' },
  { needs: ['portfolio'], from: ' a project made\nprogress they care about, a project is stuck or has been waiting on them for a while,' },
  { needs: ['channels', 'dashboard'], from: ' a reminder they set came due,' },
  { needs: ['proposals'], from: ', or a proposal\'s result' },
  { needs: ['handoff'], from: 'Platform and maintenance news', through: 'and stay quiet. ' },
  { needs: ['channels', 'handoff'], from: ' (the approval phrase above and a\nquestion\'s code like Q12 are the two exceptions)',
    else: f => !f.has('proposals') ? '' : f.has('channels') ? ' (the approval phrase above and a command your snapshot\'s owner_commands\nnames, with its id, are the two exceptions)'
      : ' (a command your snapshot\'s owner_commands names, with its id, is the exception)' },
  { needs: ['channels', 'dashboard'], from: 'Daily brief: when a reminder asks', through: 'it carries no weather.\n\n' },
  { needs: ['channels'], from: 'Every message to your owner is read on a phone', through: 'never shortened or left out.\n\n' },
  { needs: ['channels', 'dashboard'], from: 'Reply to your owner through the message tool', through: 'never notes to yourself.\n',
    else: f => f.has('channels') ? localReply() : 'Your owner reads what you write here, in this conversation: end every turn with words\nmeant for your owner, never notes to yourself.\n' },
  { needs: ['channels'], from: '`created_at` on a message is', through: 'from the\nchat app. ' },
  { needs: ['channels', 'dashboard'], from: 'Chat channels: your host may connect', through: 'or try again in a few minutes).\n',
    else: f => f.has('channels') ? localChannels(f) : '' },
  { needs: ['groups'], from: 'Group chats: your host may bind', through: 'tell your owner in one short line.\n' },
  { needs: ['catalog'], from: 'Basic capabilities: the table "Your basic capabilities"', through: 'propose adding it.\n' },
  { needs: ['handoff'], from: 'A director\'s letter to you is answered', through: 'message to a director yourself. ' },
]);

/** The features a host offers the steward's prompt: its capabilities named in
 *  STEWARD_PROMPT_FEATURES, with `memory` meaning config.memory is set, `ownerMessage` the
 *  host's own `memory` capability and `dashboard` config.ownerDashboard not false (`dashboard`
 *  here: absent, true). */
export function stewardPromptFeatures(capabilities, { memory, dashboard = true } = {}) {
  const declared = capabilities ?? [];
  const derived = ['memory', 'ownerMessage', 'dashboard'];
  return new Set([...declared.filter(name => !derived.includes(name) && STEWARD_PROMPT_FEATURES.includes(name)),
    ...(memory ? ['memory'] : []), ...(declared.includes('memory') ? ['ownerMessage'] : []), ...(dashboard ? ['dashboard'] : [])]);
}

/** steward.md (`text`) for a host with `features` (a Set): every cut whose features are not all
 *  there is removed, or replaced by its `else`. */
export function stewardPrompt(text, features) {
  let prompt = String(text);
  for (const cut of STEWARD_CUTS) {
    if (cut.needs.every(name => features.has(name))) continue;
    const start = prompt.indexOf(cut.from);
    if (start < 0) continue;
    const tail = cut.through ?? cut.from;
    const at = cut.through ? prompt.indexOf(cut.through, start + cut.from.length) : start;
    if (at < 0) continue;
    prompt = prompt.slice(0, start) + (cut.else ? cut.else(features) : '') + prompt.slice(at + tail.length);
  }
  return prompt;
}

/** The steward's prompt for a host: its `features` (stewardPromptFeatures) and its owner's
 *  `language` (HostConfig.ownerLanguage). The cuts run first, on steward.md as written (their
 *  anchors are its own, 'zh', text, and none holds a language passage); the language then
 *  replaces each passage wherever a cut left it, so a cut-away passage is simply not there and a
 *  kept one is swapped in its own section. A 'zh' host with every feature gets steward.md byte
 *  for byte. */
export function stewardPromptFor(text, { features, language }) {
  return stewardPromptInLanguage(stewardPrompt(text, features), language);
}
