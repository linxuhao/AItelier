/** The owner's commands on a standalone DSH (plugin step 15, design-steward 7): registered by
 *  the built-in local host in DSH's command registry (`ctx.commands`, @deepseek-ai/dsh-commands)
 *  and run by the owner in DSH's own client. A command never reaches the model: the registry
 *  logs it and shows its answer to the owner. An external host registers none of them.
 *
 *    /questions                 the open questions to the owner
 *    /answer <id> <text>        answer one; recorded answered_by owner:local, it wakes the root
 *    /proposals                 the open proposals (with the host's projectsDir only)
 *    /approve <id>              run one (create_project) now; the result wakes the steward
 *    /decline <id> [reason]     decline one
 *    /profile                   the resident profile, its version and its file (with memory only)
 *    /purge <id>                purge one memory entry from every version: the first /purge <id>
 *                               only warns; /purge <id> PURGE within 5 minutes of that warning
 *                               purges (with memory only)
 *    /forget <id> [reason]      forget one memory entry: out of search and reads, recoverable;
 *                               it wakes the steward with one line (with memory only)
 *    /restore <id>              bring a forgotten entry back; it wakes the steward (with memory only)
 *    /status                    a read-only overview: the root's last phase, its work items, the open
 *                               questions and proposals, and one line per project (with projectsDir)
 *    /bind <slack|wechat>       a one-time 6-digit code; sent from the owner's own chat account
 *                               within 10 minutes, it binds that account (with channels only)
 *    /channels                  the bindings, the default channel, each channel's status and the
 *                               WeChat login QR's file (with channels only)
 *    /unbind <channel>          the channel's account no longer reaches the steward (with channels only)
 *    /default <channel|none>    where the steward's own messages go when they answer no chat message
 *                               (with channels only)
 *
 *  A DSH command carries no user identity (CommandInvocation: commandId, agent, rawInput,
 *  attachments, signal), so every decision taken here is the owner's as owner:local. The
 *  registry is the process's: every agent's UI lists these commands, and each answers only in
 *  the root session's UI (`isRoot(invocation.agent)`), where the owner talks to the director. */

import { readFile, readdir } from 'node:fs/promises';
import { join } from 'node:path';
import { isSettled } from '../lib/host.mjs';

export const PURGE_WORD = 'PURGE';
/** How long a /purge warning stands for its confirmation. */
export const PURGE_CONFIRM_MS = 5 * 60 * 1000;
const OWNER = 'owner:local';
const ok = text => ({ kind: 'success', text });
const fail = text => ({ kind: 'error', text });
/** The first word of a command's input, and the rest (trimmed). */
const split = raw => { const m = /^\s*(\S+)(?:\s+([\s\S]*))?$/.exec(raw); return m ? [m[1], (m[2] ?? '').trim()] : ['', '']; };
const refusal = (what, result) => fail(`${what}: ${result.refused}${result.reason ? ' - ' + result.reason : ''}`);
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
/** Oldest first by the given time field, then by id: the order things were asked or proposed in. */
const byTime = field => ([a, x], [b, y]) => String(x[field] ?? '').localeCompare(String(y[field] ?? '')) || a.localeCompare(b);
/** One open proposal as the owner reads it; a record edited by hand into another shape is
 *  shown for what it is, never thrown on (it would hide every other proposal). */
function proposalLine(id, p) {
  const payload = isRecord(p.payload) ? p.payload : {};
  const seeds = Array.isArray(payload.seed_nodes) ? payload.seed_nodes.length : 0;
  const shape = typeof payload.project_id === 'string' && typeof payload.title === 'string' && typeof payload.goal === 'string'
    ? '' : '\n  (this record is not a whole create_project: approving it fails, decline it)';
  return `- ${id} ${p.kind}: ${payload.project_id ?? '?'} "${payload.title ?? ''}" (${seeds} work item(s))\n  goal: ${payload.goal ?? ''}\n  reason: ${p.reason ?? ''}${shape}`;
}

