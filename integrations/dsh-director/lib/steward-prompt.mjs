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
 *  exists only where the steward can hand off to one, so the inbox and letter passages need `handoff`.
 *
 *  Each cut runs from the first `from` through the first `through` after it (just `from`
 *  without one). A cut whose text is not in the prompt (removed by an earlier, wider cut, or a
 *  host's own prompt file) changes nothing; the shipped steward.md has every cut's text once
 *  (test/steward-prompt.test.mjs). */

export const STEWARD_PROMPT_FEATURES = Object.freeze(['portfolio', 'handoff', 'proposals', 'channels', 'groups', 'catalog', 'memory', 'ownerMessage']);

const canDo = f => 'What you can do: ' + [
  f.has('portfolio') && 'read every project (portfolio_projects, portfolio_project, portfolio_conversation)',
  f.has('handoff') && 'read your inboxes whole (portfolio_inbox for a project letter, private_inbox for a letter in your private inbox)',
  'talk with your owner' + (f.has('memory') ? ' and keep what matters in their memory' : ''),
  f.has('handoff') && 'pass your owner\'s words to a project\'s director (handoff)',
  f.has('proposals') && 'propose actions for your owner to approve (propose / withdraw_proposal)',
].filter(Boolean).join('; ') + '. Your tools are the ones listed'
  + (f.has('catalog') ? ' plus your connection tools; when a "basic capabilities" table follows, it says exactly which jobs you can do and with which tool.' : ', and no others.') + '\n';

export const STEWARD_CUTS = Object.freeze([
  { needs: ['portfolio', 'handoff', 'memory', 'proposals', 'catalog'], from: 'What you can do: read every project',
    through: 'it says exactly which jobs you can do and with which tool.\n', else: canDo },
  { needs: ['handoff'], from: 'Inbox: you have two.', through: 'has_more with next_after for the page after this one.\n\n' },
  { needs: ['handoff', 'proposals'], from: ' Starting or stopping a director, creating a', through: 'approves.',
    else: f => f.has('proposals') ? ' What your host can do for your owner happens only through a proposal your\nowner approves (propose names the kinds).' : '' },
  { needs: ['channels'], from: 'Relaying is not answering:', through: 'combine an answer.\n' },
  { needs: ['portfolio', 'memory'], from: '- If it is a question you can answer from the projects or from memory', through: 'which memory entry).\n',
    else: f => f.has('portfolio') ? '- If it is a question you can answer from the projects, answer it.\n  Say which project the answer comes from.\n'
      : f.has('memory') ? '- If it is a question you can answer from memory, answer it.\n  Say which memory entry the answer comes from.\n'
      : '- If it is a question you can answer, answer it.\n' },
  { needs: ['handoff'], from: '- If it is something a project\'s director must act on', through: 'relay it.\n' },
  { needs: ['proposals'], from: '- If it needs an action only your owner may take', through: '  decides.\n' },
  { needs: ['handoff'], from: '(a new project, starting or stopping\n  a director)', else: () => '(a new project)' },
  { needs: ['channels'], from: '  When you ask your owner to decide it, write the exact phrase', through: 'never approve or decline anything yourself.\n' },
  { needs: ['channels'], from: '  If your context snapshot has owner_commands', through: '  decides.\n',
    else: () => '  Tell your owner how to decide it: when your context snapshot has owner_commands, give them\n  the command it names; only your owner decides.\n' },
  { needs: ['channels', 'handoff'], from: '- Questions a director asked your owner stay theirs', through: 'do not remind them yourself.\n' },
  { needs: ['memory'], from: 'Memory: your owner\'s profile is in your context snapshot', through: 'citing the id of that message.\n' },
  { needs: ['ownerMessage'], from: 'Forget only when your owner asked, citing the id of that message.',
    else: () => 'Never forget an entry yourself: memory_forget refuses on this host,\nwhich cannot show which message your owner wrote; when your owner asks, give them the forget command\nyour context snapshot\'s owner_commands names.' },
  { needs: ['handoff'], from: ', director letters\nand wakes', else: () => ' and wakes' },
  { needs: ['handoff'], from: ', a director\'s letter or a wake', else: () => ', or a wake' },
  { needs: ['groups'], from: ' Group turns are never in it.' },
  { needs: ['portfolio'], from: ' a project made\nprogress they care about, a project is stuck or has been waiting on them for a while,' },
  { needs: ['channels'], from: ' a reminder they set came due,' },
  { needs: ['proposals'], from: ', or a proposal\'s result' },
  { needs: ['handoff'], from: 'Platform and maintenance news', through: 'and stay quiet. ' },
  { needs: ['channels'], from: ' (the approval phrase above and a\nquestion\'s code like Q12 are the two exceptions)',
    else: f => f.has('proposals') ? ' (a command your snapshot\'s owner_commands names, with its id, is the exception)' : '' },
  { needs: ['channels'], from: 'Daily brief: when a reminder asks', through: 'it carries no weather.\n\n' },
  { needs: ['channels'], from: 'Every message to your owner is read on a phone', through: 'never shortened or left out.\n\n' },
  { needs: ['channels'], from: 'Reply to your owner through the message tool', through: 'never notes to yourself.\n',
    else: () => 'Your owner reads what you write here, in this conversation: end every turn with words\nmeant for your owner, never notes to yourself.\n' },
  { needs: ['channels'], from: '`created_at` on a message is', through: 'from the\nchat app. ' },
  { needs: ['channels'], from: 'Chat channels: your host may connect', through: 'or try again in a few minutes).\n' },
  { needs: ['groups'], from: 'Group chats: your host may bind', through: 'tell your owner in one short line.\n' },
  { needs: ['catalog'], from: 'Basic capabilities: the table "Your basic capabilities"', through: 'propose adding it.\n' },
  { needs: ['handoff'], from: 'A director\'s letter to you is answered', through: 'message to a director yourself. ' },
]);

/** The features a host offers the steward's prompt: its capabilities named in
 *  STEWARD_PROMPT_FEATURES, with `memory` meaning config.memory is set and `ownerMessage` the
 *  host's own `memory` capability. */
export function stewardPromptFeatures(capabilities, { memory } = {}) {
  const declared = capabilities ?? [];
  return new Set([...declared.filter(name => name !== 'memory' && name !== 'ownerMessage' && STEWARD_PROMPT_FEATURES.includes(name)),
    ...(memory ? ['memory'] : []), ...(declared.includes('memory') ? ['ownerMessage'] : [])]);
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