/** What /status shows of one root at most: the open work items listed (the rest counted). */
export const STATUS_OPEN_SHOWN = 20;
/** One file of a root, read for /status: {value}, {missing: true} or {problem} - never thrown. */
async function readSafe(file, parse) {
  let raw;
  try { raw = await readFile(file, 'utf8'); }
  catch (error) { return error?.code === 'ENOENT' ? { missing: true } : { problem: String(error?.code ?? error?.message ?? error) }; }
  try { return { value: parse(raw) }; } catch (error) { return { problem: 'does not parse (' + String(error?.message ?? error).slice(0, 120) + ')' }; }
}
const parseJson = raw => JSON.parse(raw);
/** progress.jsonl's last line that parses as an object. */
const lastLine = raw => {
  const lines = raw.split('\n').map(line => line.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) { try { const row = JSON.parse(lines[i]); if (isRecord(row)) return row; } catch { /* the line before */ } }
  return undefined;
};
const counted = counts => Object.entries(counts).map(([state, n]) => `${n} ${state}`).join(', ');
/** One root's facts: its phase, work items by state, the open ones, open questions and proposals. */
async function rootFacts(dir) {
  const progress = await readSafe(join(dir, 'progress.jsonl'), lastLine);
  const work = await readSafe(join(dir, 'work.json'), parseJson);
  const questions = await readSafe(join(dir, 'questions.json'), parseJson);
  const items = Array.isArray(work.value?.items) ? work.value.items.filter(isRecord) : [];
  const counts = {};
  for (const item of items) { const state = typeof item.state === 'string' ? item.state : '?'; counts[state] = (counts[state] ?? 0) + 1; }
  return {
    phase: progress.problem ? `progress.jsonl is unreadable: ${progress.problem}` : !progress.value ? 'no phase reported yet'
      : `${progress.value.phase ?? '?'}${progress.value.at ? ' at ' + progress.value.at : ''}${progress.value.error ? ' - ' + String(progress.value.error).split('\n')[0].slice(0, 200) : ''}`,
    work: work.problem ? `work.json is unreadable: ${work.problem}` : work.value !== undefined && !Array.isArray(work.value?.items) ? 'work.json has no items list'
      : items.length ? `${items.length} (${counted(counts)})` : 'none',
    open: items.filter(item => !isSettled(item.state)),
    questions: questions.problem ? `questions.json is unreadable: ${questions.problem}`
      : String(Object.values(isRecord(questions.value) ? questions.value : {}).filter(q => isRecord(q) && q.status === 'open').length),
  };
}
/** /status: the root's last phase, its work items, its open questions and proposals; with
 *  `projectsDir` one line per project. Plain text; a missing or hand-edited file is said, never thrown. */
export async function statusText({ root, projectsDir }) {
  const facts = await rootFacts(root);
  const lines = [`Status of ${root}`, `Phase: ${facts.phase}`, `Work items: ${facts.work}`];
  if (facts.open.length) {
    lines.push(`Open work (${facts.open.length}):`, ...facts.open.slice(0, STATUS_OPEN_SHOWN).map(item =>
      `- ${item.id ?? '?'}${item.title ? ' "' + String(item.title).slice(0, 120) + '"' : ''} [${item.state ?? '?'}${item.holder ? ', held by ' + item.holder : ''}]`));
    if (facts.open.length > STATUS_OPEN_SHOWN) lines.push(`  ... and ${facts.open.length - STATUS_OPEN_SHOWN} more (work.json)`);
  }
  lines.push(`Open questions: ${facts.questions}`);
  if (projectsDir) {
    const proposals = await readSafe(join(root, 'proposals.json'), parseJson);
    lines.push(`Open proposals: ${proposals.problem ? `proposals.json is unreadable: ${proposals.problem}`
      : Object.values(isRecord(proposals.value) ? proposals.value : {}).filter(p => isRecord(p) && p.status === 'open').length}`);
    let names;
    try { names = (await readdir(projectsDir, { withFileTypes: true })).filter(e => e.isDirectory() && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(e.name)).map(e => e.name).sort(); }
    catch (error) { lines.push(`Projects: ${projectsDir} ${error?.code === 'ENOENT' ? 'does not exist' : 'is unreadable: ' + (error?.code ?? error?.message)}`); names = undefined; }
    if (names) {
      lines.push(`Projects (${names.length}) in ${projectsDir}:`);
      for (const name of names) {
        const dir = join(projectsDir, name), project = await readSafe(join(dir, 'project.json'), parseJson), one = await rootFacts(dir);
        const title = isRecord(project.value) && typeof project.value.title === 'string' ? ` "${project.value.title.slice(0, 120)}"` : '';
        lines.push(`- ${name}${title}: ${one.phase}; work items: ${one.work}; open questions: ${one.questions}`);
      }
    }
  }
  return lines.join('\n');
}
const CHANNEL_NAMES = ['slack', 'wechat'];
const CHANNEL_LABEL = { slack: 'Slack', wechat: 'WeChat' };

/** @param commands the registry (ctx.commands): register(definition) -> unregister
 *  @param o questions(): the questions.json map; answer({id, text, by}); proposals(): the
 *    proposals.json map and decide({id, approve, reason, by}), both absent without projectsDir;
 *    memory: the owner's memory store (absent without memory); memoryError: why a configured
 *    memory is not open (then /profile, /purge, /forget and /restore say so); wake(line): tell
 *    the steward one line (a wake event; /forget and /restore); isRoot(agent): whether a command
 *    came from the root session's UI (absent: every UI); status(): /status's text (absent: no
 *    /status); channels: the owner's side of the chat channels (local-channels.mjs `owner`:
 *    issueCode, view, unbind, setDefault), absent without them.
 *  @returns the unregister functions. */
export function registerOwnerCommands(commands, { questions, answer, proposals, decide, memory, memoryError, wake, isRoot, status, channels }) {
  const defs = [
    { name: 'questions', description: 'List the open questions your director or steward asked you.',
      async handler() {
        const open = Object.entries(await questions()).filter(([, q]) => isRecord(q) && q.status === 'open').sort(byTime('asked_at'));
        if (!open.length) return ok('No open questions.');
        return ok([`Open questions (${open.length}):`, ...open.map(([id, q]) => `- ${id}: ${q.text}`
          + (q.context ? `\n  context: ${q.context}` : '') + `\n  asked: ${q.asked_at}`), '', 'Answer with: /answer <id> <text>'].join('\n'));
      } },
    { name: 'answer', description: 'Answer one open question: /answer <id> <text>. It wakes your director or steward.',
      input: { hint: '<id> <text>' },
      async handler({ rawInput }) {
        const [id, text] = split(rawInput);
        if (!id || !text) return fail('Usage: /answer <id> <text>');
        const result = await answer({ id, text, by: OWNER });
        return result.refused ? refusal(`Not answered ${id}`, result) : ok(`Answered ${id} (as ${result.answered_by}, ${result.answered_at}).`);
      } },
  ];
  if (proposals && decide) defs.push(
    { name: 'proposals', description: 'List the open proposals your steward made.',
      async handler() {
        const open = Object.entries(await proposals()).filter(([, p]) => isRecord(p) && p.status === 'open').sort(byTime('created_at'));
        if (!open.length) return ok('No open proposals.');
        return ok([`Open proposals (${open.length}):`, ...open.map(([id, p]) => proposalLine(id, p)),
        '', 'Approve with /approve <id>, decline with /decline <id> [reason].'].join('\n'));
      } },
    { name: 'approve', description: 'Approve one open proposal and run it now: /approve <id>.', input: { hint: '<id>' },
      async handler({ rawInput }) {
        const [id] = split(rawInput);
        if (!id) return fail('Usage: /approve <id>');
        const result = await decide({ id, approve: true, by: OWNER });
        if (result.refused) return refusal(`Not approved ${id}`, result);
        return result.status === 'done'
          ? ok(`Approved ${id}: created project ${result.result?.project_id} at ${result.result?.root} with ${result.result?.work_items} work item(s).`)
          : fail(`Approved ${id}, but it failed: ${result.result?.error} - ${result.result?.reason}`);
      } },
    { name: 'decline', description: 'Decline one open proposal: /decline <id> [reason].', input: { hint: '<id> [reason]' },
      async handler({ rawInput }) {
        const [id, reason] = split(rawInput);
        if (!id) return fail('Usage: /decline <id> [reason]');
        const result = await decide({ id, approve: false, ...(reason ? { reason } : {}), by: OWNER });
        return result.refused ? refusal(`Not declined ${id}`, result) : ok(`Declined ${id}.`);
      } });
  /** One line to the steward, after the memory changed (whitespace typed in a reason is folded,
   *  so it stays one line); a failed wake is said, never thrown: the change is already made. */
  const tell = async line => {
    try { await wake?.(line.replace(/\s+/g, ' ')); return ''; }
    catch (error) { return ` Your steward was not told: ${error?.message ?? error}.`; }
  };
  // id -> until when its confirmation is accepted; filled only by a warning the owner was shown.
  const purgeWarned = new Map();
  if (memory) defs.push(
    { name: 'profile', description: 'Show your resident profile, its version and the file to edit.',
      async handler() {
        const p = await memory.profile();
        return ok([`Profile (version ${p.version}; ${p.text.length} of ${p.limit} characters).`,
          `To change it, edit ${memory.dir}/profile.md: the next read records your edit as yours.`, '',
          p.text.trim() ? p.text : '(empty)'].join('\n'));
      } },
    { name: 'purge', description: `Purge one memory entry from every version, for good: /purge <id>, then /purge <id> ${PURGE_WORD}.`,
      input: { hint: '<id>' },
      async handler({ rawInput }) {
        const [id, word] = split(rawInput);
        if (!id) return fail(`Usage: /purge <id> (then /purge <id> ${PURGE_WORD} to confirm)`);
        if (word && word !== PURGE_WORD) return fail(`Nothing purged: the confirmation word is ${PURGE_WORD} (/purge ${id} ${PURGE_WORD}).`);
        // The word counts only after the warning (design 3.6: type it a second time).
        if (!word || !(purgeWarned.get(id) > Date.now())) {
          purgeWarned.set(id, Date.now() + PURGE_CONFIRM_MS);
          return ok(`Nothing purged yet. Purging ${id} cannot be undone: its text is removed from the whole history of your memory. To go ahead, send within 5 minutes: /purge ${id} ${PURGE_WORD}`);
        }
        purgeWarned.delete(id);
        const result = await memory.purge([id], { by: OWNER });
        return result.purged === true ? ok(`Purged ${id}: ${JSON.stringify(result)}`) : fail(`Not purged ${id}: ${JSON.stringify(result)}`);
      } },
    { name: 'forget', description: 'Forget one memory entry (recoverable with /restore): /forget <id> [reason]. It tells your steward.',
      input: { hint: '<id> [reason]' },
      async handler({ rawInput }) {
        const [id, given] = split(rawInput);
        if (!id) return fail('Usage: /forget <id> [reason]');
        const reason = given || 'no reason given';
        const result = await memory.forget([id], { reason, by: OWNER });
        if (result.refused) return refusal(`Not forgotten ${id}`, result);
        if (result.unknown?.includes(id)) return fail(`Not forgotten ${id}: unknown_id - your memory has no entry ${id}`);
        // A purged id is still on the store's forgotten records (flagged purged), so forget calls
        // it `already`; only one still on the restorable list may be offered /restore.
        if (result.already?.includes(id)) return memory.forgottenList().some(r => r.id === id)
          ? fail(`Not forgotten ${id}: already_forgotten - /restore ${id} brings it back`)
          : fail(`Not forgotten ${id}: purged - it was removed for good and cannot be restored`);
        const told = await tell(`The owner forgot memory entry ${id} with /forget (reason: ${reason}); it is out of search and reads until they /restore it.`);
        return ok(`Forgotten ${id}: it is out of search and reads; /restore ${id} brings it back, /purge ${id} removes it for good (${result.note}; remote: ${result.remote_forget}).${told}`);
      } },
    { name: 'restore', description: 'Bring back one forgotten memory entry: /restore <id>. It tells your steward.', input: { hint: '<id>' },
      async handler({ rawInput }) {
        const [id] = split(rawInput);
        if (!id) return fail('Usage: /restore <id>');
        const result = await memory.restore(id, OWNER);
        if (result.refused) return refusal(`Not restored ${id}`, result);
        const told = await tell(`The owner restored memory entry ${id} with /restore; it is back in search and reads.`);
        return ok(`Restored ${id}: it is back in search and reads.${told}`);
      } });
  else if (memoryError) {
    // Configured but not open: the commands stay, and say why, instead of vanishing.
    const unavailable = async () => fail(`Your memory is not available: ${memoryError}`);
    defs.push({ name: 'profile', description: 'Show your resident profile (your memory is not available now).', handler: unavailable },
      { name: 'purge', description: 'Purge one memory entry (your memory is not available now).', input: { hint: '<id>' }, handler: unavailable },
      { name: 'forget', description: 'Forget one memory entry (your memory is not available now).', input: { hint: '<id> [reason]' }, handler: unavailable },
      { name: 'restore', description: 'Restore one forgotten memory entry (your memory is not available now).', input: { hint: '<id>' }, handler: unavailable });
  }
  if (status) defs.push({ name: 'status', description: 'Show an overview: the last phase, the work items, the open questions and proposals, and the projects.',
    async handler() {
      try { return ok(await status()); } catch (error) { return fail(`No status: ${error?.message ?? error}`); }
    } });
  if (channels) {
    const channelArg = raw => { const [name] = split(raw); return CHANNEL_NAMES.includes(name.toLowerCase()) ? name.toLowerCase() : undefined; };
    defs.push(
      { name: 'bind', description: 'Bind your own Slack or WeChat account to your steward: /bind <slack|wechat> gives a one-time code to send from it.',
        input: { hint: '<slack|wechat>' },
        async handler({ rawInput }) {
          const channel = channelArg(rawInput);
          if (!channel) return fail('Usage: /bind <slack|wechat>');
          const issued = channels.issueCode(channel);
          if (issued.refused) return refusal('No code', issued);
          const seen = (await channels.view()).channels.find(row => row.channel === channel);
          return ok([`Send this code to the bot from your own ${CHANNEL_LABEL[channel]} account before ${issued.expires_at}: ${issued.code}`,
            'It works once, and only for the account that sends it; a new /bind replaces it. The bot answers whether it bound.',
            ...(seen?.bound ? [`${CHANNEL_LABEL[channel]} is bound to ${seen.binding.sender_ref} now: the account that sends the code replaces it.`] : []),
            ...(seen?.status ? [] : [`No ${CHANNEL_LABEL[channel]} channel plugin has reported yet: until it runs, nothing reaches the bot (/channels).`])].join('\n'));
        } },
      { name: 'channels', description: 'Show your chat channels: who is bound, the default channel, each one\'s status and the WeChat login QR.',
        async handler() {
          const view = await channels.view();
          const lines = ['Chat channels:'];
          for (const row of view.channels) {
            const label = CHANNEL_LABEL[row.channel];
            const who = row.bound ? `bound to ${row.binding.sender_ref} since ${row.binding.bound_at}`
              : row.binding?.revoked_at ? `not bound (unbound at ${row.binding.revoked_at})` : 'not bound';
            const state = !row.status ? 'its plugin has not reported (not loaded, or not started yet)'
              : `${row.status.connected ? 'connected' : 'not connected'}${row.status.detail ? ` (${row.status.detail})` : ''}, reported ${row.status.at}`;
            lines.push(`- ${label}: ${who}; ${state}`);
            if (row.code_until) lines.push(`  a binding code is waiting until ${row.code_until}`);
            if (row.qr) lines.push(`  login QR: ${row.qr} - open it and scan it with the WeChat account the bot uses (it is valid for about 2 minutes)`);
          }
          if (view.status_error) lines.push(`(status.json is unreadable: ${view.status_error})`);
          lines.push(view.default_channel ? `Default channel: ${CHANNEL_LABEL[view.default_channel]} - your steward's own messages go there when they answer no chat message.`
            : 'Default channel: none - your steward\'s own messages go to a chat only when they answer one (/default <channel> sets one).',
          'Bind with /bind <slack|wechat>, unbind with /unbind <channel>.');
          return ok(lines.join('\n'));
        } },
      { name: 'unbind', description: 'Unbind a chat account from your steward: /unbind <slack|wechat>.', input: { hint: '<slack|wechat>' },
        async handler({ rawInput }) {
          const channel = channelArg(rawInput);
          if (!channel) return fail('Usage: /unbind <slack|wechat>');
          const result = await channels.unbind(channel);
          if (result.refused) return refusal(`Not unbound ${channel}`, result);
          return result.unbound ? ok(`Unbound ${CHANNEL_LABEL[channel]} (${result.sender_ref}): its messages no longer reach your steward.`)
            : ok(`${CHANNEL_LABEL[channel]} is not bound.`);
        } },
      { name: 'default', description: 'Choose where your steward\'s own messages go: /default <slack|wechat|none>.', input: { hint: '<slack|wechat|none>' },
        async handler({ rawInput }) {
          const [word] = split(rawInput);
          const channel = word.toLowerCase() === 'none' ? null : channelArg(rawInput);
          if (channel === undefined) return fail('Usage: /default <slack|wechat|none>');
          const result = await channels.setDefault(channel);
          if (result.refused) return refusal('Not changed', result);
          return ok(channel ? `Default channel: ${CHANNEL_LABEL[channel]}.` : 'No default channel: your steward\'s own messages stay here unless they answer a chat message.');
        } });
  }
  const rootOnly = handler => async invocation => isRoot && !isRoot(invocation?.agent)
    ? fail('This command answers only in your director\'s own session, where you talk to it.') : handler(invocation);
  return defs.map(def => commands.register({ ...def, handler: rootOnly(def.handler) }));
}
